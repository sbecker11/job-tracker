"""One lead card at a time.

A full Pending Actions render searches Gmail for every card that is missing
a link. That takes about forty seconds. A manual request for one lead builds
only that card, then writes the page again with that card first and every
other card left as it was.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from job_tracker.pipeline.decline import decline_greeting
from job_tracker.pipeline.pending_workflow import (
    TODAY_TIERS,
    _attach_folder_links,
    _attach_lead_statuses,
    _attach_senders,
    _card_channel,
    _contact_attempt_count,
    _gmail_url_for_lead,
    _human_inbound_conversations,
    _next_action_clarify,
    _recruiter_contact,
    _send_route_fields,
    _sort_time,
    _thread_url_for_lead,
    _unanswered_days,
    _unanswered_inbound,
    clean_message_body_for_display,
    draft_recruiter_followup_ack,
    draft_wait_followup_message,
)
from job_tracker.pipeline.qualifying_reply import draft_qualifying_reply
from job_tracker.pipeline.reply_intent import draft_live_thread_reply
from job_tracker.pipeline.reply_plan import reply_plan
from job_tracker.pipeline.store import get_job, list_job_conversations

_STAGE_KEYS = ("clarify", "sendResume", "waitSchedule")


def _age_days(first_seen: str | None, now: datetime) -> int:
    if not first_seen:
        return 0
    try:
        seen = datetime.fromisoformat(first_seen)
    except ValueError:
        return 0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=now.tzinfo)
    return max(0, (now - seen).days)


def iter_active_cards(payload: dict) -> Iterator[dict]:
    stages = payload.get("stages") or {}
    for key in _STAGE_KEYS:
        yield from stages.get(key) or []
    decide = stages.get("decideApply") or {}
    if isinstance(decide, dict):
        for bucket in decide.values():
            if isinstance(bucket, list):
                yield from bucket


def _drop_key(items: list[dict], job_key: str) -> list[dict]:
    return [item for item in items if (item.get("normalizedKey") or "") != job_key]


def place_card_first(payload: dict, card: dict) -> dict:
    """Put `card` at the top. Every other card keeps its cached body."""
    job_key = card.get("normalizedKey") or ""
    stages = payload.setdefault("stages", {})
    for key in _STAGE_KEYS:
        stages[key] = _drop_key(list(stages.get(key) or []), job_key)
    decide = stages.get("decideApply") or {}
    if isinstance(decide, dict):
        for bucket, rows in list(decide.items()):
            if isinstance(rows, list):
                decide[bucket] = _drop_key(rows, job_key)
        stages["decideApply"] = decide

    stage = card.get("stage") or "clarify"
    if stage == "send_resume":
        stages.setdefault("sendResume", []).insert(0, card)
    elif stage == "wait_schedule":
        stages.setdefault("waitSchedule", []).insert(0, card)
    else:
        stages.setdefault("clarify", []).insert(0, card)

    others = [item for item in iter_active_cards(payload) if item is not card and item.get("todayRank") is not None]
    others.sort(key=lambda item: int(item.get("todayRank") or 0))
    card["todayRank"] = 0
    card["todayTier"] = 1
    card["todayLabel"] = TODAY_TIERS[1]
    for rank, item in enumerate(others, start=1):
        item["todayRank"] = rank

    counts = {
        "clarify": len(stages.get("clarify") or []),
        "send_resume": len(stages.get("sendResume") or []),
        "wait_schedule": len(stages.get("waitSchedule") or []),
        "decide_apply": sum(len(v) for v in (stages.get("decideApply") or {}).values() if isinstance(v, list)),
    }
    for stage_row in payload.get("pipeline") or []:
        if stage_row.get("id") in counts:
            stage_row["count"] = counts[stage_row["id"]]
    return payload


def build_one_lead_card(conn, job_key: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The clarify card for one lead. Does not search Gmail."""
    now = now or datetime.now().astimezone()
    row = conn.execute(
        "SELECT normalized_key, company, title, source_label, first_seen, status FROM job_leads WHERE normalized_key = ?",
        (job_key,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no lead {job_key}")
    convs = list(list_job_conversations(conn, job_key))
    unanswered = _unanswered_inbound(convs)
    latest = unanswered or (convs[-1] if convs else None)
    recruiter, email, phone, email_is_li_relay = _recruiter_contact(conn, job_key)
    body = (latest["body_text"] if latest else "") or ""
    subj = (latest["summary"] if latest else "") or row["title"] or ""
    human_in = sorted(_human_inbound_conversations(convs), key=lambda c: _sort_time(c["occurred_at"]))
    pitch = (human_in[0]["body_text"] or "") if human_in else body
    later = "\n".join(c["body_text"] or "" for c in human_in[1:])
    drafted = draft_qualifying_reply(
        pitch,
        subject=subj,
        known_recruiter_name=recruiter,
        received_at=(latest["occurred_at"] if latest else "") or "",
        thread_replies=later,
        followup=bool(unanswered and any(c["direction"] == "outbound" for c in convs)),
    )
    gaps = drafted.gaps
    still_needs = any(
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
        if still_needs and drafted.body.strip()
        else draft_recruiter_followup_ack(
            recruiter_name=recruiter or drafted.recruiter_name,
            company=row["company"] or "",
            title=row["title"] or "",
            inbound_snippet=subj,
        )
    )
    thread = drafted.thread_url or _thread_url_for_lead(convs, conn, job_key)
    channel = _card_channel(
        thread_url=thread,
        source_label=row["source_label"] or "",
        email=email,
        email_is_li_relay=email_is_li_relay,
    )
    unanswered_days = _unanswered_days(convs, _age_days, now) if unanswered else 0
    message_id = (latest["message_id"] if latest else "") or ""
    gmail_url = _gmail_url_for_lead(conn, job_key, message_id)
    item: dict[str, Any] = {
        "kind": "lead",
        "stage": "clarify",
        "channel": channel,
        "recruiterName": recruiter or drafted.recruiter_name,
        "recruiterEmail": email,
        "recruiterPhone": phone,
        "emailIsLinkedInRelay": email_is_li_relay,
        "subject": subj,
        "company": row["company"] or drafted.company_guess,
        "title": row["title"] or drafted.title_guess,
        "threadUrl": thread,
        "gmailUrl": gmail_url,
        "draftReply": draft_body,
        "messageBody": clean_message_body_for_display(body),
        "ageDays": _age_days(row["first_seen"], now),
        "messageId": message_id,
        "normalizedKey": job_key,
        "replyId": f"reply-due:{job_key}",
        "contactAttempts": _contact_attempt_count(convs),
        "replyDue": unanswered is not None,
        "unansweredDays": unanswered_days,
        "status": row["status"] or "",
    }
    from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT

    _attach_folder_links(item, conn=conn, output_root=DEFAULT_OUTPUT_ROOT)
    _attach_lead_statuses([item], conn)
    item["declineGreeting"] = decline_greeting(
        recruiter_name=item.get("recruiterName") or "",
        company=item.get("company") or "",
        title=item.get("title") or "",
    )
    item.update(_send_route_fields(conn, job_key))
    item["followupDraft"] = draft_wait_followup_message(
        recruiter_name=item.get("recruiterName") or "",
        company=item.get("company") or "",
        title=item.get("title") or "",
        waiting_days=int(item.get("waitingDays") or 0),
    )
    item["replyPlan"] = reply_plan(item)
    _attach_senders(item, conn, reply_plan)
    next_action = _next_action_clarify(
        channel=item["replyPlan"]["channel"],
        recruiter_name=item.get("recruiterName") or "",
        reply_due=bool(item.get("replyDue")),
        unanswered_days=int(item.get("unansweredDays") or 0),
        thread_url=item.get("threadUrl") or "",
        gmail_url=item.get("gmailUrl") or "",
        routes=item["replyPlan"]["routes"],
    )
    item["nextAction"] = next_action
    item["actionHint"] = next_action
    from job_tracker.pipeline.llm_reply import apply_llm_drafts

    apply_llm_drafts([item], conn)
    return item


def refresh_one_lead(
    conn,
    job_key: str,
    *,
    payload_path: Path,
    ui_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Generate one card and rewrite the page with that card first."""
    now = now or datetime.now().astimezone()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    card = build_one_lead_card(conn, job_key, now=now)
    cached = next(
        (item for item in iter_active_cards(payload) if (item.get("normalizedKey") or "") == job_key),
        None,
    )
    if cached:
        filled = False
        for field in ("gmailUrl", "threadUrl", "folderPath", "companyFolderPath"):
            if not card.get(field) and cached.get(field):
                card[field] = cached[field]
                filled = field in ("gmailUrl", "threadUrl") or filled
        if filled:
            card["replyPlan"] = reply_plan(card)
            card["nextAction"] = _next_action_clarify(
                channel=card["replyPlan"]["channel"],
                recruiter_name=card.get("recruiterName") or "",
                reply_due=bool(card.get("replyDue")),
                unanswered_days=int(card.get("unansweredDays") or 0),
                routes=card["replyPlan"]["routes"],
            )
            card["actionHint"] = card["nextAction"]
    place_card_first(payload, card)
    payload["generatedAt"] = now.isoformat()
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    payload_path.write_text(text, encoding="utf-8")
    if ui_path is not None:
        ui_path.parent.mkdir(parents=True, exist_ok=True)
        ui_path.write_text(text, encoding="utf-8")
    return card


def refresh_named_lead(
    conn,
    *,
    company: str,
    title: str,
    payload_path: Path,
    ui_path: Path | None = None,
) -> dict[str, Any]:
    job = get_job(conn, company, title)
    if job is None:
        raise ValueError(f"no lead for {title!r} @ {company!r}")
    return refresh_one_lead(conn, job["normalized_key"], payload_path=payload_path, ui_path=ui_path)
