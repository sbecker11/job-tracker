"""CARD-14: Manage lead's sender chooser — contacts, newest writer first."""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline.models import JobContact, JobConversation, JobLead, normalize_key
from job_tracker.pipeline.pending_workflow import _attach_senders
from job_tracker.pipeline.store import add_job_contact, add_job_conversation, connect, upsert_lead

KEY = normalize_key("Acme", "Data Engineer")


def _plan(item: dict) -> dict:
    return {"recipient": {"name": item.get("recruiterName", "")}, "routes": []}


def test_senders_newest_first_and_relay_contacts_fold_into_recruiter(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(conn, JobLead(company="Acme", title="Data Engineer", source_message_id="s", source_label="linkedin_message"))
    pat = add_job_contact(conn, JobContact(job_key=KEY, name="Pat Lee", role="recruiter"))
    relay = add_job_contact(conn, JobContact(job_key=KEY, name="", role="recruiter", linkedin_reply_to="x@reply.linkedin.com"))
    sam = add_job_contact(conn, JobContact(job_key=KEY, name="Sam Ortiz", role="hiring_manager", email="sam@acme.com"))
    for cid, mid, at in ((pat, "m1", "2026-10-01T10:00:00+00:00"), (relay, "m2", "2026-10-03T10:00:00+00:00"), (sam, "m3", "2026-10-05T10:00:00+00:00")):
        add_job_conversation(
            conn,
            JobConversation(job_key=KEY, contact_id=cid, message_id=mid, direction="inbound",
                            summary=f"note {mid}", body_text=f"distinct body for {mid}", occurred_at=at),
        )
    item = {"normalizedKey": KEY, "recruiterName": "Pat Lee", "replyPlan": {"recipient": {"name": "Pat Lee"}, "routes": []}}
    _attach_senders(item, conn, _plan)
    names = [s["name"] for s in item["senders"]]
    assert names == ["Sam Ortiz", "Pat Lee"]
    pat_row = item["senders"][1]
    assert pat_row["primary"] and [m["summary"] for m in pat_row["messages"]] == ["note m2", "note m1"]
    assert item["senders"][0]["replyPlan"]["recipient"]["name"] == "Sam Ortiz"
