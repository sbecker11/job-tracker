"""Tests for process_awaiting_llm_review CLI with generate_two_tier_package mocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.cli import process_awaiting_llm_review
from job_tracker.cli.process_awaiting_llm_review import main as sweep_main
from job_tracker.pipeline.llm_apply import CallMetrics, EvaluationResult, TwoTierPackageResult
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, upsert_lead
from job_tracker.scoring.scorer import ScoreResult


def _lead(
    conn,
    *,
    company: str,
    title: str = "Software Engineer",
    match_pct: float = 90.0,
    verdict: str = "pursue",
    status: str = "new",
) -> None:
    upsert_lead(
        conn,
        JobLead(
            company=company,
            title=title,
            source_message_id=f"m-{company}",
            source_label="linkedin_message",
            jd_text=f"{company} is hiring a {title}. Python, AWS, Spring Boot.",
            jd_resolved=True,
            match_pct=match_pct,
            verdict=verdict,
            status=status,
        ),
    )


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    _lead(conn, company="WaferWire", match_pct=100.0, verdict="pursue")
    conn.close()
    return db_path


def _tier(
    *,
    ran_llm: bool = True,
    verdict: str = "pursue",
    resume: Path | None = None,
    cover: Path | None = None,
    match_pct: float = 90.0,
) -> TwoTierPackageResult:
    metrics = CallMetrics(step="evaluate", model="fake", input_tokens=10, output_tokens=5, elapsed_s=1.0, cost_usd=0.01)
    evaluation = (
        EvaluationResult(verdict=verdict, match_pct=85.0, rationale="good fit", job_summary="role", metrics=metrics)
        if ran_llm
        else None
    )
    return TwoTierPackageResult(
        no_llm_score=ScoreResult(match_pct=match_pct, verdict="pursue", rationale=["Match"]),
        jd_path=Path("/tmp/jd.docx"),
        no_llm_review_path=Path("/tmp/no-llm.docx"),
        ran_full_llm_review=ran_llm,
        evaluation=evaluation,
        full_llm_review_path=Path("/tmp/full.docx") if ran_llm else None,
        resume_path=resume,
        cover_letter_path=cover,
        generate_metrics=None,
    )


def test_missing_db(tmp_path: Path, capsys):
    rc = sweep_main(["--db", str(tmp_path / "nope.db")])
    assert rc == 1
    assert "No leads DB" in capsys.readouterr().err


def test_nothing_to_do(tmp_path: Path, capsys):
    db = tmp_path / "empty.db"
    conn = connect(db)
    conn.close()
    rc = sweep_main(["--db", str(db)])
    assert rc == 0
    assert "Nothing awaiting full-LLM-review" in capsys.readouterr().out


def test_below_gate_lead_not_picked_up(tmp_path: Path, capsys):
    """A lead whose rule-based score never cleared the gate (e.g. rule
    verdict='pass' from a dealbreaker, or just a low match_pct) shouldn't
    even show up as a candidate — same criterion apply_package.py's own
    should_run_llm_review gate uses, just applied at the SQL-filter level
    here so a whole backlog isn't touched every hour for nothing."""
    db = tmp_path / "leads.db"
    conn = connect(db)
    _lead(conn, company="LowMatch", match_pct=40.0, verdict="review")
    conn.close()
    rc = sweep_main(["--db", str(db)])
    assert rc == 0
    assert "Nothing awaiting full-LLM-review" in capsys.readouterr().out


def test_review_needed_lead_not_picked_up(tmp_path: Path, capsys):
    """The special 'JD unresolved' marker verdict is a different dashboard
    bucket entirely and must never be swept here (there's no real jd_text
    to evaluate for it in the first place)."""
    db = tmp_path / "leads.db"
    conn = connect(db)
    _lead(conn, company="NoJD", match_pct=0.0, verdict="REVIEW NEEDED")
    conn.close()
    rc = sweep_main(["--db", str(db)])
    assert rc == 0
    assert "Nothing awaiting full-LLM-review" in capsys.readouterr().out


def test_dry_run_lists_candidates_without_calling_api(seeded_db: Path, capsys):
    rc = sweep_main(["--db", str(seeded_db), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 lead(s) awaiting full-LLM-review" in out
    assert "WaferWire" in out


def test_pursue_advances_status_to_package_generated(monkeypatch, seeded_db: Path, tmp_path: Path, capsys):
    resume = tmp_path / "resume.docx"
    cover = tmp_path / "cover.docx"
    resume.write_text("r")
    cover.write_text("c")
    monkeypatch.setattr(
        process_awaiting_llm_review,
        "generate_two_tier_package",
        lambda *a, **k: _tier(resume=resume, cover=cover),
    )
    rc = sweep_main(["--db", str(seeded_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PURSUE" in out.upper()

    conn = connect(seeded_db)
    row = conn.execute("SELECT status, llm_verdict FROM job_leads WHERE company = 'WaferWire'").fetchone()
    conn.close()
    assert row["status"] == "package_generated"
    assert row["llm_verdict"] == "pursue"


def test_pass_advances_status_to_skipped(monkeypatch, seeded_db: Path, capsys):
    monkeypatch.setattr(
        process_awaiting_llm_review,
        "generate_two_tier_package",
        lambda *a, **k: _tier(verdict="pass"),
    )
    rc = sweep_main(["--db", str(seeded_db)])
    assert rc == 0

    conn = connect(seeded_db)
    row = conn.execute("SELECT status, llm_verdict FROM job_leads WHERE company = 'WaferWire'").fetchone()
    conn.close()
    assert row["status"] == "skipped"
    assert row["llm_verdict"] == "pass"


def test_review_leaves_status_at_new(monkeypatch, seeded_db: Path, capsys):
    monkeypatch.setattr(
        process_awaiting_llm_review,
        "generate_two_tier_package",
        lambda *a, **k: _tier(verdict="review"),
    )
    rc = sweep_main(["--db", str(seeded_db)])
    assert rc == 0

    conn = connect(seeded_db)
    row = conn.execute("SELECT status, llm_verdict FROM job_leads WHERE company = 'WaferWire'").fetchone()
    conn.close()
    assert row["status"] == "new"
    assert row["llm_verdict"] == "review"


def test_rescore_drops_below_gate_leaves_untouched(monkeypatch, seeded_db: Path, capsys):
    """If scoring.scorer's rules changed since match_pct was last stored and
    a fresh recompute inside generate_two_tier_package no longer clears the
    gate, nothing should be persisted — leave it at 'new' for the dashboard
    to re-triage correctly rather than half-updating the row."""
    monkeypatch.setattr(
        process_awaiting_llm_review,
        "generate_two_tier_package",
        lambda *a, **k: _tier(ran_llm=False, match_pct=50.0),
    )
    rc = sweep_main(["--db", str(seeded_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "below gate on rescore" in out

    conn = connect(seeded_db)
    row = conn.execute("SELECT status, llm_verdict FROM job_leads WHERE company = 'WaferWire'").fetchone()
    conn.close()
    assert row["status"] == "new"
    assert row["llm_verdict"] is None


def test_one_bad_lead_does_not_kill_the_whole_sweep(monkeypatch, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    _lead(conn, company="Broken")
    _lead(conn, company="Fine")
    conn.close()

    calls = {"n": 0}

    def _fake(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("api down")
        return _tier(verdict="pursue")

    monkeypatch.setattr(process_awaiting_llm_review, "generate_two_tier_package", _fake)
    rc = sweep_main(["--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "1 error(s)" in out
    assert "Processed 1 lead(s)" in out


def test_limit_caps_candidates(monkeypatch, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    _lead(conn, company="One")
    _lead(conn, company="Two")
    conn.close()
    rc = sweep_main(["--db", str(db), "--dry-run", "--limit", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 lead(s) awaiting full-LLM-review" in out
