"""Tests for the list_leads review/export CLI."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from job_tracker.cli.list_leads import main as list_leads_main
from job_tracker.pipeline.llm_apply import CallMetrics, EvaluationResult
from job_tracker.pipeline.models import JobContact, JobConversation, JobLead
from job_tracker.pipeline.store import add_job_contact, add_job_conversation, connect, update_llm_evaluation, upsert_lead


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(
        conn,
        JobLead(
            company="Stripe",
            title="Software Engineer",
            source_message_id="m1",
            source_label="single-jd",
            match_pct=42.0,
            matched_skills=["python", "aws"],
            verdict="pursue",
            rationale=["Match 42.0%"],
            jd_resolved=True,
            jd_source="ats_api",
            jd_text="Stripe is hiring a Software Engineer.\n\nResponsibilities:\n- Build APIs",
        ),
    )
    upsert_lead(
        conn,
        JobLead(
            company="BigCorp",
            title="Java Developer",
            source_message_id="m2",
            source_label="single-jd",
            match_pct=2.0,
            matched_skills=[],
            verdict="pass",
            rationale=["Match 2.0%"],
        ),
    )
    conn.close()
    return db_path


def test_list_leads_filters_by_verdict(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--verdict", "pursue"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stripe" in out
    assert "BigCorp" not in out


def test_list_leads_json_output(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--json"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert any(r["matched_skills"] == ["python", "aws"] for r in rows)


def test_list_leads_csv_export(seeded_db: Path, tmp_path: Path):
    csv_path = tmp_path / "out.csv"
    rc = list_leads_main(["--db", str(seeded_db), "--csv", str(csv_path)])
    assert rc == 0
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert {r["company"] for r in rows} == {"Stripe", "BigCorp"}


def test_list_leads_set_status(seeded_db: Path):
    rc = list_leads_main(["--db", str(seeded_db), "--verdict", "pursue", "--set-status", "pursued"])
    assert rc == 0

    conn = connect(seeded_db)
    row = conn.execute("SELECT status, pursued_at FROM job_leads WHERE company = 'Stripe'").fetchone()
    assert row["status"] == "pursued"
    assert row["pursued_at"] is not None
    other = conn.execute("SELECT status FROM job_leads WHERE company = 'BigCorp'").fetchone()
    assert other["status"] == "new"
    conn.close()


def test_list_leads_missing_db_reports_error(tmp_path: Path, capsys):
    rc = list_leads_main(["--db", str(tmp_path / "nope.db")])
    assert rc == 1


def test_list_leads_filters_by_company_and_title(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--company", "strip", "--title", "software"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stripe" in out
    assert "BigCorp" not in out


def test_list_leads_json_includes_jd_text(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--company", "Stripe", "--json"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert "Build APIs" in rows[0]["jd_text"]
    assert rows[0]["jd_source"] == "ats_api"


def test_list_leads_show_jd_text_prints_full_text(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--company", "Stripe", "--show-jd-text"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stripe is hiring a Software Engineer." in out
    assert "- Build APIs" in out


def test_list_leads_show_jd_text_handles_missing_text(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--company", "BigCorp", "--show-jd-text"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no JD text stored" in out


def test_list_leads_show_review_renders_stored_llm_evaluation(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    update_llm_evaluation(
        conn,
        JobLead(company="Stripe", title="Software Engineer", source_message_id="m1", source_label="single-jd").normalized_key,
        EvaluationResult(
            verdict="pursue",
            match_pct=88.0,
            job_summary="Building payments infra APIs.",
            dealbreaker_checks=[{"check": "Banned stack", "status": "clean", "notes": "Python only."}],
            skills_alignment=[{"requirement": "APIs", "evidence": "Years of API work.", "strength": "strong"}],
            flags=["Fully remote but HQ-centric culture."],
            rationale="Strong overall fit.",
            framing_guidance=["Lead with distributed-systems experience."],
            metrics=CallMetrics(step="evaluate", model="claude-sonnet-5"),
        ),
    )
    conn.close()

    rc = list_leads_main(["--db", str(seeded_db), "--company", "Stripe", "--show-review"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Software Engineer @ Stripe" in out
    assert "Building payments infra APIs." in out
    assert "Banned stack" in out
    assert "Recommendation: PURSUE" in out
    assert "distributed-systems experience" in out


def test_list_leads_show_review_handles_not_yet_evaluated(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--company", "BigCorp", "--show-review"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not yet LLM-evaluated" in out


def test_list_leads_show_review_handles_legacy_flat_string_notes(seeded_db: Path, capsys):
    """Regression: leads evaluated before the 2026-07-07 richer-schema change
    stored llm_dealbreaker_notes/llm_skills_alignment as flat lists of prose
    strings rather than {check/status/notes}/{requirement/evidence/strength}
    dicts. render_jd_review() indexes into each entry with .get(...), which
    raises AttributeError on a bare str — --show-review must degrade
    gracefully (showing the legacy text) instead of crashing."""
    conn = connect(seeded_db)
    key = JobLead(
        company="Stripe", title="Software Engineer", source_message_id="m1", source_label="single-jd"
    ).normalized_key
    conn.execute(
        """
        UPDATE job_leads
        SET llm_verdict = 'pursue', llm_match_pct = 70.0,
            llm_dealbreaker_notes = ?, llm_skills_alignment = ?,
            llm_rationale = 'Legacy-format review.'
        WHERE normalized_key = ?
        """,
        (
            json.dumps(["No C2C-only requirement identified."]),
            json.dumps(["Backend APIs -> strong evidence in prior roles."]),
            key,
        ),
    )
    conn.commit()
    conn.close()

    rc = list_leads_main(["--db", str(seeded_db), "--company", "Stripe", "--show-review"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No C2C-only requirement identified." in out
    assert "Backend APIs -> strong evidence in prior roles." in out
    assert "Recommendation: PURSUE" in out


def test_list_leads_show_contacts_prints_tracked_contacts(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    key = JobLead(company="Stripe", title="Software Engineer", source_message_id="m1", source_label="single-jd").normalized_key
    add_job_contact(conn, JobContact(job_key=key, name="Jane Doe", email="jane@stripe.com", phone="555-1234", role="recruiter"))
    conn.close()

    rc = list_leads_main(["--db", str(seeded_db), "--company", "Stripe", "--show-contacts"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Software Engineer @ Stripe" in out
    assert "Jane Doe" in out
    assert "555-1234" in out
    assert "jane@stripe.com" in out


def test_list_leads_show_contacts_handles_no_contacts(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--company", "BigCorp", "--show-contacts"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no contacts tracked" in out


def test_list_leads_show_communications_prints_conversation_history(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    key = JobLead(company="Stripe", title="Software Engineer", source_message_id="m1", source_label="single-jd").normalized_key
    contact_id = add_job_contact(conn, JobContact(job_key=key, name="Jane Doe", email="jane@stripe.com", role="recruiter"))
    add_job_conversation(
        conn,
        JobConversation(
            job_key=key,
            contact_id=contact_id,
            message_id="msg-1",
            direction="inbound",
            summary="Following up on your application",
            body_text="Hi Shawn, just checking in on your availability.",
            occurred_at="2026-06-01T00:00:00Z",
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=key,
            message_id="msg-2",
            direction="outbound",
            summary="Re: Following up",
            body_text="Happy to chat this week!",
            occurred_at="2026-06-02T00:00:00Z",
        ),
    )
    conn.close()

    rc = list_leads_main(["--db", str(seeded_db), "--company", "Stripe", "--show-communications"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Software Engineer @ Stripe" in out
    assert "<-" in out and "Jane Doe" in out  # inbound, attributed to the contact
    assert "->" in out  # outbound
    assert "Following up on your application" in out
    assert "just checking in on your availability" in out
    assert "Happy to chat this week!" in out
    # Oldest first (2026-06-01 before 2026-06-02).
    assert out.index("checking in") < out.index("Happy to chat")


def test_list_leads_show_communications_handles_no_conversations(seeded_db: Path, capsys):
    rc = list_leads_main(["--db", str(seeded_db), "--company", "BigCorp", "--show-communications"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no communications archived" in out


def test_list_leads_waiting_filter_only_shows_awaiting_response_leads(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    key = JobLead(company="Stripe", title="Software Engineer", source_message_id="m1", source_label="single-jd").normalized_key
    add_job_conversation(conn, JobConversation(job_key=key, direction="outbound", summary="Applied"))
    conn.close()

    rc = list_leads_main(["--db", str(seeded_db), "--waiting"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stripe" in out
    assert "BigCorp" not in out


def test_list_leads_default_table_shows_waiting_column(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    key = JobLead(company="Stripe", title="Software Engineer", source_message_id="m1", source_label="single-jd").normalized_key
    add_job_conversation(
        conn, JobConversation(job_key=key, direction="outbound", summary="Applied", occurred_at="2026-06-01T00:00:00+00:00")
    )
    conn.close()

    rc = list_leads_main(["--db", str(seeded_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "WAITING" in out
    assert "2026-06-01" in out
