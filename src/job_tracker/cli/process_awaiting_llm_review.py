"""CLI: automated sweep that finally closes the "Awaiting full-LLM-review"
loop (2026-07-19).

Background: two separate gaps fed leads into that dashboard bucket
(`store.list_leads_awaiting_full_llm_review`'s docstring has the full
story) with nothing ever revisiting them — `scan_communications.py`'s
deliberate "no happy path" stub-lead creation being the main one in
practice, but a normal digest whose free rule-based score cleared the LLM
gate before `triage_recruiter_inbox.py` got to the real LLM call can land
here too. Verified live: 21 leads sitting in this state, several 12+ days
old, a 100%-match lead among them — the dashboard's own comment on that
bucket ("wait for the pipeline") promised automation that didn't exist.

This command is that automation: for every eligible lead (see
`store.list_leads_awaiting_full_llm_review`), it runs the exact same
two-tier pipeline `apply_package.py` runs by hand on one lead at a time
(`pipeline/llm_apply.generate_two_tier_package`) — full-LLM-review always
(these leads already cleared the score gate that decides whether to spend
the LLM call), résumé + cover letter only on an actual "pursue" verdict —
and advances each lead's `status` exactly the way `triage_recruiter_inbox.py`
does after generating a package, so this doesn't leave a second inconsistent
code path for the same state transition.

Safe to run every hour: leads only ever leave the candidate set (by getting
an `llm_verdict` stamped), never re-enter it, so a lead already processed
this cycle is never re-billed next cycle. `--limit` exists as a spend
circuit-breaker for a backlog catch-up run, not because normal hourly
volume is expected to be large.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, DEFAULT_OUTPUT_ROOT, generate_two_tier_package
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    advance_status,
    connect,
    get_sibling_titles,
    list_leads_awaiting_full_llm_review,
    update_llm_evaluation,
)
from job_tracker.scoring.scorer import DEFAULT_FRAMEWORK_PATH, load_framework


def _llm_review_gate_pct(framework_path: Path) -> float:
    framework = load_framework(framework_path)
    return float((framework.get("thresholds") or {}).get("llm_review_min_pct", 70))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Sweep every lead stuck in the 'Awaiting full-LLM-review' state (cleared the free "
        "rule-based score's gate, no full LLM review yet) and run the same two-tier review "
        "apply_package.py runs by hand — full-LLM-review always, résumé + cover letter on a "
        "'pursue' verdict."
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model id/alias (default: {DEFAULT_MODEL})")
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    ap.add_argument("--framework", type=Path, default=DEFAULT_FRAMEWORK_PATH)
    ap.add_argument("--limit", type=int, help="Max number of leads to process this run")
    ap.add_argument("--dry-run", action="store_true", help="List what would be processed without calling the API")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"No leads DB found at {args.db} — run scripts/run_pipeline.py first.", file=sys.stderr)
        return 1

    gate_pct = _llm_review_gate_pct(args.framework)

    conn = connect(args.db)
    candidates = list_leads_awaiting_full_llm_review(conn, gate_pct)
    if args.limit:
        candidates = candidates[: args.limit]

    if not candidates:
        print("Nothing awaiting full-LLM-review — every eligible lead already has an llm_verdict.")
        conn.close()
        return 0

    print(f"{len(candidates)} lead(s) awaiting full-LLM-review (score >= {gate_pct:.0f}%) with {args.model}.")
    if args.dry_run:
        for lead in candidates:
            print(f"  - {lead['title']} @ {lead['company']}  ({lead['match_pct']:.0f}% rule-based match)")
        conn.close()
        return 0

    total_cost = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    pursue: list[str] = []
    review: list[str] = []
    passed = 0
    errors: list[str] = []

    for i, lead in enumerate(candidates, start=1):
        label = f"{lead['title']} @ {lead['company']}"
        print(f"[{i}/{len(candidates)}] {label} ...", end=" ", flush=True)
        start = time.monotonic()
        sibling_titles = tuple(get_sibling_titles(conn, lead["company"], exclude_title=lead["title"]))
        try:
            result = generate_two_tier_package(
                lead["jd_text"],
                company=lead["company"],
                title=lead["title"],
                apply_url=lead["apply_url"] or "",
                model=args.model,
                output_root=args.output_root,
                multi_lead=len(sibling_titles) > 0,
                sibling_titles=sibling_titles,
            )
        except Exception as exc:  # noqa: BLE001 — one bad lead shouldn't kill the whole sweep
            print(f"ERROR ({exc})")
            errors.append(f"{label}: {exc}")
            continue

        elapsed = time.monotonic() - start
        if not result.ran_full_llm_review:
            # The score gate got re-checked against a freshly recomputed
            # score inside generate_two_tier_package and this lead no
            # longer clears it (e.g. scoring.scorer's rules changed since
            # match_pct was last stored) — nothing to persist, leave it at
            # "new" for the next dashboard rescore to re-triage correctly.
            print(f"below gate on rescore ({result.no_llm_score.match_pct:.0f}%), skipped")
            continue

        evaluation = result.evaluation
        update_llm_evaluation(conn, lead["normalized_key"], evaluation)

        if result.resume_path is not None:
            advance_status(conn, lead["normalized_key"], "package_generated")
        elif evaluation.verdict == "pass":
            advance_status(conn, lead["normalized_key"], "skipped")
        # "review" verdicts deliberately stay at "new" — a human decision,
        # not something this sweep infers (same rule triage_recruiter_inbox
        # applies after its own generate_two_tier_package call).

        cost = evaluation.metrics.cost_usd if evaluation.metrics else None
        total_cost += cost or 0.0
        total_input_tokens += evaluation.metrics.input_tokens if evaluation.metrics else 0
        total_output_tokens += evaluation.metrics.output_tokens if evaluation.metrics else 0
        cost_str = f"${cost:.4f}" if cost is not None else "n/a"
        print(f"{evaluation.verdict.upper()} ({evaluation.match_pct:.0f}%, {elapsed:.1f}s, {cost_str})")

        if evaluation.verdict == "pursue":
            pursue.append(label)
        elif evaluation.verdict == "review":
            review.append(label)
        else:
            passed += 1

    conn.close()

    print("\n" + "=" * 70)
    print(f"Processed {len(candidates) - len(errors)} lead(s) — {len(pursue)} pursue, {len(review)} review, {passed} pass")
    if errors:
        print(f"{len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")
    print(f"Total: {total_input_tokens} in / {total_output_tokens} out tokens, ${total_cost:.4f}")

    if pursue:
        print("\n=== PURSUE (package generated) ===")
        for label in pursue:
            print(f"  - {label}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
