"""CON-6: "the recruiter" for a lead is picked one way everywhere."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from job_tracker.pipeline.models import JobContact, JobLead, normalize_key
from job_tracker.pipeline.pending_workflow import _recruiter_contact
from job_tracker.pipeline.store import connect, recruiter_contacts, upsert_lead

ROOT = Path(__file__).resolve().parents[1]
KEY = normalize_key("Acme Health", "Senior Data Engineer")


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "leads.db")
    upsert_lead(c, JobLead(company="Acme Health", title="Senior Data Engineer", source_message_id="s", source_label="email"))
    yield c
    c.close()


def _add(conn, name, email, role, last):
    conn.execute(
        "INSERT INTO job_contacts (job_key, name, email, role, first_contacted_at, last_contacted_at) "
        "VALUES (?, ?, ?, ?, '2026-10-01T00:00:00Z', ?)",
        (KEY, name, email, role, last),
    )


def test_role_recruiter_beats_more_recent_other_contact(conn):
    _add(conn, "Hiring Manager", "hm@acme.example", "hiring_manager", "2026-10-20T00:00:00Z")
    _add(conn, "Rita Recruiter", "rita@agency.example", "recruiter", "2026-10-02T00:00:00Z")
    assert [r["name"] for r in recruiter_contacts(conn, KEY)] == ["Rita Recruiter", "Hiring Manager"]


def test_most_recently_active_recruiter_wins(conn):
    _add(conn, "Old Contact", "old@agency.example", "recruiter", "2026-10-02T00:00:00Z")
    _add(conn, "New Contact", "new@agency.example", "recruiter", "2026-10-21T00:00:00Z")
    assert recruiter_contacts(conn, KEY)[0]["name"] == "New Contact"
    assert _recruiter_contact(conn, KEY)[1] == "new@agency.example"


def test_company_inbox_is_never_the_recruiter(conn):
    _add(conn, "Acme Careers", "careers@acme.example", "company", "2026-10-22T00:00:00Z")
    assert recruiter_contacts(conn, KEY) == []


def test_no_private_recruiter_order_left():
    """The old picks ordered by id or first_contacted_at; none may remain."""
    for rel in ("src/job_tracker/pipeline/pending_workflow.py", "scripts/render_pending_actions.py",
                "src/job_tracker/pipeline/find_mail.py"):
        text = (ROOT / rel).read_text()
        assert not re.search(r"FROM job_contacts[^\"]*?ORDER BY (?:id|first_contacted_at)", text, re.S), rel
