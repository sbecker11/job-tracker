"""Stage-based pending-actions workflow (Clarify → Send résumé → Wait → Decide/apply).

Channel (LinkedIn vs email) is a badge only — priority is recruiter contact
attempts, then age. Used by `scripts/render_pending_actions.py` to emit
`var/pending-actions.json` for the React UI.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from job_tracker.pipeline.qualifying_reply import detect_pitch_gaps, draft_qualifying_reply
from job_tracker.pipeline.reply_continuity import (
    RECRUITING_GMAIL_USER,
    enrich_item_reply_links,
    recruiting_gmail_message_url,
)
from job_tracker.pipeline.store import list_job_conversations
from job_tracker.scoring.scorer import DEFAULT_FRAMEWORK_PATH, load_framework

_OUTREACH_MAX_AGE_DAYS = 14

# Re-export for callers/tests that imported these from pending_workflow.
_GMAIL_API_ID_RE = re.compile(r"^[0-9a-f]{10,}$", re.I)


def _gmail_api_id_for_conversations(convs: list, preferred: str = "") -> str:
    """Pick a hex Gmail API id for the last recruiter-side message when possible.

    Prefers ``preferred`` when it is already a Gmail API id; otherwise the newest
    **inbound** hex id (recruiter → Shawn); else the newest hex id of any
    direction (same thread — still opens recruiting Gmail for reply).
    """
    pref = (preferred or "").strip()
    if _GMAIL_API_ID_RE.fullmatch(pref):
        return pref
    inbound_hex: list[str] = []
    any_hex: list[str] = []
    for c in convs:
        mid = str(c["message_id"] or "").strip() if hasattr(c, "keys") else str((c or {}).get("message_id") or "").strip()
        if not _GMAIL_API_ID_RE.fullmatch(mid):
            continue
        any_hex.append(mid)
        if (c["direction"] if hasattr(c, "keys") else (c or {}).get("direction") or "") == "inbound":
            inbound_hex.append(mid)
    if inbound_hex:
        return inbound_hex[-1]
    if any_hex:
        return any_hex[-1]
    return ""


def _gmail_url_for_lead(conn, job_key: str, preferred_message_id: str = "") -> str:
    convs = list(list_job_conversations(conn, job_key)) if job_key else []
    return recruiting_gmail_message_url(
        _gmail_api_id_for_conversations(convs, preferred_message_id)
    )


def _wait_followup_days() -> int:
    """Days waiting before Contact priority prompts a re-initiate follow-up."""
    try:
        thresholds = (load_framework(DEFAULT_FRAMEWORK_PATH).get("thresholds") or {})
        return int(thresholds.get("wait_followup_days", 7))
    except Exception:
        return 7

_RESUME_ASK_RE = re.compile(
    r"(?i)\b("
    r"send (me )?(your |a )?(resume|résumé|cv)|"
    r"(resume|résumé|cv) (please|attached|asap)|"
    r"please (share|send|attach).{0,40}(resume|résumé|cv)|"
    r"looking forward to (receiving |seeing )?(your )?(resume|résumé|cv)"
    r")\b"
)

_NO_REPLY_RE = re.compile(
    r"(?i)(no[\-]?reply|donotreply|do[\-]?not[\-]?reply|notifications?@|"
    r"jobalerts?@|jobs\-noreply@|alerts@|noreply@)"
)

_DIGEST_TEXT_RE = re.compile(
    r"(?i)("
    r"jobs you don.?t want to miss|"
    r"more jobs like|"
    r"job alert|"
    r"jobs? digest|"
    r"new jobs for you|"
    r"jobs matching your|"
    r"recommended jobs|"
    r"talent\.com"
    r")"
)

_AUTO_ACK_RE = re.compile(
    r"(?i)("
    r"thank you for applying|"
    r"thanks for applying|"
    r"we (have )?received your application|"
    r"application received|"
    r"your application has been (received|submitted)"
    r")"
)

_SYSTEM_CONTACT_DOMAIN_RE = re.compile(
    r"(?i)@(alerts\.)?talent\.com$|@linkedin\.com$|@greenhouse-mail\.io$|"
    r"@ashbyhq\.com$|@mail\.greenhouse\.io$"
)


def _is_system_email(email: str) -> bool:
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return False
    if _NO_REPLY_RE.search(e):
        return True
    return bool(_SYSTEM_CONTACT_DOMAIN_RE.search(e))


def _is_digest_conversation(conv) -> bool:
    """Job-alert digests are not recruiter contact attempts."""
    blob = f"{conv['summary'] or ''}\n{(conv['body_text'] or '')[:800]}"
    return bool(_DIGEST_TEXT_RE.search(blob))


def _is_auto_ack_conversation(conv) -> bool:
    """ATS 'thanks for applying' mail is not a human follow-up to answer."""
    blob = f"{conv['summary'] or ''}\n{(conv['body_text'] or '')[:800]}"
    return bool(_AUTO_ACK_RE.search(blob))


def _human_inbound_conversations(convs: list) -> list:
    return [
        c
        for c in convs
        if (c["direction"] or "") == "inbound"
        and not _is_digest_conversation(c)
        and not _is_auto_ack_conversation(c)
    ]


def _unanswered_inbound(convs: list) -> object | None:
    """Latest human inbound that arrived after Shawn's last outbound (or with no outbound)."""
    human_in = _human_inbound_conversations(convs)
    if not human_in:
        return None
    outbound = [c for c in convs if (c["direction"] or "") == "outbound"]
    last_in = max(human_in, key=lambda c: c["occurred_at"] or "")
    if not outbound:
        return last_in
    last_out = max(outbound, key=lambda c: c["occurred_at"] or "")
    if (last_in["occurred_at"] or "") > (last_out["occurred_at"] or ""):
        return last_in
    return None


def _has_human_recruiter_on_file(conn, job_key: str) -> bool:
    """True when a named person or non-system email/phone exists for this lead."""
    rows = conn.execute(
        """
        SELECT name, email, phone, linkedin_reply_to FROM job_contacts
        WHERE job_key = ?
        """,
        (job_key,),
    ).fetchall()
    for row in rows:
        name = (row["name"] or "").strip()
        email = (row["email"] or "").strip()
        phone = (row["phone"] or "").strip()
        li_reply = ""
        try:
            li_reply = (row["linkedin_reply_to"] or "").strip()
        except (IndexError, KeyError):
            li_reply = ""
        if name and not _is_system_email(email):
            return True
        if email and not _is_system_email(email):
            return True
        if li_reply and name:
            return True
        if phone and not email:
            return True
    return False


def _next_action_clarify(
    *,
    channel: str,
    recruiter_name: str = "",
    reply_due: bool = False,
    unanswered_days: int = 0,
    thread_url: str = "",
    gmail_url: str = "",
) -> str:
    who = recruiter_name.strip() or "the recruiter"
    if (thread_url or "").strip():
        open_step = f"Open LinkedIn thread (3) Paste & send to {who}"
    elif (gmail_url or "").strip():
        open_step = f"Reply in Gmail to {who} (LinkedIn continuity copy)"
    elif channel == "linkedin":
        open_step = f"Open LinkedIn thread (3) Paste & send to {who}"
    else:
        open_step = f"Reply in Gmail to {who}"

    if reply_due:
        age = f" ({unanswered_days}d unanswered)" if unanswered_days else ""
        if (thread_url or "").strip() or (channel == "linkedin" and not (gmail_url or "").strip()):
            return (
                f"YOUR ACTION: Recruiter followed up{age} — reply now. "
                f"(1) Copy reply (2) {open_step} "
                f"(4) Dismiss / marked replied"
            )
        return (
            f"YOUR ACTION: Recruiter followed up{age} — reply now. "
            f"(1) Copy reply (2) {open_step} (3) Dismiss / marked replied"
        )
    if (thread_url or "").strip() or (channel == "linkedin" and not (gmail_url or "").strip()):
        return (
            f"YOUR ACTION: (1) Copy reply (2) {open_step} "
            f"(4) Dismiss / marked replied"
        )
    return f"YOUR ACTION: (1) Copy reply (2) {open_step} (3) Dismiss / marked replied"


def draft_recruiter_followup_ack(
    *,
    recruiter_name: str = "",
    company: str = "",
    title: str = "",
    inbound_snippet: str = "",
) -> str:
    """Short reply when a recruiter followed up after our last outbound."""
    who = (recruiter_name or "").strip() or "there"
    role_bits = " — ".join(p for p in (title.strip(), company.strip()) if p)
    role_clause = f" on the {role_bits} role" if role_bits else ""
    return (
        f"Hi {who},\n\n"
        f"Thanks for the follow-up{role_clause} — still very interested. "
        f"Happy to jump on a quick call or send anything else you need.\n\n"
        f"Best,\nShawn"
    )


def _next_action_send_resume(
    *,
    recruiter_name: str = "",
    apply_url: str = "",
    package_ready: bool = False,
    channel: str = "email",
    recruiter_email: str = "",
) -> str:
    who = recruiter_name.strip()
    if apply_url and not who:
        return (
            "YOUR ACTION: This is ATS apply work, not a recruiter email — "
            "use Decide/apply (or open Apply URL). Do not treat alert digests as contacts."
        )
    target = who or "the recruiter"
    via = (
        f"email to {recruiter_email}"
        if recruiter_email
        else ("LinkedIn thread" if channel == "linkedin" else "email/LinkedIn")
    )
    if not package_ready:
        return (
            f"YOUR ACTION: (1) Generate the résumé package first "
            f"(2) Open package folder (3) Copy message & send to {target} via {via} "
            f"(4) Mark sent — email Sent-scan usually auto-moves to Wait; LinkedIn needs Mark sent"
        )
    return (
        f"YOUR ACTION: (1) Open package folder (2) Copy message & attach résumé/cover to {target} "
        f"via {via} (3) Mark sent — email Sent-scan usually auto-moves to Wait; LinkedIn needs Mark sent"
    )


def draft_resume_send_message(
    *,
    recruiter_name: str = "",
    company: str = "",
    title: str = "",
) -> str:
    """Short attach-and-send note for Send résumé (CLAUDE.md §12 short-form)."""
    who = (recruiter_name or "").strip() or "there"
    role_bits = " — ".join(p for p in (title.strip(), company.strip()) if p)
    role_clause = f" for the {role_bits} role" if role_bits else ""
    return (
        f"Hi {who},\n\n"
        f"Please find attached my résumé and cover letter{role_clause}.\n\n"
        f"Happy to walk through fit whenever useful.\n\n"
        f"Best,\nShawn"
    )


def draft_wait_followup_message(
    *,
    recruiter_name: str = "",
    company: str = "",
    title: str = "",
    waiting_days: int = 0,
) -> str:
    """Short status-check-in when Wait has exceeded the follow-up threshold."""
    who = (recruiter_name or "").strip() or "there"
    role_bits = " — ".join(p for p in (title.strip(), company.strip()) if p)
    role_clause = f" on the {role_bits} role" if role_bits else ""
    days_bit = f" ({waiting_days} days)" if waiting_days > 0 else ""
    return (
        f"Hi {who},\n\n"
        f"Just bumping this{role_clause} — checking whether there's any update"
        f"{days_bit}, or if you need anything else from me.\n\n"
        f"Best,\nShawn"
    )


def _next_action_wait(*, waiting_days: int = 0, threshold: int = 7, recruiter_name: str = "") -> str:
    if waiting_days >= threshold:
        who = recruiter_name.strip() or "the recruiter"
        return (
            f"YOUR ACTION: Wait exceeded {threshold}d ({waiting_days}d silent) — "
            f"(1) Copy follow-up (2) Re-initiate with {who} (3) Mark sent → reset Wait clock"
        )
    remaining = threshold - waiting_days
    return (
        f"YOUR ACTION: None right now — wait for their reply "
        f"({waiting_days}d of {threshold}d before follow-up; {remaining}d left). "
        f"Schedule a call only if already agreed."
    )
PIPELINE_STAGES = (
    {
        "id": "clarify",
        "label": "Clarify",
        "action": "Reply with clarifiers (end client / W2–1099 / remote / rate)",
    },
    {
        "id": "send_resume",
        "label": "Send résumé",
        "action": "Send package to the recruiter, then mark sent",
    },
    {
        "id": "wait_schedule",
        "label": "Wait / schedule",
        "action": "Ball in their court — or book the follow-up",
    },
    {
        "id": "decide_apply",
        "label": "Decide / apply",
        "action": "Review verdict or submit the application",
    },
)


def _priority_sort_key(item: dict) -> tuple:
    # Unreplied recruiter messages first, then attempts / age.
    reply_rank = 0 if item.get("replyDue") else 1
    unanswered = int(item.get("unansweredDays") or item.get("ageDays") or 0)
    return (
        reply_rank,
        -int(item.get("contactAttempts") or 0),
        -unanswered,
        (item.get("company") or "").lower(),
    )


def _channel_from_addresses(*, from_address: str = "", thread_url: str = "", source_label: str = "") -> str:
    fr = (from_address or "").strip().lower()
    src = (source_label or "").strip().lower()
    if thread_url or "linkedin" in fr or src.startswith("linkedin"):
        return "linkedin"
    return "email"


def _has_replyable_contact(*, from_address: str = "", recruiter_name: str = "", thread_url: str = "") -> bool:
    if (thread_url or "").strip():
        return True
    if (recruiter_name or "").strip():
        return True
    fr = (from_address or "").strip()
    if not fr:
        return False
    return not bool(_NO_REPLY_RE.search(fr))


def build_clarify_queue(
    *,
    linkedin_reply_queue: list[dict],
    unmatched_communications: list[dict],
    age_days_fn: Callable[[str | None, datetime], int],
    now: datetime,
    conn,
) -> list[dict]:
    """Priority-A: replyable recruiter outreach needing a clarifying reply."""
    queue: list[dict] = []
    seen_reply_ids: set[str] = set()
    seen_keys: set[str] = set()
    seen_mids: set[str] = set()
    continuity_cache: dict[str, dict[str, str]] = {}

    def _add(item: dict) -> None:
        rid = item.get("replyId") or ""
        key = item.get("normalizedKey") or ""
        mid = item.get("messageId") or ""
        if rid and rid in seen_reply_ids:
            return
        if key and key in seen_keys:
            return
        if mid and mid in seen_mids:
            return
        if rid:
            seen_reply_ids.add(rid)
        if key:
            seen_keys.add(key)
        if mid:
            seen_mids.add(mid)
        if not item.get("gmailUrl"):
            gmail_url = ""
            if key:
                gmail_url = _gmail_url_for_lead(conn, key, mid)
            if not gmail_url:
                gmail_url = recruiting_gmail_message_url(mid)
            if gmail_url:
                item["gmailUrl"] = gmail_url
        # When reply links are still missing, search recruiting Gmail for the
        # best LinkedIn/email continuity copy and backfill message_id.
        if not item.get("gmailUrl") or (
            (item.get("channel") == "linkedin" or "linkedin" in (item.get("subject") or "").lower())
            and not item.get("threadUrl")
        ):
            enrich_item_reply_links(item, conn=conn, cache=continuity_cache)
        next_action = _next_action_clarify(
            channel=item.get("channel") or "email",
            recruiter_name=item.get("recruiterName") or "",
            reply_due=bool(item.get("replyDue")),
            unanswered_days=int(item.get("unansweredDays") or 0),
            thread_url=item.get("threadUrl") or "",
            gmail_url=item.get("gmailUrl") or "",
        )
        item["nextAction"] = next_action
        item["actionHint"] = next_action
        queue.append(item)

    for item in linkedin_reply_queue:
        key = item.get("normalizedKey") or ""
        attempts = int(item.get("contactAttempts") or 0)
        unanswered_days = int(item.get("ageDays") or 0)
        if key:
            convs = list(list_job_conversations(conn, key))
            attempts = len(_human_inbound_conversations(convs)) or max(1, attempts)
            unanswered = _unanswered_inbound(convs)
            if unanswered is not None and unanswered["occurred_at"]:
                unanswered_days = age_days_fn(unanswered["occurred_at"], now)
        channel = _channel_from_addresses(
            thread_url=item.get("threadUrl") or "",
            source_label="linkedin_message" if item.get("kind") == "lead" else "",
            from_address="",
        )
        next_action = _next_action_clarify(
            channel=channel,
            recruiter_name=item.get("recruiterName") or "",
            reply_due=True,
            unanswered_days=unanswered_days,
        )
        _add(
            {
                **item,
                "stage": "clarify",
                "channel": channel,
                "contactAttempts": max(1, attempts),
                "replyDue": True,
                "unansweredDays": unanswered_days,
                "actionHint": next_action,
                "nextAction": next_action,
            }
        )

    # Email (and any non-LinkedIn) unmatched pitches with a draft + replyable contact.
    for m in unmatched_communications:
        if (m.get("ageDays") or 0) > _OUTREACH_MAX_AGE_DAYS:
            continue
        mid = m.get("messageId") or ""
        if mid and mid in seen_mids:
            continue
        if m.get("linkedinReplyId"):
            continue  # already represented via LinkedIn section-0 card
        draft = (m.get("draftReply") or "").strip()
        if not draft:
            continue
        fr = m.get("fromAddress") or ""
        thread = m.get("threadUrl") or ""
        name = m.get("recruiterName") or ""
        if not _has_replyable_contact(from_address=fr, recruiter_name=name, thread_url=thread):
            continue
        # Skip pure LinkedIn senders — those belong in linkedin_reply_queue.
        if fr.strip().lower() in {"hit-reply@linkedin.com", "inmail-hit-reply@linkedin.com"}:
            continue
        body = m.get("body") or ""
        gaps = detect_pitch_gaps(body, subject=m.get("subject") or "")
        if not any(
            (gaps.needs_jd, gaps.needs_end_client, gaps.needs_engagement, gaps.needs_remote, gaps.needs_rate_band)
        ):
            continue
        reply_id = f"mid:{mid}" if mid else f"fallback:{(name or '').lower()}|{(m.get('subject') or '').lower()}"
        channel = _channel_from_addresses(from_address=fr, thread_url=thread)
        next_action = _next_action_clarify(
            channel=channel, recruiter_name=name, reply_due=True, unanswered_days=m.get("ageDays") or 0
        )
        _add(
            {
                "kind": "unmatched",
                "stage": "clarify",
                "channel": channel,
                "recruiterName": name,
                "subject": m.get("subject") or "",
                "company": m.get("companyGuess") or "",
                "title": m.get("titleGuess") or "",
                "threadUrl": thread,
                "draftReply": draft,
                "ageDays": m.get("ageDays") or 0,
                "messageId": mid,
                "replyId": reply_id,
                "contactAttempts": 1,
                "replyDue": True,
                "unansweredDays": m.get("ageDays") or 0,
                "actionHint": next_action,
                "nextAction": next_action,
            }
        )

    # Stub / email leads with inbound, no outbound yet, still missing clarifiers.
    for r in conn.execute(
        """
        SELECT normalized_key, company, title, source_label, first_seen
        FROM job_leads
        WHERE deleted_at IS NULL
          AND status = 'new'
        ORDER BY first_seen DESC
        """
    ):
        key = r["normalized_key"]
        if key in seen_keys:
            continue
        age = age_days_fn(r["first_seen"], now)
        if age > _OUTREACH_MAX_AGE_DAYS:
            continue
        convs = list(list_job_conversations(conn, key))
        if not convs:
            continue
        if any((c["direction"] or "") == "outbound" for c in convs):
            continue
        inbound = _human_inbound_conversations(convs)
        if not inbound:
            continue
        latest = inbound[-1]
        body = latest["body_text"] or ""
        subj = latest["summary"] or r["title"] or ""
        draft = draft_qualifying_reply(body, subject=subj)
        if not draft.thread_url and not draft.recruiter_name and not _has_human_recruiter_on_file(conn, key):
            continue
        gaps = draft.gaps
        if not any(
            (gaps.needs_jd, gaps.needs_end_client, gaps.needs_engagement, gaps.needs_remote, gaps.needs_rate_band)
        ):
            continue
        channel = _channel_from_addresses(
            thread_url=draft.thread_url,
            source_label=r["source_label"] or "",
        )
        reply_id = f"key:{key}"
        next_action = _next_action_clarify(
            channel=channel,
            recruiter_name=draft.recruiter_name,
            reply_due=True,
            unanswered_days=age,
        )
        _add(
            {
                "kind": "lead",
                "stage": "clarify",
                "channel": channel,
                "recruiterName": draft.recruiter_name,
                "subject": subj,
                "company": r["company"] or draft.company_guess,
                "title": r["title"] or draft.title_guess,
                "threadUrl": draft.thread_url,
                "draftReply": draft.body,
                "ageDays": age,
                "messageId": latest["message_id"] or "",
                "normalizedKey": key,
                "replyId": reply_id,
                "contactAttempts": len(inbound),
                "replyDue": True,
                "unansweredDays": age,
                "actionHint": next_action,
                "nextAction": next_action,
            }
        )

    # Recruiter followed up after Shawn's last outbound — must not get buried.
    _REPLY_DUE_MAX_AGE_DAYS = 30
    for r in conn.execute(
        """
        SELECT normalized_key, company, title, source_label, first_seen
        FROM job_leads
        WHERE deleted_at IS NULL
          AND status NOT IN ('rejected', 'skipped', 'withdrawn', 'accepted', 'started',
                             'deleted', 'unavailable', 'hired')
        """
    ):
        key = r["normalized_key"]
        if key in seen_keys:
            continue
        if not _has_human_recruiter_on_file(conn, key):
            continue
        convs = list(list_job_conversations(conn, key))
        outbound = [c for c in convs if (c["direction"] or "") == "outbound"]
        if not outbound:
            continue  # first-touch pitches handled above
        unanswered = _unanswered_inbound(convs)
        if unanswered is None:
            continue
        unanswered_days = age_days_fn(unanswered["occurred_at"], now)
        if unanswered_days > _REPLY_DUE_MAX_AGE_DAYS:
            continue
        recruiter, email, email_is_li_relay = _recruiter_contact(conn, key)
        body = unanswered["body_text"] or ""
        subj = unanswered["summary"] or r["title"] or ""
        drafted = draft_qualifying_reply(body, subject=subj)
        gaps = drafted.gaps
        still_needs_clarifiers = any(
            (
                gaps.needs_jd,
                gaps.needs_end_client,
                gaps.needs_engagement,
                gaps.needs_remote,
                gaps.needs_rate_band,
            )
        )
        draft_body = (
            drafted.body
            if still_needs_clarifiers and drafted.body.strip()
            else draft_recruiter_followup_ack(
                recruiter_name=recruiter or drafted.recruiter_name,
                company=r["company"] or "",
                title=r["title"] or "",
                inbound_snippet=subj,
            )
        )
        channel = _channel_from_addresses(
            thread_url=drafted.thread_url,
            source_label=r["source_label"] or "",
        )
        if email and not _is_system_email(email) and not email_is_li_relay:
            channel = "email"
        next_action = _next_action_clarify(
            channel=channel,
            recruiter_name=recruiter or drafted.recruiter_name,
            reply_due=True,
            unanswered_days=unanswered_days,
        )
        _add(
            {
                "kind": "lead",
                "stage": "clarify",
                "channel": channel,
                "recruiterName": recruiter or drafted.recruiter_name,
                "recruiterEmail": email,
                "emailIsLinkedInRelay": email_is_li_relay,
                "subject": subj,
                "company": r["company"] or drafted.company_guess,
                "title": r["title"] or drafted.title_guess,
                "threadUrl": drafted.thread_url or _thread_url_for_lead(convs),
                "draftReply": draft_body,
                "ageDays": age_days_fn(r["first_seen"], now),
                "messageId": unanswered["message_id"] or "",
                "normalizedKey": key,
                "replyId": f"reply-due:{key}",
                "contactAttempts": len(_human_inbound_conversations(convs)),
                "replyDue": True,
                "unansweredDays": unanswered_days,
                "actionHint": next_action,
                "nextAction": next_action,
            }
        )

    queue.sort(key=_priority_sort_key)
    return queue


def _recruiter_contact(conn, job_key: str) -> tuple[str, str, bool]:
    """Return (display_name, mailto_email, email_is_linkedin_relay).

    Prefer a real non-system `email`. Fall back to `linkedin_reply_to`
    (uuid@reply.linkedin.com) so Clarify can still open a mail draft into
    the LinkedIn thread when the recruiter never left a personal address.
    """
    row = conn.execute(
        """
        SELECT name, email, linkedin_reply_to FROM job_contacts
        WHERE job_key = ?
        ORDER BY CASE WHEN role = 'recruiter' THEN 0 ELSE 1 END, first_contacted_at ASC
        """,
        (job_key,),
    ).fetchall()
    fallback_name, fallback_li = "", ""
    for r in row:
        name = (r["name"] or "").strip()
        email = (r["email"] or "").strip()
        try:
            li_reply = (r["linkedin_reply_to"] or "").strip()
        except (IndexError, KeyError):
            li_reply = ""
        if email and _is_system_email(email):
            if li_reply and not fallback_li:
                fallback_name, fallback_li = name or email, li_reply
            continue
        if email:
            return name or email, email, False
        if li_reply and not fallback_li:
            fallback_name, fallback_li = name, li_reply
        if name and not email and not li_reply:
            return name, "", False
    if fallback_li:
        return fallback_name or fallback_li, fallback_li, True
    return "", "", False


def _package_folder_abs(conn, *, company: str, title: str, output_root: Path) -> tuple[str, bool]:
    """Absolute package folder path + whether résumé+cover exist on disk."""
    from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _safe_filename

    root = Path(output_root) if output_root else DEFAULT_OUTPUT_ROOT
    n = conn.execute(
        """
        SELECT COUNT(*) AS n FROM job_leads
        WHERE deleted_at IS NULL AND company = ?
        """,
        (company,),
    ).fetchone()["n"]
    company_safe = _safe_filename(company)
    package_rel = (
        f"{company_safe}/{_safe_filename(f'{company}_{title}')}" if n > 1 else company_safe
    )
    lead_dir = root / package_rel
    # Prefer an existing on-disk folder even if multi_lead naming differs.
    if not lead_dir.is_dir():
        flat = root / company_safe
        if flat.is_dir():
            lead_dir = flat
    names = [p.name.lower() for p in lead_dir.glob("*.docx")] if lead_dir.is_dir() else []
    ready = any("resume" in n for n in names) and any("cover" in n for n in names)
    return (str(lead_dir) if lead_dir.is_dir() else str(root / package_rel), ready)


def _thread_url_for_lead(convs: list) -> str:
    for c in convs:
        blob = f"{c['body_text'] or ''}\n{c['summary'] or ''}"
        m = re.search(r"(https://www\.linkedin\.com/messaging/thread/\S+)", blob, re.I)
        if m:
            return m.group(1).rstrip(").,]")
    return ""


def build_send_resume_queue(
    *,
    ready_to_apply: list[dict],
    needs_decision_forced: list[dict],
    clarify_keys: set[str],
    conn,
    age_days_fn: Callable[[str | None, datetime], int],
    now: datetime,
    output_root: Path | None = None,
) -> list[dict]:
    """Priority-A′: recruiter asked for a résumé *and* the package is on disk.

    A résumé ask alone is not enough — Shawn must still review no-LLM / full-LLM
    reviews and choose pursue (generate package) or skip. Until résumé+cover
    exist, the lead stays in Decide/apply.
    """
    from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT

    root = Path(output_root) if output_root else DEFAULT_OUTPUT_ROOT
    queue: list[dict] = []
    seen: set[str] = set()

    def _append_item(
        *,
        key: str,
        company: str,
        title: str,
        source_label: str,
        age_days: int,
        contact_attempts: int,
        asked: bool,
        apply_url: str = "",
        package_kind: str = "",
        folder_hint: str = "",
    ) -> bool:
        recruiter, email, email_is_li_relay = _recruiter_contact(conn, key)
        folder_abs, disk_ready = _package_folder_abs(conn, company=company, title=title, output_root=root)
        if folder_hint and not Path(folder_abs).is_dir():
            hinted = root / folder_hint
            if hinted.is_dir():
                folder_abs = str(hinted)
                names = [p.name.lower() for p in hinted.glob("*.docx")]
                disk_ready = any("resume" in n for n in names) and any("cover" in n for n in names)
        # Gate: never ask Shawn to send before he has reviewed → pursued → package.
        if not disk_ready:
            return False
        channel = _channel_from_addresses(source_label=source_label or "")
        if email and not _is_system_email(email) and not email_is_li_relay:
            channel = "email"
        convs = list(list_job_conversations(conn, key))
        draft = draft_resume_send_message(
            recruiter_name=recruiter, company=company, title=title
        )
        next_action = _next_action_send_resume(
            recruiter_name=recruiter,
            apply_url=apply_url,
            package_ready=True,
            channel=channel,
            recruiter_email=email,
        )
        seen.add(key)
        item = {
                "kind": "lead",
                "stage": "send_resume",
                "channel": channel,
                "company": company,
                "title": title,
                "normalizedKey": key,
                "recruiterName": recruiter,
                "recruiterEmail": email,
                "emailIsLinkedInRelay": email_is_li_relay,
                "ageDays": age_days,
                "folderPath": folder_abs,
                "applyUrl": apply_url,
                "threadUrl": _thread_url_for_lead(convs),
                "gmailUrl": recruiting_gmail_message_url(
                    _gmail_api_id_for_conversations(convs)
                ),
                "contactAttempts": max(1, contact_attempts),
                "resumeRequested": asked,
                "packageReady": True,
                "packageKind": package_kind,
                "draftReply": draft,
                "actionHint": next_action,
                "nextAction": next_action,
                "markSentUrl": f"mps://mark?key={quote(key, safe='')}&channel={channel}",
            }
        if not item["gmailUrl"] or not item["threadUrl"]:
            enrich_item_reply_links(item, conn=conn)
        queue.append(item)
        return True

    for bucket, label in (
        (ready_to_apply, "ready"),
        (needs_decision_forced, "forced"),
    ):
        for lead in bucket:
            key = lead.get("normalizedKey") or ""
            if not key or key in clarify_keys or key in seen:
                continue
            convs = list(list_job_conversations(conn, key))
            human_inbound = _human_inbound_conversations(convs)
            has_person = bool(lead.get("directRecruiter")) or _has_human_recruiter_on_file(conn, key)
            asked = any(
                _RESUME_ASK_RE.search((c["body_text"] or "") + " " + (c["summary"] or ""))
                for c in human_inbound
            )
            if not has_person:
                continue
            if not asked and not lead.get("directRecruiter"):
                continue
            row = conn.execute(
                "SELECT awaiting_response_since, source_label FROM job_leads WHERE normalized_key = ?",
                (key,),
            ).fetchone()
            if row and row["awaiting_response_since"] and not asked:
                continue
            _append_item(
                key=key,
                company=lead.get("company") or "",
                title=lead.get("title") or "",
                source_label=(row["source_label"] if row else "") or "",
                age_days=int(lead.get("ageDays") or 0),
                contact_attempts=len(human_inbound),
                asked=asked,
                apply_url=lead.get("applyUrl") or "",
                package_kind=label,
                folder_hint=lead.get("folderPath") or "",
            )

    for r in conn.execute(
        """
        SELECT normalized_key, company, title, source_label, first_seen, awaiting_response_since
        FROM job_leads
        WHERE deleted_at IS NULL
          AND status IN ('new', 'package_generated')
        """
    ):
        key = r["normalized_key"]
        if key in seen or key in clarify_keys:
            continue
        if not _has_human_recruiter_on_file(conn, key):
            continue
        convs = list(list_job_conversations(conn, key))
        human_inbound = _human_inbound_conversations(convs)
        outbound = [c for c in convs if (c["direction"] or "") == "outbound"]
        if not human_inbound or not outbound:
            continue
        asked = any(
            _RESUME_ASK_RE.search((c["body_text"] or "") + " " + (c["summary"] or ""))
            for c in human_inbound
        )
        if not asked:
            continue
        _append_item(
            key=key,
            company=r["company"] or "",
            title=r["title"] or "",
            source_label=r["source_label"] or "",
            age_days=age_days_fn(r["first_seen"], now),
            contact_attempts=len(human_inbound),
            asked=True,
        )

    queue.sort(key=_priority_sort_key)
    return queue


def build_wait_schedule_queue(
    *,
    conn,
    clarify_keys: set[str],
    send_keys: set[str],
    age_days_fn: Callable[[str | None, datetime], int],
    now: datetime,
) -> list[dict]:
    """After outbound clarify/résumé — awaiting recruiter reply or call scheduling.

    When waitingDays >= wait_followup_days, surface a re-initiate prompt with a
    draft status-check-in (Contact priority treats these as actionable).
    """
    queue: list[dict] = []
    threshold = _wait_followup_days()
    for r in conn.execute(
        """
        SELECT normalized_key, company, title, source_label, first_seen,
               awaiting_response_since, status
        FROM job_leads
        WHERE deleted_at IS NULL
          AND awaiting_response_since IS NOT NULL
          AND status NOT IN ('rejected', 'skipped', 'withdrawn', 'accepted', 'started',
                             'deleted', 'unavailable', 'hired')
        ORDER BY awaiting_response_since ASC
        """
    ):
        key = r["normalized_key"]
        if key in clarify_keys or key in send_keys:
            continue
        convs = list(list_job_conversations(conn, key))
        human_inbound = _human_inbound_conversations(convs)
        waiting_days = age_days_fn(r["awaiting_response_since"], now)
        follow_up_due = waiting_days >= threshold
        recruiter, email, email_is_li_relay = _recruiter_contact(conn, key)
        channel = _channel_from_addresses(source_label=r["source_label"] or "")
        if email and not _is_system_email(email) and not email_is_li_relay:
            channel = "email"
        next_action = _next_action_wait(
            waiting_days=waiting_days, threshold=threshold, recruiter_name=recruiter
        )
        draft = (
            draft_wait_followup_message(
                recruiter_name=recruiter,
                company=r["company"] or "",
                title=r["title"] or "",
                waiting_days=waiting_days,
            )
            if follow_up_due
            else ""
        )
        item = {
                "kind": "lead",
                "stage": "wait_schedule",
                "channel": channel,
                "company": r["company"] or "",
                "title": r["title"] or "",
                "normalizedKey": key,
                "recruiterName": recruiter,
                "recruiterEmail": email,
                "emailIsLinkedInRelay": email_is_li_relay,
                "ageDays": age_days_fn(r["first_seen"], now),
                "waitingDays": waiting_days,
                "awaitingSince": r["awaiting_response_since"],
                "status": r["status"],
                "contactAttempts": max(1, len(human_inbound)),
                "followUpDue": follow_up_due,
                "followUpThresholdDays": threshold,
                "draftReply": draft,
                "threadUrl": _thread_url_for_lead(convs),
                "gmailUrl": recruiting_gmail_message_url(
                    _gmail_api_id_for_conversations(convs)
                ),
                "markSentUrl": f"mps://mark?key={quote(key, safe='')}&channel={channel}",
                "actionHint": next_action,
                "nextAction": next_action,
            }
        if not item["gmailUrl"] or not item["threadUrl"]:
            enrich_item_reply_links(item, conn=conn)
        queue.append(item)
    # Overdue follow-ups first, then longest wait, then contact attempts.
    queue.sort(
        key=lambda x: (
            0 if x.get("followUpDue") else 1,
            -int(x.get("waitingDays") or 0),
            -int(x.get("contactAttempts") or 0),
        )
    )
    return queue


def build_decide_apply_stage(
    *,
    ready_to_apply: list[dict],
    needs_decision: list[dict],
    needs_decision_forced: list[dict],
    awaiting_llm_review: list[dict],
    jd_unresolved: list[dict],
    send_keys: set[str],
    conn=None,
) -> dict[str, list[dict]]:
    """Priority-B funnel buckets (exclude leads already in send-résumé)."""

    def _resume_requested(key: str) -> bool:
        if not conn or not key:
            return False
        convs = list(list_job_conversations(conn, key))
        return any(
            _RESUME_ASK_RE.search((c["body_text"] or "") + " " + (c["summary"] or ""))
            for c in _human_inbound_conversations(convs)
        )

    def _filter(rows: list[dict], *, bucket: str) -> list[dict]:
        out = []
        for lead in rows:
            key = lead.get("normalizedKey") or ""
            if key and key in send_keys:
                continue
            asked = _resume_requested(key)
            if bucket == "needsDecision" or bucket == "needsDecisionForced":
                next_action = (
                    "YOUR ACTION: Review no-LLM + full-LLM reviews → pursue (generate package) "
                    "or skip. Recruiter already asked for a résumé — decide before sending."
                    if asked
                    else "YOUR ACTION: Review no-LLM + full-LLM reviews → pursue (generate package) or skip."
                )
            elif bucket == "readyToApply":
                next_action = (
                    "YOUR ACTION: Package is ready — submit via Apply URL (or send to recruiter if Contact priority lists it)."
                )
            elif bucket == "awaitingLlmReview":
                next_action = "YOUR ACTION: Wait for full-LLM-review (or run the pipeline) — no decision yet."
            else:
                next_action = "YOUR ACTION: Recover the full JD before scoring/deciding."
            out.append(
                {
                    **lead,
                    "stage": "decide_apply",
                    "resumeRequested": asked,
                    "nextAction": next_action,
                    "actionHint": next_action,
                }
            )
        return out

    return {
        "readyToApply": _filter(ready_to_apply, bucket="readyToApply"),
        "needsDecision": _filter(needs_decision, bucket="needsDecision"),
        "needsDecisionForced": _filter(needs_decision_forced, bucket="needsDecisionForced"),
        "awaitingLlmReview": _filter(awaiting_llm_review, bucket="awaitingLlmReview"),
        "jdUnresolved": _filter(jd_unresolved, bucket="jdUnresolved"),
    }


def build_workflow_payload(
    data: dict,
    *,
    conn,
    age_days_fn: Callable[[str | None, datetime], int],
    now: datetime,
    output_root: Path | None = None,
) -> dict[str, Any]:
    """Assemble the React-facing workflow snapshot from a `render()` data dict."""
    clarify = build_clarify_queue(
        linkedin_reply_queue=data.get("linkedin_reply_queue") or [],
        unmatched_communications=data.get("unmatched_communications") or [],
        age_days_fn=age_days_fn,
        now=now,
        conn=conn,
    )
    clarify_keys = {i["normalizedKey"] for i in clarify if i.get("normalizedKey")}

    send_resume = build_send_resume_queue(
        ready_to_apply=data.get("ready_to_apply") or [],
        needs_decision_forced=data.get("needs_decision_forced") or [],
        clarify_keys=clarify_keys,
        conn=conn,
        age_days_fn=age_days_fn,
        now=now,
        output_root=output_root,
    )
    send_keys = {i["normalizedKey"] for i in send_resume if i.get("normalizedKey")}

    wait_schedule = build_wait_schedule_queue(
        conn=conn,
        clarify_keys=clarify_keys,
        send_keys=send_keys,
        age_days_fn=age_days_fn,
        now=now,
    )

    decide_apply = build_decide_apply_stage(
        ready_to_apply=data.get("ready_to_apply") or [],
        needs_decision=data.get("needs_decision") or [],
        needs_decision_forced=data.get("needs_decision_forced") or [],
        awaiting_llm_review=data.get("awaiting_llm_review") or [],
        jd_unresolved=data.get("jd_unresolved") or [],
        send_keys=send_keys,
        conn=conn,
    )

    decide_count = sum(len(v) for v in decide_apply.values())
    counts = {
        "clarify": len(clarify),
        "send_resume": len(send_resume),
        "wait_schedule": len(wait_schedule),
        "decide_apply": decide_count,
    }
    pipeline = [{**stage, "count": counts[stage["id"]]} for stage in PIPELINE_STAGES]

    generated = data.get("generated_at")
    generated_iso = generated.isoformat() if hasattr(generated, "isoformat") else str(generated or "")

    return {
        "generatedAt": generated_iso,
        "folderRoot": str(output_root) if output_root else str(
            __import__("job_tracker.pipeline.llm_apply", fromlist=["DEFAULT_OUTPUT_ROOT"]).DEFAULT_OUTPUT_ROOT
        ),
        "waitFollowupDays": _wait_followup_days(),
        "pipeline": pipeline,
        "stages": {
            "clarify": clarify,
            "sendResume": send_resume,
            "waitSchedule": wait_schedule,
            "decideApply": decide_apply,
        },
        "unmatchedCommunications": data.get("unmatched_communications") or [],
        "poisonedLinkedin": data.get("poisoned_linkedin") or [],
        "scheduleHealth": data.get("schedule_health") or {},
        "totalLeads": data.get("total_leads") or 0,
        "notPrioritizedCount": data.get("not_prioritized_count") or 0,
        "manualHandled": data.get("manual_handled") or [],
        "directRecruiterCount": data.get("direct_recruiter_count") or 0,
    }
