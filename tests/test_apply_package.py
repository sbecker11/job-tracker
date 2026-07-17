"""Tests for apply_package CLI with generate_two_tier_package mocked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from job_tracker.cli import apply_package
from job_tracker.cli.apply_package import main as apply_package_main
from job_tracker.pipeline.llm_apply import CallMetrics, EvaluationResult, TwoTierPackageResult
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, upsert_lead
from job_tracker.scoring.scorer import ScoreResult


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(
        conn,
        JobLead(
            company="Acme",
            title="Software Engineer",
            source_message_id="m1",
            source_label="single-jd",
            jd_text="Acme is hiring a Software Engineer. Python, AWS, Spring Boot.",
            jd_resolved=True,
        ),
    )
    conn.close()
    return db_path


def _tier(
    *,
    ran_llm: bool = True,
    verdict: str = "pursue",
    resume: Path | None = None,
    cover: Path | None = None,
    warnings: list[str] | None = None,
    with_metrics: bool = True,
) -> TwoTierPackageResult:
    metrics = (
        CallMetrics(step="evaluate", model="fake", input_tokens=10, output_tokens=5, elapsed_s=1.2, cost_usd=0.01)
        if with_metrics
        else None
    )
    gen = (
        CallMetrics(step="generate", model="fake", input_tokens=20, output_tokens=8, elapsed_s=2.0, cost_usd=0.02)
        if with_metrics and resume
        else None
    )
    evaluation = (
        EvaluationResult(
            verdict=verdict,
            match_pct=85.0,
            rationale="good fit",
            job_summary="SWE role",
            metrics=metrics,
        )
        if ran_llm
        else None
    )
    return TwoTierPackageResult(
        no_llm_score=ScoreResult(match_pct=80.0, verdict="pursue", rationale=["Match 80%"]),
        jd_path=Path("/tmp/jd.docx"),
        no_llm_review_path=Path("/tmp/no-llm.docx"),
        ran_full_llm_review=ran_llm,
        evaluation=evaluation,
        full_llm_review_path=Path("/tmp/full.docx") if ran_llm else None,
        resume_path=resume,
        cover_letter_path=cover,
        warnings=warnings or [],
        generate_metrics=gen,
    )


def test_apply_package_missing_db(tmp_path: Path, capsys):
    rc = apply_package_main(
        ["--company", "X", "--title", "Y", "--db", str(tmp_path / "missing.db")]
    )
    assert rc == 1
    assert "No leads DB" in capsys.readouterr().err


def test_apply_package_missing_lead(seeded_db: Path, capsys):
    rc = apply_package_main(["--company", "Nope", "--title", "X", "--db", str(seeded_db)])
    assert rc == 1
    assert "No stored lead" in capsys.readouterr().err


def test_apply_package_no_jd_text(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    upsert_lead(
        conn,
        JobLead(company="Bare", title="Role", source_message_id="m", source_label="manual"),
    )
    conn.close()
    rc = apply_package_main(["--company", "Bare", "--title", "Role", "--db", str(db)])
    assert rc == 1
    assert "no stored jd_text" in capsys.readouterr().err


def test_apply_package_below_gate(monkeypatch, seeded_db: Path, capsys):
    monkeypatch.setattr(apply_package, "generate_two_tier_package", lambda *a, **k: _tier(ran_llm=False))
    rc = apply_package_main(["--company", "Acme", "--title", "Software Engineer", "--db", str(seeded_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no-LLM review" in out
    assert "Below the full-LLM-review gate" in out


def test_apply_package_pursue_with_package(monkeypatch, seeded_db: Path, capsys, tmp_path: Path):
    resume = tmp_path / "resume.docx"
    cover = tmp_path / "cover.docx"
    resume.write_text("r")
    cover.write_text("c")
    monkeypatch.setattr(
        apply_package,
        "generate_two_tier_package",
        lambda *a, **k: _tier(resume=resume, cover=cover, warnings=["bad phrase"]),
    )
    monkeypatch.setattr(apply_package, "render_jd_review", lambda *a, **k: "REVIEW TEXT")
    rc = apply_package_main(["--company", "Acme", "--title", "Software Engineer", "--db", str(seeded_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "REVIEW TEXT" in out
    assert "Résumé saved" in out
    assert "WARNING" in out


def test_apply_package_json_and_comparison(monkeypatch, seeded_db: Path, tmp_path: Path, capsys):
    resume = tmp_path / "r.docx"
    cover = tmp_path / "c.docx"
    resume.write_text("r")
    cover.write_text("c")
    comparison = tmp_path / "cmp.jsonl"
    comparison.write_text(
        json.dumps({"company": "Acme", "title": "Software Engineer", "other": 1}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        apply_package,
        "generate_two_tier_package",
        lambda *a, **k: _tier(resume=resume, cover=cover),
    )
    rc = apply_package_main(
        [
            "--company",
            "Acme",
            "--title",
            "Software Engineer",
            "--db",
            str(seeded_db),
            "--json",
            "--comparison-jsonl",
            str(comparison),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "pursue"
    assert payload["resume_path"]
    updated = json.loads(comparison.read_text(encoding="utf-8").strip())
    assert updated["claude_ai_verdict"] == "pursue"
    assert "resume-path" in updated


def test_apply_package_llm_pass_no_package(monkeypatch, seeded_db: Path, capsys):
    monkeypatch.setattr(
        apply_package,
        "generate_two_tier_package",
        lambda *a, **k: _tier(verdict="pass", with_metrics=False),
    )
    monkeypatch.setattr(apply_package, "render_jd_review", lambda *a, **k: "pass review")
    rc = apply_package_main(["--company", "Acme", "--title", "Software Engineer", "--db", str(seeded_db)])
    assert rc == 0
    assert "No package generated" in capsys.readouterr().out


def test_update_comparison_jsonl_no_file_or_no_eval(tmp_path: Path):
    assert apply_package._update_comparison_jsonl(tmp_path / "missing.jsonl", company="A", title="B", result=_tier()) is False
    path = tmp_path / "c.jsonl"
    path.write_text("{}\n")
    assert (
        apply_package._update_comparison_jsonl(
            path, company="A", title="B", result=_tier(ran_llm=False)
        )
        is False
    )


def test_apply_package_comparison_miss_note(monkeypatch, seeded_db: Path, tmp_path: Path, capsys):
    cmp_path = tmp_path / "empty.jsonl"
    cmp_path.write_text(json.dumps({"company": "Other", "title": "X"}) + "\n")
    monkeypatch.setattr(apply_package, "generate_two_tier_package", lambda *a, **k: _tier(ran_llm=False))
    rc = apply_package_main(
        [
            "--company",
            "Acme",
            "--title",
            "Software Engineer",
            "--db",
            str(seeded_db),
            "--comparison-jsonl",
            str(cmp_path),
        ]
    )
    assert rc == 0
    assert "no matching line" in capsys.readouterr().err


def test_apply_package_force_generate_non_pursue(monkeypatch, seeded_db: Path, tmp_path: Path, capsys):
    resume = tmp_path / "r.docx"
    cover = tmp_path / "c.docx"
    resume.write_text("r")
    cover.write_text("c")
    monkeypatch.setattr(
        apply_package,
        "generate_two_tier_package",
        lambda *a, **k: _tier(verdict="review", resume=resume, cover=cover),
    )
    monkeypatch.setattr(apply_package, "render_jd_review", lambda *a, **k: "review")
    rc = apply_package_main(
        ["--company", "Acme", "--title", "Software Engineer", "--db", str(seeded_db), "--force"]
    )
    assert rc == 0
    assert "generated anyway via --force" in capsys.readouterr().out
