"""CLI: LLM catch-up pass for parked unmatched InMails.

See `pipeline/unmatched_llm_promote.py` for why this exists. Runs hourly from
`recruiting-automation/run_cycle.sh`; safe to re-run — each message gets at
most one successful (cached) LLM extraction.

Usage:
    promote_unmatched_llm.py [--newer-than 21] [--limit 20] [--dry-run]
    promote_unmatched_llm.py --all            # backfill: no age or count limit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.llm_extract import DEFAULT_MODEL
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect
from job_tracker.pipeline.unmatched_llm_promote import promote_unmatched_with_llm


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run one cached LLM company/title extraction on parked unmatched InMails that never "
        "had one, and turn every extracted company + title into a lead."
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--newer-than", type=int, default=21, metavar="DAYS", help="Only messages parked within DAYS (default: 21)")
    ap.add_argument("--limit", type=int, default=20, help="Max messages per run (default: 20)")
    ap.add_argument("--all", action="store_true", help="Ignore --newer-than and --limit (one-off backfill)")
    ap.add_argument("--llm-extraction-model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="List eligible messages; no LLM calls, no DB writes")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        results = promote_unmatched_with_llm(
            conn,
            model=args.llm_extraction_model,
            newer_than_days=None if args.all else args.newer_than,
            limit=None if args.all else args.limit,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()

    for r in results:
        print(f"[{r.action}] {r.subject[:70]!r}  ({r.message_id})")
        for key in r.job_keys:
            print(f"    -> {key}")
    promoted = sum(1 for r in results if r.job_keys)
    print(
        f"\n{len(results)} parked message(s) checked: {promoted} promoted, {len(results) - promoted} not promoted.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
