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

from job_tracker.pipeline.card_patch import spawn_card_refresh
from job_tracker.pipeline.decline import (
    DECLINE_REASONS,
    decline_unmatched,
    draft_decline_letter,
    record_decline,
)
from job_tracker.pipeline.pending_workflow import _recruiter_contact
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Decline one lead by normalized_key.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--key", default="", help="Exact job_leads.normalized_key")
    ap.add_argument(
        "--message-id",
        default="",
        help="unmatched_messages.message_id — creates a stub lead, then declines it",
    )
    ap.add_argument("--company", default="", help="With --message-id, agency or end client if known")
    ap.add_argument("--title", default="", help="With --message-id, role title (default: the subject)")
    ap.add_argument("--contact-name", default="", help="With --message-id, recruiter name")
    ap.add_argument("--reason", required=True, choices=[r.code for r in DECLINE_REASONS])
    ap.add_argument("--note", default="", help="Required for --reason other; optional extra context otherwise")
    ap.add_argument("--channel", default="other", choices=("linkedin", "email", "sms", "other"))
    ap.add_argument("--no-letter", action="store_true", help="Skip with the reason, without logging a sent letter")
    ap.add_argument("--letter-text", default="", help="The letter as actually sent (edited); default: the drafted letter")
    ap.add_argument("--print-letter", action="store_true", help="Print the drafted letter and change nothing")
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db}.", file=sys.stderr)
        return 1
    if args.reason == "other" and not args.note.strip():
        print("--reason other needs --note.", file=sys.stderr)
        return 2
    if bool(args.key) == bool(args.message_id):
        print("Pass exactly one of --key or --message-id.", file=sys.stderr)
        return 2

    conn = connect(args.db)
    try:
        if args.message_id:
            from job_tracker.cli.record_unmatched_reply import stub_lead_names
            from job_tracker.pipeline.signature import parse_signature
            from job_tracker.pipeline.store import get_unmatched_message

            parked = get_unmatched_message(conn, args.message_id)
            if parked is None:
                print(f"No unmatched message {args.message_id!r}.", file=sys.stderr)
                return 1
            sig = parse_signature(parked["body_text"] or "")
            name = args.contact_name.strip() or sig.name
            company, title = stub_lead_names(name, args.company, args.title or (parked["subject"] or ""))
            letter = draft_decline_letter(
                args.reason, recruiter_name=name, company=company, title=title, note=args.note
            )
            if args.letter_text.strip():
                letter = args.letter_text.strip()
            if args.print_letter:
                print(letter)
                return 0
            job_key = decline_unmatched(
                conn,
                args.message_id,
                code=args.reason,
                note=args.note,
                letter="" if args.no_letter else letter,
                channel=args.channel,
                company=args.company,
                title=args.title,
                contact_name=args.contact_name,
            )
            row = conn.execute(
                "SELECT company, title, 'new' AS status FROM job_leads WHERE normalized_key = ?", (job_key,)
            ).fetchone()
            refresh_key = job_key
            refresh_message = args.message_id
        else:
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
            if args.letter_text.strip():
                letter = args.letter_text.strip()
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
            refresh_key = args.key
            refresh_message = ""
    finally:
        conn.close()
    spawn_card_refresh(args.db, refresh_key, message_id=refresh_message)

    print(f"{row['title']!r} @ {row['company']!r}: {row['status']} -> skipped (declined: {args.reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
