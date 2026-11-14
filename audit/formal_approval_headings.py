#!/usr/bin/env python3
"""Recount formal approvals and heading-based selections in the saved validation sample."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'collection'))
from analyze import parse_time, read_states
from analyze_full_reviews import LABELS, in_window, observation


def inspect_sample(seed_path, states_path, recovered_path):
    states = read_states(states_path).usable | read_states(recovered_path).usable
    records = json.loads(seed_path.read_text(encoding='utf-8-sig'))
    counts, cases = Counter(), []
    for item in records:
        if not item.get('complete'):
            continue
        key = item['repo'].lower(), int(item['pr'])
        approvals = [r for r in item['reviews'] if r['is_copilot'] and r['state'] == 'APPROVED']
        counts['all_approval_reviews'] += len(approvals)
        counts['all_approval_reviews_with_heading'] += sum(r['assessment'] in LABELS for r in approvals)
        state = states.get(key)
        if state is None:
            continue
        qualifying = [r for r in approvals if in_window(r)
                      and (state.created is None or parse_time(r['submitted_at']) >= state.created)
                      and (state.end is None or parse_time(r['submitted_at']) <= state.end)]
        counts['qualifying_approval_reviews'] += len(qualifying)
        counts['qualifying_approval_reviews_with_heading'] += sum(r['assessment'] in LABELS for r in qualifying)
        if not qualifying:
            continue
        counts['qualifying_approval_prs'] += 1
        text = [r for r in item['reviews'] if r['is_copilot'] and r['assessment'] in LABELS and in_window(r)]
        selected, _ = observation(key, text, state)
        if selected:
            counts['selected_' + selected.label] += 1
        before_end = [r for r in item['reviews'] if r['is_copilot']
                      and parse_time(r['submitted_at']) is not None and state.end is not None
                      and (state.created is None or parse_time(r['submitted_at']) >= state.created)
                      and parse_time(r['submitted_at']) <= state.end]
        latest = max((parse_time(r['submitted_at']) for r in before_end), default=None)
        last = [r for r in before_end if parse_time(r['submitted_at']) == latest]
        last_is_approved = bool(last) and all(r['state'] == 'APPROVED' for r in last)
        last_matches = bool(last) and all(r['commit_id'] == state.head for r in last)
        counts['last_copilot_review_is_approved'] += last_is_approved
        counts['last_copilot_review_matches_saved_head'] += last_matches
        cases.append({'repo': key[0], 'pr': key[1],
                      'formal_review_ids': [r['review_id'] for r in qualifying],
                      'selected_text_group': selected.label if selected else None,
                      'selected_text_sha_match': selected.sha_match() if selected and state.state == 'MERGED' else None,
                      'last_copilot_review_is_approved': last_is_approved,
                      'last_copilot_review_matches_saved_head': last_matches})
    return {'counts': dict(counts), 'cases': cases,
            'inputs_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (seed_path, states_path, recovered_path)},
            'scope': 'Saved complete validation review lists and frozen states; not a full-checkpoint recount.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=Path, default=ROOT / 'data/api_validation_seed.json')
    parser.add_argument('--states', type=Path, default=ROOT / 'data/frozen/pr_states.csv')
    parser.add_argument('--recovered', type=Path, default=ROOT / 'data/frozen/pr_states_recovered.csv')
    args = parser.parse_args()
    print(json.dumps(inspect_sample(args.seed, args.states, args.recovered), indent=2))


if __name__ == '__main__':
    main()
