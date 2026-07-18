"""CLI: resolve one parked `unmatched_messages` row (scan_communications.py's
"couldn't confidently match this" queue — see that module's docstring)
against a real job, creating the JobContact/JobConversation Tier 1/2/3
matching couldn't create automatically.

Use `--list` first to see what's waiting (a ~160-char preview per message);
the message_id it prints is what `--message-id` below expects. Use
`--message-id <id> --show` to read one in full before deciding how (or
whether) to resolve it.

Recruiter contact info (name/email/phone) is auto-detected from the
message body via `pipeline/signature.py` (2026-07-17) whenever
--contact-name/--contact-email/--contact-phone are all left blank — pass
any of those explicitly to override, or --no-auto-signature to skip
detection entirely.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.signature import parse_signature
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    connect,
    find_similar_jobs,
    get_job,
    get_unmatched_message,
    list_unmatched_messages,
    resolve_unmatched_message,
    upsert_lead,
)


def _print_unmatched_list(conn) -> None:
    rows = list_unmatched_messages(conn)
    if not rows:
        print("No unmatched communications waiting for review.")
        return
    print(f"{len(rows)} unmatched communication(s):\n")
    for row in rows:
        print(f"- message_id={row['message_id']}  ({row['direction']}, detected {row['detected_at']})")
        print(f"    subject: {row['subject']!r}")
        print(f"    from={row['from_address']!r}  to={row['to_address']!r}")
        preview = (row["body_text"] or "").strip().replace("\n", " ")[:160]
        print(f"    preview: {preview!r}\n")
    print("Use --message-id <id> --show to read one in full.")


def _print_full_message(row) -> None:
    """The `--list` preview above is deliberately truncated to ~160 chars so
    the list itself stays scannable — this is the "actually read the whole
    thing" counterpart, for deciding what --company/--title (and --create)
    to resolve it with."""
    print(f"message_id: {row['message_id']}")
    print(f"thread_id:  {row['thread_id'] or '(none)'}")
    print(f"direction:  {row['direction']}")
    print(f"from:       {row['from_address'] or '(none)'}")
    print(f"to:         {row['to_address'] or '(none)'}")
    print(f"subject:    {row['subject']}")
    print(f"detected:   {row['detected_at']}")
    print()
    print(row["body_text"] or "(no body text stored)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--list", action="store_true", help="List all unresolved unmatched communications and exit")
    ap.add_argument(
        "--show",
        action="store_true",
        help="Print the full stored text for --message-id and exit (no resolving) — --list's preview is "
        "truncated to ~160 chars; this is the full body",
    )
    ap.add_argument("--message-id", help="The unmatched message to resolve (or to --show in full)")
    ap.add_argument("--company", help="Company of the job this message belongs to")
    ap.add_argument("--title", help="Title of the job this message belongs to")
    ap.add_argument(
        "--create",
        action="store_true",
        help="If --company/--title don't match any existing job, create a new stub lead instead of erroring "
        "(use for a genuinely brand-new lead surfaced only through this communication, e.g. a vague first "
        "pitch with no JD yet)",
    )
    ap.add_argument(
        "--contact-name",
        default="",
        help="Overrides whatever pipeline.signature auto-detects from the message body (see --show to preview it)",
    )
    ap.add_argument("--contact-email", default="")
    ap.add_argument("--contact-phone", default="")
    ap.add_argument(
        "--no-auto-signature",
        action="store_true",
        help="Skip the automatic name/email/phone signature-block detection entirely, even when --contact-* "
        "flags are also left blank (rare — e.g. a signature block that misfired)",
    )
    ap.add_argument("--contact-role", default="recruiter", choices=("recruiter", "hiring_manager", "referral", "other"))
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.list:
            _print_unmatched_list(conn)
            return 0

        if args.show:
            if not args.message_id:
                ap.error("--message-id is required with --show")
            unmatched = get_unmatched_message(conn, args.message_id)
            if unmatched is None:
                print(f"No unmatched_messages row for message_id={args.message_id!r}.", file=sys.stderr)
                print("Run with --list to see what's actually waiting.", file=sys.stderr)
                return 1
            _print_full_message(unmatched)
            return 0

        if not args.message_id or not args.company or not args.title:
            ap.error("--message-id, --company, and --title are required unless --list is given")

        unmatched = get_unmatched_message(conn, args.message_id)
        if unmatched is None:
            print(f"No unmatched_messages row for message_id={args.message_id!r}.", file=sys.stderr)
            print("Run with --list to see what's actually waiting.", file=sys.stderr)
            return 1
        if unmatched["resolved_at"]:
            print(
                f"message_id={args.message_id!r} was already resolved to {unmatched['resolved_job_key']!r} "
                f"at {unmatched['resolved_at']}.",
                file=sys.stderr,
            )
            return 1

        job = get_job(conn, args.company, args.title)
        if job is None:
            if not args.create:
                print(f"No job found for {args.title!r} @ {args.company!r}.", file=sys.stderr)
                candidates = find_similar_jobs(conn, args.company, args.title)
                if candidates:
                    print("Did you mean one of these (use the exact company/title shown)?", file=sys.stderr)
                    for m in candidates[:5]:
                        print(f"  {m.title} @ {m.company}  (score={m.combined_score:.2f})", file=sys.stderr)
                print("Pass --create to make a new stub lead instead.", file=sys.stderr)
                return 1
            lead = JobLead(
                company=args.company,
                title=args.title,
                source_message_id=args.message_id,
                source_label="linkedin_message",
                extraction_confidence=0.5,
                verdict="review",
                rationale=[f"Created from an unmatched communication ({args.message_id}) via resolve_communication.py"],
            )
            upsert_lead(conn, lead)
            job_key = lead.normalized_key
            print(f"Created new stub lead {args.title!r} @ {args.company!r}.")
        else:
            job_key = job["normalized_key"]

        contact_name, contact_email, contact_phone = args.contact_name, args.contact_email, args.contact_phone
        if not args.no_auto_signature and not (contact_name or contact_email or contact_phone):
            detected = parse_signature(unmatched["body_text"] or "")
            if detected:
                contact_name, contact_email, contact_phone = detected.name, detected.email, detected.phone
                print(
                    f"Auto-detected from message body: name={contact_name!r} email={contact_email!r} "
                    f"phone={contact_phone!r} (pass --contact-name/--contact-email/--contact-phone to override, "
                    "or --no-auto-signature to skip)."
                )

        conversation_id = resolve_unmatched_message(
            conn,
            args.message_id,
            job_key,
            contact_name=contact_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            contact_role=args.contact_role,
        )
        print(f"Resolved message_id={args.message_id!r} -> {job_key!r} (conversation id={conversation_id}).")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
