"""End-to-end tests for the classify -> extract -> resolve -> score -> store pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_tracker.email.models import EmailMessage, ExtractedRole
from job_tracker.pipeline.run import run_pipeline
from job_tracker.pipeline.store import connect, list_leads

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_all_fixtures() -> list[EmailMessage]:
    return [
        EmailMessage(**json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(FIXTURES_DIR.glob("*.json"))
    ]


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "leads.db"


def test_pipeline_skips_noise_rejection_and_digests(db_path: Path):
    summary = run_pipeline(load_all_fixtures(), db_path=db_path, resolve_full_jd=False)
    assert summary.skipped.get("noise") == 1
    assert summary.skipped.get("rejection") == 1
    assert summary.skipped.get("link-only-digest") == 2


def test_pipeline_routes_recruiter_outreach_separately(db_path: Path):
    summary = run_pipeline(load_all_fixtures(), db_path=db_path, resolve_full_jd=False)
    assert len(summary.outreach_needs_reply) == 1
    assert summary.outreach_needs_reply[0]["message_id"] == "fixture-recruiter-outreach"


def test_pipeline_produces_leads_for_single_and_multi_jd(db_path: Path):
    summary = run_pipeline(load_all_fixtures(), db_path=db_path, resolve_full_jd=False)
    companies = {lead["company"] for lead in summary.leads}
    assert "Stripe" in companies
    assert "StartupCo" in companies
    assert "Acme Corp" in companies
    assert "Robert Half" in companies  # from the flattened job-board digest
    # 1 single-jd + 3 multi-jd bullets + 2 ATS search-agent digest listings
    # + 4 flattened job-board digest listings. (The Ref-no web-aggregation
    # and marketing-noise fixtures correctly produce zero leads — company
    # is ambiguous/absent in those formats — see test_extract.py for why.)
    assert len(summary.leads) == 10


def test_pipeline_dedups_on_rerun(db_path: Path):
    messages = load_all_fixtures()
    first = run_pipeline(messages, db_path=db_path, resolve_full_jd=False)
    second = run_pipeline(messages, db_path=db_path, resolve_full_jd=False)
    assert first.new_leads == 10
    assert second.new_leads == 0

    conn = connect(db_path)
    rows = list_leads(conn)
    assert len(rows) == 10
    assert all(row["times_seen"] == 2 for row in rows)
    conn.close()


def test_pipeline_offline_mode_never_hits_network(db_path: Path, monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("resolve_ats_jd should not be called when resolve_full_jd=False")

    monkeypatch.setattr("job_tracker.pipeline.run.resolve_ats_jd", _fail)
    summary = run_pipeline(load_all_fixtures(), db_path=db_path, resolve_full_jd=False)
    assert summary.total_messages == 11


def test_pipeline_reuses_postings_across_roles_from_same_company(db_path: Path, monkeypatch):
    """3 StartupCo roles in one digest should fetch StartupCo's board once, not 3x."""
    calls = []

    def _fake_gather_postings(company, verbose=False):
        calls.append(company)
        return []

    monkeypatch.setattr("job_tracker.pipeline.run.gather_postings", _fake_gather_postings)
    run_pipeline(load_all_fixtures(), db_path=db_path, resolve_full_jd=True)

    startupco_calls = [c for c in calls if c.lower() == "startupco"]
    assert len(startupco_calls) == 1, f"expected 1 gather_postings call for StartupCo, got {len(startupco_calls)}"


def test_llm_fallback_is_off_by_default(db_path: Path):
    summary = run_pipeline(load_all_fixtures(), db_path=db_path, resolve_full_jd=False)
    assert summary.llm_fallback_used == 0
    assert summary.llm_fallback_rescued == 0


def test_llm_fallback_rescues_a_message_the_regex_pass_couldnt_finish(db_path: Path, monkeypatch):
    """The Ref-no web-aggregation digest fixture leaves company blank on every
    listing (ambiguous by design — see test_extract.py); with --llm-fallback
    on, a (mocked) LLM call should be able to complete it instead."""

    def _fake_llm(conn, message, *, model=None, client=None):
        if message.id != "fixture-ref-no-web-aggregation-digest":
            return []
        return [
            ExtractedRole(
                company="Example Co",
                title="Senior Full Stack Engineer",
                source="llm_fallback",
                confidence=0.8,
            )
        ]

    monkeypatch.setattr("job_tracker.pipeline.run.extract_roles_llm_cached", _fake_llm)
    summary = run_pipeline(
        load_all_fixtures(), db_path=db_path, resolve_full_jd=False, use_llm_fallback=True
    )

    assert summary.llm_fallback_used >= 1
    assert summary.llm_fallback_rescued == 1
    assert any(lead["company"] == "Example Co" for lead in summary.leads)


def test_llm_fallback_not_invoked_for_messages_regex_already_handled(db_path: Path, monkeypatch):
    calls = []

    def _fake_llm(conn, message, *, model=None, client=None):
        calls.append(message.id)
        return []

    monkeypatch.setattr("job_tracker.pipeline.run.extract_roles_llm_cached", _fake_llm)
    run_pipeline(load_all_fixtures(), db_path=db_path, resolve_full_jd=False, use_llm_fallback=True)

    assert "fixture-stripe-single-jd" not in calls
