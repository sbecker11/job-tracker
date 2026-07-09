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


def test_onsite_tag_in_title_is_dealbreaker():
    # Regression test (2026-07-06): a Des Moines, IA "(Onsite)" role slipped
    # through as a pursue before this dealbreaker existed.
    jd = "Sr. AI Full Stack Engineer in Des Moines, IA (Onsite). Java, Spring Boot, AWS, React."
    result = score_jd(jd)
    assert result.verdict == "pass"
    assert any(h.id == "onsite_only" and h.load_bearing for h in result.dealbreaker_hits)


def test_flexible_hybrid_mention_of_onsite_is_not_a_dealbreaker():
    jd = """
    Senior Software Engineer — mostly remote, with occasional onsite team
    days a few times a year. Python, AWS, React.
    """
    result = score_jd(jd)
    hits = [h for h in result.dealbreaker_hits if h.id == "onsite_only"]
    assert not hits or not hits[0].load_bearing


def test_spelled_out_days_per_week_onsite_is_a_dealbreaker():
    # Regression (2026-07-07): a Torrance, CA role slipped through because
    # "Expected onsite schedule is four days per week." used a spelled-out
    # number, which the old digit-only "5 days? (a week)? onsite" pattern
    # never caught.
    jd = """
    Senior Software Engineer

    Work Environment

    This position is based onsite at Acme's headquarters in Torrance, California.

    Expected onsite schedule is four days per week.

    Python, AWS, React.
    """
    result = score_jd(jd)
    assert result.verdict == "pass"
    assert any(h.id == "onsite_only" and h.load_bearing for h in result.dealbreaker_hits)


def test_dealbreaker_keyword_under_nice_to_have_heading_is_not_load_bearing():
    # Regression (2026-07-07): "Nice To Have\n\nExperience with Golang (Go)."
    # tripped golang's min_hits=2 (one hit from "golang", one from "(Go)")
    # even though the JD explicitly frames it as optional — a raw hit count
    # can't distinguish "required" from "nice to have" without knowing which
    # section a mention falls under.
    jd = """
    Senior Software Engineer — Python, AWS, Aurora Postgres, React, TypeScript.

    Requirements

    5+ years of professional software engineering experience.
    Strong experience with cloud-native architectures.

    Nice To Have

    Experience with Golang (Go).
    Experience with containerization platforms like Docker.
    """
    result = score_jd(jd)
    hits = [h for h in result.dealbreaker_hits if h.id == "golang"]
    assert hits and not hits[0].load_bearing
    assert result.verdict != "pass"


def test_soft_section_ends_at_next_heading_even_if_unrecognized():
    # Regression (2026-07-07): once a "Nice to Have" section was entered,
    # the sweep previously stayed in "soft" mode for the rest of the
    # document unless it hit one of a fixed list of recognized hard
    # headings — so a genuine hard requirement (an onsite mandate) sitting
    # under an unrecognized heading like "Work Environment" was silently
    # exempted from the dealbreaker sweep. Any heading-shaped line must
    # reset back to "hard" by default.
    jd = """
    Senior Software Engineer — Python, AWS, React.

    Nice To Have

    Experience with Kubernetes and Docker.

    Work Environment

    This role is onsite only, no remote option.
    """
    result = score_jd(jd)
    hits = [h for h in result.dealbreaker_hits if h.id == "onsite_only"]
    assert hits and hits[0].load_bearing
    assert result.verdict == "pass"
