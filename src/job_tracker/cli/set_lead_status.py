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

from job_tracker.pipeline.models import LEAD_STAGES, JobConversation
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    add_job_conversation,
    advance_status,
    append_lead_note,
    connect,
    utc_now_iso,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Set one lead's status by normalized_key.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--key", required=True, help="Exact job_leads.normalized_key")
    ap.add_argument(
        "--status",
        required=True,
        choices=LEAD_STAGES,
        help="Target lifecycle stage ('reopened' undoes a close)",
    )
    ap.add_argument(
        "--reason",
        default="",
        help="Optional free-text reason, appended to job_leads.notes as a timestamped line",
    )
    ap.add_argument("--on", help="ISO date/timestamp for the stage column (default: now)")
    ap.add_argument(
        "--sent-channel",
        choices=("linkedin", "email", "other"),
        help="Also log --sent-message as an outbound message on this channel (restarts the wait clock)",
    )
    ap.add_argument("--sent-message", default="", help="Text Shawn sent (with --sent-channel)")
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

    if args.sent_channel and not args.sent_message.strip():
        print("--sent-channel needs --sent-message.", file=sys.stderr)
        conn.close()
        return 1

    prior_status = row["status"]
    if args.sent_channel:
        when = args.on or utc_now_iso()
        add_job_conversation(
            conn,
            JobConversation(
                job_key=args.key,
                message_id=f"status-sent:{args.key}:{when}",
                channel=args.sent_channel,
                direction="outbound",
                summary=f"Sent with status {args.status} (pending-actions)",
                occurred_at=when,
                body_text=args.sent_message.strip(),
            ),
        )
    # force=True: this is always a deliberate human action (CLI or the UI's
    # Manage-lead control), same reasoning as list_leads.py --set-status —
    # unlike pipeline callers, it may legitimately revive an off-ramp lead
    # (skipped -> applied, etc.) or otherwise move backwards.
    target = args.status
    advance_status(conn, args.key, target, when=args.on, force=True)
    if target != "deleted":
        conn.execute("UPDATE job_leads SET deleted_at = NULL WHERE normalized_key = ?", (args.key,))
    if target == "reopened":
        # Latest reopen, not the first — a lead can be closed and reopened more than once.
        conn.execute(
            "UPDATE job_leads SET reopened_at = ? WHERE normalized_key = ?",
            (args.on or utc_now_iso(), args.key),
        )
    conn.commit()
    note = f"Status changed {prior_status} -> {target}"
    if args.reason.strip():
        note += f": {args.reason.strip()}"
    if args.sent_channel:
        note += f" (message sent via {args.sent_channel})"
    append_lead_note(conn, args.key, note)
    conn.close()

    print(f"{row['title']!r} @ {row['company']!r}: {prior_status} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
