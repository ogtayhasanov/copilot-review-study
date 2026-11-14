#!/usr/bin/env python3
"""Generate every paper table, figure, and result macro from analysis JSON.

Default mode requires full_review_results.json with PR-level rows. The explicit
--summary-preview switch is for the separately labelled reported-count snapshot
when rendering a layout preview. The input type and SHA256 are recorded in the
build manifest. This script does not fetch data or publish a repository.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from math import sqrt
from pathlib import Path
import re
import shutil
import tempfile

GROUPS = ("ready", "not_ready", "uncertain")
NAMES = {"ready": "Approval recommended", "not_ready": "Changes recommended", "uncertain": "Needs a closer look"}
FIELDS = ("prs", "merged", "comparable_merged", "matching_heads")
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 10, tzinfo=timezone.utc)


def integer(value, field):
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def percent(n, d):
    return f"{100*n/d:.1f}%" if d else "n/a"


def number(n):
    return f"{n:,}"


def ratio(n, d):
    return number(n) + "/" + number(d)


def full_sha(value):
    value = str(value or "").lower()
    return value if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) and value.strip("0") else ""


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def validate_results(result, summary_preview=False):
    c, status, outcomes, f = (result[k] for k in ("counts", "status", "outcomes", "formal_approvals"))
    for k, v in c.items(): integer(v, "counts." + k)
    for k, v in status.items(): integer(v, "status." + k)
    if status.get("pending", 0) or status.get("running", 0) or result.get("partial_collection"):
        raise ValueError("Collection still has pending PRs; the final paper build requires a completed pass")
    if sum(status.values()) != c["input_prs"] or status.get("complete", 0) != c["review_lists_complete"]:
        raise ValueError("Collection status totals differ from the input/complete counters")
    for key in (*GROUPS, "ALL"):
        for field in FIELDS: integer(outcomes[key].get(field, 0), f"outcomes.{key}.{field}")
        o = outcomes[key]
        if not 0 <= o.get("matching_heads", 0) <= o.get("comparable_merged", 0) <= o.get("merged", 0) <= o.get("prs", 0):
            raise ValueError("Inconsistent outcome counts in " + key)
    for field in FIELDS:
        if sum(outcomes[k].get(field, 0) for k in GROUPS) != outcomes["ALL"].get(field, 0):
            raise ValueError("Assessment groups do not sum to ALL for " + field)
    if c["complete_lists_without_usable_state"] > c["review_lists_complete"]:
        raise ValueError("State exclusions exceed complete review lists")
    linked = c["review_lists_complete"] - c["complete_lists_without_usable_state"]
    if outcomes["ALL"]["prs"] > linked:
        raise ValueError("Outcome cohort exceeds linked review/state records")
    for k, v in f.items(): integer(v, "formal_approvals." + k)
    if sum(f.get(k, 0) for k in ("merged", "closed", "open")) != f["prs_with_copilot_approval_before_terminal"]:
        raise ValueError("Formal approval states do not sum to the formal cohort")
    for k in ("merged_bot_authored", "merged_without_current_user_approval_by_merge",
              "merged_with_comparable_last_approval_sha"):
        if f.get(k, 0) > f.get("merged", 0): raise ValueError("Formal-approval subset exceeds merged PRs: " + k)
    if f["merged_last_approval_sha_matches_saved_head"] > f["merged_with_comparable_last_approval_sha"]:
        raise ValueError("Formal-approval SHA matches exceed comparable cases")
    if c["all_current_qualifying_reviews_after_merge"] > c["merged_prs_with_current_qualifying_review"]:
        raise ValueError("Post-merge count exceeds its denominator")
    if summary_preview:
        if "details" in result:
            raise ValueError("Use normal mode for results containing PR-level rows")
        return {"source_kind": "reported_summary_preview", "rows_checked": 0}
    details = result.get("details")
    if not isinstance(details, list):
        raise ValueError("Missing PR-level details. Supply full_review_results.json, not the reported-count snapshot")
    window = result.get("review_window_utc")
    if not isinstance(window, list) or len(window) != 2 or [stamp(s) for s in window] != [START, END]:
        raise ValueError("Result review window differs from the paper's 1 Sep--9 Nov 2026 UTC window")
    derived = {k: Counter() for k in GROUPS}
    seen = set()
    for row in details:
        key = str(row["repo"]).lower(), int(row["pr"])
        if key in seen: raise ValueError("Duplicate PR-level key")
        seen.add(key)
        label, state = row["assessment"], row["state"]
        if label not in GROUPS or state not in {"OPEN", "CLOSED", "MERGED"}:
            raise ValueError("Unknown assessment/state in result rows")
        if not START <= stamp(row["submitted_at"]) < END:
            raise ValueError("Selected review is outside the paper window")
        d = derived[label]
        d["prs"] += 1
        d[state.lower()] += 1
        if state == "MERGED":
            sha, head = full_sha(row["review_sha"]), full_sha(row["saved_head"])
            match = (sha == head) if sha and head and len(sha) == len(head) else None
            if row.get("sha_match") is not match:
                raise ValueError("Stored SHA-match flag differs from the PR row's actual SHAs")
            d["comparable_merged"] += match is not None
            d["matching_heads"] += match is True
    if len(details) != outcomes["ALL"]["prs"]:
        raise ValueError("PR-level row count differs from the outcome cohort")
    for key in GROUPS:
        for field in (*FIELDS, "open", "closed"):
            if derived[key].get(field, 0) != outcomes[key].get(field, 0):
                raise ValueError(f"PR-level rows disagree with {key}.{field}")
    return {"source_kind": "analysis_json_with_pr_rows", "rows_checked": len(details)}


def read_probe(path, target_path):
    with target_path.open(encoding="utf-8-sig", newline="") as f:
        targets = {r["review_id"]: r["kind"] for r in csv.DictReader(f)}
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    seen, counts = set(), Counter()
    for r in rows:
        rid, kind = r["review_id"], r["sample_kind"]
        if rid in seen or targets.get(rid) != kind:
            raise ValueError("Probe IDs differ from the fixed probe manifest")
        seen.add(rid)
        counts[kind + "_tested"] += 1
        counts[kind + "_found"] += int(r["raw_event_count"]) > 0
    if seen != targets.keys(): raise ValueError("Probe export is missing target rows")
    return counts


def latex(text):
    return str(text).replace("%", r"\%")


def table(caption, label, headers, rows, wide=False):
    columns = r"p{0.60\linewidth}rr" if wide else "lrrr"
    lines = [r"\begin{table}[H]", r"\centering", r"\caption{" + caption + "}",
             r"\label{" + label + "}", r"\small", r"\begin{tabular}{@{}" + columns + r"@{}}",
             r"\toprule", " & ".join(headers) + r" \\", r"\midrule"]
    lines.extend(" & ".join(latex(c) for c in row) + r" \\" for row in rows)
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}", ""]
    return "\n".join(lines)


def markdown(title, headers, rows):
    return "\n".join([title, "", "| " + " | ".join(headers) + " |",
                      "|" + "---|" * len(headers), *("| " + " | ".join(str(c) for c in row) + " |" for row in rows), ""])


def validate_context(context, result, result_sha256):
    if context['saved_analysis_sha256'] != result_sha256:
        raise ValueError('Approval context belongs to a different saved analysis')
    if context['verified_text_cohort_prs'] != result['outcomes']['ALL']['prs']:
        raise ValueError('Approval context text cohort differs from the paper')
    if context['checkpoint_status'] != result['status'] or context['verified_original_formal_approval_counts'] != result['formal_approvals']:
        raise ValueError('Approval context checkpoint/formal counts differ from the paper')
    rates = context['baseline_no_current_user_approval_by_merge']
    for value in rates.values():
        n, d = integer(value['numerator'], 'context numerator'), integer(value['denominator'], 'context denominator')
        if n > d or (d and abs(value['percent'] - 100*n/d) > .00001):
            raise ValueError('Inconsistent approval-context fraction')
    full = rates['merged_text_cohort']
    other = rates['merged_text_cohort_without_formal_copilot_approval']
    formal = rates['formal_copilot_merged_cohort']
    if full['denominator'] != result['outcomes']['ALL']['merged'] or formal != {
            'numerator': result['formal_approvals']['merged_without_current_user_approval_by_merge'],
            'denominator': result['formal_approvals']['merged'], 'percent': formal['percent']}:
        raise ValueError('Approval-context denominators disagree with the paper')
    if context['formal_merged_overlap_with_text_cohort'] != formal['denominator']:
        raise ValueError('This context table requires all formal merged PRs to overlap the text cohort')
    if full['denominator'] - other['denominator'] != formal['denominator'] or full['numerator'] - other['numerator'] != formal['numerator']:
        raise ValueError('Formal/nonformal approval-context partitions disagree')
    shared = rates['nonformal_text_merges_in_formal_cohort_repositories']
    restricted = rates['same_repositories_and_merged_on_or_after_first_formal_approval']
    if not restricted['denominator'] <= shared['denominator'] <= other['denominator']:
        raise ValueError('Invalid repository/time subset sizes')
    for key, total in [('formal_approval_repository_concentration', result['formal_approvals']['prs_with_copilot_approval_before_terminal']),
                       ('merged_formal_approval_repository_concentration', formal['denominator'])]:
        c = context[key]
        entries = c['per_repository']
        if c['prs'] != total or len(entries) != c['repositories'] or len({r['repository'] for r in entries}) != len(entries) or sum(r['prs'] for r in entries) != total:
            raise ValueError('Inconsistent repository-concentration totals')
        for k in (1, 5, 10):
            v = c['top_' + str(k)]
            if v['denominator'] != total or v['numerator'] != sum(sorted((r['prs'] for r in entries), reverse=True)[:k]):
                raise ValueError('Inconsistent top-repository share')
    for field in ('numerator', 'denominator'):
        if sum(v[field] for v in context['by_text_assessment'].values()) != full[field]:
            raise ValueError('Assessment-specific approval counts do not sum to the full baseline')


def render_context(context):
    values = context['baseline_no_current_user_approval_by_merge']
    rows = []
    macros = {}
    definitions = [
        ('FormalBaseline', 'formal_copilot_merged_cohort', 'Formal Copilot approvals'),
        ('TextBaseline', 'merged_text_cohort', 'All merged text-assessment PRs'),
        ('NonformalBaseline', 'merged_text_cohort_without_formal_copilot_approval', 'Text-assessment PRs without formal Copilot approval'),
        ('SharedBaseline', 'nonformal_text_merges_in_formal_cohort_repositories', 'Nonformal comparison PRs in formal-cohort repositories'),
        ('PeriodBaseline', 'same_repositories_and_merged_on_or_after_first_formal_approval', 'Same repositories, merged on/after first formal approval')]
    for prefix, key, title in definitions:
        v = values[key]
        n, d = v['numerator'], v['denominator']
        rows.append((title, ratio(n, d), percent(n, d)))
        macros[prefix + 'Count'] = number(n)
        macros[prefix + 'PRs'] = number(d)
        macros[prefix + 'Rate'] = latex(percent(n, d))
    for prefix, key in [('FormalRepo', 'formal_approval_repository_concentration'), ('MergedFormalRepo', 'merged_formal_approval_repository_concentration')]:
        c = context[key]
        macros[prefix + 'Count'] = number(c['repositories'])
        for k, label in [(1, 'One'), (5, 'Five'), (10, 'Ten')]:
            v = c['top_' + str(k)]
            macros[prefix + 'Top' + label + 'PRs'] = number(v['numerator'])
            macros[prefix + 'Top' + label + 'Rate'] = latex(percent(v['numerator'], v['denominator']))
    date = stamp(context['first_qualifying_formal_approval_utc'])
    macros['FirstFormalDate'] = str(date.day) + date.strftime(' %B %Y')
    text = '\n'.join(r'\newcommand{' + '\\' + k + '}{' + v + '}' for k, v in macros.items()) + '\n'
    caption = 'Current user-account approval records among merged PRs. Each row counts PRs without a qualifying user-account approval by merge. Repository-restricted rows exclude formal Copilot approvals and use repositories represented in the full formal-approval cohort.'
    return text, table(caption, 'tab:approval-context', ['Merged PR group', 'Count', 'Share'], rows, True), markdown('Table 3. Approval-record context', ['Merged PR group', 'Count', 'Share'], rows)


def render(result, audit, probe, repository_url=None, context=None, heading_audit=None):
    c, o, f, a = result["counts"], result["outcomes"], result["formal_approvals"], audit["counts"]
    def frac(n, d): return ratio(n, d), percent(n, d)
    formal = [
        ("Recorded as merged", *frac(f["merged"], f["prs_with_copilot_approval_before_terminal"])),
        ("Merged with no current user-account approval record dated by merge", *frac(f["merged_without_current_user_approval_by_merge"], f["merged"])),
        ("Merged PR authored by an account classified as a bot", *frac(f["merged_bot_authored"], f["merged"])),
        ("Last formal-approval SHA matched the saved head", *frac(f["merged_last_approval_sha_matches_saved_head"], f["merged_with_comparable_last_approval_sha"]))]
    outcome = [(NAMES.get(k, "All"), number(o[k]["prs"]),
                number(o[k]["merged"]) + " (" + percent(o[k]["merged"], o[k]["prs"]) + ")",
                ratio(o[k]["matching_heads"], o[k]["comparable_merged"]) + " (" + percent(o[k]["matching_heads"], o[k]["comparable_merged"]) + ")")
               for k in (*GROUPS, "ALL")]
    audit_rows = [
        ("Complete review lists in the random validation sample", *frac(a["accessible"], audit["sample_size"])),
        ("PRs with a qualifying API review absent from the export", *frac(a["prs_with_missing_qualifying_reviews"], a["accessible"])),
        ("Qualifying API reviews absent from the export", *frac(a["missing_qualifying_reviews"], a["api_qualifying_reviews"])),
        ("Selected group changed after adding missing reviews", *frac(a["group_changes_adding_missing_reviews"], a["accessible_with_frozen_state"])),
        ("All-after-merge status changed after adding missing reviews", *frac(a["post_merge_status_changed_adding_missing"], a["accessible_merged"]))]
    raw = {
        "FrameCount": c["input_prs"], "CompleteLists": c["review_lists_complete"],
        "UnavailableLists": result["status"].get("unavailable", 0), "ErrorLists": result["status"].get("error", 0),
        "MissingStateLists": c["complete_lists_without_usable_state"],
        "LinkedStateLists": c["review_lists_complete"] - c["complete_lists_without_usable_state"],
        "OutcomePRs": o["ALL"]["prs"], "OutcomeMerged": o["ALL"]["merged"],
        "OverallMatches": o["ALL"]["matching_heads"], "OverallComparable": o["ALL"]["comparable_merged"],
        "UnknownComparedSHAs": o["ALL"]["merged"] - o["ALL"]["comparable_merged"],
        "ChangesMatches": o["not_ready"]["matching_heads"], "ChangesComparable": o["not_ready"]["comparable_merged"],
        "ChangesMismatches": o["not_ready"]["comparable_merged"] - o["not_ready"]["matching_heads"],
        "PostMergeCount": c["all_current_qualifying_reviews_after_merge"], "PostMergeDenominator": c["merged_prs_with_current_qualifying_review"],
        "NoCurrentMergedReview": c["merged_prs_without_current_qualifying_review"],
        "FormalPRs": f["prs_with_copilot_approval_before_terminal"], "FormalMerged": f["merged"],
        "FormalClosed": f.get("closed", 0), "FormalOpen": f.get("open", 0),
        "FormalNoUser": f["merged_without_current_user_approval_by_merge"], "FormalBotAuthor": f["merged_bot_authored"],
        "FormalMatches": f["merged_last_approval_sha_matches_saved_head"], "FormalComparable": f["merged_with_comparable_last_approval_sha"],
        "FormalMismatches": f["merged_with_comparable_last_approval_sha"] - f["merged_last_approval_sha_matches_saved_head"],
        "AuditSample": audit["sample_size"], "AuditAccessible": a["accessible"],
        "AuditInaccessible": audit["sample_size"] - a["accessible"], "AuditMissingState": a["accessible_without_frozen_state"],
        "AuditLinked": a["accessible_with_frozen_state"], "AuditReviewCount": a["api_qualifying_reviews"],
        "AuditMissingReviews": a["missing_qualifying_reviews"], "AuditMissingPRs": a["prs_with_missing_qualifying_reviews"],
        "AuditGroupChanges": a["group_changes_adding_missing_reviews"],
        "ProbeMissingTested": probe["missing_tested"], "ProbeMissingFound": probe["missing_found"],
        "ProbeControlsTested": probe["positive_control_tested"], "ProbeControlsFound": probe["positive_control_found"]}
    raw.update({
        'ReadyMerged': o['ready']['merged'], 'ChangesMerged': o['not_ready']['merged'],
        'ReadyTerminalPRs': o['ready']['merged'] + o['ready'].get('closed', 0),
        'ChangesTerminalPRs': o['not_ready']['merged'] + o['not_ready'].get('closed', 0)})
    if heading_audit is not None:
        raw['SampleFormalReviews'] = heading_audit['counts']['qualifying_approval_reviews']
        raw['SampleFormalHeadings'] = heading_audit['counts']['qualifying_approval_reviews_with_heading']
    macros = {k: number(v) for k, v in raw.items()}
    rates = {
        'ReadyTerminalMergeRate': (raw['ReadyMerged'], raw['ReadyTerminalPRs']),
        'ChangesTerminalMergeRate': (raw['ChangesMerged'], raw['ChangesTerminalPRs']),
        'ReadyOpenRate': (o['ready'].get('open', 0), o['ready']['prs']),
        'ChangesOpenRate': (o['not_ready'].get('open', 0), o['not_ready']['prs']),
        'FormalShareOfMergedText': (raw['FormalMerged'], raw['OutcomeMerged']),
        'ChangesFormalBoundRate': (raw['ChangesMatches'], raw['ChangesComparable'] - raw['FormalMerged']),
        "ReadyMatchRate": (o['ready']['matching_heads'], o['ready']['comparable_merged']),
        "CoverageRate": (c["review_lists_complete"], c["input_prs"]),
        "ReadyMergeRate": (o["ready"]["merged"], o["ready"]["prs"]),
        "ChangesMergeRate": (o["not_ready"]["merged"], o["not_ready"]["prs"]),
        "ChangesMatchRate": (o["not_ready"]["matching_heads"], o["not_ready"]["comparable_merged"]),
        "ChangesMismatchRate": (raw["ChangesMismatches"], raw["ChangesComparable"]),
        "OverallMatchRate": (raw["OverallMatches"], raw["OverallComparable"]),
        "PostMergeRate": (raw["PostMergeCount"], raw["PostMergeDenominator"]),
        "FormalNoUserRate": (raw["FormalNoUser"], raw["FormalMerged"]),
        "FormalMatchRate": (raw["FormalMatches"], raw["FormalComparable"]),
        "AuditMissingReviewRate": (a["missing_qualifying_reviews"], a["api_qualifying_reviews"]),
        "AuditMissingPRRate": (a["prs_with_missing_qualifying_reviews"], a["accessible"]),
        "AuditGroupChangeRate": (a["group_changes_adding_missing_reviews"], a["accessible_with_frozen_state"])}
    macros.update({k: latex(percent(n, d)) for k, (n, d) in rates.items()})
    macros["MergeGapPP"] = f"{100*(o['ready']['merged']/o['ready']['prs']-o['not_ready']['merged']/o['not_ready']['prs']):.1f}"
    macros['TerminalMergeGapPP'] = f"{100*(raw['ReadyMerged']/raw['ReadyTerminalPRs']-raw['ChangesMerged']/raw['ChangesTerminalPRs']):.1f}"
    for label,n,d in [("AuditMissingPR",a['prs_with_missing_qualifying_reviews'],a['accessible']),
                      ("AuditGroupChange",a['group_changes_adding_missing_reviews'],a['accessible_with_frozen_state'])]:
        z=1.959963984540054; phat=n/d; den=1+z*z/d
        center=(phat+z*z/(2*d))/den; width=z*sqrt(phat*(1-phat)/d+z*z/(4*d*d))/den
        macros[label+"LowCI"]=f"{100*max(0,center-width):.1f}"
        macros[label+"HighCI"]=f"{100*min(1,center+width):.1f}"
    if repository_url and not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?", repository_url):
        raise ValueError("Supply the GitHub repository root URL, https://github.com/owner/repository")
    macros["RepositoryLink"] = (r"\par Replication repository: \url{" + repository_url.rstrip("/") + "}." if repository_url else "")
    rendered = {"results_macros.tex": "\n".join(r"\newcommand{" + "\\" + k + "}{" + v + "}" for k, v in macros.items()) + "\n"}
    rendered["formal_table.tex"] = table("Formal Copilot approvals. The merged rate uses all qualifying formal-approval PRs. The remaining rows describe the merged subset.", "tab:formal", ["Measure", "Count", "Share"], formal, True)
    rendered["outcome_table.tex"] = table("Outcomes by last qualifying text assessment. Merge rates use every PR in the group. Head-match rates use comparable merged PRs.", "tab:outcomes", ["Assessment", "PRs", "Merged", "Matching heads among merges"], outcome)
    rendered["archive_table.tex"] = table("Validation of the frozen export. Review-level and PR-level denominators are stated separately.", "tab:audit", ["Measure", "Count", "Share"], audit_rows, True)
    context_markdown = ''
    if context is not None:
        extra_macros, context_table, context_markdown = render_context(context)
        rendered['results_macros.tex'] += extra_macros
        rendered['approval_context_table.tex'] = context_table
    if heading_audit is not None:
        rendered['formal_heading_audit.json'] = json.dumps(heading_audit, indent=2) + '\n'
    rendered["tables.md"] = "\n".join([
        markdown("Table 1. Text assessments", ["Assessment", "PRs", "Merged", "Matching heads among merges"], outcome),
        markdown("Table 2. Formal approvals", ["Measure", "Count", "Share"], formal),
        context_markdown,
        markdown("Table 4. Archive audit" if context is not None else "Table 3. Archive audit", ["Measure", "Count", "Share"], audit_rows)])
    return rendered


def figure(result, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise ValueError("Figure generation needs matplotlib: python -m pip install matplotlib") from None
    o = result["outcomes"]
    x = list(range(3))
    merged = [100*o[k]["merged"]/o[k]["prs"] for k in GROUPS]
    match = [100*o[k]["matching_heads"]/o[k]["prs"] for k in GROUPS]
    fig, ax = plt.subplots(figsize=(7.6, 3.5))
    a = ax.bar([i-.18 for i in x], merged, .36, color="#444444", label="Merged")
    b = ax.bar([i+.18 for i in x], match, .36, color="#bbbbbb", edgecolor="#777777", linewidth=.6, label="Merged with matching head")
    ax.set_ylim(0, 105); ax.set_yticks(range(0, 101, 20))
    ax.set_ylabel("Share of all PRs in the group (%)")
    ax.set_xticks(x, [f"{NAMES[k]}\n(n = {o[k]['prs']:,})" for k in GROUPS])
    ax.grid(axis="y", color="#eeeeee", linewidth=.6); ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(.5, 1.18), ncol=2, frameon=False)
    for bars in (a, b): ax.bar_label(bars, labels=[f"{bar.get_height():.1f}%" for bar in bars], padding=3, fontsize=9)
    fig.tight_layout(); fig.savefig(path, dpi=300, bbox_inches="tight", facecolor="white"); plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", type=Path)
    home = Path(__file__).resolve().parent
    p.add_argument("--audit", type=Path, default=home/"data/validation_audit.json")
    p.add_argument("--probe", type=Path, default=home/"data/archive_probe.csv")
    p.add_argument("--probe-targets", type=Path, default=home/"data/probe_targets.csv")
    p.add_argument("--out-dir", type=Path, default=home/"paper/generated")
    p.add_argument("--repository-url")
    p.add_argument("--approval-context", type=Path, default=home/'data/approval_context_results.json')
    p.add_argument("--summary-preview", action="store_true")
    args = p.parse_args()
    try:
        result = json.loads(args.results.read_text(encoding="utf-8-sig"))
        verified = validate_results(result, args.summary_preview)
        audit = json.loads(args.audit.read_text(encoding="utf-8-sig"))
        if result['counts']['input_prs'] != audit['population']:
            raise ValueError("Result and validation audit describe different input-frame sizes")
        probe = read_probe(args.probe, args.probe_targets)
        result_hash = hashlib.sha256(args.results.read_bytes()).hexdigest()
        context = json.loads(args.approval_context.read_text(encoding='utf-8-sig'))
        validate_context(context, result, result_hash)
        from audit.formal_approval_headings import inspect_sample
        heading_audit = inspect_sample(home/'data/api_validation_seed.json', home/'data/frozen/pr_states.csv', home/'data/frozen/pr_states_recovered.csv')
        outputs = render(result, audit, probe, args.repository_url, context, heading_audit)
        manifest = {**verified, "result_file": args.results.name,
                    "result_sha256": hashlib.sha256(args.results.read_bytes()).hexdigest(),
                    "repository_url": args.repository_url, "tables": ["outcome_table.tex", "formal_table.tex", "approval_context_table.tex", "archive_table.tex"],
                    "approval_context_file": args.approval_context.name,
                    "approval_context_sha256": hashlib.sha256(args.approval_context.read_bytes()).hexdigest(),
                    "approval_context_validation": "saved-analysis hash, supplied checkpoint/formal counts, cohort partitions, and repository aggregates; raw reviews remain in the local checkpoint",
                    'formal_heading_sample_counts': heading_audit['counts'],
                    "formal_approval_validation": "aggregate constraints; approval-level records are in the native-review checkpoint"}
        outputs["build_manifest.json"] = json.dumps(manifest, indent=2) + "\n"
        args.out_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.out_dir.parent) as temporary:
            stage = Path(temporary)
            for name, text in outputs.items(): (stage/name).write_text(text, encoding="utf-8")
            figure(result, stage/"merge_outcomes.png")
            args.out_dir.mkdir(parents=True, exist_ok=True)
            for source in stage.iterdir(): shutil.move(str(source), args.out_dir/source.name)
        print(outputs["tables.md"])
        print(f"PR-level rows checked: {verified['rows_checked']:,}. Input type: {verified['source_kind']}.")
        if not args.repository_url: print("Repository URL not supplied; no repository link was inserted.")
        print("Generated", args.out_dir)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())

