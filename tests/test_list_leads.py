"""Tests for the list_leads review/export CLI."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from job_tracker.cli.list_leads import main as list_leads_main
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, upsert_lead


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(
        conn,
        JobLead(
            company="Stripe",
            title="Software Engineer",
            source_message_id="m1",
            source_label="single-jd",
            match_pct=42.0,
            matched_skills=["python", "aws"],
            verdict="pursue",
            rationale=["Match 42.0%"],
        ),
    )
    upsert_lead(
        conn,
        JobLead(
            company="BigCorp",
            title="Java Developer",
            source_message_id="m2",
            source_label="single-jd",
            match_pct=2.0,
            matched_skills=[],
            verdict="pass",
            rationale=["Match 2.0%"],
        ),
    )
    conn.close()
    return db_path


def test_list_leads_filters_by_verdict(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--verdict", "pursue"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stripe" in out
    assert "BigCorp" not in out


def test_list_leads_json_output(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--json"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert any(r["matched_skills"] == ["python", "aws"] for r in rows)


def test_list_leads_csv_export(seeded_db: Path, tmp_path: Path):
    csv_path = tmp_path / "out.csv"
    rc = list_leads_main(["--db", str(seeded_db), "--csv", str(csv_path)])
    assert rc == 0
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert {r["company"] for r in rows} == {"Stripe", "BigCorp"}


def test_list_leads_set_status(seeded_db: Path):
    rc = list_leads_main(["--db", str(seeded_db), "--verdict", "pursue", "--set-status", "pursuing"])
    assert rc == 0

    conn = connect(seeded_db)
    row = conn.execute("SELECT status FROM job_leads WHERE company = 'Stripe'").fetchone()
    assert row["status"] == "pursuing"
    other = conn.execute("SELECT status FROM job_leads WHERE company = 'BigCorp'").fetchone()
    assert other["status"] == "new"
    conn.close()


def test_list_leads_missing_db_reports_error(tmp_path: Path, capsys):
    rc = list_leads_main(["--db", str(tmp_path / "nope.db")])
    assert rc == 1
