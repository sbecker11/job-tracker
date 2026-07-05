"""Tests for LLM-driven JD evaluation + résumé/cover-letter generation
(pipeline/llm_apply.py).

No real Anthropic API calls are made here — the client is always a fake
stand-in so the test suite runs offline and free of charge. The candidate
profile is also faked via an autouse fixture so tests don't depend on
~/Wisdom/CLAUDE.md existing on the machine running them.
"""

from __future__ import annotations

import json

import pytest
from docx import Document

from job_tracker.pipeline import llm_apply


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
    """Stands in for anthropic.Anthropic. Scripted with a queue of responses
    consumed in call order, so multi-call flows (JSON repair, house-rule
    repair, evaluate-then-generate) can be tested deterministically."""

    def __init__(self, responses: list | None = None, error: Exception | None = None):
        # Each item is either a raw text string (defaults: 100 in / 50 out
        # tokens) or an (text, input_tokens, output_tokens) tuple.
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("FakeClient ran out of scripted responses")
        item = self.responses.pop(0)
        if isinstance(item, tuple):
            text, input_tokens, output_tokens = item
        else:
            text, input_tokens, output_tokens = item, 100, 50
        return _FakeMessage(text, input_tokens, output_tokens)


@pytest.fixture(autouse=True)
def fake_profile(monkeypatch, tmp_path):
    """Point the module at a small throwaway candidate profile for every
    test, so nothing here depends on ~/Wisdom/CLAUDE.md existing."""
    profile_path = tmp_path / "CLAUDE.md"
    profile_path.write_text("FAKE CANDIDATE PROFILE FOR TESTS", encoding="utf-8")
    monkeypatch.setattr(llm_apply, "_CANDIDATE_PROFILE_PATH", profile_path)
    return profile_path


_EVAL_PAYLOAD_PURSUE = json.dumps(
    {
        "dealbreaker_notes": ["No dealbreakers fire."],
        "skills_alignment": ["Python: strong match."],
        "match_pct": 85,
        "verdict": "pursue",
        "rationale": "Strong overall fit.",
    }
)

_EVAL_PAYLOAD_PASS = json.dumps(
    {
        "dealbreaker_notes": ["Angular required as sole frontend framework."],
        "skills_alignment": [],
        "match_pct": 10,
        "verdict": "pass",
        "rationale": "Dealbreaker fires.",
    }
)

_CLEAN_CONTENT = {
    "resume": {
        "positioning_line": "Senior Software Engineer",
        "summary": "A summary.",
        "skills": ["Python", "React"],
        "experience": [
            {"employer": "HomePortfolio", "dates": "1997-2002", "role_note": "CTO", "bullets": ["Did things."], "subsections": []},
            {"employer": "Spexture (Independent Consulting)", "dates": "2019-present", "role_note": None, "bullets": ["Built things."], "subsections": [
                {"heading": "Portfolio Projects", "bullets": ["Built linkage-engine."]},
            ]},
        ],
        "education": ["PhD, Computer Vision — MIT Media Lab"],
    },
    "cover_letter": {
        "salutation": "Dear Hiring Team,",
        "paragraphs": ["I am excited to apply.", "I have relevant experience."],
    },
}


# --- evaluate_lead ---------------------------------------------------------


def test_evaluate_lead_parses_response_and_computes_metrics():
    client = _FakeClient(responses=[(_EVAL_PAYLOAD_PURSUE, 1000, 200)])
    result = llm_apply.evaluate_lead("some JD text", company="Acme", title="Engineer", model="claude-haiku-4-5", client=client)

    assert result.verdict == "pursue"
    assert result.match_pct == 85
    assert result.dealbreaker_notes == ["No dealbreakers fire."]
    assert result.skills_alignment == ["Python: strong match."]
    assert result.rationale == "Strong overall fit."
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "claude-haiku-4-5"

    assert result.metrics is not None
    assert result.metrics.step == "evaluate"
    assert result.metrics.input_tokens == 1000
    assert result.metrics.output_tokens == 200
    assert result.metrics.cost_usd == pytest.approx(1000 / 1_000_000 * 1.00 + 200 / 1_000_000 * 5.00)


def test_evaluate_lead_defaults_verdict_to_review_when_missing():
    payload = json.dumps({"match_pct": 50})
    client = _FakeClient(responses=[payload])
    result = llm_apply.evaluate_lead("JD", company="Acme", title="Eng", model="claude-haiku-4-5", client=client)
    assert result.verdict == "review"


# --- JSON parsing / repair retry --------------------------------------------


def test_call_and_parse_json_repairs_invalid_json_with_one_retry():
    broken = '{"a": "unescaped " quote"}'
    fixed = json.dumps({"a": "fixed"})
    client = _FakeClient(responses=[(broken, 500, 500), (fixed, 200, 50)])

    data, calls = llm_apply._call_and_parse_json(
        "system", "user", model="claude-haiku-4-5", client=client, step="evaluate"
    )

    assert data == {"a": "fixed"}
    assert len(client.calls) == 2
    assert [c.step for c in calls] == ["evaluate", "evaluate_json_repair"]


def test_call_and_parse_json_strips_markdown_fence():
    payload = "```json\n" + json.dumps({"x": 1}) + "\n```"
    client = _FakeClient(responses=[payload])
    data, calls = llm_apply._call_and_parse_json("s", "u", model="claude-haiku-4-5", client=client, step="generate")
    assert data == {"x": 1}
    assert len(calls) == 1


def test_call_and_parse_json_raises_if_repair_also_fails():
    client = _FakeClient(responses=["not json", "still not json"])
    with pytest.raises(json.JSONDecodeError):
        llm_apply._call_and_parse_json("s", "u", model="claude-haiku-4-5", client=client, step="evaluate")


# --- house rule checks -------------------------------------------------------


def test_check_house_rules_flags_banned_term():
    content = {"resume": {"summary": "Worked at Cambria Health for years."}, "cover_letter": {}}
    warnings = llm_apply._check_house_rules(content, company="Acme")
    assert any("Cambria" in w for w in warnings)


def test_check_house_rules_flags_compensation_figure():
    content = {
        "resume": {},
        "cover_letter": {"paragraphs": ["I target $90/hr on W2, roughly $105/hr on C2C."]},
    }
    warnings = llm_apply._check_house_rules(content, company="Bellese")
    assert any("compensation" in w for w in warnings)


def test_check_house_rules_flags_work_authorization_statement():
    content = {
        "resume": {},
        "cover_letter": {"paragraphs": ["I am a US citizen and eligible for Public Trust clearance."]},
    }
    warnings = llm_apply._check_house_rules(content, company="Bellese")
    assert any("work-authorization" in w for w in warnings)


def test_check_house_rules_clean_content_has_no_warnings():
    warnings = llm_apply._check_house_rules(_CLEAN_CONTENT, company="Acme")
    assert warnings == []


# --- house rule auto-repair --------------------------------------------------


def test_repair_house_rule_violations_calls_model_and_returns_fixed_content():
    dirty = {"cover_letter": {"paragraphs": ["I am a US citizen."]}}
    clean = {"cover_letter": {"paragraphs": ["I bring relevant experience."]}}
    client = _FakeClient(responses=[(json.dumps(clean), 300, 100)])

    repaired, calls = llm_apply._repair_house_rule_violations(
        dirty, issues=["possible work-authorization statement"], model="claude-haiku-4-5", client=client
    )

    assert repaired == clean
    assert len(calls) == 1
    assert calls[0].step == "generate_house_rule_repair"


def test_repair_house_rule_violations_keeps_original_on_failure():
    dirty = {"cover_letter": {"paragraphs": ["I am a US citizen."]}}
    client = _FakeClient(responses=["not json", "still not json"])
    repaired, calls = llm_apply._repair_house_rule_violations(
        dirty, issues=["issue"], model="claude-haiku-4-5", client=client
    )
    assert repaired == dirty
    assert calls == []


# --- generate_package end-to-end --------------------------------------------


def test_generate_package_skips_generation_when_verdict_is_not_pursue(tmp_path):
    client = _FakeClient(responses=[(_EVAL_PAYLOAD_PASS, 900, 150)])
    result = llm_apply.generate_package(
        "JD text", company="Acme", title="Engineer", model="claude-haiku-4-5", client=client, output_root=tmp_path
    )

    assert result.evaluation.verdict == "pass"
    assert result.resume_path is None
    assert result.cover_letter_path is None
    assert result.generate_metrics is None
    assert len(client.calls) == 1  # only the evaluate call, no generation
    assert result.total_input_tokens == 900
    assert result.total_output_tokens == 150


def test_generate_package_full_flow_saves_docx_and_aggregates_metrics(tmp_path):
    client = _FakeClient(
        responses=[
            (_EVAL_PAYLOAD_PURSUE, 1000, 200),
            (json.dumps(_CLEAN_CONTENT), 2000, 3000),
        ]
    )
    result = llm_apply.generate_package(
        "JD text", company="Bellese", title="Senior Engineer", model="claude-haiku-4-5", client=client, output_root=tmp_path
    )

    assert result.evaluation.verdict == "pursue"
    assert result.resume_path is not None and result.resume_path.exists()
    assert result.cover_letter_path is not None and result.cover_letter_path.exists()
    assert result.warnings == []

    assert result.generate_metrics.input_tokens == 2000
    assert result.generate_metrics.output_tokens == 3000
    assert result.total_input_tokens == 1000 + 2000
    assert result.total_output_tokens == 200 + 3000
    expected_cost = (
        (1000 / 1_000_000 * 1.00 + 200 / 1_000_000 * 5.00)
        + (2000 / 1_000_000 * 1.00 + 3000 / 1_000_000 * 5.00)
    )
    assert result.total_cost_usd == pytest.approx(expected_cost)

    # HomePortfolio (listed first in _CLEAN_CONTENT) must be rendered last.
    doc = Document(result.resume_path)
    employer_lines = [p.text for p in doc.paragraphs if "HomePortfolio" in p.text or "Spexture" in p.text]
    assert employer_lines[-1].startswith("HomePortfolio")


def test_generate_package_auto_repairs_house_rule_violation_before_saving(tmp_path):
    dirty_content = {
        "resume": _CLEAN_CONTENT["resume"],
        "cover_letter": {"salutation": "Dear Hiring Team,", "paragraphs": ["I am a US citizen and eligible for Public Trust clearance."]},
    }
    repaired_content = {
        "resume": _CLEAN_CONTENT["resume"],
        "cover_letter": {"salutation": "Dear Hiring Team,", "paragraphs": ["I bring deep relevant experience."]},
    }
    client = _FakeClient(
        responses=[
            (_EVAL_PAYLOAD_PURSUE, 1000, 200),
            (json.dumps(dirty_content), 1500, 1500),
            (json.dumps(repaired_content), 400, 300),
        ]
    )
    result = llm_apply.generate_package(
        "JD text", company="Bellese", title="Senior Engineer", model="claude-haiku-4-5", client=client, output_root=tmp_path
    )

    assert result.warnings == []  # cleared after the repair pass
    assert len(client.calls) == 3  # evaluate, generate, house-rule repair
    # Repair-call tokens must be folded into the generate step total.
    assert result.generate_metrics.input_tokens == 1500 + 400
    assert result.generate_metrics.output_tokens == 1500 + 300

    doc = Document(result.cover_letter_path)
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "citizen" not in full_text.lower()
    assert "deep relevant experience" in full_text


def test_generate_package_leaves_warnings_if_repair_cannot_clear_them(tmp_path):
    dirty_content = {
        "resume": _CLEAN_CONTENT["resume"],
        "cover_letter": {"paragraphs": ["I am a US citizen."]},
    }
    still_dirty_content = {
        "resume": _CLEAN_CONTENT["resume"],
        "cover_letter": {"paragraphs": ["I am still a US citizen, for emphasis."]},
    }
    client = _FakeClient(
        responses=[
            (_EVAL_PAYLOAD_PURSUE, 1000, 200),
            (json.dumps(dirty_content), 1000, 1000),
            (json.dumps(still_dirty_content), 400, 300),
        ]
    )
    result = llm_apply.generate_package(
        "JD text", company="Bellese", title="Senior Engineer", model="claude-haiku-4-5", client=client, output_root=tmp_path
    )

    assert result.warnings  # repair pass didn't fully clear the violation
    assert result.resume_path is not None  # still saved — warnings are non-blocking


# --- cost/metrics helpers -----------------------------------------------------


def test_cost_usd_known_model():
    cost = llm_apply._cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(1.00 + 5.00)


def test_cost_usd_unknown_model_returns_none():
    assert llm_apply._cost_usd("some-future-model", 1000, 1000) is None


def test_sum_metrics_aggregates_multiple_calls():
    calls = [
        llm_apply.CallMetrics(step="generate", model="claude-haiku-4-5", input_tokens=100, output_tokens=50, elapsed_s=1.0, cost_usd=0.001),
        llm_apply.CallMetrics(step="generate_house_rule_repair", model="claude-haiku-4-5", input_tokens=40, output_tokens=20, elapsed_s=0.5, cost_usd=0.0005),
    ]
    total = llm_apply._sum_metrics("generate", "claude-haiku-4-5", calls)
    assert total.input_tokens == 140
    assert total.output_tokens == 70
    assert total.elapsed_s == pytest.approx(1.5)
    assert total.cost_usd == pytest.approx(0.0015)


def test_sum_metrics_handles_empty_call_list():
    total = llm_apply._sum_metrics("generate", "claude-haiku-4-5", [])
    assert total.input_tokens == 0
    assert total.cost_usd is None


# --- rendering helpers --------------------------------------------------------


def test_safe_filename_strips_unsafe_characters():
    assert llm_apply._safe_filename("Shawn Becker: Résumé (Draft)!.docx") == "Shawn_Becker_Rsum_Draft.docx"


def test_render_resume_places_homeportfolio_last_regardless_of_input_order(tmp_path):
    resume = {
        "experience": [
            {"employer": "HomePortfolio", "dates": "1997-2002", "bullets": ["a"]},
            {"employer": "Sierra Vista Group", "dates": "2002-2011", "bullets": ["b"]},
        ]
    }
    path = llm_apply.render_resume(resume, company="Acme", title="Engineer", out_dir=tmp_path)
    doc = Document(path)
    employer_paras = [p.text for p in doc.paragraphs if "HomePortfolio" in p.text or "Sierra Vista" in p.text]
    assert employer_paras[0].startswith("Sierra Vista")
    assert employer_paras[1].startswith("HomePortfolio")


def test_render_cover_letter_includes_phone_and_salutation(tmp_path):
    cover_letter = {"salutation": "Dear Hiring Team,", "paragraphs": ["Body paragraph."]}
    path = llm_apply.render_cover_letter(cover_letter, company="Acme", title="Engineer", out_dir=tmp_path)
    doc = Document(path)
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert llm_apply.CANDIDATE_PHONE in full_text
    assert "Dear Hiring Team," in full_text
    assert "Body paragraph." in full_text


# --- candidate profile loading -------------------------------------------------


def test_load_candidate_profile_raises_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_apply, "_CANDIDATE_PROFILE_PATH", tmp_path / "does_not_exist.md")
    with pytest.raises(llm_apply.LLMApplyError):
        llm_apply._load_candidate_profile()
