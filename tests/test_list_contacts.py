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


def test_list_contacts_filters_by_contact_name(seeded_db: Path, capsys):
    rc = list_contacts_main(["--db", str(seeded_db), "--contact", "jane doe"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Jane Doe" in out
    assert "John Roe" not in out


def test_list_contacts_filters_by_contact_email(seeded_db: Path, capsys):
    rc = list_contacts_main(["--db", str(seeded_db), "--contact", "john@globex.com"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "John Roe" in out
    assert "Jane Doe" not in out


def test_list_contacts_filters_by_contact_phone(seeded_db: Path, capsys):
    rc = list_contacts_main(["--db", str(seeded_db), "--contact", "555-1111"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Jane Doe" in out
    assert "John Roe" not in out


def test_list_contacts_by_contact_surfaces_every_related_lead(tmp_path: Path, capsys):
    """The 'a recruiter I've worked with before just called' lookup: one
    recruiter sourced two different leads, and looking them up by name
    should surface both, not just the most recent."""
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    initech = JobLead(company="Initech", title="Backend Engineer", source_message_id="m3", source_label="single-jd")
    hooli = JobLead(company="Hooli", title="Platform Engineer", source_message_id="m4", source_label="single-jd")
    upsert_lead(conn, initech)
    upsert_lead(conn, hooli)
    add_job_contact(
        conn,
        JobContact(
            job_key=initech.normalized_key,
            name="Cole Keener",
            email="cole@crbworkforce.com",
            phone="530-722-7548",
            role="recruiter",
        ),
    )
    add_job_contact(
        conn,
        JobContact(
            job_key=hooli.normalized_key,
            name="Cole Keener",
            email="cole@crbworkforce.com",
            phone="530-722-7548",
            role="recruiter",
        ),
    )
    conn.close()

    rc = list_contacts_main(["--db", str(db_path), "--contact", "Cole Keener"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Initech" in out
    assert "Hooli" in out
    assert out.count("Cole Keener") == 2
