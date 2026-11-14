#!/usr/bin/env python3
"""Rebuild paper results from the bundled compressed native result file."""
import argparse
import gzip
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, default=ROOT/'data/full_review_results.json.gz')
    parser.add_argument('--out-dir', type=Path, default=ROOT/'paper/generated')
    parser.add_argument('--repository-url', default='https://github.com/ogtayhasanov/copilot-review-study')
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix='copilot-paper-') as temporary:
            result = Path(temporary)/'full_review_results.json'
            opener = gzip.open if args.results.suffix == '.gz' else open
            with opener(args.results, 'rb') as source, result.open('wb') as target:
                shutil.copyfileobj(source, target)
            command = [sys.executable, str(ROOT/'build_paper_results.py'), str(result),
                       '--out-dir', str(args.out_dir.resolve())]
            if args.repository_url:
                command += ['--repository-url', args.repository_url]
            return subprocess.call(command, cwd=ROOT)
    except OSError as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    raise SystemExit(main())
