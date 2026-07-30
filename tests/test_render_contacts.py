"""Tests for scripts/render_contacts.py — the static, client-side-searchable
"a recruiter I've worked with before just called" lookup page (2026-07-24).

render_contacts.py lives in scripts/, not src/job_tracker/, so it isn't on
pytest's `pythonpath = ["src"]` — loaded here via importlib instead of a
normal import (same approach as test_render_pending_actions.py).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from job_tracker.pipeline.models import JobContact, JobLead
from job_tracker.pipeline.store import add_job_contact, connect, upsert_lead

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_contacts.py"
_spec = importlib.util.spec_from_file_location("render_contacts", _SCRIPT_PATH)
render_contacts = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("render_contacts", render_contacts)
assert _spec.loader is not None
_spec.loader.exec_module(render_contacts)


def _seed_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    directv = JobLead(company="DIRECTV", title="Senior Data Engineer", source_message_id="m1", source_label="single-jd")
    initech = JobLead(company="Initech", title="Backend Engineer", source_message_id="m2", source_label="single-jd")
    upsert_lead(conn, directv)
    upsert_lead(conn, initech)
    add_job_contact(
        conn,
        JobContact(
            job_key=directv.normalized_key,
            name="Cole Keener",
            email="cole@crbworkforce.com",
            phone="530-722-7548",
            role="recruiter",
        ),
    )
    add_job_contact(
        conn,
        JobContact(
            job_key=initech.normalized_key,
            name="Jane Doe",
            email="jane@initech.com",
            phone="555-1111",
            role="hiring_manager",
        ),
    )
    # add_job_contact stamps first/last_contacted_at with "now" — override
    # directly so the recency-sort test below has a deterministic order.
    conn.execute(
        "UPDATE job_contacts SET last_contacted_at = ? WHERE lower(email) = ?",
        ("2026-07-22T17:17:47+00:00", "cole@crbworkforce.com"),
    )
    conn.execute(
        "UPDATE job_contacts SET last_contacted_at = ? WHERE lower(email) = ?",
        ("2026-06-05T00:00:00+00:00", "jane@initech.com"),
    )
    conn.commit()
    conn.close()
    return db_path


def test_to_rows_maps_every_contact_field():
    contacts = [
        {
            "name": "Cole Keener",
            "job_company": "DIRECTV",
            "job_title": "Senior Data Engineer",
            "role": "recruiter",
            "phone": "530-722-7548",
            "email": "cole@crbworkforce.com",
            "last_contacted_at": "2026-07-22T17:17:47+00:00",
        }
    ]
    # No `conn` passed here (no DB needed) — folderPath stays "" in that
    # case; test_folder_path_matches_single_vs_multi_lead_naming below
    # covers the actual folder-path derivation against a real DB.
    rows = render_contacts._to_rows(contacts)
    assert rows == [
        {
            "name": "Cole Keener",
            "company": "DIRECTV",
            "title": "Senior Data Engineer",
            "role": "recruiter",
            "phone": "530-722-7548",
            "email": "cole@crbworkforce.com",
            "lastContactedAt": "2026-07-22T17:17:47+00:00",
            "folderPath": "",
        }
    ]


def test_folder_path_flat_for_single_lead_company(tmp_path: Path):
    db_path = _seed_db(tmp_path)
    conn = connect(db_path)
    try:
        # DIRECTV has exactly one lead in _seed_db -> flat company folder.
        assert render_contacts._folder_path(conn, company="DIRECTV", title="Senior Data Engineer") == "DIRECTV"
    finally:
        conn.close()


def test_folder_path_nested_for_multi_lead_company(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    try:
        acme = JobLead(company="Acme", title="Backend Engineer", source_message_id="m1", source_label="single-jd")
        acme2 = JobLead(company="Acme", title="Frontend Engineer", source_message_id="m2", source_label="single-jd")
        upsert_lead(conn, acme)
        upsert_lead(conn, acme2)
        assert render_contacts._folder_path(conn, company="Acme", title="Backend Engineer") == "Acme/Acme_Backend_Engineer"
    finally:
        conn.close()


def test_to_rows_sorts_most_recently_contacted_first():
    contacts = [
        {"name": "Older", "job_company": "A", "job_title": "T", "role": "recruiter", "phone": "", "email": "",
         "last_contacted_at": "2026-06-01T00:00:00+00:00"},
        {"name": "Newer", "job_company": "B", "job_title": "T", "role": "recruiter", "phone": "", "email": "",
         "last_contacted_at": "2026-07-22T00:00:00+00:00"},
    ]
    rows = render_contacts._to_rows(contacts)
    assert [r["name"] for r in rows] == ["Newer", "Older"]


def test_to_rows_handles_missing_fields_without_raising():
    contacts = [{"name": None, "job_company": "A", "job_title": "T", "role": None, "phone": None, "email": None,
                 "last_contacted_at": None}]
    rows = render_contacts._to_rows(contacts)
    assert rows[0]["name"] == ""
    assert rows[0]["role"] == ""
    assert rows[0]["phone"] == ""


def test_main_writes_html_with_every_contact_embedded(tmp_path: Path):
    db_path = _seed_db(tmp_path)
    out_path = tmp_path / "contacts.html"

    rc = render_contacts.main(["--db", str(db_path), "--output", str(out_path)])
    assert rc == 0

    html = out_path.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "Cole Keener" in html
    assert "Jane Doe" in html
    assert "cole@crbworkforce.com" in html
    # No unreplaced Python-side ${PLACEHOLDER} markers leaked into the
    # output — JS template-literal `${...}` expressions (e.g. `${c.name}`
    # inside rowHtml()) are legitimate and expected to remain.
    assert "${GENERATED_AT}" not in html
    assert "${FOOTER_NOTE}" not in html
    assert "${CONTACTS_JSON}" not in html
    assert "${FOLDER_ROOT}" not in html


def test_main_embedded_json_round_trips_and_is_recency_sorted(tmp_path: Path):
    db_path = _seed_db(tmp_path)
    out_path = tmp_path / "contacts.html"
    render_contacts.main(["--db", str(db_path), "--output", str(out_path)])

    html = out_path.read_text(encoding="utf-8")
    marker = "const CONTACTS = "
    start = html.index(marker) + len(marker)
    end = html.index(";\n", start)
    rows = json.loads(html[start:end])

    assert len(rows) == 2
    assert rows[0]["name"] == "Cole Keener"  # last_contacted_at 2026-07-22, more recent than Jane Doe's 2026-06-05
    assert rows[1]["name"] == "Jane Doe"
    # Both DIRECTV and Initech are single-lead companies in _seed_db -> flat folders.
    assert rows[0]["folderPath"] == "DIRECTV"
    assert rows[1]["folderPath"] == "Initech"


def test_main_errors_cleanly_when_db_missing(tmp_path: Path, capsys):
    missing_db = tmp_path / "does-not-exist.db"
    rc = render_contacts.main(["--db", str(missing_db), "--output", str(tmp_path / "out.html")])
    assert rc == 1
    assert "No leads DB found" in capsys.readouterr().err
