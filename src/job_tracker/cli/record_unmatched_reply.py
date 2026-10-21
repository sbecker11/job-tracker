"""CLI: record a reply Shawn already sent (usually on LinkedIn) to a parked
`unmatched_messages` row — the Pending Actions "Reply sent" button on an
unmatched card.

LinkedIn never emails a copy of messages Shawn sends, so without this the
pipeline can't tell he replied and keeps the card under Reply to recruiter.
This resolves the message onto a lead (creating an "end-client TBD" stub when
the pitch names no company/role, like a logged call), logs the reply as an
outbound conversation, and so starts the wait clock on the recruiter.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.models import JobConversation, JobLead
from job_tracker.pipeline.signature import parse_signature
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    add_job_conversation,
    append_lead_note,
    connect,
    get_job,
    get_unmatched_message,
    resolve_unmatched_message,
    upsert_lead,
    utc_now_iso,
)


def stub_lead_names(contact_name: str, company: str = "", title: str = "") -> tuple[str, str]:
    company = company.strip() or f"End-client TBD via {contact_name.strip() or 'LinkedIn recruiter'}"
    return company, (title.strip() or "Role TBD")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--message-id", required=True, help="unmatched_messages.message_id of their message")
    ap.add_argument("--text", required=True, help="The reply as sent")
    ap.add_argument("--sent-at", default="", help="When it was sent (ISO 8601); default now")
    ap.add_argument("--channel", default="linkedin", choices=("linkedin", "email", "sms", "other"))
    ap.add_argument("--contact-name", default="", help="Recruiter name (default: from their signature)")
    ap.add_argument("--company", default="", help="Company, if known (default: End-client TBD via <name>)")
    ap.add_argument("--title", default="", help="Role, if known (default: Role TBD)")
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db}.", file=sys.stderr)
        return 1
    text = args.text.strip()
    if not text:
        print("--text is empty.", file=sys.stderr)
        return 2

    conn = connect(args.db)
    try:
        row = get_unmatched_message(conn, args.message_id)
        if row is None:
            print(f"No unmatched_messages row for message_id={args.message_id!r}.", file=sys.stderr)
            return 1
        # Two stored copies of one InMail point at each other via superseded_by;
        # work on the surviving one so both get resolved.
        if row["superseded_by"]:
            survivor = get_unmatched_message(conn, row["superseded_by"])
            if survivor is not None:
                row = survivor

        sig = parse_signature(row["body_text"] or "")
        name = args.contact_name.strip() or sig.name
        if row["resolved_job_key"]:
            job_key = row["resolved_job_key"]
        else:
            company, title = stub_lead_names(name, args.company, args.title)
            job = get_job(conn, company, title)
            if job is None:
                lead = JobLead(
                    company=company,
                    title=title,
                    source_message_id=row["message_id"],
                    source_label="linkedin_message" if args.channel == "linkedin" else args.channel,
                    extraction_confidence=0.5,
                    verdict="review",
                    rationale=[f"Created when Shawn recorded his reply to unmatched message {row['message_id']}"],
                )
                upsert_lead(conn, lead)
                job_key = lead.normalized_key
            else:
                job_key = job["normalized_key"]
            resolve_unmatched_message(
                conn,
                row["message_id"],
                job_key,
                contact_name=name,
                contact_email=sig.email,
                contact_phone=sig.phone,
            )
            if args.message_id != row["message_id"]:
                conn.execute(
                    "UPDATE unmatched_messages SET resolved_job_key = ?, resolved_at = ? "
                    "WHERE message_id = ? AND resolved_at IS NULL",
                    (job_key, utc_now_iso(), args.message_id),
                )
                conn.commit()

        when = args.sent_at.strip() or utc_now_iso()
        add_job_conversation(
            conn,
            JobConversation(
                job_key=job_key,
                message_id=f"reply-recorded:{row['message_id']}:{when}",
                channel=args.channel,
                direction="outbound",
                summary=f"Reply to {name or 'the recruiter'} (recorded from pending-actions)",
                occurred_at=when,
                body_text=text,
            ),
        )
        append_lead_note(conn, job_key, f"Reply sent via {args.channel} — waiting on {name or 'the recruiter'}")
        print(f"Recorded reply on {job_key!r}; waiting on {name or 'the recruiter'} since {when}.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
