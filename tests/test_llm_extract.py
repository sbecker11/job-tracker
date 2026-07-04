"""Tests for the LLM extraction fallback (pipeline/llm_extract.py).

No real Anthropic API calls are made here — the client is always a fake
stand-in so the test suite runs offline and free of charge.
"""

from __future__ import annotations

import json
from pathlib import Path

from job_tracker.email.models import EmailMessage
from job_tracker.pipeline import llm_extract, store


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text: str):
        self.content = [_FakeBlock(text)]


class _FakeClient:
    """Stands in for anthropic.Anthropic; records calls, never hits the network."""

    def __init__(self, response_text: str = "[]", error: Exception | None = None):
        self.response_text = response_text
        self.error = error
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _FakeMessage(self.response_text)


def _message(msg_id: str = "m1") -> EmailMessage:
    return EmailMessage(
        id=msg_id,
        from_address="digest@example-job-board.com",
        subject="3 new jobs matching your search",
        body_plain="Senior Backend Engineer at Acme Corp\nData Analyst at Beta Inc",
    )


def test_extract_roles_llm_parses_clean_json_array():
    payload = json.dumps(
        [
            {"company": "Acme Corp", "title": "Senior Backend Engineer", "apply_url": "", "confidence": 0.9},
            {"company": "Beta Inc", "title": "Data Analyst", "apply_url": "", "confidence": 0.8},
        ]
    )
    client = _FakeClient(response_text=payload)
    roles = llm_extract.extract_roles_llm(_message(), client=client)

    assert len(roles) == 2
    assert roles[0].company == "Acme Corp"
    assert roles[0].title == "Senior Backend Engineer"
    assert roles[0].source == "llm_fallback"
    assert roles[0].confidence == 0.9
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == llm_extract.DEFAULT_MODEL


def test_extract_roles_llm_strips_markdown_code_fence():
    payload = "```json\n" + json.dumps([{"company": "Acme", "title": "Engineer", "confidence": 0.7}]) + "\n```"
    client = _FakeClient(response_text=payload)
    roles = llm_extract.extract_roles_llm(_message(), client=client)
    assert len(roles) == 1
    assert roles[0].company == "Acme"


def test_extract_roles_llm_returns_empty_list_for_no_postings():
    client = _FakeClient(response_text="[]")
    roles = llm_extract.extract_roles_llm(_message(), client=client)
    assert roles == []


def test_extract_roles_llm_drops_items_missing_both_fields():
    payload = json.dumps(
        [
            {"company": "", "title": "", "confidence": 0.9},
            {"company": "Acme", "title": "", "confidence": 0.4},
        ]
    )
    client = _FakeClient(response_text=payload)
    roles = llm_extract.extract_roles_llm(_message(), client=client)
    # The fully-empty item is dropped; the partial one (company only) is kept
    # so it can still be flagged for manual review downstream, same as the
    # regex extractor's partial-match behavior.
    assert len(roles) == 1
    assert roles[0].company == "Acme"
    assert roles[0].title == ""


def test_extract_roles_llm_never_raises_on_client_error():
    client = _FakeClient(error=RuntimeError("network exploded"))
    roles = llm_extract.extract_roles_llm(_message(), client=client)
    assert roles == []


def test_extract_roles_llm_never_raises_on_unparseable_response():
    client = _FakeClient(response_text="not json at all")
    roles = llm_extract.extract_roles_llm(_message(), client=client)
    assert roles == []


def test_extract_roles_llm_clamps_out_of_range_confidence():
    payload = json.dumps([{"company": "Acme", "title": "Engineer", "confidence": 5.0}])
    client = _FakeClient(response_text=payload)
    roles = llm_extract.extract_roles_llm(_message(), client=client)
    assert roles[0].confidence == 1.0


def test_cached_wrapper_calls_llm_once_then_reuses_cache(tmp_path: Path):
    conn = store.connect(tmp_path / "leads.db")
    payload = json.dumps([{"company": "Acme", "title": "Engineer", "confidence": 0.8}])
    client = _FakeClient(response_text=payload)
    message = _message("cached-msg")

    first = llm_extract.extract_roles_llm_cached(conn, message, client=client)
    second = llm_extract.extract_roles_llm_cached(conn, message, client=client)

    assert len(client.calls) == 1  # second call was served from the cache
    assert len(first) == 1 and len(second) == 1
    assert first[0].company == second[0].company == "Acme"
    conn.close()


def test_cached_wrapper_caches_empty_result_too(tmp_path: Path):
    """A genuinely-empty digest must not be re-billed on every future run."""
    conn = store.connect(tmp_path / "leads.db")
    client = _FakeClient(response_text="[]")
    message = _message("empty-msg")

    first = llm_extract.extract_roles_llm_cached(conn, message, client=client)
    second = llm_extract.extract_roles_llm_cached(conn, message, client=client)

    assert first == []
    assert second == []
    assert len(client.calls) == 1
    conn.close()


def test_cached_wrapper_does_not_cache_a_failed_call(tmp_path: Path):
    """A transient failure should be retried on the next run, not baked in."""
    conn = store.connect(tmp_path / "leads.db")
    client = _FakeClient(error=RuntimeError("boom"))
    message = _message("failed-msg")

    llm_extract.extract_roles_llm_cached(conn, message, client=client)
    llm_extract.extract_roles_llm_cached(conn, message, client=client)

    assert len(client.calls) == 2  # not cached, so both calls actually hit the fake client
    conn.close()
