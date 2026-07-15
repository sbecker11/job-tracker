"""Tests for the interactive manual-lead-creation CLI (UC-3)."""

from __future__ import annotations

from pathlib import Path

from job_tracker.cli.add_job import main as add_job_main
from job_tracker.pipeline.store import connect, list_job_contacts, list_job_conversations


def _scripted_input(answers: list[str]):
    """Returns an input_func that pops one canned answer per call, so tests
    don't touch real stdin."""
    answers = list(answers)

    def _input(_prompt: str = "") -> str:
        return answers.pop(0) if answers else ""

    return _input


def test_add_job_creates_a_new_lead_with_defaults(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    answers = [
        "Acme",  # company
        "Software Engineer",  # title
        "",  # apply url
        "",  # note
        "",  # status (default "new")
        "",  # contact name (skip)
    ]
    rc = add_job_main(["--db", str(db_path)], input_func=_scripted_input(answers), print_func=lambda *a, **k: None)
    assert rc == 0

    conn = connect(db_path)
    row = conn.execute("SELECT * FROM job_leads WHERE company = 'Acme'").fetchone()
    assert row is not None
    assert row["title"] == "Software Engineer"
    assert row["status"] == "new"
    assert row["source_label"] == "manual"
    conn.close()


def test_add_job_requires_company_and_title_before_proceeding(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    printed: list[str] = []
    answers = [
        "",  # company blank -> reprompted
        "Globex",  # company
        "",  # title blank -> reprompted
        "Data Engineer",  # title
        "",  # apply url
        "",  # note
        "",  # status
        "",  # contact name
    ]
    rc = add_job_main(
        ["--db", str(db_path)], input_func=_scripted_input(answers), print_func=lambda *a, **k: printed.append(str(a))
    )
    assert rc == 0
    assert any("required" in p for p in printed)

    conn = connect(db_path)
    row = conn.execute("SELECT * FROM job_leads WHERE company = 'Globex'").fetchone()
    assert row is not None
    assert row["title"] == "Data Engineer"
    conn.close()


def test_add_job_sets_status_and_stamps_the_matching_timestamp(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    answers = ["Acme", "Software Engineer", "", "", "applied", ""]
    rc = add_job_main(["--db", str(db_path)], input_func=_scripted_input(answers), print_func=lambda *a, **k: None)
    assert rc == 0

    conn = connect(db_path)
    row = conn.execute("SELECT status, applied_at FROM job_leads WHERE company = 'Acme'").fetchone()
    assert row["status"] == "applied"
    assert row["applied_at"] is not None
    conn.close()


def test_add_job_reprompts_on_invalid_status_choice(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    answers = ["Acme", "Software Engineer", "", "", "bogus-status", "applied", ""]
    rc = add_job_main(["--db", str(db_path)], input_func=_scripted_input(answers), print_func=lambda *a, **k: None)
    assert rc == 0

    conn = connect(db_path)
    row = conn.execute("SELECT status FROM job_leads WHERE company = 'Acme'").fetchone()
    assert row["status"] == "applied"
    conn.close()


def test_add_job_creates_contact_when_name_given(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    answers = [
        "Acme",
        "Software Engineer",
        "",  # apply url
        "",  # note
        "",  # status
        "Jane Doe",  # contact name
        "jane@acme.com",  # contact email
        "555-1234",  # contact phone
        "recruiter",  # contact role
    ]
    rc = add_job_main(["--db", str(db_path)], input_func=_scripted_input(answers), print_func=lambda *a, **k: None)
    assert rc == 0

    conn = connect(db_path)
    key = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Acme'").fetchone()["normalized_key"]
    contacts = list_job_contacts(conn, key)
    assert len(contacts) == 1
    assert contacts[0]["name"] == "Jane Doe"
    assert contacts[0]["phone"] == "555-1234"
    conn.close()


def test_add_job_note_does_not_change_awaiting_response_since(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    answers = ["Acme", "Software Engineer", "", "Found via a referral", "", ""]
    rc = add_job_main(["--db", str(db_path)], input_func=_scripted_input(answers), print_func=lambda *a, **k: None)
    assert rc == 0

    conn = connect(db_path)
    key = conn.execute("SELECT normalized_key, awaiting_response_since FROM job_leads WHERE company = 'Acme'").fetchone()
    assert key["awaiting_response_since"] is None
    conversations = list_job_conversations(conn, key["normalized_key"])
    assert len(conversations) == 1
    assert conversations[0]["summary"] == "Found via a referral"
    conn.close()
