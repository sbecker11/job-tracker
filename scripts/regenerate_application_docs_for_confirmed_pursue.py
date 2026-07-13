#!/usr/bin/env python3
"""Re-render résumé + cover letter for every lead already confirmed
`verdict='pursue' AND llm_verdict='pursue'` — WITHOUT re-running the paid
`evaluate_lead` step, since a fresh, valid `full-LLM-review.docx` already
exists for each of these from the 2026-07-12 batch run.

This only re-spends on the "generate" LLM call (résumé + cover letter
content) and re-renders through the fixed `render_resume`/`render_cover_letter`
(letter_style.py template fix, 2026-07-12) — the existing `full-LLM-review.docx`,
`no-LLM-review.docx`, and `JobDescription.docx` are left untouched.

Usage:
    python scripts/regenerate_application_docs_for_confirmed_pursue.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.llm_apply import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_ROOT,
    _check_house_rules,
    _generate_content,
    _repair_house_rule_violations,
    _sum_metrics,
    render_cover_letter,
    render_resume,
)
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, get_sibling_titles  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    rows = conn.execute(
        "SELECT normalized_key, company, title, jd_text FROM job_leads "
        "WHERE verdict = 'pursue' AND llm_verdict = 'pursue' "
        "AND jd_text IS NOT NULL AND jd_text != '' "
        "ORDER BY company, title"
    ).fetchall()
    leads = [dict(r) for r in rows]
    if args.limit:
        leads = leads[: args.limit]

    print(f"{len(leads)} confirmed-pursue leads to regenerate application docs for.")
    if args.dry_run:
        for lead in leads:
            print(f"  - {lead['company']} / {lead['title']}")
        conn.close()
        return 0

    ok = 0
    errors = 0
    total_cost = 0.0

    for i, lead in enumerate(leads, 1):
        company, title, jd_text = lead["company"], lead["title"], lead["jd_text"]
        sibling_titles = tuple(get_sibling_titles(conn, company, exclude_title=title))
        multi_lead = len(sibling_titles) > 0

        print(f"[{i}/{len(leads)}] {company} / {title} ...", flush=True)
        try:
            content, generate_calls = _generate_content(jd_text, company=company, title=title, model=args.model)
            warnings = _check_house_rules(content, company=company)
            if warnings:
                content, repair_calls = _repair_house_rule_violations(
                    content, issues=warnings, model=args.model
                )
                generate_calls += repair_calls
                warnings = _check_house_rules(content, company=company)

            resume_path = render_resume(
                content.get("resume") or {}, company=company, title=title,
                out_dir=args.output_root, multi_lead=multi_lead, sibling_titles=sibling_titles,
            )
            cover_letter_path = render_cover_letter(
                content.get("cover_letter") or {}, company=company, title=title,
                out_dir=args.output_root, multi_lead=multi_lead, sibling_titles=sibling_titles,
            )
            metrics = _sum_metrics("generate", args.model, generate_calls)
            cost = metrics.cost_usd if metrics and metrics.cost_usd is not None else 0.0
            total_cost += cost
            ok += 1
            warn_suffix = f"  ⚠ warnings: {warnings}" if warnings else ""
            print(f"    -> {resume_path.name}, {cover_letter_path.name}  (${cost:.4f}){warn_suffix}", flush=True)
        except Exception as exc:  # noqa: BLE001 - keep the batch going on a per-lead failure
            errors += 1
            print(f"    ERROR: {exc}", flush=True)
            continue

    conn.close()
    print()
    print(f"Done. regenerated: {ok}  errors: {errors}  total LLM cost: ${total_cost:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
