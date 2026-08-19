"""Tests for stage-based pending workflow (Clarify → Send résumé → Wait → Decide/apply)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_tracker.pipeline.models import JobContact, JobConversation, JobLead, UnmatchedMessage
from job_tracker.pipeline.pending_workflow import build_workflow_payload
from job_tracker.pipeline.store import (
    add_job_contact,
    add_job_conversation,
    connect,
    mark_duplicate,
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
    assert hit["followUpDue"] is False
    assert "wait" in hit["nextAction"].lower()


def test_wait_followup_due_after_threshold(tmp_path: Path):
    """Waiting past wait_followup_days prompts a re-initiate draft."""
    from job_tracker.pipeline.store import set_awaiting_response

    conn = connect(tmp_path / "leads.db")
    lead = JobLead(
        company="Gamma",
        title="Engineer",
        source_message_id="m3",
        source_label="single-jd",
        jd_text="python aws",
        match_pct=80.0,
        verdict="pursue",
    )
    upsert_lead(conn, lead)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="outbound",
            summary="Sent résumé",
            occurred_at=(NOW - timedelta(days=10)).isoformat(),
            body_text="Please find attached...",
        ),
    )
    set_awaiting_response(conn, lead.normalized_key, True, when=(NOW - timedelta(days=10)).isoformat())
    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW, output_root=tmp_path
    )
    conn.close()

    wait = workflow["stages"]["waitSchedule"]
    hit = next(i for i in wait if i.get("normalizedKey") == lead.normalized_key)
    assert hit["followUpDue"] is True
    assert hit["waitingDays"] >= 7
    assert hit["draftReply"]
    assert "Re-initiate" in hit["nextAction"] or "follow-up" in hit["nextAction"].lower()
    assert workflow["waitFollowupDays"] == 7


def test_recruiter_followup_after_outbound_is_clarify_reply_due(tmp_path: Path):
    """Recruiter followed up after our reply — must surface as replyDue Clarify."""
    from job_tracker.pipeline.models import JobContact
    from job_tracker.pipeline.store import add_job_contact

    conn = connect(tmp_path / "leads.db")
    lead = JobLead(
        company="NeoTekTest",
        title="AWS AI Engineer",
        source_message_id="m-follow",
        source_label="linkedin_message",
        jd_text="python aws sagemaker",
        match_pct=88.0,
        verdict="pursue",
    )
    upsert_lead(conn, lead)
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
            summary="Initial pitch",
            occurred_at=(NOW - timedelta(days=10)).isoformat(),
            body_text="Hi Shawn, opportunity details...",
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="outbound",
            summary="Clarifiers sent",
            occurred_at=(NOW - timedelta(days=8)).isoformat(),
            body_text="Quick clarifiers on W2 / end client...",
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="inbound",
            summary="Follow-up with more detail",
            occurred_at=(NOW - timedelta(days=2)).isoformat(),
            body_text="Thanks — here is more detail on the HIPAA scrubbing vision. Let me know.",
        ),
    )
    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW, output_root=tmp_path
    )
    conn.close()

    clarify = workflow["stages"]["clarify"]
    hit = next(i for i in clarify if i.get("normalizedKey") == lead.normalized_key)
    assert hit["replyDue"] is True
    assert hit["unansweredDays"] >= 2
    assert "Recruiter followed up" in hit["nextAction"]
    assert hit["draftReply"]


def test_archived_leads_include_duplicate_of_survivor_details(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    survivor = JobLead(
        company="NeoTek", title="AWS AI Engineer", source_message_id="m1", source_label="single-jd"
    )
    dup = JobLead(
        company="Department of Medicaid",
        title="AWS AI Engineer",
        source_message_id="m2",
        source_label="single-jd",
    )
    plain_skip = JobLead(company="Acme", title="Backend Engineer", source_message_id="m3", source_label="single-jd")
    upsert_lead(conn, survivor)
    upsert_lead(conn, dup)
    upsert_lead(conn, plain_skip)
    mark_duplicate(conn, dup.normalized_key, duplicate_of_key=survivor.normalized_key)
    from job_tracker.pipeline.store import advance_status

    advance_status(conn, plain_skip.normalized_key, "skipped")

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW, output_root=tmp_path
    )
    conn.close()

    archived = workflow["archivedLeads"]
    dup_item = next(i for i in archived if i["normalizedKey"] == dup.normalized_key)
    assert dup_item["duplicateOfKey"] == survivor.normalized_key
    assert dup_item["duplicateOfCompany"] == "NeoTek"
    assert dup_item["duplicateOfTitle"] == "AWS AI Engineer"

    plain_item = next(i for i in archived if i["normalizedKey"] == plain_skip.normalized_key)
    assert "duplicateOfKey" not in plain_item


def test_archived_leads_include_earliest_recruiter_contact(tmp_path: Path):
    """The archived-leads list should surface the earliest job_contacts row
    for each lead (2026-08-19) so the Pending actions search box can match
    on recruiter name/email/phone, not just company/title."""
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(company="Acme", title="Backend Engineer", source_message_id="m1", source_label="single-jd")
    upsert_lead(conn, lead)
    from job_tracker.pipeline.store import advance_status

    advance_status(conn, lead.normalized_key, "skipped")
    add_job_contact(
        conn,
        JobContact(job_key=lead.normalized_key, name="Jane Recruiter", email="jane@acme.com", phone="555-1234"),
    )

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW, output_root=tmp_path
    )
    conn.close()

    item = next(i for i in workflow["archivedLeads"] if i["normalizedKey"] == lead.normalized_key)
    assert item["recruiterName"] == "Jane Recruiter"
    assert item["recruiterEmail"] == "jane@acme.com"
    assert item["recruiterPhone"] == "555-1234"


def test_duplicate_count_surfaces_on_survivor_wherever_it_appears(tmp_path: Path):
    """A survivor lead should show its duplicateCount in whatever stage it
    naturally lands in (not only once it's archived) — added 2026-08-17 so
    "how do I see duplicates of the lead I'm looking at right now" works
    from Decide/apply, not just the dedicated Duplicate leads folder."""
    conn = connect(tmp_path / "leads.db")
    survivor = JobLead(
        company="NeoTek",
        title="AWS AI Engineer",
        source_message_id="m-neo",
        source_label="linkedin_message",
        jd_text="python aws sagemaker",
        match_pct=88.0,
        verdict="pursue",
    )
    upsert_lead(conn, survivor)
    conn.execute(
        "UPDATE job_leads SET llm_verdict = ?, llm_match_pct = ? WHERE normalized_key = ?",
        ("review", 52.0, survivor.normalized_key),
    )
    conn.commit()
    dup = JobLead(
        company="Department of Medicaid",
        title="AWS AI Engineer",
        source_message_id="m-dom",
        source_label="single-jd",
    )
    upsert_lead(conn, dup)
    mark_duplicate(conn, dup.normalized_key, duplicate_of_key=survivor.normalized_key)

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW, output_root=tmp_path
    )
    conn.close()

    needs = workflow["stages"]["decideApply"]["needsDecision"]
    hit = next(i for i in needs if i.get("normalizedKey") == survivor.normalized_key)
    assert hit["duplicateCount"] == 1
    assert hit["duplicateKeys"] == [dup.normalized_key]


def test_applied_or_beyond_lead_still_appears_in_archived_leads(tmp_path: Path):
    """A survivor lead that's since moved past the apply gate (status
    "applied"/"interviewing"/etc.) is absent from every clarify/send_resume/
    wait_schedule/decide_apply bucket — added 2026-08-17 after a duplicate's
    "Go to this lead" link hit a dead end for exactly this case: the
    survivor wasn't in the active funnel, and (before this test/fix) wasn't
    in archivedLeads either, so nothing in the whole payload could name it
    as a real, linkable row."""
    conn = connect(tmp_path / "leads.db")
    survivor = JobLead(
        company="Upstart", title="Sr Software Engineer - Lending Platform", source_message_id="m1", source_label="single-jd"
    )
    upsert_lead(conn, survivor)
    from job_tracker.pipeline.store import advance_status

    advance_status(conn, survivor.normalized_key, "applied")

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW, output_root=tmp_path
    )
    conn.close()

    for bucket in workflow["stages"]["decideApply"].values():
        assert all(i["normalizedKey"] != survivor.normalized_key for i in bucket)

    archived = workflow["archivedLeads"]
    hit = next(i for i in archived if i["normalizedKey"] == survivor.normalized_key)
    assert hit["status"] == "applied"


def test_recruiting_gmail_message_url_pins_authuser():
    from job_tracker.pipeline.pending_workflow import recruiting_gmail_message_url

    url = recruiting_gmail_message_url("19fafc5dddc2baa5")
    assert "accounts.google.com/AccountChooser" in url
    assert "shawnbecker.recruiting" in url
    assert "19fafc5dddc2baa5" in url
    assert "#all/19fafc5dddc2baa5" in url or "%23all%2F19fafc5dddc2baa5" in url
    assert recruiting_gmail_message_url("imap:<foo@bar>") == ""
    assert recruiting_gmail_message_url("") == ""
