# Copilot review assessments and pull-request outcomes

Research artifact for **Formal Copilot Approvals and Merges without Current User Approval Records**, by Ogtay Hasanov.

This package reproduces the paper's tables, figure, and result macros from the saved native API analysis output. It includes the paper PDF and LaTeX source, frozen input files, validation evidence, and review-collection and analysis code.

## Main results

The input frame contains 79,296 public-repository pull requests with assessments dated 1 September through 9 November 2026 UTC. Complete review lists were obtained for 76,113 PRs. After joining frozen states and selecting qualifying assessments, the text-assessment cohort contains 69,801 PRs.

| Assessment | PRs | Merged | Matching saved/reviewed SHA among comparable merges |
|---|---:|---:|---:|
| Approval recommended | 23,967 | 21,503 (89.7%) | 16,755/21,473 (78.0%) |
| Changes recommended | 27,648 | 22,648 (81.9%) | 6,437/22,590 (28.5%) |
| Needs a closer look | 18,186 | 15,608 (85.8%) | 10,785/15,584 (69.2%) |
| All | 69,801 | 59,759 (85.6%) | 33,977/59,647 (57.0%) |

Of 418 merged PRs with a qualifying formal Copilot approval, 389 (93.1%) had no current non-Copilot User-account approval record dated by merge. Of 65,710 merged PRs with current qualifying Copilot reviews, 5,951 (9.1%) had every such review submitted after merge.

These are observations about API review records and commit identity. An absent current approval record does not establish that nobody inspected or previously approved a PR. Equal SHAs identify the same saved and reviewed commit; they do not reconstruct every intermediate commit. The saved head is independently verified as the head at merge for five two-parent merge cases. The study does not measure software quality or establish Copilot's causal effect.

## Reproduce the tables and figure

Use Python 3.10 or later. In the extracted repository folder, run:

```powershell
python -m pip install -r requirements.txt
python reproduce.py
```

The script reads `data/full_review_results.json.gz` directly, checks all 69,801 PR-level rows, and writes `paper/generated/`. The JSON was compressed without changing its contents. No token or network request is needed for this build.

The build checks unique PR keys, assessment and state counts, review dates, saved/reviewed SHAs, coverage, formal-approval aggregate constraints, and the fixed archive-probe targets. The generated manifest records the raw result-file SHA256 and checked row count. Formal approvals are supplied as aggregates in the result JSON; recounting those records independently requires the full native-review checkpoint.

The build inserts the replication link, https://github.com/ogtayhasanov/copilot-review-study, in the paper's generated macros. To compile the manuscript, install a LaTeX distribution, then run:

```powershell
Set-Location paper
pdflatex -interaction=nonstopmode -halt-on-error copilot_approval_assessments.tex
pdflatex -interaction=nonstopmode -halt-on-error copilot_approval_assessments.tex
```

The PDF already included in `paper/` is the visually checked rendering from the native result JSON. It includes the replication repository link. The generated tables and figure use the same denominators described in the manuscript.

## Files

| Path | Contents |
|---|---|
| `reproduce.py` | One-command build from the compressed saved results |
| `build_paper_results.py` | Validation and generation of tables, figure, and numeric macros |
| `data/full_review_results.json.gz` | Native result JSON, including all 69,801 selected PR rows |
| `data/frozen/` | Original verdict export and frozen state CSV files |
| `data/api_validation_seed.json` | Normalized complete review lists for the 1,923 accessible validation PRs |
| `data/validation_audit.json` | Corrected random-sample reconciliation results |
| `data/archive_probe.csv` | Raw-archive probe: 0/50 missing IDs and 10/10 controls found |
| `data/probe_targets.csv` | Fixed missing/control ID manifest |
| `data/head_audit_results.json` | Twenty-PR saved-head audit |
| `collection/` | REST/GraphQL collectors, full analyzer, and behavior checks |
| `audit/` | Raw-archive queries and probe summarizer |
| `paper/` | PDF, LaTeX manuscript, generated tables, and figure |
| `CHECKSUMS.sha256` | SHA256 of every other included file |

## Collection and analysis

The full `reviews.sqlite` checkpoint remains on the author's collection machine and is not included in this package. The saved JSON supports rebuilding all reported paper tables and checking the selected text-assessment rows. It does not contain every underlying full-cohort review record.

The collectors and frozen roster allow a separate collection run. Such a run queries current GitHub records, which can change or become inaccessible; it need not reproduce the historical checkpoint exactly. For a new collection, run from the repository root:

```powershell
python collection/collect_reviews_graphql.py data/frozen/verdicts.csv --checkpoint reviews.sqlite --seed data/api_validation_seed.json
python collection/analyze_full_reviews.py reviews.sqlite data/frozen/verdicts.csv data/frozen/pr_states.csv data/frozen/pr_states_recovered.csv --output-prefix recomputed_full_review_results
```

Collection uses `GH_TOKEN` or `GITHUB_TOKEN` when present and otherwise asks for a hidden token. Progress is saved page by page. Rerunning the same collector resumes its checkpoint. The frozen historical PR states remain the outcome source.

## Archive audit

`audit/audit_missing_review_ids.sql` is the executed 50-missing-ID plus 10-control probe. Its saved result is `data/archive_probe.csv`.

`audit/audit_review_capture_by_actor.sql` is the optional, not-yet-executed comparison of Copilot and non-Copilot User-account review capture. It contains all target IDs and can be pasted into BigQuery. Compare its all-submitted-Copilot group with its non-Copilot User-account group. Account type does not prove a person acted, and any finding is conditional on the sampled Copilot-reviewed PR frame and study window.

## Behavior checks

```powershell
python -m unittest -v test_paper_results
python -m unittest discover -s collection -p "test_*.py"
```

## Citation

The preprint has not yet received an arXiv identifier. The title and author are recorded in `CITATION.cff`; add the final publication identifier after submission.
