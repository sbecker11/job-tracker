"""Tests for run_pipeline CLI (fixtures + mocked Gmail)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_tracker.cli import run_pipeline as run_pipeline_cli
from job_tracker.cli.run_pipeline import main as run_pipeline_main
from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.run import PipelineSummary

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_run_pipeline_fixture(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    fixture = FIXTURES / "stripe_single_jd.json"
    rc = run_pipeline_main(["--fixture", str(fixture), "--db", str(db), "--offline"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Processed" in out


def test_run_pipeline_all_fixtures_json(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    rc = run_pipeline_main(
        ["--all-fixtures", "--fixtures-dir", str(FIXTURES), "--db", str(db), "--offline", "--json"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_messages"] >= 1
    assert "leads" in payload


def test_run_pipeline_all_fixtures_empty(tmp_path: Path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = run_pipeline_main(["--all-fixtures", "--fixtures-dir", str(empty), "--db", str(tmp_path / "db.sqlite")])
    assert rc == 1
    assert "No fixtures" in capsys.readouterr().err


def test_run_pipeline_dry_run_no_messages(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(run_pipeline_cli, "fetch_unread", lambda **k: [])
    rc = run_pipeline_main(["--dry-run", "--db", str(tmp_path / "leads.db"), "--offline"])
    assert rc == 0
    assert "No messages matched" in capsys.readouterr().err


def test_run_pipeline_message_id_mocked(monkeypatch, tmp_path: Path, capsys):
    msg = EmailMessage(**json.loads((FIXTURES / "stripe_single_jd.json").read_text()))
    monkeypatch.setattr(run_pipeline_cli, "fetch_message_by_id", lambda *a, **k: msg)
    rc = run_pipeline_main(
        ["--message-id", "fixture-stripe", "--db", str(tmp_path / "leads.db"), "--offline"]
    )
    assert rc == 0
    assert "Processed" in capsys.readouterr().out


def test_print_report_covers_sections(capsys):
    summary = PipelineSummary(
        total_messages=3,
        skipped={"noise": 1},
        new_leads=1,
        llm_fallback_used=1,
        llm_fallback_rescued=1,
        leads=[
            {
                "match_pct": 80.0,
                "title": "SWE",
                "company": "Acme",
                "apply_url": "https://example.com",
                "rationale": ["strong match"],
                "verdict": "pursue",
            },
            {
                "match_pct": 55.0,
                "title": "DE",
                "company": "Beta",
                "apply_url": "",
                "rationale": [],
                "verdict": "review",
            },
            {
                "match_pct": 10.0,
                "title": "X",
                "company": "Y",
                "apply_url": "",
                "rationale": [],
                "verdict": "pass",
            },
        ],
        outreach_needs_reply=[{"subject": "Hi", "from": "r@x.com"}],
        needs_review=[
            {
                "message_id": "m1",
                "subject": "Digest",
                "reason": "truncated",
                "partial": {"title": "Role A"},
            },
            {
                "message_id": "m1",
                "subject": "Digest",
                "reason": "truncated",
                "partial": {"title": "Role B"},
            },
            {
                "message_id": "m1",
                "subject": "Digest",
                "reason": "truncated",
                "partial": {"title": "Role C"},
            },
            {
                "message_id": "m1",
                "subject": "Digest",
                "reason": "truncated",
                "partial": {"title": "Role D"},
            },
        ],
    )
    run_pipeline_cli._print_report(summary)
    out = capsys.readouterr().out
    assert "PURSUE" in out
    assert "REVIEW" in out
    assert "RECRUITER OUTREACH" in out
    assert "EXTRACTION NEEDS REVIEW" in out
    assert "and 1 more" in out
    assert "PASS" in out
