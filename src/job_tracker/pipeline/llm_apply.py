"""LLM-driven JD evaluation + résumé/cover-letter generation.

Runs the same "JD Match Framework" (dealbreaker sweep -> skills alignment ->
match % -> verdict) documented in ~/CLAUDE.md §10, and — only on a
"pursue" verdict — generates tailored résumé + cover letter content per
§5-§9 and §11, then renders it to real .docx files.

This is a plain two-stage function pipeline (evaluate, then generate), not a
service architecture: each stage is a single structured LLM call with a
fixed input/output shape, not an autonomous multi-step agent, so a
synchronous in-process call is the right level of complexity — see the
chat history for why this was deliberately NOT split into MCP/FastAPI
services.

Contact-info constants below are hardcoded from CLAUDE.md §1 rather than
left to the model to reproduce verbatim, since a hallucinated typo in an
email/phone number is a much worse failure mode than losing a little
creative freedom the model doesn't need for that text anyway.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("JOB_TRACKER_APPLY_MODEL", "claude-sonnet-5")

# USD per million tokens: (input, output). Source: platform.claude.com/docs/en/about-claude/pricing
# (checked 2026-07-03). Sonnet 5 is on introductory pricing through 2026-08-31, then $3/$15.
_MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4-8": (5.00, 25.00),
}

_CANDIDATE_PROFILE_PATH = Path(
    os.environ.get("JOB_TRACKER_CANDIDATE_PROFILE_PATH", str(Path.home() / "CLAUDE.md"))
)

DEFAULT_OUTPUT_ROOT = Path.home() / "Desktop" / "Resumes" / "2026"

CANDIDATE_NAME = "Shawn Becker"
CANDIDATE_EMAIL = "shawn.becker@spexture.com"
CANDIDATE_PHONE = "+1 857-891-0896"
CANDIDATE_LINKEDIN = "linkedin.com/in/shawnbecker"
CANDIDATE_GITHUB = "github.com/sbecker11"
GITHUB_COLOR_HEX = "555555"

# CLAUDE.md §4 "Banned terms" — checked mechanically post-generation as a
# safety net; the prompt already instructs the model to avoid these, but a
# hardcoded name-collision (e.g. "Cambria" vs "Cambia Health Solutions") is
# exactly the kind of thing worth a belt-and-suspenders check.
_BANNED_TERMS = ["Spexture LLC", "sub-100ms response", "Cline", "Member Nav", "Cambria"]

# CLAUDE.md §4 house rule #11 bans any work-authorization statement outright.
# This is a natural-language rule an LLM can quietly overstep (observed in
# testing: it added a citizenship/clearance statement to a cover letter for
# a role that didn't ask for one), so it gets a regex safety net on top of
# the prompt.
_WORK_AUTH_RE = re.compile(
    r"\bUS citizen(ship)?\b|\bgreen card\b|authorized to work|work authorization|"
    r"eligible for (a )?(public trust|security clearance|clearance)|"
    r"sponsorship (is )?(not )?(required|needed|available)",
    re.IGNORECASE,
)
# CLAUDE.md §4 house rule #12 (added 2026-07-05 after a real leak was found in
# the corpus review: a cover letter compared a W2 rate against an equivalent
# C2C rate). No dollar figure, hourly rate, or salary range belongs in either
# deliverable — compensation is a live conversation, never written down here.
_COMP_FIGURE_RE = re.compile(
    r"\$\s?\d[\d,.]*\s*(?:/\s*hr|per\s*hour|/\s*hour|k\b)|"
    r"\b\d[\d,.]*\s*(?:k|K)\s*(?:/\s*yr|per\s*year|base|salary)|"
    r"\bhourly rate\b|\bsalary range\b|\bcompensation range\b",
    re.IGNORECASE,
)

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


class LLMApplyError(RuntimeError):
    """Raised when the candidate profile is missing or the LLM call fails unrecoverably."""


@dataclass
class CallMetrics:
    """Usage/cost accounting for one Anthropic API call."""

    step: str  # "evaluate" | "generate" | "house_rule_repair"
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_s: float = 0.0
    cost_usd: float | None = None


def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    pricing = _MODEL_PRICING_USD_PER_MTOK.get(model)
    if pricing is None:
        return None
    in_rate, out_rate = pricing
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def _sum_metrics(step: str, model: str, calls: list[CallMetrics]) -> CallMetrics:
    """Roll up every call made in service of one logical step (including any
    JSON-repair or house-rule-repair retries) into a single step-level total,
    so "cost of the generate step" means everything it took to get a usable
    result, not just the first (possibly-failed) call."""
    input_tokens = sum(c.input_tokens for c in calls)
    output_tokens = sum(c.output_tokens for c in calls)
    elapsed_s = sum(c.elapsed_s for c in calls)
    costs = [c.cost_usd for c in calls if c.cost_usd is not None]
    cost_usd = sum(costs) if costs else None
    return CallMetrics(
        step=step, model=model, input_tokens=input_tokens, output_tokens=output_tokens,
        elapsed_s=elapsed_s, cost_usd=cost_usd,
    )


@dataclass
class EvaluationResult:
    verdict: str  # "pursue" | "review" | "pass"
    match_pct: float
    job_summary: str = ""
    # Each item: {"check": str, "status": "clean" | "warning" | "fail", "notes": str}.
    # One row per CLAUDE.md §3 dealbreaker plus comp floor / remote-location fit /
    # durability lens (§2) — the model derives the exact set from whatever the
    # loaded candidate profile's dealbreaker table actually contains, so this
    # isn't hardcoded to today's five checks.
    dealbreaker_checks: list[dict] = field(default_factory=list)
    # Each item: {"requirement": str, "evidence": str, "strength": "very_strong" |
    # "strong" | "moderate" | "minor_gap" | "gap"}.
    skills_alignment: list[dict] = field(default_factory=list)
    # Notable non-dealbreaker concerns worth flagging (e.g. a title/seniority
    # mismatch, an ambiguous hybrid policy) — things that don't fail the
    # dealbreaker sweep but should still shape how the candidate approaches it.
    flags: list[str] = field(default_factory=list)
    rationale: str = ""
    # Only on pursue/review — concrete guidance for how to position the
    # application (what to lead with, what to downplay, an angle for the
    # cover letter) given whatever `flags` surfaced. Empty on a clean pass.
    framing_guidance: list[str] = field(default_factory=list)
    metrics: CallMetrics | None = None


@dataclass
class PackageResult:
    evaluation: EvaluationResult
    jd_path: Path | None = None
    review_path: Path | None = None
    resume_path: Path | None = None
    cover_letter_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    generate_metrics: CallMetrics | None = None

    @property
    def total_input_tokens(self) -> int:
        return (self.evaluation.metrics.input_tokens if self.evaluation.metrics else 0) + (
            self.generate_metrics.input_tokens if self.generate_metrics else 0
        )

    @property
    def total_output_tokens(self) -> int:
        return (self.evaluation.metrics.output_tokens if self.evaluation.metrics else 0) + (
            self.generate_metrics.output_tokens if self.generate_metrics else 0
        )

    @property
    def total_elapsed_s(self) -> float:
        return (self.evaluation.metrics.elapsed_s if self.evaluation.metrics else 0.0) + (
            self.generate_metrics.elapsed_s if self.generate_metrics else 0.0
        )

    @property
    def total_cost_usd(self) -> float | None:
        parts = [
            m.cost_usd
            for m in (self.evaluation.metrics, self.generate_metrics)
            if m is not None and m.cost_usd is not None
        ]
        return sum(parts) if parts else None


def _load_candidate_profile() -> str:
    if not _CANDIDATE_PROFILE_PATH.exists():
        raise LLMApplyError(
            f"Candidate profile not found at {_CANDIDATE_PROFILE_PATH}. Set "
            "JOB_TRACKER_CANDIDATE_PROFILE_PATH if it lives somewhere else."
        )
    return _CANDIDATE_PROFILE_PATH.read_text(encoding="utf-8")


# The SDK's default is 600s per attempt (with retries on top) — fine for a
# one-off call, but a batch run (e.g. triage_recruiter_inbox.py over a large
# backlog) can silently stall for an hour-plus if even one call/retry hits a
# slow patch, since nothing in this codebase's call sites has its own
# timeout. Real evaluate/generate calls finish in well under a minute; a
# call still running after 2 is almost certainly stuck, not just slow, and
# job-tracker's built-in JSON-repair retry (see `_call_and_parse_json`)
# already covers the "got a bad response, try once more" case on top of this.
_ANTHROPIC_TIMEOUT_S = 120.0


def _client():
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")  # pragma: allowlist secret
    if not api_key:
        raise LLMApplyError("ANTHROPIC_API_KEY is not set (see .env.example).")
    return anthropic.Anthropic(api_key=api_key, timeout=_ANTHROPIC_TIMEOUT_S)  # pragma: allowlist secret


def _parse_json_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise LLMApplyError(f"expected a JSON object, got {type(data).__name__}")
    return data


def _call(system: str, user: str, *, model: str, client=None, max_tokens: int = 4096, step: str = "call") -> tuple[str, CallMetrics]:
    client = client or _client()
    start = time.monotonic()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    elapsed_s = time.monotonic() - start
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    metrics = CallMetrics(
        step=step, model=model, input_tokens=input_tokens, output_tokens=output_tokens,
        elapsed_s=elapsed_s, cost_usd=_cost_usd(model, input_tokens, output_tokens),
    )
    return text, metrics


_REPAIR_SYSTEM_PROMPT = (
    "The text below was supposed to be a single valid JSON object but failed to parse "
    f"({{error}}). Output ONLY the corrected, valid JSON object — same content and keys, "
    "just fix the syntax (e.g. escape stray quotes inside string values). No markdown fences, no prose."
)


def _call_and_parse_json(
    system: str, user: str, *, model: str, client=None, max_tokens: int = 4096, step: str = "call"
) -> tuple[dict, list[CallMetrics]]:
    """Call the model and parse its JSON response, with one repair retry if the
    first response isn't valid JSON (observed failure mode: an unescaped quote
    inside a generated string value, e.g. around a quoted program name).
    Returns the parsed dict plus every CallMetrics incurred (1 normally, 2 if
    a repair retry was needed)."""
    client = client or _client()
    raw, metrics = _call(system, user, model=model, client=client, max_tokens=max_tokens, step=step)
    try:
        return _parse_json_object(raw), [metrics]
    except (json.JSONDecodeError, LLMApplyError) as exc:
        logger.warning("JSON parse failed, retrying with a repair call: %s", exc)
        repaired, repair_metrics = _call(
            _REPAIR_SYSTEM_PROMPT.format(error=exc),
            raw,
            model=model,
            client=client,
            max_tokens=max_tokens,
            step=f"{step}_json_repair",
        )
        return _parse_json_object(repaired), [metrics, repair_metrics]


_EVAL_SYSTEM_PROMPT = """You evaluate a job description against a specific candidate's real background, \
using the candidate profile document below, which defines the "JD Match Framework" the candidate uses \
(see its "JD Match Framework" section). Follow that framework's steps exactly: dealbreaker sweep, then \
skills alignment, then an honest match percentage, then a verdict. Produce a review detailed enough that \
the candidate can decide whether to pursue this role WITHOUT re-reading the original JD.

Rules:
- Never invent experience, skills, or history not present in the candidate profile.
- Use "pursue" when the dealbreaker sweep clears and the match is genuinely strong.
- Use "pass" when a real (load-bearing) dealbreaker fires, or the match is weak.
- Use "review" only for genuinely borderline cases worth a human's own judgment.
- A skill or technology mentioned only as one alternative among several acceptable options \
(e.g. "React (or Angular)", "Golang, Java, Python, Ruby, C#, or similar") is NOT load-bearing on its own \
unless the surrounding text makes clear it's the team's actual primary/required choice.
- "job_summary": 2-3 plain sentences on what the role/team actually does, based on the JD text — not a \
sales pitch, just enough for the candidate to recall this specific posting later without re-reading it.
- "dealbreaker_checks": one row per check the candidate profile's dealbreaker table/targeting section \
defines (e.g. banned stack items, employment type, compensation floor, location/remote fit) PLUS the \
profile's "durability lens" if it has one. Each row's "status" is "clean" (no concern), "warning" (a \
soft/non-fatal concern worth noting), or "fail" (an actual dealbreaker). "notes" cites the specific JD \
language that led to that status — never a generic restatement of the rule.
- "skills_alignment": one row per significant JD requirement, each mapped to the candidate's actual \
matching evidence (a named real engagement or portfolio project from the profile, not a vague "yes"). \
"strength" is one of "very_strong", "strong", "moderate", "minor_gap", "gap" — "gap" only if the profile \
has genuinely nothing relevant to point to for that requirement.
- "flags": notable concerns that are NOT dealbreakers but still change how the candidate should approach \
this application — e.g. a title/seniority mismatch (JD's actual day-to-day responsibilities read as more \
junior or more senior than the title suggests), an unusually vague scope, a signal the team is much \
earlier/later stage than the durability lens prefers. Empty list if nothing stands out.
- "framing_guidance": only populate on "pursue" or "review" — concrete, specific advice for HOW to \
position the application given whatever "flags" surfaced (what to lead with, what to downplay or reframe, \
a concrete cover-letter angle). Not generic advice ("highlight your skills") — it must reference the \
specific flags/gaps found for this role. Empty list on "pass".

Respond with ONLY a raw JSON object (no markdown fences, no prose outside the JSON), with exactly these keys:
{
  "job_summary": string,
  "dealbreaker_checks": [ {"check": string, "status": "clean" | "warning" | "fail", "notes": string}, ... ],
  "skills_alignment": [
    {"requirement": string, "evidence": string, "strength": "very_strong" | "strong" | "moderate" | "minor_gap" | "gap"}, ...
  ],
  "match_pct": number (0-100),
  "flags": [string, ...],
  "verdict": "pursue" | "review" | "pass",
  "rationale": string,
  "framing_guidance": [string, ...]
}

--- CANDIDATE PROFILE ---
{profile}
--- END CANDIDATE PROFILE ---
"""

_GENERATE_SYSTEM_PROMPT = """You write a tailored résumé and cover letter for a candidate applying to a \
specific job, using ONLY the real background documented in the candidate profile below. Follow every \
house rule, structure rule, and content rule in that document exactly (canonical résumé structure, career \
timeline/dates, education formatting, banned terms, IC-only framing, and the "Generation Workflow & Output \
Conventions" content rules) — especially:
- Do NOT invent experience, employers, projects, or skills not present in the profile.
- Do NOT include any compensation figure/range, availability statement, or work-authorization statement of \
any kind — this includes citizenship, residency, green-card, sponsorship, or security-clearance-eligibility \
statements, EVEN IF the job description requires them or mentions clearance/citizenship requirements. Simply \
omit the topic entirely; do not reassure the reader about it.
- Do NOT include years on any degree, patent, or certification.
- Tailor bullets/skills selection to this specific JD by choosing from the candidate's real portfolio \
projects and technical anchors — pick what's actually relevant, don't list everything.
- If HomePortfolio is included in "experience", it must be the LAST entry.
- The cover letter body should tie 2-3 concrete real engagements/projects to this JD's stated needs.
- Do not write a salutation naming a specific person unless one is explicitly given in the job description \
below; otherwise use "Dear Hiring Team,".

Respond with ONLY a raw JSON object (no markdown fences, no prose outside the JSON), with exactly this shape:
{
  "resume": {
    "positioning_line": string,
    "summary": string,
    "skills": [string, ...],
    "experience": [
      {
        "employer": string,
        "dates": string,
        "role_note": string | null,
        "bullets": [string, ...],
        "subsections": [ {"heading": string, "bullets": [string, ...]}, ... ]
      }
    ],
    "education": [string, ...]
  },
  "cover_letter": {
    "salutation": string,
    "paragraphs": [string, ...]
  }
}

--- CANDIDATE PROFILE ---
{profile}
--- END CANDIDATE PROFILE ---
"""


def evaluate_lead(
    jd_text: str,
    *,
    company: str,
    title: str,
    model: str = DEFAULT_MODEL,
    client=None,
) -> EvaluationResult:
    profile = _load_candidate_profile()
    system = _EVAL_SYSTEM_PROMPT.replace("{profile}", profile)
    user = f"Company: {company}\nTitle: {title}\n\nJob description:\n{jd_text}"
    data, calls = _call_and_parse_json(system, user, model=model, client=client, step="evaluate")

    def _dict_list(key: str, expected_keys: tuple[str, ...]) -> list[dict]:
        items = data.get(key) or []
        out = []
        for item in items:
            if isinstance(item, dict):
                out.append({k: str(item.get(k, "")) for k in expected_keys})
            elif item:
                # Defensive: tolerate an older/degraded flat-string response
                # (e.g. from a JSON-repair retry that lost structure) rather
                # than dropping the note entirely.
                out.append({expected_keys[0]: str(item)})
        return out

    return EvaluationResult(
        verdict=str(data.get("verdict", "review")).strip().lower(),
        match_pct=float(data.get("match_pct", 0) or 0),
        job_summary=str(data.get("job_summary", "")),
        dealbreaker_checks=_dict_list("dealbreaker_checks", ("check", "status", "notes")),
        skills_alignment=_dict_list("skills_alignment", ("requirement", "evidence", "strength")),
        flags=[str(f) for f in (data.get("flags") or [])],
        rationale=str(data.get("rationale", "")),
        framing_guidance=[str(g) for g in (data.get("framing_guidance") or [])],
        metrics=_sum_metrics("evaluate", model, calls),
    )


_STATUS_LABEL = {"clean": "✅ Clean", "warning": "⚠️ Warning", "fail": "❌ Fail"}
_STRENGTH_LABEL = {
    "very_strong": "Very strong",
    "strong": "Strong",
    "moderate": "Moderate",
    "minor_gap": "Minor gap",
    "gap": "Gap",
}


def render_jd_review(evaluation: EvaluationResult, *, company: str, title: str) -> str:
    """Render an `EvaluationResult` as a human-readable markdown JD review —
    the "About the job / dealbreaker sweep / skills alignment / recommendation"
    report format the candidate reviews leads in (see chat history for the
    reference example this mirrors). Deterministic and free — pure formatting
    over already-computed evaluation data, no LLM call."""
    lines = [f"## {title} @ {company}", ""]

    if evaluation.job_summary:
        lines += ["### About the job", "", evaluation.job_summary, ""]

    if evaluation.dealbreaker_checks:
        lines += ["### Dealbreaker sweep", "", "| Check | Status | Notes |", "|---|---|---|"]
        for row in evaluation.dealbreaker_checks:
            status = _STATUS_LABEL.get(row.get("status", ""), row.get("status", ""))
            lines.append(f"| {row.get('check', '')} | {status} | {row.get('notes', '')} |")
        lines.append("")
        n_fail = sum(1 for r in evaluation.dealbreaker_checks if r.get("status") == "fail")
        lines.append("**No hard dealbreakers.**" if n_fail == 0 else f"**{n_fail} hard dealbreaker(s) fired.**")
        lines.append("")

    if evaluation.skills_alignment:
        lines += [
            "### Skills alignment", "",
            "| JD requirement | Your evidence | Strength |", "|---|---|---|",
        ]
        for row in evaluation.skills_alignment:
            strength = _STRENGTH_LABEL.get(row.get("strength", ""), row.get("strength", ""))
            lines.append(f"| {row.get('requirement', '')} | {row.get('evidence', '')} | {strength} |")
        lines.append("")

    lines.append(f"**Technical match: ~{evaluation.match_pct:.0f}%.** {evaluation.rationale}".strip())
    lines.append("")

    if evaluation.flags:
        lines += ["### Flags", ""]
        lines += [f"- {flag}" for flag in evaluation.flags]
        lines.append("")

    lines.append(f"### Recommendation: {evaluation.verdict.upper()}")
    if evaluation.framing_guidance:
        lines.append("")
        lines += [f"- {g}" for g in evaluation.framing_guidance]

    return "\n".join(lines).rstrip() + "\n"


def _add_table(doc: Document, *, headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for cell, header in zip(table.rows[0].cells, headers):
        cell.text = header
        for p in cell.paragraphs:
            for run in p.runs:
                run.bold = True
    for row in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, row):
            cell.text = value


def render_jd_review_docx(
    evaluation: EvaluationResult, *, company: str, title: str, out_dir: Path = DEFAULT_OUTPUT_ROOT
) -> Path:
    """Render an `EvaluationResult` as `LLM_Review.docx` in that job's
    folder — the docx counterpart of `render_jd_review()`'s markdown, with
    the dealbreaker sweep and skills alignment as real tables instead of
    markdown pipe-tables (this is a saved artifact, not a terminal print)."""
    doc = Document()
    doc.add_heading(f"{title} @ {company}", level=1)

    if evaluation.job_summary:
        doc.add_heading("About the job", level=2)
        doc.add_paragraph(evaluation.job_summary)

    if evaluation.dealbreaker_checks:
        doc.add_heading("Dealbreaker sweep", level=2)
        _add_table(
            doc,
            headers=("Check", "Status", "Notes"),
            rows=[
                (
                    row.get("check", ""),
                    _STATUS_LABEL.get(row.get("status", ""), row.get("status", "")),
                    row.get("notes", ""),
                )
                for row in evaluation.dealbreaker_checks
            ],
        )
        n_fail = sum(1 for r in evaluation.dealbreaker_checks if r.get("status") == "fail")
        p = doc.add_paragraph()
        p.add_run(
            "No hard dealbreakers." if n_fail == 0 else f"{n_fail} hard dealbreaker(s) fired."
        ).bold = True

    if evaluation.skills_alignment:
        doc.add_heading("Skills alignment", level=2)
        _add_table(
            doc,
            headers=("JD requirement", "Your evidence", "Strength"),
            rows=[
                (
                    row.get("requirement", ""),
                    row.get("evidence", ""),
                    _STRENGTH_LABEL.get(row.get("strength", ""), row.get("strength", "")),
                )
                for row in evaluation.skills_alignment
            ],
        )

    p = doc.add_paragraph()
    p.add_run(f"Technical match: ~{evaluation.match_pct:.0f}%.").bold = True
    if evaluation.rationale:
        p.add_run(f" {evaluation.rationale}")

    if evaluation.flags:
        doc.add_heading("Flags", level=2)
        for flag in evaluation.flags:
            doc.add_paragraph(flag, style="List Bullet")

    doc.add_heading(f"Recommendation: {evaluation.verdict.upper()}", level=2)
    for guidance in evaluation.framing_guidance:
        doc.add_paragraph(guidance, style="List Bullet")

    out_path = _job_folder(out_dir, company=company, title=title) / "LLM_Review.docx"
    doc.save(str(out_path))
    return out_path


def _generate_content(
    jd_text: str,
    *,
    company: str,
    title: str,
    model: str,
    client=None,
) -> tuple[dict, list[CallMetrics]]:
    profile = _load_candidate_profile()
    system = _GENERATE_SYSTEM_PROMPT.replace("{profile}", profile)
    user = f"Company: {company}\nTitle: {title}\n\nJob description:\n{jd_text}"
    return _call_and_parse_json(system, user, model=model, client=client, max_tokens=8192, step="generate")


_HOUSE_RULE_REPAIR_SYSTEM_PROMPT = """Rewrite the JSON object below, fixing ONLY these violations, and leave \
everything else exactly as-is:
{issues}

Specifically: remove any sentence or clause about citizenship, residency, green card, work authorization, or \
security-clearance eligibility (do not replace it with anything, just remove it and smooth the surrounding \
sentence); and remove any dollar figure, hourly rate, salary range, or compensation comparison (e.g. a \
W2-vs-C2C rate comparison) — again just remove it and smooth the surrounding sentence, don't replace it \
with a vaguer restatement of the same idea.

Respond with ONLY the corrected raw JSON object, same schema and keys, no markdown fences, no prose."""


def _repair_house_rule_violations(
    content: dict, *, issues: list[str], model: str, client=None
) -> tuple[dict, list[CallMetrics]]:
    system = _HOUSE_RULE_REPAIR_SYSTEM_PROMPT.format(issues="\n".join(f"- {i}" for i in issues))
    try:
        return _call_and_parse_json(
            system, json.dumps(content), model=model, client=client, max_tokens=8192, step="generate_house_rule_repair"
        )
    except (json.JSONDecodeError, LLMApplyError) as exc:
        logger.warning("House-rule repair pass failed (%s); keeping original content with warnings intact", exc)
        return content, []


def _check_house_rules(content: dict, *, company: str) -> list[str]:
    """Mechanical safety net for the house rules the generation prompt states
    in natural language. Returns human-readable warning strings; never
    raises, and never silently edits content — callers should surface these
    to the user so they can review before sending anything out."""
    text = json.dumps(content)
    warnings = [f"banned term found: {term!r}" for term in _BANNED_TERMS if term.lower() in text.lower()]

    cover_text = json.dumps(content.get("cover_letter") or {})
    if _WORK_AUTH_RE.search(cover_text):
        warnings.append(
            "possible work-authorization/citizenship/clearance statement found in cover letter — "
            "CLAUDE.md bans these outright; remove before sending"
        )

    if _COMP_FIGURE_RE.search(text):
        warnings.append(
            "possible compensation figure/rate/range found — CLAUDE.md §4 rule 12 bans these outright "
            "(compensation is a live conversation, never written into the package); remove before sending"
        )

    return warnings


def _safe_filename(name: str) -> str:
    return _UNSAFE_FILENAME_CHARS.sub("", name.replace(" ", "_"))


def _job_folder(out_dir: Path, *, company: str, title: str) -> Path:
    """One folder per job under the output root (`<Company>_<Title>/`)
    holding every artifact for that role together — JD, LLM review, résumé,
    cover letter — instead of scattering them across parallel Reviews/
    CoverLetters subfolders. Created if missing; reused as-is if a prior
    evaluate/generate run already made it (e.g. the JD + review land first,
    the résumé/cover letter land later only if the verdict is "pursue")."""
    folder = out_dir / _safe_filename(f"{company}_{title}")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def render_job_description(jd_text: str, *, company: str, title: str, out_dir: Path = DEFAULT_OUTPUT_ROOT) -> Path:
    """Save the JD text this lead was actually evaluated against as its own
    docx, alongside the review/résumé/cover letter — so the full context for
    a decision is preserved even if the source email is later deleted or the
    ATS posting expires."""
    doc = Document()
    doc.add_heading(f"{title} @ {company}", level=1)
    for para in (jd_text or "(no JD text captured)").split("\n\n"):
        if para.strip():
            doc.add_paragraph(para.strip())

    out_path = _job_folder(out_dir, company=company, title=title) / "JobDescription.docx"
    doc.save(str(out_path))
    return out_path


def _add_muted_github_line(doc: Document, extra: str = "") -> None:
    p = doc.add_paragraph()
    if extra:
        p.add_run(extra)
    run = p.add_run(CANDIDATE_GITHUB)
    run.font.color.rgb = RGBColor.from_string(GITHUB_COLOR_HEX)
    run.font.size = Pt(10)


def render_resume(resume: dict, *, company: str, title: str, out_dir: Path = DEFAULT_OUTPUT_ROOT) -> Path:
    doc = Document()
    doc.add_heading(CANDIDATE_NAME, level=1)
    doc.add_paragraph(resume.get("positioning_line") or "Senior Software Engineer & Independent Consultant")
    _add_muted_github_line(doc, extra=f"{CANDIDATE_EMAIL}  |  {CANDIDATE_LINKEDIN}  |  ")

    if resume.get("summary"):
        doc.add_heading("Summary", level=2)
        doc.add_paragraph(resume["summary"])

    if resume.get("skills"):
        doc.add_heading("Skills", level=2)
        doc.add_paragraph(", ".join(resume["skills"]))

    experience = list(resume.get("experience") or [])
    # Safety net: HomePortfolio must be last regardless of what the model returned.
    experience.sort(key=lambda e: 1 if "homeportfolio" in str(e.get("employer", "")).lower() else 0)

    if experience:
        doc.add_heading("Experience", level=2)
    for entry in experience:
        header_p = doc.add_paragraph()
        header_p.add_run(str(entry.get("employer", ""))).bold = True
        if entry.get("dates"):
            header_p.add_run(f"   ({entry['dates']})").italic = True
        if entry.get("role_note"):
            note_p = doc.add_paragraph(str(entry["role_note"]))
            for run in note_p.runs:
                run.italic = True
        for bullet in entry.get("bullets") or []:
            doc.add_paragraph(str(bullet), style="List Bullet")
        for sub in entry.get("subsections") or []:
            sub_p = doc.add_paragraph()
            sub_p.add_run(str(sub.get("heading", ""))).bold = True
            for bullet in sub.get("bullets") or []:
                doc.add_paragraph(str(bullet), style="List Bullet")

    if resume.get("education"):
        doc.add_heading("Education & Credentials", level=2)
        for item in resume["education"]:
            doc.add_paragraph(str(item), style="List Bullet")

    out_path = _job_folder(out_dir, company=company, title=title) / _safe_filename(
        f"Shawn_Becker_Resume_{company}_{title}.docx"
    )
    doc.save(str(out_path))
    return out_path


def render_cover_letter(cover_letter: dict, *, company: str, title: str, out_dir: Path = DEFAULT_OUTPUT_ROOT) -> Path:
    doc = Document()
    doc.add_paragraph().add_run(CANDIDATE_NAME).bold = True
    _add_muted_github_line(doc, extra=f"{CANDIDATE_EMAIL}  |  {CANDIDATE_PHONE}  |  {CANDIDATE_LINKEDIN}  |  ")
    doc.add_paragraph()

    doc.add_paragraph(cover_letter.get("salutation") or "Dear Hiring Team,")
    for para in cover_letter.get("paragraphs") or []:
        doc.add_paragraph(str(para))

    doc.add_paragraph()
    doc.add_paragraph("Sincerely,")
    doc.add_paragraph(CANDIDATE_NAME)

    out_path = _job_folder(out_dir, company=company, title=title) / _safe_filename(
        f"Shawn_Becker_Cover_Letter_{company}_{title}.docx"
    )
    doc.save(str(out_path))
    return out_path


def generate_package(
    jd_text: str,
    *,
    company: str,
    title: str,
    model: str = DEFAULT_MODEL,
    client=None,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> PackageResult:
    """Evaluate a lead (always saving the JD text + LLM review into that
    job's folder — see `_job_folder`) and, only on a "pursue" verdict,
    additionally generate + save a tailored résumé and cover letter into the
    same folder. Returns the evaluation either way — CLAUDE.md's "on a
    dealbreaker or pass, report the mismatch and stop" only means "don't
    generate a résumé/cover letter", not "don't write down the review"."""
    evaluation = evaluate_lead(jd_text, company=company, title=title, model=model, client=client)
    jd_path = render_job_description(jd_text, company=company, title=title, out_dir=output_root)
    review_path = render_jd_review_docx(evaluation, company=company, title=title, out_dir=output_root)
    if evaluation.verdict != "pursue":
        return PackageResult(evaluation=evaluation, jd_path=jd_path, review_path=review_path)

    content, generate_calls = _generate_content(jd_text, company=company, title=title, model=model, client=client)
    warnings = _check_house_rules(content, company=company)
    if warnings:
        logger.warning("Generated content for %s / %s failed house-rule checks: %s", company, title, warnings)
        content, repair_calls = _repair_house_rule_violations(content, issues=warnings, model=model, client=client)
        generate_calls += repair_calls
        warnings = _check_house_rules(content, company=company)
        if warnings:
            logger.warning("House-rule violations persisted after repair pass for %s / %s: %s", company, title, warnings)

    resume_path = render_resume(content.get("resume") or {}, company=company, title=title, out_dir=output_root)
    cover_letter_path = render_cover_letter(
        content.get("cover_letter") or {}, company=company, title=title, out_dir=output_root
    )
    return PackageResult(
        evaluation=evaluation,
        jd_path=jd_path,
        review_path=review_path,
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        warnings=warnings,
        generate_metrics=_sum_metrics("generate", model, generate_calls),
    )
