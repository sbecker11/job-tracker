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


def test_draft_evan_asks_for_jd_and_engagement():
    draft = draft_qualifying_reply(EVAN, subject="AI Engineer Job Opportunity")
    assert draft.recruiter_name.startswith("Evan")
    assert "messaging/thread/" in draft.thread_url
    assert "job description" in draft.body.lower() or "paste" in draft.body.lower()
    assert "C2C" in draft.body
    assert draft.body.strip().endswith("Best, Shawn")
    assert "$" not in draft.body  # never invent a figure


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
    assert draft.body.strip().endswith("Best, Shawn")


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
