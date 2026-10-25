import json

from docx import Document

from job_tracker.pipeline import store
from job_tracker.pipeline.required_skills import (
    canonical_skill,
    extract_required_skills,
    is_wheelhouse,
    parse_extraction,
    render_required_skills_docx,
    save_extraction,
)

FW = {"dealbreakers": [{"id": "golang", "label": "Go", "keywords": ["golang"]}]}


def test_wheelhouse_titles():
    assert is_wheelhouse("Senior Software Engineer", "Python and AWS.", FW)
    assert is_wheelhouse("Sr. Data Engineer", "Spark.", FW)
    assert is_wheelhouse("Engineer Software III", "Java.", FW)
    assert not is_wheelhouse("Software Engineering Manager", "Python.", FW)
    assert not is_wheelhouse("Senior Communications Manager", "", FW)
    assert not is_wheelhouse("Data Scientist 2", "", FW)


def test_wheelhouse_drops_load_bearing_dealbreaker():
    assert not is_wheelhouse("Senior Backend Engineer", "Requirements\nStrong Golang experience.", FW)


def test_canonical_aliases():
    assert canonical_skill("postgres") == "PostgreSQL"
    assert canonical_skill(" Large Language Models ") == "LLMs"
    assert canonical_skill("Snowflake") == "Snowflake"


def test_parse_dedupes_and_prefers_required():
    data = {
        "role_family": "data",
        "seniority": "senior",
        "years_required": "5",
        "skills": [
            {"skill": "Postgres", "canonical": "postgres", "category": "database", "importance": "required"},
            {"skill": "PostgreSQL", "canonical": "PostgreSQL", "category": "database", "importance": "preferred"},
            {"skill": "dbt", "category": "data", "importance": "preferred", "source": "message", "years": "2"},
            {"skill": "", "canonical": ""},
            {"skill": "Weird", "category": "nonsense"},
        ],
    }
    r = parse_extraction(data, job_key="k")
    names = [(s.canonical, s.importance) for s in r.skills]
    assert names == [("PostgreSQL", "required"), ("dbt", "preferred"), ("Weird", "required")]
    assert r.skills[1].source == "message" and r.skills[1].years == 2.0
    assert r.skills[2].category == "practice"
    assert r.years_required == 5.0


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = type("U", (), {"input_tokens": 1000, "output_tokens": 500})()


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.messages = self
        self.prompts = []

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        return _Resp(json.dumps(self.payload))


def test_extract_save_and_render(tmp_path, monkeypatch):
    monkeypatch.setattr("job_tracker.pipeline.llm_apply.log_call", lambda **_: None)
    client = _Client(
        {
            "role_family": "software",
            "seniority": "senior",
            "engagement": {"employment": "full_time", "remote": "remote", "location": ""},
            "skills": [{"skill": "Java", "category": "language", "importance": "required", "quote": "Java 17"}],
        }
    )
    r = extract_required_skills(
        job_key="acme::sse",
        company="Acme",
        title="Senior Software Engineer",
        jd_text="Java 17 required.",
        messages=["Client wants Kafka too."],
        model="claude-sonnet-5",
        client=client,
    )
    assert "RECRUITER MESSAGE 1" in client.prompts[0]
    assert r.cost_usd and r.input_hash

    conn = store.connect(tmp_path / "leads.db")
    save_extraction(conn, r)
    save_extraction(conn, r)
    assert conn.execute("SELECT count(*) FROM lead_skills WHERE job_key='acme::sse'").fetchone()[0] == 1

    out = render_required_skills_docx(r, company="Acme", title="Senior Software Engineer", out_path=tmp_path / "R.docx")
    text = "\n".join(p.text for p in Document(out).paragraphs)
    assert "Required (1)" in text and "Preferred / nice to have (0)" in text
