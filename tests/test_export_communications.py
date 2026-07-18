"""Tests for the on-demand communications PDF export CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.cli.export_communications import main as export_main
from job_tracker.pipeline.models import JobConversation, JobLead
from job_tracker.pipeline.store import add_job_conversation, connect, upsert_lead


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = JobLead(company="Acme", title="Engineer", source_message_id="m1", source_label="single-jd")
    upsert_lead(conn, lead)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="inbound",
            summary="They reached out",
            body_text="Hi Shawn, we'd love to chat about a role. \u2014 Recruiter",
        ),
    )
    conn.close()
    return db_path


def test_export_writes_pdf(seeded_db: Path, tmp_path: Path, capsys):
    out_root = tmp_path / "out"
    rc = export_main(["--db", str(seeded_db), "--company", "Acme", "--title", "Engineer", "--output-root", str(out_root)])
    assert rc == 0

    pdf_path = out_root / "Acme" / "communications" / "Communications_Engineer.pdf"
    assert pdf_path.is_file()
    assert pdf_path.stat().st_size > 0
    assert "Exported 1 conversation" in capsys.readouterr().out


def test_export_no_conversations_is_a_noop(tmp_path: Path, capsys):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(conn, JobLead(company="NoConvos", title="Role", source_message_id="m1", source_label="single-jd"))
    conn.close()

    rc = export_main(["--db", str(db_path), "--company", "NoConvos", "--title", "Role", "--output-root", str(tmp_path / "out")])
    assert rc == 0
    assert "nothing to export" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_export_unknown_job_errors(tmp_path: Path, capsys):
    db_path = tmp_path / "leads.db"
    connect(db_path).close()
    rc = export_main(["--db", str(db_path), "--company", "Nope", "--title", "Nope", "--output-root", str(tmp_path / "out")])
    assert rc == 1
    assert "No job found" in capsys.readouterr().err
