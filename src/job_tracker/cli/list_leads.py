"""CLI: review or export stored job leads without re-running the pipeline."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from job_tracker.pipeline.llm_apply import EvaluationResult, render_jd_review
from job_tracker.pipeline.models import LEAD_STAGES
from job_tracker.pipeline.store import DEFAULT_DB_PATH, advance_status, connect, list_leads

_COLUMNS = [
    "company",
    "title",
    "match_pct",
    "verdict",
    "llm_match_pct",
    "llm_verdict",
    "llm_job_summary",
    "llm_rationale",
    "llm_structural_verdict",
    "llm_next_step",
    "llm_cover_letter_strategy",
    "status",
    "apply_url",
    "jd_resolved",
    "jd_source",
    "times_seen",
    "first_seen",
    "last_seen",
    "pursued_at",
    "package_generated_at",
    "applied_at",
    "following_up_at",
    "interviewing_at",
    "offered_at",
    "accepted_at",
    "started_at",
    "skipped_at",
    "jd_text",
]


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["matched_skills"] = json.loads(d.get("matched_skills") or "[]")
    d["rationale"] = json.loads(d.get("rationale") or "[]")
    d["llm_dealbreaker_notes"] = json.loads(d.get("llm_dealbreaker_notes") or "[]")
    d["llm_skills_alignment"] = json.loads(d.get("llm_skills_alignment") or "[]")
    d["llm_flags"] = json.loads(d.get("llm_flags") or "[]")
    d["llm_framing_guidance"] = json.loads(d.get("llm_framing_guidance") or "[]")
    d["llm_interview_prep"] = json.loads(d.get("llm_interview_prep") or "[]")
    return d


def _coerce_legacy_notes(items: list, *, string_field: str) -> list[dict]:
    """Leads evaluated before the 2026-07-07 richer-schema change stored
    llm_dealbreaker_notes/llm_skills_alignment as flat lists of prose strings,
    not {check/status/notes} or {requirement/evidence/strength} dicts —
    render_jd_review()/_docx() index into them with `.get(...)` and crash on a
    bare str. Wrap any legacy string entries into a minimal dict (all other
    fields blank) so old leads still render instead of raising; dict entries
    (the current schema) pass through untouched."""
    return [item if isinstance(item, dict) else {string_field: item} for item in items]


def _row_to_evaluation(r: dict) -> EvaluationResult:
    """Reconstruct an EvaluationResult purely from stored DB columns, so the
    full JD review can be re-rendered on demand (--show-review) without
    needing to track the review .md file's path separately."""
    return EvaluationResult(
        verdict=r.get("llm_verdict") or "",
        match_pct=r.get("llm_match_pct") or 0.0,
        job_summary=r.get("llm_job_summary") or "",
        dealbreaker_checks=_coerce_legacy_notes(r["llm_dealbreaker_notes"], string_field="notes"),
        skills_alignment=_coerce_legacy_notes(r["llm_skills_alignment"], string_field="evidence"),
        flags=r["llm_flags"],
        rationale=r.get("llm_rationale") or "",
        framing_guidance=r["llm_framing_guidance"],
        structural_verdict=r.get("llm_structural_verdict") or "",
        next_step=r.get("llm_next_step") or "",
        cover_letter_strategy=r.get("llm_cover_letter_strategy") or "",
        interview_prep=r["llm_interview_prep"],
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Review or export stored job leads.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--verdict", choices=["pursue", "review", "pass"], help="Filter by keyword-scorer verdict")
    ap.add_argument("--llm-verdict", choices=["pursue", "review", "pass"], help="Filter by LLM-evaluator verdict")
    ap.add_argument("--status", help=f"Filter by status/stage ({', '.join(LEAD_STAGES)})")
    ap.add_argument("--company", help="Filter to companies containing this text (case-insensitive)")
    ap.add_argument("--title", help="Filter to titles containing this text (case-insensitive)")
    ap.add_argument("--csv", type=Path, help="Write results to this CSV path instead of printing")
    ap.add_argument("--json", action="store_true", help="Print full JSON (including rationale, jd_text) instead of a table")
    ap.add_argument(
        "--show-jd-text",
        action="store_true",
        help="Print the full stored JD text for each matching lead (best combined with --company/--title to narrow to one)",
    )
    ap.add_argument(
        "--show-review",
        action="store_true",
        help="Print the full JD review (job summary, dealbreaker sweep, skills alignment, flags, "
        "recommendation) for each matching lead — re-rendered from stored data, not re-evaluated "
        "(best combined with --company/--title to narrow to one)",
    )
    ap.add_argument(
        "--set-status",
        choices=LEAD_STAGES,
        help="Advance all matching rows to this lifecycle stage (e.g. --company Acme --title "
        "'Software Engineer' --set-status applied). Stamps the matching <stage>_at column with "
        "--on (or now) — see models.LEAD_STAGES for the full applied -> interviewing -> offered -> "
        "accepted -> started path.",
    )
    ap.add_argument(
        "--on",
        help="ISO date/timestamp for --set-status's stage column (default: now). E.g. --on 2026-07-10",
    )
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db} — run scripts/run_pipeline.py first.", file=sys.stderr)
        return 1

    conn = connect(args.db)
    rows = [_row_to_dict(r) for r in list_leads(conn, verdict=args.verdict)]
    if args.llm_verdict:
        rows = [r for r in rows if r["llm_verdict"] == args.llm_verdict]
    if args.status:
        rows = [r for r in rows if r["status"] == args.status]
    if args.company:
        needle = args.company.lower()
        rows = [r for r in rows if needle in r["company"].lower()]
    if args.title:
        needle = args.title.lower()
        rows = [r for r in rows if needle in r["title"].lower()]

    if args.set_status:
        for r in rows:
            advance_status(conn, r["normalized_key"], args.set_status, when=args.on)
        print(f"Updated {len(rows)} row(s) to status={args.set_status}")
        conn.close()
        return 0

    conn.close()

    if not rows:
        print("No matching leads.")
        return 0

    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k) for k in _COLUMNS})
        print(f"Wrote {len(rows)} leads to {args.csv}")
        return 0

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    if args.show_jd_text:
        for i, r in enumerate(rows):
            if i:
                print("\n" + "=" * 70 + "\n")
            print(f"{r['title']} @ {r['company']}  [{r['jd_source'] or 'no jd text stored'}]")
            print("-" * 70)
            print(r.get("jd_text") or "(no JD text stored for this lead)")
        return 0

    if args.show_review:
        for i, r in enumerate(rows):
            if i:
                print("\n" + "=" * 70 + "\n")
            if not r.get("llm_verdict"):
                print(f"{r['title']} @ {r['company']}  — not yet LLM-evaluated, no review available")
                continue
            print(render_jd_review(_row_to_evaluation(r), company=r["company"], title=r["title"]))
        return 0

    header = (
        f"{'KW%':>5}  {'KW-VERD':<8} {'LLM%':>5}  {'LLM-VERD':<8} {'STATUS':<9} "
        f"{'COMPANY':<24} {'TITLE':<40}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        llm_pct = f"{r['llm_match_pct']:>4.0f}%" if r.get("llm_match_pct") is not None else "  n/a"
        llm_verdict = r.get("llm_verdict") or "-"
        print(
            f"{r['match_pct']:>4.0f}%  {r['verdict']:<8} {llm_pct}  {llm_verdict:<8} {r['status']:<9} "
            f"{r['company'][:24]:<24} {r['title'][:40]:<40}"
        )
    print(f"\n{len(rows)} lead(s). ('KW' = keyword scorer, 'LLM' = CLAUDE.md framework via Anthropic API, n/a = not yet LLM-evaluated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
