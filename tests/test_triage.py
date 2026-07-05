"""Tests for the recruiter-inbox triage decision logic (pipeline/triage.py).

No real Anthropic API or ATS network calls — the LLM client is always a fake
stand-in and `resolve_full_jd=False` skips live ATS lookups, so these run
offline and free of charge.
"""

from __future__ import annotations

import json

import pytest

from job_tracker.email.models import EmailMessage
from job_tracker.pipeline import llm_apply, triage


@pytest.fixture(autouse=True)
def fake_profile(monkeypatch, tmp_path):
    profile_path = tmp_path / "CLAUDE.md"
    profile_path.write_text("FAKE CANDIDATE PROFILE FOR TESTS", encoding="utf-8")
    monkeypatch.setattr(llm_apply, "_CANDIDATE_PROFILE_PATH", profile_path)
    return profile_path


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeMessage:
    def __init__(self, text: str, input_tokens: int, output_tokens: int):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage(input_tokens, output_tokens)


class _FakeClient:
    def __init__(self, responses: list | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, tuple):
            text, input_tokens, output_tokens = item
        else:
            text, input_tokens, output_tokens = item, 100, 50
        return _FakeMessage(text, input_tokens, output_tokens)


def _eval_payload(verdict: str, match_pct: float = 50) -> str:
    return json.dumps(
        {
            "dealbreaker_notes": [],
            "skills_alignment": [],
            "match_pct": match_pct,
            "verdict": verdict,
            "rationale": f"scored {verdict}",
        }
    )


_GENERATE_PAYLOAD = json.dumps(
    {
        "resume": {
            "positioning_line": "Senior Engineer",
            "summary": "A summary.",
            "skills": ["Python"],
            "experience": [],
            "education": [],
        },
        "cover_letter": {"salutation": "Dear Hiring Team,", "paragraphs": ["I am interested."]},
    }
)


def _single_jd_message(**overrides) -> EmailMessage:
    fields = dict(
        id="msg-1",
        from_address="noreply@greenhouse.io",
        subject="Software Engineer — Acme",
        snippet="Apply for Software Engineer at Acme",
        body_plain=(
            "Hi Shawn,\n\nAcme is hiring a Software Engineer.\n\n"
            "View role: https://boards.greenhouse.io/acme/jobs/1234\n\n"
            "Responsibilities:\n- Build APIs\n- Ship features\n\nApply today."
        ),
    )
    fields.update(overrides)
    return EmailMessage(**fields)


def _noise_message() -> EmailMessage:
    return EmailMessage(
        id="msg-noise",
        from_address="newsletter@example.com",
        subject="Your weekly digest",
        body_plain="Check out our newsletter this week!",
    )


def _rejection_message() -> EmailMessage:
    return EmailMessage(
        id="msg-rejection",
        from_address="talent@acme.example",
        subject="Update on your application",
        body_plain="After careful consideration, we have decided to pursue other candidates.",
    )


def _outreach_message() -> EmailMessage:
    return EmailMessage(
        id="msg-outreach",
        from_address="recruiter@staffingco.com",
        subject="Quick question",
        body_plain="I came across your profile and would love to connect for a quick chat about an opportunity.",
    )


# --- deny / needs-review short-circuits (no LLM call) ------------------------


def test_noise_message_denied_without_llm_call():
    result = triage.triage_message(_noise_message(), client=_FakeClient())
    assert result.outcome == triage.DENY
    assert result.roles == []


def test_rejection_message_denied_without_llm_call():
    result = triage.triage_message(_rejection_message(), client=_FakeClient())
    assert result.outcome == triage.DENY


def test_recruiter_outreach_needs_review_without_llm_call():
    result = triage.triage_message(_outreach_message(), client=_FakeClient())
    assert result.outcome == triage.NEEDS_REVIEW
    assert result.classifier_label == "recruiter-outreach"


# --- single-JD -> LLM evaluate (+ generate) ----------------------------------


def test_single_jd_pursue_verdict_accepts_and_generates(tmp_path):
    client = _FakeClient(responses=[(_eval_payload("pursue", 85), 900, 200), (_GENERATE_PAYLOAD, 1200, 800)])
    result = triage.triage_message(
        _single_jd_message(),
        resolve_full_jd=False,
        output_root=tmp_path,
        client=client,
    )
    assert result.outcome == triage.ACCEPT
    assert len(result.roles) == 1
    role = result.roles[0]
    assert role.lead.company == "Acme"
    assert role.package.evaluation.verdict == "pursue"
    assert role.package.resume_path is not None
    assert role.package.resume_path.exists()


def test_single_jd_pass_verdict_denies_and_skips_generation(tmp_path):
    client = _FakeClient(responses=[(_eval_payload("pass", 5), 900, 200)])
    result = triage.triage_message(
        _single_jd_message(),
        resolve_full_jd=False,
        output_root=tmp_path,
        client=client,
    )
    assert result.outcome == triage.DENY
    assert result.roles[0].package.resume_path is None
    assert len(client.calls) == 1  # evaluate only, no generate call spent


def test_single_jd_review_verdict_needs_review(tmp_path):
    client = _FakeClient(responses=[(_eval_payload("review", 40), 900, 200)])
    result = triage.triage_message(
        _single_jd_message(),
        resolve_full_jd=False,
        output_root=tmp_path,
        client=client,
    )
    assert result.outcome == triage.NEEDS_REVIEW


def test_no_generate_flag_never_spends_on_generation_even_when_pursue(tmp_path):
    client = _FakeClient(responses=[(_eval_payload("pursue", 90), 900, 200)])
    result = triage.triage_message(
        _single_jd_message(),
        resolve_full_jd=False,
        generate=False,
        output_root=tmp_path,
        client=client,
    )
    assert result.outcome == triage.ACCEPT
    assert result.roles[0].package.resume_path is None
    assert len(client.calls) == 1


def test_extraction_failure_needs_review_without_llm_call():
    message = EmailMessage(
        id="msg-unparseable",
        from_address="jobs@example.com",
        subject="Job opportunity",
        body_plain="We have an exciting opportunity for an engineer. Apply now!",
    )
    result = triage.triage_message(message, client=_FakeClient())
    # No ATS link/company signal for the regex extractor to latch onto.
    assert result.outcome in (triage.NEEDS_REVIEW, triage.DENY)
    if result.outcome == triage.NEEDS_REVIEW:
        assert result.roles == []
