"""A contact's name, email and LinkedIn link each stay in their own field (REQUIREMENTS CON-1, CON-2)."""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline.models import JobContact, JobConversation, JobLead
from job_tracker.pipeline.pending_workflow import _card_channel, _recruiter_contact, _thread_url_for_lead
from job_tracker.pipeline.store import (
    add_job_contact,
    add_job_conversation,
    clean_contact_fields,
    connect,
    is_person_email,
    list_job_conversations,
    name_from_email,
    save_lead_contact,
    upsert_lead,
)

THREAD = "https://www.linkedin.com/messaging/thread/2-abc==/"


def _lead(conn, company="Meta", title="Data Engineer") -> str:
    upsert_lead(conn, JobLead(company=company, title=title, source_message_id="m1", source_label="single-jd"))
    return f"{company.lower()}::{title.lower()}"


def test_person_email():
    assert is_person_email("aman.mishra@pinnacle99inc.com")
    for e in ("jobs@my.theladders.com", "alerts@energyjobline.com", "linkedin.com/in/x", "careers@acme.com",
              "no-reply@us.greenhouse-mail.io", "shawn.becker@spexture.com", "hit-reply@linkedin.com"):
        assert not is_person_email(e), e


def test_name_from_email_never_guesses():
    assert name_from_email("aman.mishra@pinnacle99inc.com") == "Aman Mishra"
    assert name_from_email("tanmay.v@diverselynx.com") == "Tanmay"
    assert name_from_email("vinayv@fisecglobal.net") == ""
    assert name_from_email("jobs.team@theladders.com") == ""


def test_linkedin_link_typed_as_email_moves_to_linkedin_url():
    assert clean_contact_fields("x@y.com", "linkedin.com/in/prisha-singh", "") == ("", "", "https://www.linkedin.com/in/prisha-singh")


def test_saved_contacts_keep_fields_apart(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _lead(conn)
    save_lead_contact(conn, key, name="Prisha Singh", email="linkedin.com/in/prisha-singh-102457257")
    add_job_contact(conn, JobContact(job_key=key, name="rahul.bhadoriya@akraya.com", email="rahul.bhadoriya@akraya.com"))
    rows = {r["name"]: dict(r) for r in conn.execute("SELECT name, email, linkedin_url FROM job_contacts")}
    assert rows["Prisha Singh"]["email"] == "" and rows["Prisha Singh"]["linkedin_url"].endswith("prisha-singh-102457257")
    assert rows[""]["email"] == "rahul.bhadoriya@akraya.com"


def test_recruiter_contact_never_shows_an_email_as_the_name(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _lead(conn, "EAD Only", "Data Architect")
    conn.execute("INSERT INTO job_contacts (job_key, name, email, role) VALUES (?, '', 'aman.mishra@pinnacle99inc.com', 'recruiter')", (key,))
    assert _recruiter_contact(conn, key)[:2] == ("Aman Mishra", "aman.mishra@pinnacle99inc.com")
    board = _lead(conn, "Cox", "Engineer")
    conn.execute("INSERT INTO job_contacts (job_key, name, email, role) VALUES (?, '', 'jobs@my.theladders.com', 'recruiter')", (board,))
    assert _recruiter_contact(conn, board) == ("", "", "", False)


def test_linkedin_thread_makes_a_linkedin_card_unless_there_is_a_real_email(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _lead(conn, "IBM", "Role TBD")
    conn.execute("INSERT INTO job_contacts (job_key, name, linkedin_reply_to, role) VALUES (?, 'Nalini Karan', ?, 'recruiter')", (key, THREAD))
    add_job_conversation(conn, JobConversation(job_key=key, direction="inbound", summary="pasted", body_text="It's fully remote"))
    thread = _thread_url_for_lead(list(list_job_conversations(conn, key)), conn, key)
    assert thread == THREAD
    name, email, _, relay = _recruiter_contact(conn, key)
    assert (name, email) == ("Nalini Karan", "")
    assert _card_channel(thread_url=thread, source_label="single-jd", email=email, email_is_li_relay=relay) == "linkedin"
    assert _card_channel(thread_url=thread, source_label="single-jd", email="ed@intentsify.io", email_is_li_relay=False) == "email"
    assert _card_channel(thread_url=thread, source_label="single-jd", email="jobs@my.theladders.com", email_is_li_relay=False) == "linkedin"
