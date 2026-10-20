"""LLM catch-up pass for parked unmatched InMails (pipeline/unmatched_llm_promote.py).

No real Anthropic API calls — the client is a fake that records calls.
"""

from __future__ import annotations

import json
from pathlib import Path

from job_tracker.pipeline.models import UnmatchedMessage
from job_tracker.pipeline.store import (
    connect,
    dismiss_unmatched_message,
    get_unmatched_message,
    list_unmatched_messages,
    record_unmatched_message,
)
from job_tracker.pipeline.unmatched_llm_promote import promote_unmatched_with_llm


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str):
        self.content = [_FakeBlock(text)]


class _FakeClient:
    def __init__(self, response_text: str = "[]", error: Exception | None = None):
        self.response_text = response_text
        self.error = error
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.response_text)


ED = """
Building high-volume data products with Python and AWS

        Ed Soucie
        Reply
        https://www.linkedin.com/messaging/thread/2-ED-THREAD/

I'm partnering directly with the CTO and Data Products leadership at Intentsify, an AI-powered
buyer intelligence company. This is a backend engineering role.
"""


def _park(conn, message_id: str, *, subject: str, body: str, thread: str) -> None:
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id=message_id,
            thread_id=thread,
            direction="inbound",
            from_address="inmail-hit-reply@linkedin.com",
            to_address="shawn.becker@spexture.com",
            subject=subject,
            body_text=body,
        ),
    )


def _roles(*pairs: tuple[str, str, float]) -> str:
    return json.dumps(
        [{"company": c, "title": t, "apply_url": "", "confidence": conf, "excerpt": ""} for c, t, conf in pairs]
    )


def test_promotes_single_role_and_caches(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    _park(conn, "imap:ed-1", subject="Building high-volume data products with Python and AWS", body=ED, thread="t-ed")
    client = _FakeClient(_roles(("Intentsify", "Backend Software Engineer", 0.8)))

    results = promote_unmatched_with_llm(conn, client=client)
    assert len(results) == 1
    assert results[0].job_keys == ["intentsify::backend software engineer"]
    assert get_unmatched_message(conn, "imap:ed-1")["resolved_job_key"] == "intentsify::backend software engineer"
    assert list_unmatched_messages(conn) == []

    parked_at = get_unmatched_message(conn, "imap:ed-1")["detected_at"]
    lead = conn.execute(
        "SELECT first_seen, last_seen, jd_text FROM job_leads WHERE normalized_key = 'intentsify::backend software engineer'"
    ).fetchone()
    assert lead["first_seen"] == lead["last_seen"] == parked_at
    assert "AI-powered" in lead["jd_text"]
    conv = conn.execute("SELECT occurred_at FROM job_conversations WHERE message_id = 'imap:ed-1'").fetchone()
    assert conv["occurred_at"] == parked_at

    # A second run has nothing left to do and never re-bills the LLM.
    assert promote_unmatched_with_llm(conn, client=client) == []
    assert len(client.calls) == 1
    conn.close()


def test_two_roles_become_two_leads(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    _park(conn, "imap:grant-1", subject="Analytics roles", body="Two roles at Remy Corporation", thread="t-grant")
    client = _FakeClient(
        _roles(
            ("Remy Corporation", "Analytics Engineer III", 0.7),
            ("Remy Corporation", "Senior Analytics Engineer", 0.9),
        )
    )

    results = promote_unmatched_with_llm(conn, client=client)
    assert results[0].job_keys == [
        "remy corporation::senior analytics engineer",
        "remy corporation::analytics engineer iii",
    ]
    assert get_unmatched_message(conn, "imap:grant-1")["resolved_job_key"] == "remy corporation::senior analytics engineer"
    convs = conn.execute(
        "SELECT job_key FROM job_conversations WHERE message_id = 'imap:grant-1' ORDER BY job_key"
    ).fetchall()
    assert [r["job_key"] for r in convs] == [
        "remy corporation::analytics engineer iii",
        "remy corporation::senior analytics engineer",
    ]
    conn.close()


def test_no_roles_stays_parked_but_is_not_retried(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    _park(conn, "imap:vague-1", subject="Exploring new opportunities together", body="Open to a chat?", thread="t-v")
    client = _FakeClient("[]")

    results = promote_unmatched_with_llm(conn, client=client)
    assert results[0].job_keys == []
    assert len(list_unmatched_messages(conn)) == 1
    assert promote_unmatched_with_llm(conn, client=client) == []
    assert len(client.calls) == 1
    conn.close()


def test_failed_call_is_retried_next_run(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    _park(conn, "imap:ed-2", subject="Building high-volume data products", body=ED, thread="t-ed2")

    promote_unmatched_with_llm(conn, client=_FakeClient(error=RuntimeError("network")))
    retry = _FakeClient(_roles(("Intentsify", "Backend Software Engineer", 0.8)))
    results = promote_unmatched_with_llm(conn, client=retry)
    assert results[0].job_keys == ["intentsify::backend software engineer"]
    conn.close()


def test_dismissed_and_dry_run_rows_are_untouched(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    _park(conn, "imap:dismissed-1", subject="Sr. Analytics Engineer", body="Remy Corporation", thread="t-d")
    dismiss_unmatched_message(conn, "imap:dismissed-1")
    _park(conn, "imap:ed-3", subject="Building high-volume data products", body=ED, thread="t-ed3")
    client = _FakeClient(_roles(("Intentsify", "Backend Software Engineer", 0.8)))

    results = promote_unmatched_with_llm(conn, client=client, dry_run=True)
    assert [r.message_id for r in results] == ["imap:ed-3"]
    assert client.calls == []
    assert conn.execute("SELECT count(*) FROM job_leads").fetchone()[0] == 0
    conn.close()
