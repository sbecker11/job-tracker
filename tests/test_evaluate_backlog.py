"""Tests for evaluate_backlog CLI with evaluate_lead mocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.cli import evaluate_backlog
from job_tracker.cli.evaluate_backlog import main as evaluate_backlog_main
from job_tracker.pipeline.llm_apply import CallMetrics, EvaluationResult
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, upsert_lead


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    for i, (company, title) in enumerate(
        [("Acme", "SWE"), ("Beta", "DE"), ("Gamma", "ML")]
    ):
        upsert_lead(
            conn,
            JobLead(
                company=company,
                title=title,
                source_message_id=f"m{i}",
                source_label="single-jd",
                jd_text=f"{company} hiring {title}. Python AWS.",
                jd_resolved=True,
            ),
        )
    conn.close()
    return db_path


def _eval(verdict: str, match_pct: float = 70.0, with_metrics: bool = True) -> EvaluationResult:
    return EvaluationResult(
        verdict=verdict,
        match_pct=match_pct,
        rationale=f"{verdict} rationale",
        metrics=CallMetrics(
            step="evaluate",
            model="fake",
            input_tokens=11,
            output_tokens=3,
            elapsed_s=0.5,
            cost_usd=0.001,
        )
        if with_metrics
        else None,
    )


def test_evaluate_backlog_missing_db(tmp_path: Path, capsys):
    rc = evaluate_backlog_main(["--db", str(tmp_path / "nope.db")])
    assert rc == 1
    assert "No leads DB" in capsys.readouterr().err


def test_evaluate_backlog_nothing_to_do(tmp_path: Path, capsys):
    db = tmp_path / "empty.db"
    conn = connect(db)
    conn.close()
    rc = evaluate_backlog_main(["--db", str(db)])
    assert rc == 0
    assert "Nothing to evaluate" in capsys.readouterr().out


def test_evaluate_backlog_dry_run(seeded_db: Path, capsys):
    rc = evaluate_backlog_main(["--db", str(seeded_db), "--dry-run", "--limit", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "lead(s) to evaluate" in out
    assert "Acme" in out or "SWE" in out


def test_evaluate_backlog_runs_and_summarizes(monkeypatch, seeded_db: Path, capsys):
    responses = [_eval("pursue", 90), _eval("review", 60), _eval("pass", 20)]

    def fake_eval(*a, **k):
        return responses.pop(0)

    monkeypatch.setattr(evaluate_backlog, "evaluate_lead", fake_eval)
    rc = evaluate_backlog_main(["--db", str(seeded_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PURSUE" in out
    assert "REVIEW" in out
    assert "To generate a résumé" in out


def test_evaluate_backlog_handles_errors(monkeypatch, seeded_db: Path, capsys):
    def boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(evaluate_backlog, "evaluate_lead", boom)
    rc = evaluate_backlog_main(["--db", str(seeded_db), "--limit", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "error(s)" in out
