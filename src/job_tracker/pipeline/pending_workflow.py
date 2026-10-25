"""Stage-based pending-actions workflow (Clarify → Send résumé → Wait → Decide/apply).

Channel (LinkedIn vs email) is a badge only — priority is recruiter contact
attempts, then age. Used by `scripts/render_pending_actions.py` to emit
`var/pending-actions.json` for the React UI.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from job_tracker.pipeline.pitch_match import pitches_overlap
from job_tracker.pipeline.qualifying_reply import (
    SIGNATURE,
    detect_pitch_gaps,
    draft_qualifying_reply,
    extract_linkedin_thread_url,
    is_candidate_marketing,
)
from job_tracker.pipeline.reply_intent import draft_live_thread_reply
from job_tracker.pipeline.reply_continuity import (
    RECRUITING_GMAIL_USER,
    enrich_item_reply_links,
    recruiting_gmail_message_url,
)
from job_tracker.pipeline.signature import parse_signature
from job_tracker.pipeline.store import (
    recruiter_contacts,
    RECRUITER_ORDER_SQL,
    GENERIC_RELAY_ADDRESSES,
    is_person_email,
    list_job_conversations,
    name_from_email,
)
from job_tracker.scoring.scorer import DEFAULT_FRAMEWORK_PATH, load_framework

_OUTREACH_MAX_AGE_DAYS = 14

# Re-export for callers/tests that imported these from pending_workflow.
_GMAIL_API_ID_RE = re.compile(r"^[0-9a-f]{10,}$", re.I)

# LinkedIn notification emails pad their invisible tracking pixel area with
# runs of COMBINING GRAPHEME JOINER (U+034F) — harmless in the raw stored
# body, but renders as a wall of visible dots if ever put in front of a
# user (2026-08-23, surfaced when a Clarify-queue row's message history
# turned out to be readable in the raw JSON but not exposed in the React
# UI at all). Stripped only for display; the stored `body_text` in
# job_conversations/unmatched_messages is left untouched.
_LINKEDIN_COMBINING_JOINER = "\u034f"


def clean_message_body_for_display(text: str) -> str:
    """Strip LinkedIn's invisible tracking-pixel padding for on-screen display."""
    return (text or "").replace(_LINKEDIN_COMBINING_JOINER, "")


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


def _conv_text(conv) -> str:
    return f"{conv['summary'] or ''}\n{conv['body_text'] or ''}"


def _distinct_human_inbound(convs: list) -> list:
    """One entry per pitch. A Gmail notification of a pasted thread is the
    same contact, and the paste is the one the card quotes."""
    kept: list = []
    for conv in _human_inbound_conversations(convs):
        text = _conv_text(conv)
        for i, prev in enumerate(kept):
            if pitches_overlap(text, _conv_text(prev)):
                if (conv["message_id"] or "").startswith("paste:") and not (
                    prev["message_id"] or ""
                ).startswith("paste:"):
                    kept[i] = conv
                break
        else:
            kept.append(conv)
    return kept


def _contact_attempt_count(convs: list) -> int:
    return len(_distinct_human_inbound(convs))


def _sort_time(ts: str | None) -> datetime:
    try:
        when = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _unanswered_days(convs: list, age_days_fn: Callable[[str | None, datetime], int], now: datetime) -> int:
    """CARD-8: days since the oldest human inbound after Shawn's last outbound."""
    outbound = [_sort_time(c["occurred_at"]) for c in convs if (c["direction"] or "") == "outbound"]
    last_out = max(outbound) if outbound else None
    waiting = [
        c for c in _human_inbound_conversations(convs)
        if c["occurred_at"] and (last_out is None or _sort_time(c["occurred_at"]) > last_out)
    ]
    if not waiting:
        return 0
    oldest = min(waiting, key=lambda c: _sort_time(c["occurred_at"]))
    return age_days_fn(oldest["occurred_at"], now)


def _unanswered_inbound(convs: list) -> object | None:
    """Latest human inbound that arrived after Shawn's last outbound (or with no outbound)."""
    human_in = _distinct_human_inbound(convs)
    if not human_in:
        return None
    outbound = [c for c in convs if (c["direction"] or "") == "outbound"]
    last_in = max(human_in, key=lambda c: _sort_time(c["occurred_at"]))
    if not outbound:
        return last_in
    last_out = max(outbound, key=lambda c: _sort_time(c["occurred_at"]))
    if _sort_time(last_in["occurred_at"]) > _sort_time(last_out["occurred_at"]):
        return last_in
    return None


def _has_human_recruiter_on_file(conn, job_key: str) -> bool:
    """True when a named person or non-system email/phone exists for this lead."""
    rows = conn.execute(
        """
        SELECT name, email, phone, linkedin_reply_to FROM job_contacts
        WHERE job_key = ? AND COALESCE(role, '') != 'company'
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
    screen_first: bool = False,
    routes: list[dict] | None = None,
    dealbreaker: str = "",
) -> str:
    """`routes` is the card's reply plan (pipeline/reply_plan.py); when given,
    the send step follows its first route instead of guessing from the links.
    `dealbreaker` (a §3 label) means the card's draft is a decline letter."""
    who = recruiter_name.strip() or "the recruiter"
    thread_url = (thread_url or "").strip()
    gmail_url = (gmail_url or "").strip()
    age = f" ({unanswered_days}d unanswered)" if unanswered_days else ""

    steps: list[str] = []
    if screen_first:
        steps.append("Attach latest résumé (generic, not JD-tailored)")
    if routes is not None:
        steps += _route_steps(routes[0] if routes else None, who)
    elif thread_url:
        steps += ["Copy reply", "Open LinkedIn thread", f"Paste & send to {who}"]
    elif channel == "linkedin" and gmail_url:
        steps += ["Copy reply", f"Reply in Gmail to {who} (LinkedIn continuity copy)"]
    elif channel == "linkedin":
        steps += ["Copy reply", f"Find {who}'s LinkedIn conversation, paste & send"]
    else:
        steps += ["Copy reply", f"Reply in Gmail to {who}"]
    steps.append("Dismiss / marked replied")

    if dealbreaker:
        prefix = f"Dealbreaker in the pitch ({dealbreaker}) — send the decline below, then dismiss. "
    elif screen_first and reply_due:
        prefix = f"Recruiter wants résumé + call before a JD{age} — reply now. "
    elif reply_due:
        prefix = f"Recruiter followed up{age} — reply now. "
    else:
        prefix = ""
    numbered = " ".join(f"({i}) {step}" for i, step in enumerate(steps, 1))
    return f"YOUR ACTION: {prefix}{numbered}"


def _route_steps(route: dict | None, who: str) -> list[str]:
    if route is None:
        return ["Copy reply", "No reply route on file — open Manage lead → Search for sender"]
    kind = route.get("kind")
    if kind == "compose":
        return [f"Email {who} (Gmail opens with the reply filled in) and press Send"]
    if kind == "company":
        return ["Email the company inbox (Gmail opens with the reply filled in) and press Send"]
    if kind == "text":
        return [f"Text {who} (check the message, then Send)"]
    if kind == "call":
        return [f"Call {who} — office line, not textable; use the reply as talking points"]
    if kind == "thread" and route.get("linkedin"):
        return ["Copy reply", "Open LinkedIn thread", f"Paste & send to {who}"]
    if kind == "thread":
        return ["Copy reply", "Open the thread", f"Paste & send to {who}"]
    if kind == "profile":
        return ["Copy reply", f"Open {who}'s LinkedIn profile, click Message, paste & send"]
    if kind == "notification":
        return ["Copy reply", "Open the LinkedIn notification, click its Reply button, paste & send"]
    return ["Copy reply", f"Reply in Gmail to {who}"]


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
        f"{SIGNATURE}"
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
        f"{SIGNATURE}"
    )


def draft_wait_followup_message(
    *,
    recruiter_name: str = "",
    company: str = "",
    title: str = "",
    waiting_days: int = 0,
) -> str:
    """Short status check-in (Wait past the follow-up threshold, or Manage Lead → Following up)."""
    from job_tracker.pipeline.decline import clean_role_fields, first_name

    who = first_name(recruiter_name)
    company, title = clean_role_fields(company, title)
    if title and company:
        role_clause = f" on the {title} role at {company}"
    elif title:
        role_clause = f" on the {title} role"
    elif company:
        role_clause = f" on the role at {company}"
    else:
        role_clause = ""
    days_bit = f"It's been {waiting_days} days since I last reached out, so I " if waiting_days > 0 else "I "
    return (
        f"Hi {who},\n\n"
        f"Just checking in{role_clause}. {days_bit}wanted to see whether there's any update, "
        f"or if you need anything else from me.\n\n"
        f"{SIGNATURE}"
    )


def _next_action_wait(*, waiting_days: int = 0, threshold: int = 7, recruiter_name: str = "") -> str:
    if waiting_days >= threshold:
        who = recruiter_name.strip() or "the recruiter"
        return (
            f"YOUR ACTION: Wait exceeded {threshold}d ({waiting_days}d silent) — "
            f"follow up with {who}: Manage lead opens on Follow up — edit the message, "
            f"paste it into the thread and send, then click Message sent"
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
    """Sort for least dead-time + highest interview likelihood.

    1. Reply-due recruiter threads first (clock is running on the relationship)
    2. Confirmed direct-recruiter outreach (human pitch ≫ cold digest)
    3. More contact attempts / longer unanswered
    4. Higher match % when present
    """
    reply_rank = 0 if item.get("replyDue") else 1
    dro = item.get("directRecruiterOutreach")
    if dro is True or dro == 1 or dro == "yes":
        dro_rank = 0
    elif dro is False or dro == 0 or dro == "no":
        dro_rank = 2
    else:
        dro_rank = 1
    unanswered = int(item.get("unansweredDays") or item.get("ageDays") or 0)
    try:
        match = -float(item.get("matchPct") or item.get("llmMatchPct") or 0)
    except (TypeError, ValueError):
        match = 0.0
    return (
        reply_rank,
        dro_rank,
        -int(item.get("contactAttempts") or 0),
        match,
        -unanswered,
        (item.get("company") or "").lower(),
    )


def _channel_from_addresses(*, from_address: str = "", thread_url: str = "", source_label: str = "") -> str:
    fr = (from_address or "").strip().lower()
    src = (source_label or "").strip().lower()
    if thread_url or "linkedin" in fr or src.startswith("linkedin"):
        return "linkedin"
    if not fr and src in ("call", "phone", "sms", "text"):
        return "sms"
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
    from job_tracker.pipeline.decline import pitch_dealbreaker

    queue: list[dict] = []
    seen_reply_ids: set[str] = set()
    seen_keys: set[str] = set()
    seen_mids: set[str] = set()
    seen_threads: set[str] = set()
    continuity_cache: dict[str, dict[str, str]] = {}

    def _add(item: dict) -> None:
        rid = item.get("replyId") or ""
        key = item.get("normalizedKey") or ""
        mid = item.get("messageId") or ""
        thread = (item.get("threadUrl") or "").strip()
        if rid and rid in seen_reply_ids:
            return
        if key and key in seen_keys:
            return
        if mid and mid in seen_mids:
            return
        # Same LinkedIn conversation often lands twice: Spexture IMAP + Gmail
        # InMail copy, with different messageIds and no normalizedKey.
        if thread and thread in seen_threads:
            return
        if rid:
            seen_reply_ids.add(rid)
        if key:
            seen_keys.add(key)
        if mid:
            seen_mids.add(mid)
        if thread:
            seen_threads.add(thread)
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
        screen_first = bool(item.get("screenFirst"))
        if not screen_first:
            screen_first = detect_pitch_gaps(
                item.get("messageBody") or "",
                subject=item.get("subject") or "",
            ).screen_first
            if screen_first:
                item["screenFirst"] = True
        next_action = _next_action_clarify(
            channel=item.get("channel") or "email",
            recruiter_name=item.get("recruiterName") or "",
            reply_due=bool(item.get("replyDue")),
            unanswered_days=int(item.get("unansweredDays") or 0),
            thread_url=item.get("threadUrl") or "",
            gmail_url=item.get("gmailUrl") or "",
            screen_first=screen_first,
        )
        if item.get("postDecline") and item.get("nextAction"):
            next_action = item["nextAction"]
        item["nextAction"] = next_action
        item["actionHint"] = next_action
        queue.append(item)

    for item in linkedin_reply_queue:
        key = item.get("normalizedKey") or ""
        attempts = int(item.get("contactAttempts") or 0)
        unanswered_days = int(item.get("ageDays") or 0)
        if key:
            convs = list(list_job_conversations(conn, key))
            attempts = _contact_attempt_count(convs) or max(1, attempts)
            if _unanswered_inbound(convs) is not None:
                unanswered_days = _unanswered_days(convs, age_days_fn, now)
        channel = _channel_from_addresses(
            thread_url=item.get("threadUrl") or "",
            source_label="linkedin_message" if item.get("kind") == "lead" else "",
            from_address=item.get("fromAddress") or "",
        )
        next_action = _next_action_clarify(
            channel=channel,
            recruiter_name=item.get("recruiterName") or "",
            reply_due=True,
            unanswered_days=unanswered_days,
            thread_url=item.get("threadUrl") or "",
            gmail_url=item.get("gmailUrl") or "",
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
        if is_candidate_marketing(body, subject=m.get("subject") or ""):
            continue
        blocker = pitch_dealbreaker(
            body, subject=m.get("subject") or "", recruiter_name=name, title=m.get("titleGuess") or ""
        )
        if blocker is not None:
            draft = blocker.letter
        else:
            gaps = detect_pitch_gaps(body, subject=m.get("subject") or "")
            if not any(
                (gaps.needs_jd, gaps.needs_end_client, gaps.needs_engagement, gaps.needs_remote, gaps.needs_rate_band)
            ):
                continue
        reply_id = f"mid:{mid}" if mid else f"fallback:{(name or '').lower()}|{(m.get('subject') or '').lower()}"
        channel = _channel_from_addresses(from_address=fr, thread_url=thread)
        next_action = _next_action_clarify(
            channel=channel,
            recruiter_name=name,
            reply_due=blocker is None,
            unanswered_days=m.get("ageDays") or 0,
            thread_url=thread,
            dealbreaker=blocker.label if blocker else "",
        )
        _add(
            {
                "kind": "unmatched",
                "dealbreaker": blocker.label if blocker else "",
                "stage": "clarify",
                "channel": channel,
                "recruiterName": name,
                "recruiterEmail": fr.strip() if is_person_email(fr) else "",
                "subject": m.get("subject") or "",
                "company": m.get("companyGuess") or "",
                "title": m.get("titleGuess") or "",
                "threadUrl": thread,
                "draftReply": draft,
                "messageBody": clean_message_body_for_display(body),
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
        SELECT normalized_key, company, title, source_label, first_seen, jd_text
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
        distinct = _distinct_human_inbound(convs)
        latest = distinct[-1] if distinct else inbound[-1]
        # Leads made by Gmail/IMAP triage before 2026-10-22 logged the first
        # message without its body; the pitch text is on the lead instead.
        body = latest["body_text"] or (r["jd_text"] if len(inbound) == 1 else "") or ""
        subj = latest["summary"] or r["title"] or ""
        recruiter, email, phone, email_is_li_relay = _recruiter_contact(conn, key)
        draft = draft_qualifying_reply(
            body, subject=subj, known_recruiter_name=recruiter, received_at=latest["occurred_at"] or ""
        )
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
            recruiter_name=draft.recruiter_name or recruiter,
            reply_due=True,
            unanswered_days=_unanswered_days(convs, age_days_fn, now),
            thread_url=draft.thread_url or "",
        )
        _add(
            {
                "kind": "lead",
                "stage": "clarify",
                "channel": channel,
                "recruiterName": recruiter or draft.recruiter_name,
                "recruiterEmail": email,
                "recruiterPhone": phone,
                "emailIsLinkedInRelay": email_is_li_relay,
                "subject": subj,
                "company": r["company"] or draft.company_guess,
                "title": r["title"] or draft.title_guess,
                "threadUrl": draft.thread_url,
                "draftReply": draft.body,
                "messageBody": clean_message_body_for_display(body),
                "ageDays": age,
                "messageId": latest["message_id"] or "",
                "normalizedKey": key,
                "replyId": reply_id,
                "contactAttempts": _contact_attempt_count(convs),
                "replyDue": True,
                "unansweredDays": _unanswered_days(convs, age_days_fn, now),
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
          AND status NOT IN ('rejected', 'skipped', 'accepted', 'started',
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
        if age_days_fn(unanswered["occurred_at"], now) > _REPLY_DUE_MAX_AGE_DAYS:
            continue
        unanswered_days = _unanswered_days(convs, age_days_fn, now)
        recruiter, email, phone, email_is_li_relay = _recruiter_contact(conn, key)
        body = unanswered["body_text"] or ""
        subj = unanswered["summary"] or r["title"] or ""
        human_in = sorted(_human_inbound_conversations(convs), key=lambda c: _sort_time(c["occurred_at"]))
        pitch = (human_in[0]["body_text"] or "") if human_in else body
        later = "\n".join(c["body_text"] or "" for c in human_in[1:])
        drafted = draft_qualifying_reply(
            pitch,
            subject=subj,
            known_recruiter_name=recruiter,
            received_at=unanswered["occurred_at"] or "",
            thread_replies=later,
            followup=True,
        )
        gaps = drafted.gaps
        still_needs_clarifiers = any(
            (
                gaps.needs_jd,
                gaps.needs_end_client,
                gaps.needs_engagement,
                gaps.needs_remote,
                gaps.needs_rate_band,
                gaps.screen_first,
            )
        )
        draft_body = draft_live_thread_reply(body, first_name=drafted.first_name, gaps=gaps) or (
            drafted.body
            if still_needs_clarifiers and drafted.body.strip()
            else draft_recruiter_followup_ack(
                recruiter_name=recruiter or drafted.recruiter_name,
                company=r["company"] or "",
                title=r["title"] or "",
                inbound_snippet=subj,
            )
        )
        thread = drafted.thread_url or _thread_url_for_lead(convs, conn, key)
        channel = _card_channel(
            thread_url=thread,
            source_label=r["source_label"] or "",
            email=email,
            email_is_li_relay=email_is_li_relay,
        )
        next_action = _next_action_clarify(
            channel=channel,
            recruiter_name=recruiter or drafted.recruiter_name,
            reply_due=True,
            unanswered_days=unanswered_days,
            thread_url=thread,
        )
        _add(
            {
                "kind": "lead",
                "stage": "clarify",
                "channel": channel,
                "recruiterName": recruiter or drafted.recruiter_name,
                "recruiterEmail": email,
                "recruiterPhone": phone,
                "emailIsLinkedInRelay": email_is_li_relay,
                "subject": subj,
                "company": r["company"] or drafted.company_guess,
                "title": r["title"] or drafted.title_guess,
                "threadUrl": thread,
                "draftReply": draft_body,
                "messageBody": clean_message_body_for_display(body),
                "ageDays": age_days_fn(r["first_seen"], now),
                "messageId": unanswered["message_id"] or "",
                "normalizedKey": key,
                "replyId": f"reply-due:{key}",
                "contactAttempts": _contact_attempt_count(convs),
                "replyDue": True,
                "unansweredDays": unanswered_days,
                "actionHint": next_action,
                "nextAction": next_action,
            }
        )

    # Recruiter wrote back after Shawn declined — skipped leads are excluded
    # above, so without this the reply (often a better offer) is invisible.
    from job_tracker.pipeline.decline import (
        decline_reason,
        draft_post_decline_reply,
        terms_changed,
    )

    for r in conn.execute(
        """
        SELECT normalized_key, company, title, source_label, first_seen,
               decline_reason, decline_note, declined_at
        FROM job_leads
        WHERE deleted_at IS NULL AND status = 'skipped' AND declined_at IS NOT NULL
        """
    ):
        key = r["normalized_key"]
        if key in seen_keys:
            continue
        convs = list(list_job_conversations(conn, key))
        unanswered = _unanswered_inbound(convs)
        if unanswered is None or _sort_time(unanswered["occurred_at"]) <= _sort_time(r["declined_at"]):
            continue
        if age_days_fn(unanswered["occurred_at"], now) > _REPLY_DUE_MAX_AGE_DAYS:
            continue
        declined = _sort_time(r["declined_at"])
        unanswered_days = _unanswered_days(
            [c for c in convs if _sort_time(c["occurred_at"]) > declined], age_days_fn, now
        )
        code = r["decline_reason"] or "other"
        try:
            label = decline_reason(code).label
        except ValueError:
            label = code
        if code == "other" and (r["decline_note"] or "").strip():
            label = f"Other: {r['decline_note'].strip()}"
        body = unanswered["body_text"] or ""
        changed = terms_changed(code, f"{unanswered['summary'] or ''}\n{body}")
        recruiter, email, phone, email_is_li_relay = _recruiter_contact(conn, key)
        thread = _thread_url_for_lead(convs, conn, key)
        channel = _card_channel(
            thread_url=thread, source_label=r["source_label"] or "", email=email, email_is_li_relay=email_is_li_relay
        )
        who = recruiter or "the recruiter"
        lead_in = (
            "may have changed the terms you declined on — read it, then Reopen if it's now a fit"
            if changed
            else "read it; Reopen if it changes things, otherwise Copy reply or Dismiss"
        )
        next_action = (
            f"YOUR ACTION: {who} replied after you declined ({label}) {unanswered_days}d ago — {lead_in}."
        )
        _add(
            {
                "kind": "lead",
                "stage": "clarify",
                "channel": channel,
                "recruiterName": recruiter,
                "recruiterEmail": email,
                "recruiterPhone": phone,
                "emailIsLinkedInRelay": email_is_li_relay,
                "subject": unanswered["summary"] or r["title"] or "",
                "company": r["company"] or "",
                "title": r["title"] or "",
                "threadUrl": thread,
                "draftReply": draft_post_decline_reply(
                    recruiter_name=recruiter, company=r["company"] or "", title=r["title"] or ""
                ),
                "messageBody": clean_message_body_for_display(body),
                "ageDays": age_days_fn(r["first_seen"], now),
                "messageId": unanswered["message_id"] or "",
                "normalizedKey": key,
                "replyId": f"post-decline:{key}",
                "contactAttempts": _contact_attempt_count(convs),
                "replyDue": True,
                "unansweredDays": unanswered_days,
                "postDecline": True,
                "declineReason": code,
                "declineReasonLabel": label,
                "termsChanged": changed,
                "actionHint": next_action,
                "nextAction": next_action,
            }
        )

    queue.sort(key=_priority_sort_key)
    return queue


def _send_route_fields(conn, job_key: str) -> dict:
    """What Manage lead needs to pick a send route: the recruiter's LinkedIn
    profile, a company inbox (Save contact → Company), and whether a
    recruiter owns the lead — company email is only offered when none does."""
    rows = conn.execute(
        "SELECT name, email, role, linkedin_url FROM job_contacts "
        "WHERE job_key = ? ORDER BY id DESC",
        (job_key,),
    ).fetchall()
    fields: dict = {"hasRecruiter": _has_human_recruiter_on_file(conn, job_key)}
    for r in rows:
        role = r["role"] or ""
        email = (r["email"] or "").strip()
        if role == "company" and email and "companyEmail" not in fields:
            fields["companyEmail"] = email
            fields["companyContactName"] = (r["name"] or "").strip()
    for r in recruiter_contacts(conn, job_key):
        if (r["linkedin_url"] or "").strip():
            fields["recruiterLinkedinUrl"] = r["linkedin_url"].strip()
            break
    fields.update(_resolve_recipient(conn, job_key))
    return fields


_SENDER_MESSAGES = 3


def _attach_senders(item: dict, conn, reply_plan_fn) -> None:
    """REQUIREMENTS CARD-14: the lead's human contacts for Manage lead's sender
    chooser, each with its own reply plan and latest received messages. Rows
    logged without a contact, or under a bare LinkedIn-relay contact (no name,
    email, phone or profile), belong to the card's recruiter. Newest first, so
    the default sender is whoever wrote last."""
    key = item.get("normalizedKey")
    if not key:
        return
    def col(c, name: str) -> str:
        return (c[name] or "") if name in c.keys() else ""

    contacts = [
        c
        for c in recruiter_contacts(conn, key)
        if not col(c, "deleted_at")
        and any(col(c, f).strip() for f in ("name", "email", "phone", "linkedin_url"))
    ]
    if not contacts:
        return
    inbound = sorted(
        _human_inbound_conversations(list(list_job_conversations(conn, key))),
        key=lambda c: _sort_time(c["occurred_at"]),
        reverse=True,
    )
    card_name = (item.get("recruiterName") or "").strip().lower()
    primary = next((c for c in contacts if (c["name"] or "").strip().lower() == card_name), contacts[0])
    ids = {c["id"] for c in contacts}
    senders = []
    for c in contacts:
        is_primary = c["id"] == primary["id"]
        mine = [m for m in inbound if m["contact_id"] == c["id"] or (is_primary and m["contact_id"] not in ids)]
        messages = [
            {
                "at": m["occurred_at"] or "",
                "summary": m["summary"] or "",
                "body": clean_message_body_for_display(m["body_text"] or ""),
            }
            for m in mine[:_SENDER_MESSAGES]
        ]
        if is_primary:
            plan = item.get("replyPlan")
        else:
            plan = reply_plan_fn(
                {
                    **{k: v for k, v in item.items() if not k.startswith("resolved") and k != "gmailUrl"},
                    "recruiterName": col(c, "name"),
                    "recruiterEmail": col(c, "email"),
                    "recruiterPhone": col(c, "phone"),
                    "recruiterLinkedinUrl": col(c, "linkedin_url"),
                    "threadUrl": col(c, "linkedin_reply_to"),
                    "emailIsLinkedInRelay": False,
                    "messageBody": mine[0]["body_text"] if mine else "",
                }
            )
        senders.append(
            {
                "id": c["id"],
                "name": c["name"] or (item.get("recruiterName") if is_primary else "") or c["email"] or "(unnamed)",
                "role": c["role"] or "",
                "primary": is_primary,
                "latestAt": messages[0]["at"] if messages else "",
                "messages": messages,
                "replyPlan": plan,
            }
        )
    senders.sort(key=lambda s: _sort_time(s["latestAt"] or None), reverse=True)
    item["senders"] = senders


_LINKEDIN_PROFILE_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.I)


def _phone_digits(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else ""


def _usable_email(email: str) -> bool:
    return is_person_email(email)


def _resolve_recipient(conn, job_key: str) -> dict:
    """Best-effort Send to details when the lead's own recruiter contact has no
    email: the same person on another lead (matched by phone or name+email),
    their signature in this lead's stored inbound messages, or a saved LinkedIn
    reply thread. Read-only — Save contact is what persists a correction."""
    contacts = [dict(r) for r in recruiter_contacts(conn, job_key)]
    if any(_usable_email(c.get("email") or "") for c in contacts):
        return {}
    out: dict = {}
    for c in contacts:
        reply_to = (c.get("linkedin_reply_to") or "").strip()
        if reply_to.startswith("http") and "resolvedThreadUrl" not in out:
            out["resolvedThreadUrl"] = reply_to
    for c in contacts:
        digits = _phone_digits(c.get("phone") or "")
        if not digits:
            continue
        for other in conn.execute(
            "SELECT job_key, email, linkedin_url, phone FROM job_contacts "
            "WHERE job_key != ? AND COALESCE(email, '') != '' ORDER BY id DESC",
            (job_key,),
        ).fetchall():
            if _phone_digits(other["phone"] or "") == digits and _usable_email(other["email"]):
                out["resolvedEmail"] = other["email"].strip()
                out["resolvedVia"] = f"same phone on your {other['job_key'].split('::')[0]} lead"
                if (other["linkedin_url"] or "").strip():
                    out.setdefault("resolvedLinkedinUrl", other["linkedin_url"].strip())
                return out
    try:
        bodies = conn.execute(
            "SELECT body_text FROM job_conversations WHERE job_key = ? AND direction = 'inbound' "
            "AND COALESCE(body_text, '') != '' ORDER BY occurred_at DESC LIMIT 10",
            (job_key,),
        ).fetchall()
    except sqlite3.OperationalError:
        bodies = []
    for (body,) in bodies:
        sig = parse_signature(body)
        email = (sig.email or "").rstrip(".,;")
        if email and _usable_email(email):
            out["resolvedEmail"] = email
            out["resolvedVia"] = "their signature"
            break
        m = _LINKEDIN_PROFILE_RE.search(body)
        if m and "resolvedLinkedinUrl" not in out:
            out["resolvedLinkedinUrl"] = m.group(0)
    return out


def _recruiter_contact(conn, job_key: str) -> tuple[str, str, str, bool]:
    """Return (display_name, mailto_email, phone, email_is_linkedin_relay).

    Prefer a real non-system `email`. Fall back to `linkedin_reply_to`
    (uuid@reply.linkedin.com) so Clarify can still open a mail draft into
    the LinkedIn thread when the recruiter never left a personal address.

    `phone` (2026-08-19) rides along with whichever contact this picks —
    added so Clarify/Send résumé/Wait's "search" boxes can match on phone
    the same way ArchivedLeadsPanel's already does, without a second query.
    """
    row = recruiter_contacts(conn, job_key)
    fallback_name, fallback_li, fallback_phone = "", "", ""
    for r in row:
        raw_email = (r["email"] or "").strip()
        name = (r["name"] or "").strip()
        if "@" in name:
            name = ""
        email = raw_email if is_person_email(raw_email) else ""
        name = name or name_from_email(email)
        phone = (r["phone"] or "").strip()
        try:
            li_reply = (r["linkedin_reply_to"] or "").strip()
        except (IndexError, KeyError):
            li_reply = ""
        if "@" not in li_reply:
            li_reply = ""
        if email:
            return name, email, phone, False
        if li_reply and not fallback_li:
            fallback_name, fallback_li, fallback_phone = name, li_reply, phone
            continue
        if name and not raw_email:
            return name, "", phone, False
        if not fallback_name and name:
            fallback_name, fallback_phone = name, phone
    if fallback_li:
        return fallback_name, fallback_li, fallback_phone, True
    if fallback_name:
        return fallback_name, "", fallback_phone, False
    return "", "", "", False


def _package_folder_abs(conn, *, company: str, title: str, output_root: Path) -> tuple[str, bool]:
    """Absolute package folder path + whether résumé+cover exist on disk.

    Always prefers the nested `<Company>/<Title>/` layout (2026-08-14).
    Falls back to legacy `<Company>/<Company>_<Title>/` or flat `<Company>/`
    when the canonical nested path is missing but an older layout still exists.
    """
    from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _safe_filename

    root = Path(output_root) if output_root else DEFAULT_OUTPUT_ROOT
    company_safe = _safe_filename(company)
    title_safe = _safe_filename(title)
    nested = root / company_safe / title_safe
    legacy_prefixed = root / company_safe / _safe_filename(f"{company}_{title}")
    flat = root / company_safe
    if nested.is_dir():
        lead_dir = nested
    elif legacy_prefixed.is_dir():
        lead_dir = legacy_prefixed
    elif flat.is_dir():
        lead_dir = flat
    else:
        lead_dir = nested
    names = [p.name.lower() for p in lead_dir.glob("*.docx")] if lead_dir.is_dir() else []
    ready = any("resume" in n for n in names) and any("cover" in n for n in names)
    return (str(lead_dir) if lead_dir.is_dir() else str(nested), ready)


def _company_folder_abs(*, company: str, output_root: Path) -> str:
    from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _safe_filename

    root = Path(output_root) if output_root else DEFAULT_OUTPUT_ROOT
    return str(root / _safe_filename(company or "Unknown"))


def _attach_folder_links(
    item: dict,
    *,
    conn,
    output_root: Path,
) -> None:
    """Set folderPath (role) + companyFolderPath when company is known."""
    company = (item.get("company") or "").strip()
    if not company:
        return
    title = (item.get("title") or "").strip()
    role_abs, _ready = _package_folder_abs(
        conn, company=company, title=title or company, output_root=output_root
    )
    if not item.get("folderPath"):
        item["folderPath"] = role_abs
    elif not str(item["folderPath"]).startswith("/"):
        # Decide/apply buckets carry relative paths from render_pending_actions.
        from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT

        root = Path(output_root) if output_root else DEFAULT_OUTPUT_ROOT
        item["folderPath"] = str(root / str(item["folderPath"]))
    item["companyFolderPath"] = _company_folder_abs(company=company, output_root=output_root)


def _duplicate_keys(conn) -> dict[str, list[str]]:
    """`{survivor_normalized_key: [duplicate_normalized_key, ...]}`, most
    recently skipped first (same ordering as store.list_duplicates_of()) —
    every survivor lead worth showing a "N duplicates" link on, wherever it
    appears across the funnel (not just once it's archived), plus the
    specific duplicate keys needed to deep-link the badge straight to the
    first one's row on the Duplicates skipped tab. Added 2026-08-17 (keys,
    not just a count, added the same day after the count-only version could
    switch tabs but not land on a specific row — see
    pending-actions-ui/src/lib/links.ts's leadAnchorId)."""
    rows = conn.execute(
        "SELECT duplicate_of_key, normalized_key FROM job_leads "
        "WHERE duplicate_of_key IS NOT NULL ORDER BY skipped_at DESC"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["duplicate_of_key"], []).append(r["normalized_key"])
    return out


def _attach_lead_statuses(items: list[dict], conn) -> None:
    """Copy `job_leads.status` onto every item that has a `normalizedKey`.

    Wait/archived builders already set this; Clarify, Send résumé, and
    Decide/apply historically did not, which made a UI status filter
    impossible on the active funnel. Skip keys that already have a
    non-empty status so those builders stay authoritative.
    """
    keys = sorted({i.get("normalizedKey") for i in items if i.get("normalizedKey")})
    if not keys:
        return
    statuses: dict[str, str] = {}
    chunk = 400
    for i in range(0, len(keys), chunk):
        batch = keys[i : i + chunk]
        placeholders = ",".join("?" for _ in batch)
        for r in conn.execute(
            f"SELECT normalized_key, status FROM job_leads WHERE normalized_key IN ({placeholders})",
            batch,
        ):
            statuses[r["normalized_key"]] = r["status"] or ""
    for item in items:
        key = item.get("normalizedKey")
        if not key or item.get("status"):
            continue
        item["status"] = statuses.get(key, "")


def _attach_duplicate_count(item: dict, dup_keys: dict[str, list[str]]) -> None:
    """Set `duplicateCount` + `duplicateKeys` (only when > 0, to keep the
    payload lean — see `_attach_folder_links` for the analogous pattern) on
    any item carrying a `normalizedKey` that other leads have been marked a
    duplicate of."""
    key = item.get("normalizedKey")
    if not key:
        return
    keys = dup_keys.get(key) or []
    if keys:
        item["duplicateCount"] = len(keys)
        item["duplicateKeys"] = keys


def _thread_url_for_lead(convs: list, conn=None, job_key: str = "") -> str:
    """The lead's LinkedIn conversation link, from its messages or, failing
    that, a contact field it was saved in (REQUIREMENTS ROUTE-4)."""
    for c in convs:
        url = extract_linkedin_thread_url(f"{c['body_text'] or ''}\n{c['summary'] or ''}")
        if url:
            return url
    if conn is not None and job_key:
        for r in recruiter_contacts(conn, job_key):
            for value in (r["linkedin_reply_to"], r["email"], r["linkedin_url"]):
                url = extract_linkedin_thread_url(value or "")
                if url:
                    return url
    return ""


def _card_channel(*, thread_url: str, source_label: str, email: str, email_is_li_relay: bool) -> str:
    """One rule for every stage: a real person's email makes it an email card;
    otherwise a LinkedIn thread link (or a LinkedIn source) makes it LinkedIn."""
    if email and is_person_email(email) and not email_is_li_relay:
        return "email"
    return _channel_from_addresses(thread_url=thread_url, source_label=source_label)


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
    """Priority-A′: a recruiter is on the lead *and* the package is on disk.

    Ready-to-apply leads qualify when a named recruiter wrote at least one
    real (non-digest) message — an InMail pitch is reason enough to send the
    package back, even without an explicit résumé ask. Forced packages still
    need the ask or the direct-recruiter flag. A résumé ask alone is not
    enough — Shawn must still review and choose pursue (generate package) or
    skip. Until résumé+cover exist, the lead stays in Decide/apply.
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
        direct_recruiter: bool | None = None,
        match_pct: float | None = None,
    ) -> bool:
        recruiter, email, phone, email_is_li_relay = _recruiter_contact(conn, key)
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
        convs = list(list_job_conversations(conn, key))
        thread = _thread_url_for_lead(convs, conn, key)
        channel = _card_channel(
            thread_url=thread, source_label=source_label or "", email=email, email_is_li_relay=email_is_li_relay
        )
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
                "recruiterPhone": phone,
                "emailIsLinkedInRelay": email_is_li_relay,
                "ageDays": age_days,
                "folderPath": folder_abs,
                "applyUrl": apply_url,
                "threadUrl": thread,
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
                "directRecruiterOutreach": direct_recruiter,
                "matchPct": match_pct,
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
            pitched = label == "ready" and bool(human_inbound)
            if not asked and not lead.get("directRecruiter") and not pitched:
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
                contact_attempts=_contact_attempt_count(convs),
                asked=asked,
                apply_url=lead.get("applyUrl") or "",
                package_kind=label,
                folder_hint=lead.get("folderPath") or "",
                direct_recruiter=lead.get("directRecruiter"),
                match_pct=lead.get("matchPct"),
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
        dro_row = conn.execute(
            "SELECT direct_recruiter_outreach, llm_match_pct, match_pct FROM job_leads WHERE normalized_key = ?",
            (key,),
        ).fetchone()
        dro = None if dro_row is None or dro_row["direct_recruiter_outreach"] is None else bool(
            dro_row["direct_recruiter_outreach"]
        )
        match_pct = None
        if dro_row is not None:
            match_pct = dro_row["llm_match_pct"] if dro_row["llm_match_pct"] is not None else dro_row["match_pct"]
        _append_item(
            key=key,
            company=r["company"] or "",
            title=r["title"] or "",
            source_label=r["source_label"] or "",
            age_days=age_days_fn(r["first_seen"], now),
            contact_attempts=_contact_attempt_count(convs),
            asked=True,
            direct_recruiter=dro,
            match_pct=match_pct,
        )

    queue.sort(key=_priority_sort_key)
    return queue


def build_wait_schedule_queue(
    *,
    conn,
    exclude_keys: set[str],
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
          AND status NOT IN ('rejected', 'skipped', 'accepted', 'started',
                             'deleted', 'unavailable', 'hired')
        ORDER BY awaiting_response_since ASC
        """
    ):
        key = r["normalized_key"]
        if key in exclude_keys:
            continue
        convs = list(list_job_conversations(conn, key))
        human_inbound = _human_inbound_conversations(convs)
        waiting_days = age_days_fn(r["awaiting_response_since"], now)
        follow_up_due = waiting_days >= threshold
        recruiter, email, phone, email_is_li_relay = _recruiter_contact(conn, key)
        thread = _thread_url_for_lead(convs, conn, key)
        channel = _card_channel(
            thread_url=thread, source_label=r["source_label"] or "", email=email, email_is_li_relay=email_is_li_relay
        )
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
                "recruiterPhone": phone,
                "emailIsLinkedInRelay": email_is_li_relay,
                "ageDays": age_days_fn(r["first_seen"], now),
                "waitingDays": waiting_days,
                "awaitingSince": r["awaiting_response_since"],
                "status": r["status"],
                "contactAttempts": max(1, _contact_attempt_count(convs)),
                "followUpDue": follow_up_due,
                "followUpThresholdDays": threshold,
                "draftReply": draft,
                "threadUrl": thread,
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


# Every status that isn't closed (skipped/rejected/deleted/unavailable/hired)
# or already won (accepted/started).
STALE_ACTIVE_STATUSES = (
    "new",
    "pursued",
    "package_generated",
    "applied",
    "following_up",
    "interviewing",
    "offered",
    "reopened",
)
STALE_ACTIVE_DAYS = 30
PROBABLY_DEAD_DAYS = 45


def build_stale_active_queue(
    *,
    conn,
    exclude_keys: set[str],
    age_days_fn: Callable[[str | None, datetime], int],
    now: datetime,
    stale_days: int = STALE_ACTIVE_DAYS,
) -> list[dict]:
    """Leads still in an open status (STALE_ACTIVE_STATUSES) with no contact in
    `stale_days` and no wait clock running — otherwise they sit on no list at
    all. Rendered as Wait rows so the card can send a check-in or close it.
    Reopened leads with no wait clock are listed right away (no silence
    threshold) so Shawn picks their next step."""
    placeholders = ",".join("?" for _ in STALE_ACTIVE_STATUSES)
    rows = conn.execute(
        f"""
        SELECT l.normalized_key, l.company, l.title, l.source_label, l.first_seen, l.status,
               MAX(
                 COALESCE(l.last_seen, l.first_seen, ''),
                 COALESCE(l.pursued_at, ''),
                 COALESCE(l.package_generated_at, ''),
                 COALESCE(l.applied_at, ''),
                 COALESCE(l.following_up_at, ''),
                 COALESCE(l.interviewing_at, ''),
                 COALESCE(l.offered_at, ''),
                 COALESCE(l.reopened_at, ''),
                 COALESCE((SELECT MAX(c.occurred_at) FROM job_conversations c
                           WHERE c.job_key = l.normalized_key), '')
               ) AS last_comm
        FROM job_leads l
        WHERE l.deleted_at IS NULL
          AND l.awaiting_response_since IS NULL
          AND l.status IN ({placeholders})
        """,
        STALE_ACTIVE_STATUSES,
    ).fetchall()
    queue: list[dict] = []
    for r in rows:
        key = r["normalized_key"]
        if key in exclude_keys:
            continue
        silent_days = age_days_fn(r["last_comm"] or None, now)
        reopened = r["status"] == "reopened"
        if silent_days < stale_days and not reopened:
            continue
        convs = list(list_job_conversations(conn, key))
        recruiter, email, phone, email_is_li_relay = _recruiter_contact(conn, key)
        thread = _thread_url_for_lead(convs, conn, key)
        channel = _card_channel(
            thread_url=thread, source_label=r["source_label"] or "", email=email, email_is_li_relay=email_is_li_relay
        )
        status_text = (r["status"] or "").replace("_", " ")
        who = recruiter or "the recruiter"
        if reopened:
            next_action = (
                f"YOUR ACTION: Reopened ({silent_days}d since last contact) — Manage lead → "
                f"Follow up with {who}, record what happened (Applied, Interviewing…), or Decline"
            )
        else:
            next_action = (
                f"YOUR ACTION: Still marked {status_text}, but nothing in {silent_days}d — "
                f"Manage lead → Follow up to check in with {who}, or close it as Unavailable / Rejected"
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
            "recruiterPhone": phone,
            "emailIsLinkedInRelay": email_is_li_relay,
            "ageDays": age_days_fn(r["first_seen"], now),
            "waitingDays": silent_days,
            "awaitingSince": r["last_comm"] or "",
            "status": r["status"],
            "contactAttempts": max(1, _contact_attempt_count(convs)),
            "followUpDue": True,
            "followUpThresholdDays": stale_days,
            "staleActive": not reopened,
            "reopened": reopened,
            "draftReply": draft_wait_followup_message(
                recruiter_name=recruiter,
                company=r["company"] or "",
                title=r["title"] or "",
                waiting_days=silent_days,
            ),
            "threadUrl": thread,
            "gmailUrl": recruiting_gmail_message_url(_gmail_api_id_for_conversations(convs)),
            "markSentUrl": f"mps://mark?key={quote(key, safe='')}&channel={channel}",
            "actionHint": next_action,
            "nextAction": next_action,
        }
        if not item["gmailUrl"] or not item["threadUrl"]:
            enrich_item_reply_links(item, conn=conn)
        queue.append(item)
    queue.sort(key=lambda x: -int(x.get("waitingDays") or 0))
    return queue


TODAY_TIERS = {
    1: "Reply to recruiter",
    2: "Send package to recruiter",
    3: "Follow up",
    4: "Apply",
    5: "Decide",
    6: "Waiting on them",
    8: "Decline — dealbreaker in the pitch",
    9: "Probably dead — check in or close",
}

_LIVE_STATUS_WEIGHT = {"offered": 0, "interviewing": 0, "following_up": 1, "reopened": 1}


def _pct(item: dict) -> float:
    try:
        return float(item.get("matchPct") or 0)
    except (TypeError, ValueError):
        return 0.0


def annotate_today_order(
    *,
    clarify: list[dict],
    send_resume: list[dict],
    wait_schedule: list[dict],
    decide_apply: dict[str, list[dict]],
    dead_days: int = PROBABLY_DEAD_DAYS,
) -> None:
    """Stamp `todayTier` / `todayLabel` / `todayRank` on every active item so
    the UI's single list reads as "what to do next, in order" across stages.

    Tiers: recruiter waiting on a reply → package ready for a recruiter →
    follow-ups on live threads → apply (best fit first) → decide → nothing to
    do yet → probably dead (silent `dead_days`+ or stale active status).
    """
    ranked: list[tuple[tuple, dict]] = []

    def _add(item: dict, tier: int, sub: tuple) -> None:
        item["todayTier"] = tier
        item["todayLabel"] = TODAY_TIERS[tier]
        ranked.append(((tier, *sub), item))

    for item in clarify:
        if item.get("dealbreaker"):
            _add(item, 8, (-int(item.get("ageDays") or 0),))
            continue
        _add(item, 1, (0 if item.get("replyDue") else 1, -int(item.get("unansweredDays") or 0)))
    for item in send_resume:
        if item.get("resumeRequested"):
            _add(item, 1, (0, -int(item.get("ageDays") or 0)))
        else:
            _add(item, 2, (-_pct(item), int(item.get("ageDays") or 0)))
    for item in wait_schedule:
        waiting = int(item.get("waitingDays") or 0)
        if item.get("reopened"):
            _add(item, 3, (_LIVE_STATUS_WEIGHT["reopened"], waiting))
        elif item.get("staleActive") or (item.get("followUpDue") and waiting >= dead_days):
            _add(item, 9, (waiting,))
        elif item.get("followUpDue"):
            _add(item, 3, (_LIVE_STATUS_WEIGHT.get(item.get("status") or "", 2), waiting))
        else:
            _add(item, 6, (waiting,))
    for bucket, rows in decide_apply.items():
        for item in rows:
            if bucket == "readyToApply":
                _add(item, 4, (-_pct(item), int(item.get("ageDays") or 0)))
            else:
                _add(item, 5, (-_pct(item), int(item.get("ageDays") or 0)))

    ranked.sort(key=lambda pair: pair[0])
    for rank, (_, item) in enumerate(ranked):
        item["todayRank"] = rank


def build_decide_apply_stage(
    *,
    ready_to_apply: list[dict],
    needs_decision: list[dict],
    needs_decision_forced: list[dict],
    awaiting_llm_review: list[dict],
    jd_unresolved: list[dict],
    exclude_keys: set[str],
    conn=None,
) -> dict[str, list[dict]]:
    """Priority-B funnel buckets (exclude leads already in Clarify, Send résumé or Wait)."""

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
            if key and key in exclude_keys:
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


_ARCHIVED_STATUSES = (
    "skipped",
    "rejected",
    "deleted",
    "unavailable",
    "hired",
    # Applied-or-beyond (store._APPLIED_OR_BEYOND_STATUSES) — these leads
    # are equally absent from every decide_apply/clarify/send_resume/
    # wait_schedule bucket above (render_pending_actions.build_workflow
    # buckets them into manual_status/manual_handled instead, a
    # company+status *count* only, with no normalizedKey to link to). Added
    # 2026-08-17 after "Go to this lead" (a duplicate's survivor link) hit a
    # dead end for a lead that had gone on to status="applied" — it wasn't
    # in the active funnel, but wasn't here either, so there was nowhere in
    # the whole payload a UI element could point at it.
    "applied",
    "following_up",
    "interviewing",
    "offered",
    "accepted",
    "started",
)
# Per-status timestamp column stamped by pipeline/store.py's advance_status —
# COALESCE picks whichever one is actually set for this lead's status.
_ARCHIVED_AT_COLUMNS = (
    "skipped_at, rejected_at, deleted_at, unavailable_at, hired_at, "
    "applied_at, following_up_at, interviewing_at, offered_at, accepted_at, started_at"
)


def _list_archived_leads(conn, *, age_days_fn: Callable[[str | None, datetime], int], now: datetime) -> list[dict]:
    """Leads off the active clarify/send_resume/wait_schedule/decide_apply
    funnel — either a decision already made (skipped/rejected/deleted/
    unavailable/hired) or already past the apply gate (applied/
    following_up/interviewing/offered/accepted/started) — still worth a
    "what did I decide/submit, and what was said" lookup, e.g. via **View
    communications**, and still a valid "Go to this lead" destination for
    duplicate-of navigation. Sorted most-recently-decided first. Added
    2026-08-17 after a request to browse a skipped lead's message history
    from the UI rather than dropping to the CLI; broadened the same day to
    include applied-or-beyond statuses once a duplicate's survivor link hit
    a dead end for a since-applied lead."""
    placeholders = ",".join("?" for _ in _ARCHIVED_STATUSES)
    rows = conn.execute(
        f"""
        SELECT normalized_key, company, title, status, match_pct, llm_verdict,
               first_seen, duplicate_of_key,
               COALESCE({_ARCHIVED_AT_COLUMNS}, first_seen) AS decided_at
        FROM job_leads
        WHERE status IN ({placeholders})
        """,
        _ARCHIVED_STATUSES,
    ).fetchall()
    # duplicate_of_key can point at *any* lead (active or archived) — a
    # second lookup by company/title rather than assuming the survivor is
    # itself in this same archived batch.
    survivor_keys = {r["duplicate_of_key"] for r in rows if r["duplicate_of_key"]}
    survivors: dict[str, dict] = {}
    if survivor_keys:
        ph = ",".join("?" for _ in survivor_keys)
        for s in conn.execute(
            f"SELECT normalized_key, company, title FROM job_leads WHERE normalized_key IN ({ph})",
            tuple(survivor_keys),
        ):
            survivors[s["normalized_key"]] = {"company": s["company"] or "", "title": s["title"] or ""}

    # Earliest-contacted recruiter per lead (2026-08-19, for the "search
    # company or title" box's expanded scope) — one batched query rather
    # than N+1 per-lead lookups, since this list can run to 1000+ rows.
    # `setdefault` keeps only the first row per job_key, which is the
    # CON-6 recruiter given `RECRUITER_ORDER_SQL`.
    lead_keys = [r["normalized_key"] for r in rows]
    contacts: dict[str, dict] = {}
    if lead_keys:
        ph = ",".join("?" for _ in lead_keys)
        for c in conn.execute(
            f"""
            SELECT job_key, name, email, phone
            FROM job_contacts
            WHERE job_key IN ({ph}) AND COALESCE(role, '') != 'company'
            ORDER BY job_key, {RECRUITER_ORDER_SQL}
            """,
            tuple(lead_keys),
        ):
            contacts.setdefault(
                c["job_key"],
                {"name": c["name"] or "", "email": c["email"] or "", "phone": c["phone"] or ""},
            )

    out: list[dict] = []
    for r in rows:
        key = r["normalized_key"]
        comm_count = len(list_job_conversations(conn, key))
        item = {
            "normalizedKey": key,
            "company": r["company"] or "",
            "title": r["title"] or "",
            "status": r["status"] or "",
            "matchPct": round(r["match_pct"] or 0.0, 1),
            "verdict": r["llm_verdict"] or "",
            "decidedAt": r["decided_at"] or "",
            "ageDays": age_days_fn(r["first_seen"], now),
            "commCount": comm_count,
        }
        contact = contacts.get(key)
        if contact:
            if contact["name"]:
                item["recruiterName"] = contact["name"]
            if contact["email"]:
                item["recruiterEmail"] = contact["email"]
            if contact["phone"]:
                item["recruiterPhone"] = contact["phone"]
        dup_key = r["duplicate_of_key"]
        if dup_key:
            item["duplicateOfKey"] = dup_key
            survivor = survivors.get(dup_key)
            if survivor:
                item["duplicateOfCompany"] = survivor["company"]
                item["duplicateOfTitle"] = survivor["title"]
        out.append(item)
    out.sort(key=lambda l: l["decidedAt"], reverse=True)
    return out


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

    # CARD-1 stage precedence: Clarify > Send résumé > Wait > Decide/apply.
    wait_schedule = build_wait_schedule_queue(
        conn=conn,
        exclude_keys=clarify_keys | send_keys,
        age_days_fn=age_days_fn,
        now=now,
    )
    wait_schedule.extend(
        build_stale_active_queue(
            conn=conn,
            exclude_keys=clarify_keys
            | send_keys
            | {i["normalizedKey"] for i in wait_schedule}
            | {
                i.get("normalizedKey")
                for bucket in (
                    "ready_to_apply",
                    "needs_decision",
                    "needs_decision_forced",
                    "awaiting_llm_review",
                    "jd_unresolved",
                )
                for i in data.get(bucket) or []
            },
            age_days_fn=age_days_fn,
            now=now,
        )
    )
    wait_keys = {i["normalizedKey"] for i in wait_schedule if i.get("normalizedKey")}

    decide_apply = build_decide_apply_stage(
        ready_to_apply=data.get("ready_to_apply") or [],
        needs_decision=data.get("needs_decision") or [],
        needs_decision_forced=data.get("needs_decision_forced") or [],
        awaiting_llm_review=data.get("awaiting_llm_review") or [],
        jd_unresolved=data.get("jd_unresolved") or [],
        exclude_keys=clarify_keys | send_keys | wait_keys,
        conn=conn,
    )

    from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT

    root = Path(output_root) if output_root else DEFAULT_OUTPUT_ROOT
    dup_keys = _duplicate_keys(conn)
    for item in clarify:
        _attach_folder_links(item, conn=conn, output_root=root)
        _attach_duplicate_count(item, dup_keys)
    for item in send_resume:
        _attach_folder_links(item, conn=conn, output_root=root)
        _attach_duplicate_count(item, dup_keys)
    for item in wait_schedule:
        _attach_folder_links(item, conn=conn, output_root=root)
        _attach_duplicate_count(item, dup_keys)
    for bucket in decide_apply.values():
        for item in bucket:
            _attach_folder_links(item, conn=conn, output_root=root)
            _attach_duplicate_count(item, dup_keys)

    status_items = [*clarify, *send_resume, *wait_schedule]
    for bucket in decide_apply.values():
        status_items.extend(bucket)
    _attach_lead_statuses(status_items, conn)
    from job_tracker.pipeline.decline import DECLINE_CLOSING, DECLINE_REASONS, decline_greeting

    for item in status_items:
        if item.get("normalizedKey") and not item.get("postDecline"):
            item["declineGreeting"] = decline_greeting(
                recruiter_name=item.get("recruiterName") or "",
                company=item.get("company") or "",
                title=item.get("title") or "",
            )
            item.update(_send_route_fields(conn, item["normalizedKey"]))
            item["followupDraft"] = draft_wait_followup_message(
                recruiter_name=item.get("recruiterName") or "",
                company=item.get("company") or "",
                title=item.get("title") or "",
                waiting_days=int(item.get("waitingDays") or 0),
            )
    from job_tracker.pipeline.reply_plan import reply_plan

    for item in status_items:
        item["replyPlan"] = reply_plan(item)
        _attach_senders(item, conn, reply_plan)
        if (
            item.get("stage") == "clarify"
            and not item.get("postDecline")
            and str(item.get("nextAction") or "").startswith("YOUR ACTION:")
        ):
            item["nextAction"] = _next_action_clarify(
                channel=item["replyPlan"]["channel"],
                recruiter_name=item.get("recruiterName") or "",
                reply_due=bool(item.get("replyDue")),
                unanswered_days=int(item.get("unansweredDays") or 0),
                screen_first=bool(item.get("screenFirst")),
                routes=item["replyPlan"]["routes"],
                dealbreaker=item.get("dealbreaker") or "",
            )
    annotate_today_order(
        clarify=clarify,
        send_resume=send_resume,
        wait_schedule=wait_schedule,
        decide_apply=decide_apply,
    )

    archived_leads = _list_archived_leads(conn, age_days_fn=age_days_fn, now=now)
    for item in archived_leads:
        _attach_folder_links(item, conn=conn, output_root=root)
        _attach_duplicate_count(item, dup_keys)
        item["replyPlan"] = reply_plan(item)

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
        "declineReasons": [
            {"code": r.code, "label": r.label, "sentence": r.sentence} for r in DECLINE_REASONS
        ],
        "declineClosing": DECLINE_CLOSING,
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
        "archivedLeads": archived_leads,
    }
