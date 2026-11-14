#!/usr/bin/env python3
"""Fetch every review page for a frozen PR roster; resume from a SQLite checkpoint.

Python 3.10+; standard library only. A token is read from GH_TOKEN/GITHUB_TOKEN
or requested with hidden input. It is never saved. Complete validation lists
can be imported with --seed to avoid fetching those PRs twice.
"""

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import getpass
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


API = "https://api.github.com"
COPILOT = {"copilot", "copilot-pull-request-reviewer", "copilot-pull-request-reviewer[bot]"}
HEADING = re.compile(r"^\s*#{2,4}\s*(?:[🟢🟡🔵]\s*)?"
                     r"(Approval recommended|Changes recommended|Needs a closer look)\b", re.I | re.M)
LABELS = {"approval recommended": "READY", "changes recommended": "NOT_READY",
          "needs a closer look": "ABSTAIN"}
SCHEMA = "1"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StopCollection(Exception):
    pass


class RequestFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"HTTP {code}" if isinstance(code, int) else str(code))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise StopCollection("GitHub returned an unexpected redirect host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def normalize(row):
    if not isinstance(row, dict) or not isinstance(row.get("id"), int):
        raise ValueError("Review response lacks a numeric review ID")
    user = row.get("user") or {}
    login = str(user.get("login") or "").lower()
    body = row.get("body") or ""
    if not isinstance(body, str):
        raise ValueError("Invalid review body")
    match = HEADING.search(body)
    return {"review_id": str(row["id"]), "submitted_at": row.get("submitted_at"),
            "commit_id": row.get("commit_id"), "state": row.get("state"),
            "is_copilot": login in COPILOT, "account_type": user.get("type") or "",
            "assessment": LABELS[match.group(1).lower()] if match else None,
            "user_login": login, "body": body if login in COPILOT else None,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest() if login in COPILOT else None}


def load_roster(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"repo", "pr"} <= set(reader.fieldnames or []):
            raise ValueError("Input needs repo and pr columns")
        keys = set()
        for row in reader:
            repo, number = row["repo"].strip().lower(), row["pr"].strip()
            if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", repo) or not number.isdigit() or int(number) <= 0:
                raise ValueError("Invalid PR key in input; no rows were silently skipped")
            keys.add((repo, int(number)))
    if not keys:
        raise ValueError("Input roster is empty")
    return sorted(keys)


@contextmanager
def lock_checkpoint(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = Path(str(path) + ".lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise StopCollection("Another collector is using this checkpoint.") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise StopCollection("Another collector is using this checkpoint.") from None
        yield
    finally:
        stream.close()


def open_db(path, keys):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS prs (
          repo TEXT NOT NULL, pr INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          next_page INTEGER NOT NULL DEFAULT 1, completed_at TEXT, source TEXT,
          canonical_repo TEXT, error TEXT, PRIMARY KEY(repo,pr));
        CREATE TABLE IF NOT EXISTS reviews (
          repo TEXT NOT NULL, pr INTEGER NOT NULL, review_id TEXT NOT NULL,
          submitted_at TEXT, commit_id TEXT, state TEXT, is_copilot INTEGER NOT NULL,
          account_type TEXT, assessment TEXT, user_login TEXT, body TEXT, body_sha256 TEXT,
          PRIMARY KEY(repo,pr,review_id));
        CREATE TABLE IF NOT EXISTS pages (
          repo TEXT NOT NULL, pr INTEGER NOT NULL, page INTEGER NOT NULL,
          count INTEGER NOT NULL, id_digest TEXT NOT NULL, fetched_at TEXT NOT NULL,
          PRIMARY KEY(repo,pr,page));
    """)
    expected = {"schema_version": SCHEMA, "roster_sha256": hashlib.sha256(
        json.dumps(keys, separators=(",", ":")).encode()).hexdigest()}
    metadata = dict(db.execute("SELECT key,value FROM metadata"))
    for name, value in expected.items():
        if name in metadata and metadata[name] != value:
            db.close()
            raise ValueError("Checkpoint schema or PR roster differs; use the matching input or a new checkpoint")
    with db:
        db.executemany("INSERT OR IGNORE INTO metadata VALUES (?,?)", expected.items())
        db.executemany("INSERT OR IGNORE INTO prs(repo,pr) VALUES (?,?)", keys)
    return db


def save_reviews(db, key, reviews):
    columns = ("review_id", "submitted_at", "commit_id", "state", "is_copilot", "account_type",
               "assessment", "user_login", "body", "body_sha256")
    db.executemany("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   [(*key, *(row.get(c) for c in columns)) for row in reviews])


def import_seed(db, path):
    records = json.loads(path.read_text(encoding="utf-8"))
    imported = 0
    with db:
        for row in records:
            if not row.get("complete"):
                continue
            key = row["repo"].lower(), int(row["pr"])
            old = db.execute("SELECT status,next_page FROM prs WHERE repo=? AND pr=?", key).fetchone()
            if old != ("pending", 1) or db.execute(
                    "SELECT 1 FROM reviews WHERE repo=? AND pr=? LIMIT 1", key).fetchone():
                continue
            save_reviews(db, key, row["reviews"])
            db.execute("UPDATE prs SET status='complete',completed_at=?,source='validation_seed',"
                       "canonical_repo=?,error=NULL WHERE repo=? AND pr=?",
                       (row.get("fetched_at", "2026-11-12"), row.get("canonical_repo", key[0]), *key))
            imported += 1
    return imported


def counts(db):
    result = {name: 0 for name in ("pending", "running", "complete", "unavailable", "error")}
    result.update(dict(db.execute("SELECT status,COUNT(*) FROM prs GROUP BY status")))
    return result


def print_status(db):
    c = counts(db)
    total = sum(c.values())
    print(f"PR roster {total:,} | complete {c['complete']:,} | "
          f"pending {c['pending'] + c['running']:,} | unavailable {c['unavailable']:,} | errors {c['error']:,}", flush=True)
    return c


def wait_until(stamp):
    while stamp > time.time():
        time.sleep(min(60, stamp - time.time()))


def retry_delay(headers, attempt):
    delays = []
    value = headers.get("Retry-After")
    if value:
        try:
            delays.append(float(value))
        except ValueError:
            try:
                delays.append(parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    if headers.get("X-RateLimit-Remaining") == "0":
        try:
            delays.append(float(headers["X-RateLimit-Reset"]) - time.time() + 3)
        except (KeyError, ValueError, OverflowError):
            pass
    return max([x for x in delays if math.isfinite(x) and x > 0] or [min(60 * 2 ** attempt, 3600)])


class GitHub:
    def __init__(self, token, interval=0.8):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "copilot-complete-review-audit"}
        self.opener = urllib.request.build_opener(SafeRedirect())
        self.interval = interval
        self.ready = 0.0
        self.calls = 0
        self.rate_wait = 0.0

    def fetch(self, repo, pr, page):
        url = f"{API}/repos/{repo}/pulls/{pr}/reviews?per_page=100&page={page}"
        transient = rate_attempt = 0
        while True:
            if self.rate_wait > time.time():
                minutes = (self.rate_wait - time.time()) / 60
                print(f"GitHub rate-limit pause: {minutes:.1f} minutes; resumes automatically. Checkpoint retained.", flush=True)
                wait_until(self.rate_wait)
            time.sleep(max(0, self.ready - time.monotonic()))
            self.ready = time.monotonic() + self.interval
            self.calls += 1
            req = urllib.request.Request(url, headers=self.headers)
            try:
                with self.opener.open(req, timeout=45) as response:
                    body, headers, final_url = response.read(), response.headers, response.geturl()
            except urllib.error.HTTPError as exc:
                with exc:
                    status, headers = exc.code, exc.headers
                    hint = exc.read(8192).lower()
                if status == 401:
                    raise StopCollection("GitHub rejected the token (HTTP 401). Checkpoint retained.") from None
                if status == 429 or (status == 403 and (headers.get("Retry-After")
                        or headers.get("X-RateLimit-Remaining") == "0" or b"rate limit" in hint)):
                    if rate_attempt >= 8:
                        raise StopCollection("Repeated rate-limit responses. Checkpoint retained; rerun later.") from None
                    self.rate_wait = time.time() + retry_delay(headers, rate_attempt)
                    rate_attempt += 1
                    continue
                if status in (408, 500, 502, 503, 504):
                    transient += 1
                    if transient <= 6:
                        time.sleep(min(2 ** transient, 60))
                        continue
                raise RequestFailure(status) from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                transient += 1
                if transient <= 6:
                    time.sleep(min(2 ** transient, 60))
                    continue
                raise RequestFailure("network retries exhausted") from None
            try:
                reviews = json.loads(body)
            except (ValueError, UnicodeError):
                raise RequestFailure("invalid JSON response") from None
            if not isinstance(reviews, list) or len(reviews) > 100:
                raise RequestFailure("unexpected reviews response")
            if headers.get("X-RateLimit-Remaining") == "0":
                self.rate_wait = time.time() + retry_delay(headers, 0)
            return reviews, headers, final_url


def collect_one(db, client, key):
    page = db.execute("SELECT next_page FROM prs WHERE repo=? AND pr=?", key).fetchone()[0]
    with db:
        db.execute("UPDATE prs SET status='running',error=NULL WHERE repo=? AND pr=?", key)
    while page <= 10000:
        raw, headers, final_url = client.fetch(*key, page)
        rows = [normalize(row) for row in raw]
        ids = [row["review_id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise RequestFailure("duplicate review IDs within a page")
        digest = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        if rows and db.execute("SELECT 1 FROM pages WHERE repo=? AND pr=? AND id_digest=? LIMIT 1",
                               (*key, digest)).fetchone():
            raise RequestFailure("repeated review page; pagination not complete")
        # Link is authoritative when supplied. A full page without Link also
        # requires checking the next page, including an empty final page.
        more = 'rel="next"' in (headers.get("Link") or "") or len(raw) == 100
        canonical = re.search(r"/repos/([^/]+/[^/]+)/pulls/", final_url)
        with db:
            save_reviews(db, key, rows)
            db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?)",
                       (*key, page, len(rows), digest, now()))
            db.execute("UPDATE prs SET next_page=?,status=?,completed_at=?,source='github_rest',"
                       "canonical_repo=?,error=NULL WHERE repo=? AND pr=?",
                       (page + 1, "running" if more else "complete", None if more else now(),
                        canonical.group(1).lower() if canonical else key[0], *key))
        if not more:
            return
        page += 1
    raise RequestFailure("page safety limit reached; list is incomplete")


def export_failures(db, path):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("repo", "pr", "status", "next_page", "reason"))
        writer.writerows(db.execute("SELECT repo,pr,status,next_page,error FROM prs "
                                   "WHERE status IN ('unavailable','error') ORDER BY repo,pr"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="verdicts.csv or a repo/pr roster")
    parser.add_argument("--checkpoint", type=Path, default=Path("reviews.sqlite"))
    parser.add_argument("--seed", type=Path, help="normalized complete validation lists (optional)")
    parser.add_argument("--limit", type=int, help="process at most this many pending PRs for a pilot")
    parser.add_argument("--interval", type=float, default=0.8, help="minimum request-start interval in seconds")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args()
    if args.interval < 0.5 or (args.limit is not None and args.limit <= 0):
        parser.error("interval must be >=0.5 and limit must be positive")
    db = None
    try:
        keys = load_roster(args.input)
        with lock_checkpoint(args.checkpoint):
            db = open_db(args.checkpoint, keys)
            if args.seed:
                print(f"Imported {import_seed(db, args.seed):,} complete validation lists.", flush=True)
            if args.retry_failures:
                with db:
                    db.execute("UPDATE prs SET status='pending',error=NULL WHERE status IN ('unavailable','error')")
            status = print_status(db)
            if args.status_only:
                return 0
            pending = db.execute("SELECT repo,pr FROM prs WHERE status IN ('pending','running') ORDER BY repo,pr").fetchall()
            if args.limit:
                pending = pending[:args.limit]
            if not pending:
                print("No pending PRs. Use --retry-failures to retry inaccessible or failed PRs.")
                return 0 if not status["error"] else 2
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            if not token:
                token = getpass.getpass("GitHub token (hidden input): ")
            if not token.strip():
                raise StopCollection("No token supplied. Nothing was fetched; checkpoint retained.")
            client = GitHub(token.strip(), args.interval)
            started, last_report, consecutive_errors = time.monotonic(), 0.0, 0
            for index, key in enumerate(pending, 1):
                try:
                    collect_one(db, client, key)
                    consecutive_errors = 0
                except (RequestFailure, ValueError) as exc:
                    inaccessible = isinstance(exc, RequestFailure) and exc.code in (403, 404, 410, 451)
                    with db:
                        db.execute("UPDATE prs SET status=?,error=? WHERE repo=? AND pr=?",
                                   ("unavailable" if inaccessible else "error", str(exc), *key))
                    consecutive_errors = 0 if inaccessible else consecutive_errors + 1
                    if consecutive_errors >= 5:
                        raise StopCollection("Five consecutive request failures. Checkpoint retained; rerun to resume.")
                if time.monotonic() - last_report >= 30 or index == len(pending):
                    print_status(db)
                    print(f"This run: {index:,}/{len(pending):,} processed, {client.calls:,} requests, "
                          f"{(time.monotonic() - started) / 3600:.2f} hours.", flush=True)
                    last_report = time.monotonic()
            final = print_status(db)
            print("Collection pass finished. Inaccessible or failed PRs are not counted as complete.")
            return 0 if not final["error"] else 2
    except KeyboardInterrupt:
        print("\nStopped. Each completed page is saved; rerun the same command to resume.", flush=True)
        return 130
    except EOFError:
        print("Token input was closed. Rerun in PowerShell, or set GH_TOKEN before starting.", file=sys.stderr)
        return 1
    except (StopCollection, ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    finally:
        if db is not None:
            export_failures(db, args.checkpoint.with_name(args.checkpoint.stem + "_failures.csv"))
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
