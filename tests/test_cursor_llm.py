"""The Grok adapter, with Agent.prompt stubbed. No test calls Cursor."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from job_tracker.pipeline import cursor_llm
from job_tracker.pipeline.cursor_llm import (
    GROK_HIGH_FAST,
    CursorLLMError,
    CursorMessages,
    require_grok,
)


class _Usage:
    input_tokens = 11
    output_tokens = 7
    cache_read_tokens = 3
    cache_write_tokens = 2


class _Result:
    status = "finished"
    result = '{"ok": true}'
    usage = _Usage()


def test_create_maps_prompt_text_and_tokens(monkeypatch):
    seen = {}

    def prompt(message, options=None, **_kwargs):
        seen["message"] = message
        seen["model"] = options.model.id
        seen["params"] = {item.id: item.value for item in options.model.params}
        seen["tools"] = options.tools
        seen["cwd"] = options.local.cwd
        return _Result()

    monkeypatch.setattr(cursor_llm, "Agent", SimpleNamespace(prompt=prompt))
    client = CursorMessages(api_key_value="cursor_test")
    response = client.messages.create(
        model=GROK_HIGH_FAST,
        max_tokens=32,
        system=[{"type": "text", "text": "Return JSON", "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "hello"}],
        thinking={"type": "disabled"},
    )
    assert response.content[0].type == "text"
    assert response.content[0].text == '{"ok": true}'
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 7
    assert response.usage.cache_read_input_tokens == 3
    assert response.usage.cache_creation_input_tokens == 2
    assert "Return JSON" in seen["message"]
    assert "hello" in seen["message"]
    assert seen["model"] == GROK_HIGH_FAST
    assert seen["params"]["reasoning_effort"] == "high"
    assert seen["params"]["fast"] == "true"
    assert seen["tools"] == []
    assert seen["cwd"]


def test_claude_model_is_refused():
    with pytest.raises(CursorLLMError, match="only Grok"):
        require_grok("claude-sonnet-5")
    assert require_grok("") == GROK_HIGH_FAST
    assert require_grok(None) == GROK_HIGH_FAST


def test_missing_key(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    with pytest.raises(CursorLLMError, match="CURSOR_API_KEY"):
        CursorMessages()
