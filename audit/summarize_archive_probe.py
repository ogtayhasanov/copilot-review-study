#!/usr/bin/env python3
"""Check the 60-row BigQuery probe export and summarize its evidence."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("result_csv", type=Path)
    p.add_argument("--sample", type=Path, default=Path(__file__).with_name("missing_review_id_sample.csv"))
    args = p.parse_args()
    with args.sample.open(encoding="utf-8-sig", newline="") as f:
        expected = {r["review_id"]: r["kind"] for r in csv.DictReader(f)}
    counts = Counter()
    seen = set()
    with args.result_csv.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"review_id", "sample_kind", "raw_event_count", "rows_original_rules_would_emit"}
        if not required <= set(reader.fieldnames or []):
            p.error("CSV must be the final result of audit_missing_review_ids.sql")
        for r in reader:
            rid, kind = r["review_id"], r["sample_kind"]
            if rid not in expected or kind != expected[rid] or rid in seen:
                p.error("Unexpected or duplicate target ID in result")
            seen.add(rid)
            present = int(r["raw_event_count"]) > 0
            emits = int(r["rows_original_rules_would_emit"]) > 0
            counts[kind + "_tested"] += 1
            counts[kind + "_present_in_archive"] += present
            counts[kind + "_absent_in_archive_window"] += not present
            counts[kind + "_present_but_original_rules_drop"] += present and not emits
            counts[kind + "_original_rules_would_emit"] += emits
    if seen != expected.keys():
        p.error(f"Expected 60 target rows; {len(expected.keys() - seen)} IDs are missing from the exported result")
    result = {"counts": dict(counts), "all_controls_found": counts["positive_control_present_in_archive"] == 10,
              "interpretation": [
                  "Raw presence distinguishes IDs available in this archive window from IDs absent from that scan.",
                  "Present IDs dropped by the rules need lifecycle, heading-version, action, and reviewer inspection.",
                  "IDs that original rules would emit but are absent from the CSV need query/export provenance inspection.",
                  "Absence does not establish why an event was not captured, or that it was absent in every archive window.",
                  "Current API headings may have been added after the historical event; mixed causes are possible."]}
    output = args.result_csv.with_name("archive_probe_summary.json")
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    print("Saved", output)


if __name__ == "__main__":
    main()
