"""Tests for scripts/render_pending_actions.py's age-based display/sort
(added 2026-07-15: a lead's value decays the longer it sits unreviewed, so
the pending-actions page needs to show + default-sort by days-since-received)
and Finder folder links (company root vs per-title package folder).

render_pending_actions.py lives in scripts/, not src/job_tracker/, so it
isn't on pytest's `pythonpath = ["src"]` — loaded here via importlib instead
of a normal import.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_tracker.pipeline.models import JobContact, JobConversation, JobLead, UnmatchedMessage
from job_tracker.pipeline.store import (
    add_job_contact,
    add_job_conversation,
    connect,
    record_message_processed,
    record_unmatched_message,
    update_llm_evaluation,
    upsert_lead,
)
from job_tracker.pipeline.llm_apply import CallMetrics, EvaluationResult

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_pending_actions.py"
_spec = importlib.util.spec_from_file_location("render_pending_actions", _SCRIPT_PATH)
render_pending_actions = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("render_pending_actions", render_pending_actions)
assert _spec.loader is not None
_spec.loader.exec_module(render_pending_actions)


NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_age_days_computes_whole_days_since_first_seen():
    ten_days_ago = (NOW - timedelta(days=10)).isoformat()
    assert render_pending_actions._age_days(ten_days_ago, NOW) == 10


def test_age_days_zero_for_a_lead_seen_today():
    assert render_pending_actions._age_days(NOW.isoformat(), NOW) == 0


def test_age_days_handles_missing_or_malformed_value_without_raising():
    assert render_pending_actions._age_days(None, NOW) == 0
    assert render_pending_actions._age_days("", NOW) == 0
    assert render_pending_actions._age_days("not-a-date", NOW) == 0


def test_age_days_treats_naive_timestamp_as_utc():
    naive_five_days_ago = (NOW - timedelta(days=5)).replace(tzinfo=None).isoformat()
    assert render_pending_actions._age_days(naive_five_days_ago, NOW) == 5


def _make_lead(conn, *, company: str, title: str, match_pct: float, verdict: str, first_seen: str) -> JobLead:
    lead = JobLead(
        company=company,
        title=title,
        source_message_id=f"test-{company}-{title}",
        source_label="test",
        match_pct=match_pct,
        verdict=verdict,
        jd_text="some jd text",
    )
    upsert_lead(conn, lead)
    conn.execute(
        "UPDATE job_leads SET first_seen = ? WHERE normalized_key = ?", (first_seen, lead.normalized_key)
    )
    conn.commit()
    return lead


def _set_llm_review(conn, lead: JobLead, *, llm_verdict: str, llm_match_pct: float) -> None:
    update_llm_evaluation(
        conn,
        lead.normalized_key,
        EvaluationResult(
            verdict=llm_verdict,
            match_pct=llm_match_pct,
            job_summary="test",
            dealbreaker_checks=[],
            skills_alignment=[],
            flags=[],
            rationale="test",
            framing_guidance=[],
            metrics=CallMetrics(
                step="evaluate", model="test", input_tokens=1, output_tokens=1, cost_usd=0.0
            ),
        ),
    )


def test_render_sorts_needs_decision_oldest_first_by_default(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    newer = _make_lead(
        conn, company="Newer Co", title="Engineer", match_pct=80.0, verdict="pursue",
        first_seen=(NOW - timedelta(days=2)).isoformat(),
    )
    older = _make_lead(
        conn, company="Older Co", title="Engineer", match_pct=40.0, verdict="review",
        first_seen=(NOW - timedelta(days=20)).isoformat(),
    )
    _set_llm_review(conn, newer, llm_verdict="pursue", llm_match_pct=80.0)
    _set_llm_review(conn, older, llm_verdict="review", llm_match_pct=40.0)

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    companies_in_order = [entry["company"] for entry in data["needs_decision"]]
    assert companies_in_order == ["Older Co", "Newer Co"]
    assert data["needs_decision"][0]["ageDays"] == 20
    assert data["needs_decision"][1]["ageDays"] == 2


def test_render_surfaces_direct_recruiter_flag_on_funnel_entries(tmp_path: Path):
    """2026-07-21, tri-state: a lead's funnel-bucket entry must carry the
    *actual* direct_recruiter_outreach value through as-is (True/False/None)
    for the dashboard's tri-state badge (filled gold star / no badge / empty
    outline star), and direct_recruiter_count must total up only the
    explicit Trues across every bucket."""
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    # match_pct >= LLM_REVIEW_GATE_PCT (70) with no llm_verdict yet lands a
    # lead in the "awaiting_llm_review" funnel bucket — one of the 5 buckets
    # direct_recruiter_count actually sums over (not_prioritized is excluded).
    direct = _make_lead(
        conn, company="WaferWire", title="Data Engineer", match_pct=80.0, verdict="review",
        first_seen=(NOW - timedelta(days=1)).isoformat(),
    )
    conn.execute(
        "UPDATE job_leads SET direct_recruiter_outreach = 1 WHERE normalized_key = ?", (direct.normalized_key,)
    )
    not_direct = _make_lead(
        conn, company="Reviewed Not Direct Co", title="Engineer", match_pct=80.0, verdict="review",
        first_seen=(NOW - timedelta(days=1)).isoformat(),
    )
    conn.execute(
        "UPDATE job_leads SET direct_recruiter_outreach = 0 WHERE normalized_key = ?", (not_direct.normalized_key,)
    )
    _make_lead(
        conn, company="Cold Digest Co", title="Engineer", match_pct=80.0, verdict="review",
        first_seen=(NOW - timedelta(days=1)).isoformat(),
    )
    conn.commit()

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    all_entries = (
        data["jd_unresolved"] + data["awaiting_llm_review"] + data["needs_decision"]
        + data["needs_decision_forced"] + data["ready_to_apply"]
    )
    by_company = {e["company"]: e for e in all_entries}
    assert by_company["WaferWire"]["directRecruiter"] is True
    assert by_company["Reviewed Not Direct Co"]["directRecruiter"] is False
    assert by_company["Cold Digest Co"]["directRecruiter"] is None
    # normalizedKey (2026-07-21) must also be carried through — the
    # dashboard's inline tri-state <select> needs it to target the setdro://
    # helper at the right lead.
    assert by_company["WaferWire"]["normalizedKey"] == direct.normalized_key
    assert data["direct_recruiter_count"] == 1
    # "Cold Digest Co" was never explicitly reviewed (still NULL in the DB)
    # — it must still count as undecided.
    assert data["direct_recruiter_undecided_count"] == 1
    # 2026-07-21: the subset of the undecided count that's actually visible
    # in the funnel-bucket tables (and therefore recomputable client-side
    # after an inline edit) — here all leads are in visible buckets, so it
    # matches the whole-DB figure, but the two are NOT always equal (see
    # the next test).
    assert data["direct_recruiter_undecided_visible_count"] == 1


def test_render_populates_age_days_on_funnel_buckets(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    _make_lead(
        conn, company="Auto Skip Co", title="Engineer", match_pct=10.0, verdict="pass",
        first_seen=(NOW - timedelta(days=3)).isoformat(),
    )
    unresolved = _make_lead(
        conn, company="Unresolved Co", title="Engineer", match_pct=0.0, verdict="REVIEW NEEDED",
        first_seen=(NOW - timedelta(days=7)).isoformat(),
    )
    # verdict REVIEW NEEDED is what lands in jd_unresolved; clear jd so gate logic doesn't confuse.
    conn.execute(
        "UPDATE job_leads SET jd_text = '' WHERE normalized_key = ?", (unresolved.normalized_key,)
    )
    conn.commit()

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    assert data["not_prioritized_count"] >= 1
    assert data["jd_unresolved"][0]["ageDays"] == 7
    assert data["jd_unresolved"][0]["company"] == "Unresolved Co"


def test_lead_folder_paths_single_vs_multi_lead(tmp_path: Path):
    """Company link uses the shared company root; title link uses the
    lead package folder (nested under company once a second title exists)."""
    package, company, count = render_pending_actions._lead_folder_and_count(
        tmp_path, company="Acme", title="Senior SWE", multi_lead=False
    )
    assert company == "Acme"
    assert package == "Acme"
    assert count == 0

    package, company, count = render_pending_actions._lead_folder_and_count(
        tmp_path, company="Acme", title="Senior SWE", multi_lead=True
    )
    assert company == "Acme"
    assert package == "Acme/Senior_SWE"
    assert count == 0


def test_render_multi_lead_company_exposes_distinct_folder_paths(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    backend = _make_lead(
        conn, company="Acme", title="Backend Engineer", match_pct=80.0, verdict="pursue",
        first_seen=NOW.isoformat(),
    )
    frontend = _make_lead(
        conn, company="Acme", title="Frontend Engineer", match_pct=75.0, verdict="pursue",
        first_seen=NOW.isoformat(),
    )
    _set_llm_review(conn, backend, llm_verdict="review", llm_match_pct=80.0)
    _set_llm_review(conn, frontend, llm_verdict="review", llm_match_pct=75.0)

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    by_title = {e["title"]: e for e in data["needs_decision"]}
    assert by_title["Backend Engineer"]["companyFolderPath"] == "Acme"
    assert by_title["Frontend Engineer"]["companyFolderPath"] == "Acme"
    assert by_title["Backend Engineer"]["folderPath"] == "Acme/Backend_Engineer"
    assert by_title["Frontend Engineer"]["folderPath"] == "Acme/Frontend_Engineer"


def test_html_wires_title_and_company_finder_links(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = _make_lead(
        conn, company="Acme", title="Engineer", match_pct=80.0, verdict="pursue",
        first_seen=NOW.isoformat(),
    )
    _set_llm_review(conn, lead, llm_verdict="review", llm_match_pct=80.0)
    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    text = render_pending_actions._render_html(data, output_root=tmp_path)
    assert "function titleCellHtml(" in text
    assert "companyFolderPath" in text
    assert "Open this role's folder in Finder" in text
    assert "Open company folder in Finder" in text
    assert "titleCellHtml(lead.title, lead.folderPath, lead.fileCount, lead.commCount, lead.company)" in text
    assert "companyCellHtml(lead.company, lead.companyFolderPath)" in text
    assert '"companyFolderPath": "Acme"' in text
    assert '"folderPath": "Acme"' in text


def test_render_computes_comm_count_per_lead(tmp_path: Path):
    """2026-07-22: each funnel entry carries commCount — the number of
    archived job_conversations rows for that lead — so the dashboard can
    show a clickable "view full history" badge next to the title without
    embedding the full conversation text (unlike unmatched_communications,
    where the whole body has to be inlined since there's no per-lead
    "generate a fresh PDF on click" helper for THOSE unresolved messages)."""
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = _make_lead(
        conn, company="Chatty Co", title="Engineer", match_pct=80.0, verdict="review",
        first_seen=NOW.isoformat(),
    )
    quiet_lead = _make_lead(
        conn, company="Quiet Co", title="Engineer", match_pct=80.0, verdict="review",
        first_seen=NOW.isoformat(),
    )
    _set_llm_review(conn, lead, llm_verdict="review", llm_match_pct=80.0)
    _set_llm_review(conn, quiet_lead, llm_verdict="review", llm_match_pct=80.0)
    contact_id = add_job_contact(conn, JobContact(job_key=lead.normalized_key, name="Jane Doe", email="jane@chatty.example"))
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key, contact_id=contact_id, message_id="m1",
            direction="inbound", summary="Hello", occurred_at=NOW.isoformat(),
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key, contact_id=contact_id, message_id="m2",
            direction="outbound", summary="Re: Hello", occurred_at=NOW.isoformat(),
        ),
    )

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    by_company = {e["company"]: e for e in data["needs_decision"]}
    assert by_company["Chatty Co"]["commCount"] == 2
    assert by_company["Quiet Co"]["commCount"] == 0


def test_html_wires_communications_badge(tmp_path: Path):
    """The 💬-badge markup/CSS and the viewcomms:// URL builder must be
    present in the rendered page — mirrors test_html_wires_title_and_
    company_finder_links' pattern for folderUrl()."""
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = _make_lead(
        conn, company="Acme", title="Engineer", match_pct=80.0, verdict="pursue",
        first_seen=NOW.isoformat(),
    )
    _set_llm_review(conn, lead, llm_verdict="review", llm_match_pct=80.0)
    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    text = render_pending_actions._render_html(data, output_root=tmp_path)
    assert "function commsUrl(" in text
    assert "viewcomms://open?company=" in text
    assert "comms-badge" in text
    assert '"commCount": 0' in text


def test_calendar_month_uptime_counts_distinct_ok_hours(tmp_path: Path):
    """Uptime = distinct local hours with Cycle-complete ÷ hours since month start."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    # NOW is 2026-07-15 12:00 UTC → expected hours from Jul 1 00:00 local through 12:00
    local_now = NOW.astimezone()
    month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Two OK cycles in the same hour → one covered hour; one incomplete → ignored
    h1 = month_start + timedelta(hours=3)
    (logs_dir / f"run-{h1.strftime('%Y%m%d-%H%M%S')}.log").write_text(
        "start\n=== Cycle complete ===\n", encoding="utf-8"
    )
    (logs_dir / f"run-{(h1 + timedelta(minutes=10)).strftime('%Y%m%d-%H%M%S')}.log").write_text(
        "start\n=== Cycle complete ===\n", encoding="utf-8"
    )
    h2 = month_start + timedelta(hours=5)
    (logs_dir / f"run-{h2.strftime('%Y%m%d-%H%M%S')}.log").write_text(
        "start\nFAILED: something\n", encoding="utf-8"
    )
    h3 = month_start + timedelta(hours=8)
    (logs_dir / f"run-{h3.strftime('%Y%m%d-%H%M%S')}.log").write_text(
        "start\n=== Cycle complete ===\n", encoding="utf-8"
    )

    result = render_pending_actions._calendar_month_uptime(logs_dir, now=NOW)
    expected = int((local_now.replace(minute=0, second=0, microsecond=0) - month_start).total_seconds() // 3600) + 1
    assert result["expectedHours"] == expected
    assert result["coveredHours"] == 2
    assert result["okCycles"] == 3
    assert result["uptimePct"] == round(100.0 * 2 / expected, 1)
    assert "Jul 2026" in result["headerLabel"]


def test_schedule_health_banner_and_poisoned_linkedin(tmp_path: Path):
    """2026-08-01: pending-actions must surface schedule gaps + wrongly-triaged InMails."""
    state_dir = tmp_path / "automation-state"
    state_dir.mkdir()
    logs_dir = state_dir.parent / "logs"
    logs_dir.mkdir()
    # last OK was 10h ago → stale at 6h threshold
    last_ok = int((NOW - timedelta(hours=10)).timestamp())
    (state_dir / "last_ok_cycle").write_text(str(last_ok), encoding="utf-8")
    (state_dir / "expiry_epoch").write_text(str(int((NOW + timedelta(days=2)).timestamp())), encoding="utf-8")
    # One OK cycle in July so month uptime is computable / wired into the header
    ok_at = NOW.replace(day=2, hour=9, minute=0, second=0, microsecond=0)
    (logs_dir / f"run-{ok_at.strftime('%Y%m%d-%H%M%S')}.log").write_text(
        "=== Cycle complete ===\n", encoding="utf-8"
    )

    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    record_message_processed(
        conn,
        "msg-poison-li",
        outcome="NEEDS_REVIEW",
        subject="Boomi InMail",
        from_address="inmail-hit-reply@linkedin.com",
        lead_keys=[],
        label_applied="JobTracker/NEEDS_REVIEW",
    )
    data = render_pending_actions.render(
        conn, output_root=tmp_path, now=NOW, automation_state_dir=state_dir
    )
    conn.close()

    assert data["schedule_health"]["stale"] is True
    assert data["schedule_health"]["level"] == "warning"
    assert data["schedule_health"]["monthUptime"]["coveredHours"] == 1
    assert len(data["poisoned_linkedin"]) == 1
    assert data["poisoned_linkedin"][0]["messageId"] == "msg-poison-li"

    text = render_pending_actions._render_html(data, output_root=tmp_path)
    assert "schedule-health" in text
    assert "month-uptime" in text
    assert "Jul 2026 uptime" in text
    assert "POISONED_LINKEDIN" in text
    assert "section-poisoned-linkedin" in text


def test_unmatched_communications_carries_full_body_alongside_preview(tmp_path: Path):
    """2026-07-17: the table's "Preview" cell is truncated to 180 chars, but
    the page has no live DB access to fetch the rest on demand — the full
    text has to already be embedded in `body` so the dashboard's click-to-
    expand can show it (with From/To/Subject/Message-Id repeated above it,
    so the expanded block reads standalone)."""
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    long_body = "This is the full message body. " * 20  # > 180 chars
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id="msg-abc",
            direction="inbound",
            from_address="radha@clevanoo.example",
            to_address="me@example.com",
            subject="Exciting opportunity",
            body_text=long_body,
        ),
    )

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    assert len(data["unmatched_communications"]) == 1
    entry = data["unmatched_communications"][0]
    assert entry["messageId"] == "msg-abc"
    assert entry["body"] == long_body
    assert len(entry["preview"]) <= 180
    assert entry["preview"] in long_body
    assert entry["draftReply"].strip().endswith("Best, Shawn")
    assert "linkedin_reply_queue" in data

    text = render_pending_actions._render_html(data, output_root=tmp_path)
    assert "preview-cell" in text
    assert "preview-full" in text
    assert '"body": "This is the full message body.' in text
    assert 'headerLine("Message-Id"' in text
    assert 'headerLine("Subject"' in text
    assert 'headerLine("From"' in text
    assert 'headerLine("To"' in text
    assert "section-linkedin-replies" in text
    assert "Copy reply" in text
    # Section 0 uses an explicit dark palette (not the old light #fafafa/#fff cards).
    assert "#section-linkedin-replies" in text
    assert "background: #0a0b0e" in text
    assert "background: var(--bg-elevated, #fafafa)" not in text
    assert ".reply-card pre.draft" in text and "background: #0a0b0e" in text
    # Show/Hide LinkedIn reply wiring (2026-08-06).
    assert "li-reply-toggle" in text
    assert "toggleLinkedinReply" in text
    assert "linkedinReplyId" in text or "linkedinReplyToggleCellHtml" in text


def test_linkedin_reply_ids_stamp_matching_leads_and_unmatched(tmp_path: Path):
    """Section 0 reply cards share a stable replyId with matching funnel /
    unmatched rows so Show/Hide can cross-link them."""
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = _make_lead(
        conn,
        company="Acme",
        title="Senior Engineer",
        match_pct=80.0,
        verdict="review",
        first_seen=NOW.isoformat(),
    )
    conn.execute(
        "UPDATE job_leads SET source_label = ? WHERE normalized_key = ?",
        ("linkedin_message", lead.normalized_key),
    )
    conn.commit()
    _set_llm_review(conn, lead, llm_verdict="review", llm_match_pct=80.0)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            message_id="imap:<lead-thread@linkedin.com>",
            channel="linkedin",
            direction="inbound",
            summary="Exciting opportunity for your skills",
            occurred_at=NOW.isoformat(),
            body_text=(
                "Hi Shawn,\n\nI'm Jane Doe at Acme recruiting for a Senior Engineer role.\n"
                "https://www.linkedin.com/messaging/thread/abc123/\n\nBest,\nJane"
            ),
        ),
    )
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id="imap:<unmatched-thread@linkedin.com>",
            direction="inbound",
            from_address="inmail-hit-reply@linkedin.com",
            to_address="me@example.com",
            subject="Remote opportunity — Full Stack",
            body_text=(
                "Hi Shawn,\n\nI'm Sam Recruiter.\n"
                "https://www.linkedin.com/messaging/thread/xyz789/\n\nBest,\nSam"
            ),
            detected_at=NOW.isoformat(),
        ),
    )

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    assert data["linkedin_reply_queue"]
    assert all(item.get("replyId") for item in data["linkedin_reply_queue"])

    lead_ids = {
        item["replyId"]
        for item in data["linkedin_reply_queue"]
        if item.get("normalizedKey") == lead.normalized_key
    }
    assert lead_ids, "expected a section-0 card for the LinkedIn stub lead"
    stamped = [
        row
        for bucket in (
            data["needs_decision"],
            data["needs_decision_forced"],
            data["ready_to_apply"],
            data["awaiting_llm_review"],
            data["jd_unresolved"],
        )
        for row in bucket
        if row["normalizedKey"] == lead.normalized_key
    ]
    assert stamped, "lead should appear in a funnel section"
    assert stamped[0]["linkedinReplyId"] in lead_ids

    unmatched_ids = {
        item["replyId"]
        for item in data["linkedin_reply_queue"]
        if item.get("messageId") == "imap:<unmatched-thread@linkedin.com>"
    }
    assert unmatched_ids
    unmatched_row = next(
        m for m in data["unmatched_communications"]
        if m["messageId"] == "imap:<unmatched-thread@linkedin.com>"
    )
    assert unmatched_row["linkedinReplyId"] in unmatched_ids

    text = render_pending_actions._render_html(data, output_root=tmp_path)
    assert "LinkedIn reply" in text
    assert "Show reply" in text
    assert "li-reply-toggle" in text


def test_json_for_script_escapes_angle_brackets():
    """imap:<id@host> must not appear raw inside <script> JSON."""
    raw = render_pending_actions._json_for_script(
        {"messageId": "imap:<123@host.example>"}
    )
    assert "imap:<" not in raw
    assert "\\u003c" in raw
    assert "\\u003e" in raw
    assert json.loads(raw)["messageId"] == "imap:<123@host.example>"
