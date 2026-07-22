"""CLI: triage a plain-IMAP mailbox (non-Gmail) the same way
`triage_recruiter_inbox.py` triages the Gmail recruiting funnel — classify,
extract, resolve JD, LLM-score (+ auto-generate a résumé/cover letter on a
"pursue" verdict), then file the source message into an IMAP folder mirroring
the Gmail JobTracker/* label taxonomy.

Added 2026-07-22 after a real, confirmed gap: `shawn.becker@spexture.com` —
the email address on Shawn's résumé header — is Hostinger-hosted (plain
IMAP), not Gmail/Google Workspace. Cole Keener (DIRECTV recruiter)'s replies
landed there, and at least one message never reached
`shawnbecker.recruiting@gmail.com` at all — the only mailbox the Gmail-API-
based automation (`triage_recruiter_inbox.py`, `scan_communications.py`) can
see. This CLI closes that gap by giving the pipeline a second, independent
mailbox source using the exact same triage/extraction/scoring/storage code
(`pipeline.triage.triage_message`, `pipeline.comms_match.match_message_to_job`,
`pipeline.store`) — only the message source (IMAP vs Gmail API) and the
"labeling" mechanism (IMAP folder move vs Gmail label+archive) differ.

Dedup/skip-already-triaged uses `store.is_communication_seen` — a superset
check across `processed_messages`, `job_conversations`, and
`unmatched_messages` (the same check `scan_communications.py` uses), since
this script's two branches (see below) record their outcome in different
tables. IMAP message ids are namespaced `imap:<Message-Id header>` (or
`imap-uid:<folder>:<uid>` when a message has no Message-Id) by
`imap_reader.parse_rfc822_message`, so they can never collide with Gmail's
hex ids in any of those shared tables.

## Two branches, mirroring the Gmail-side split across two separate CLIs

`hit-reply@linkedin.com` / `inmail-hit-reply@linkedin.com` mail carries a
real InMail/reply's actual text (see `scan_communications.py`'s module
docstring) — job_tracker's own `email.classifier.classify()` is not designed
to see this mail at all in the Gmail pipeline (`scan_communications.py`
intercepts it upstream of `triage_recruiter_inbox.py`); running it through
`classify()`/`triage_message()` anyway mislabels it LINK_ONLY_DIGEST purely
on sender domain, with no extraction attempted and no lead ever created —
confirmed live 2026-07-22 dry-running this exact file against
shawn.becker@spexture.com's real inbox (~25 "Message replied: ..."
notifications, several with genuine recruiter pitches in the body, all fell
into that trap on the first draft of this script). Mail from these two
senders is instead routed through `pipeline.comms_match.match_message_to_job`
with `use_llm_fallback=True` and handled exactly like
`scan_communications.py`'s `_scan_one` (stub-lead creation, jd_text
enrichment, unmatched-message parking) — reusing its helper functions
directly rather than re-deriving them. Everything else still goes through
`triage_recruiter_inbox.py`'s existing-lead-short-circuit -> `triage_message`
path unchanged.

No OAuth here — just a username/password IMAP login
(`imap_reader.ImapAccount.from_env`), read from the shared `.env`
(`<PREFIX>_IMAP_HOST/PORT/USER/PASSWORD`), never printed or logged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_tracker.cli.scan_communications import (
    _create_lead_from_extraction,
    _save_message_txt,
    _update_lead_jd_text,
)
from job_tracker.email.imap_reader import ImapAccount, connect, ensure_folder, fetch_message, list_message_uids, move_message
from job_tracker.pipeline.comms_match import match_message_to_job
from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, DEFAULT_OUTPUT_ROOT
from job_tracker.pipeline.llm_extract import DEFAULT_MODEL as DEFAULT_LLM_EXTRACT_MODEL
from job_tracker.pipeline.llm_interview import extract_interview_details_llm
from job_tracker.pipeline.models import JobContact, JobConversation, UnmatchedMessage
from job_tracker.pipeline.post_application import (
    PostApplicationLabel,
    apply_post_application_signal,
    classify_post_application,
)
from job_tracker.pipeline.signature import parse_signature
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    DEFAULT_REJECTION_COOLDOWN_DAYS,
    GENERIC_RELAY_ADDRESSES,
    add_job_contact,
    add_job_conversation,
    advance_status,
    connect as db_connect,
    find_matching_job,
    is_communication_seen,
    record_message_processed,
    record_unmatched_message,
    update_llm_evaluation,
    upsert_lead,
)
from job_tracker.pipeline.triage import (
    DEFAULT_MAX_LLM_EXTRACTED_ROLES,
    NEEDS_REVIEW,
    PURSUE,
    SKIP,
    effective_verdict,
    triage_message,
)

# The two LinkedIn sender addresses that carry actual message text — see
# `scan_communications.py`'s `DEFAULT_INBOUND_QUERY` docstring for why these
# two specifically, as opposed to any other `@linkedin.com` digest sender.
_LINKEDIN_PERSONAL_REPLY_SENDERS = frozenset({"hit-reply@linkedin.com", "inmail-hit-reply@linkedin.com"})

# IMAP hierarchy separator on Hostinger (Dovecot) is "." with everything
# nested under INBOX (see `INBOX.Archive`, `INBOX.Sent`, etc. already there)
# — mirrors that convention instead of inventing a top-level mailbox.
_LINKED_FOLDER = "INBOX.JobTracker.Linked"
_NEEDS_FOLLOWUP_FOLDER = "INBOX.JobTracker.NeedsFollowup"
_OUTCOME_FOLDERS = {
    PURSUE: "INBOX.JobTracker.PURSUE",
    SKIP: "INBOX.JobTracker.SKIP",
    NEEDS_REVIEW: "INBOX.JobTracker.NEEDS_REVIEW",
}


def _link_existing_conversation(
    conn,
    imap_conn,
    message,
    outcome,
    uid: str,
    *,
    source_folder: str,
    dry_run: bool,
    use_llm_fallback: bool = False,
    llm_extraction_model: str = DEFAULT_LLM_EXTRACT_MODEL,
    llm_extract_client=None,
) -> None:
    """Mirror of `triage_recruiter_inbox._link_existing_conversation` for an
    IMAP-sourced message — see that function's docstring for the underlying
    "existing-lead short-circuit" rationale. Files the message into
    `_LINKED_FOLDER` instead of applying a Gmail label."""
    print(f"\n[LINKED] {message.subject}  <{message.from_address}>  ({message.id})")
    print(f"  matched an existing job via {outcome.tier}: {outcome.reason}")
    if dry_run:
        print("  (dry run — no IMAP move applied, no conversation stored)")
        return

    detected = parse_signature(message.combined_text)
    contact_email = ""
    if detected and detected.email:
        contact_email = detected.email
    elif message.from_address.strip().lower() not in GENERIC_RELAY_ADDRESSES:
        contact_email = message.from_address

    contact_id = None
    if contact_email or (detected and (detected.name or detected.phone)):
        contact_id = add_job_contact(
            conn,
            JobContact(
                job_key=outcome.job_key,
                name=detected.name if detected else "",
                email=contact_email,
                phone=detected.phone if detected else "",
                role="recruiter",
                source_message_id=message.id,
            ),
        )

    # Post-application signal detection (2026-07-22) — see
    # pipeline/post_application.py's module docstring.
    post_app = classify_post_application(message.combined_text)
    conversation_summary = message.subject
    if post_app.label == PostApplicationLabel.INTERVIEW_INVITE and use_llm_fallback:
        details = extract_interview_details_llm(message, model=llm_extraction_model, client=llm_extract_client)
        if details is not None and not details.is_empty:
            conversation_summary = details.as_summary()
    post_app_action = apply_post_application_signal(
        conn, outcome.job_key, post_app, message_id=message.id, email_text=message.combined_text
    )
    if post_app_action:
        print(f"  post-application signal: {post_app_action}")

    add_job_conversation(
        conn,
        JobConversation(
            job_key=outcome.job_key,
            contact_id=contact_id,
            message_id=message.id,
            channel="email",
            direction="inbound",
            summary=conversation_summary,
            thread_id=message.thread_id,
            body_text=message.combined_text,
        ),
    )
    move_message(imap_conn, uid, from_folder=source_folder, to_folder=_LINKED_FOLDER)


def _handle_linkedin_reply(
    conn,
    imap_conn,
    message,
    uid: str,
    *,
    source_folder: str,
    use_llm_fallback: bool,
    llm_model: str,
    output_root: Path,
    dry_run: bool,
    llm_extract_client=None,
) -> str:
    """Mirror of `scan_communications._scan_one`'s post-`match_message_to_job`
    handling for one `hit-reply@`/`inmail-hit-reply@` message — stub-lead
    creation, jd_text enrichment, contact/conversation recording, or
    unmatched parking — then files the message into `_LINKED_FOLDER` (a job
    was resolved, one way or another) or `_NEEDS_FOLLOWUP_FOLDER` (nothing
    usable at all). Returns a short action string for the run log."""
    outcome = match_message_to_job(conn, message, direction="inbound", use_llm_fallback=use_llm_fallback, llm_model=llm_model)
    print(f"\n[LINKEDIN-REPLY] {message.subject}  <{message.from_address}>  ({message.id})")
    print(f"  {outcome.tier}: {outcome.reason}")
    if dry_run:
        print("  (dry run — no IMAP move applied, no DB writes)")
        return "dry-run"

    job_key = outcome.job_key
    action = ""
    if outcome.is_new_lead_candidate:
        job_key = _create_lead_from_extraction(conn, outcome.extracted_role, message, message.id)
        action = "new lead created"
    elif outcome.matched and outcome.tier == "llm_company_title" and outcome.extracted_role is not None:
        if _update_lead_jd_text(conn, job_key, outcome.extracted_role, message):
            action = "linked (+ jd_text updated)"

    if job_key is not None:
        detected = parse_signature(message.combined_text)
        contact_email = ""
        if detected and detected.email:
            contact_email = detected.email
        elif message.from_address.strip().lower() not in GENERIC_RELAY_ADDRESSES:
            contact_email = message.from_address
        contact_id = None
        if contact_email or (detected and (detected.name or detected.phone)):
            contact_id = add_job_contact(
                conn,
                JobContact(
                    job_key=job_key,
                    name=detected.name if detected else "",
                    email=contact_email,
                    phone=detected.phone if detected else "",
                    role="recruiter",
                    source_message_id=message.id,
                ),
            )

        # Post-application signal detection (2026-07-22) — see
        # pipeline/post_application.py's module docstring.
        post_app = classify_post_application(message.combined_text)
        conversation_summary = message.subject
        if post_app.label == PostApplicationLabel.INTERVIEW_INVITE and use_llm_fallback:
            details = extract_interview_details_llm(message, model=llm_model, client=llm_extract_client)
            if details is not None and not details.is_empty:
                conversation_summary = details.as_summary()
        post_app_action = apply_post_application_signal(
            conn, job_key, post_app, message_id=message.id, email_text=message.combined_text
        )

        add_job_conversation(
            conn,
            JobConversation(
                job_key=job_key,
                contact_id=contact_id,
                message_id=message.id,
                channel="email",
                direction="inbound",
                summary=conversation_summary,
                thread_id=message.thread_id,
                body_text=message.combined_text,
            ),
        )
        if not action:
            action = "linked"
        if post_app_action:
            action += f" ({post_app_action})"
        if outcome.tier in ("llm_company_title", "llm_new_lead"):
            job_row = conn.execute(
                "SELECT company, title FROM job_leads WHERE normalized_key = ?", (job_key,)
            ).fetchone()
            txt_path = _save_message_txt(
                conn,
                job_key=job_key,
                company=job_row["company"],
                title=job_row["title"],
                message=message,
                message_id=message.id,
                output_root=output_root,
            )
            action += f" (+ archived {txt_path.name})"
        move_message(imap_conn, uid, from_folder=source_folder, to_folder=_LINKED_FOLDER)
    else:
        record_unmatched_message(
            conn,
            UnmatchedMessage(
                message_id=message.id,
                thread_id=message.thread_id,
                direction="inbound",
                from_address=message.from_address,
                to_address=message.to_address,
                subject=message.subject,
                body_text=message.combined_text,
            ),
        )
        action = "parked (unmatched)"
        move_message(imap_conn, uid, from_folder=source_folder, to_folder=_NEEDS_FOLLOWUP_FOLDER)

    print(f"  -> {action}")
    return action


def _print_result(result, *, dry_run: bool) -> None:
    print(f"\n[{result.outcome}] {result.subject}  <{result.from_address}>  ({result.message_id})")
    print(f"  classifier: {result.classifier_label}  —  {result.reason}")
    for role_outcome in result.roles:
        ev = role_outcome.package.evaluation or role_outcome.package.no_llm_score
        print(f"    {role_outcome.lead.title} @ {role_outcome.lead.company}: {ev.verdict.upper()} ({ev.match_pct:.0f}%)")
        if role_outcome.package.jd_path:
            print(f"      folder:       {role_outcome.package.jd_path.parent}")
        review = role_outcome.package.full_llm_review_path or role_outcome.package.no_llm_review_path
        if review:
            print(f"      review:       {review}")
        if role_outcome.package.resume_path:
            print(f"      resume:       {role_outcome.package.resume_path}")
            print(f"      cover letter: {role_outcome.package.cover_letter_path}")
        if role_outcome.package.warnings:
            print(f"      \u26a0 warnings: {role_outcome.package.warnings}")
    if dry_run:
        print("  (dry run — no IMAP move applied, no lead stored)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Triage a plain-IMAP mailbox the same way triage_recruiter_inbox.py triages Gmail: "
        "LLM-score each JD, auto-generate a résumé/cover letter on 'pursue', then file the message into "
        "an INBOX.JobTracker.* IMAP folder."
    )
    ap.add_argument(
        "--imap-prefix",
        default="SPEXTURE",
        help="Env var prefix for <PREFIX>_IMAP_HOST/PORT/USER/PASSWORD (default: SPEXTURE)",
    )
    ap.add_argument("--folder", default="INBOX", help="IMAP folder to scan (default: INBOX)")
    ap.add_argument("--limit", type=int, help="Max messages to process this run")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model id/alias (default: {DEFAULT_MODEL})")
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    ap.add_argument("--no-generate", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument(
        "--llm-fallback",
        action="store_true",
        help=(
            "Enable cached LLM extraction fallback for BOTH branches: link-only-digest/unparsable "
            "SINGLE_JD/MULTI_JD_IN_BODY mail (triage_message's own fallback), and hit-reply@/"
            "inmail-hit-reply@linkedin.com messages with no thread/contact match (comms_match's Tier 3). "
            "Costs real money per message (cached by message_id, so a re-run never re-bills)."
        ),
    )
    ap.add_argument("--llm-extraction-model", default=DEFAULT_LLM_EXTRACT_MODEL)
    ap.add_argument("--max-llm-extracted-roles", type=int, default=DEFAULT_MAX_LLM_EXTRACTED_ROLES)
    ap.add_argument("--rejection-cooldown-days", type=int, default=DEFAULT_REJECTION_COOLDOWN_DAYS)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and print what would happen, but never move messages or write to the DB",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-triage messages even if store.is_communication_seen already says this message id was handled",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    account = ImapAccount.from_env(args.imap_prefix)
    imap_conn = connect(account)
    uids = list_message_uids(imap_conn, folder=args.folder, criteria="ALL", limit=args.limit)

    if not uids:
        print("No messages in this folder.", file=sys.stderr)
        imap_conn.logout()
        return 0

    conn = db_connect(args.db)
    postings_cache: dict[str, list] = {}
    results = []
    skipped_already_processed = 0
    linked_to_existing_count = 0
    errored_uids: list[str] = []

    if not args.dry_run:
        for folder in list(_OUTCOME_FOLDERS.values()) + [_LINKED_FOLDER, _NEEDS_FOLLOWUP_FOLDER]:
            ensure_folder(imap_conn, folder)

    try:
        for uid in uids:
            try:
                message = fetch_message(imap_conn, uid, folder=args.folder)
            except Exception as exc:
                print(f"\n[ERROR] uid={uid}: {exc!r} — skipping, will retry next run", file=sys.stderr)
                errored_uids.append(uid)
                continue

            if not args.dry_run and not args.force and is_communication_seen(conn, message.id):
                skipped_already_processed += 1
                continue

            if message.from_address.strip().lower() in _LINKEDIN_PERSONAL_REPLY_SENDERS:
                try:
                    _handle_linkedin_reply(
                        conn,
                        imap_conn,
                        message,
                        uid,
                        source_folder=args.folder,
                        use_llm_fallback=args.llm_fallback,
                        llm_model=args.llm_extraction_model,
                        output_root=args.output_root,
                        dry_run=args.dry_run,
                    )
                except Exception as exc:
                    print(f"\n[ERROR] {message.id}: {exc!r} — skipping, will retry next run", file=sys.stderr)
                    errored_uids.append(uid)
                continue

            try:
                link_outcome = match_message_to_job(conn, message, direction="inbound")
                if link_outcome.matched:
                    _link_existing_conversation(
                        conn,
                        imap_conn,
                        message,
                        link_outcome,
                        uid,
                        source_folder=args.folder,
                        dry_run=args.dry_run,
                        use_llm_fallback=args.llm_fallback,
                        llm_extraction_model=args.llm_extraction_model,
                    )
                    linked_to_existing_count += 1
                    if not args.dry_run:
                        record_message_processed(
                            conn,
                            message.id,
                            outcome="LINKED",
                            subject=message.subject,
                            from_address=message.from_address,
                            lead_keys=[link_outcome.job_key],
                            label_applied=_LINKED_FOLDER,
                            archived=True,
                        )
                    continue

                result = triage_message(
                    message,
                    model=args.model,
                    generate=not args.no_generate,
                    output_root=args.output_root,
                    resolve_full_jd=not args.offline,
                    postings_cache=postings_cache,
                    use_llm_extraction_fallback=args.llm_fallback,
                    llm_extraction_model=args.llm_extraction_model,
                    max_llm_extracted_roles=args.max_llm_extracted_roles,
                    conn=conn,
                    rejection_cooldown_days=args.rejection_cooldown_days,
                )
            except Exception as exc:
                print(f"\n[ERROR] {message.id}: {exc!r} — skipping, will retry next run", file=sys.stderr)
                errored_uids.append(uid)
                continue

            results.append(result)
            _print_result(result, dry_run=args.dry_run)

            if args.dry_run:
                continue

            lead_keys = []
            for role_outcome in result.roles:
                upsert_lead(conn, role_outcome.lead)
                key = role_outcome.lead.normalized_key
                lead_keys.append(key)
                if role_outcome.package.evaluation is not None:
                    update_llm_evaluation(conn, key, role_outcome.package.evaluation)

                job_key = key
                match = find_matching_job(conn, role_outcome.lead.company, role_outcome.lead.title)
                if match and match.normalized_key != key:
                    job_key = match.normalized_key
                    print(
                        f"    \u26a0 looks like the same role as an existing job "
                        f"({match.company} / {match.title}, already tracked) — "
                        f"linking this contact there instead of a new job"
                    )

                contact_id = add_job_contact(
                    conn,
                    JobContact(job_key=job_key, email=result.from_address, role="recruiter", source_message_id=message.id),
                )
                add_job_conversation(
                    conn,
                    JobConversation(
                        job_key=job_key,
                        contact_id=contact_id,
                        message_id=message.id,
                        direction="inbound",
                        summary=result.subject,
                    ),
                )

                role_verdict = effective_verdict(role_outcome.package)
                if role_outcome.package.resume_path is not None:
                    advance_status(conn, key, "package_generated")
                elif role_verdict == "pursue":
                    advance_status(conn, key, "pursued")
                elif role_verdict == "pass":
                    advance_status(conn, key, "skipped")

            target_folder = _OUTCOME_FOLDERS[result.outcome]
            move_message(imap_conn, uid, from_folder=args.folder, to_folder=target_folder)
            record_message_processed(
                conn,
                message.id,
                outcome=result.outcome,
                subject=result.subject,
                from_address=result.from_address,
                lead_keys=lead_keys,
                label_applied=target_folder,
                archived=True,
            )
    finally:
        conn.close()
        try:
            imap_conn.logout()
        except Exception:
            pass

    if skipped_already_processed:
        print(f"\n(skipped {skipped_already_processed} message(s) already triaged in a prior run)", file=sys.stderr)
    if linked_to_existing_count:
        print(
            f"\n(linked {linked_to_existing_count} message(s) to an existing tracked lead via thread/contact "
            "match — recorded as a conversation, not re-triaged)",
            file=sys.stderr,
        )
    if errored_uids:
        print(
            f"\n({len(errored_uids)} message(s) errored and were left untouched — re-run to retry): {', '.join(errored_uids)}",
            file=sys.stderr,
        )

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "message_id": r.message_id,
                        "subject": r.subject,
                        "from": r.from_address,
                        "outcome": r.outcome,
                        "reason": r.reason,
                        "classifier_label": r.classifier_label,
                        "roles": [
                            {
                                "company": ro.lead.company,
                                "title": ro.lead.title,
                                "verdict": effective_verdict(ro.package),
                                "match_pct": ro.package.evaluation.match_pct
                                if ro.package.evaluation is not None
                                else ro.package.no_llm_score.match_pct,
                                "jd_path": str(ro.package.jd_path) if ro.package.jd_path else None,
                                "resume_path": str(ro.package.resume_path) if ro.package.resume_path else None,
                                "cover_letter_path": str(ro.package.cover_letter_path)
                                if ro.package.cover_letter_path
                                else None,
                                "warnings": ro.package.warnings,
                            }
                            for ro in r.roles
                        ],
                    }
                    for r in results
                ],
                indent=2,
            )
        )

    counts = {PURSUE: 0, SKIP: 0, NEEDS_REVIEW: 0}
    for r in results:
        counts[r.outcome] += 1
    print(
        f"\nProcessed {len(results)} message(s): {counts[PURSUE]} PURSUE, {counts[SKIP]} SKIP, "
        f"{counts[NEEDS_REVIEW]} NEEDS_REVIEW",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
