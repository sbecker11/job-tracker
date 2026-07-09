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


def _skills_alignment(text: str, framework: dict[str, Any]) -> tuple[float, list[str]]:
    skills = framework.get("skills") or []
    total_weight = sum(s.get("weight", 1) for s in skills) or 1
    matched_weight = 0
    matched: list[str] = []
    for skill in skills:
        pattern = skill["keyword"]
        if re.search(pattern, text, re.I):
            matched_weight += skill.get("weight", 1)
            matched.append(re.sub(r"\\b|\\\.", "", pattern).replace("\\", ""))
    match_pct = round(matched_weight / total_weight * 100, 1)
    return match_pct, matched


def score_jd(
    jd_text: str,
    *,
    framework_path: Path = DEFAULT_FRAMEWORK_PATH,
) -> ScoreResult:
    """Run the dealbreaker sweep + skills alignment against JD text."""
    framework = load_framework(framework_path)
    text = jd_text or ""

    dealbreaker_hits = _dealbreaker_sweep(text, framework)
    match_pct, matched_skills = _skills_alignment(text, framework)

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

    if matched_skills:
        rationale.append("Matched skills: " + ", ".join(sorted(matched_skills)))
    else:
        rationale.append("No known skills matched — JD text may be thin (snippet only) or vocabulary gap")

    return ScoreResult(
        match_pct=match_pct,
        matched_skills=matched_skills,
        dealbreaker_hits=dealbreaker_hits,
        verdict=verdict,
        rationale=rationale,
    )
