"""CLI: archive recruiter communications the main triage flow never sees.

Background (2026-07-17): `triage_recruiter_inbox.py` only ever reads mail
comms-migration has labeled `Category/recruiter_job`. LinkedIn "Message
replied: ..." notifications — the actual back-and-forth on an existing
recruiter conversation — are deliberately routed to `Category/social`
instead (a standing rule in comms-migration/rules/rules.yaml, kept that way
on purpose so ordinary InMail traffic doesn't clutter the job funnel). The
side effect: those replies were invisible to job-tracker entirely — no
JobContact, no JobConversation, nothing — even when they contained real
signal (e.g. a recruiter confirming W2 and naming the end client). This
command is the fix: it scans specifically for that traffic (plus your own
outgoing replies, once they're linkable) and archives it against the right
job using `pipeline/comms_match.py`'s tiered matching, without touching
Gmail labels or the recruiter_job funnel at all.

Scope, deliberately conservative:
  - Inbound: only `hit-reply@linkedin.com` / `inmail-hit-reply@linkedin.com`
    — the two LinkedIn addresses that carry the actual message text (unlike
    `messaging-digest-noreply@`'s "X just messaged you" stubs, which have
    nothing to match or archive beyond a first name).
  - Outbound (`--include-sent`, opt-in): Sent-folder messages that match
    Tier 1 (thread id or recipient already known) ONLY. An unmatched
    outbound message is silently skipped rather than parked in the
    unmatched queue — Sent folders carry plenty of non-recruiting mail, and
    parking every unrecognized outgoing email would flood the review queue
    with noise. Inbound unmatched mail IS parked, since the sender
    addresses above are unambiguously recruiting-related by construction.

Writes to Gmail too, as of 2026-07-19 — see "Gmail labeling" below. Before
that this command was read-only against Gmail entirely; it still writes
nothing to a message's *content*, only two new labels distinct from
`gmail_writer`'s PURSUE/SKIP/NEEDS_REVIEW trio, and only ever to the exact
messages this scan itself touches. Besides that, this only ever writes to
`var/leads.db` (and, for the two cases described below, the matching job's
folder under `--output-root`).

Gmail labeling (2026-07-19): the whole point of `triage_recruiter_inbox.py`
relabeling `Category/recruiter_job` mail is so the mailbox owner can
eventually trust Gmail's own label state enough to stop reviewing recruiting
mail directly — but that only works end-to-end if EVERY category of
recruiting traffic gets a trustworthy label, and this command's traffic
(`Category/social` LinkedIn replies) previously got none at all: a fully
archived, perfectly-linked reply looked identical in the inbox to one still
sitting unprocessed. Now, every inbound message this scan can actually
resolve gets `JobTracker/Linked` and is archived (INBOX removed) — fully
captured, nothing left to do via Gmail. Every inbound message that gets
parked in the unmatched queue instead gets `JobTracker/NeedsFollowup` and is
deliberately left in the inbox (not archived) — it's exactly the traffic
still worth a human's attention, either directly or via
`resolve_communication.py`. Outbound (Sent-folder) messages are never
labeled — Sent isn't something the owner reviews for "still needs my
attention." `--dry-run` skips Gmail writes exactly like it already skips DB
writes.

"No happy path" for extracted leads (2026-07-17 refinement): when Tier 3
extraction (`pipeline/comms_match.py`) finds BOTH a company and a title —
whether it matches an existing job or is brand new — this command:
  1. Saves the raw message as a `.txt` document into that job's folder
     (`communications/Email_<message_id>.txt`) and records it as a
     `JobDocument` (`doc_type="email_txt"`), same convention as
     `export_communications.py`'s PDF export.
  2. Updates the job lead's `jd_text` with whatever the message added — for
     a brand-new lead, that's `role.snippet` (falling back to the whole
     message body); for an existing `status="new"` lead, the excerpt is
     appended if it isn't already in there. A lead a human has already
     triaged (`status` past `"new"`) is left alone, same guard
     `store.upsert_lead` already applies everywhere else.
It deliberately stops there — no ATS lookup, no `no-LLM-review`/
`full-LLM-review` docx, no résumé/cover-letter generation. Those stay a
separate, explicit step (`apply_package.py`, or the next
`render_pending_actions.py` rescore for the free score) so a vague LinkedIn
pitch never silently turns into a fully-reviewed, packaged lead with nobody
having read it.

Contact enrichment (2026-07-17): every matched/linked inbound message also
runs `pipeline/signature.py` against the body to pull a real recruiter
name/email/phone out of the sign-off block, if one's there — the `From:`
header alone is useless for this (always a generic LinkedIn relay
address), and this is the difference between a contact record you can
actually use to follow up and one that's empty.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_tracker.email import gmail_writer
from job_tracker.email.gmail_reader import (
    default_credentials_path,
    default_token_path,
    fetch_message,
    get_gmail_service,
    get_gmail_service_writable,
    list_message_ids,
)
from job_tracker.email.models import EmailMessage, ExtractedRole
from job_tracker.pipeline.comms_match import match_message_to_job
from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _job_folder, _safe_filename
from job_tracker.pipeline.llm_extract import DEFAULT_MODEL as DEFAULT_LLM_EXTRACT_MODEL
from job_tracker.pipeline.llm_interview import extract_interview_details_llm
from job_tracker.pipeline.models import JobContact, JobConversation, JobDocument, JobLead, UnmatchedMessage
from job_tracker.pipeline.post_application import (
    PostApplicationLabel,
    apply_post_application_signal,
    classify_post_application,
)
from job_tracker.pipeline.signature import parse_signature
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    GENERIC_RELAY_ADDRESSES,
    add_job_contact,
    add_job_conversation,
    add_job_document,
    connect,
    get_sibling_titles,
    is_communication_seen,
    record_unmatched_message,
    upsert_lead,
)
from job_tracker.scoring.scorer import score_jd

# The two LinkedIn sender addresses that carry actual message text (a
# fresh InMail, or a "Message replied: ..." notification quoting the
# reply) rather than just a name + a link to go read it on-site.
DEFAULT_INBOUND_QUERY = "(from:hit-reply@linkedin.com OR from:inmail-hit-reply@linkedin.com)"
DEFAULT_SENT_QUERY = "in:sent"


def _save_message_txt(
    conn, *, job_key: str, company: str, title: str, message: EmailMessage, message_id: str, output_root: Path
) -> Path:
    """Archive the raw message as a `.txt` `JobDocument` under this job's
    folder — the filesystem-side half of "save the email as a txt file and
    add it as a new document for that company+title" (2026-07-17). Mirrors
    `export_communications.py`'s PDF export, just per-message and plain text
    instead of one consolidated on-demand PDF."""
    multi_lead = len(get_sibling_titles(conn, company, exclude_title=title)) > 0
    job_dir = _job_folder(output_root, company=company, title=title, multi_lead=multi_lead)
    out_path = job_dir / "communications" / f"Email_{_safe_filename(message_id)}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "\n".join(
            [
                f"Message-Id: {message_id}",
                f"Thread-Id: {message.thread_id}",
                f"Subject: {message.subject}",
                f"From: {message.from_address}",
                f"To: {message.to_address}",
                "",
                message.combined_text,
            ]
        ),
        encoding="utf-8",
    )
    add_job_document(conn, JobDocument(job_key=job_key, doc_type="email_txt", path_or_url=str(out_path)))
    return out_path


def _update_lead_jd_text(conn, job_key: str, role: ExtractedRole, message: EmailMessage) -> bool:
    """"Attempt to extract job-lead data from the follow-up message and
    update the job-lead as needed" (2026-07-17), for a message that matched
    an EXISTING lead via LLM extraction. Only touches `status="new"` leads —
    identical guard to `store.upsert_lead`'s own "once a human has triaged
    it, stop silently rewriting it" rule. Returns True if the lead was
    actually updated."""
    row = conn.execute(
        "SELECT company, title, status, jd_text FROM job_leads WHERE normalized_key = ?", (job_key,)
    ).fetchone()
    if row is None or row["status"] != "new":
        return False
    excerpt = (role.snippet or message.combined_text or "").strip()
    existing = row["jd_text"] or ""
    if not excerpt or excerpt in existing:
        return False
    merged = f"{existing}\n\n--- Follow-up message excerpt ({message.subject}) ---\n{excerpt}" if existing else excerpt
    score = score_jd(merged)
    upsert_lead(
        conn,
        JobLead(
            company=row["company"],
            title=row["title"],
            source_message_id="",
            source_label="",
            jd_resolved=True,
            jd_source="email_body",
            jd_text=merged,
            match_pct=score.match_pct,
            matched_skills=list(score.matched_skills),
            verdict=score.verdict,
            rationale=list(score.rationale),
            status="new",
        ),
    )
    return True


def _create_lead_from_extraction(conn, role: ExtractedRole, message: EmailMessage, message_id: str) -> str:
    """"If the company and title can be extracted... add it as a new
    document for that company+title - in the DB" (2026-07-17): a brand-new
    stub lead for an `MatchOutcome.is_new_lead_candidate` message —
    deliberately NOT the full "magic path" (no ATS lookup, no LLM review, no
    package) per the same instruction's "no need to go off the happy path."
    Scored with the free rule-based pass only so it shows up honestly in the
    funnel dashboard rather than sitting at the 0.0/"review" defaults until
    the next `render_pending_actions.py` rescore. Returns the new job_key."""
    jd_text = (role.snippet or message.combined_text or "").strip()
    score = score_jd(jd_text) if jd_text else None
    lead = JobLead(
        company=role.company,
        title=role.title,
        source_message_id=message_id,
        source_label="linkedin_message",
        apply_url=role.apply_url,
        extraction_confidence=role.confidence,
        jd_resolved=bool(jd_text),
        jd_source="email_body" if jd_text else "",
        jd_text=jd_text,
        match_pct=score.match_pct if score else 0.0,
        matched_skills=list(score.matched_skills) if score else [],
        verdict=score.verdict if score else "review",
        rationale=list(score.rationale) if score else [f"Created from a LinkedIn message ({message_id}); no JD text yet"],
    )
    upsert_lead(conn, lead)
    return lead.normalized_key


def _scan_one(
    conn,
    service,
    message_id: str,
    *,
    direction: str,
    use_llm_fallback: bool,
    llm_model: str,
    dry_run: bool,
    output_root: Path,
    label_ids: dict[str, str] | None = None,
    llm_extract_client=None,
) -> dict:
    message = fetch_message(service, message_id)
    outcome = match_message_to_job(
        conn, message, direction=direction, use_llm_fallback=use_llm_fallback, llm_model=llm_model
    )
    result = {
        "message_id": message_id,
        "direction": direction,
        "subject": message.subject,
        "from": message.from_address,
        "to": message.to_address,
        "tier": outcome.tier,
        "reason": outcome.reason,
        "job_key": outcome.job_key,
        "action": "skipped (dry run)" if dry_run else "",
    }
    if dry_run:
        return result

    job_key = outcome.job_key
    if outcome.is_new_lead_candidate:
        job_key = _create_lead_from_extraction(conn, outcome.extracted_role, message, message_id)
        result["job_key"] = job_key
        result["action"] = "new lead created"
    elif outcome.matched and outcome.tier == "llm_company_title" and outcome.extracted_role is not None:
        if _update_lead_jd_text(conn, job_key, outcome.extracted_role, message):
            result["action"] = "linked (+ jd_text updated)"

    if job_key is not None:
        contact_id = None
        other_address = message.from_address if direction == "inbound" else message.to_address
        # Real recruiter name/email/phone often sit in the message body
        # itself (LinkedIn's sender-block template, or the recruiter's own
        # typed sign-off) even though the `From:` header is always a
        # generic relay address for InMail — see pipeline/signature.py.
        # Inbound only: an outbound (Sent-folder) message is Shawn's own
        # writing, and parsing it here could mistake his own sign-off for
        # the recruiter's.
        detected = parse_signature(message.combined_text) if direction == "inbound" else None
        contact_email = ""
        if detected and detected.email:
            contact_email = detected.email
        elif other_address and other_address.strip().lower() not in GENERIC_RELAY_ADDRESSES:
            # Never record a LinkedIn relay address as *the* contact — see
            # comms_match._GENERIC_RELAY_ADDRESSES; it identifies the
            # platform, not the recruiter, and would otherwise poison Tier 2
            # for every future message from ANY recruiter landing on this job.
            contact_email = other_address
        if contact_email or (detected and (detected.name or detected.phone)):
            contact_id = add_job_contact(
                conn,
                JobContact(
                    job_key=job_key,
                    name=detected.name if detected else "",
                    email=contact_email,
                    phone=detected.phone if detected else "",
                    role="recruiter",
                    source_message_id=message_id,
                ),
            )

        # Post-application signal detection (2026-07-22): a message matched
        # to an existing lead may itself be a rejection, an application-
        # received confirmation, or an interview invite — see
        # pipeline/post_application.py. Inbound only; direction == "outbound"
        # (Shawn's own Sent-folder replies) never carries one of these.
        conversation_summary = message.subject
        post_app_action = ""
        if direction == "inbound":
            post_app = classify_post_application(message.combined_text)
            if post_app.label == PostApplicationLabel.INTERVIEW_INVITE and use_llm_fallback:
                details = extract_interview_details_llm(message, model=llm_model, client=llm_extract_client)
                if details is not None and not details.is_empty:
                    conversation_summary = details.as_summary()
            post_app_action = apply_post_application_signal(
                conn, job_key, post_app, message_id=message_id, email_text=message.combined_text
            )

        add_job_conversation(
            conn,
            JobConversation(
                job_key=job_key,
                contact_id=contact_id,
                message_id=message_id,
                channel="email",
                direction=direction,
                summary=conversation_summary,
                thread_id=message.thread_id,
                body_text=message.combined_text,
            ),
        )
        if not result["action"]:
            result["action"] = "linked"
        if post_app_action:
            result["action"] += f" ({post_app_action})"

        # "save the email as a txt file... for that company+title" only
        # applies once BOTH company and title were extracted — a bare
        # thread-id/contact-email match (no fresh extraction) has nothing
        # new to archive beyond what's already in job_conversations.body_text.
        if outcome.tier in ("llm_company_title", "llm_new_lead"):
            # Use the lead's own canonical company/title, not the LLM's raw
            # extracted text — for a fuzzy Tier-3a match those can differ
            # slightly (e.g. "Clevanoo" vs. "Clevanoo LLC"), and `_job_folder`
            # derives its path from the string given, so anything but the
            # canonical value would archive this .txt into a NEW, wrong
            # folder disconnected from that job's existing JD/résumé/review.
            job_row = conn.execute(
                "SELECT company, title FROM job_leads WHERE normalized_key = ?", (job_key,)
            ).fetchone()
            txt_path = _save_message_txt(
                conn, job_key=job_key, company=job_row["company"], title=job_row["title"],
                message=message, message_id=message_id, output_root=output_root,
            )
            result["action"] += f" (+ archived {txt_path.name})"
    elif direction == "inbound":
        record_unmatched_message(
            conn,
            UnmatchedMessage(
                message_id=message_id,
                thread_id=message.thread_id,
                direction=direction,
                from_address=message.from_address,
                to_address=message.to_address,
                subject=message.subject,
                body_text=message.combined_text,
            ),
        )
        result["action"] = "parked (unmatched)"
    else:
        result["action"] = "skipped (unmatched outbound)"

    # Gmail labeling (2026-07-19) — see module docstring. Inbound only:
    # Sent-folder messages are never labeled, and `label_ids` is None
    # entirely in `--dry-run` (see `main()`), so this whole block is a
    # no-op there, matching the DB writes it's already skipping.
    if direction == "inbound" and label_ids:
        if job_key is not None:
            gmail_writer.label_and_archive(service, message_id, label_ids["linked"], archive=True)
        else:
            gmail_writer.label_and_archive(service, message_id, label_ids["needs_followup"], archive=False)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Archive recruiter communications (LinkedIn message replies, and optionally your own "
        "Sent-folder replies) against the right tracked job — see module docstring for why this exists "
        "as a separate command from triage_recruiter_inbox.py."
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--inbound-query", default=DEFAULT_INBOUND_QUERY)
    ap.add_argument("--newer-than", type=int, default=14, metavar="DAYS", help="Default: 14")
    ap.add_argument("--limit", type=int, help="Max messages to scan per direction")
    ap.add_argument(
        "--include-sent",
        action="store_true",
        help="Also scan the Sent folder for outbound replies (Tier-1 thread/contact match only — see module docstring)",
    )
    ap.add_argument("--sent-query", default=DEFAULT_SENT_QUERY)
    ap.add_argument(
        "--llm-fallback",
        action="store_true",
        help="For inbound mail with no thread/contact match, fall back to one cached LLM call per message "
        "to extract a company/title and fuzzy-match it against existing jobs before giving up to the "
        "unmatched queue. Costs real money per message (haiku-tier, cached by message_id).",
    )
    ap.add_argument("--llm-extraction-model", default=DEFAULT_LLM_EXTRACT_MODEL)
    ap.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Where a new lead's/matched lead's archived .txt message and folder live (default: "
        f"{DEFAULT_OUTPUT_ROOT}) — only touched when a company+title pair is actually extracted",
    )
    ap.add_argument("--account", default=None)
    ap.add_argument("--credentials", type=Path, default=None)
    ap.add_argument("--token", type=Path, default=None)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; never write to the DB or to Gmail (no labels/archiving)",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    credentials_path = args.credentials or default_credentials_path(args.account)
    label_ids: dict[str, str] | None = None
    if args.dry_run:
        # Scoring-only preview never mutates the mailbox — no reason to force
        # the separate, one-time-consent write token just to look.
        token_path = args.token or default_token_path(args.account)
        service = get_gmail_service(credentials_path, token_path, account=args.account)
    else:
        token_path = args.token or default_token_path(args.account, writable=True)
        service = get_gmail_service_writable(credentials_path, token_path, account=args.account)
        label_ids = {
            "linked": gmail_writer.get_or_create_label(service, gmail_writer.LINKED_LABEL),
            "needs_followup": gmail_writer.get_or_create_label(service, gmail_writer.NEEDS_FOLLOWUP_LABEL),
        }

    conn = connect(args.db)
    results: list[dict] = []
    try:
        inbound_ids = list_message_ids(
            service, query=args.inbound_query, limit=args.limit, newer_than_days=args.newer_than
        )
        for message_id in inbound_ids:
            if is_communication_seen(conn, message_id):
                continue
            results.append(
                _scan_one(
                    conn,
                    service,
                    message_id,
                    direction="inbound",
                    use_llm_fallback=args.llm_fallback,
                    llm_model=args.llm_extraction_model,
                    dry_run=args.dry_run,
                    output_root=args.output_root,
                    label_ids=label_ids,
                )
            )

        if args.include_sent:
            sent_ids = list_message_ids(
                service, query=args.sent_query, limit=args.limit, newer_than_days=args.newer_than
            )
            for message_id in sent_ids:
                if is_communication_seen(conn, message_id):
                    continue
                results.append(
                    _scan_one(
                        conn,
                        service,
                        message_id,
                        direction="outbound",
                        # Sent mail never gets the LLM fallback in v1 (see module
                        # docstring) — Tier 1 only, to keep Sent-folder scanning cheap
                        # and to avoid billing an LLM call on ordinary personal mail.
                        use_llm_fallback=False,
                        llm_model=args.llm_extraction_model,
                        dry_run=args.dry_run,
                        output_root=args.output_root,
                    )
                )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"[{r['action'] or r['tier']}] {r['direction']}  {r['subject'][:70]!r}  <{r['from'] or r['to']}>")
            print(f"    tier={r['tier']}  job_key={r['job_key']}")
            print(f"    {r['reason']}")
        linked = sum(1 for r in results if r["action"].startswith("linked"))
        new_leads = sum(1 for r in results if r["action"].startswith("new lead created"))
        parked = sum(1 for r in results if r["action"] == "parked (unmatched)")
        print(
            f"\nScanned {len(results)} new message(s): {linked} linked, {new_leads} new lead(s) created, "
            f"{parked} parked as unmatched, {len(results) - linked - new_leads - parked} skipped.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
