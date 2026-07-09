"""Tests for the SQLite-backed lead store, including schema migration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from job_tracker.pipeline.models import (
    JobContact,
    JobConversation,
    JobDocument,
    JobLead,
    JobMeeting,
    JobOffer,
)
from job_tracker.pipeline.store import (
    _SCHEMA,
    add_job_contact,
    add_job_conversation,
    add_job_document,
    add_job_meeting,
    add_job_offer,
    advance_status,
    connect,
    find_matching_job,
    find_similar_jobs,
    is_message_processed,
    processed_at,
    latest_conversation_at,
    list_job_contacts,
    list_job_conversations,
    list_job_documents,
    list_job_meetings,
    list_job_offers,
    record_message_processed,
    update_llm_evaluation,
    upsert_lead,
)


def test_update_llm_evaluation_persists_full_review_data(tmp_path: Path):
    """Regression test for the 2026-07-07 data-loss bug: match_pct,
    dealbreaker_checks, skills_alignment, flags, and framing_guidance must
    all survive into the llm_* columns, not just verdict/rationale."""
    from job_tracker.pipeline.llm_apply import CallMetrics, EvaluationResult

    conn = connect(tmp_path / "leads.db")
    upsert_lead(
        conn,
        JobLead(company="Acme", title="Engineer", source_message_id="m1", source_label="single-jd"),
    )

    evaluation = EvaluationResult(
        verdict="pursue",
        match_pct=92.0,
        job_summary="A greenfield agentic-AI team.",
        dealbreaker_checks=[{"check": "Banned stack", "status": "clean", "notes": "Python only."}],
        skills_alignment=[{"requirement": "Python", "evidence": "10 years.", "strength": "very_strong"}],
        flags=["Seniority mismatch."],
        rationale="Cleanest fit in the pipeline.",
        framing_guidance=["Lead with the healthcare throughline."],
        metrics=CallMetrics(step="evaluate", model="claude-sonnet-5", input_tokens=100, output_tokens=50, cost_usd=0.001),
    )
    key = JobLead(company="Acme", title="Engineer", source_message_id="m1", source_label="single-jd").normalized_key
    update_llm_evaluation(conn, key, evaluation)

    row = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()
    assert row["llm_verdict"] == "pursue"
    assert row["llm_match_pct"] == 92.0
    assert row["llm_job_summary"] == "A greenfield agentic-AI team."
    assert json.loads(row["llm_dealbreaker_notes"]) == [{"check": "Banned stack", "status": "clean", "notes": "Python only."}]
    assert json.loads(row["llm_skills_alignment"]) == [
        {"requirement": "Python", "evidence": "10 years.", "strength": "very_strong"}
    ]
    assert json.loads(row["llm_flags"]) == ["Seniority mismatch."]
    assert row["llm_rationale"] == "Cleanest fit in the pipeline."
    assert json.loads(row["llm_framing_guidance"]) == ["Lead with the healthcare throughline."]
    assert row["llm_eval_cost_usd"] == pytest.approx(0.001)
    conn.close()


def test_jd_text_persists_on_insert(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(
        conn,
        JobLead(
            company="Acme",
            title="Engineer",
            source_message_id="m1",
            source_label="single-jd",
            jd_resolved=True,
            jd_source="ats_api",
            jd_text="Full JD body text here.",
        ),
    )
    row = conn.execute("SELECT jd_text, jd_source FROM job_leads WHERE company='Acme'").fetchone()
    assert row["jd_text"] == "Full JD body text here."
    assert row["jd_source"] == "ats_api"
    conn.close()


def test_jd_text_refreshes_while_status_is_new(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = JobLead(
        company="Acme",
        title="Engineer",
        source_message_id="m1",
        source_label="single-jd",
        jd_resolved=False,
        jd_source="email_body",
        jd_text="First pass — only the email body was available.",
    )
    upsert_lead(conn, lead)

    # Re-seen later, this time the ATS resolved successfully with the real JD.
    better_lead = JobLead(
        company="Acme",
        title="Engineer",
        source_message_id="m2",
        source_label="single-jd",
        jd_resolved=True,
        jd_source="ats_api",
        jd_text="Second pass — full ATS-resolved JD text.",
    )
    is_new = upsert_lead(conn, better_lead)
    assert is_new is False

    row = conn.execute("SELECT jd_text, jd_source, jd_resolved FROM job_leads WHERE company='Acme'").fetchone()
    assert row["jd_text"] == "Second pass — full ATS-resolved JD text."
    assert row["jd_source"] == "ats_api"
    assert row["jd_resolved"] == 1
    conn.close()


def test_jd_text_preserved_once_status_is_no_longer_new(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(
        conn,
        JobLead(
            company="Acme",
            title="Engineer",
            source_message_id="m1",
            source_label="single-jd",
            jd_text="Original JD text the user already reviewed.",
        ),
    )
    conn.execute("UPDATE job_leads SET status = 'pursued' WHERE company = 'Acme'")
    conn.commit()

    # A later re-send of the same digest must not silently overwrite the JD
    # text (or anything else) once a human has started acting on the lead.
    upsert_lead(
        conn,
        JobLead(
            company="Acme",
            title="Engineer",
            source_message_id="m2",
            source_label="single-jd",
            jd_text="A different digest re-send's JD text.",
        ),
    )
    row = conn.execute("SELECT jd_text, status FROM job_leads WHERE company='Acme'").fetchone()
    assert row["jd_text"] == "Original JD text the user already reviewed."
    assert row["status"] == "pursued"
    conn.close()


def test_advance_status_stamps_matching_date_column(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(company="Acme", title="Engineer", source_message_id="m1", source_label="single-jd")
    upsert_lead(conn, lead)

    advance_status(conn, lead.normalized_key, "pursued", when="2026-07-01T00:00:00+00:00")
    row = conn.execute(
        "SELECT status, pursued_at, package_generated_at FROM job_leads WHERE normalized_key = ?",
        (lead.normalized_key,),
    ).fetchone()
    assert row["status"] == "pursued"
    assert row["pursued_at"] == "2026-07-01T00:00:00+00:00"
    assert row["package_generated_at"] is None
    conn.close()


def test_advance_status_never_overwrites_an_already_stamped_stage(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(company="Acme", title="Engineer", source_message_id="m1", source_label="single-jd")
    upsert_lead(conn, lead)

    advance_status(conn, lead.normalized_key, "applied", when="2026-07-01T00:00:00+00:00")
    advance_status(conn, lead.normalized_key, "applied", when="2026-07-15T00:00:00+00:00")
    row = conn.execute(
        "SELECT applied_at FROM job_leads WHERE normalized_key = ?", (lead.normalized_key,)
    ).fetchone()
    assert row["applied_at"] == "2026-07-01T00:00:00+00:00"
    conn.close()


def test_advance_status_rejects_unknown_stage(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(company="Acme", title="Engineer", source_message_id="m1", source_label="single-jd")
    upsert_lead(conn, lead)
    with pytest.raises(ValueError):
        advance_status(conn, lead.normalized_key, "not-a-real-stage")
    conn.close()


def test_processed_messages_round_trip(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    assert is_message_processed(conn, "msg-1") is False

    record_message_processed(
        conn,
        "msg-1",
        outcome="PURSUE",
        subject="Software Engineer @ Acme",
        from_address="noreply@greenhouse.io",
        lead_keys=["acme::software engineer"],
        label_applied="JobTracker/PURSUE",
        archived=True,
    )
    assert is_message_processed(conn, "msg-1") is True
    row = conn.execute("SELECT outcome, archived FROM processed_messages WHERE message_id = 'msg-1'").fetchone()
    assert row["outcome"] == "PURSUE"
    assert row["archived"] == 1
    conn.close()


def test_processed_at_returns_none_for_unknown_message(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    assert processed_at(conn, "never-seen") is None
    conn.close()


def test_processed_at_returns_stored_timestamp_for_resuming_a_forced_batch(tmp_path: Path):
    """Backs `--force-since` in triage_recruiter_inbox.py: resuming an
    interrupted --force batch needs to tell "already redone this session"
    apart from "stale row from before the batch started" by timestamp."""
    conn = connect(tmp_path / "leads.db")
    record_message_processed(
        conn,
        "msg-1",
        outcome="NEEDS_REVIEW",
        subject="Some digest",
        from_address="jobs@example.com",
        lead_keys=[],
        label_applied="JobTracker/NEEDS_REVIEW",
        archived=True,
    )
    ts = processed_at(conn, "msg-1")
    assert ts is not None
    assert ts >= "2020-01-01T00:00:00+00:00"  # sane ISO8601 UTC shape, not empty/garbage
    conn.close()


def test_connect_migrates_a_pre_existing_db_missing_jd_text_column(tmp_path: Path):
    """Regression: upgrading job-tracker must not require deleting var/leads.db."""
    db_path = tmp_path / "old_leads.db"

    # Simulate a database created before the jd_text column existed.
    legacy_schema = _SCHEMA.replace("    jd_text TEXT,\n", "")
    assert "jd_text" not in legacy_schema
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(legacy_schema)
    raw_conn.execute(
        """
        INSERT INTO job_leads (normalized_key, company, title, status, first_seen, last_seen)
        VALUES ('acme::engineer', 'Acme', 'Engineer', 'new', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """
    )
    raw_conn.commit()
    raw_conn.close()

    conn = connect(db_path)  # should migrate in place, not raise
    row = conn.execute("SELECT jd_text FROM job_leads WHERE company='Acme'").fetchone()
    assert row["jd_text"] is None
    conn.close()


def test_connect_migrates_legacy_approved_passed_columns_and_status_values(tmp_path: Path):
    """Regression for the 2026-07-07 approved/passed -> pursued/skipped rename
    (to match gmail_writer.PURSUE_LABEL/SKIP_LABEL): a pre-existing DB with the
    old approved_at/passed_at columns and 'approved'/'passed' status values
    must come up under connect() with the data intact under the new names,
    not silently orphaned or dropped."""
    db_path = tmp_path / "old_leads.db"

    legacy_schema = _SCHEMA.replace(
        "    status TEXT DEFAULT 'new',\n",
        "    status TEXT DEFAULT 'new',\n    approved_at TEXT,\n    passed_at TEXT,\n",
    )
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(legacy_schema)
    raw_conn.execute(
        """
        INSERT INTO job_leads (normalized_key, company, title, status, approved_at, first_seen, last_seen)
        VALUES ('acme::engineer', 'Acme', 'Engineer', 'approved', '2026-07-01T00:00:00+00:00',
                '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """
    )
    raw_conn.execute(
        """
        INSERT INTO job_leads (normalized_key, company, title, status, passed_at, first_seen, last_seen)
        VALUES ('globex::engineer', 'Globex', 'Engineer', 'passed', '2026-07-02T00:00:00+00:00',
                '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """
    )
    raw_conn.commit()
    raw_conn.close()

    conn = connect(db_path)  # should migrate in place, not raise
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(job_leads)")}
    assert "approved_at" not in existing
    assert "passed_at" not in existing

    pursued_row = conn.execute("SELECT status, pursued_at FROM job_leads WHERE company='Acme'").fetchone()
    assert pursued_row["status"] == "pursued"
    assert pursued_row["pursued_at"] == "2026-07-01T00:00:00+00:00"

    skipped_row = conn.execute("SELECT status, skipped_at FROM job_leads WHERE company='Globex'").fetchone()
    assert skipped_row["status"] == "skipped"
    assert skipped_row["skipped_at"] == "2026-07-02T00:00:00+00:00"
    conn.close()


# --- Job CRM entities (docs/JOB_CRM_VISION.md) ------------------------------


def _seed_lead(conn, *, company="Acme", title="Software Engineer") -> str:
    lead = JobLead(company=company, title=title, source_message_id="m1", source_label="single-jd")
    upsert_lead(conn, lead)
    return lead.normalized_key


def test_add_job_contact_dedupes_on_email_within_a_job(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _seed_lead(conn)

    id1 = add_job_contact(conn, JobContact(job_key=key, email="Recruiter@Agency.com", role="recruiter"))
    id2 = add_job_contact(conn, JobContact(job_key=key, email="recruiter@agency.com", role="recruiter"))

    assert id1 == id2
    contacts = list_job_contacts(conn, key)
    assert len(contacts) == 1
    conn.close()


def test_add_job_contact_allows_multiple_distinct_contacts_per_job(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _seed_lead(conn)

    add_job_contact(conn, JobContact(job_key=key, email="agency1@example.com"))
    add_job_contact(conn, JobContact(job_key=key, email="agency2@example.com"))

    assert len(list_job_contacts(conn, key)) == 2
    conn.close()


def test_job_conversation_round_trip_and_latest_conversation_at(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _seed_lead(conn)
    contact_id = add_job_contact(conn, JobContact(job_key=key, email="r@agency.com"))

    add_job_conversation(
        conn,
        JobConversation(job_key=key, contact_id=contact_id, message_id="m1", occurred_at="2026-01-01T00:00:00+00:00"),
    )
    add_job_conversation(
        conn,
        JobConversation(job_key=key, contact_id=contact_id, message_id="m2", occurred_at="2026-02-01T00:00:00+00:00"),
    )

    conversations = list_job_conversations(conn, key)
    assert len(conversations) == 2
    assert latest_conversation_at(conn, key) == "2026-02-01T00:00:00+00:00"
    conn.close()


def test_job_document_auto_increments_version_per_doc_type(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _seed_lead(conn)

    add_job_document(conn, JobDocument(job_key=key, doc_type="resume", path_or_url="/tmp/resume_v1.docx"))
    add_job_document(conn, JobDocument(job_key=key, doc_type="resume", path_or_url="/tmp/resume_v2.docx"))
    add_job_document(conn, JobDocument(job_key=key, doc_type="jd_snapshot", path_or_url="/tmp/jd.txt"))

    docs = {(d["doc_type"], d["version"]): d["path_or_url"] for d in list_job_documents(conn, key)}
    assert docs[("resume", 1)] == "/tmp/resume_v1.docx"
    assert docs[("resume", 2)] == "/tmp/resume_v2.docx"
    assert docs[("jd_snapshot", 1)] == "/tmp/jd.txt"
    conn.close()


def test_job_meeting_round_trip(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _seed_lead(conn)
    contact_id = add_job_contact(conn, JobContact(job_key=key, email="r@agency.com"))

    add_job_meeting(
        conn,
        JobMeeting(job_key=key, contact_id=contact_id, kind="phone_screen", status="confirmed", scheduled_at="2026-03-01T15:00:00+00:00"),
    )

    meetings = list_job_meetings(conn, key)
    assert len(meetings) == 1
    assert meetings[0]["kind"] == "phone_screen"
    assert meetings[0]["status"] == "confirmed"
    conn.close()


def test_job_offer_round_trip_and_listing_across_jobs(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key_a = _seed_lead(conn, company="Acme", title="Engineer")
    key_b = _seed_lead(conn, company="Globex", title="Engineer II")

    add_job_offer(conn, JobOffer(job_key=key_a, base_salary=150000, bonus=10000))
    add_job_offer(conn, JobOffer(job_key=key_b, base_salary=160000, bonus=5000))

    assert len(list_job_offers(conn)) == 2
    assert len(list_job_offers(conn, key_a)) == 1
    assert list_job_offers(conn, key_a)[0]["base_salary"] == 150000
    conn.close()


def test_find_matching_job_exact_normalized_key(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    _seed_lead(conn, company="Acme Inc.", title="Software Engineer")

    match = find_matching_job(conn, "Acme Inc", "Software Engineer")
    assert match is not None
    assert match.company == "Acme Inc."


def test_find_matching_job_fuzzy_title_variant_clears_auto_merge_threshold(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    _seed_lead(conn, company="Acme Corporation", title="Senior Software Engineer")

    # Close enough title phrasing from a second recruiter pitching the same role.
    match = find_matching_job(conn, "Acme Corporation", "Senior Software Engineer II")
    assert match is not None
    conn.close()


def test_find_matching_job_returns_none_for_unrelated_role(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    _seed_lead(conn, company="Acme Corporation", title="Senior Software Engineer")

    match = find_matching_job(conn, "Totally Different Co", "Marketing Manager")
    assert match is None
    conn.close()


def test_find_similar_jobs_surfaces_ambiguous_candidates_without_auto_merging(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    _seed_lead(conn, company="Acme Corporation", title="Senior Software Engineer")

    # "Sr" vs "Senior" phrasing is close enough to surface as a candidate,
    # but not confident enough to auto-merge without confirmation.
    candidates = find_similar_jobs(conn, "Acme Corporation", "Sr Software Engineer")
    assert len(candidates) == 1
    assert candidates[0].combined_score < 0.92
    assert find_matching_job(conn, "Acme Corporation", "Sr Software Engineer") is None
    conn.close()
