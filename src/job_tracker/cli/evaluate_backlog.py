"""CLI: run the LLM-based JD Match Framework (pipeline/llm_apply.evaluate_lead)
over every stored lead that has JD text but hasn't been LLM-evaluated yet.

Deliberately evaluate-only (cheap, ~$0.02-0.04/lead) — résumé/cover-letter
generation (apply-package) is a separate, more expensive step you run by
hand against the leads this confirms are worth pursuing. This keeps the
person in the loop on which leads actually get a generated package, rather
than silently generating documents for everything the model calls "pursue".
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, evaluate_lead
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, list_leads_needing_llm_eval, update_llm_evaluation


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the LLM JD Match Framework over stored leads that haven't been LLM-evaluated yet."
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model id/alias (default: {DEFAULT_MODEL})")
    ap.add_argument("--limit", type=int, help="Max number of leads to evaluate this run")
    ap.add_argument("--dry-run", action="store_true", help="List what would be evaluated without calling the API")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"No leads DB found at {args.db} — run scripts/run_pipeline.py first.", file=sys.stderr)
        return 1

    conn = connect(args.db)
    candidates = list_leads_needing_llm_eval(conn)
    if args.limit:
        candidates = candidates[: args.limit]

    if not candidates:
        print("Nothing to evaluate — every lead with JD text has already been LLM-evaluated.")
        conn.close()
        return 0

    print(f"{len(candidates)} lead(s) to evaluate with {args.model}.")
    if args.dry_run:
        for lead in candidates:
            print(f"  - {lead['title']} @ {lead['company']}")
        conn.close()
        return 0

    total_cost = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    pursue: list[tuple[str, dict]] = []
    review: list[tuple[str, dict]] = []
    passed = 0
    errors: list[str] = []

    for i, lead in enumerate(candidates, start=1):
        label = f"{lead['title']} @ {lead['company']}"
        print(f"[{i}/{len(candidates)}] {label} ...", end=" ", flush=True)
        start = time.monotonic()
        try:
            evaluation = evaluate_lead(
                lead["jd_text"], company=lead["company"], title=lead["title"], model=args.model
            )
        except Exception as exc:  # noqa: BLE001 — one bad lead shouldn't kill the whole batch
            print(f"ERROR ({exc})")
            errors.append(f"{label}: {exc}")
            continue

        update_llm_evaluation(conn, lead["normalized_key"], evaluation)
        elapsed = time.monotonic() - start
        cost = evaluation.metrics.cost_usd if evaluation.metrics else None
        total_cost += cost or 0.0
        total_input_tokens += evaluation.metrics.input_tokens if evaluation.metrics else 0
        total_output_tokens += evaluation.metrics.output_tokens if evaluation.metrics else 0

        cost_str = f"${cost:.4f}" if cost is not None else "n/a"
        print(f"{evaluation.verdict.upper()} ({evaluation.match_pct:.0f}%, {elapsed:.1f}s, {cost_str})")

        row = {"company": lead["company"], "title": lead["title"], "apply_url": lead["apply_url"], "evaluation": evaluation}
        if evaluation.verdict == "pursue":
            pursue.append((label, row))
        elif evaluation.verdict == "review":
            review.append((label, row))
        else:
            passed += 1

    conn.close()

    print("\n" + "=" * 70)
    print(f"Evaluated {len(candidates) - len(errors)} lead(s) — {len(pursue)} pursue, {len(review)} review, {passed} pass")
    if errors:
        print(f"{len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")
    print(f"Total: {total_input_tokens} in / {total_output_tokens} out tokens, ${total_cost:.4f}")

    if pursue:
        print("\n=== PURSUE ===")
        for label, row in pursue:
            ev = row["evaluation"]
            print(f"  [{ev.match_pct:.0f}%] {label}  {row['apply_url']}")
            print(f"           {ev.rationale}")

    if review:
        print("\n=== REVIEW ===")
        for label, row in review:
            ev = row["evaluation"]
            print(f"  [{ev.match_pct:.0f}%] {label}  {row['apply_url']}")

    if pursue:
        print(
            "\nTo generate a résumé + cover letter for a pursue lead:\n"
            '  python scripts/apply_package.py --company "<company>" --title "<title>"'
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
