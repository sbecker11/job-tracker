"""Tests for the attach_document CLI (store RTR/other files as a JobDocument)."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.cli.attach_document import main as attach_document_main
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, list_job_documents, upsert_lead


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(conn, JobLead(company="Acme", title="Software Engineer", source_message_id="m1", source_label="single-jd"))
    conn.close()
    return db_path


def test_attach_document_stores_local_file_path(seeded_db: Path, tmp_path: Path):
    rtr_path = tmp_path / "signed_rtr.pdf"
    rtr_path.write_text("fake pdf contents", encoding="utf-8")

    rc = attach_document_main(
        ["--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer", "--doc-type", "rtr", "--file", str(rtr_path)]
    )
    assert rc == 0

    conn = connect(seeded_db)
    key = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Acme'").fetchone()["normalized_key"]
    docs = list_job_documents(conn, key)
    assert len(docs) == 1
    assert docs[0]["doc_type"] == "rtr"
    assert docs[0]["path_or_url"] == str(rtr_path)
    conn.close()


def test_attach_document_warns_but_still_stores_missing_file(seeded_db: Path, tmp_path: Path, capsys):
    missing_path = tmp_path / "does_not_exist.pdf"
    rc = attach_document_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--doc-type", "rtr", "--file", str(missing_path),
        ]
    )
    assert rc == 0
    assert "does not exist" in capsys.readouterr().err

    conn = connect(seeded_db)
    key = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Acme'").fetchone()["normalized_key"]
    assert len(list_job_documents(conn, key)) == 1
    conn.close()


def test_attach_document_accepts_url_instead_of_file(seeded_db: Path):
    rc = attach_document_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--doc-type", "jd_snapshot", "--url", "https://example.com/jd",
        ]
    )
    assert rc == 0

    conn = connect(seeded_db)
    key = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Acme'").fetchone()["normalized_key"]
    docs = list_job_documents(conn, key)
    assert docs[0]["path_or_url"] == "https://example.com/jd"
    conn.close()


def test_attach_document_versions_repeat_doc_types(seeded_db: Path, tmp_path: Path):
    f1 = tmp_path / "resume_v1.docx"
    f2 = tmp_path / "resume_v2.docx"
    f1.write_text("v1", encoding="utf-8")
    f2.write_text("v2", encoding="utf-8")

    attach_document_main(["--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer", "--doc-type", "resume", "--file", str(f1)])
    attach_document_main(["--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer", "--doc-type", "resume", "--file", str(f2)])

    conn = connect(seeded_db)
    key = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Acme'").fetchone()["normalized_key"]
    docs = sorted(list_job_documents(conn, key), key=lambda d: d["version"])
    assert [d["version"] for d in docs] == [1, 2]
    conn.close()


def test_attach_document_unknown_job_reports_error(seeded_db: Path, tmp_path: Path, capsys):
    f = tmp_path / "rtr.pdf"
    f.write_text("x", encoding="utf-8")
    rc = attach_document_main(
        [
            "--db", str(seeded_db), "--company", "Nonexistent Co", "--title", "Nowhere Job",
            "--doc-type", "rtr", "--file", str(f),
        ]
    )
    assert rc == 1
    assert "No job found" in capsys.readouterr().err


def test_attach_document_unknown_job_with_similar_suggestion(seeded_db: Path, tmp_path: Path, capsys):
    f = tmp_path / "rtr.pdf"
    f.write_text("x", encoding="utf-8")
    rc = attach_document_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Enginer",
            "--doc-type", "rtr", "--file", str(f),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "Did you mean" in err
