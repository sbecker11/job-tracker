#!/usr/bin/env python3
"""Batch: force the paid full-LLM-review step for every stored lead whose
DB `verdict` is currently 'pursue' — regardless of whether the free
no-LLM-review score clears the normal 70% gate (via
`force_llm_review=True` on `generate_two_tier_package`).

For each such lead:
  - Runs the LLM evaluation -> writes `full-LLM-review.docx`.
  - Persists the `llm_*` columns via `update_llm_evaluation`.
  - If the fresh LLM verdict == 'pursue': also generates a résumé + cover
    letter (`generate_two_tier_package`'s normal behavior) — restoring the
    application package that `move_packages_without_full_llm_review.py`
    quarantined earlier for genuinely-still-good leads.
  - If the fresh LLM verdict != 'pursue': downgrades `job_leads.verdict`
    in the DB to match, since a real review now disagrees with the
    earlier rule-based/triage call.

Usage:
    python scripts/run_full_llm_review_for_pursue_leads.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, DEFAULT_OUTPUT_ROOT, generate_two_tier_package  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, get_sibling_titles, update_llm_evaluation  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    ap.add_argument("--limit", type=int, default=None, help="Process only the first N leads (for testing)")
    ap.add_argument("--dry-run", action="store_true", help="List leads that would be processed, make no LLM calls")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    rows = conn.execute(
        "SELECT normalized_key, company, title, jd_text FROM job_leads "
        "WHERE verdict = 'pursue' AND jd_text IS NOT NULL AND jd_text != '' "
        "ORDER BY company, title"
    ).fetchall()
    leads = [dict(r) for r in rows]
    if args.limit:
        leads = leads[: args.limit]

    print(f"{len(leads)} pursue leads with stored JD text to process.")
    if args.dry_run:
        for lead in leads:
            print(f"  - {lead['company']} / {lead['title']}")
        conn.close()
        return 0

    confirmed = 0
    downgraded = 0
    errors = 0
    total_cost = 0.0

    for i, lead in enumerate(leads, 1):
        company, title, key, jd_text = lead["company"], lead["title"], lead["normalized_key"], lead["jd_text"]
        sibling_titles = tuple(get_sibling_titles(conn, company, exclude_title=title))

        print(f"[{i}/{len(leads)}] {company} / {title} ...", flush=True)
        try:
            result = generate_two_tier_package(
                jd_text,
                company=company,
                title=title,
                model=args.model,
                output_root=args.output_root,
                force_llm_review=True,
                multi_lead=len(sibling_titles) > 0,
                sibling_titles=sibling_titles,
            )
        except Exception as exc:  # noqa: BLE001 - keep the batch going on a per-lead failure
            errors += 1
            print(f"    ERROR: {exc}", flush=True)
            continue

        if result.evaluation is None:
            print("    -> no evaluation produced (unexpected)", flush=True)
            continue

        update_llm_evaluation(conn, key, result.evaluation)
        total_cost += result.total_cost_usd or 0.0

        if result.evaluation.verdict == "pursue":
            confirmed += 1
            suffix = " ; résumé + cover letter generated" if result.resume_path else ""
            print(f"    -> pursue confirmed (match {result.evaluation.match_pct:.0f}%){suffix}", flush=True)
        else:
            downgraded += 1
            conn.execute(
                "UPDATE job_leads SET verdict = ? WHERE normalized_key = ?",
                (result.evaluation.verdict, key),
            )
            print(
                f"    -> DOWNGRADED to '{result.evaluation.verdict}' (match {result.evaluation.match_pct:.0f}%); "
                "DB verdict updated",
                flush=True,
            )
        conn.commit()

    conn.close()
    print()
    print(
        f"Done. confirmed pursue: {confirmed}  downgraded: {downgraded}  errors: {errors}  "
        f"total LLM cost: ${total_cost:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
