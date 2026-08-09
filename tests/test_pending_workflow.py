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
    assert hit["nextAction"].startswith("YOUR ACTION:")
    assert "LinkedIn" in hit["nextAction"]


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
    assert hit["nextAction"].startswith("YOUR ACTION:")
    assert "Gmail" in hit["nextAction"]


def test_digest_alert_not_in_send_resume_or_clarify(tmp_path: Path):
    """Neighbor-style talent.com digests must not pollute Contact priority."""
    from job_tracker.pipeline.models import JobContact
    from job_tracker.pipeline.store import add_job_contact

    conn = connect(tmp_path / "leads.db")
    lead = JobLead(
        company="Neighbor",
        title="Senior Software Engineer",
        source_message_id="m-digest",
        source_label="job-alert",
        jd_text="python aws",
        match_pct=82.0,
        verdict="pursue",
        apply_url="https://jobs.example.com/neighbor/sse",
    )
    upsert_lead(conn, lead)
    add_job_contact(
        conn,
        JobContact(
            job_key=lead.normalized_key,
            name="Talent.com Alerts",
            email="no-reply@alerts.talent.com",
            role="recruiter",
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            message_id="gmail:digest-1",
            channel="email",
            direction="inbound",
            summary="Jobs you don't want to miss",
            occurred_at=(NOW - timedelta(days=1)).isoformat(),
            body_text=(
                "Jobs you don't want to miss\n"
                "More jobs like Senior Software Engineer at Neighbor\n"
                "From talent.com alerts"
            ),
        ),
    )
    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    data.setdefault("readyToApply", [])
    if not any(x.get("normalizedKey") == lead.normalized_key for x in data.get("readyToApply", [])):
        data["readyToApply"].append(
            {
                "normalizedKey": lead.normalized_key,
                "company": "Neighbor",
                "title": "Senior Software Engineer",
                "ageDays": 1,
                "matchPct": 82.0,
                "applyUrl": lead.apply_url,
                "folderPath": str(tmp_path / "Neighbor"),
                "directRecruiter": False,
            }
        )
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW, output_root=tmp_path
    )
    conn.close()

    send = workflow["stages"]["sendResume"]
    clarify = workflow["stages"]["clarify"]
    assert not any(i.get("normalizedKey") == lead.normalized_key for i in send)
    assert not any(i.get("normalizedKey") == lead.normalized_key for i in clarify)


def test_resume_ask_without_package_stays_in_decide_not_send(tmp_path: Path):
    """Recruiter asked for résumé, but Shawn hasn't pursued/generated yet."""
    from job_tracker.pipeline.models import JobContact
    from job_tracker.pipeline.store import add_job_contact

    conn = connect(tmp_path / "leads.db")
    lead = JobLead(
        company="NeoTek",
        title="AWS AI Engineer",
        source_message_id="m-neo",
        source_label="linkedin_message",
        jd_text="python aws sagemaker",
        match_pct=88.0,
        verdict="pursue",
    )
    upsert_lead(conn, lead)
    conn.execute(
        "UPDATE job_leads SET llm_verdict = ?, llm_match_pct = ? WHERE normalized_key = ?",
        ("review", 52.0, lead.normalized_key),
    )
    conn.commit()
    add_job_contact(
        conn,
        JobContact(
            job_key=lead.normalized_key,
            name="Deven Mehta",
            email="deven@example.com",
            role="recruiter",
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="inbound",
            summary="Pitch",
            occurred_at=(NOW - timedelta(days=5)).isoformat(),
            body_text="Hi Shawn, role details...",
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="outbound",
            summary="Clarifiers",
            occurred_at=(NOW - timedelta(days=3)).isoformat(),
            body_text="Quick clarifiers...",
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="inbound",
            summary="please send resume",
            occurred_at=(NOW - timedelta(days=1)).isoformat(),
            body_text="Thanks — please send resume when you can.",
        ),
    )
    # Review folder only — no résumé/cover.
    pkg = tmp_path / "NeoTek"
    pkg.mkdir()
    (pkg / "no-LLM-review.docx").write_bytes(b"x")
    (pkg / "full-LLM-review.docx").write_bytes(b"x")

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW, output_root=tmp_path
    )
    conn.close()

    assert not any(
        i.get("normalizedKey") == lead.normalized_key for i in workflow["stages"]["sendResume"]
    )
    needs = workflow["stages"]["decideApply"]["needsDecision"]
    hit = next(i for i in needs if i.get("normalizedKey") == lead.normalized_key)
    assert hit["resumeRequested"] is True
    assert "pursue" in hit["nextAction"].lower() or "skip" in hit["nextAction"].lower()


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
    hit = next(i for i in wait if i.get("normalizedKey") == lead.normalized_key)
    assert hit["nextAction"].startswith("YOUR ACTION:")
    assert "wait" in hit["nextAction"].lower()
