"""Extra edge coverage for llm_extract helpers."""

from __future__ import annotations

import json

import pytest

from job_tracker.email.models import EmailMessage
from job_tracker.pipeline import llm_extract


def test_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm_extract.LLMExtractionError, match="ANTHROPIC_API_KEY"):
        llm_extract._client()


def test_parse_response_rejects_non_list():
    with pytest.raises(llm_extract.LLMExtractionError, match="JSON array"):
        llm_extract._parse_response_text('{"company": "Acme"}')


def test_parse_response_tolerates_trailing_prose():
    """Regression test (found live 2026-07-17 via scan_communications.py on
    non-digest content): a model can close the JSON array correctly and
    then add an unfenced explanatory sentence afterward — that's still a
    usable answer, not a parse failure."""
    text = '[{"company": "Acme", "title": "SWE", "confidence": 0.9}]\nHope that helps!'
    data = llm_extract._parse_response_text(text)
    assert data == [{"company": "Acme", "title": "SWE", "confidence": 0.9}]


def test_parse_response_still_raises_on_genuinely_invalid_json():
    with pytest.raises(json.JSONDecodeError):
        llm_extract._parse_response_text("not json at all")


def test_cost_usd_unknown_model():
    assert llm_extract._cost_usd("unknown-model", 100, 50) is None


def test_items_to_roles_skips_junk():
    roles = llm_extract._items_to_roles(
        [
            "not-a-dict",
            {"company": "", "title": ""},
            {"company": "Acme", "title": "SWE", "confidence": "bad"},
            {"company": "Beta", "title": "DE", "confidence": 1.5, "excerpt": "snippet"},
        ]
    )
    assert len(roles) == 2
    assert roles[0].confidence == 0.5
    assert roles[1].confidence == 1.0
    assert roles[1].snippet == "snippet"


def test_call_llm_raw_prints_and_parses(capsys):
    class _Block:
        type = "text"
        text = json.dumps([{"company": "Acme", "title": "SWE", "confidence": 0.9}])

    class _Usage:
        input_tokens = 10
        output_tokens = 5

    class _Msg:
        content = [_Block()]
        usage = _Usage()

    class _Client:
        messages = type("M", (), {"create": staticmethod(lambda **k: _Msg())})()

    msg = EmailMessage(id="m", from_address="a@b.com", subject="s", body_plain="body")
    items = llm_extract._call_llm_raw(msg, model=llm_extract.DEFAULT_MODEL, client=_Client())
    assert items[0]["company"] == "Acme"
    out = capsys.readouterr().out
    assert "llm extract" in out
