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
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_tracker.pipeline.models import JobLead, UnmatchedMessage
from job_tracker.pipeline.store import connect, record_unmatched_message, update_llm_evaluation, upsert_lead
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
    assert package == "Acme/Acme_Senior_SWE"
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
    assert by_title["Backend Engineer"]["folderPath"] == "Acme/Acme_Backend_Engineer"
    assert by_title["Frontend Engineer"]["folderPath"] == "Acme/Acme_Frontend_Engineer"


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
    assert "titleCellHtml(lead.title, lead.folderPath, lead.fileCount)" in text
    assert "companyCellHtml(lead.company, lead.companyFolderPath)" in text
    assert '"companyFolderPath": "Acme"' in text
    assert '"folderPath": "Acme"' in text


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

    text = render_pending_actions._render_html(data, output_root=tmp_path)
    assert "preview-cell" in text
    assert "preview-full" in text
    assert '"body": "This is the full message body.' in text
    assert 'headerLine("Message-Id"' in text
    assert 'headerLine("Subject"' in text
    assert 'headerLine("From"' in text
    assert 'headerLine("To"' in text
