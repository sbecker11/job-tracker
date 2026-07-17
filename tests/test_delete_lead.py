"""Tests for soft-delete / unavailable / hired / hard-purge of job leads."""

from __future__ import annotations

import pytest
from pathlib import Path

from job_tracker.cli.delete_lead import main as delete_lead_main
from job_tracker.cli.list_leads import main as list_leads_main
from job_tracker.pipeline.models import LEAD_STAGES, JobContact, JobConversation, JobLead
from job_tracker.pipeline.store import (
    add_job_contact,
    add_job_conversation,
    connect,
    list_leads,
    mark_lead_deleted,
    mark_lead_hired,
    mark_lead_unavailable,
    set_awaiting_response,
    upsert_lead,
)


def _seed(
    db_path: Path,
    *,
    company: str = "Acme",
    title: str = "Software Engineer",
    message_id: str = "m-del",
) -> str:
    conn = connect(db_path)
    lead = JobLead(
        company=company,
        title=title,
        source_message_id=message_id,
        source_label="manual",
        match_pct=50.0,
        verdict="pursue",
    )
    upsert_lead(conn, lead)
    add_job_contact(
        conn,
        JobContact(job_key=lead.normalized_key, name="Pat Recruiter", email="pat@agency.com"),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            channel="email",
            direction="inbound",
            summary="initial ping",
        ),
    )
    conn.close()
    return lead.normalized_key


# --- store API ---------------------------------------------------------------


def test_lead_stages_include_hide_off_ramps():
    assert "deleted" in LEAD_STAGES
    assert "unavailable" in LEAD_STAGES
    assert "hired" in LEAD_STAGES
    # Happy-path hire for *this* lead stays distinct from the hide off-ramp.
    assert "accepted" in LEAD_STAGES
    assert "started" in LEAD_STAGES


def test_mark_lead_deleted_store_api(tmp_path: Path):
    db = tmp_path / "leads.db"
    key = _seed(db)
    conn = connect(db)
    set_awaiting_response(conn, key, True)
    mark_lead_deleted(conn, key, when="2026-07-16T12:00:00+00:00", reason="duplicate")

    row = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()
    assert row["status"] == "deleted"
    assert row["deleted_at"] == "2026-07-16T12:00:00+00:00"
    assert row["awaiting_response_since"] is None
    assert list_leads(conn) == []
    assert len(list_leads(conn, include_deleted=True)) == 1
    summaries = [
        r["summary"]
        for r in conn.execute(
            "SELECT summary FROM job_conversations WHERE job_key = ?", (key,)
        )
    ]
    assert "deleted: duplicate" in summaries
    # CRM children kept
    assert conn.execute("SELECT COUNT(*) FROM job_contacts WHERE job_key = ?", (key,)).fetchone()[0] == 1
    conn.close()


def test_mark_lead_deleted_without_reason_skips_conversation_note(tmp_path: Path):
    db = tmp_path / "leads.db"
    key = _seed(db)
    conn = connect(db)
    before = conn.execute(
        "SELECT COUNT(*) FROM job_conversations WHERE job_key = ?", (key,)
    ).fetchone()[0]
    mark_lead_deleted(conn, key)
    after = conn.execute(
        "SELECT COUNT(*) FROM job_conversations WHERE job_key = ?", (key,)
    ).fetchone()[0]
    assert after == before
    assert conn.execute(
        "SELECT status FROM job_leads WHERE normalized_key = ?", (key,)
    ).fetchone()[0] == "deleted"
    conn.close()


def test_mark_lead_unavailable_store_api(tmp_path: Path):
    db = tmp_path / "leads.db"
    key = _seed(db)
    conn = connect(db)
    mark_lead_unavailable(conn, key, when="2026-07-15", reason="withdrawn")

    row = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()
    assert row["status"] == "unavailable"
    assert row["unavailable_at"] == "2026-07-15"
    summaries = [
        r["summary"]
        for r in conn.execute(
            "SELECT summary FROM job_conversations WHERE job_key = ?", (key,)
        )
    ]
    assert "unavailable: withdrawn" in summaries
    conn.close()


def test_mark_lead_unavailable_default_reason(tmp_path: Path):
    db = tmp_path / "leads.db"
    key = _seed(db)
    conn = connect(db)
    mark_lead_unavailable(conn, key)
    summaries = [
        r["summary"]
        for r in conn.execute(
            "SELECT summary FROM job_conversations WHERE job_key = ?", (key,)
        )
    ]
    assert "unavailable: no longer available" in summaries
    conn.close()


def test_mark_lead_hired_store_api(tmp_path: Path):
    db = tmp_path / "leads.db"
    key = _seed(db)
    conn = connect(db)
    mark_lead_hired(conn, key, when="2026-07-14T09:00:00+00:00", reason="took other offer")

    row = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()
    assert row["status"] == "hired"
    assert row["hired_at"] == "2026-07-14T09:00:00+00:00"
    summaries = [
        r["summary"]
        for r in conn.execute(
            "SELECT summary FROM job_conversations WHERE job_key = ?", (key,)
        )
    ]
    assert "hired: took other offer" in summaries
    conn.close()


def test_mark_lead_hired_default_reason(tmp_path: Path):
    db = tmp_path / "leads.db"
    key = _seed(db)
    conn = connect(db)
    mark_lead_hired(conn, key)
    summaries = [
        r["summary"]
        for r in conn.execute(
            "SELECT summary FROM job_conversations WHERE job_key = ?", (key,)
        )
    ]
    assert "hired: already hired" in summaries
    conn.close()


def test_list_leads_hides_all_three_off_ramps(tmp_path: Path):
    db = tmp_path / "leads.db"
    k1 = _seed(db, company="A", title="T1", message_id="m1")
    k2 = _seed(db, company="B", title="T2", message_id="m2")
    k3 = _seed(db, company="C", title="T3", message_id="m3")
    _seed(db, company="D", title="T4", message_id="m4")  # stays active

    conn = connect(db)
    mark_lead_deleted(conn, k1, reason="dup")
    mark_lead_unavailable(conn, k2)
    mark_lead_hired(conn, k3)

    visible = list_leads(conn)
    assert len(visible) == 1
    assert visible[0]["company"] == "D"

    all_rows = list_leads(conn, include_deleted=True)
    assert len(all_rows) == 4
    assert {r["status"] for r in all_rows} == {"deleted", "unavailable", "hired", "new"}
    conn.close()


# --- CLI ---------------------------------------------------------------------


def test_soft_delete_hides_from_default_list(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    _seed(db)

    rc = delete_lead_main(
        ["--db", str(db), "--company", "Acme", "--title", "Software Engineer", "--reason", "duplicate"]
    )
    assert rc == 0
    assert "Marked deleted" in capsys.readouterr().out

    conn = connect(db)
    assert list_leads(conn) == []
    deleted = list_leads(conn, include_deleted=True)
    assert len(deleted) == 1
    assert deleted[0]["status"] == "deleted"
    assert deleted[0]["deleted_at"] is not None
    assert deleted[0]["awaiting_response_since"] is None
    convs = conn.execute(
        "SELECT summary FROM job_conversations WHERE job_key = ? ORDER BY id",
        (deleted[0]["normalized_key"],),
    ).fetchall()
    assert any("deleted: duplicate" in (c["summary"] or "") for c in convs)
    conn.close()

    rc = list_leads_main(["--db", str(db)])
    assert rc == 0
    assert "Acme" not in capsys.readouterr().out

    rc = list_leads_main(["--db", str(db), "--status", "deleted"])
    assert rc == 0
    assert "Acme" in capsys.readouterr().out


def test_unavailable_hides_from_default_list(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    _seed(db)

    rc = delete_lead_main(
        ["--db", str(db), "--company", "Acme", "--title", "Software Engineer", "--unavailable"]
    )
    assert rc == 0
    assert "Marked unavailable" in capsys.readouterr().out

    conn = connect(db)
    assert list_leads(conn) == []
    hidden = list_leads(conn, include_deleted=True)
    assert len(hidden) == 1
    assert hidden[0]["status"] == "unavailable"
    assert hidden[0]["unavailable_at"] is not None
    assert hidden[0]["awaiting_response_since"] is None
    convs = conn.execute(
        "SELECT summary FROM job_conversations WHERE job_key = ? ORDER BY id",
        (hidden[0]["normalized_key"],),
    ).fetchall()
    assert any("unavailable: no longer available" in (c["summary"] or "") for c in convs)
    conn.close()

    rc = list_leads_main(["--db", str(db), "--status", "unavailable"])
    assert rc == 0
    assert "Acme" in capsys.readouterr().out


def test_already_hired_hides_from_default_list(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    _seed(db)

    rc = delete_lead_main(
        ["--db", str(db), "--company", "Acme", "--title", "Software Engineer", "--already-hired"]
    )
    assert rc == 0
    assert "Marked hired" in capsys.readouterr().out

    conn = connect(db)
    assert list_leads(conn) == []
    hidden = list_leads(conn, include_deleted=True)
    assert len(hidden) == 1
    assert hidden[0]["status"] == "hired"
    assert hidden[0]["hired_at"] is not None
    assert hidden[0]["awaiting_response_since"] is None
    convs = conn.execute(
        "SELECT summary FROM job_conversations WHERE job_key = ? ORDER BY id",
        (hidden[0]["normalized_key"],),
    ).fetchall()
    assert any("hired: already hired" in (c["summary"] or "") for c in convs)
    conn.close()

    rc = list_leads_main(["--db", str(db), "--status", "hired"])
    assert rc == 0
    assert "Acme" in capsys.readouterr().out


def test_cli_custom_reason_and_on_timestamp(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    _seed(db)

    rc = delete_lead_main(
        [
            "--db",
            str(db),
            "--company",
            "Acme",
            "--title",
            "Software Engineer",
            "--already-hired",
            "--reason",
            "accepted Waystar",
            "--on",
            "2026-07-10",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Marked hired" in out
    assert "accepted Waystar" in out

    conn = connect(db)
    row = conn.execute("SELECT status, hired_at FROM job_leads").fetchone()
    assert row["status"] == "hired"
    assert row["hired_at"] == "2026-07-10"
    conn.close()


def test_cli_idempotent_already_deleted(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    _seed(db)
    args = ["--db", str(db), "--company", "Acme", "--title", "Software Engineer"]
    assert delete_lead_main(args) == 0
    capsys.readouterr()
    assert delete_lead_main(args) == 0
    assert "Already deleted" in capsys.readouterr().out


def test_cli_idempotent_already_unavailable(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    _seed(db)
    args = [
        "--db",
        str(db),
        "--company",
        "Acme",
        "--title",
        "Software Engineer",
        "--unavailable",
    ]
    assert delete_lead_main(args) == 0
    capsys.readouterr()
    assert delete_lead_main(args) == 0
    assert "Already unavailable" in capsys.readouterr().out


def test_cli_idempotent_already_hired(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    _seed(db)
    args = [
        "--db",
        str(db),
        "--company",
        "Acme",
        "--title",
        "Software Engineer",
        "--already-hired",
    ]
    assert delete_lead_main(args) == 0
    capsys.readouterr()
    assert delete_lead_main(args) == 0
    assert "Already marked hired" in capsys.readouterr().out


def test_cli_missing_job(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    connect(db).close()  # empty schema
    rc = delete_lead_main(
        ["--db", str(db), "--company", "Nope", "--title", "Ghost Role"]
    )
    assert rc == 1
    assert "No job found" in capsys.readouterr().err


def test_cli_missing_db(tmp_path: Path, capsys):
    rc = delete_lead_main(
        ["--db", str(tmp_path / "missing.db"), "--company", "Acme", "--title", "SE"]
    )
    assert rc == 1
    assert "No leads DB found" in capsys.readouterr().err


def test_cli_mutually_exclusive_flags(tmp_path: Path):
    db = tmp_path / "leads.db"
    _seed(db)
    with pytest.raises(SystemExit):
        delete_lead_main(
            [
                "--db",
                str(db),
                "--company",
                "Acme",
                "--title",
                "Software Engineer",
                "--unavailable",
                "--already-hired",
            ]
        )


def test_purge_removes_lead_and_children(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    key = _seed(db)

    rc = delete_lead_main(
        ["--db", str(db), "--company", "Acme", "--title", "Software Engineer", "--purge"]
    )
    assert rc == 1  # needs --yes
    assert "requires --yes" in capsys.readouterr().err

    rc = delete_lead_main(
        ["--db", str(db), "--company", "Acme", "--title", "Software Engineer", "--purge", "--yes"]
    )
    assert rc == 0
    assert "Purged" in capsys.readouterr().out

    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM job_contacts WHERE job_key = ?", (key,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM job_conversations WHERE job_key = ?", (key,)).fetchone()[0] == 0
    conn.close()


def test_restore_via_set_status_brings_back_to_default_list(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    _seed(db)
    assert (
        delete_lead_main(
            [
                "--db",
                str(db),
                "--company",
                "Acme",
                "--title",
                "Software Engineer",
                "--unavailable",
            ]
        )
        == 0
    )
    capsys.readouterr()

    rc = list_leads_main(
        [
            "--db",
            str(db),
            "--company",
            "Acme",
            "--title",
            "Software Engineer",
            "--set-status",
            "applied",
            "--on",
            "2026-07-16",
        ]
    )
    assert rc == 0

    conn = connect(db)
    visible = list_leads(conn)
    assert len(visible) == 1
    assert visible[0]["status"] == "applied"
    assert visible[0]["applied_at"] == "2026-07-16"
    # Original unavailable_at kept (COALESCE — first stamp wins)
    assert visible[0]["unavailable_at"] is not None
    conn.close()


def test_list_leads_include_deleted_flag(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    _seed(db)
    delete_lead_main(
        ["--db", str(db), "--company", "Acme", "--title", "Software Engineer", "--already-hired"]
    )
    capsys.readouterr()

    rc = list_leads_main(["--db", str(db), "--include-deleted"])
    assert rc == 0
    assert "Acme" in capsys.readouterr().out
