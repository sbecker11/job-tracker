"""Tests for the list_contacts CLI (name, company, role, phone, email report)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from job_tracker.cli.list_contacts import main as list_contacts_main
from job_tracker.pipeline.models import JobContact, JobLead
from job_tracker.pipeline.store import add_job_contact, connect, upsert_lead


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    acme = JobLead(company="Acme", title="Software Engineer", source_message_id="m1", source_label="single-jd")
    globex = JobLead(company="Globex", title="Data Engineer", source_message_id="m2", source_label="single-jd")
    upsert_lead(conn, acme)
    upsert_lead(conn, globex)
    add_job_contact(conn, JobContact(job_key=acme.normalized_key, name="Jane Doe", email="jane@acme.com", phone="555-1111", role="recruiter"))
    add_job_contact(conn, JobContact(job_key=globex.normalized_key, name="John Roe", email="john@globex.com", phone="555-2222", role="hiring_manager"))
    conn.close()
    return db_path


def test_list_contacts_default_table_shows_all(seeded_db: Path, capsys):
    rc = list_contacts_main(["--db", str(seeded_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Jane Doe" in out
    assert "John Roe" in out
    assert "555-1111" in out
    assert "555-2222" in out


def test_list_contacts_filters_by_company(seeded_db: Path, capsys):
    rc = list_contacts_main(["--db", str(seeded_db), "--company", "acme"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Jane Doe" in out
    assert "John Roe" not in out


def test_list_contacts_json_output(seeded_db: Path, capsys):
    rc = list_contacts_main(["--db", str(seeded_db), "--json"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert any(r["email"] == "jane@acme.com" for r in rows)


def test_list_contacts_csv_export(seeded_db: Path, tmp_path: Path):
    csv_path = tmp_path / "contacts.csv"
    rc = list_contacts_main(["--db", str(seeded_db), "--csv", str(csv_path)])
    assert rc == 0
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"Jane Doe", "John Roe"}


def test_list_contacts_handles_no_matches(seeded_db: Path, capsys):
    rc = list_contacts_main(["--db", str(seeded_db), "--company", "nonexistent"])
    assert rc == 0
    assert "No matching contacts" in capsys.readouterr().out
