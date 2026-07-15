"""CLI: log a manual interaction (email/call/other conversation, or a
scheduled/completed meeting/interview) against an existing job — the
write side of `job_conversations`/`job_meetings` for anything that didn't
come from the automated triage flow (docs/JOB_CRM_VISION.md UC-5, and the
manual half of UC-1 for non-email contact).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.models import JobContact, JobConversation, JobMeeting
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    add_job_contact,
    add_job_conversation,
    add_job_meeting,
    connect,
    find_similar_jobs,
    get_job,
    set_awaiting_response,
)

_CHANNELS = ("email", "call", "other")
_DIRECTIONS = ("inbound", "outbound", "other")
_MEETING_KINDS = ("phone_screen", "onsite", "technical", "other")
_MEETING_STATUSES = ("proposed", "confirmed", "completed", "cancelled")


def _resolve_job(conn, company: str, title: str):
    job = get_job(conn, company, title)
    if job is not None:
        return job
    candidates = find_similar_jobs(conn, company, title)
    print(f"No job found for {title!r} @ {company!r}.", file=sys.stderr)
    if candidates:
        print("Did you mean one of these (use the exact company/title shown)?", file=sys.stderr)
        for m in candidates[:5]:
            print(f"  {m.title} @ {m.company}  (score={m.combined_score:.2f})", file=sys.stderr)
    else:
        print("Use scripts/add_job.py to create it first if this is a new job.", file=sys.stderr)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Log a manual conversation or meeting/interview against an existing job."
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--company", required=True)
    ap.add_argument("--title", required=True)

    kind_group = ap.add_mutually_exclusive_group(required=True)
    kind_group.add_argument("--conversation", action="store_true", help="Log an email/call/other exchange")
    kind_group.add_argument("--meeting", action="store_true", help="Log a scheduled/completed interview or call")

    # Conversation fields.
    ap.add_argument("--channel", choices=_CHANNELS, default="email", help="Conversation channel (default: email)")
    ap.add_argument(
        "--direction",
        choices=_DIRECTIONS,
        help="Conversation direction — required with --conversation. 'outbound' (you spoke) marks the job "
        "awaiting-response; 'inbound' (they spoke) clears it; 'other' leaves it untouched.",
    )
    ap.add_argument("--summary", help="Conversation summary — required with --conversation")
    ap.add_argument("--occurred-at", help="ISO8601 timestamp (default: now)")

    # Meeting fields.
    ap.add_argument("--kind", choices=_MEETING_KINDS, default="other", help="Meeting kind (default: other)")
    ap.add_argument("--status", choices=_MEETING_STATUSES, default="proposed", help="Meeting status (default: proposed)")
    ap.add_argument("--notes", default="", help="Meeting notes")
    ap.add_argument("--scheduled-at", default="", help="ISO8601 timestamp the meeting is/was scheduled for")

    # Optional contact to attach/create alongside this entry.
    ap.add_argument("--contact-name", default="")
    ap.add_argument("--contact-email", default="")
    ap.add_argument("--contact-phone", default="")
    ap.add_argument("--contact-role", default="recruiter", choices=("recruiter", "hiring_manager", "referral", "other"))

    # Manual override for "whose turn is it" — see store.add_job_conversation/
    # set_awaiting_response docstrings.
    waiting_group = ap.add_mutually_exclusive_group()
    waiting_group.add_argument("--waiting", action="store_true", help="Force-mark this job as awaiting a response")
    waiting_group.add_argument("--not-waiting", action="store_true", help="Force-clear awaiting-response on this job")

    args = ap.parse_args(argv)

    if args.conversation and not (args.direction and args.summary):
        ap.error("--conversation requires both --direction and --summary")

    conn = connect(args.db)
    try:
        job = _resolve_job(conn, args.company, args.title)
        if job is None:
            return 1
        job_key = job["normalized_key"]

        contact_id = None
        if args.contact_name or args.contact_email:
            contact_id = add_job_contact(
                conn,
                JobContact(
                    job_key=job_key,
                    name=args.contact_name,
                    email=args.contact_email,
                    phone=args.contact_phone,
                    role=args.contact_role,
                ),
            )

        awaiting_override = True if args.waiting else False if args.not_waiting else None

        if args.conversation:
            kwargs = dict(
                job_key=job_key,
                contact_id=contact_id,
                channel=args.channel,
                direction=args.direction,
                summary=args.summary,
            )
            if args.occurred_at:
                kwargs["occurred_at"] = args.occurred_at
            add_job_conversation(conn, JobConversation(**kwargs), awaiting_response=awaiting_override)
            print(f"Logged {args.direction} {args.channel} conversation for {args.title} @ {args.company}.")
        else:
            add_job_meeting(
                conn,
                JobMeeting(
                    job_key=job_key,
                    contact_id=contact_id,
                    kind=args.kind,
                    status=args.status,
                    notes=args.notes,
                    scheduled_at=args.scheduled_at,
                ),
            )
            if awaiting_override is not None:
                set_awaiting_response(conn, job_key, awaiting_override)
            print(f"Logged {args.kind} meeting ({args.status}) for {args.title} @ {args.company}.")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
