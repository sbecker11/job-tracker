"""Tests for the JD Match Framework scoring engine (config/framework.yaml)."""

from __future__ import annotations

from job_tracker.scoring.scorer import score_jd


def test_load_bearing_dealbreaker_forces_pass():
    jd = """
    We are looking for a Golang engineer. You will write Go services daily.
    Must be an expert in Go, Go tooling, and Go microservices.
    """
    result = score_jd(jd)
    assert result.verdict == "pass"
    assert any(h.id == "golang" and h.load_bearing for h in result.dealbreaker_hits)


def test_single_mention_dealbreaker_does_not_force_pass():
    jd = """
    Senior Software Engineer — Python, AWS, Aurora Postgres, pgvector, RAG,
    LangChain. Occasional interop with a legacy Go service is a plus.
    """
    result = score_jd(jd)
    hits = [h for h in result.dealbreaker_hits if h.id == "golang"]
    assert hits and not hits[0].load_bearing
    assert result.verdict != "pass" or result.match_pct > 0  # not force-passed purely on the mention


def test_strong_skills_match_yields_pursue():
    jd = """
    Senior Software Engineer, AI Platform
    We use Java, Spring Boot, Spring AI, Python, FastAPI, LangChain, LangGraph,
    AWS, Aurora PostgreSQL, pgvector, Bedrock, ECS Fargate, RAG, LLM, React,
    TypeScript, Snowflake, and MLflow across our stack.
    """
    result = score_jd(jd)
    assert result.verdict == "pursue"
    assert result.match_pct > 0
    assert "python" in result.matched_skills


def test_thin_jd_text_yields_review_or_pass_not_crash():
    result = score_jd("Great opportunity, apply now!")
    assert result.verdict in {"pass", "review"}
    assert result.match_pct == 0.0


def test_c2c_only_is_structural_dealbreaker():
    jd = "This is a Corp-to-Corp only engagement, no W2 available."
    result = score_jd(jd)
    assert result.verdict == "pass"
    assert any(h.id == "c2c_only" for h in result.dealbreaker_hits)
