"""Qualifying LinkedIn reply drafts + heuristic Position/company promotion."""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline.models import UnmatchedMessage
from job_tracker.pipeline.qualifying_reply import (
    detect_pitch_gaps,
    draft_qualifying_reply,
    extract_role_heuristic,
    promote_heuristic_unmatched,
)
from job_tracker.pipeline.store import connect, list_unmatched_messages, record_unmatched_message

MADHVENDRA = """
Exciting opportunity in software and data engineering

      Madhvendra Kumar
        Reply
        https://www.linkedin.com/messaging/thread/2-ZDMxMTM2MDUtOTQ2My00M2Q0LWEzZTUtNDNlZDY1YTUwYjNlXzEwMA==/

Hi,

Position: LLM Data Engineer
Type: Contract (Long term)
Location: Remote

Context
: We are looking for a Generalist Data Engineer for one of our clients building a healthcare-focused AI benchmark.

Required Skills
	• Data engineering: Production-grade pipeline code
	• AWS data stack: S3, AWS Glue, Spark on EMR, Athena

Best regards,
Madhvendra Kumar
Sr. Technical Recruiter
KPG99,INC
  madhvendra@kpgtech.com   Direct: 6098300389
"""

EVAN = """
AI Engineer Job Opportunity

      Evan T.
        Reply
        https://www.linkedin.com/messaging/thread/2-MjVhMzhmNDEtMzQ3MS00NzVlLWE5ZDUtYmJjM2I2Y2NlMGU5XzEwMA==/

Hey Shawn, I wanted to reach out because we currently have an opening with one of our main clients for an AI Engineer. I've attached the job description, if you are interested please send me over a copy of your resume and some availability to discuss the role!

Evan Turner
Technology Recruiter
"""


def test_extract_role_heuristic_madhvendra():
    role = extract_role_heuristic(MADHVENDRA)
    assert role is not None
    assert role.title == "LLM Data Engineer"
    assert "KPG99" in role.company.upper()
    assert "Glue" in role.snippet or "AWS" in role.snippet


def test_extract_role_heuristic_evan_thin_pitch_returns_none():
    assert extract_role_heuristic(EVAN) is None
    assert extract_role_heuristic(EVAN, subject="AI Engineer Job Opportunity") is None


GRANT = """
Sr. Analytics Engineer - Remote - FTE

        Grant Naudet
        Reply
        https://www.linkedin.com/messaging/thread/2-ZWU4OWZlYTQtYzA3Yi00ODVjLWE0ZjUtMWZiY2M1ZDliNTBhXzEwMA==/

I'm partnering with a higher education client to fill two full-time Analytics Engineer opportunities.

Analytics Engineer III — Requirements
* Advanced SQL and Python skills, with dbt or similar transformation tools.

Grant Naudet
Sourcing Recruiter
GNaudet@remycorp.com
Remy Corporation
www.remycorp.com

----------------------------------------

This email was intended for Shawn Becker (Open to Senior Software, Full Stack, & Data Engineer · AI/ML Engineer)
"""


def test_extract_role_heuristic_subject_title_and_corporation_line():
    role = extract_role_heuristic(GRANT, subject="Sr. Analytics Engineer - Remote - FTE")
    assert role is not None
    assert role.title == "Sr. Analytics Engineer"
    assert role.company == "Remy Corporation"
    assert "This email was intended" not in role.snippet


def test_extract_role_heuristic_subject_without_role_noun_returns_none():
    assert extract_role_heuristic(GRANT, subject="Exploring new opportunities together") is None
    assert extract_role_heuristic(GRANT, subject="Building high-volume data products with Python and AWS") is None


def test_draft_evan_is_screen_first_not_jd_gate():
    """Evan asks for résumé + availability with no real JD — send generic résumé, don't gate on a paste."""
    gaps = detect_pitch_gaps(EVAN, subject="AI Engineer Job Opportunity")
    assert gaps.needs_jd is True
    assert gaps.screen_first is True
    draft = draft_qualifying_reply(EVAN, subject="AI Engineer Job Opportunity")
    assert draft.recruiter_name.startswith("Evan")
    assert "messaging/thread/" in draft.thread_url
    assert "latest résumé" in draft.body.lower()
    assert "paste the job description" not in draft.body.lower()
    assert "before I send a résumé" not in draft.body
    assert "C2C" in draft.body
    assert "rate" not in draft.body.lower()
    assert "$" not in draft.body
    assert draft.body.strip().endswith("Best, Shawn\nshawn.becker@spexture.com")


def test_draft_madhvendra_skips_jd_and_remote_asks():
    gaps = detect_pitch_gaps(MADHVENDRA)
    assert gaps.needs_jd is False
    assert gaps.needs_remote is False
    assert gaps.needs_engagement is True
    assert gaps.needs_end_client is True
    draft = draft_qualifying_reply(MADHVENDRA)
    assert "LLM Data Engineer" in draft.body
    assert "paste the job description" not in draft.body.lower()
    assert "C2C" in draft.body
    assert draft.body.strip().endswith("Best, Shawn\nshawn.becker@spexture.com")
    assert draft.gaps.screen_first is False


STEPHEN_CALL_THEN_JD = """
AI Engineer Salt Lake City

        Stephen Gordon Joughin
        Reply
        https://www.linkedin.com/messaging/thread/2-N2UzNDQ3NTgtZjVlMS00YWZhLTk0NGMtZmQ3YTAyNDk5NzdiXzEwMA==/

Would you be open to a confidential chat? I can share who the client is when we speak.
It's a hybrid role based in Salt Lake City.
"""


def test_draft_confidential_chat_is_screen_first():
    gaps = detect_pitch_gaps(STEPHEN_CALL_THEN_JD, subject="AI Engineer Salt Lake City")
    assert gaps.screen_first is True
    draft = draft_qualifying_reply(STEPHEN_CALL_THEN_JD, subject="AI Engineer Salt Lake City")
    assert "latest résumé" in draft.body.lower()
    assert "Who is the end client" not in draft.body
    assert "paste the job description" not in draft.body.lower()
    assert draft.body.strip().endswith("Best, Shawn\nshawn.becker@spexture.com")


def test_promote_heuristic_unmatched(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id="imap:madh-1",
            direction="inbound",
            from_address="inmail-hit-reply@linkedin.com",
            to_address="shawn.becker@spexture.com",
            subject="Exciting opportunity",
            body_text=MADHVENDRA,
        ),
    )
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id="imap:evan-1",
            direction="inbound",
            from_address="inmail-hit-reply@linkedin.com",
            subject="AI Engineer Job Opportunity",
            body_text=EVAN,
        ),
    )
    promoted = promote_heuristic_unmatched(conn)
    assert len(promoted) == 1
    assert "llm data engineer" in promoted[0]
    assert len(list_unmatched_messages(conn)) == 1  # Evan still parked
    row = conn.execute(
        "SELECT company, title, source_label FROM job_leads WHERE normalized_key = ?",
        (promoted[0],),
    ).fetchone()
    assert "KPG99" in row["company"].upper()
    assert row["title"] == "LLM Data Engineer"
    assert row["source_label"] == "linkedin_message"
    conn.close()


def test_draft_names_recruiter_date_and_client_location():
    body = (
        "Hi We have a Lead Python Fullstack role with our direct client in New Jersey it's a Remote role\n\n"
        "Skills : Python , Fullstack , Claude is Must to Have AI tool Django , Numpy , Panda's\n\n"
        "Please let me know if you would be interested in this role\n\nRajneesh bhardwaj\nRecruitment Specialist"
    )
    draft = draft_qualifying_reply(
        body,
        subject="Position for a Lead AI Python Fullstack Remote with Direct client",
        known_recruiter_name="Rajneesh Bhardwaj",
        received_at="2026-10-12T16:34:00+00:00",
    )
    assert draft.body.startswith("Hi Rajneesh,")
    assert "Thanks for reaching out on October 12" in draft.body
    assert "before I send you my résumé" in draft.body
    assert "on-site with the client in New Jersey?" in draft.body
    assert draft.body.endswith("Best, Shawn\nshawn.becker@spexture.com")


INMAIL_WITH_CREDENTIAL = """Enterprise Data Architecture & Applied AI Consulting Opportunity
Carlson Nkwain, PhD
Global Health & International Development Advisor | Health Security
https://www.linkedin.com/comm/in/carlson-nkwain?lipi=urn%3Ali&trk=eml
Hello Shawn,
I'm representing a U.S.-based AI startup. Would you be interested in learning more?
Carlson Nkwain, PhD
Yes, interested…https://www.linkedin.com/comm/messaging/thread/2-NzRkZDc0MzctOWUwMC00ODVkLWJjMjUtMzFmZjFlMDNhNWJlXzEwMA==/?lipi=urn%3Ali&midToken=AQH
Reply
        https://www.linkedin.com/comm/messaging/thread/2-NzRkZDc0MzctOWUwMC00ODVkLWJjMjUtMzFmZjFlMDNhNWJlXzEwMA==/?lipi=urn%3Ali
"""


def test_inmail_with_credential_greets_by_name_and_finds_comm_thread():
    draft = draft_qualifying_reply(INMAIL_WITH_CREDENTIAL, subject="Enterprise Data Architecture & Applied AI Consulting Opportunity")
    assert draft.recruiter_name == "Carlson Nkwain"
    assert draft.body.startswith("Hi Carlson,")
    assert draft.thread_url == (
        "https://www.linkedin.com/messaging/thread/2-NzRkZDc0MzctOWUwMC00ODVkLWJjMjUtMzFmZjFlMDNhNWJlXzEwMA==/"
    )
