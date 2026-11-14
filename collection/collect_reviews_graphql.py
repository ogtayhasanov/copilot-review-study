#!/usr/bin/env python3
"""Batched, resumable native GitHub review collection on the existing checkpoint.

Use in the extracted copilot_completion folder. Completed REST/validation PRs
are preserved. An unfinished PR switches to cursor pagination on its first
successful GraphQL page. No third-party Python packages are required.
"""

import argparse
from collections import defaultdict, deque
from datetime import datetime
import getpass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from collect_all_reviews import (GitHub, SafeRedirect, StopCollection, RequestFailure,
    counts, export_failures, import_seed, load_roster, lock_checkpoint, normalize,
    now, open_db, print_status, retry_delay, save_reviews, wait_until)

ENDPOINT = "https://api.github.com/graphql"
PREFLIGHT_VERSION = "graphql-review-v1"
UNAVAILABLE = {"NOT_FOUND", "FORBIDDEN", "RESOURCE_NOT_ACCESSIBLE"}
FIELDS = """fullDatabaseId url submittedAt state body
            author { login __typename } commit { oid }"""


class RetryBatch(Exception):
    pass


class RestartPR(Exception):
    pass


def error_type(error):
    return str(error.get("type") or (error.get("extensions") or {}).get("code") or "UNKNOWN").upper()


def build_query(jobs):
    """jobs are (repo/pr key, opaque after cursor or None)."""
    groups = defaultdict(list)
    for index, (key, cursor) in enumerate(jobs):
        groups[key[0]].append((f"p{index}", key, cursor))
    aliases, repositories = {}, []
    for index, (repo, group) in enumerate(groups.items()):
        ra = f"r{index}"
        owner, name = (json.dumps(s) for s in repo.split("/", 1))
        prs = []
        for pa, key, cursor in group:
            aliases[ra, pa] = (key, cursor)
            after = ", after:" + json.dumps(cursor) if cursor is not None else ""
            prs.append(f"{pa}:pullRequest(number:{key[1]}) {{ number reviews(first:100{after}) {{ "
                       f"totalCount nodes {{ {FIELDS} }} pageInfo {{ hasNextPage endCursor }} }} }}")
        repositories.append(f"{ra}:repository(owner:{owner},name:{name}) {{ nameWithOwner {' '.join(prs)} }}")
    return "query { rateLimit { cost remaining resetAt limit } " + " ".join(repositories) + " }", aliases


def normalize_node(node):
    if not isinstance(node, dict):
        raise ValueError("Null or malformed review node")
    value = node.get("fullDatabaseId")
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value or "")):
        match = re.search(r"#pullrequestreview-([0-9]+)$", str(node.get("url") or ""))
        if not match:
            raise ValueError("Review lacks an exact native database ID")
        value = match.group(1)
    if int(value) <= 0:
        raise ValueError("Review has an invalid database ID")
    actor = node.get("author") or {}
    commit = node.get("commit") or {}
    return normalize({"id": int(value), "user": {"login": actor.get("login"), "type": actor.get("__typename")},
                      "body": node.get("body"), "submitted_at": node.get("submittedAt"),
                      "commit_id": commit.get("oid"), "state": node.get("state")})


def decode_pages(data, errors, aliases):
    if not isinstance(data, dict) or not isinstance(errors, list):
        raise RetryBatch("Malformed GraphQL response")
    if any(not isinstance(e, dict) for e in errors):
        raise RetryBatch("Malformed GraphQL errors")
    if any(not isinstance(e.get("path"), list) or not e["path"] for e in errors):
        raise RetryBatch("GraphQL returned a top-level error")
    good, unavailable, retry = [], [], []
    for (ra, pa), (key, cursor) in aliases.items():
        relevant = [e for e in errors if e["path"][0] == ra
                    and (len(e["path"]) == 1 or e["path"][1] == pa)]
        if relevant:
            if all(error_type(e) in UNAVAILABLE for e in relevant):
                unavailable.append(key)
            else:
                retry.append((key, "GraphQL field error: " + ",".join(sorted({error_type(e) for e in relevant}))))
            continue
        if ra not in data:
            retry.append((key, "Missing repository alias"))
            continue
        repo = data[ra]
        if repo is None:
            unavailable.append(key)
            continue
        if not isinstance(repo, dict) or pa not in repo:
            retry.append((key, "Missing PR alias"))
            continue
        pr = repo[pa]
        if pr is None:
            unavailable.append(key)
            continue
        try:
            if not isinstance(pr, dict) or pr.get("number") != key[1]:
                raise ValueError("PR number differs from requested key")
            conn = pr["reviews"]
            info, nodes, total = conn["pageInfo"], conn["nodes"], conn["totalCount"]
            if not isinstance(nodes, list) or len(nodes) > 100:
                raise ValueError("Invalid review nodes")
            if not isinstance(total, int) or isinstance(total, bool) or total < len(nodes):
                raise ValueError("Invalid review totalCount")
            if type(info["hasNextPage"]) is not bool:
                raise ValueError("Invalid hasNextPage")
            end, more = info["endCursor"], info["hasNextPage"]
            if more and (not isinstance(end, str) or not end or end == cursor or not nodes):
                raise ValueError("Review pagination did not advance")
            rows = [normalize_node(n) for n in nodes]
            if len({r["review_id"] for r in rows}) != len(rows):
                raise ValueError("Duplicate native IDs in a review page")
            good.append({"key": key, "after": cursor, "end": end, "more": more,
                         "total": total, "rows": rows, "canonical": repo.get("nameWithOwner") or key[0]})
        except (KeyError, TypeError, ValueError) as exc:
            retry.append((key, str(exc)))
    return good, unavailable, retry


class GraphQL:
    def __init__(self, token, interval=0.4):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "Content-Type": "application/json", "User-Agent": "copilot-batched-review-audit"}
        self.opener = urllib.request.build_opener(SafeRedirect())
        self.interval, self.ready, self.rate_wait = interval, 0.0, 0.0
        self.calls, self.last_cost, self.remaining = 0, None, None
        self.secret = token

    def fetch(self, query):
        rate_attempt = 0
        while True:
            if self.rate_wait > time.time():
                print(f"GitHub quota pause: {(self.rate_wait-time.time())/60:.1f} minutes; resumes automatically.", flush=True)
                wait_until(self.rate_wait)
            time.sleep(max(0, self.ready - time.monotonic()))
            self.ready = time.monotonic() + self.interval
            request = urllib.request.Request(ENDPOINT, json.dumps({"query": query}).encode(), self.headers)
            self.calls += 1
            try:
                with self.opener.open(request, timeout=60) as response:
                    body, headers = response.read(), response.headers
            except urllib.error.HTTPError as exc:
                with exc:
                    status, headers, hint = exc.code, exc.headers, exc.read(8192).lower()
                if status == 401:
                    raise StopCollection("GitHub rejected the token (401). Saved progress retained.") from None
                if status == 429 or (status == 403 and (headers.get("Retry-After")
                        or headers.get("X-RateLimit-Remaining") == "0" or b"rate limit" in hint)):
                    if rate_attempt >= 8:
                        raise StopCollection("Repeated quota responses. Rerun later; progress retained.") from None
                    self.rate_wait = time.time() + retry_delay(headers, rate_attempt)
                    rate_attempt += 1
                    continue
                if status in (408, 500, 502, 503, 504):
                    raise RetryBatch(f"HTTP {status}") from None
                raise StopCollection(f"GraphQL returned HTTP {status}. Completed PRs are retained.") from None
            except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
                raise RetryBatch("Network interruption") from None
            try:
                result = json.loads(body)
            except (ValueError, UnicodeError):
                raise RetryBatch("Invalid GraphQL JSON") from None
            if not isinstance(result, dict):
                raise RetryBatch("Invalid GraphQL response")
            data, errors = result.get("data") or {}, result.get("errors") or []
            if not isinstance(data, dict) or not isinstance(errors, list):
                raise RetryBatch("Invalid GraphQL data/errors")
            rate_errors = [e for e in errors if isinstance(e, dict) and
                           (error_type(e) in {"RATE_LIMITED", "RATE_LIMIT_EXCEEDED"}
                            or "rate limit" in str(e.get("message", "")).lower())]
            if rate_errors:
                if rate_attempt >= 8:
                    raise StopCollection("Repeated GraphQL quota responses; checkpoint retained.")
                self.rate_wait = time.time() + retry_delay(headers, rate_attempt)
                rate_attempt += 1
                continue
            rate = data.get("rateLimit") or {}
            self.last_cost, self.remaining = rate.get("cost"), rate.get("remaining")
            if self.remaining == 0 or headers.get("X-RateLimit-Remaining") == "0":
                try:
                    reset = datetime.fromisoformat(str(rate["resetAt"]).replace("Z", "+00:00")).timestamp() + 3
                except (KeyError, ValueError, TypeError):
                    reset = time.time() + retry_delay(headers, 0)
                self.rate_wait = reset
            validation = [e for e in errors if isinstance(e, dict) and not e.get("path")
                          and (error_type(e) in {"UNDEFINEDFIELD", "UNDEFINEDARGUMENT", "GRAPHQL_VALIDATION_FAILED", "UNAUTHORIZED"}
                               or "doesn't exist" in str(e.get("message", "")))]
            if validation:
                msg = str(validation[0].get("message", "query rejected")).replace(self.secret, "[redacted]")[:220]
                raise StopCollection("GraphQL rejected the query: " + msg)
            return data, errors


def initialize_fast(db, checkpoint):
    """Create one recoverable backup before adding cursor tables."""
    backup = checkpoint.with_name(checkpoint.stem + "_before_fast.sqlite")
    if not backup.exists():
        temporary = backup.with_name(backup.name + ".tmp")
        print("Making a one-time checkpoint backup...", flush=True)
        target = sqlite3.connect(temporary)
        try:
            db.backup(target)
        finally:
            target.close()
        os.replace(temporary, backup)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS gql_cursor (
          repo TEXT NOT NULL,pr INTEGER NOT NULL,after_cursor TEXT,
          PRIMARY KEY(repo,pr));
        CREATE TABLE IF NOT EXISTS gql_pages (
          repo TEXT NOT NULL,pr INTEGER NOT NULL,after_cursor TEXT NOT NULL,
          end_cursor TEXT,id_digest TEXT NOT NULL,fetched_at TEXT NOT NULL,
          PRIMARY KEY(repo,pr,after_cursor));
    """)


def cursor_for(db, key):
    row = db.execute("SELECT g.after_cursor FROM gql_cursor g JOIN prs p USING(repo,pr) "
                     "WHERE g.repo=? AND g.pr=? AND p.source='github_graphql'", key).fetchone()
    return row[0] if row else None


def commit_page(db, page):
    key, after = page["key"], page["after"]
    state = db.execute("SELECT status FROM prs WHERE repo=? AND pr=?", key).fetchone()
    if state is None or state[0] == "complete":
        raise ValueError("Refusing to overwrite a completed or unknown PR")
    if cursor_for(db, key) != after:
        raise ValueError("Checkpoint cursor differs from requested page")
    ids = [r["review_id"] for r in page["rows"]]
    digest = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
    with db:
        if after is None:
            # Only this unfinished PR restarts when changing transport. Every
            # completed PR is preserved byte for byte by the status guard.
            db.execute("DELETE FROM reviews WHERE repo=? AND pr=?", key)
            db.execute("DELETE FROM pages WHERE repo=? AND pr=?", key)
            db.execute("DELETE FROM gql_pages WHERE repo=? AND pr=?", key)
        elif ids:
            marks = ",".join("?" for _ in ids)
            if db.execute(f"SELECT 1 FROM reviews WHERE repo=? AND pr=? AND review_id IN ({marks}) LIMIT 1",
                          (*key, *ids)).fetchone():
                raise RestartPR("Overlapping cursor pages")
        if page["more"] and db.execute(
                "SELECT 1 FROM gql_pages WHERE repo=? AND pr=? AND end_cursor=?", (*key, page["end"])).fetchone():
            raise RestartPR("Repeated pagination cursor")
        save_reviews(db, key, page["rows"])
        total = db.execute("SELECT COUNT(*) FROM reviews WHERE repo=? AND pr=?", key).fetchone()[0]
        if total > page["total"] or (not page["more"] and total != page["total"]):
            raise RestartPR("Review count changed during pagination")
        db.execute("INSERT OR REPLACE INTO gql_pages VALUES (?,?,?,?,?,?)",
                   (*key, after or "", page["end"], digest, now()))
        db.execute("INSERT OR REPLACE INTO gql_cursor VALUES (?,?,?)", (*key, page["end"]))
        db.execute("UPDATE prs SET status=?,next_page=1,source='github_graphql',completed_at=?,"
                   "canonical_repo=?,error=NULL WHERE repo=? AND pr=?",
                   ("running" if page["more"] else "complete", None if page["more"] else now(), page["canonical"].lower(), *key))


def restart_incomplete(db, key):
    if db.execute("SELECT status FROM prs WHERE repo=? AND pr=?", key).fetchone()[0] == "complete":
        raise ValueError("Cannot restart a completed PR")
    with db:
        db.execute("DELETE FROM gql_cursor WHERE repo=? AND pr=?", key)
        db.execute("DELETE FROM gql_pages WHERE repo=? AND pr=?", key)
        db.execute("UPDATE prs SET status='pending' WHERE repo=? AND pr=?", key)
    # Old partial review rows remain until a replacement first page succeeds.


def preflight(db, client, token):
    existing = db.execute("SELECT value FROM metadata WHERE key='fast_preflight'").fetchone()
    if existing and existing[0] == PREFLIGHT_VERSION:
        return
    preferred = ("iomete/iom-docs", 630)
    row = db.execute("SELECT repo,pr FROM prs WHERE repo=? AND pr=?", preferred).fetchone()
    if row is None:
        row = db.execute("SELECT repo,pr FROM prs WHERE status='complete' ORDER BY repo,pr LIMIT 1").fetchone()
    if row is None:
        row = db.execute("SELECT repo,pr FROM prs ORDER BY repo,pr LIMIT 1").fetchone()
    key = tuple(row)
    print(f"Checking native REST/GraphQL equivalence on {key[0]}#{key[1]}...", flush=True)
    native, after = [], None
    while True:
        query, aliases = build_query([(key, after)])
        data, errors = client.fetch(query)
        good, unavailable, retry = decode_pages(data, errors, aliases)
        if unavailable or retry or len(good) != 1:
            raise StopCollection("Preflight could not read the verification PR; saved data has not been replaced.")
        p = good[0]
        native.extend(p["rows"])
        if not p["more"]:
            if len({r["review_id"] for r in native}) != p["total"]:
                raise StopCollection("Preflight review counts changed; rerun to check again.")
            break
        after = p["end"]
    rest_client, rest, number = GitHub(token), [], 1
    while True:
        raw, headers, _ = rest_client.fetch(*key, number)
        rest.extend(normalize(r) for r in raw)
        if len(raw) < 100 and 'rel="next"' not in (headers.get("Link") or ""):
            break
        number += 1
    fields = ("submitted_at", "commit_id", "state", "is_copilot", "account_type", "assessment", "body_sha256")
    def comparable(rows):
        result = {}
        for r in rows:
            record = [r.get(k) for k in fields]
            # Native timestamps from the two APIs can use Z or +00:00.
            if record[0]:
                record[0] = datetime.fromisoformat(record[0].replace("Z", "+00:00")).isoformat()
            result[r["review_id"]] = record
        return result
    if comparable(native) != comparable(rest):
        raise StopCollection("REST/GraphQL preflight differed. Collection stopped safely; keep the checkpoint.")
    with db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('fast_preflight',?)", (PREFLIGHT_VERSION,))
    print(f"Preflight passed: {len(native)} native reviews match on all analysis fields.", flush=True)


def collect(db, client, keys, batch_size=20):
    queue = deque(keys)
    failures, restarts = defaultdict(int), defaultdict(int)
    started, last_report, processed, completed, healthy = time.monotonic(), 0.0, 0, 0, 0
    current_batch, system_failures = batch_size, 0
    while queue:
        batch = [queue.popleft() for _ in range(min(current_batch, len(queue)))]
        jobs = [(key, cursor_for(db, key)) for key in batch]
        query, aliases = build_query(jobs)
        try:
            data, errors = client.fetch(query)
            good, unavailable, retry = decode_pages(data, errors, aliases)
            system_failures = 0
        except RetryBatch as exc:
            system_failures += 1
            if system_failures >= 6:
                raise StopCollection(f"Six consecutive API/network failures ({exc}); progress retained. Rerun later.")
            good, unavailable, retry = [], [], [(key, str(exc)) for key in batch]
        for key in unavailable:
            with db:
                db.execute("UPDATE prs SET status='unavailable',error='Unavailable to GraphQL token' WHERE repo=? AND pr=?", key)
            processed += 1
        for page in good:
            key = page["key"]
            try:
                commit_page(db, page)
            except RestartPR as exc:
                restarts[key] += 1
                if restarts[key] <= 2:
                    restart_incomplete(db, key)
                    queue.appendleft(key)
                else:
                    retry.append((key, str(exc)))
                continue
            if page["more"]:
                queue.appendleft(key)
            else:
                processed += 1
                completed += 1
            failures[key] = 0
        if retry:
            healthy = 0
            if current_batch > 1:
                current_batch = max(1, min(current_batch, len(batch)) // 2)
                print(f"Retrying affected PRs in batches of {current_batch} ({retry[0][1]}).", flush=True)
            worst = 0
            for key, reason in reversed(retry):
                failures[key] += 1
                worst = max(worst, failures[key])
                if failures[key] >= 6:
                    with db:
                        db.execute("UPDATE prs SET status='error',error=? WHERE repo=? AND pr=?", (reason, *key))
                    processed += 1
                    continue
                queue.appendleft(key)
            if queue:
                time.sleep(min(2 ** worst, 30))
        else:
            healthy += 1
            if healthy >= 5 and current_batch < batch_size:
                current_batch = min(batch_size, current_batch * 2)
                healthy = 0
        elapsed = time.monotonic() - started
        if time.monotonic() - last_report >= 20 or not queue:
            print_status(db)
            speed = completed * 3600 / elapsed if elapsed > 0 else 0
            left = counts(db)["pending"] + counts(db)["running"]
            eta = f"{left / speed:.1f}h" if completed >= 50 and speed else "measuring"
            print(f"Fast run: {processed:,}/{len(keys):,} processed, {client.calls:,} GraphQL requests, "
                  f"{speed:,.0f} complete PRs/h, ETA {eta}; batch {current_batch}, "
                  f"last cost {client.last_cost}, quota left {client.remaining}.", flush=True)
            last_report = time.monotonic()
    return processed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    p.add_argument("--checkpoint", type=Path, default=Path("reviews.sqlite"))
    p.add_argument("--seed", type=Path)
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("--limit", type=int)
    p.add_argument("--retry-failures", action="store_true")
    args = p.parse_args()
    if not 1 <= args.batch_size <= 40 or (args.limit is not None and args.limit <= 0):
        p.error("batch-size must be 1..40 and limit must be positive")
    db = None
    try:
        keys = load_roster(args.input)
        with lock_checkpoint(args.checkpoint):
            db = open_db(args.checkpoint, keys)
            if args.seed:
                print(f"Imported {import_seed(db, args.seed):,} complete validation lists.", flush=True)
            print_status(db)
            if args.retry_failures:
                with db:
                    db.execute("UPDATE prs SET status='pending',error=NULL WHERE status IN ('unavailable','error')")
            pending = db.execute("SELECT repo,pr FROM prs WHERE status IN ('pending','running') ORDER BY repo,pr").fetchall()
            if args.limit:
                pending = pending[:args.limit]
            if not pending:
                print("No pending PRs. Use --retry-failures to retry failed or inaccessible PRs.")
                return 0
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or getpass.getpass("GitHub token (hidden input): ")
            token = token.strip()
            if not token:
                raise StopCollection("No token supplied; checkpoint retained.")
            initialize_fast(db, args.checkpoint)
            client = GraphQL(token)
            # No sampled/completed PR data are overwritten by this check.
            preflight(db, client, token)
            collect(db, client, pending, args.batch_size)
            print_status(db)
            print("Fast collection pass finished. Inaccessible PRs are reported separately.")
            return 0
    except KeyboardInterrupt:
        print("\nStopped. Successful pages are saved; rerun the same fast command to resume.")
        return 130
    except (StopCollection, RequestFailure, RetryBatch, ValueError, OSError, sqlite3.Error, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if db is not None:
            export_failures(db, args.checkpoint.with_name(args.checkpoint.stem + "_failures.csv"))
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
