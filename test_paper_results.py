"""Behavior checks for file-driven paper counts and honest input handling."""
from copy import deepcopy
from pathlib import Path
import json
import unittest

from build_paper_results import validate_results, render, read_probe


def actual_result():
    groups={
        'ready':{'prs':1,'merged':1,'comparable_merged':1,'matching_heads':1},
        'not_ready':{'prs':1,'merged':1,'comparable_merged':1,'matching_heads':0},
        'uncertain':{'prs':1,'merged':1,'comparable_merged':1,'matching_heads':1},
        'ALL':{'prs':3,'merged':3,'comparable_merged':3,'matching_heads':2}}
    return {'counts':{'input_prs':3,'review_lists_complete':3,'complete_lists_without_usable_state':0,
                      'merged_prs_with_current_qualifying_review':3,'all_current_qualifying_reviews_after_merge':0,
                      'merged_prs_without_current_qualifying_review':0},
            'status':{'complete':3},'outcomes':groups,
            'formal_approvals':{'prs_with_copilot_approval_before_terminal':1,'merged':1,'closed':0,'open':0,
               'merged_bot_authored':0,'merged_without_current_user_approval_by_merge':1,
               'merged_with_comparable_last_approval_sha':1,'merged_last_approval_sha_matches_saved_head':1},
            'review_window_utc':['2026-09-01T00:00:00+00:00','2026-11-10T00:00:00+00:00'],
            'details':[{'repo':'example/study','pr':i,'assessment':label,'state':'MERGED','submitted_at':'2026-10-01T00:00:00Z',
                        'review_sha':'a'*40,'saved_head':('b' if label=='not_ready' else 'a')*40,
                        'sha_match':label!='not_ready'} for i,label in enumerate(['ready','not_ready','uncertain'],1)]}


class ResultsTests(unittest.TestCase):
    def test_valid_analysis_rows_are_recounted(self):
        self.assertEqual(validate_results(actual_result())['rows_checked'],3)

    def test_duplicate_pr_rows_are_rejected(self):
        r=actual_result();r['details'][1]['pr']=1
        with self.assertRaisesRegex(ValueError,'Duplicate'):validate_results(r)

    def test_sha_flags_are_verified_from_the_commit_ids(self):
        r=actual_result();r['details'][1]['sha_match']=True
        with self.assertRaisesRegex(ValueError,'SHA-match'):validate_results(r)

    def test_snapshot_cannot_silently_impersonate_result_rows(self):
        r=actual_result();r.pop('details')
        with self.assertRaisesRegex(ValueError,'Missing PR-level'):validate_results(r)
        self.assertEqual(validate_results(r,True)['source_kind'],'reported_summary_preview')

    def test_pending_collection_is_rejected(self):
        r=actual_result();r['status']['pending']=1
        with self.assertRaisesRegex(ValueError,'pending'):validate_results(r)

    def test_mismatched_group_totals_are_rejected(self):
        r=actual_result();r['outcomes']['ALL']['matching_heads']=1
        with self.assertRaisesRegex(ValueError,'sum to ALL'):validate_results(r)

    def test_generation_uses_input_counts_and_has_no_invented_repo(self):
        here=Path(__file__).parent
        audit=json.loads((here/'data/validation_audit.json').read_text())
        probe=read_probe(here/'data/archive_probe.csv',here/'data/probe_targets.csv')
        result=actual_result();out=render(result,audit,probe)
        self.assertIn('1/1',out['formal_table.tex'])
        self.assertIn(r'\newcommand{\RepositoryLink}{}',out['results_macros.tex'])
        self.assertNotIn('418/429',out['formal_table.tex'])
        with_link=render(result,audit,probe,'https://github.com/example/study')
        self.assertIn('https://github.com/example/study',with_link['results_macros.tex'])


if __name__=='__main__':unittest.main()
