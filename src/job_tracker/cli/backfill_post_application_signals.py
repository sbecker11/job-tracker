"""CLI: rescan ALL stored `job_conversations` history for post-application
signals (rejection / application-received / interview-invite) and backfill
`job_leads.status` for leads whose live triage run happened BEFORE
`pipeline/post_application.py` existed (2026-07-22) — i.e. every message
already archived as a conversation before this feature shipped never got a
chance to advance its lead's status automatically.

One-time catch-up companion to the live wiring in `cli/scan_communications.py`,
`cli/triage_recruiter_inbox.py` (`_link_existing_conversation`), and
`cli/triage_imap_inbox.py` — those three now apply the same detection to
every NEW matched message going forward; this script is what makes the
existing backlog consistent with that in one pass. Safe to re-run any number
of times: `pipeline.post_application.apply_post_application_signal`'s
forward-only stage guard means a message that already caused its effect (or
that arrives "late", after a further-along message already has) is always a
no-op the second time.

Also the fix vehicle for the real bug this feature grew out of: a plain
application-received confirmation (e.g. Solace's ATS auto-reply) that used to
match `email/classifier.py`'s now-removed "thank you for applying" rejection
pattern and got mislabeled `JobTracker/SKIP` — this rescans that same message
text with the corrected classifier and applies the RIGHT signal
(APPLICATION_RECEIVED, not REJECTION) retroactively.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.llm_interview import extract_interview_details_llm
from job_tracker.pipeline.post_application import (
    PostApplicationLabel,
    apply_post_application_signal,
    classify_post_application,
)
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    connect,
    list_all_inbound_conversations_with_body,
    update_job_conversation_summary,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rescan all stored inbound job_conversations for post-application signals "
        "(rejection/application-received/interview-invite) and backfill job_leads.status."
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--limit", type=int, help="Max conversation rows to scan (for testing)")
    ap.add_argument(
        "--llm-fallback",
        action="store_true",
        help="For the first interview-invite message that actually advances a lead to "
        "'interviewing', also make one LLM call to extract structured interview details "
        "(date/time/format/interviewer) and rewrite that conversation's summary with them. "
        "Costs real money per interview invite found (not per message scanned).",
    )
    ap.add_argument("--llm-extraction-model", default=None)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change; never write to the DB",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        rows = list_all_inbound_conversations_with_body(conn)
        if args.limit:
            rows = rows[: args.limit]

        results: list[dict] = []
        counts = {label.value: 0 for label in PostApplicationLabel}
        actions_taken = 0

        for row in rows:
            classification = classify_post_application(row["body_text"])
            counts[classification.label.value] += 1

            action = ""
            if not args.dry_run:
                action = apply_post_application_signal(
                    conn,
                    row["job_key"],
                    classification,
                    message_id=row["message_id"] or "",
                    email_text=row["body_text"],
                    when=row["occurred_at"],
                )
                if action and classification.label == PostApplicationLabel.INTERVIEW_INVITE and args.llm_fallback:
                    message = EmailMessage(
                        id=row["message_id"] or f"conversation-{row['id']}",
                        from_address="",
                        subject=row["summary"] or "",
                        body_plain=row["body_text"] or "",
                    )
                    model = args.llm_extraction_model
                    details = (
                        extract_interview_details_llm(message, model=model)
                        if model
                        else extract_interview_details_llm(message)
                    )
                    if details is not None and not details.is_empty:
                        update_job_conversation_summary(conn, row["id"], details.as_summary())
                        action += " (+ interview details extracted)"

            if action:
                actions_taken += 1
            if action or classification.label != PostApplicationLabel.NEXT_STEPS:
                results.append(
                    {
                        "job_key": row["job_key"],
                        "message_id": row["message_id"],
                        "occurred_at": row["occurred_at"],
                        "label": classification.label.value,
                        "reasons": classification.reasons,
                        "action": action or ("(dry run — would classify)" if args.dry_run else "(no-op)"),
                    }
                )

        if args.json:
            import json

            print(json.dumps(results, indent=2))
        else:
            for r in results:
                print(f"[{r['label']}] {r['job_key']}  ({r['occurred_at']})")
                print(f"    {r['action']}")
                print(f"    reasons: {'; '.join(r['reasons'])}")

        print(
            f"\nScanned {len(rows)} inbound conversation(s): "
            + ", ".join(f"{v} {k}" for k, v in counts.items())
            + f". {actions_taken} lead(s) actually updated.",
            file=sys.stderr,
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
