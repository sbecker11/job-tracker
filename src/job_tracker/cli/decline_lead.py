"""CLI: decline one lead — log the "no thank you" letter Shawn just sent (or
skip without one) and mark the lead skipped with a structured reason.

Backend for the Pending Actions "Decline" form (tools/decline-lead/,
`declinelead://decline?key=...&reason=...`); usable from a terminal too.
Never sends anything itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.decline import DECLINE_REASONS, draft_decline_letter, record_decline
from job_tracker.pipeline.pending_workflow import _recruiter_contact
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Decline one lead by normalized_key.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--key", required=True, help="Exact job_leads.normalized_key")
    ap.add_argument("--reason", required=True, choices=[r.code for r in DECLINE_REASONS])
    ap.add_argument("--note", default="", help="Required for --reason other; optional extra context otherwise")
    ap.add_argument("--channel", default="other", choices=("linkedin", "email", "other"))
    ap.add_argument("--no-letter", action="store_true", help="Skip with the reason, without logging a sent letter")
    ap.add_argument("--print-letter", action="store_true", help="Print the drafted letter and change nothing")
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db}.", file=sys.stderr)
        return 1
    if args.reason == "other" and not args.note.strip():
        print("--reason other needs --note.", file=sys.stderr)
        return 2

    conn = connect(args.db)
    try:
        row = conn.execute(
            "SELECT company, title, status FROM job_leads WHERE normalized_key = ?", (args.key,)
        ).fetchone()
        if row is None:
            print(f"No lead found with normalized_key={args.key!r}.", file=sys.stderr)
            return 1
        recruiter, _email, _phone, _relay = _recruiter_contact(conn, args.key)
        letter = draft_decline_letter(
            args.reason,
            recruiter_name=recruiter,
            company=row["company"] or "",
            title=row["title"] or "",
            note=args.note,
        )
        if args.print_letter:
            print(letter)
            return 0
        record_decline(
            conn,
            args.key,
            code=args.reason,
            note=args.note,
            letter="" if args.no_letter else letter,
            channel=args.channel,
        )
    finally:
        conn.close()

    print(f"{row['title']!r} @ {row['company']!r}: {row['status']} -> skipped (declined: {args.reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
