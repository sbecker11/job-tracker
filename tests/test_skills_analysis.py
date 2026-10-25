import json

from job_tracker.pipeline import skills_analysis as sa
from job_tracker.pipeline.required_skills import LeadSkill, SkillExtraction, save_extraction
from job_tracker.pipeline import store


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Client:
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = self

    def create(self, **_):
        usage = type("U", (), {"input_tokens": 100, "output_tokens": 50})()
        return type("R", (), {"content": [_Block(json.dumps(self.replies.pop(0)))], "usage": usage})()


def _lead(conn, key, family, skills):
    conn.execute("INSERT INTO job_leads (normalized_key, company, title, status) VALUES (?, ?, 'SWE', 'new')", (key, key.title()))
    save_extraction(
        conn,
        SkillExtraction(
            job_key=key,
            role_family=family,
            seniority="senior",
            years_required=None,
            engagement={},
            skills=[LeadSkill(n, n, c, imp, None, "jd", "") for n, c, imp in skills],
        ),
    )


def test_merge_match_aggregate_render(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "MAP_CACHE", tmp_path / "map.json")
    monkeypatch.setattr(sa, "MATCH_CACHE", tmp_path / "match.json")
    monkeypatch.setattr("job_tracker.pipeline.llm_apply.log_call", lambda **_: None)
    conn = store.connect(tmp_path / "leads.db")
    _lead(conn, "a", "data", [("Python", "language", "required"), ("API Development", "architecture", "required"), ("dbt", "data", "required")])
    _lead(conn, "b", "ml_ai", [("Python", "language", "required"), ("API Design", "architecture", "preferred"), ("Kubernetes", "devops", "required")])
    _lead(conn, "c", "data", [("Python", "language", "required")])  # too few skills: not a real JD

    rows = sa.load_rows(conn)
    assert {r.job_key for r in rows} == {"a", "b"}
    names = {r.canonical for r in rows}
    merge = {n: {"standard": "API Design" if n.startswith("API") else n, "category": ""} for n in names}
    sa.run_merge(names, model="claude-sonnet-5", client=_Client([merge]))
    assert sa.pending_merge(names) == []

    standards = {sa.standard_of(n, sa._read(sa.MAP_CACHE)) for n in names}
    assert standards == {"Python", "API Design", "dbt", "Kubernetes"}
    match = {
        "Python": {"status": "met_client", "evidence": "SeniorLink PySpark"},
        "API Design": {"status": "met_client", "evidence": "Adobe REST APIs"},
        "dbt": {"status": "met_client", "evidence": "Fannie Mae"},
        "Kubernetes": {"status": "partial", "evidence": "Certification only", "close": "Deploy linkage-engine on EKS"},
    }
    sa.run_match(standards, profile="(profile)", model="claude-sonnet-5", client=_Client([match]))

    agg = sa.aggregate(rows)
    by = {s["skill"]: s for s in agg["skills"]}
    assert agg["leads"] == 2 and agg["families"] == {"data": 1, "ml_ai": 1}
    assert by["Python"]["by"] == {"data": [1, 0], "ml_ai": [1, 0]}
    assert by["API Design"]["by"] == {"data": [1, 0], "ml_ai": [0, 1]}
    assert by["Kubernetes"]["status"] == "partial"

    page = sa.render_html(agg, generated="now")
    assert "Most requested skills you lack" in page and "#fff" not in page.lower()
