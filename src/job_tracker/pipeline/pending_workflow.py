"""Stage-based pending-actions workflow (Clarify → Send résumé → Wait → Decide/apply).

Channel (LinkedIn vs email) is a badge only — priority is recruiter contact
attempts, then age. Used by `scripts/render_pending_actions.py` to emit
`var/pending-actions.json` for the React UI.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

from job_tracker.pipeline.qualifying_reply import detect_pitch_gaps, draft_qualifying_reply
from job_tracker.pipeline.store import list_job_conversations

_OUTREACH_MAX_AGE_DAYS = 14

_RESUME_ASK_RE = re.compile(
    r"(?i)\b("
    r"send (me )?(your |a )?(resume|résumé|cv)|"
    r"(resume|résumé|cv) (please|attached|asap)|"
    r"please (share|send|attach).{0,40}(resume|résumé|cv)|"
    r"looking forward to (receiving |seeing )?(your )?(resume|résumé|cv)"
    r")\b"
)

_NO_REPLY_RE = re.compile(
    r"(?i)(no[\-]?reply|donotreply|do[\-]?not[\-]?reply|notifications?@|jobalerts?@|jobs\-noreply@)"
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
    return (-int(item.get("contactAttempts") or 0), -int(item.get("ageDays") or 0), (item.get("company") or "").lower())


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
        queue.append(item)

    for item in linkedin_reply_queue:
        key = item.get("normalizedKey") or ""
        attempts = int(item.get("contactAttempts") or 0)
        if key and attempts < 1:
            convs = list(list_job_conversations(conn, key))
            attempts = sum(1 for c in convs if (c["direction"] or "") == "inbound")
        _add(
            {
                **item,
                "stage": "clarify",
                "channel": _channel_from_addresses(
                    thread_url=item.get("threadUrl") or "",
                    source_label="linkedin_message" if item.get("kind") == "lead" else "",
                    from_address="",
                ),
                "contactAttempts": max(1, attempts),
                "actionHint": "Copy reply → open thread → send → dismiss",
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
        _add(
            {
                "kind": "unmatched",
                "stage": "clarify",
                "channel": _channel_from_addresses(from_address=fr, thread_url=thread),
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
                "actionHint": "Copy reply → reply in Gmail → dismiss when sent",
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
        inbound = [c for c in convs if (c["direction"] or "") == "inbound"]
        if not inbound:
            continue
        latest = inbound[-1]
        body = latest["body_text"] or ""
        subj = latest["summary"] or r["title"] or ""
        draft = draft_qualifying_reply(body, subject=subj)
        if not draft.thread_url and not draft.recruiter_name:
            # Still allow email leads when the from/summary implies a person.
            if not body.strip():
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
                "actionHint": (
                    "Copy reply → open LinkedIn thread → send → dismiss"
                    if channel == "linkedin"
                    else "Copy reply → reply in Gmail → dismiss when sent"
                ),
            }
        )

    queue.sort(key=_priority_sort_key)
    return queue


def build_send_resume_queue(
    *,
    ready_to_apply: list[dict],
    needs_decision_forced: list[dict],
    clarify_keys: set[str],
    conn,
    age_days_fn: Callable[[str | None, datetime], int],
    now: datetime,
) -> list[dict]:
    """Priority-A′: recruiter asked for a résumé / package ready to send to a person."""
    queue: list[dict] = []
    seen: set[str] = set()

    # Packages on disk for leads with real recruiter conversation history.
    for bucket, label in (
        (ready_to_apply, "ready"),
        (needs_decision_forced, "forced"),
    ):
        for lead in bucket:
            key = lead.get("normalizedKey") or ""
            if not key or key in clarify_keys or key in seen:
                continue
            convs = list(list_job_conversations(conn, key))
            inbound = [c for c in convs if (c["direction"] or "") == "inbound"]
            if not inbound and not lead.get("directRecruiter"):
                continue
            asked = any(_RESUME_ASK_RE.search((c["body_text"] or "") + " " + (c["summary"] or "")) for c in inbound)
            # Direct recruiter + package ready counts even without explicit "send resume"
            # phrasing — they already asked you into a thread.
            if not asked and not lead.get("directRecruiter") and len(inbound) < 1:
                continue
            # If we're still waiting on them after an outbound, prefer wait stage.
            row = conn.execute(
                "SELECT awaiting_response_since, source_label FROM job_leads WHERE normalized_key = ?",
                (key,),
            ).fetchone()
            if row and row["awaiting_response_since"]:
                # Outbound already logged — only keep here if latest inbound asked for résumé
                # after our last outbound (they replied asking for the doc).
                if not asked:
                    continue
            seen.add(key)
            queue.append(
                {
                    "kind": "lead",
                    "stage": "send_resume",
                    "channel": _channel_from_addresses(source_label=(row["source_label"] if row else "") or ""),
                    "company": lead.get("company") or "",
                    "title": lead.get("title") or "",
                    "normalizedKey": key,
                    "ageDays": lead.get("ageDays") or 0,
                    "matchPct": lead.get("matchPct"),
                    "folderPath": lead.get("folderPath") or "",
                    "companyFolderPath": lead.get("companyFolderPath") or "",
                    "applyUrl": lead.get("applyUrl") or "",
                    "directRecruiter": lead.get("directRecruiter"),
                    "contactAttempts": len(inbound),
                    "resumeRequested": asked,
                    "packageReady": True,
                    "packageKind": label,
                    "actionHint": "Open package folder → send résumé to recruiter → mark sent / await schedule",
                }
            )

    # Inbound "please send resume" on new leads that already have an outbound clarify.
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
        convs = list(list_job_conversations(conn, key))
        inbound = [c for c in convs if (c["direction"] or "") == "inbound"]
        outbound = [c for c in convs if (c["direction"] or "") == "outbound"]
        if not inbound or not outbound:
            continue
        asked = any(_RESUME_ASK_RE.search((c["body_text"] or "") + " " + (c["summary"] or "")) for c in inbound)
        if not asked:
            continue
        seen.add(key)
        queue.append(
            {
                "kind": "lead",
                "stage": "send_resume",
                "channel": _channel_from_addresses(source_label=r["source_label"] or ""),
                "company": r["company"] or "",
                "title": r["title"] or "",
                "normalizedKey": key,
                "ageDays": age_days_fn(r["first_seen"], now),
                "contactAttempts": len(inbound),
                "resumeRequested": True,
                "packageReady": False,
                "actionHint": "Generate or open package → send résumé → mark sent",
            }
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
    """After outbound clarify/résumé — awaiting recruiter reply or call scheduling."""
    queue: list[dict] = []
    for r in conn.execute(
        """
        SELECT normalized_key, company, title, source_label, first_seen,
               awaiting_response_since, status
        FROM job_leads
        WHERE deleted_at IS NULL
          AND awaiting_response_since IS NOT NULL
          AND status NOT IN ('rejected', 'skipped', 'withdrawn', 'accepted', 'started')
        ORDER BY awaiting_response_since ASC
        """
    ):
        key = r["normalized_key"]
        if key in clarify_keys or key in send_keys:
            continue
        convs = list(list_job_conversations(conn, key))
        inbound = [c for c in convs if (c["direction"] or "") == "inbound"]
        waiting_days = age_days_fn(r["awaiting_response_since"], now)
        queue.append(
            {
                "kind": "lead",
                "stage": "wait_schedule",
                "channel": _channel_from_addresses(source_label=r["source_label"] or ""),
                "company": r["company"] or "",
                "title": r["title"] or "",
                "normalizedKey": key,
                "ageDays": age_days_fn(r["first_seen"], now),
                "waitingDays": waiting_days,
                "awaitingSince": r["awaiting_response_since"],
                "status": r["status"],
                "contactAttempts": len(inbound),
                "actionHint": "No action unless scheduling a call — waiting on recruiter",
            }
        )
    queue.sort(key=lambda x: (-int(x.get("contactAttempts") or 0), -int(x.get("waitingDays") or 0)))
    return queue


def build_decide_apply_stage(
    *,
    ready_to_apply: list[dict],
    needs_decision: list[dict],
    needs_decision_forced: list[dict],
    awaiting_llm_review: list[dict],
    jd_unresolved: list[dict],
    send_keys: set[str],
) -> dict[str, list[dict]]:
    """Priority-B funnel buckets (exclude leads already in send-résumé)."""

    def _filter(rows: list[dict]) -> list[dict]:
        out = []
        for lead in rows:
            key = lead.get("normalizedKey") or ""
            if key and key in send_keys:
                continue
            out.append({**lead, "stage": "decide_apply"})
        return out

    return {
        "readyToApply": _filter(ready_to_apply),
        "needsDecision": _filter(needs_decision),
        "needsDecisionForced": _filter(needs_decision_forced),
        "awaitingLlmReview": _filter(awaiting_llm_review),
        "jdUnresolved": _filter(jd_unresolved),
    }


def build_workflow_payload(data: dict, *, conn, age_days_fn: Callable[[str | None, datetime], int], now: datetime) -> dict[str, Any]:
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
