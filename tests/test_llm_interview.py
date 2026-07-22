"""Tests for structured interview-detail extraction (pipeline/llm_interview.py).

No real Anthropic API calls are made here — the client is always a fake
stand-in, same pattern as tests/test_llm_extract.py.
"""

from __future__ import annotations

import json

from job_tracker.email.models import EmailMessage
from job_tracker.pipeline import llm_interview


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text: str):
        self.content = [_FakeBlock(text)]


class _FakeClient:
    def __init__(self, response_text: str = "{}", error: Exception | None = None):
        self.response_text = response_text
        self.error = error
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _FakeMessage(self.response_text)


def _message() -> EmailMessage:
    return EmailMessage(
        id="m1",
        from_address="recruiter@example.com",
        subject="Interview invite",
        body_plain="We'd love to set up an interview.",
    )


def test_extract_interview_details_llm_parses_clean_json():
    payload = json.dumps(
        {
            "date_text": "Thursday, July 24",
            "time_text": "2:00 PM ET",
            "format": "video",
            "interviewer_name": "Jane Smith, Engineering Manager",
            "notes": "45-minute technical round",
        }
    )
    client = _FakeClient(response_text=payload)
    details = llm_interview.extract_interview_details_llm(_message(), client=client)

    assert details is not None
    assert details.date_text == "Thursday, July 24"
    assert details.time_text == "2:00 PM ET"
    assert details.format == "video"
    assert details.interviewer_name == "Jane Smith, Engineering Manager"
    assert not details.is_empty


def test_extract_interview_details_llm_strips_markdown_fence():
    payload = "```json\n" + json.dumps({"date_text": "Monday", "time_text": "", "format": "", "interviewer_name": "", "notes": ""}) + "\n```"
    client = _FakeClient(response_text=payload)
    details = llm_interview.extract_interview_details_llm(_message(), client=client)
    assert details is not None
    assert details.date_text == "Monday"


def test_extract_interview_details_llm_invalid_format_becomes_empty_string():
    payload = json.dumps({"date_text": "", "time_text": "", "format": "carrier pigeon", "interviewer_name": "", "notes": ""})
    client = _FakeClient(response_text=payload)
    details = llm_interview.extract_interview_details_llm(_message(), client=client)
    assert details is not None
    assert details.format == ""


def test_extract_interview_details_llm_returns_none_on_client_error():
    client = _FakeClient(error=RuntimeError("boom"))
    details = llm_interview.extract_interview_details_llm(_message(), client=client)
    assert details is None


def test_extract_interview_details_llm_returns_none_on_unparseable_response():
    client = _FakeClient(response_text="not json")
    details = llm_interview.extract_interview_details_llm(_message(), client=client)
    assert details is None


def test_interview_details_is_empty_true_for_all_blank_fields():
    payload = json.dumps({"date_text": "", "time_text": "", "format": "", "interviewer_name": "", "notes": ""})
    client = _FakeClient(response_text=payload)
    details = llm_interview.extract_interview_details_llm(_message(), client=client)
    assert details is not None
    assert details.is_empty


def test_as_summary_renders_all_fields():
    details = llm_interview.InterviewDetails(
        date_text="Thursday, July 24",
        time_text="2:00 PM ET",
        format="video",
        interviewer_name="Jane Smith",
        notes="45-minute technical round",
    )
    summary = details.as_summary()
    assert "Thursday, July 24" in summary
    assert "2:00 PM ET" in summary
    assert "(video)" in summary
    assert "Jane Smith" in summary
    assert "45-minute technical round" in summary


def test_as_summary_falls_back_when_empty():
    details = llm_interview.InterviewDetails()
    assert details.as_summary() == "Interview invite"


def test_notes_are_truncated_to_200_chars():
    long_note = "x" * 500
    payload = json.dumps({"date_text": "", "time_text": "", "format": "", "interviewer_name": "", "notes": long_note})
    client = _FakeClient(response_text=payload)
    details = llm_interview.extract_interview_details_llm(_message(), client=client)
    assert details is not None
    assert len(details.notes) == 200
