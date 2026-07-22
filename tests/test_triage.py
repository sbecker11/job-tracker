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
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, record_rejection, upsert_lead


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
            "dealbreaker_checks": [],
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
    assert result.outcome == triage.SKIP
    assert result.roles == []
    # Correctly identified as not job content — nothing was ever there to extract.
    assert result.extraction_complete is True


def test_rejection_message_denied_without_llm_call():
    result = triage.triage_message(_rejection_message(), client=_FakeClient())
    assert result.outcome == triage.SKIP


def test_recruiter_outreach_needs_review_without_llm_call():
    result = triage.triage_message(_outreach_message(), client=_FakeClient())
    assert result.outcome == triage.NEEDS_REVIEW
    assert result.classifier_label == "recruiter-outreach"
    # No JD to score by design — extraction was never even attempted.
    assert result.extraction_complete is False


# --- single-JD -> LLM evaluate (+ generate) ----------------------------------


def test_single_jd_pursue_verdict_accepts_and_generates(tmp_path):
    client = _FakeClient(responses=[(_eval_payload("pursue", 85), 900, 200), (_GENERATE_PAYLOAD, 1200, 800)])
    result = triage.triage_message(
        _single_jd_message(),
        resolve_full_jd=False,
        force_llm_review=True,
        output_root=tmp_path,
        client=client,
    )
    assert result.outcome == triage.PURSUE
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
        force_llm_review=True,
        output_root=tmp_path,
        client=client,
    )
    assert result.outcome == triage.SKIP
    assert result.roles[0].package.resume_path is None
    assert len(client.calls) == 1  # evaluate only, no generate call spent


def test_single_jd_lead_leaves_direct_recruiter_outreach_undecided(tmp_path):
    """2026-07-21 redesign: `direct_recruiter_outreach` is exclusively
    human-decided (via `review_direct_recruiter_outreach.py`), never set by
    the ingestion pipeline — every lead triage.py creates must leave it at
    its default `None` ("not yet reviewed"), regardless of the message's
    content."""
    client = _FakeClient(responses=[(_eval_payload("pursue", 85), 900, 200), (_GENERATE_PAYLOAD, 1200, 800)])
    result = triage.triage_message(
        _single_jd_message(),
        resolve_full_jd=False,
        force_llm_review=True,
        output_root=tmp_path,
        client=client,
    )
    assert result.roles[0].lead.direct_recruiter_outreach is None


def test_single_jd_review_verdict_needs_review(tmp_path):
    client = _FakeClient(responses=[(_eval_payload("review", 40), 900, 200)])
    result = triage.triage_message(
        _single_jd_message(),
        resolve_full_jd=False,
        force_llm_review=True,
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
        force_llm_review=True,
        output_root=tmp_path,
        client=client,
    )
    assert result.outcome == triage.PURSUE
    assert result.roles[0].package.resume_path is None
    assert len(client.calls) == 1


# --- rejection cooldown disqualification (no LLM call) ----------------------


def test_recently_rejected_role_is_disqualified_without_llm_call(tmp_path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(company="Acme", title="Software Engineer", source_message_id="m0", source_label="single-jd")
    upsert_lead(conn, lead)
    record_rejection(conn, lead.normalized_key, when="2026-07-01T00:00:00+00:00")

    client = _FakeClient()  # no responses queued — a call would raise IndexError
    result = triage.triage_message(
        _single_jd_message(),
        resolve_full_jd=False,
        conn=conn,
        client=client,
    )
    assert result.outcome == triage.SKIP
    assert len(result.roles) == 1
    role = result.roles[0]
    assert role.package.evaluation is None  # never ran the LLM stage
    assert "disqualified" in role.lead.rationale[0]
    assert len(client.calls) == 0
    conn.close()


def test_rejection_outside_cooldown_window_is_not_disqualified(tmp_path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(company="Acme", title="Software Engineer", source_message_id="m0", source_label="single-jd")
    upsert_lead(conn, lead)
    # Stamp a rejection far enough in the past that the default 90-day
    # cooldown has already elapsed relative to "now" — should score normally.
    record_rejection(conn, lead.normalized_key, when="2020-01-01T00:00:00+00:00")

    client = _FakeClient(responses=[(_eval_payload("pursue", 85), 900, 200), (_GENERATE_PAYLOAD, 1200, 800)])
    result = triage.triage_message(
        _single_jd_message(),
        resolve_full_jd=False,
        force_llm_review=True,
        output_root=tmp_path,
        conn=conn,
        client=client,
    )
    assert result.outcome == triage.PURSUE
    conn.close()


def test_rejection_cooldown_days_zero_disables_disqualification(tmp_path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(company="Acme", title="Software Engineer", source_message_id="m0", source_label="single-jd")
    upsert_lead(conn, lead)
    record_rejection(conn, lead.normalized_key, when="2026-07-01T00:00:00+00:00")

    client = _FakeClient(responses=[(_eval_payload("pursue", 85), 900, 200), (_GENERATE_PAYLOAD, 1200, 800)])
    result = triage.triage_message(
        _single_jd_message(),
        resolve_full_jd=False,
        force_llm_review=True,
        output_root=tmp_path,
        conn=conn,
        client=client,
        rejection_cooldown_days=0,
    )
    assert result.outcome == triage.PURSUE
    conn.close()


def test_unrelated_rejection_does_not_disqualify_a_different_role(tmp_path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(company="Widgetco", title="Data Engineer", source_message_id="m0", source_label="single-jd")
    upsert_lead(conn, lead)
    record_rejection(conn, lead.normalized_key, when="2026-07-01T00:00:00+00:00")

    client = _FakeClient(responses=[(_eval_payload("pursue", 85), 900, 200), (_GENERATE_PAYLOAD, 1200, 800)])
    result = triage.triage_message(
        _single_jd_message(),  # Acme / Software Engineer — unrelated to Widgetco's rejection
        resolve_full_jd=False,
        force_llm_review=True,
        output_root=tmp_path,
        conn=conn,
        client=client,
    )
    assert result.outcome == triage.PURSUE
    conn.close()


def test_extraction_failure_needs_review_without_llm_call():
    message = EmailMessage(
        id="msg-unparseable",
        from_address="jobs@example.com",
        subject="Job opportunity",
        body_plain="We have an exciting opportunity for an engineer. Apply now!",
    )
    result = triage.triage_message(message, client=_FakeClient())
    # No ATS link/company signal for the regex extractor to latch onto.
    assert result.outcome in (triage.NEEDS_REVIEW, triage.SKIP)
    if result.outcome == triage.NEEDS_REVIEW:
        assert result.roles == []


# --- multi-JD-in-body: per-role snippet isolation ---------------------------


def _multi_jd_message(**overrides) -> EmailMessage:
    fields = dict(
        id="msg-multi",
        from_address="careers@startup.example",
        subject="We're hiring — open roles at StartupCo",
        snippet="Multiple engineering openings",
        body_plain=(
            "Hi Shawn,\n\nStartupCo has several open roles:\n\n"
            "- Senior Software Engineer — Python and distributed systems\n"
            "- Full Stack Engineer — must be onsite 5 days a week\n\n"
            "Apply:\nhttps://jobs.lever.co/startupco/senior-backend\n"
            "https://jobs.lever.co/startupco/fullstack\n\nThanks,\nStartupCo People Team"
        ),
    )
    fields.update(overrides)
    return EmailMessage(**fields)


def test_multi_jd_roles_scored_against_own_snippet_not_whole_digest(tmp_path):
    """Regression (2026-07-07): before ExtractedRole.snippet existed, every
    role fanned out of a digest whose ATS lookup failed was scored against
    `message.combined_text` — i.e. the ENTIRE digest, including sibling
    roles' requirements. Here, the "Full Stack Engineer" bullet's "onsite 5
    days a week" language must never leak into the "Senior Software
    Engineer" role's jd_text (and vice versa for "distributed systems")."""
    client = _FakeClient(
        responses=[
            (_eval_payload("review", 50), 900, 200),
            (_eval_payload("review", 50), 900, 200),
        ]
    )
    result = triage.triage_message(
        _multi_jd_message(),
        resolve_full_jd=False,
        generate=False,
        output_root=tmp_path,
        client=client,
    )
    by_title = {r.lead.title: r.lead for r in result.roles}
    senior_lead = by_title["Senior Software Engineer"]
    fullstack_lead = by_title["Full Stack Engineer"]

    assert "distributed systems" in senior_lead.jd_text
    assert "onsite" not in senior_lead.jd_text
    assert "onsite" in fullstack_lead.jd_text
    assert "distributed systems" not in fullstack_lead.jd_text
    assert senior_lead.jd_source == "digest_snippet"
    assert fullstack_lead.jd_source == "digest_snippet"


# --- link-only-digest extraction fallback (opt-in) --------------------------


def _digest_message(**overrides) -> EmailMessage:
    fields = dict(
        id="msg-digest",
        from_address="jobs@my.theladders.com",
        subject="New Jobs Posted: Apply before others do",
        body_plain=(
            "Jobs Posted in the Last 24 Hours\n\n"
            "Software Development Manager - Merge Hemo\n$155K - $232K* | Remote\n"
            "https://my.theladders.com/apply/1\n\n"
            "Core Engineering - Hands-on Engineer Leader\n$150K - $200K* | Salt Lake City, UT\n"
            "https://my.theladders.com/apply/2\n"
        ),
    )
    fields.update(overrides)
    return EmailMessage(**fields)


def _extract_payload(items: list[dict]) -> str:
    return json.dumps(items)


def test_link_only_digest_without_fallback_needs_review_no_extraction_call():
    result = triage.triage_message(_digest_message(), client=_FakeClient())
    assert result.classifier_label == "link-only-digest"
    assert result.outcome == triage.NEEDS_REVIEW
    assert result.roles == []
    assert result.extraction_complete is False


def test_link_only_digest_with_llm_fallback_extracts_and_scores_roles(tmp_path):
    extract_client = _FakeClient(
        responses=[
            _extract_payload(
                [
                    {"company": "Merge Hemo", "title": "Software Development Manager", "confidence": 0.9},
                    {"company": "Goldman Sachs", "title": "Core Engineering Leader", "confidence": 0.8},
                ]
            )
        ]
    )
    eval_client = _FakeClient(
        responses=[_eval_payload("review", 40), _eval_payload("pursue", 85), _GENERATE_PAYLOAD]
    )
    result = triage.triage_message(
        _digest_message(),
        resolve_full_jd=False,
        force_llm_review=True,
        output_root=tmp_path,
        client=eval_client,
        use_llm_extraction_fallback=True,
        llm_extract_client=extract_client,
    )
    assert result.classifier_label == "link-only-digest"
    assert len(result.roles) == 2
    assert {r.lead.company for r in result.roles} == {"Merge Hemo", "Goldman Sachs"}
    assert result.outcome == triage.PURSUE  # at least one role scored "pursue"
    # Found fewer roles than the cap — no truncation, nothing left on the table.
    assert result.extraction_complete is True


def test_link_only_digest_llm_fallback_finds_nothing_still_needs_review():
    extract_client = _FakeClient(responses=[_extract_payload([])])
    result = triage.triage_message(
        _digest_message(),
        client=_FakeClient(),
        use_llm_extraction_fallback=True,
        llm_extract_client=extract_client,
    )
    assert result.outcome == triage.NEEDS_REVIEW
    assert result.roles == []
    assert result.extraction_complete is False


def test_link_only_digest_fallback_caps_roles_evaluated_by_confidence(tmp_path):
    extract_client = _FakeClient(
        responses=[
            _extract_payload(
                [
                    {"company": "LowCo", "title": "Engineer A", "confidence": 0.3},
                    {"company": "HighCo", "title": "Engineer B", "confidence": 0.95},
                    {"company": "MidCo", "title": "Engineer C", "confidence": 0.6},
                ]
            )
        ]
    )
    eval_client = _FakeClient(responses=[_eval_payload("pass", 10)])
    result = triage.triage_message(
        _digest_message(),
        resolve_full_jd=False,
        force_llm_review=True,
        output_root=tmp_path,
        client=eval_client,
        use_llm_extraction_fallback=True,
        llm_extract_client=extract_client,
        max_llm_extracted_roles=1,
    )
    # Only the single highest-confidence role should have been evaluated.
    assert len(result.roles) == 1
    assert result.roles[0].lead.company == "HighCo"
    assert len(eval_client.calls) == 1
    # 3 complete roles found but capped to 1 — there may be more left out.
    assert result.extraction_complete is False
