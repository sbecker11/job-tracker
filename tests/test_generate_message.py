"""Tests for the generate_message CLI. The underlying LLM call
(llm_apply.generate_followup_message) is unit-tested against a fake
Anthropic client in test_llm_apply.py; here it's stubbed out entirely so
these tests only exercise this CLI's own wiring (job resolution, contact
auto-pick, days-since-contact math, saving the draft + JobDocument row).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.cli import generate_message as generate_message_module
from job_tracker.cli.generate_message import main as generate_message_main
from job_tracker.pipeline.llm_apply import FollowupMessageResult
from job_tracker.pipeline.models import JobContact, JobLead
from job_tracker.pipeline.store import add_job_contact, connect, list_job_documents, upsert_lead


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(conn, JobLead(company="Acme", title="Software Engineer", source_message_id="m1", source_label="single-jd"))
    conn.close()
    return db_path


@pytest.fixture()
def fake_generate(monkeypatch):
    calls: list[dict] = []

    def _fake(kind, **kwargs):
        calls.append({"kind": kind, **kwargs})
        return FollowupMessageResult(kind=kind, text="Hi,\n\nDraft body.\n\nShawn Becker", warnings=[])

    monkeypatch.setattr(generate_message_module, "generate_followup_message", _fake)
    return calls


def test_generate_message_thank_you_saves_document_and_prints_text(seeded_db: Path, tmp_path: Path, fake_generate, capsys):
    out_root = tmp_path / "out"
    rc = generate_message_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--kind", "thank_you", "--output-root", str(out_root),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Draft body." in out
    assert "Saved to" in out

    conn = connect(seeded_db)
    key = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Acme'").fetchone()["normalized_key"]
    docs = list_job_documents(conn, key)
    assert len(docs) == 1
    assert docs[0]["doc_type"] == "thank_you"
    assert Path(docs[0]["path_or_url"]).exists()
    conn.close()

    assert fake_generate[0]["kind"] == "thank_you"


def test_generate_message_auto_picks_most_recent_contact_name(seeded_db: Path, tmp_path: Path, fake_generate):
    conn = connect(seeded_db)
    key = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Acme'").fetchone()["normalized_key"]
    add_job_contact(conn, JobContact(job_key=key, name="Jane Doe", email="jane@acme.com"))
    conn.close()

    rc = generate_message_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--kind", "thank_you", "--output-root", str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    assert fake_generate[0]["contact_name"] == "Jane Doe"


def test_generate_message_contact_name_flag_overrides_auto_pick(seeded_db: Path, tmp_path: Path, fake_generate):
    conn = connect(seeded_db)
    key = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Acme'").fetchone()["normalized_key"]
    add_job_contact(conn, JobContact(job_key=key, name="Jane Doe", email="jane@acme.com"))
    conn.close()

    rc = generate_message_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--kind", "thank_you", "--contact-name", "Bob Smith", "--output-root", str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    assert fake_generate[0]["contact_name"] == "Bob Smith"


def test_generate_message_status_check_in_computes_days_since_contact(seeded_db: Path, tmp_path: Path, fake_generate):
    from datetime import datetime, timedelta, timezone

    conn = connect(seeded_db)
    twenty_days_ago = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    conn.execute(
        "UPDATE job_leads SET awaiting_response_since = ? WHERE company = 'Acme'", (twenty_days_ago,)
    )
    conn.commit()
    conn.close()

    rc = generate_message_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--kind", "status_check_in", "--output-root", str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    assert fake_generate[0]["days_since_contact"] == 20


def test_generate_message_thank_you_does_not_compute_days_since_contact(seeded_db: Path, tmp_path: Path, fake_generate):
    rc = generate_message_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--kind", "thank_you", "--output-root", str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    assert fake_generate[0]["days_since_contact"] is None


def test_generate_message_prints_warnings_to_stderr(seeded_db: Path, tmp_path: Path, monkeypatch, capsys):
    def _fake(kind, **kwargs):
        return FollowupMessageResult(kind=kind, text="Draft.", warnings=["possible compensation figure/rate/range found"])

    monkeypatch.setattr(generate_message_module, "generate_followup_message", _fake)

    rc = generate_message_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--kind", "thank_you", "--output-root", str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "compensation figure" in err


def test_generate_message_unknown_job_reports_error(seeded_db: Path, tmp_path: Path, fake_generate, capsys):
    rc = generate_message_main(
        [
            "--db", str(seeded_db), "--company", "Nonexistent Co", "--title", "Nowhere Job",
            "--kind", "thank_you", "--output-root", str(tmp_path / "out"),
        ]
    )
    assert rc == 1
    assert "No job found" in capsys.readouterr().err
    assert fake_generate == []


def test_days_since_helpers():
    from job_tracker.cli.generate_message import _days_since

    assert _days_since(None) is None
    assert _days_since("not-a-date") is None
    # naive ISO gets UTC
    assert _days_since("2020-01-01T00:00:00") is not None
    assert _days_since("2020-01-01T00:00:00+00:00") is not None


def test_generate_message_unknown_job_with_similar(seeded_db: Path, tmp_path: Path, fake_generate, capsys):
    rc = generate_message_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Enginer",
            "--kind", "thank_you", "--output-root", str(tmp_path / "out"),
        ]
    )
    assert rc == 1
    assert "Did you mean" in capsys.readouterr().err
