"""A pasted LinkedIn pitch and its Gmail copy are one lead, including rows already stored."""

from __future__ import annotations

from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.comms_match import match_message_to_job
from job_tracker.pipeline.models import JobContact, JobConversation, JobLead, UnmatchedMessage
from job_tracker.pipeline.paste_linkedin import post_pasted_thread
from job_tracker.pipeline.pitch_match import link_historic_same_pitches, pitches_overlap
from job_tracker.pipeline.store import (
    add_job_contact,
    add_job_conversation,
    connect,
    get_lead_status,
    record_unmatched_message,
    upsert_lead,
)

PITCH = (
    "Hi Shawn,Hope you are doing good. I'm looking Data Engineer "
    "(Databricks & Cloud Analytics),SaltLake City,UT-W2 or H1 Transfer- Onsite. "
    "Plz let me know if you are available and would like to apply"
)
PANE = f"Jaideep S\n\n{PITCH}\n"
THREAD = "https://www.linkedin.com/messaging/thread/2-abc123==/"
NOTICE = f"Jaideep S sent you a message\n\n{PITCH}\n\n{THREAD}\n"
OTHER = (
    "Jaideep S\n\nHi Shawn, we need a Staff Platform Engineer for a twelve month "
    "remote contract building internal developer tools. Please send your resume "
    "if the team is a fit.\n"
)


def _email_lead(conn, message_id: str, *, first_seen: str) -> str:
    lead = JobLead(
        company="Codeforce 360",
        title="Snowflake Data Engineer",
        source_message_id=message_id,
        source_label="linkedin_message",
    )
    upsert_lead(conn, lead)
    conn.execute(
        "UPDATE job_leads SET first_seen = ? WHERE normalized_key = ?",
        (first_seen, lead.normalized_key),
    )
    conn.commit()
    add_job_contact(
        conn,
        JobContact(
            job_key=lead.normalized_key,
            name="Jaideep S",
            role="recruiter",
            source_message_id=message_id,
            linkedin_reply_to="abc@reply.linkedin.com",
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            message_id=message_id,
            channel="email",
            direction="inbound",
            summary="Jaideep S sent you a message",
            body_text=NOTICE,
        ),
    )
    return lead.normalized_key


def _paste_lead(conn, *, first_seen: str) -> str:
    lead = JobLead(
        company="End-client TBD via Jaideep S",
        title="Data Engineer (Databricks & Cloud Analytics)",
        source_message_id="paste:historic",
        source_label="linkedin_message",
    )
    upsert_lead(conn, lead)
    conn.execute(
        "UPDATE job_leads SET first_seen = ? WHERE normalized_key = ?",
        (first_seen, lead.normalized_key),
    )
    conn.commit()
    add_job_contact(
        conn,
        JobContact(
            job_key=lead.normalized_key,
            name="Jaideep S",
            role="recruiter",
            source_message_id="paste:historic",
        ),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            message_id="paste:historic",
            channel="linkedin",
            direction="inbound",
            summary="Data Engineer (Databricks & Cloud Analytics)",
            body_text=PANE,
        ),
    )
    return lead.normalized_key


def _thread_files(root):
    linkedin = list(root.rglob("communications/LinkedIn_thread.txt"))
    email = list(root.rglob("communications/Email_thread.txt"))
    return linkedin, email


def test_a_stored_email_and_a_stored_paste_become_one_lead(tmp_path):
    conn = connect(tmp_path / "leads.db")
    resumes = tmp_path / "resumes"
    email_key = _email_lead(conn, "gmail-old", first_seen="2026-10-01T00:00:00+00:00")
    paste_key = _paste_lead(conn, first_seen="2026-10-24T00:00:00+00:00")

    actions = link_historic_same_pitches(conn, output_root=resumes)
    skipped = [item for item in actions if item.get("skipped") == email_key]
    assert skipped and skipped[0]["kept"] == paste_key
    assert get_lead_status(conn, email_key) == "skipped"
    assert get_lead_status(conn, paste_key) == "new"
    bodies = [
        row["body_text"] or ""
        for row in conn.execute(
            "SELECT body_text FROM job_conversations WHERE job_key = ?", (paste_key,)
        )
    ]
    assert any(THREAD in body for body in bodies)
    assert conn.execute(
        "SELECT COUNT(*) FROM job_conversations WHERE job_key = ?", (paste_key,)
    ).fetchone()[0] == 2
    replies = [
        row["linkedin_reply_to"]
        for row in conn.execute(
            "SELECT linkedin_reply_to FROM job_contacts WHERE job_key = ?", (paste_key,)
        )
    ]
    assert "abc@reply.linkedin.com" in replies
    linkedin, email = _thread_files(resumes)
    assert len(linkedin) == 1 and len(email) == 1
    assert PITCH in linkedin[0].read_text()
    assert "gmail-old" in email[0].read_text()
    assert THREAD in email[0].read_text()
    assert linkedin[0].parent == email[0].parent
    assert linkedin[0].parent.name == "communications"
    assert linkedin[0].parent.parent.parent.parent == resumes


def test_a_paste_arriving_after_a_stored_email_does_not_create_a_second_lead(tmp_path):
    conn = connect(tmp_path / "leads.db")
    resumes = tmp_path / "resumes"
    email_key = _email_lead(conn, "gmail-old", first_seen="2026-10-01T00:00:00+00:00")
    pasted = post_pasted_thread(conn, PANE, use_llm=False, output_root=resumes)
    assert pasted["created"] is False
    assert pasted["jobKey"] == email_key
    assert conn.execute("SELECT COUNT(*) FROM job_leads").fetchone()[0] == 1
    linkedin, email = _thread_files(resumes)
    assert len(linkedin) == 1 and len(email) == 1
    assert "paste:" in linkedin[0].read_text()
    assert "gmail-old" in email[0].read_text()


def test_parked_mail_already_in_the_database_resolves_onto_the_paste(tmp_path):
    conn = connect(tmp_path / "leads.db")
    resumes = tmp_path / "resumes"
    paste_key = _paste_lead(conn, first_seen="2026-10-24T00:00:00+00:00")
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id="gmail-parked",
            direction="inbound",
            from_address="hit-reply@linkedin.com",
            subject="Jaideep S sent you a message",
            body_text=NOTICE,
        ),
    )
    actions = link_historic_same_pitches(conn, output_root=resumes)
    assert any(item.get("kept") == paste_key and item.get("claimed_unmatched") for item in actions)
    row = conn.execute(
        "SELECT resolved_job_key FROM unmatched_messages WHERE message_id = ?",
        ("gmail-parked",),
    ).fetchone()
    assert row["resolved_job_key"] == paste_key
    bodies = [
        row["body_text"] or ""
        for row in conn.execute("SELECT body_text FROM job_conversations WHERE job_key = ?", (paste_key,))
    ]
    assert any(THREAD in body for body in bodies)
    assert conn.execute("SELECT COUNT(*) FROM job_conversations").fetchone()[0] == 2
    linkedin, email = _thread_files(resumes)
    assert PITCH in linkedin[0].read_text()
    assert "gmail-parked" in email[0].read_text()
    assert linkedin[0].parent.parent.parent.parent == resumes


def test_a_shared_greeting_without_the_role_is_not_the_same_pitch():
    greet = (
        "Hi Shawn, hope you are doing good this week and I wanted to reach out "
        "about a new opening on my desk. "
    )
    data = greet + "We have a Data Engineer role on Databricks in Salt Lake City."
    java = greet + "We have a Java Fullstack role, contract, onsite in Bedminster NJ."
    assert pitches_overlap(data, java) is False
    assert pitches_overlap(PITCH, NOTICE) is True


def test_a_different_pitch_from_the_same_recruiter_stays_its_own_lead(tmp_path):
    conn = connect(tmp_path / "leads.db")
    first = post_pasted_thread(conn, PANE, use_llm=False)
    second = post_pasted_thread(conn, OTHER, use_llm=False)
    assert first["jobKey"] != second["jobKey"]
    assert link_historic_same_pitches(conn) == []
    assert get_lead_status(conn, first["jobKey"]) == "new"
    assert get_lead_status(conn, second["jobKey"]) == "new"


def test_a_new_notification_matches_the_paste_without_a_second_conversation(tmp_path):
    conn = connect(tmp_path / "leads.db")
    pasted = post_pasted_thread(conn, PANE, use_llm=False)
    message = EmailMessage(
        id="gmail-new",
        from_address="hit-reply@linkedin.com",
        subject="Jaideep S sent you a message",
        body_plain=NOTICE,
    )
    outcome = match_message_to_job(conn, message, direction="inbound", use_llm_fallback=True)
    assert outcome.tier == "same_pitch"
    assert outcome.job_key == pasted["jobKey"]
    assert conn.execute("SELECT COUNT(*) FROM job_conversations").fetchone()[0] == 1
