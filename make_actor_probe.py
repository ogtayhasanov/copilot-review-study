#!/usr/bin/env python3
"""Create one unfiltered archive-presence query for the API validation sample."""
import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("seed", type=Path, help="api_validation_seed.json from the completed validation")
    p.add_argument("--controls", type=Path, default=Path(__file__).parent/"data/probe_targets.csv")
    p.add_argument("--output", type=Path, default=Path("audit_review_capture_by_actor.sql"))
    args = p.parse_args()
    groups = defaultdict(set)
    prs = json.loads(args.seed.read_text(encoding="utf-8-sig"))
    start, end = datetime(2026,9,1,tzinfo=timezone.utc), datetime(2026,11,10,tzinfo=timezone.utc)
    for pr in prs:
        if not pr["complete"]: continue
        for r in pr["reviews"]:
            if not r.get("submitted_at"): continue
            t = datetime.fromisoformat(r["submitted_at"].replace("Z", "+00:00"))
            if not start <= t < end: continue
            rid = r["review_id"]
            if not str(rid).isdigit(): raise ValueError("Non-numeric native review ID")
            if r["is_copilot"]:
                groups["copilot_all_submitted"].add(rid)
                if r.get("assessment") in {"READY", "NOT_READY", "ABSTAIN"}:
                    groups["copilot_qualifying_assessments"].add(rid)
            elif (r.get("account_type") or "").lower() == "user":
                groups["non_copilot_user_account"].add(rid)
    with args.controls.open(encoding="utf-8-sig", newline="") as f:
        groups["known_present_controls"] = {r["review_id"] for r in csv.DictReader(f) if r["kind"] == "positive_control"}
    selects = []
    for kind, ids in sorted(groups.items()):
        values = ",\n    ".join("'" + rid + "'" for rid in sorted(ids, key=int))
        selects.append(f"  SELECT '{kind}' AS review_type, review_id FROM UNNEST([\n    {values}\n  ]) AS review_id")
    sql = """-- Run this entire SELECT once. It returns four summary rows.
-- All reviews are native submissions dated 1 Sep--9 Nov 2026 UTC.
-- Raw presence has no actor, reviewer, action, or heading filter.
-- Compare copilot_all_submitted with non_copilot_user_account for like-for-like
-- review-event coverage. User account type is not proof a person acted.
-- The two Copilot groups overlap. Each row counts unique IDs within its group.
WITH targets AS (
""" + "\n  UNION ALL\n".join(selects) + """
), present AS (
  SELECT DISTINCT JSON_VALUE(payload,'$.review.id') AS review_id
  FROM `githubarchive.day.2026*`
  WHERE _TABLE_SUFFIX BETWEEN '0901' AND '1109'
    AND type='PullRequestReviewEvent'
    AND JSON_VALUE(payload,'$.review.id') IN (SELECT review_id FROM targets)
)
SELECT t.review_type, COUNT(*) AS api_review_ids,
  COUNTIF(p.review_id IS NOT NULL) AS found_in_raw_archive,
  COUNTIF(p.review_id IS NULL) AS absent_from_raw_archive,
  ROUND(100*SAFE_DIVIDE(COUNTIF(p.review_id IS NULL),COUNT(*)),2) AS missing_percent
FROM targets t LEFT JOIN present p USING(review_id)
GROUP BY t.review_type ORDER BY t.review_type;
"""
    if len(sql.encode()) >= 1_000_000: raise ValueError("Probe exceeds the intended query-size bound")
    args.output.write_text(sql, encoding="utf-8")
    print(json.dumps({k:len(v) for k,v in groups.items()},indent=2))
    print("Saved",args.output,"— compare rates within the sampled PR frame, not all GH Archive studies.")


if __name__ == "__main__": main()
