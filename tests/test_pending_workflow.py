"""Tests for stage-based pending workflow (Clarify → Send résumé → Wait → Decide/apply)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_tracker.pipeline.models import JobConversation, JobLead, UnmatchedMessage
from job_tracker.pipeline.pending_workflow import build_workflow_payload
from job_tracker.pipeline.store import (
    add_job_conversation,
    connect,
    record_unmatched_message,
    upsert_lead,
)

# Import render helpers the same way as test_render_pending_actions.
import importlib.util
import sys

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_pending_actions.py"
_spec = importlib.util.spec_from_file_location("render_pending_actions", _SCRIPT_PATH)
render_pending_actions = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("render_pending_actions", render_pending_actions)
assert _spec.loader is not None
_spec.loader.exec_module(render_pending_actions)

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def test_workflow_clarify_includes_linkedin_stub_and_sorts_by_attempts(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(
        company="Acme",
        title="Senior Engineer",
        source_message_id="m1",
        source_label="linkedin_message",
        jd_text="python aws data",
        match_pct=80.0,
        verdict="review",
    )
    upsert_lead(conn, lead)
    conn.execute(
        "UPDATE job_leads SET first_seen = ? WHERE normalized_key = ?",
        ((NOW - timedelta(days=2)).isoformat(), lead.normalized_key),
    )
    conn.commit()
    body = (
        "Hi Shawn,\n\nI'm Jane Doe at Acme.\n"
        "https://www.linkedin.com/messaging/thread/abc/\n\nBest,\nJane"
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            message_id="imap:<1@li>",
            channel="linkedin",
            direction="inbound",
            summary="Exciting opportunity for your skills",
            occurred_at=(NOW - timedelta(days=2)).isoformat(),
            body_text=body,
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            message_id="imap:<2@li>",
            channel="linkedin",
            direction="inbound",
            summary="Following up",
            occurred_at=(NOW - timedelta(days=1)).isoformat(),
            body_text=body + "\nJust following up.",
        ),
    )

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW
    )
    conn.close()

    assert workflow["pipeline"][0]["id"] == "clarify"
    clarify = workflow["stages"]["clarify"]
    assert any(i.get("normalizedKey") == lead.normalized_key for i in clarify)
    hit = next(i for i in clarify if i.get("normalizedKey") == lead.normalized_key)
    assert hit["contactAttempts"] == 2
    assert hit["channel"] == "linkedin"
    assert hit["draftReply"]


def test_workflow_email_unmatched_lands_in_clarify(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id="gmail:email-1",
            direction="inbound",
            from_address="prachi@agency.com",
            to_address="shawnbecker.recruiting@gmail.com",
            subject="Exploring a new opportunity together",
            body_text=(
                "Hi Shawn,\n\nI'm Prachi Gupta with an exciting contract role.\n"
                "Please reply with your interest.\n\nBest,\nPrachi Gupta\nRecruiter"
            ),
            detected_at=NOW.isoformat(),
        ),
    )
    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW
    )
    conn.close()

    clarify = workflow["stages"]["clarify"]
    assert any(i.get("messageId") == "gmail:email-1" for i in clarify)
    hit = next(i for i in clarify if i.get("messageId") == "gmail:email-1")
    assert hit["channel"] == "email"
    assert "W2" in hit["draftReply"] or "1099" in hit["draftReply"]


def test_workflow_wait_uses_awaiting_response_since(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(
        company="Beta",
        title="Engineer",
        source_message_id="m2",
        source_label="single-jd",
        jd_text="java spring",
        match_pct=75.0,
        verdict="review",
    )
    upsert_lead(conn, lead)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="outbound",
            summary="Sent clarifiers",
            occurred_at=(NOW - timedelta(days=3)).isoformat(),
            body_text="Hi, quick clarifiers...",
        ),
    )
    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW
    )
    conn.close()

    wait = workflow["stages"]["waitSchedule"]
    assert any(i.get("normalizedKey") == lead.normalized_key for i in wait)
