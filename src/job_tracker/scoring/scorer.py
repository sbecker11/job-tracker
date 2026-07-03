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


def _dealbreaker_sweep(text: str, framework: dict[str, Any]) -> list[DealbreakerHit]:
    hits: list[DealbreakerHit] = []
    for entry in framework.get("dealbreakers") or []:
        count = _count_hits(text, entry.get("keywords") or [])
        if count == 0:
            continue
        min_hits = entry.get("min_hits", 1)
        hits.append(
            DealbreakerHit(
                id=entry["id"],
                label=entry["label"],
                verdict=entry.get("verdict", "Dealbreaker"),
                hit_count=count,
                load_bearing=count >= min_hits,
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
