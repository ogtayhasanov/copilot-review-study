"""Behavior checks for pagination, restart, rate delays, and native grouping."""
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from analyze import PullState, Verdict
from analyze_full_reviews import compute
from collect_all_reviews import RequestFailure, collect_one, counts, normalize, open_db, retry_delay


def review(i, label="Changes recommended", stamp="2026-09-05T01:00:00Z"):
    return {"id": i, "user": {"login": "copilot-pull-request-reviewer[bot]", "type": "Bot"},
            "body": "## 🔴 Ordinary findings\n\n## 🟢 " + label,
            "submitted_at": stamp, "state": "COMMENTED", "commit_id": "a" * 40}


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def fetch(self, repo, pr, page):
        self.requested.append(page)
        value = self.pages[page]
        if isinstance(value, Exception):
            raise value
        return value, {}, f"https://api.github.com/repos/{repo}/pulls/{pr}/reviews"


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "reviews.sqlite"
        self.key = ("example/project", 1)
        self.db = open_db(self.path, [self.key])

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_second_page_failure_never_marks_pr_complete_and_restart_keeps_first_page(self):
        first = [review(i) for i in range(1, 101)]
        with self.assertRaises(RequestFailure):
            collect_one(self.db, Client({1: first, 2: RequestFailure(503)}), self.key)
        self.assertEqual(counts(self.db)["complete"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 100)
        resumed = Client({2: [review(101)]})
        collect_one(self.db, resumed, self.key)
        self.assertEqual(resumed.requested, [2])
        self.assertEqual(counts(self.db)["complete"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 101)

    def test_exact_hundred_requires_empty_page_before_complete(self):
        client = Client({1: [review(i) for i in range(1, 101)], 2: []})
        collect_one(self.db, client, self.key)
        self.assertEqual(client.requested, [1, 2])
        self.assertEqual(counts(self.db)["complete"], 1)

    def test_repeated_page_does_not_silently_claim_completeness(self):
        page = [review(i) for i in range(1, 101)]
        with self.assertRaises(RequestFailure):
            collect_one(self.db, Client({1: page, 2: page}), self.key)
        self.assertEqual(counts(self.db)["complete"], 0)

    def test_classification_requires_the_recorded_alias_and_a_heading(self):
        row = review(1)
        self.assertEqual(normalize(row)["assessment"], "NOT_READY")
        row["body"] = "Text mentioning Approval recommended in passing."
        self.assertIsNone(normalize(row)["assessment"])
        row["user"]["login"] = "some-person"
        self.assertFalse(normalize(row)["is_copilot"])
        self.assertIsNone(normalize(row)["body"])

    def test_latest_before_merge_is_selected_even_when_api_order_is_reversed(self):
        raw = [review(3, "Changes recommended", "2026-09-05T04:00:00Z"),
               review(2, "Approval recommended", "2026-09-05T02:00:00Z"),
               review(1, "Changes recommended", "2026-09-05T01:00:00Z")]
        collect_one(self.db, Client({1: raw}), self.key)
        opened = datetime(2026, 9, 5, tzinfo=timezone.utc)
        merged = datetime(2026, 9, 5, 3, tzinfo=timezone.utc)
        state = PullState("MERGED", opened, merged, "a" * 40, "someone", "user", "someone", "user")
        result = compute(self.db, [self.key], {self.key: state}, {}, defaultdict(set))
        self.assertEqual(result["outcomes"]["ready"]["prs"], 1)
        self.assertEqual(result["outcomes"]["ready"]["matching_heads"], 1)
        self.assertEqual(result["counts"]["all_current_qualifying_reviews_after_merge"], 0)

    def test_pending_collection_requires_explicit_partial_label(self):
        with self.assertRaises(ValueError):
            compute(self.db, [self.key], {}, {}, {})

    def test_retry_after_is_not_shortened(self):
        self.assertGreaterEqual(retry_delay({"Retry-After": "7200"}, 0), 7200)
        self.assertGreaterEqual(retry_delay({"Retry-After": "172800"}, 0), 172800)


if __name__ == "__main__":
    unittest.main()
