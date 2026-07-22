"""Tests for pipeline/post_application.py — signal classification for
messages already matched to an existing tracked lead, and the forward-only
stage guard `apply_post_application_signal()` applies before writing."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.post_application import (
    PostApplicationLabel,
    apply_post_application_signal,
    classify_post_application,
)
from job_tracker.pipeline.store import advance_status, connect, get_lead_status, upsert_lead

# --- classify_post_application ---------------------------------------------


def test_rejection_phrase_wins():
    text = "Subject: Update\nAfter careful consideration, we have decided to pursue other candidates."
    result = classify_post_application(text)
    assert result.label == PostApplicationLabel.REJECTION
    assert result.confidence >= 0.8


def test_real_solace_confirmation_is_application_received_not_rejection():
    """Regression for the 2026-07-22 bug: this exact real Solace ATS
    auto-reply used to match classifier.py's now-removed "thank you for
    applying" rejection pattern and got mislabeled REJECTION -> SKIP. It
    carries no negative/decline language at all — must classify as
    APPLICATION_RECEIVED."""
    text = (
        "Thank You for Applying to Solace!\n"
        "Hi Shawn,\n\nThank you for applying to the Data Engineer opportunity here at Solace! "
        "We've received your information and our recruiting team will be reviewing it as quickly "
        "as they can. We'll be in touch about next steps!\n\nAll the best,\nTeam Solace"
    )
    result = classify_post_application(text)
    assert result.label == PostApplicationLabel.APPLICATION_RECEIVED
    assert result.confidence >= 0.7


def test_interview_invite_beats_application_received_confirmation_preamble():
    text = (
        "Thank you for applying! We'd like to invite you to an interview next Thursday at 2pm "
        "with our engineering manager."
    )
    result = classify_post_application(text)
    assert result.label == PostApplicationLabel.INTERVIEW_INVITE


@pytest.mark.parametrize(
    "text",
    [
        "We would like to schedule an interview with you for next week.",
        "Congratulations! You've been selected to move forward to the next round of interviews.",
        "Can we set up a quick technical interview for Tuesday?",
    ],
)
def test_interview_invite_patterns(text: str):
    assert classify_post_application(text).label == PostApplicationLabel.INTERVIEW_INVITE


@pytest.mark.parametrize(
    "text",
    [
        "We've received your application and will review it shortly.",
        "Thanks for applying — we are reviewing your application now.",
        "Your application has been received.",
    ],
)
def test_application_received_patterns(text: str):
    assert classify_post_application(text).label == PostApplicationLabel.APPLICATION_RECEIVED


def test_generic_followup_is_next_steps():
    text = "Hi Shawn, just checking in — are you still interested in this role? What's your availability like?"
    result = classify_post_application(text)
    assert result.label == PostApplicationLabel.NEXT_STEPS
    assert result.confidence == 0.5


# --- apply_post_application_signal ------------------------------------------


@pytest.fixture()
def db(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(conn, JobLead(company="Acme Corp", title="Senior Engineer", source_message_id="m0", source_label="single-jd"))
    yield conn
    conn.close()


def _key(company: str, title: str) -> str:
    from job_tracker.pipeline.models import normalize_key

    return normalize_key(company, title)


def test_application_received_advances_new_lead_to_applied(db):
    key = _key("Acme Corp", "Senior Engineer")
    classification = classify_post_application("Thank you for applying! We've received your application.")
    action = apply_post_application_signal(db, key, classification, message_id="m1")
    assert "applied" in action
    assert get_lead_status(db, key) == "applied"


def test_application_received_is_noop_once_already_interviewing(db):
    """Forward-only guard: a stray application-received confirmation
    arriving after the lead is already interviewing must not reset it."""
    key = _key("Acme Corp", "Senior Engineer")
    advance_status(db, key, "interviewing")
    classification = classify_post_application("We've received your application.")
    action = apply_post_application_signal(db, key, classification, message_id="m1")
    assert action == ""
    assert get_lead_status(db, key) == "interviewing"


def test_interview_invite_advances_applied_lead(db):
    key = _key("Acme Corp", "Senior Engineer")
    advance_status(db, key, "applied")
    classification = classify_post_application("We'd like to schedule an interview with you.")
    action = apply_post_application_signal(db, key, classification, message_id="m2")
    assert "interviewing" in action
    assert get_lead_status(db, key) == "interviewing"


def test_rejection_applies_from_any_forward_stage(db):
    key = _key("Acme Corp", "Senior Engineer")
    advance_status(db, key, "interviewing")
    classification = classify_post_application("Unfortunately, we have decided to move forward with other candidates.")
    action = apply_post_application_signal(db, key, classification, message_id="m3", email_text="rejection text")
    assert "rejected" in action
    assert get_lead_status(db, key) == "rejected"


def test_rejection_is_noop_if_already_rejected(db):
    key = _key("Acme Corp", "Senior Engineer")
    advance_status(db, key, "rejected")
    classification = classify_post_application("Unfortunately, we will not be moving forward.")
    action = apply_post_application_signal(db, key, classification, message_id="m4")
    assert action == ""


def test_terminal_off_ramp_never_resurrected_by_application_received(db):
    """A lead marked 'skipped' (our own pass decision) must never be
    silently flipped back to 'applied' by a late-arriving confirmation
    email — skipped has a rank of -1 in the forward stage ordering."""
    key = _key("Acme Corp", "Senior Engineer")
    advance_status(db, key, "skipped")
    classification = classify_post_application("We've received your application.")
    action = apply_post_application_signal(db, key, classification, message_id="m5")
    assert action == ""
    assert get_lead_status(db, key) == "skipped"


def test_next_steps_is_always_a_noop(db):
    key = _key("Acme Corp", "Senior Engineer")
    classification = classify_post_application("Just checking in on your availability.")
    action = apply_post_application_signal(db, key, classification, message_id="m6")
    assert action == ""
    assert get_lead_status(db, key) == "new"
