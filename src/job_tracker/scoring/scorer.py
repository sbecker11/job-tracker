"""JD Match Framework: dealbreaker sweep + skills alignment -> match % -> verdict.

Implements CLAUDE.md §10 ("JD Match Framework") as a deterministic, keyword
heuristic engine (v1; no LLM). Config lives in config/framework.yaml so the
dealbreaker list and skills vocabulary can be updated without touching code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FRAMEWORK_PATH = _REPO_ROOT / "config" / "framework.yaml"


@dataclass
class DealbreakerHit:
    id: str
    label: str
    verdict: str
    hit_count: int
    load_bearing: bool


@dataclass
class ScoreResult:
    match_pct: float
    matched_skills: list[str] = field(default_factory=list)
    dealbreaker_hits: list[DealbreakerHit] = field(default_factory=list)
    verdict: str = "review"  # "pursue" | "review" | "pass"
    rationale: list[str] = field(default_factory=list)
    # Combined weight of vocabulary terms (candidate + generic) actually
    # found in the JD text — the match_pct denominator. Low values mean
    # match_pct is low-confidence/noisy (see MIN_RELEVANT_WEIGHT) rather
    # than a genuine signal of poor fit.
    relevant_weight: float = 0.0


@lru_cache(maxsize=4)
def load_framework(path: Path = DEFAULT_FRAMEWORK_PATH) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _count_hits(text: str, patterns: list[str]) -> int:
    total = 0
    for pattern in patterns:
        total += len(re.findall(pattern, text, re.I))
    return total


# A raw keyword-hit count can't tell "Required: Go" from "Nice to have:
# experience with Golang (Go)" — both count the same, but only the first is
# actually load-bearing. Backtested against 61 stored LLM evaluations
# (2026-07-07): this exact miss (an optional-section mention of a §3
# stack dealbreaker keyword) turned a genuinely strong match into a
# false "pass" for real cases (e.g. a JD listing Go only under "Nice To
# Have" tripped `min_hits` anyway). `_split_hard_soft` buckets each line
# by the most recent heading it saw so hits under a soft heading can be
# excluded from the load-bearing count entirely, without needing a full
# JD parser — worst case (an unrecognized heading), text defaults to
# "hard", so this can only make the sweep MORE lenient, never silently
# drop a genuine hard requirement into the soft bucket.
_SOFT_HEADING_RE = re.compile(
    r"(nice[- ]to[- ]have|preferred qualifications?|preferred skills?|preferred:|"
    r"bonus points?|bonus:|is a plus\b|would be a plus\b|a plus:|pluses?:|optional:|"
    r"not required|good to have|desired qualifications?|desired skills?)",
    re.I,
)
# A very short line with no sentence punctuation is heuristically "heading
# shaped" (JDs reliably format section titles this way: "Requirements",
# "Nice to Have", "Work Environment", ...) — real requirement bullets, even
# short ones, are near-always full phrases/sentences (5+ words) rather than
# 1-4 word fragments. Any heading-shaped line resets the mode — defaulting
# to "hard" unless it specifically matches _SOFT_HEADING_RE — rather than
# only a recognized _HARD_HEADING_RE resetting it. Without this, a soft
# section with no later *recognized* hard heading (e.g. "Work Environment"
# or "Compensation" following a "Nice to Have" block) stayed stuck in "soft"
# mode for the rest of the document, silently swallowing later hard
# requirements (e.g. an onsite mandate under "Work Environment").
def _is_heading_shaped(line: str) -> bool:
    if not line or len(line) > 40 or re.search(r"[.!?]", line):
        return False
    words = line.lstrip("-*•").split()
    return 0 < len(words) <= 4


def _split_hard_soft(text: str) -> tuple[str, str]:
    """Split JD text into "hard requirement" text and "nice to have /
    preferred / bonus" text by tracking the most recent heading-shaped line
    seen. Everything before the first heading (and under any heading that
    doesn't specifically read as "nice to have/preferred/bonus") stays in
    the "hard" bucket — this can only make the sweep MORE lenient, never
    silently drop a genuine hard requirement into the soft bucket."""
    hard_lines: list[str] = []
    soft_lines: list[str] = []
    mode = "hard"
    for line in text.splitlines():
        stripped = line.strip()
        if _is_heading_shaped(stripped):
            mode = "soft" if _SOFT_HEADING_RE.search(stripped) else "hard"
            continue
        (soft_lines if mode == "soft" else hard_lines).append(line)
    return "\n".join(hard_lines), "\n".join(soft_lines)


def _dealbreaker_sweep(text: str, framework: dict[str, Any]) -> list[DealbreakerHit]:
    hard_text, soft_text = _split_hard_soft(text)
    hits: list[DealbreakerHit] = []
    for entry in framework.get("dealbreakers") or []:
        keywords = entry.get("keywords") or []
        hard_count = _count_hits(hard_text, keywords)
        soft_count = _count_hits(soft_text, keywords)
        if hard_count == 0 and soft_count == 0:
            continue
        min_hits = entry.get("min_hits", 1)
        hits.append(
            DealbreakerHit(
                id=entry["id"],
                label=entry["label"],
                verdict=entry.get("verdict", "Dealbreaker"),
                hit_count=hard_count + soft_count,
                # Soft-section mentions never count toward load-bearing,
                # regardless of min_hits — see module comment above.
                load_bearing=hard_count >= min_hits,
            )
        )
    return hits


def _clean_keyword_label(pattern: str) -> str:
    return re.sub(r"\\b|\\\.", "", pattern).replace("\\", "")


def _weighted_hits(text: str, terms: list[dict[str, Any]]) -> tuple[float, list[str]]:
    """Sum of weights for vocabulary terms that actually appear in `text`,
    plus their cleaned labels — a term only counts if it's *present*, unlike
    the old denominator which summed every vocabulary weight unconditionally."""
    weight = 0.0
    matched: list[str] = []
    for term in terms:
        pattern = term["keyword"]
        if re.search(pattern, text, re.I):
            weight += term.get("weight", 1)
            matched.append(_clean_keyword_label(pattern))
    return weight, matched


# Below this much combined weight (candidate skills + generic vocabulary
# terms actually present in the JD), match_pct is too noisy to trust as a
# gate signal. Backtested against the historical corpus (2026-07-11): thin
# JD text (e.g. a mangled digest-email snippet carrying almost no
# recognizable tech terms) could hit a spurious 100% purely because the one
# term present happened to also be a candidate skill — e.g. relevant_weight
# 1.0 (a single "full-stack" mention, nothing else recognized) scoring
# 100% regardless of whether the underlying JD was ever seen at all. Below
# the floor, real signal (rel_weight 10-25 in the same corpus) started
# tracking the LLM's own match_pct reasonably well; above it, a handful of
# JDs each hit hundreds of words summarizing multi-decade experience
# unrelated to fit (rel_weight fell off again toward the very high end of
# real JDs), so this is a floor, not a full noise model — see
# scripts/backfill_jd_review_docs.py-adjacent calibration notes in chat
# history for the raw sweep this was picked from.
MIN_RELEVANT_WEIGHT = 5.0


def _skills_alignment(text: str, framework: dict[str, Any]) -> tuple[float, list[str], float]:
    """JD-relative skills match %: of the technical vocabulary this JD
    actually mentions — both required *and* nice-to-have, since a JD's own
    "nice to have" skills are still real signal about its stack, unlike the
    dealbreaker sweep's hard/soft split which specifically cares whether a
    requirement is *mandatory* — (candidate `skills:` + the broader,
    generic `generic_tech_vocabulary:` reference list), what fraction does
    the candidate's own skill list cover?

    2026-07-11 rescale: the old formula (matched weight / the *entire*
    ~40-term skills vocabulary, matched or not) could never clear ~25% for
    any real JD — no single posting mentions most of a candidate's whole
    career stack — which made a literal "run the LLM review at >=70%
    match" gate meaningless. This version's denominator is only the
    vocabulary terms actually present in *this* JD (candidate skills +
    generic terms both count toward "present"), so a JD whose entire
    stated stack happens to be things the candidate knows can score near
    100%, and 70% is a real, discriminating bar — guarded by
    MIN_RELEVANT_WEIGHT so a near-empty JD can't fake a high score.
    """
    skills = framework.get("skills") or []
    generic_terms = framework.get("generic_tech_vocabulary") or []

    matched_weight, matched = _weighted_hits(text, skills)
    generic_weight, _ = _weighted_hits(text, generic_terms)

    relevant_weight = matched_weight + generic_weight
    if relevant_weight < MIN_RELEVANT_WEIGHT:
        return 0.0, matched, relevant_weight
    match_pct = round(matched_weight / relevant_weight * 100, 1)
    return match_pct, matched, relevant_weight


def score_jd(
    jd_text: str,
    *,
    framework_path: Path = DEFAULT_FRAMEWORK_PATH,
) -> ScoreResult:
    """Run the dealbreaker sweep + skills alignment against JD text."""
    framework = load_framework(framework_path)
    text = jd_text or ""

    dealbreaker_hits = _dealbreaker_sweep(text, framework)
    match_pct, matched_skills, relevant_weight = _skills_alignment(text, framework)

    load_bearing_hits = [h for h in dealbreaker_hits if h.load_bearing]
    mention_only_hits = [h for h in dealbreaker_hits if not h.load_bearing]

    thresholds = framework.get("thresholds") or {}
    pursue_min = thresholds.get("pursue_min_pct", 15)
    review_min = thresholds.get("review_min_pct", 5)

    rationale: list[str] = []

    if load_bearing_hits:
        verdict = "pass"
        for h in load_bearing_hits:
            rationale.append(f"Dealbreaker — {h.label} ({h.hit_count} mentions): {h.verdict}")
    elif match_pct >= pursue_min:
        verdict = "pursue"
        rationale.append(f"Match {match_pct}% >= pursue threshold {pursue_min}%")
    elif match_pct >= review_min:
        verdict = "review"
        rationale.append(f"Match {match_pct}% is between {review_min}% and {pursue_min}% — needs a manual look")
    else:
        verdict = "pass"
        rationale.append(f"Match {match_pct}% below review threshold {review_min}%")

    for h in mention_only_hits:
        rationale.append(
            f"Note: '{h.label}' mentioned {h.hit_count}x but below load-bearing threshold — verify manually"
        )

    if relevant_weight < MIN_RELEVANT_WEIGHT:
        rationale.append(
            f"JD too thin to score reliably (only {relevant_weight:.0f} pts of recognizable tech vocabulary "
            f"found, need {MIN_RELEVANT_WEIGHT:.0f}+) — match_pct forced to 0, needs a manual look"
        )
    elif matched_skills:
        rationale.append("Matched skills: " + ", ".join(sorted(matched_skills)))
    else:
        rationale.append("No known skills matched — JD text may be thin (snippet only) or vocabulary gap")

    return ScoreResult(
        match_pct=match_pct,
        matched_skills=matched_skills,
        dealbreaker_hits=dealbreaker_hits,
        verdict=verdict,
        rationale=rationale,
        relevant_weight=relevant_weight,
    )


def should_run_llm_review(score: ScoreResult, *, framework_path: Path = DEFAULT_FRAMEWORK_PATH) -> bool:
    """Gate for the two-tier review pipeline (no-LLM-review.docx ->
    full-LLM-review.docx): purely `match_pct >= thresholds.llm_review_min_pct`
    (config/framework.yaml), as specified. Deliberately does NOT also
    require a clean rule-based dealbreaker sweep — backtesting against the
    historical corpus (2026-07-11) found a real "pursue" lead (Waystar)
    whose rule-based sweep threw a false-positive load-bearing "Angular"
    hit (JD mentioned it in a legacy-migration context the keyword sweep
    can't distinguish from a real requirement) that the full LLM review
    correctly read as non-load-bearing. Gating on the rule-based dealbreaker
    sweep in addition to score would have silently dropped that lead before
    the smarter LLM pass ever got a chance to catch the nuance — exactly
    the failure mode the second tier exists to avoid."""
    framework = load_framework(framework_path)
    gate_pct = (framework.get("thresholds") or {}).get("llm_review_min_pct", 70)
    return score.match_pct >= gate_pct
