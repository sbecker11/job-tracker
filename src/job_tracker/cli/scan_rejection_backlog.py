"""Scan archived recruiter mail for rejection signals missed by live triage.

Phase 2 intake: find rejection emails (including Archives / already-labeled SKIP)
where a tracked lead exists but status was never advanced to `rejected`.
Dry-run by default; `--apply --yes` writes after explicit confirmation.

  python scripts/scan_rejection_backlog.py
  python scripts/scan_rejection_backlog.py --apply --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.email.classifier import classify as classify_message
from job_tracker.email.gmail_reader import (
    KNOWN_ACCOUNTS,
    default_credentials_path,
    default_token_path,
    fetch_message,
    get_gmail_service,
    list_message_ids,
)
from job_tracker.email.labels import Label
from job_tracker.pipeline.comms_match import match_message_to_job
from job_tracker.pipeline.post_application import apply_post_application_signal, classify_post_application
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    add_job_conversation,
    connect,
    get_lead_status,
    is_message_processed,
)
from job_tracker.scoring.models import JobConversation

# Include archived SKIP mail and unlabeled recruiter_job — rejections often
# archive as SKIP before a lead match ran (2026-07-31 gap).
DEFAULT_QUERY = (
    "(label:Category/recruiter_job OR label:JobTracker/SKIP) "
    "-label:JobTracker/PURSUE -label:JobTracker/NEEDS_REVIEW "
    "-label:JobTracker/Linked -label:JobTracker/NeedsFollowup"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--account", choices=KNOWN_ACCOUNTS, default=None)
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--newer-than", type=int, default=90, metavar="DAYS")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--llm-fallback", action="store_true", default=True)
    ap.add_argument("--no-llm-fallback", action="store_false", dest="llm_fallback")
    ap.add_argument("--credentials", type=Path, default=None)
    ap.add_argument("--token", type=Path, default=None)
    ap.add_argument("--apply", action="store_true", help="Write matched rejections to the DB")
    ap.add_argument("--yes", action="store_true", help="Required with --apply (confirm writes)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.apply and not args.yes:
        print("Refusing --apply without --yes (confirm-before-write).", file=sys.stderr)
        return 2

    query = args.query
    if args.newer_than:
        query = f"{query} newer_than:{args.newer_than}d"

    credentials_path = args.credentials or default_credentials_path(args.account)
    token_path = args.token or default_token_path(args.account)
    service = get_gmail_service(credentials_path, token_path, account=args.account)

    message_ids = list_message_ids(service, query=query, limit=args.limit)
    if not message_ids:
        print("No messages matched.", file=sys.stderr)
        return 0

    conn = connect(args.db)
    proposals: list[dict] = []
    try:
        for message_id in message_ids:
            message = fetch_message(service, message_id)
            result = classify_message(message)
            if result.label != Label.REJECTION:
                continue

            job_key = None
            tier = ""
            status_before = None

            # Prefer existing processed link if present.
            row = conn.execute(
                "SELECT lead_keys FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row and row["lead_keys"]:
                keys = [k.strip() for k in (row["lead_keys"] or "").split(",") if k.strip()]
                if keys:
                    job_key = keys[0]
                    tier = "processed_lead_keys"
            else:
                outcome = match_message_to_job(
                    conn,
                    message,
                    direction="inbound",
                    use_llm_fallback=args.llm_fallback,
                )
                if outcome.matched:
                    job_key = outcome.job_key
                    tier = outcome.tier

            if not job_key:
                continue

            status_before = get_lead_status(conn, job_key)
            if status_before == "rejected":
                continue

            proposals.append(
                {
                    "messageId": message_id,
                    "subject": message.subject,
                    "from": message.from_address,
                    "jobKey": job_key,
                    "matchTier": tier,
                    "statusBefore": status_before,
                    "classifierReasons": result.reasons,
                    "alreadyProcessed": is_message_processed(conn, message_id),
                }
            )

            if not args.apply:
                continue

            post_app = classify_post_application(message.combined_text)
            action = apply_post_application_signal(
                conn,
                job_key,
                post_app,
                message_id=message.id,
                email_text=message.combined_text,
            )
            add_job_conversation(
                conn,
                JobConversation(
                    job_key=job_key,
                    message_id=message.id,
                    channel="email",
                    direction="inbound",
                    summary=message.subject or "Rejection",
                    thread_id=message.thread_id,
                    body_text=message.combined_text,
                ),
            )
            conn.commit()
            proposals[-1]["action"] = action or "status unchanged"

    finally:
        conn.close()

    if args.json:
        import json

        print(json.dumps({"proposed": len(proposals), "items": proposals}, indent=2))
        return 0

    print(f"Found {len(proposals)} rejection(s) worth advancing to rejected:")
    for p in proposals:
        proc = "processed" if p["alreadyProcessed"] else "unprocessed"
        print(
            f"  • {p['jobKey']}  [{p['statusBefore']}]  {proc}  "
            f"via {p['matchTier']}  —  {p['subject'][:60]}"
        )
        if args.apply:
            print(f"      applied: {p.get('action', '?')}")

    if proposals and not args.apply:
        print("\nPreview only. Apply with: python scripts/scan_rejection_backlog.py --apply --yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
