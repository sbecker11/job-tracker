"""Answer what the recruiter actually asked on a live thread (REQUIREMENTS DRAFT-9).

The clarifier template only knows what is still unknown about the role. When
the recruiter's newest message asks for a call, asks Shawn's rate, or quotes a
rate, the reply has to answer that first. Rates are compared to the internal
floor in `config/framework.yaml`; the draft never states a figure (CLAUDE.md
§4 rule 12)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from job_tracker.pipeline.qualifying_reply import SIGNATURE, PitchGaps
from job_tracker.scoring.scorer import load_framework

PHONE = "+1 857-891-0896"

_QUOTED_HISTORY_RE = re.compile(
    r"(?m)^\s*On .{0,120}wrote:\s*$|^\s*-{2,}\s*Original Message|^From: .*\n(?:Sent|Date): "
)
_LINKEDIN_CHROME_RE = re.compile(r"(?s)https://www\.linkedin\.com/messaging/thread/\S+\s*(.*?)(?:-{10,}|This email was intended|$)")
_CALL_RE = re.compile(
    r"(?i)\b(?:good|best|convenient) time\b"
    r"|\btime (?:to|for) (?:a )?(?:quick )?(?:call|chat|talk|connect|speak)"
    r"|\b(?:hop|jump|get) on a (?:quick )?call\b"
    r"|\bschedule (?:a|some) (?:quick )?(?:call|time|chat)\b"
    r"|\bopen to (?:a )?(?:quick |brief )?(?:call|conversation|chat)\b"
    r"|\b(?:speak|talk|meet|connect) with (?:our|the) (?:founder|ceo|cto|hiring manager|manager)\b"
    r"|\byour availability\b"
    r"|\bwhen (?:are|would) you (?:be )?(?:free|available)\b"
)
_DETAILS_PENDING_RE = re.compile(
    r"(?i)\b(?:exchange|trade) (?:a few )?messages\b"
    r"|\bha(?:s|ve) not (?:yet )?been (?:finalized|decided|determined)\b"
    r"|\bnot (?:yet )?finalized\b"
)
_FOUNDER_RE = re.compile(r"(?i)\b(?:our|the) (founder|ceo|cto|hiring manager)\b")
_RATE_ASK_RE = re.compile(
    r"(?i)\b(?:what(?:'|’)?s|what is|what would be) your (?:expected |desired )?(?:expectation )?(?:rate|salary|compensation)"
    r"|\b(?:rate|salary|compensation|pay) expectations?\b"
    r"|\bexpect(?:ed|ation)s? (?:rate|salary)\b"
    r"|\bexpectation rate\b"
    r"|\b(?:desired|target) (?:rate|salary)\b"
)
_NUM = r"(\d{2,3}(?:\.\d{1,3})?)"
_RANGE_SEP = r"\s*(?:-|–|to)\s*\$?\s?"
_HOURLY_RE = re.compile(
    rf"\$\s?{_NUM}(?:{_RANGE_SEP}{_NUM})?(?!\s?[kK]\b)(?![\d,])(?P<ctx>[^$\n]{{0,30}})"
)
_ANNUAL_K_RE = re.compile(rf"\$\s?{_NUM}\s?[kK]?(?:{_RANGE_SEP}{_NUM})?\s?[kK]\b")
_ANNUAL_FULL_RE = re.compile(r"\$\s?(\d{2,3}),\s?(\d{3})(?:\s*(?:-|–|to)\s*\$?\s?(\d{2,3}),\s?(\d{3}))?")
_MIN_SALARY = 30000
_C2C_RE = re.compile(r"(?i)\bc2c\b|corp[- ]to[- ]corp")
_W2_RE = re.compile(r"(?i)\bw-?2\b")
_TEN99_RE = re.compile(r"(?i)\b1099\b")
_HOURLY_CTX_RE = re.compile(r"(?i)\b(?:hr|hour|ph|w-?2|1099)\b|/h")


@dataclass
class QuotedRate:
    meets_floor: bool
    w2_and_1099: bool


def newest_message_text(body: str) -> str:
    """The recruiter's own words: the LinkedIn message after the thread link,
    or an email above its quoted history."""
    text = body or ""
    m = _LINKEDIN_CHROME_RE.search(text)
    if m and m.group(1).strip():
        text = m.group(1)
    cut = _QUOTED_HISTORY_RE.search(text)
    if cut:
        text = text[: cut.start()]
    return text.strip()


def quoted_rate(text: str, *, framework: dict | None = None) -> QuotedRate | None:
    floors = ((framework or load_framework()).get("candidate") or {}).get("compensation_floor") or {}
    hourly_floor = float(floors.get("contract_hourly_w2_usd") or 0)
    annual_floor = float(floors.get("permanent_base_usd") or 0)
    hourly: list[tuple[float, str]] = []
    for m in _HOURLY_RE.finditer(text):
        ctx = m.group("ctx")
        if not _HOURLY_CTX_RE.search(ctx):
            continue
        if _C2C_RE.search(ctx) and not (_W2_RE.search(ctx) or _TEN99_RE.search(ctx)):
            continue
        hourly.append((float(m.group(2) or m.group(1)), ctx))
    annual = [float(m.group(2) or m.group(1)) * 1000 for m in _ANNUAL_K_RE.finditer(text)]
    annual += [
        float((m.group(3) + m.group(4)) if m.group(3) else (m.group(1) + m.group(2)))
        for m in _ANNUAL_FULL_RE.finditer(text)
    ]
    annual = [v for v in annual if v >= _MIN_SALARY]
    if not hourly and not annual:
        return None
    w2 = [v for v, ctx in hourly if _W2_RE.search(ctx)]
    best = max(w2 or [v for v, _ in hourly] or [0.0])
    meets = (bool(hourly) and best >= hourly_floor) or any(v >= annual_floor for v in annual)
    both = bool(_W2_RE.search(text) and _TEN99_RE.search(text))
    return QuotedRate(meets_floor=meets, w2_and_1099=both)


def draft_live_thread_reply(
    body: str,
    *,
    first_name: str,
    gaps: PitchGaps,
    framework: dict | None = None,
) -> str | None:
    """A reply to the recruiter's newest message, or None when it asks nothing
    this module recognizes (the caller keeps its clarifier/ack draft)."""
    newest = newest_message_text(body)
    wants_call = bool(_CALL_RE.search(newest))
    rate_asked = bool(_RATE_ASK_RE.search(newest))
    rate = quoted_rate(newest, framework=framework)
    details_pending = bool(_DETAILS_PENDING_RE.search(newest))
    if not (wants_call or rate_asked or rate or details_pending):
        return None

    parts: list[str] = []
    if rate is not None and not rate.meets_floor:
        parts.append(
            "Thanks for the details. That rate is below what I'm targeting, so I'll pass on this one, "
            "but please keep me in mind for future roles."
        )
        return _compose(first_name, parts)
    if rate is not None:
        line = "The range you mentioned works for me" if rate_asked else "Thanks, that rate works for me"
        parts.append(line + (", and W2 is my preference." if rate.w2_and_1099 else "."))
    elif rate_asked:
        parts.append(
            "I'd like to see the job description and scope before settling on a rate. What band does the client have?"
        )

    if wants_call:
        who = _FOUNDER_RE.search(newest)
        when = " tomorrow" if re.search(r"(?i)\btomorrow\b", newest) else ""
        parts.append(
            f"Happy to talk{' with your ' + who.group(1).lower() if who else ''}{when}. "
            f"What times work on your side? You can also reach me at {PHONE}."
        )
        return _compose(first_name, parts)

    if details_pending:
        parts.append(
            "Happy to keep the conversation going here. Could you share the company name and what "
            "the work would involve? When the structure is settled, W2 or 1099 direct-to-individual "
            "both work for me; I'm not set up for C2C."
        )
        return _compose(first_name, parts)

    asks: list[str] = []
    if gaps.needs_end_client:
        asks.append("Who is the end client / hiring company?")
    if gaps.needs_engagement and not (rate and rate.w2_and_1099):
        asks.append("Is the engagement W2 or 1099 direct-to-individual? I'm not set up for C2C.")
    if gaps.needs_remote:
        asks.append("Is the role fully remote (US)?")
    if gaps.needs_jd:
        asks.append("Could you send the job description so I can tailor my résumé?")
    parts.extend(asks)
    return _compose(first_name, parts)


def _compose(first_name: str, parts: list[str]) -> str:
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    return f"{greeting}\n\n" + " ".join(parts) + f"\n\n{SIGNATURE}"
