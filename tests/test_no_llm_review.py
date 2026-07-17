"""Tests for the deterministic no-LLM-review CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_tracker.cli.no_llm_review import format_no_llm_review, main as no_llm_review_main
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, upsert_lead
from job_tracker.scoring.scorer import DealbreakerHit, RuleCheck, load_framework, rule_checklist, score_jd, ScoreResult


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(
        conn,
        JobLead(
            company="Acme",
            title="Senior Software Engineer",
            source_message_id="m1",
            source_label="single-jd",
            match_pct=80.0,
            matched_skills=["python", "aws"],
            verdict="pursue",
            rationale=["Match 80%"],
            jd_resolved=True,
            jd_source="ats_api",
            jd_text=(
                "Senior Software Engineer — Python, AWS, Aurora PostgreSQL, "
                "Spring Boot, Java, FastAPI, LangChain, RAG, React, TypeScript. "
                "W2 only. Must be a US citizen; no sponsorship available."
            ),
        ),
    )
    conn.close()
    return db_path


def test_rule_checklist_covers_dealbreakers_and_not_dealbreakers():
    load_framework.cache_clear()
    jd = (
        "Senior SWE — Python, AWS, Java, Spring Boot. W2 only. "
        "Must be a US citizen; authorized to work without sponsorship."
    )
    score = score_jd(jd)
    checks = rule_checklist(jd, score=score)
    by_id = {c.id: c for c in checks}

    assert by_id["golang"].status == "passed"
    assert by_id["c2c_only"].status == "passed"
    assert by_id["w2_only"].status == "passed"
    assert by_id["us_citizen_or_no_sponsorship"].status == "passed"
    assert any(c.status == "passed" and c.category == "skills" for c in checks)
    assert score.verdict in {"pursue", "review", "pass"}


def test_rule_checklist_fails_load_bearing_dealbreaker():
    load_framework.cache_clear()
    jd = "Corp-to-Corp only engagement, no W2. Golang engineer writing Go daily."
    score = score_jd(jd)
    checks = rule_checklist(jd, score=score)
    failed_ids = {c.id for c in checks if c.status == "failed"}
    assert "c2c_only" in failed_ids
    assert score.verdict == "pass"


def test_format_puts_verdict_prominently():
    jd = "Python AWS Java Spring Boot FastAPI LangChain RAG React TypeScript Aurora."
    score = score_jd(jd)
    checks = rule_checklist(jd, score=score)
    text = format_no_llm_review(score, checks, company="Acme", title="SWE")
    assert text.startswith("SWE @ Acme")
    assert "VERDICT:" in text.splitlines()[3]
    assert f"~{score.match_pct:.0f}%" in text
    assert "Passed rules" in text
    assert "Failed rules" in text
    # Closing verdict line too
    assert text.strip().splitlines()[-1].startswith("VERDICT:") or "VERDICT:" in text.strip().splitlines()[-3]


def test_cli_stdout_and_json(seeded_db: Path, capsys, tmp_path: Path):
    load_framework.cache_clear()
    out_root = tmp_path / "packages"

    rc = no_llm_review_main(
        ["--db", str(seeded_db), "--company", "Acme", "--title", "Senior Software Engineer"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "VERDICT:" in out
    assert "Passed rules" in out
    assert "Failed rules" in out

    rc = no_llm_review_main(
        [
            "--db",
            str(seeded_db),
            "--company",
            "Acme",
            "--title",
            "Senior Software Engineer",
            "--json",
            "--write",
            "--output-root",
            str(out_root),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] in {"pursue", "review", "pass"}
    assert "match_pct" in payload
    assert payload["score"]["verdict"] == payload["verdict"]
    assert isinstance(payload["passed_rules"], list)
    assert isinstance(payload["failed_rules"], list)
    assert payload["wrote_docx"] is True
    assert Path(payload["no_llm_review_path"]).is_file()


def test_cli_missing_db(tmp_path: Path, capsys):
    rc = no_llm_review_main(
        ["--db", str(tmp_path / "missing.db"), "--company", "X", "--title", "Y"]
    )
    assert rc == 1
    assert "No leads DB" in capsys.readouterr().err


def test_cli_unknown_job_and_similar(seeded_db: Path, capsys):
    rc = no_llm_review_main(
        ["--db", str(seeded_db), "--company", "Acme", "--title", "Senior Software Enginer"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "Did you mean" in err


def test_cli_no_jd_text(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    upsert_lead(
        conn,
        JobLead(company="Bare", title="Role", source_message_id="m", source_label="manual"),
    )
    conn.close()
    rc = no_llm_review_main(["--db", str(db), "--company", "Bare", "--title", "Role"])
    assert rc == 1
    assert "No jd_text" in capsys.readouterr().err


def test_format_dealbreaker_and_missing_skills_branches():
    from job_tracker.scoring.scorer import DealbreakerHit

    score = ScoreResult(
        match_pct=10.0,
        matched_skills=[],
        unmatched_jd_skills=["golang"],
        dealbreaker_hits=[
            DealbreakerHit(id="golang", label="Go/Golang", verdict="fail", hit_count=2, load_bearing=True),
            DealbreakerHit(id="w2", label="W2", verdict="clean", hit_count=1, load_bearing=False),
        ],
        verdict="pass",
        rationale=["too far"],
        relevant_weight=5.0,
    )
    checks = [
        RuleCheck(id="golang", label="Go", status="failed", reason="hit", category="dealbreaker"),
    ]
    text = format_no_llm_review(score, checks, company="X", title="Y", review_path=Path("/tmp/missing.docx"))
    assert "hard dealbreaker" in text
    assert "Missing:" in text
    assert "not on disk yet" in text
    assert "Tip: re-run with --write" in text
