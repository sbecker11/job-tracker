"""Update one lead's card in pending-actions.json after Apply, Reply sent or decline.

A full render takes about forty seconds because it rebuilds every card. A
status change or a logged reply only moves one lead, so this drops that
lead's card, rebuilds it for the stage it belongs in now (or leaves it out
when the lead is closed), and re-ranks the list with the same rules the full
render uses. Anything it can't place (Send résumé, Decide/apply) falls back
to a full render.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from job_tracker.pipeline.lead_card_cache import _age_days, build_one_lead_card, iter_active_cards
from job_tracker.pipeline.pending_workflow import (
    _attach_duplicate_count,
    _attach_folder_links,
    _attach_lead_statuses,
    _attach_senders,
    _duplicate_keys,
    _send_route_fields,
    _unanswered_inbound,
    annotate_today_order,
    build_wait_item,
    draft_wait_followup_message,
)
from job_tracker.pipeline.store import DEFAULT_DB_PATH, list_job_conversations

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_JSON = REPO_ROOT / "var" / "pending-actions.json"
DEFAULT_UI_JSON = REPO_ROOT / "pending-actions-ui" / "public" / "pending-actions.json"
CLOSED_STATUSES = frozenset({"skipped", "rejected", "deleted", "unavailable", "hired", "accepted", "started"})

_STAGE_LISTS = ("clarify", "sendResume", "waitSchedule")


class NeedsFullRender(Exception):
    """The lead now belongs in a stage only the full render can build."""


def _without(rows: list[dict], job_key: str, message_id: str) -> list[dict]:
    return [
        row
        for row in rows
        if (row.get("normalizedKey") or "") != job_key
        and not (message_id and (row.get("messageId") or "") == message_id)
    ]


def _drop_cards(payload: dict, job_key: str, message_id: str = "") -> None:
    stages = payload.setdefault("stages", {})
    for key in _STAGE_LISTS:
        stages[key] = _without(list(stages.get(key) or []), job_key, message_id)
    decide = stages.get("decideApply") or {}
    if isinstance(decide, dict):
        stages["decideApply"] = {
            bucket: _without(rows, job_key, message_id) if isinstance(rows, list) else rows
            for bucket, rows in decide.items()
        }
    if message_id:
        payload["unmatchedCommunications"] = [
            row
            for row in payload.get("unmatchedCommunications") or []
            if (row.get("messageId") or row.get("message_id") or "") != message_id
        ]


def _finish(item: dict, conn) -> dict:
    """The per-card extras build_workflow_payload adds to every active card."""
    from job_tracker.pipeline.decline import decline_greeting
    from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT
    from job_tracker.pipeline.reply_plan import reply_plan

    _attach_folder_links(item, conn=conn, output_root=DEFAULT_OUTPUT_ROOT)
    _attach_duplicate_count(item, _duplicate_keys(conn))
    _attach_lead_statuses([item], conn)
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
    item["replyPlan"] = reply_plan(item)
    _attach_senders(item, conn, reply_plan)
    if item.get("stage") == "wait_schedule":
        from job_tracker.pipeline.llm_reply import apply_llm_drafts

        apply_llm_drafts([item], conn)
    return item


def _rerank(payload: dict) -> None:
    stages = payload.get("stages") or {}
    decide = stages.get("decideApply") or {}
    annotate_today_order(
        clarify=stages.get("clarify") or [],
        send_resume=stages.get("sendResume") or [],
        wait_schedule=stages.get("waitSchedule") or [],
        decide_apply={k: v for k, v in decide.items() if isinstance(v, list)} if isinstance(decide, dict) else {},
    )
    counts = {
        "clarify": len(stages.get("clarify") or []),
        "send_resume": len(stages.get("sendResume") or []),
        "wait_schedule": len(stages.get("waitSchedule") or []),
        "decide_apply": sum(len(v) for v in decide.values() if isinstance(v, list)) if isinstance(decide, dict) else 0,
    }
    for row in payload.get("pipeline") or []:
        if row.get("id") in counts:
            row["count"] = counts[row["id"]]


def patch_lead_card(
    conn,
    job_key: str,
    *,
    payload: dict,
    message_id: str = "",
    now: datetime | None = None,
) -> str:
    """Move one lead's card in `payload`. Returns "removed", "clarify" or "wait".

    Raises NeedsFullRender when the lead belongs in Send résumé or Decide/apply.
    """
    now = now or datetime.now().astimezone()
    row = conn.execute(
        "SELECT status, deleted_at, awaiting_response_since FROM job_leads WHERE normalized_key = ?",
        (job_key,),
    ).fetchone()
    if row is not None and not row["deleted_at"] and (row["status"] or "") not in CLOSED_STATUSES:
        cached = next(
            (c for c in iter_active_cards(payload) if (c.get("normalizedKey") or "") == job_key),
            None,
        )
        if cached and cached.get("stage") not in (None, "clarify", "wait_schedule"):
            raise NeedsFullRender(f"{job_key} is in {cached.get('stage')}")
    _drop_cards(payload, job_key, message_id)
    result = "removed"
    if row is not None and not row["deleted_at"] and (row["status"] or "") not in CLOSED_STATUSES:
        convs = list(list_job_conversations(conn, job_key))
        if _unanswered_inbound(convs) is not None:
            card = _finish(build_one_lead_card(conn, job_key, now=now), conn)
            payload["stages"].setdefault("clarify", []).append(card)
            result = "clarify"
        else:
            card = build_wait_item(conn, job_key, age_days_fn=_age_days, now=now)
            if card is None:
                raise NeedsFullRender(f"{job_key} is neither waiting nor owed a reply")
            payload["stages"].setdefault("waitSchedule", []).append(_finish(card, conn))
            result = "wait"
    _rerank(payload)
    payload["generatedAt"] = now.isoformat()
    return result


def write_payload(payload: dict, *, json_path: Path = DEFAULT_JSON, ui_path: Path | None = DEFAULT_UI_JSON) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    for path in (json_path, ui_path):
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)


def spawn_card_refresh(db_path: Path, job_key: str, *, message_id: str = "") -> None:
    """Start scripts/refresh_lead_card.py in the background for the real leads DB.

    Detached so the calling CLI returns at once; the script waits for the DB lock,
    so it is safe to call from inside a comms_fast cycle that already holds it.
    Test databases are left alone.
    """
    try:
        if Path(db_path).resolve() != DEFAULT_DB_PATH.resolve():
            return
    except OSError:
        return
    args = [sys.executable, str(REPO_ROOT / "scripts" / "refresh_lead_card.py"), "--key", job_key]
    if message_id:
        args += ["--message-id", message_id]
    log = REPO_ROOT / "var" / "refresh_lead_card.log"
    try:
        with open(log, "a", encoding="utf-8") as out:
            subprocess.Popen(
                args,
                cwd=REPO_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError:
        pass


def refresh(conn, job_key: str, *, message_id: str = "", json_path: Path = DEFAULT_JSON,
            ui_path: Path | None = DEFAULT_UI_JSON) -> str:
    payload: dict[str, Any] = json.loads(json_path.read_text(encoding="utf-8"))
    result = patch_lead_card(conn, job_key, payload=payload, message_id=message_id)
    write_payload(payload, json_path=json_path, ui_path=ui_path)
    return result
