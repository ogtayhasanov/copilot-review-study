#!/usr/bin/env python3
"""Recompute outcome and approval tables from completed native review lists."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from analyze import Verdict, full_sha, parse_time, read_states, read_verdicts, select_observations
from collect_all_reviews import load_roster

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 10, tzinfo=timezone.utc)
LABELS = {"READY": "ready", "NOT_READY": "not_ready", "ABSTAIN": "uncertain"}


def in_window(row):
    stamp = parse_time(row["submitted_at"])
    return stamp is not None and START <= stamp < END


def observation(key, reviews, state):
    events = sorted(Verdict(parse_time(r["submitted_at"]), LABELS[r["assessment"]],
                           r["commit_id"] or "") for r in reviews)
    result, reasons = select_observations({key: events}, {key: state})
    return (result[0] if result else None), reasons


def percentage(n, d):
    return f"{n:,}/{d:,} ({n / d:.1%})" if d else "n/a"


def compute(db, roster, states, archive_verdicts, archive_ids, allow_partial=False):
    db.row_factory = sqlite3.Row
    metadata = {(r["repo"], r["pr"]): dict(r) for r in db.execute("SELECT * FROM prs")}
    if set(metadata) != set(roster):
        raise ValueError("Checkpoint and input PR rosters differ")
    status = Counter(row["status"] for row in metadata.values())
    if (status["pending"] or status["running"]) and not allow_partial:
        raise ValueError("Collection has pending PRs. Resume it first, or use --allow-partial for a labeled pilot")
    counts = Counter(input_prs=len(roster), review_lists_complete=status["complete"])
    groups = defaultdict(Counter)
    formal = Counter()
    details = []
    exclusions = Counter()
    for key in roster:
        if metadata[key]["status"] != "complete":
            continue
        all_reviews = [dict(r) for r in db.execute(
            "SELECT * FROM reviews WHERE repo=? AND pr=?", key)]
        reviews = [r for r in all_reviews if r["is_copilot"] and r["assessment"] in LABELS and in_window(r)]
        counts["api_qualifying_reviews"] += len(reviews)
        absent = {r["review_id"] for r in reviews} - archive_ids.get(key, set())
        counts["qualifying_reviews_absent_from_export"] += len(absent)
        counts["prs_with_qualifying_reviews_absent_from_export"] += bool(absent)
        if not reviews:
            counts["complete_lists_without_current_qualifying_review"] += 1
        state = states.get(key)
        if state is None:
            counts["complete_lists_without_usable_state"] += 1
            continue
        counts["complete_lists_with_usable_state"] += 1
        selected, reasons = observation(key, reviews, state)
        exclusions.update(reasons)
        old, _ = select_observations({key: archive_verdicts.get(key, [])}, {key: state})
        old_label = old[0].label if old else "excluded"
        new_label = selected.label if selected else "excluded"
        counts["assessment_group_changed_from_export"] += old_label != new_label
        if state.state == "MERGED":
            counts["linked_merged_prs"] += 1
            if reviews:
                counts["merged_prs_with_current_qualifying_review"] += 1
                only_after = min(parse_time(r["submitted_at"]) for r in reviews) > state.end
                counts["all_current_qualifying_reviews_after_merge"] += only_after
            else:
                counts["merged_prs_without_current_qualifying_review"] += 1
        if selected:
            group = groups[selected.label]
            group["prs"] += 1
            group[state.state.lower()] += 1
            if state.state == "MERGED":
                match = selected.sha_match()
                group["comparable_merged"] += match is not None
                group["matching_heads"] += match is True
            details.append({"repo": key[0], "pr": key[1], "state": state.state,
                            "assessment": selected.label, "submitted_at": selected.last.isoformat(),
                            "review_sha": selected.review_sha, "saved_head": state.head,
                            "sha_match": selected.sha_match() if state.state == "MERGED" else "",
                            "review_list_source": metadata[key]["source"],
                            "reviews_completed_at": metadata[key]["completed_at"]})
        # Formal approval is measured independently of whether an assessment
        # heading was present. Native state is the state returned at retrieval.
        approvals = [r for r in all_reviews if r["is_copilot"] and r["state"] == "APPROVED"
                     and in_window(r) and (state.created is None or parse_time(r["submitted_at"]) >= state.created)
                     and (state.end is None or parse_time(r["submitted_at"]) <= state.end)]
        if approvals:
            formal["prs_with_copilot_approval_before_terminal"] += 1
            formal[state.state.lower()] += 1
            if state.state == "MERGED":
                formal["merged_bot_authored"] += state.author_type == "bot"
                user_approved = any(not r["is_copilot"] and r["account_type"].lower() == "user"
                                    and r["state"] == "APPROVED" and parse_time(r["submitted_at"]) is not None
                                    and (state.created is None or parse_time(r["submitted_at"]) >= state.created)
                                    and parse_time(r["submitted_at"]) <= state.end for r in all_reviews)
                formal["merged_without_current_user_approval_by_merge"] += not user_approved
                latest = max(parse_time(r["submitted_at"]) for r in approvals)
                shas = {full_sha(r["commit_id"]) for r in approvals if parse_time(r["submitted_at"]) == latest}
                sha = next(iter(shas)) if len(shas) == 1 else ""
                formal["merged_with_comparable_last_approval_sha"] += bool(sha and state.head)
                formal["merged_last_approval_sha_matches_saved_head"] += bool(sha and state.head and sha == state.head)
    total = Counter()
    for group in groups.values():
        total.update(group)
    groups["ALL"] = total
    return {"status": dict(status), "counts": dict(counts), "exclusions": dict(exclusions),
            "outcomes": {k: dict(groups[k]) for k in ("ready", "not_ready", "uncertain", "ALL")},
            "formal_approvals": dict(formal), "partial_collection": bool(status["pending"] or status["running"]),
            "review_window_utc": [START.isoformat(), END.isoformat()], "details": details}


def write_report(result, path):
    counts, formal = result["counts"], result["formal_approvals"]
    lines = ["# Native GitHub review-list results", "", "Coverage", "",
             f"- Input PRs: {counts['input_prs']:,}",
             f"- Complete review lists: {counts['review_lists_complete']:,}",
             f"- Collection statuses: {json.dumps(result['status'], sort_keys=True)}",
             f"- Complete lists without usable frozen state: {counts.get('complete_lists_without_usable_state', 0):,}", "",
             "A completed collection pass can still have inaccessible PRs. All tables below use available records; they are not a complete census unless coverage is complete.", "",
             "## Outcomes by last qualifying assessment", "",
             "| Assessment | PRs | Merged / all PRs | Matching heads / comparable merged PRs |",
             "|---|---:|---:|---:|"]
    for label, g in result["outcomes"].items():
        lines.append(f"| {label} | {g.get('prs', 0):,} | {percentage(g.get('merged', 0), g.get('prs', 0))} | "
                     f"{percentage(g.get('matching_heads', 0), g.get('comparable_merged', 0))} |")
    n = counts.get("all_current_qualifying_reviews_after_merge", 0)
    d = counts.get("merged_prs_with_current_qualifying_review", 0)
    lines += ["", f"Every current qualifying review submitted after merge: {percentage(n, d)}.",
              f"Merged PRs with no current qualifying review are reported separately: {counts.get('merged_prs_without_current_qualifying_review', 0):,}.", "",
              "## Formal Copilot approvals", "", "| Measure | PRs |", "|---|---:|"]
    for key in ("prs_with_copilot_approval_before_terminal", "merged", "closed", "open", "merged_bot_authored",
                "merged_without_current_user_approval_by_merge", "merged_with_comparable_last_approval_sha",
                "merged_last_approval_sha_matches_saved_head"):
        lines.append(f"| {key} | {formal.get(key, 0):,} |")
    lines += ["", "Approval states describe current API records and can reflect later dismissals. Account type is GitHub's classification, not proof of human or automated activity. SHA comparisons use the saved head; a historical head at merge is asserted only when independently verified.", "",
              "The archive export is an input frame and comparison source. Its incomplete-history sensitivity estimate of 17.55 points is excluded from this report.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("checkpoint", type=Path)
    p.add_argument("verdicts", type=Path)
    p.add_argument("states", type=Path)
    p.add_argument("recovered", type=Path)
    p.add_argument("--output-prefix", type=Path, default=Path("full_review_results"))
    p.add_argument("--allow-partial", action="store_true", help="label output as a pilot while collection is pending")
    args = p.parse_args()
    try:
        states = read_states(args.states).usable | read_states(args.recovered).usable
        verdicts, _, _ = read_verdicts(args.verdicts)
        ids = defaultdict(set)
        with args.verdicts.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                ids[row["repo"].lower(), int(row["pr"])].add(row["review_id"])
        db = sqlite3.connect(f"{args.checkpoint.resolve().as_uri()}?mode=ro", uri=True)
        try:
            result = compute(db, load_roster(args.verdicts), states, verdicts, ids, args.allow_partial)
        finally:
            db.close()
        args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
        args.output_prefix.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        write_report(result, args.output_prefix.with_suffix(".md"))
        with args.output_prefix.with_suffix(".csv").open("w", encoding="utf-8", newline="") as stream:
            fields = ["repo", "pr", "state", "assessment", "submitted_at", "review_sha", "saved_head",
                      "sha_match", "review_list_source", "reviews_completed_at"]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(result["details"])
        print(args.output_prefix.with_suffix(".md").read_text(encoding="utf-8"))
        print("Saved", *(str(args.output_prefix.with_suffix(s)) for s in (".json", ".csv", ".md")))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        p.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
