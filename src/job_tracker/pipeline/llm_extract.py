"""LLM-based extraction fallback for job digest emails the rule-based parsers
in pipeline/extract.py can't confidently handle.

Design constraints (see the README section this backs):
  - Opt-in only (`--llm-fallback`), never called by default — it costs real
    money per call.
  - One call per MESSAGE, not per candidate role — a single digest can list
    dozens of incomplete roles from the regex pass; calling the LLM once per
    role would multiply cost for no benefit, since the LLM sees the whole
    message anyway and can extract every listing in one shot.
  - Result is cached by message_id (pipeline/store.py's llm_extraction_cache
    table) so re-running the pipeline over the same backlog never re-bills a
    message that's already been classified.
  - The prompt is strict about never inventing a company or title that isn't
    actually present in the text: an empty result is always preferable to a
    fabricated one, since a fabricated lead corrupts scoring and can send the
    user chasing a job that doesn't exist.
"""

from __future__ import annotations

import json
import logging
import os
import time

from job_tracker.email.models import EmailMessage, ExtractedRole

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("JOB_TRACKER_LLM_MODEL", "claude-haiku-4-5")

# USD per million tokens: (input, output). Keep in sync with llm_apply.py.
_MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4-8": (5.00, 25.00),
}

# Digests rarely need more than this to list every posting; keeping the
# prompt small keeps both latency and per-call cost down.
MAX_BODY_CHARS = 12_000

_SYSTEM_PROMPT = """You extract real job postings from a single recruiting/job-board email.

The email may be:
- a single job description,
- a digest listing several job postings (LinkedIn, Lensa, Talent.com, Indeed, \
Adzuna, Robert Half, corporate ATS "search agent" notifications, etc.),
- a rejection, marketing blast, or other message with no real posting at all.

Rules (follow all of them exactly):
1. Only extract postings that are actually described in the email text. Never \
invent, guess, or infer a company or title that isn't literally present.
2. The email SENDER is very often a job board or ATS platform (LinkedIn, \
Lensa, Talent.com, Indeed, Adzuna, ZipRecruiter, Glassdoor, Monster, Dice, \
CareerBuilder, Robert Half, etc.) forwarding postings from OTHER employers. \
Never use the job board/platform/staffing-agency name itself as the \
"company" for a listing — leave company as an empty string ("") if the \
actual hiring employer isn't stated for that specific listing, rather than \
guessing or defaulting to the sender.
3. If you cannot confidently determine BOTH a company and a title for a \
listing, still include it with whichever field you have and leave the other \
as "". Do not drop it, and do not fabricate the missing field.
4. If the email is marketing noise, a rejection, or otherwise contains no \
real posting, return an empty JSON array: []
5. Respond with ONLY a raw JSON array (no markdown code fences, no prose \
before or after it). Each element must be an object with exactly these keys:
   - "company": string (employer name, or "" if unknown)
   - "title": string (job title, or "" if unknown)
   - "apply_url": string (a direct application/posting URL if one is clearly \
associated with this specific listing, else "")
   - "excerpt": string — a VERBATIM quote copied directly from the email \
text containing everything describing THIS specific listing (requirements, \
location, seniority, etc.) and nothing about any other listing in the same \
digest. Copy the actual substring from the email; never paraphrase or \
summarize it. Empty string ("") only if the digest has no descriptive text \
for this listing beyond its title.
   - "confidence": number from 0.0 to 1.0 reflecting how certain you are \
this is a real, correctly-attributed listing

Example valid response:
[{"company": "Acme Corp", "title": "Senior Backend Engineer", "apply_url": "https://acme.com/jobs/123", "excerpt": "Senior Backend Engineer - Acme Corp - Remote (US)\\n5+ years Python, AWS, and distributed systems experience required.", "confidence": 0.9}]

Example valid response for an email with no real postings:
[]
"""


class LLMExtractionError(RuntimeError):
    """Raised when the LLM call fails or returns unusable output."""


# See the matching comment in pipeline/llm_apply.py — the SDK's 600s default
# per-attempt timeout has no business being that long for a call that
# normally finishes in seconds, and a batch triage run has no per-message
# error handling around a stuck call.
_ANTHROPIC_TIMEOUT_S = 120.0


def _client():
    import anthropic  # imported lazily so `anthropic` is only required when this path is used

    api_key = os.environ.get("ANTHROPIC_API_KEY")  # pragma: allowlist secret
    if not api_key:
        raise LLMExtractionError(
            "ANTHROPIC_API_KEY is not set. Add it to job-tracker/.env (see .env.example)."
        )
    return anthropic.Anthropic(api_key=api_key, timeout=_ANTHROPIC_TIMEOUT_S)  # pragma: allowlist secret


def _build_user_prompt(message: EmailMessage) -> str:
    body = message.combined_text[:MAX_BODY_CHARS]
    return (
        f"Email subject: {message.subject}\n"
        f"Email sender: {message.from_address}\n"
        "---- EMAIL BODY START ----\n"
        f"{body}\n"
        "---- EMAIL BODY END ----"
    )


def _parse_response_text(raw: str) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        # Models occasionally wrap JSON in a fence despite instructions not
        # to; strip a leading ```json / ``` and a trailing ``` defensively.
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    if not isinstance(data, list):
        raise LLMExtractionError(f"expected a JSON array, got {type(data).__name__}")
    return data


def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    pricing = _MODEL_PRICING_USD_PER_MTOK.get(model)
    if pricing is None:
        return None
    in_rate, out_rate = pricing
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _fmt_usd(cost: float | None) -> str:
    return f"${cost:.4f}" if cost is not None else "n/a"


def _call_llm_raw(message: EmailMessage, *, model: str, client=None) -> list[dict]:
    """Call the LLM and return the raw parsed JSON items. Raises on any
    failure (network, auth, unparseable output) — use `extract_roles_llm`
    for a version that swallows failures and returns [] instead.
    """
    client = client or _client()
    max_tokens = 4096
    user_prompt = _build_user_prompt(message)
    est_in = _estimate_tokens(_SYSTEM_PROMPT) + _estimate_tokens(user_prompt)
    est_out = max(64, min(max_tokens, max_tokens // 4))
    pred = _cost_usd(model, est_in, est_out)
    print(
        f"    [llm extract] pred ~{_fmt_usd(pred)} (est. {est_in} in / ~{est_out} out)",
        flush=True,
    )
    start = time.monotonic()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    elapsed_s = time.monotonic() - start
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cost = _cost_usd(model, input_tokens, output_tokens)
    print(
        f"    [llm extract] actual ~{_fmt_usd(cost)} "
        f"({input_tokens} in / {output_tokens} out, {elapsed_s:.1f}s)",
        flush=True,
    )
    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    return _parse_response_text(raw_text)


def _items_to_roles(items: list[dict]) -> list[ExtractedRole]:
    roles: list[ExtractedRole] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        company = str(item.get("company") or "").strip()
        title = str(item.get("title") or "").strip()
        if not company and not title:
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(confidence, 1.0))
        roles.append(
            ExtractedRole(
                company=company,
                title=title,
                apply_url=str(item.get("apply_url") or "").strip(),
                source="llm_fallback",
                confidence=confidence,
                # Verbatim per-listing excerpt (added 2026-07-07 alongside
                # ExtractedRole.snippet) — keeps a digest's roles from being
                # scored against each other's requirements downstream, same
                # motivation as the regex extractors' snippet fields.
                snippet=str(item.get("excerpt") or "").strip(),
            )
        )
    return roles


def extract_roles_llm(
    message: EmailMessage,
    *,
    model: str = DEFAULT_MODEL,
    client=None,
) -> list[ExtractedRole]:
    """Ask the LLM to extract (company, title) pairs from one email.

    Returns [] both when the LLM finds no real postings AND when the call
    fails outright — callers should treat both the same way ("still needs
    manual review") rather than distinguishing failure from a genuinely
    empty digest.
    """
    try:
        items = _call_llm_raw(message, model=model, client=client)
    except Exception:
        logger.warning("LLM extraction fallback failed for message %s", message.id, exc_info=True)
        return []
    return _items_to_roles(items)


def extract_roles_llm_cached(
    conn,
    message: EmailMessage,
    *,
    model: str = DEFAULT_MODEL,
    client=None,
) -> list[ExtractedRole]:
    """Same as `extract_roles_llm`, but checks/populates the SQLite cache in
    `conn` (pipeline/store.py's llm_extraction_cache table) first, so a given
    message is only ever billed once across repeated pipeline runs.

    A failed call is deliberately NOT cached (so a transient network error
    can be retried next run); a successful call IS cached even when it
    returns zero roles, since that's a legitimate answer ("no real postings
    here") that would otherwise be re-billed on every future run.
    """
    from job_tracker.pipeline import store

    cached = store.get_llm_cache(conn, message.id)
    if cached is not None:
        return _items_to_roles(cached)

    try:
        items = _call_llm_raw(message, model=model, client=client)
    except Exception:
        logger.warning("LLM extraction fallback failed for message %s", message.id, exc_info=True)
        return []

    store.set_llm_cache(conn, message.id, model, items)
    return _items_to_roles(items)
