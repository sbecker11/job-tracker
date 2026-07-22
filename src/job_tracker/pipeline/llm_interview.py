"""LLM-based structured extraction of interview details (date/time, format,
interviewer) from a message already classified as an INTERVIEW_INVITE post-
application signal — see `pipeline/post_application.py`.

Deliberately its own tiny module rather than folded into `llm_extract.py`:
that module extracts (company, title) job POSTINGS from arbitrary inbound
mail; this one extracts SCHEDULING details from a message already known to
be an interview invite for an already-tracked lead — a completely different
question, asked far less often (once per interview invite, not once per
digest), so it doesn't need `llm_extract.py`'s caching table at all.

Opt-in only, same reasoning as `llm_extract.py`: costs real money per call.
Callers gate this on the same `--llm-fallback` flag they already pass
through for role extraction, rather than adding a second flag.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.llm_extract import DEFAULT_MODEL, LLMExtractionError, _ANTHROPIC_TIMEOUT_S

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 6_000

_SYSTEM_PROMPT = """You extract interview scheduling details from a single recruiting email that \
invites a candidate to interview or confirms an interview has been scheduled.

Rules:
1. Only extract details actually stated in the email text. Never invent or guess a date, \
time, format, or interviewer name that isn't present.
2. Leave any field "" if the email doesn't state it.
3. Respond with ONLY a raw JSON object (no markdown fences, no prose). Keys:
   - "date_text": string — the interview date/day as written (e.g. "Thursday, July 24" or \
"next Tuesday"), or "" if not stated.
   - "time_text": string — the interview time as written (e.g. "2:00 PM ET"), or "" if not stated.
   - "format": string — one of "phone", "video", "onsite", "" (unknown/not stated).
   - "interviewer_name": string — the named interviewer(s)/panel, or "" if not stated.
   - "notes": string — a short (<=200 char) verbatim-ish summary of any other concrete detail \
(what to prepare, round number, duration), or "".

Example valid response:
{"date_text": "Thursday, July 24", "time_text": "2:00 PM ET", "format": "video", \
"interviewer_name": "Jane Smith, Engineering Manager", "notes": "45-minute technical round"}

Example valid response when nothing concrete is stated beyond the invite itself:
{"date_text": "", "time_text": "", "format": "", "interviewer_name": "", "notes": ""}
"""


@dataclass
class InterviewDetails:
    date_text: str = ""
    time_text: str = ""
    format: str = ""
    interviewer_name: str = ""
    notes: str = ""

    @property
    def is_empty(self) -> bool:
        return not any([self.date_text, self.time_text, self.format, self.interviewer_name, self.notes])

    def as_summary(self) -> str:
        """One-line human-readable rendering, used to enrich a
        `JobConversation.summary` — e.g. "Interview invite: Thursday, July 24
        at 2:00 PM ET (video) with Jane Smith, Engineering Manager"."""
        if self.is_empty:
            return "Interview invite"
        parts = ["Interview invite:"]
        when = " ".join(p for p in [self.date_text, f"at {self.time_text}" if self.time_text else ""] if p)
        if when:
            parts.append(when)
        if self.format:
            parts.append(f"({self.format})")
        if self.interviewer_name:
            parts.append(f"with {self.interviewer_name}")
        summary = " ".join(parts)
        if self.notes:
            summary += f" — {self.notes}"
        return summary


def _client():
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")  # pragma: allowlist secret
    if not api_key:
        raise LLMExtractionError(
            "ANTHROPIC_API_KEY is not set. Add it to job-tracker/.env (see .env.example)."
        )
    return anthropic.Anthropic(api_key=api_key, timeout=_ANTHROPIC_TIMEOUT_S)  # pragma: allowlist secret


def _parse_response_text(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data, _end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise LLMExtractionError(f"expected a JSON object, got {type(data).__name__}")
    return data


def _dict_to_details(data: dict) -> InterviewDetails:
    valid_formats = {"phone", "video", "onsite"}
    fmt = str(data.get("format") or "").strip().lower()
    if fmt not in valid_formats:
        fmt = ""
    return InterviewDetails(
        date_text=str(data.get("date_text") or "").strip(),
        time_text=str(data.get("time_text") or "").strip(),
        format=fmt,
        interviewer_name=str(data.get("interviewer_name") or "").strip(),
        notes=str(data.get("notes") or "").strip()[:200],
    )


def extract_interview_details_llm(
    message: EmailMessage,
    *,
    model: str = DEFAULT_MODEL,
    client=None,
) -> InterviewDetails | None:
    """Best-effort structured extraction; returns None on any failure
    (network, auth, unparseable output) so callers can fall back to the
    plain message subject as the conversation summary rather than crashing
    a triage run over a scheduling-detail nicety."""
    try:
        client = client or _client()
        user_prompt = (
            f"Email subject: {message.subject}\n"
            f"Email sender: {message.from_address}\n"
            "---- EMAIL BODY START ----\n"
            f"{message.combined_text[:MAX_BODY_CHARS]}\n"
            "---- EMAIL BODY END ----"
        )
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        data = _parse_response_text(raw_text)
        return _dict_to_details(data)
    except Exception:
        logger.warning("Interview-detail LLM extraction failed for message %s", message.id, exc_info=True)
        return None
