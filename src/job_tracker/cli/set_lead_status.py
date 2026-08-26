"""CLI: set a single lead's status (with an optional reason note) by its
exact `normalized_key` — added 2026-08-26 as the backend for a "Manage
lead" status control in the Pending Actions UI (see
tools/set-lead-status/), and usable directly from a terminal too.

Distinct from `list-leads --set-status`, which matches rows by fuzzy
--company/--title text and can hit more than one row; this one requires
the exact key so a UI click can never silently touch the wrong lead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.models import LEAD_STAGES
from job_tracker.pipeline.store import DEFAULT_DB_PATH, advance_status, append_lead_note, connect


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Set one lead's status by normalized_key.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--key", required=True, help="Exact job_leads.normalized_key")
    ap.add_argument("--status", required=True, choices=LEAD_STAGES, help="Target lifecycle stage")
    ap.add_argument(
        "--reason",
        default="",
        help="Optional free-text reason, appended to job_leads.notes as a timestamped line",
    )
    ap.add_argument("--on", help="ISO date/timestamp for the stage column (default: now)")
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db}.", file=sys.stderr)
        return 1

    conn = connect(args.db)
    row = conn.execute(
        "SELECT company, title, status FROM job_leads WHERE normalized_key = ?", (args.key,)
    ).fetchone()
    if row is None:
        print(f"No lead found with normalized_key={args.key!r}.", file=sys.stderr)
        conn.close()
        return 1

    prior_status = row["status"]
    # force=True: this is always a deliberate human action (CLI or the UI's
    # Manage-lead control), same reasoning as list_leads.py --set-status —
    # unlike pipeline callers, it may legitimately revive an off-ramp lead
    # (skipped -> applied, etc.) or otherwise move backwards.
    advance_status(conn, args.key, args.status, when=args.on, force=True)
    note = f"Status changed {prior_status} -> {args.status}"
    if args.reason.strip():
        note += f": {args.reason.strip()}"
    append_lead_note(conn, args.key, note)
    conn.close()

    print(f"{row['title']!r} @ {row['company']!r}: {prior_status} -> {args.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
