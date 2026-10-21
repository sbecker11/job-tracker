"""record-unmatched-reply — logging a LinkedIn reply LinkedIn never emails back."""

from __future__ import annotations

from pathlib import Path

from job_tracker.cli.record_unmatched_reply import main
from job_tracker.pipeline.store import connect

RELAY = "inmail-hit-reply@linkedin.com"


def _park(conn, mid: str, *, superseded_by: str | None = None) -> None:
    conn.execute(
        "INSERT INTO unmatched_messages (message_id, direction, from_address, subject, body_text, detected_at, "
        "superseded_by) VALUES (?, 'inbound', ?, 'Exploring new opportunities together', ?, "
        "'2026-10-12T22:10:45+00:00', ?)",
        (mid, RELAY, "Hi Shawn,\nOur client is looking...\n\nBest regards,\nNagesh", superseded_by),
    )
    conn.commit()


def test_records_reply_on_stub_lead_and_resolves_both_copies(tmp_path: Path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    _park(conn, "imap:<a@linkedin>")
    _park(conn, "gmail-a", superseded_by="imap:<a@linkedin>")
    conn.close()

    rc = main(
        [
            "--db", str(db),
            "--message-id", "gmail-a",
            "--contact-name", "Nagesh Galam",
            "--text", "Nagesh, yes I am quite interested.\nBest, Shawn",
            "--sent-at", "2026-10-14T18:00:00+00:00",
        ]
    )
    assert rc == 0

    conn = connect(db)
    lead = conn.execute("SELECT * FROM job_leads").fetchone()
    assert lead["company"] == "End-client TBD via Nagesh Galam"
    assert lead["title"] == "Role TBD"
    assert lead["awaiting_response_since"] == "2026-10-14T18:00:00+00:00"
    unresolved = conn.execute("SELECT COUNT(*) FROM unmatched_messages WHERE resolved_at IS NULL").fetchone()[0]
    assert unresolved == 0
    convs = conn.execute(
        "SELECT direction, channel, body_text FROM job_conversations WHERE job_key = ? ORDER BY occurred_at",
        (lead["normalized_key"],),
    ).fetchall()
    assert [c["direction"] for c in convs] == ["inbound", "outbound"]
    assert convs[1]["channel"] == "linkedin"
    assert convs[1]["body_text"].startswith("Nagesh, yes")
    conn.close()


def test_second_reply_reuses_the_resolved_lead(tmp_path: Path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    _park(conn, "imap:<b@linkedin>")
    conn.close()
    for text in ("first reply", "second reply"):
        assert main(["--db", str(db), "--message-id", "imap:<b@linkedin>", "--contact-name", "Ann", "--text", text]) == 0
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM job_leads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM job_conversations WHERE direction = 'outbound'").fetchone()[0] == 2
    conn.close()
