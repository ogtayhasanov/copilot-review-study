#!/usr/bin/env python3
"""Analyze GitHub PR snapshots with a PR roster or Copilot verdicts."""

import argparse
import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median


CLASSES = ("ready", "not_ready", "uncertain", "approved")
VERDICT_COLUMNS = {"repo", "pr", "created_at", "class", "commit_id"}
STATE_COLUMNS = {"repo", "pr", "found", "state", "merged_at", "closed_at", "head_sha"}
YES = {"yes", "true", "1"}
NO = {"no", "false", "0"}
PRKey = tuple[str, int]


def csv_rows(path, required, audit):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file, strict=True)
        headers = [name.strip().lower() for name in next(reader, [])]
        missing = sorted(required - set(headers))
        if missing or len(headers) != len(set(headers)):
            raise ValueError(f"{path}: expected unique column names; missing {missing}. Got {headers}.")
        while True:
            try:
                values = next(reader)
            except StopIteration:
                return
            except csv.Error:
                audit["malformed rows"] += 1
                continue
            if not values:
                continue
            if len(values) != len(headers):
                audit["malformed rows"] += 1
                continue
            yield dict(zip(headers, (value.strip() for value in values)))


def pr_key(row) -> PRKey | None:
    repo = row["repo"].lower()
    number = row["pr"]
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", repo):
        return None
    if not re.fullmatch(r"[0-9]{1,10}", number) or not 0 < int(number) <= 2_147_483_647:
        return None
    return repo, int(number)


def read_pr_keys(path):
    audit = Counter()
    keys = set()
    for row in csv_rows(path, {"repo", "pr"}, audit):
        key = pr_key(row)
        if key is None:
            audit["invalid PR keys"] += 1
        else:
            keys.add(key)
    if not keys:
        raise ValueError(f"{path} contains no valid PR keys.")
    return keys, audit


def parse_time(value):
    if not value or not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", value):
        return None
    try:
        stamp = datetime.fromisoformat(value.removesuffix(" UTC"))
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def full_sha(value):
    value = (value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) and value.strip("0"):
        return value
    return ""


@dataclass(frozen=True, order=True)
class Verdict:
    time: datetime
    label: str
    commit: str


def read_verdicts(path):
    audit = Counter()
    raw_labels = Counter()
    by_pr = defaultdict(dict)
    for row in csv_rows(path, VERDICT_COLUMNS, audit):
        raw_labels[row["class"] or "(empty)"] += 1
        key = pr_key(row)
        if key is None:
            audit["invalid PR keys"] += 1
            continue
        events = by_pr[key]
        label = re.sub(r"[\s-]+", "_", row["class"].lower())
        if label == "abstain":
            label = "uncertain"
        if label not in CLASSES:
            audit["rows outside the verdict taxonomy"] += 1
            continue
        stamp = parse_time(row["created_at"])
        if stamp is None:
            audit["invalid verdict timestamps"] += 1
            continue
        verdict = Verdict(stamp, label, row["commit_id"].lower())
        identity = row.get("review_id") or verdict
        if identity not in events:
            events[identity] = verdict
        elif events[identity] == verdict:
            audit["duplicate verdict rows"] += 1
        else:
            if events[identity] is not None:
                audit["conflicting review IDs within a PR"] += 1
            events[identity] = None
    verdicts = {key: sorted(event for event in events.values() if event is not None)
                for key, events in by_pr.items()}
    return verdicts, raw_labels, audit


@dataclass(frozen=True)
class PullState:
    state: str
    created: datetime | None
    end: datetime | None
    head: str
    author: str
    author_type: str
    merger: str
    merger_type: str


def parse_state(row):
    state = row["state"].upper()
    if state not in {"MERGED", "CLOSED", "OPEN"}:
        return None
    created = parse_time(row.get("created_at"))
    end = None
    if state != "OPEN":
        end = parse_time(row["merged_at"] if state == "MERGED" else row["closed_at"])
        if end is None or (created is not None and end < created):
            return None
    if state != "MERGED" and row["merged_at"]:
        return None
    return PullState(state, created, end, full_sha(row["head_sha"]),
                     row.get("author_login", "").lower(), row.get("author_type", "").lower(),
                     row.get("merged_by_login", "").lower(), row.get("merged_by_type", "").lower())


@dataclass
class StateFile:
    usable: dict[PRKey, PullState]
    seen: Counter
    unresolved: set[PRKey]
    invalid: set[PRKey]
    conflicts: set[PRKey]
    audit: Counter


def read_states(path):
    options = defaultdict(set)
    result = StateFile({}, Counter(), set(), set(), set(), Counter())
    for row in csv_rows(path, STATE_COLUMNS, result.audit):
        key = pr_key(row)
        if key is None:
            result.audit["invalid PR keys"] += 1
            continue
        result.seen[key] += 1
        found = row["found"].lower()
        if found in NO:
            result.unresolved.add(key)
            continue
        snapshot = parse_state(row) if found in YES else None
        if snapshot is None:
            result.invalid.add(key)
            continue
        options[key].add(snapshot)
    for key, snapshots in options.items():
        if len(snapshots) == 1:
            result.usable[key] = next(iter(snapshots))
        else:
            result.conflicts.add(key)
    return result


def rate(numerator, denominator):
    if not denominator:
        return "n/a"
    return f"{numerator:,}/{denominator:,} ({numerator / denominator:.1%})"


def print_coverage(keys, usable, sources, verdicts=None):
    seen = set().union(*(source.seen for source in sources))
    unresolved = set().union(*(source.unresolved for source in sources))
    invalid = set().union(*(source.invalid | source.conflicts for source in sources))
    missing = keys - usable.keys()
    bad = missing & invalid
    marked_no = (missing & unresolved) - bad
    print("COVERAGE AND DATA QUALITY")
    print(f"Distinct PR keys in input                         {len(keys):,}")
    if verdicts is not None:
        print(f"Keys with at least one usable dated verdict       {sum(map(bool, verdicts.values())):,}")
    print(f"Keys with usable PR state                         {rate(len(keys & usable.keys()), len(keys))}")
    print(f"No saved state row                                {len(missing - seen):,}")
    print(f"Only unresolved found=no rows                     {len(marked_no):,}")
    print(f"Unusable or conflicting saved rows                {len(bad):,}")
    print(f"Saved state keys outside the input                {len(seen - keys):,}")

    by_first = defaultdict(Counter)
    by_month = defaultdict(Counter)
    by_repo = defaultdict(Counter)
    for key in keys:
        bucket = by_repo[key[0]]
        bucket["total"] += 1
        bucket["usable"] += key in usable
    for key, events in (verdicts or {}).items():
        first_label = "no usable verdict"
        first_month = "no usable verdict"
        if events:
            first = events[0].time
            labels = {event.label for event in events if event.time == first}
            first_label = next(iter(labels)) if len(labels) == 1 else "ambiguous first"
            first_month = first.strftime("%Y-%m")
        for bucket in (by_first[first_label], by_month[first_month]):
            bucket["total"] += 1
            bucket["usable"] += key in usable
    for title, groups in (("First dated verdict", sorted(by_first.items())),
                          ("Month of first dated verdict (UTC)", sorted(by_month.items())),
                          ("Largest repositories by input count",
                           sorted(by_repo.items(), key=lambda item: (-item[1]["total"], item[0]))[:10])):
        if not groups:
            continue
        print(f"\n{title} | usable state / input PRs")
        for group, counts in groups:
            print(f"  {group:<38} {rate(counts['usable'], counts['total'])}")
    full = sum(counts["usable"] == counts["total"] for counts in by_repo.values())
    empty = sum(counts["usable"] == 0 for counts in by_repo.values())
    print(f"\nRepository coverage (full / partial / none)   {full:,} / {len(by_repo) - full - empty:,} / {empty:,}")
    if missing:
        print("Collection order and failures may bias the partial sample. These are observed-sample rates.")


def print_saved_outcomes(keys, usable):
    counts = Counter(usable[key].state for key in keys & usable.keys())
    total = sum(counts.values())
    print(f"\nSAVED OUTCOMES ({total:,} PRs with usable snapshots)")
    for state in ("MERGED", "CLOSED", "OPEN"):
        print(f"  {state:<8} {rate(counts[state], total)}")
    print(f"  Merged among terminal PRs   {rate(counts['MERGED'], counts['MERGED'] + counts['CLOSED'])}")
    return 0 if total else 1


@dataclass
class Observation:
    key: PRKey
    state: PullState
    last: datetime
    label: str
    review_sha: str
    verdict_count: int

    def sha_match(self):
        if self.review_sha and len(self.review_sha) == len(self.state.head):
            return self.review_sha == self.state.head
        return None

    def self_merge(self):
        if self.state.merger and self.state.author:
            return self.state.merger == self.state.author
        return None


def select_observations(verdicts, usable):
    observations = []
    exclusions = Counter()
    for key in verdicts.keys() & usable.keys():
        state = usable[key]
        events = [event for event in verdicts[key]
                  if (state.created is None or event.time >= state.created)
                  and (state.end is None or event.time <= state.end)]
        if not events:
            exclusions["no dated verdict in the PR's observed time interval"] += 1
            continue
        last = events[-1].time
        candidates = [event for event in events if event.time == last]
        labels = {event.label for event in candidates}
        if len(labels) > 1:
            exclusions["conflicting last verdict classes at the same timestamp"] += 1
            continue
        commits = {full_sha(event.commit) for event in candidates}
        sha = next(iter(commits)) if len(commits) == 1 else ""
        observations.append(Observation(key, state, last, next(iter(labels)), sha, len(events)))
    return observations, exclusions


def print_outcomes(observations):
    print(f"\nOUTCOMES BY LAST QUALIFYING COPILOT VERDICT ({len(observations):,} PRs)")
    print(f"{'Verdict':<11} {'PRs':>8} {'Merged':>8} {'Closed':>8} {'Open':>8} {'Merged / all':>25} {'Merged / terminal':>25}")
    groups = {label: [row for row in observations if row.label == label] for label in CLASSES}
    groups["ALL"] = observations
    for label, rows in groups.items():
        counts = Counter(row.state.state for row in rows)
        merged, closed, opened = (counts[state] for state in ("MERGED", "CLOSED", "OPEN"))
        print(f"{label:<11} {len(rows):>8,} {merged:>8,} {closed:>8,} {opened:>8,} "
              f"{rate(merged, len(rows)):>25} {rate(merged, merged + closed):>25}")

    print("\nMERGED PRS (shares among records with known fields)")
    print(f"{'Verdict':<11} {'SHA match':>24} {'Bot author':>24} {'Bot merger':>24} {'Self-merge':>24} {'Median hours':>13}")
    for label, rows in groups.items():
        merged_rows = [row for row in rows if row.state.state == "MERGED"]
        matches = [row.sha_match() for row in merged_rows]
        authors = [row.state.author_type for row in merged_rows]
        mergers = [row.state.merger_type for row in merged_rows]
        self_merges = [row.self_merge() for row in merged_rows]
        hours = [(row.state.end - row.last).total_seconds() / 3600 for row in merged_rows]
        stats = (rate(matches.count(True), len(matches) - matches.count(None)),
                 rate(authors.count("bot"), authors.count("bot") + authors.count("user")),
                 rate(mergers.count("bot"), mergers.count("bot") + mergers.count("user")),
                 rate(self_merges.count(True), len(self_merges) - self_merges.count(None)),
                 f"{median(hours):.2f}" if hours else "n/a")
        print(f"{label:<11} " + " ".join(f"{value:>24}" for value in stats[:4]) + f" {stats[4]:>13}")
    print("\nSHA match compares the reviewed commit with the saved PR head.")
    print(f"Missing or ambiguous review SHA among merged PRs   {sum(not r.review_sha for r in groups['ALL'] if r.state.state == 'MERGED'):,}")
    print(f"Missing head SHA among merged PRs                  {sum(not r.state.head for r in groups['ALL'] if r.state.state == 'MERGED'):,}")
    print(f"Multiple distinct observed qualifying verdicts     {rate(sum(r.verdict_count > 1 for r in observations), len(observations))}")
    print(f"Last verdict exactly at merge/closure timestamp    {sum(r.last == r.state.end for r in observations):,}")
    print("Human review activity requires separately timestamped review events.")


def write_details(path, observations):
    with path.open("x", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("repo", "pr", "state", "last_verdict", "last_verdict_at", "terminal_at",
                         "review_sha", "saved_head_sha", "sha_match", "verdict_count", "author_type",
                         "merged_by_type", "self_merge"))
        for row in sorted(observations, key=lambda observation: observation.key):
            state = row.state
            writer.writerow((*row.key, state.state, row.label, row.last.isoformat(),
                             state.end.isoformat() if state.end else "", row.review_sha, state.head,
                             row.sha_match(), row.verdict_count, state.author_type, state.merger_type,
                             row.self_merge() if state.state == "MERGED" else None))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Verdict CSV, or a repo/pr roster with --states-only.")
    parser.add_argument("states", type=Path)
    parser.add_argument("--states-only", action="store_true", help="Audit the roster and summarize saved outcomes.")
    parser.add_argument("--extra-states", type=Path, help="Recovered snapshots take precedence for overlapping keys.")
    parser.add_argument("--details", type=Path, help="Write a new PR-level CSV for a later review-event join.")
    args = parser.parse_args()
    if args.states_only and args.details:
        parser.error("--details requires verdicts")
    try:
        verdicts = None
        if args.states_only:
            keys, audit = read_pr_keys(args.input)
            if audit:
                print(f"PR roster input audit   {dict(sorted(audit.items()))}")
        else:
            verdicts, raw_labels, audit = read_verdicts(args.input)
            keys = verdicts.keys()
            print(f"Raw verdict classes (CSV rows)   {dict(sorted(raw_labels.items()))}")
            print(f"Verdict input audit   {dict(sorted(audit.items())) or 'clean'}")
        sources = []
        usable = {}
        for path in (args.states, args.extra_states):
            if path is None:
                continue
            source = read_states(path)
            sources.append(source)
            overlap = usable.keys() & source.usable.keys()
            print(f"{path} | {len(source.seen):,} distinct saved keys, "
                  f"{sum(count > 1 for count in source.seen.values()):,} duplicate keys, "
                  f"{len(source.conflicts):,} conflicting snapshot keys, "
                  f"{len(source.unresolved):,} keys ever marked found=no, "
                  f"{len(source.invalid):,} keys with invalid state rows")
            if source.audit:
                print(f"  Row audit   {dict(sorted(source.audit.items()))}")
            if overlap:
                print(f"  Recovered snapshot overrides for {len(overlap):,} overlapping keys.")
            usable.update(source.usable)
        print_coverage(keys, usable, sources, verdicts)
        if verdicts is None:
            return print_saved_outcomes(keys, usable)
        observations, exclusions = select_observations(verdicts, usable)
        print(f"\nOutcome exclusions after joining usable states   {dict(sorted(exclusions.items())) or 'none'}")
        print_outcomes(observations)
        if args.details:
            write_details(args.details, observations)
            print(f"\nWrote {args.details}")
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0 if observations else 1


if __name__ == "__main__":
    raise SystemExit(main())
