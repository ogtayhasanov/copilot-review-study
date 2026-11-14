import copy
import gzip
import hashlib
import json
from pathlib import Path
import unittest

from build_paper_results import validate_context


class ApprovalContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent
        raw = gzip.decompress((root / 'data/full_review_results.json.gz').read_bytes())
        cls.result = json.loads(raw)
        cls.sha = hashlib.sha256(raw).hexdigest()
        cls.context = json.loads((root / 'data/approval_context_results.json').read_text())

    def test_supplied_context_matches_original_analysis(self):
        validate_context(self.context, self.result, self.sha)

    def test_context_from_a_different_saved_analysis_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'different saved analysis'):
            validate_context(self.context, self.result, '0' * 64)

    def test_inconsistent_formal_partition_is_rejected(self):
        context = copy.deepcopy(self.context)
        value = context['baseline_no_current_user_approval_by_merge']['merged_text_cohort_without_formal_copilot_approval']
        value['numerator'] -= 1
        value['percent'] = 100 * value['numerator'] / value['denominator']
        with self.assertRaisesRegex(ValueError, 'partitions'):
            validate_context(context, self.result, self.sha)

    def test_renamed_repository_totals_must_sum_to_the_formal_cohort(self):
        context = copy.deepcopy(self.context)
        context['formal_approval_repository_concentration']['per_repository'][0]['prs'] -= 1
        with self.assertRaisesRegex(ValueError, 'concentration totals'):
            validate_context(context, self.result, self.sha)

    def test_wrong_top_five_share_is_rejected(self):
        context = copy.deepcopy(self.context)
        context['formal_approval_repository_concentration']['top_5']['numerator'] -= 1
        with self.assertRaisesRegex(ValueError, 'top-repository share'):
            validate_context(context, self.result, self.sha)


if __name__ == '__main__':
    unittest.main()
