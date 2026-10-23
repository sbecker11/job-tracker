"""TURN-6: a LinkedIn "Message replied" notice proves Shawn wrote first."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.pipeline.models import JobConversation, JobLead, normalize_key
from job_tracker.pipeline.store import INFERRED_REPLY_PREFIX, add_job_conversation, connect, upsert_lead

BODIES = {
    "m2": "Thanks Shawn, the client is Acme and it is fully remote on W2.",
    "m3": "Also, could you send your latest resume and a few times for a call?",
}
KEY = normalize_key("Acme", "Data Engineer")


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "leads.db")
    upsert_lead(c, JobLead(company="Acme", title="Data Engineer", source_message_id="s", source_label="linkedin_message"))
    yield c
    c.close()


def _rows(conn):
    return [
        (r["direction"], r["occurred_at"], r["message_id"])
        for r in conn.execute("SELECT * FROM job_conversations WHERE job_key = ? ORDER BY occurred_at", (KEY,))
    ]


def _inbound(mid, summary, at):
    return JobConversation(job_key=KEY, message_id=mid, direction="inbound", channel="linkedin", summary=summary, body_text=BODIES.get(mid, summary), occurred_at=at)


def test_message_replied_logs_shawns_reply_first(conn):
    add_job_conversation(conn, _inbound("m1", "Data Engineer role", "2026-10-16T19:41:00+00:00"))
    add_job_conversation(conn, _inbound("m2", "Message replied: Data Engineer role", "2026-10-22T16:10:00+00:00"))
    rows = _rows(conn)
    assert [d for d, _, _ in rows] == ["inbound", "outbound", "inbound"]
    assert rows[1][1] == "2026-10-22T16:09:00+00:00"
    assert rows[1][2] == f"{INFERRED_REPLY_PREFIX}m2"
    lead = conn.execute("SELECT awaiting_response_since FROM job_leads WHERE normalized_key = ?", (KEY,)).fetchone()
    assert lead["awaiting_response_since"] is None


def test_no_inference_when_reply_already_logged(conn):
    add_job_conversation(conn, _inbound("m1", "Data Engineer role", "2026-10-16T19:41:00+00:00"))
    add_job_conversation(
        conn,
        JobConversation(job_key=KEY, message_id="s1", direction="outbound", channel="linkedin",
                        summary="clarifiers", occurred_at="2026-10-22T16:06:00+00:00"),
    )
    add_job_conversation(conn, _inbound("m2", "Message replied: Data Engineer role", "2026-10-22T16:10:00+00:00"))
    assert [d for d, _, _ in _rows(conn)] == ["inbound", "outbound", "inbound"]


def test_duplicate_copy_does_not_infer_twice(conn):
    add_job_conversation(conn, _inbound("m2", "Message replied: Data Engineer role", "2026-10-22T16:10:00+00:00"))
    add_job_conversation(conn, _inbound("m2", "Message replied: Data Engineer role", "2026-10-22T16:10:00+00:00"))
    assert [d for d, _, _ in _rows(conn)] == ["outbound", "inbound"]


def test_back_to_back_notices_infer_one_reply(conn):
    add_job_conversation(conn, _inbound("m2", "Message replied: Data Engineer role", "2026-10-22T16:10:00+00:00"))
    add_job_conversation(conn, _inbound("m3", "Message replied: Data Engineer role", "2026-10-22T16:20:00+00:00"))
    assert [d for d, _, _ in _rows(conn)] == ["outbound", "inbound", "inbound"]


def test_ordinary_inbound_infers_nothing(conn):
    add_job_conversation(conn, _inbound("m1", "Re: Data Engineer role", "2026-10-22T16:10:00+00:00"))
    assert [d for d, _, _ in _rows(conn)] == ["inbound"]
