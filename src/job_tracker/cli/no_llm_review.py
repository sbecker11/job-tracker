"""CLI: print (and optionally rewrite) the deterministic rule-based review
for one stored lead — no LLM call.

Re-scores the lead's stored `jd_text` with `scoring.scorer.score_jd` +
`rule_checklist` so the output always reflects the current
`config/framework.yaml`, then prints match %, verdict, skills, and every
passed/failed framework rule. Optional `--write` regenerates
`no-LLM-review.docx` via `render_no_llm_review_docx`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from job_tracker.pipeline.llm_apply import (
    DEFAULT_OUTPUT_ROOT,
    _RULE_STATUS_LABEL,
    _safe_filename,
    render_no_llm_review_docx,
)
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    connect,
    find_similar_jobs,
    get_job,
    get_sibling_titles,
)
from job_tracker.scoring.scorer import RuleCheck, ScoreResult, rule_checklist, score_jd, should_run_llm_review


def _resolve_job(conn, company: str, title: str):
    job = get_job(conn, company, title)
    if job is not None:
        return job
    candidates = find_similar_jobs(conn, company, title)
    print(f"No job found for {title!r} @ {company!r}.", file=sys.stderr)
    if candidates:
        print("Did you mean one of these (use the exact company/title shown)?", file=sys.stderr)
        for m in candidates[:5]:
            print(f"  {m.title} @ {m.company}  (score={m.combined_score:.2f})", file=sys.stderr)
    else:
        print("Nothing close in leads.db — check spelling with scripts/list_leads.py.", file=sys.stderr)
    return None


def _expected_review_path(output_root: Path, *, company: str, title: str, multi_lead: bool) -> Path:
    """Read-only path for `no-LLM-review.docx` (does not mkdir / migrate)."""
    company_dir = output_root / _safe_filename(company)
    folder = company_dir if not multi_lead else company_dir / _safe_filename(f"{company}_{title}")
    return folder / "no-LLM-review.docx"


def _score_to_dict(score: ScoreResult) -> dict:
    return {
        "match_pct": score.match_pct,
        "verdict": score.verdict,
        "matched_skills": sorted(score.matched_skills),
        "unmatched_jd_skills": sorted(score.unmatched_jd_skills),
        "relevant_weight": score.relevant_weight,
        "rationale": list(score.rationale),
        "dealbreaker_hits": [asdict(h) for h in score.dealbreaker_hits],
        "clears_llm_review_gate": should_run_llm_review(score),
    }


def _checks_to_sections(checks: list[RuleCheck]) -> dict[str, list[dict]]:
    passed = [asdict(c) for c in checks if c.status == "passed"]
    failed = [asdict(c) for c in checks if c.status == "failed"]
    return {"passed_rules": passed, "failed_rules": failed}


def format_no_llm_review(
    score: ScoreResult,
    checks: list[RuleCheck],
    *,
    company: str,
    title: str,
    review_path: Path | None = None,
    wrote_path: Path | None = None,
) -> str:
    """Human-readable rule-based review (stdout counterpart of no-LLM-review.docx)."""
    gate = should_run_llm_review(score)
    passed = [c for c in checks if c.status == "passed"]
    failed = [c for c in checks if c.status == "failed"]
    lines: list[str] = [
        f"{title} @ {company}",
        "Rule-based review — no LLM call (rescored from stored jd_text).",
        "",
        f"VERDICT: {score.verdict.upper()}  |  match ~{score.match_pct:.0f}% (JD-relative)",
        (
            f"  clears full-LLM-review gate"
            if gate
            else f"  below full-LLM-review gate"
        ),
        f"  passed rules: {len(passed)}  |  failed rules: {len(failed)}",
        "",
    ]

    if score.dealbreaker_hits:
        lines.append("Dealbreaker sweep (keyword hits only)")
        lines.append(f"  {'Check':<45} {'Status':<32} Hits")
        lines.append(f"  {'-' * 45} {'-' * 32} ----")
        for h in score.dealbreaker_hits:
            status = _RULE_STATUS_LABEL[h.load_bearing]
            lines.append(f"  {h.label[:45]:<45} {status:<32} {h.hit_count}")
        n_fail = sum(1 for h in score.dealbreaker_hits if h.load_bearing)
        lines.append(
            "  → No hard dealbreakers."
            if n_fail == 0
            else f"  → {n_fail} hard dealbreaker(s) fired."
        )
        lines.append("")

    lines.append("Skills alignment (rule-based, keyword match)")
    if score.matched_skills:
        lines.append("  Matched:   " + ", ".join(sorted(score.matched_skills)))
    else:
        lines.append("  Matched:   (none)")
    if score.unmatched_jd_skills:
        lines.append("  Missing:   " + ", ".join(sorted(score.unmatched_jd_skills)))
    else:
        lines.append("  Missing:   (none recognized in JD outside candidate skills)")
    lines.append(
        f"  Match:     ~{score.match_pct:.0f}% (JD-relative)  |  "
        f"recognized tech weight: {score.relevant_weight:.0f}"
    )
    lines.append("")

    lines.append(f"Passed rules ({len(passed)})")
    if passed:
        for c in passed:
            lines.append(f"  ✅ [{c.category}] {c.id} — {c.label}")
            lines.append(f"      {c.reason}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"Failed rules ({len(failed)})")
    if failed:
        for c in failed:
            lines.append(f"  🔴 [{c.category}] {c.id} — {c.label}")
            lines.append(f"      {c.reason}")
    else:
        lines.append("  (none)")
    lines.append("")

    if score.rationale:
        lines.append("Rationale")
        for line in score.rationale:
            lines.append(f"  • {line}")
        lines.append("")

    lines.append(f"VERDICT: {score.verdict.upper()}  |  match ~{score.match_pct:.0f}%")
    if wrote_path is not None:
        lines.append(f"Wrote: {wrote_path}")
    elif review_path is not None:
        exists = "exists" if review_path.is_file() else "not on disk yet"
        lines.append(f"no-LLM-review.docx ({exists}): {review_path}")
        if not review_path.is_file():
            lines.append("  Tip: re-run with --write to generate it (still no LLM).")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Print (and optionally rewrite) the deterministic no-LLM review for one stored lead."
    )
    ap.add_argument("--company", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Package root for no-LLM-review.docx (default: {DEFAULT_OUTPUT_ROOT})",
    )
    ap.add_argument("--json", action="store_true", help="Machine-readable JSON (score + passed/failed rules)")
    ap.add_argument(
        "--write",
        "--docx",
        dest="write",
        action="store_true",
        help="Rewrite no-LLM-review.docx from the fresh score (no LLM call)",
    )
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"No leads DB found at {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    lead = _resolve_job(conn, args.company, args.title)
    if lead is None:
        conn.close()
        return 1

    company, title = lead["company"], lead["title"]
    jd_text = lead["jd_text"] or ""
    sibling_titles = tuple(get_sibling_titles(conn, company, exclude_title=title))
    multi_lead = len(sibling_titles) > 0
    conn.close()

    if not jd_text.strip():
        print(f"No jd_text stored for {title!r} @ {company!r} — nothing to score.", file=sys.stderr)
        return 1

    score = score_jd(jd_text)
    checks = rule_checklist(jd_text, score=score)
    review_path = _expected_review_path(
        args.output_root, company=company, title=title, multi_lead=multi_lead
    )

    wrote_path: Path | None = None
    if args.write:
        wrote_path = render_no_llm_review_docx(
            score,
            company=company,
            title=title,
            out_dir=args.output_root,
            multi_lead=multi_lead,
            sibling_titles=sibling_titles,
        )

    if args.json:
        sections = _checks_to_sections(checks)
        payload = {
            "company": company,
            "title": title,
            "normalized_key": lead["normalized_key"],
            # Top-level for scripting — same fields as score.verdict / score.match_pct.
            "verdict": score.verdict,
            "match_pct": score.match_pct,
            "score": _score_to_dict(score),
            **sections,
            "passed_rule_count": len(sections["passed_rules"]),
            "failed_rule_count": len(sections["failed_rules"]),
            "no_llm_review_path": str(wrote_path or review_path),
            "wrote_docx": wrote_path is not None,
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(
        format_no_llm_review(
            score,
            checks,
            company=company,
            title=title,
            review_path=review_path,
            wrote_path=wrote_path,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
