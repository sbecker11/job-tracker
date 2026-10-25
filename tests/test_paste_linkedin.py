"""A pasted LinkedIn thread becomes one inbound lead and a Clarify card."""

from __future__ import annotations

from datetime import datetime

from job_tracker.cli.paste_linkedin_lead import read_paste
from job_tracker.pipeline.paste_linkedin import post_pasted_thread
from job_tracker.pipeline.pending_workflow import build_clarify_queue
from job_tracker.pipeline.store import connect, list_job_conversations

PITCH = """
Exciting opportunity in software and data engineering

      Madhvendra Kumar
        Reply
        https://www.linkedin.com/messaging/thread/2-ZDMxMTM2MDUtOTQ2My00M2Q0LWEzZTUtNDNlZDY1YTUwYjNlXzEwMA==/

Hi,

Position: LLM Data Engineer
Type: Contract (Long term)
Location: Remote

Required Skills
	• Data engineering: Production-grade pipeline code
	• AWS data stack: S3, AWS Glue, Spark on EMR, Athena

Best regards,
Madhvendra Kumar
Sr. Technical Recruiter
KPG99,INC
  madhvendra@kpgtech.com   Direct: 6098300389
"""

THIN = """
      Evan T.
        Reply
        https://www.linkedin.com/messaging/thread/2-MjVhMzhmNDEtMzQ3MS00NzVlLWE5ZDUtYmJjM2I2Y2NlMGU5XzEwMA==/

Hey Shawn, we have an opening with one of our clients for an AI Engineer. Please send your resume.

Evan Turner
Technology Recruiter
"""


def _cards(conn):
    return build_clarify_queue(
        linkedin_reply_queue=[],
        unmatched_communications=[],
        age_days_fn=lambda _ts, _now: 0,
        now=datetime.now().astimezone(),
        conn=conn,
    )


def test_paste_creates_a_linkedin_lead_on_the_clarify_queue(tmp_path):
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, PITCH, use_llm=False)

    assert result["ok"] is True
    assert result["created"] is True
    assert result["changed"] is True
    assert "KPG99" in result["company"].upper()
    assert result["title"] == "LLM Data Engineer"
    assert result["threadUrl"].endswith("XzEwMA==/")
    assert result["recruiterName"].startswith("Madhvendra")

    row = conn.execute(
        "SELECT source_label, jd_text FROM job_leads WHERE normalized_key = ?",
        (result["jobKey"],),
    ).fetchone()
    assert row["source_label"] == "linkedin_message"
    assert "Glue" in row["jd_text"]

    convs = list(list_job_conversations(conn, result["jobKey"]))
    assert len(convs) == 1
    assert convs[0]["channel"] == "linkedin"
    assert convs[0]["direction"] == "inbound"
    assert "messaging/thread/" in convs[0]["body_text"]

    cards = [c for c in _cards(conn) if c.get("normalizedKey") == result["jobKey"]]
    assert len(cards) == 1
    assert cards[0]["channel"] == "linkedin"
    assert "messaging/thread/" in cards[0]["threadUrl"]


def test_same_thread_is_not_inserted_twice(tmp_path):
    conn = connect(tmp_path / "leads.db")
    first = post_pasted_thread(conn, PITCH, use_llm=False)
    second = post_pasted_thread(conn, PITCH + "\nThanks,\nMadhvendra\n", use_llm=False)

    assert second["created"] is False
    assert second["changed"] is False
    assert second["jobKey"] == first["jobKey"]
    assert len(list(list_job_conversations(conn, first["jobKey"]))) == 1


def test_thin_pitch_still_becomes_a_card(tmp_path):
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, THIN, use_llm=False)

    assert result["created"] is True
    assert result["title"] == "Role TBD"
    assert result["company"].startswith("End-client TBD via")
    cards = [c for c in _cards(conn) if c.get("normalizedKey") == result["jobKey"]]
    assert len(cards) == 1
    assert "messaging/thread/" in cards[0]["threadUrl"]


def test_messaging_pane_copy_uses_the_opening_name(tmp_path):
    pane = """
Jane Doe
Hi Shawn,

Position: Senior Software Engineer
Location: Remote

We need production Python and AWS Glue.

Jane Doe
Recruiter at Northwind
"""
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, pane, use_llm=False)
    assert result["created"] is True
    assert result["recruiterName"] == "Jane Doe"
    assert result["title"] == "Senior Software Engineer"
    assert result["company"] == "Northwind"
    cards = [c for c in _cards(conn) if c.get("normalizedKey") == result["jobKey"]]
    assert len(cards) == 1
    assert cards[0]["channel"] == "linkedin"


JAIDEEP = """
Jaideep S
Experienced Sr. US IT Technical Recruiter|| W2 || Contract || Fulltime || C2H || End 2 End Recruitment :- Looking for New Opportunity
Open the options list in your conversation with Shawn Becker and Jaideep S


Jaideep S Profile
Jaideep S
(He/Him)
1st degree connection
· 1st
Experienced Sr. US IT Technical Recruiter|| W2 || Contract || Fulltime || C2H || End 2 End Recruitment :- Looking for New Opportunity
Today
Jaideep S sent the following messages at 6:00 PM
View Jaideep's profileJaideep S, #OPEN_TO_WORK
Jaideep S  (He/Him)  6:00 PM

Hi Shawn,Hope you are doing good. I'm looking Data Engineer (Databricks & Cloud Analytics),SaltLake City,UT-W2 or H1 Transfer- Onsite. Plz let me know if you are available and would like to apply

View Jaideep's profileJaideep S, #OPEN_TO_WORK
Jaideep S  (He/Him)  9:44 PM
Hi shawn
"""


def test_messaging_pane_copy_with_a_single_initial_and_a_looking_sentence(tmp_path):
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, JAIDEEP, use_llm=False)
    assert result["ok"] is True
    assert result["created"] is True
    assert result["recruiterName"] == "Jaideep S"
    assert result["title"] == "Data Engineer (Databricks & Cloud Analytics)"
    assert result["company"].startswith("End-client TBD via Jaideep S")
    cards = [c for c in _cards(conn) if c.get("normalizedKey") == result["jobKey"]]
    assert len(cards) == 1
    assert cards[0]["channel"] == "linkedin"


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Usage:
    input_tokens = 12
    output_tokens = 40


class _Response:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = _Usage()


class _Client:
    def __init__(self, text):
        self.messages = self
        self.text = text
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.text, Exception):
            raise self.text
        return _Response(self.text)


def test_llm_reading_is_the_lead_and_the_templates_only_fill_gaps(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    client = _Client(
        '{"recruiter_name":"Pat Lee","company":"Northwind","title":"Analytics Engineer",'
        '"message":"Hi Shawn, Northwind needs an Analytics Engineer on the warehouse team.",'
        '"apply_url":""}'
    )
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, JAIDEEP, llm_client=client)

    assert "pasted LinkedIn" in client.kwargs["system"]
    assert result["recruiterName"] == "Pat Lee"
    assert result["company"] == "Northwind"
    assert result["title"] == "Analytics Engineer"
    convs = list(list_job_conversations(conn, result["jobKey"]))
    assert "warehouse team" in convs[0]["body_text"]
    assert "View Jaideep" not in convs[0]["body_text"]
    row = conn.execute(
        "SELECT jd_text FROM job_leads WHERE normalized_key = ?", (result["jobKey"],)
    ).fetchone()
    assert "warehouse team" in row["jd_text"]


def test_a_list_of_messages_is_stored_as_the_pitch(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    client = _Client(
        '{"recruiter_name":"Jaideep S","company":"","title":"Data Engineer (Databricks & Cloud Analytics)",'
        '"message":["Hi Shawn, looking Data Engineer (Databricks & Cloud Analytics), onsite in Salt Lake City.",'
        '"Hi shawn"],"apply_url":""}'
    )
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, JAIDEEP, llm_client=client)
    convs = list(list_job_conversations(conn, result["jobKey"]))
    assert convs[0]["body_text"].startswith("Hi Shawn, looking Data Engineer")
    assert "\n\nHi shawn" in convs[0]["body_text"]
    assert not convs[0]["body_text"].startswith("[")


VINAY = """
Hi Shawn,
This is Vinay from Codeforce 360, this is regarding a Job Opportunity as a Snowflake Data Engineer with Cortex AI Role
It's an Complete remote role
Are you interested to apply
"""

NAGENDER = """
Hi,
This is Nagender from Cambay, trying to reach you regarding Data Engineer for Remote, if interested then please share me your updated resume.

Regards,
Nagender Akhil Rawat
Cambay Consulting LLC.
Email – nagender.rawat@cambayllc.com
"""


def test_a_product_in_the_role_is_not_the_hiring_company(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    client = _Client(
        '{"recruiter_name":"Vinay Nithin","company":"Cortex AI","title":"Snowflake Data Engineer",'
        '"message":"Hi Shawn, Snowflake Data Engineer with Cortex AI Role, from Codeforce 360. Remote.",'
        '"apply_url":""}'
    )
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, VINAY, llm_client=client)
    assert result["title"] == "Snowflake Data Engineer"
    assert result["company"].startswith("End-client TBD via Vinay Nithin")


def test_the_recruiters_firm_is_not_the_hiring_company(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    client = _Client(
        '{"recruiter_name":"Nagender Akhil Rawat","company":"Cambay Consulting LLC","title":"Data Engineer",'
        '"message":"This is Nagender from Cambay, regarding a Data Engineer role, remote.",'
        '"apply_url":""}'
    )
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, NAGENDER, llm_client=client)
    assert result["title"] == "Data Engineer"
    assert result["company"].startswith("End-client TBD via Nagender Akhil Rawat")


def test_a_failed_read_falls_back_to_the_templates(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, JAIDEEP, llm_client=_Client(RuntimeError("down")))
    assert result["ok"] is True
    assert result["recruiterName"] == "Jaideep S"
    assert result["title"] == "Data Engineer (Databricks & Cloud Analytics)"
    assert result["company"].startswith("End-client TBD via Jaideep S")


def test_cleaned_message_keeps_the_thread_url(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    client = _Client(
        '{"recruiter_name":"Madhvendra Kumar","company":"KPG99","title":"LLM Data Engineer",'
        '"message":"Position: LLM Data Engineer. Remote. AWS Glue and Spark on EMR.",'
        '"apply_url":""}'
    )
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, PITCH, llm_client=client)
    convs = list(list_job_conversations(conn, result["jobKey"]))
    assert "AWS Glue" in convs[0]["body_text"]
    assert result["threadUrl"] in convs[0]["body_text"]
    cards = [c for c in _cards(conn) if c.get("normalizedKey") == result["jobKey"]]
    assert "messaging/thread/" in cards[0]["threadUrl"]


def test_paste_without_a_role_or_recruiter_is_refused(tmp_path):
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, "hello there, nothing to extract from this note at all", use_llm=False)
    assert result["ok"] is False
    assert conn.execute("SELECT COUNT(*) FROM job_leads").fetchone()[0] == 0


def test_read_paste_uses_the_clipboard_at_a_terminal_and_stdin_when_piped():
    assert read_paste(stdin_text="piped", clipboard_text="copied", stdin_is_tty=True) == "copied"
    assert read_paste(stdin_text="piped", clipboard_text="copied", stdin_is_tty=False) == "piped"
