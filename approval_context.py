#!/usr/bin/env python3
"""Audit approval baselines and repository concentration, using a read-only checkpoint.

Usage: python approval_context.py PATH_TO_reviews.sqlite
Python 3.10+, standard library only. No network requests or database writes.
Run from the study repository or keep the standalone inputs and _study folders beside the script.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import csv
import gzip
import hashlib
from itertools import groupby
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ('collection' if (ROOT / 'collection').is_dir() else '_study')))
from analyze import parse_time, read_states, full_sha
from analyze_full_reviews import LABELS, in_window, observation


def fraction(n, d):
    return {'numerator': n, 'denominator': d,
            'percent': round(100 * n / d, 6) if d else None}


def approval_by_merge(review, state):
    time = parse_time(review['submitted_at'])
    return (not review['is_copilot']
            and (review['account_type'] or '').lower() == 'user'
            and review['state'] == 'APPROVED'
            and time is not None and state.state == 'MERGED'
            and (state.created is None or time >= state.created)
            and time <= state.end)


def formal_in_lifetime(review, state):
    time = parse_time(review['submitted_at'])
    return (bool(review['is_copilot']) and review['state'] == 'APPROVED'
            and in_window(review)
            and (state.created is None or time >= state.created)
            and (state.end is None or time <= state.end))


def concentration(rows):
    counts = Counter(r['canonical_repo'] for r in rows)
    ordered = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return {'prs': len(rows), 'repositories': len(counts),
            'repository_identity': 'canonical_repo when saved, otherwise frozen roster repo',
            'original_roster_repository_count': len({r['repo'] for r in rows}),
            'top_1': fraction(sum(n for _, n in ordered[:1]), len(rows)),
            'top_5': fraction(sum(n for _, n in ordered[:5]), len(rows)),
            'top_10': fraction(sum(n for _, n in ordered[:10]), len(rows)),
            'per_repository': [{'repository': repo, 'prs': n,
                                'percent': round(100 * n / len(rows), 6)}
                               for repo, n in ordered]}


def summarize(rows):
    merged = [r for r in rows if r['state'] == 'MERGED']
    formal_all = [r for r in rows if r['formal_copilot_approval']]
    formal = [r for r in merged if r['formal_copilot_approval']]
    text = [r for r in merged if r['text_cohort']]
    formal_repos = {r['canonical_repo'] for r in formal_all}
    def rate(subset):
        return fraction(sum(not r['user_approval_by_merge'] for r in subset), len(subset))
    nonformal = [r for r in text if not r['formal_copilot_approval']]
    shared = [r for r in nonformal if r['canonical_repo'] in formal_repos]
    earliest = min((r['earliest_formal_approval'] for r in formal_all), default=None)
    shared_overlap = [r for r in shared if earliest and r['merged_at'] >= earliest]
    return {'baseline_no_current_user_approval_by_merge': {
                'merged_text_cohort': rate(text),
                'merged_text_cohort_without_formal_copilot_approval': rate(nonformal),
                'nonformal_text_merges_in_formal_cohort_repositories': rate(shared),
                'same_repositories_and_merged_on_or_after_first_formal_approval': rate(shared_overlap),
                'formal_copilot_merged_cohort': rate(formal)},
            'formal_approval_repository_concentration': concentration(formal_all),
            'merged_formal_approval_repository_concentration': concentration(formal),
            'formal_merged_overlap_with_text_cohort': sum(r['text_cohort'] for r in formal),
            'first_qualifying_formal_approval_utc': earliest,
            'by_text_assessment': {label: rate([r for r in text if r['assessment'] == label])
                                   for label in ('ready', 'not_ready', 'uncertain')},
            'interpretation': [
                'User-account approval means a currently APPROVED non-Copilot review by a GitHub User account, submitted within PR lifetime and by merge.',
                'User approvals are not restricted to the Copilot assessment study window, matching the paper definition.',
                'Absent current approval records do not establish absence of human inspection or earlier dismissed approvals.',
                'The full merged-text baseline includes overlapping formally approved PRs. The nonformal baseline excludes them.',
                'These are descriptive baselines within the Copilot-reviewed input frame, not controls drawn from all GitHub PRs.',
                'Shared-repository and overlapping-period subsets remain unadjusted for PR composition and repository rules.',
                'Checkpoint review states are measured at collection time; outcomes use the frozen PR snapshots.'
            ]}


def audit(db, states, expected):
    db.row_factory = sqlite3.Row
    metadata = {(r['repo'], r['pr']): dict(r) for r in db.execute('SELECT * FROM prs')}
    status = Counter(r['status'] for r in metadata.values())
    if dict(status) != expected['status'] or len(metadata) != expected['counts']['input_prs']:
        raise ValueError('Checkpoint roster/status differs from the saved paper analysis. No final results written.')
    expected_rows = {(r['repo'], r['pr']): r for r in expected['details']}
    if len(expected_rows) != len(expected['details']):
        raise ValueError('Duplicate PRs in saved analysis.')
    rows, observed_text = [], set()
    formal_counts = Counter()
    def process(key, reviews):
        if metadata[key]['status'] != 'complete' or key not in states:
            return
        state = states[key]
        qualifying = [r for r in reviews if r['is_copilot'] and r['assessment'] in LABELS and in_window(r)]
        selected, _ = observation(key, qualifying, state)
        saved = expected_rows.get(key)
        if bool(selected) != bool(saved):
            raise ValueError('Text-cohort membership differs from the saved paper analysis: ' + str(key))
        if selected:
            observed_text.add(key)
            if (selected.label, state.state, selected.last, selected.review_sha, state.head) != (
                    saved['assessment'], saved['state'], parse_time(saved['submitted_at']),
                    saved['review_sha'], saved['saved_head']):
                raise ValueError('Selected assessment/state/SHAs differ from saved analysis: ' + str(key))
        approvals = [r for r in reviews if formal_in_lifetime(r, state)]
        users = [r for r in reviews if approval_by_merge(r, state)]
        if approvals:
            formal_counts['prs_with_copilot_approval_before_terminal'] += 1
            formal_counts[state.state.lower()] += 1
            if state.state == 'MERGED':
                formal_counts['merged_bot_authored'] += state.author_type == 'bot'
                formal_counts['merged_without_current_user_approval_by_merge'] += not users
                latest = max(parse_time(r['submitted_at']) for r in approvals)
                shas = {full_sha(r['commit_id']) for r in approvals if parse_time(r['submitted_at']) == latest}
                sha = next(iter(shas)) if len(shas) == 1 else ''
                formal_counts['merged_with_comparable_last_approval_sha'] += bool(sha and state.head)
                formal_counts['merged_last_approval_sha_matches_saved_head'] += bool(sha and state.head and sha == state.head)
        if selected and state.state == 'MERGED' or approvals:
            canonical = (metadata[key].get('canonical_repo') or key[0]).lower()
            rows.append({'repo': key[0], 'pr': key[1], 'canonical_repo': canonical,
                         'state': state.state, 'merged_at': state.end.isoformat() if state.state == 'MERGED' else '',
                         'text_cohort': bool(selected), 'assessment': selected.label if selected else '',
                         'formal_copilot_approval': bool(approvals),
                         'earliest_formal_approval': min((parse_time(r['submitted_at']).isoformat() for r in approvals), default=''),
                         'user_approval_by_merge': bool(users),
                         'qualifying_user_approval_ids': [r['review_id'] for r in users],
                         'qualifying_formal_copilot_approval_ids': [r['review_id'] for r in approvals]})
    seen = set()
    cursor = db.execute('SELECT * FROM reviews ORDER BY repo,pr,review_id')
    for key, grouped in groupby(cursor, key=lambda r: (r['repo'], r['pr'])):
        if key not in metadata:
            raise ValueError('Checkpoint has reviews outside its PR roster.')
        seen.add(key)
        process(key, [dict(r) for r in grouped])
    for key in metadata.keys() - seen:
        process(key, [])
    if observed_text != expected_rows.keys():
        raise ValueError('Reconstructed text cohort differs from saved analysis.')
    if any(formal_counts.get(k, 0) != v for k, v in expected['formal_approvals'].items()):
        raise ValueError('Formal-approval aggregates differ from the paper: ' + repr(dict(formal_counts)))
    output = summarize(rows)
    if output['baseline_no_current_user_approval_by_merge']['merged_text_cohort']['denominator'] != expected['outcomes']['ALL']['merged']:
        raise ValueError('Merged-text baseline denominator differs from the paper.')
    output['verified_original_formal_approval_counts'] = dict(formal_counts)
    output['verified_text_cohort_prs'] = len(observed_text)
    output['checkpoint_status'] = dict(status)
    return output, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkpoint', type=Path)
    parser.add_argument('--output-prefix', type=Path, default=Path('approval_context_results'))
    args = parser.parse_args()
    try:
        checkpoint = args.checkpoint.resolve(strict=True)
        data = ROOT / 'data' if (ROOT / 'data').is_dir() else ROOT / 'inputs'
        frozen = data / 'frozen' if (data / 'frozen').is_dir() else data
        raw = gzip.decompress((data / 'full_review_results.json.gz').read_bytes())
        expected = json.loads(raw)
        states = read_states(frozen / 'pr_states.csv').usable | read_states(frozen / 'pr_states_recovered.csv').usable
        print('Reading the checkpoint without network requests or database writes...', flush=True)
        db = sqlite3.connect(checkpoint.as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            output, rows = audit(db, states, expected)
        finally:
            db.close()
        output['saved_analysis_sha256'] = hashlib.sha256(raw).hexdigest()
        output['audit_time_utc'] = datetime.now(timezone.utc).isoformat()
        prefix = args.output_prefix
        prefix.parent.mkdir(parents=True, exist_ok=True)
        prefix.with_suffix('.json').write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
        with prefix.with_suffix('.csv').open('w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows({**r, 'qualifying_user_approval_ids': json.dumps(r['qualifying_user_approval_ids']),
                             'qualifying_formal_copilot_approval_ids': json.dumps(r['qualifying_formal_copilot_approval_ids'])} for r in rows)
        for label, value in output['baseline_no_current_user_approval_by_merge'].items():
            print(label + ':', f"{value['numerator']:,}/{value['denominator']:,}",
                  f"({value['percent']:.1f}%)" if value['percent'] is not None else '(n/a)')
        for name in ('formal_approval_repository_concentration', 'merged_formal_approval_repository_concentration'):
            c = output[name]
            print(name + ':', c['prs'], 'PRs across', c['repositories'], 'repositories; top 5:', c['top_5'])
        print('Saved', prefix.with_suffix('.json'), 'and', prefix.with_suffix('.csv'))
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
