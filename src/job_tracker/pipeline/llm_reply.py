"""LLM-written messages for Reply-to-recruiter, Send-package and Follow-up cards.

The rule-based drafts in qualifying_reply / reply_intent read like a form:
they re-ask what the recruiter already said and never make the case for
Shawn. This module has the model write each reply from the actual thread,
the JD, and ~/CLAUDE.md, and keeps the rule-based draft only as a fallback
(no API key, API error, time budget spent, or a house-rule violation).

Drafts are cached in `llm_reply_drafts`, keyed by card, and reused until the
thread, JD, recruiter, channel or profile changes, so a render only pays for
cards whose inputs moved.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from job_tracker.pipeline.llm_call_log import log_call
from job_tracker.pipeline.qualifying_reply import greeting_line

logger = logging.getLogger(__name__)

PROMPT_VERSION = "2026-11-05.8"
DEFAULT_REPLY_MODEL = os.environ.get("JOB_TRACKER_REPLY_MODEL", "claude-opus-4-8")
RENDER_BUDGET_S = float(os.environ.get("JOB_TRACKER_REPLY_BUDGET_S", "90"))
_MAX_WORKERS = 6
_THREAD_CHARS = 3_000
_THREAD_MESSAGES = 8
_JD_CHARS = 6_000

SIGN_OFF_EMAIL = "Best,\nShawn\nshawn.becker@spexture.com\n+1 857-891-0896"
SIGN_OFF_LINKEDIN = "Best,\nShawn\nshawn.becker@spexture.com"

_SYSTEM = """You write replies from Shawn Becker to recruiters. Shawn needs his next role soon, so every \
reply must move the process forward: make it easy for the recruiter to submit him or book a call today.

Write like a capable senior engineer writing to a recruiter he wants to work with: direct, warm, specific. \
Never sound like a template or a form letter.

What a good reply does:
- Opens by answering or acknowledging what the recruiter actually said in their latest message.
- States clear interest in being submitted / moving forward when the role is a plausible fit.
- Each thread entry is a separate message with its own date. If the recruiter later pitched a different \
role, treat it as a second opportunity: express interest in each plausible role instead of asking which \
one is meant.
- Gives 1-3 concrete proof points from the candidate profile below that map to THIS role's core needs \
(named engagement or project + what he did). Pick the closest structural match to what the role actually \
does (profile §13 maps themes to the best proof points), not just the most recent or most impressive work. Use only facts from the profile. Never invent employers, \
metrics, tools, dates, or domain claims. Portfolio projects that AI coding agents mostly wrote are \
"designed/directed", never "built/implemented" (profile §8).
- Is honest about a real gap in one short clause when the role hinges on something he lacks, framed as a \
fast ramp-up, not an apology.
- Asks only for facts that are truly still unknown after reading the WHOLE thread and JD (e.g. the JD \
itself, engagement type W2 vs 1099-direct, remote/onsite). Never re-ask anything the recruiter already \
answered. Don't ask about rate or the end client unless it is genuinely unknown and matters. At most 2 \
questions.
- Ends with a concrete next step: offer to send his résumé right away and propose a quick call this week. \
If the recruiter's latest message asks for his résumé, say it is attached (Shawn will attach it); \
otherwise offer to send it, and never say the résumé is attached.
- Never propose specific days or times for a call; you don't know Shawn's calendar. Ask the recruiter \
to send a few times that work for them.
- Don't narrate assumptions ("so I'll assume W2"); if engagement type doesn't need asking, leave it out. \
For a full-time role at the hiring company, say nothing at all about employment type.
- Credit the right kind of work: client engagements and hand-coded projects (resume-flyer, \
color-palette-maker [React], Munsell viewer, 3D axes viewer) are "built"; the AI-agent-written projects are \
"designed/directed". Don't call his React experience thin: color-palette-maker is hand-coded React, and \
sushi-rag-mcp-server has a React frontend.

- Use his strongest credential when the role touches it: the MIT PhD in computer vision for CV/ML/imaging \
roles, the co-invented NLP patents for search/recommendation/NLP roles. Never undersell real depth.
- A full-time job posted directly by the hiring company is an employee role: don't ask about W2/1099/C2C.

Hard rules (never break):
- Today is {today}. Every client engagement in the profile is in the past (the latest, Adobe, ended \
07/2025); never describe one as current, ongoing, or "this year".
- Attribute a tool, technique, or domain to an engagement or project only if the profile lists it for \
that same one (e.g. PyTorch belongs to Angel Studios, not linkage-engine).
- Never narrate your reasoning or assumptions ("I assume...", "so no questions there").
- Never mention rate, pay, salary, or compensation at all: no figures, no ranges, and no remarks such as \
"I can't speak to compensation" or "we can discuss rate later". Simply leave the topic out.
- Never mention citizenship, visas, work authorization, green card, sponsorship, or clearance, even if \
the recruiter asks.
- Never say "Spexture LLC", "sub-100ms response", "Cline", "Member Nav", or "Cambria".
- Engagement type: W2 is ideal; 1099 direct-to-individual is fine; he is not set up for C2C. Raise this \
only for staffing-agency contract roles where the engagement type is still unknown or C2C is mentioned.
- Location: 100% remote, or onsite/hybrid within about 15 minutes of Lehi, UT. These are all within \
commuting range, so onsite there is fine and never call the commute long: {nearby}. Anywhere else \
(including Salt Lake City and Provo) is outside it: don't call that commute easy; ask whether the role \
can be remote or hybrid.
- Plain text only. No subject line, no markdown, no placeholders or brackets.
- Start with exactly this greeting line: {greeting}
- End with exactly this sign-off:
{sign_off}
- Length: {length}.

Output only the message text.

=== Candidate profile (source of truth) ===
{profile}"""


REPLY, PACKAGE, FOLLOWUP = "reply", "package", "followup"

_TASKS = {
    REPLY: "Task: write Shawn's reply to the recruiter's latest message.",
    PACKAGE: (
        "Task: Shawn is sending his tailored résumé and cover letter for this role right now; they ARE "
        "attached, so say so. Write the short message that goes with them: thank the recruiter for what they "
        "last said, confirm strong interest, give 1-2 proof points, and ask them to submit him and propose a "
        "call this week. If the recruiter's latest message is more than two weeks old, acknowledge the slow "
        "reply in one short clause, without excuses."
    ),
    FOLLOWUP: (
        "Task: Shawn is following up because the recruiter hasn't answered his last message (see the thread "
        "dates). Write a short, friendly follow-up, not a repeat of his last message: restate interest in "
        "this specific role, add one proof point he hasn't already used in the thread, and ask for an update "
        "or the next step. If his résumé was already sent, don't offer it again. Never guilt-trip or mention "
        "how long it has been."
    ),
}
_FOLLOWUP_LENGTH = "under 90 words"


@dataclass
class ReplyContext:
    card_id: str
    company: str
    title: str
    channel: str
    recruiter_name: str
    thread: list[dict[str, str]]
    jd_text: str
    review: str
    rule_draft: str
    purpose: str = REPLY

    @property
    def is_linkedin(self) -> bool:
        return self.channel == "linkedin"


def enabled() -> bool:
    if os.environ.get("JOB_TRACKER_LLM_REPLY", "1").strip() in {"0", "false", "no", "off"}:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))  # pragma: allowlist secret


def _profile_text() -> str:
    from job_tracker.pipeline.llm_apply import _load_candidate_profile

    text = _load_candidate_profile()
    start = text.find("## 1.")
    end = text.find("## 14.")
    return text[start if start >= 0 else 0 : end if end > 0 else len(text)].strip()


def ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_reply_drafts (
            card_id TEXT PRIMARY KEY,
            input_hash TEXT NOT NULL,
            draft TEXT NOT NULL,
            model TEXT NOT NULL,
            cost_usd REAL,
            created_at TEXT NOT NULL
        )
        """
    )


def _purpose(item: dict) -> str | None:
    """Which message the card needs written, or None to keep the rule-based text."""
    if item.get("postDecline") or item.get("dealbreaker"):
        return None
    stage = item.get("stage")
    if stage == "clarify" and (item.get("draftReply") or "").strip():
        return REPLY
    if stage == "send_resume" and (item.get("draftReply") or "").strip():
        return PACKAGE
    if stage == "wait_schedule" and item.get("followUpDue") and (item.get("followupDraft") or "").strip():
        return FOLLOWUP
    return None


def _field(purpose: str) -> str:
    return "followupDraft" if purpose == FOLLOWUP else "draftReply"


def _card_id(item: dict, purpose: str = REPLY) -> str:
    if item.get("normalizedKey"):
        base = f"key:{item['normalizedKey']}"
    else:
        base = f"msg:{item.get('messageId') or item.get('replyId') or ''}"
    return base if purpose == REPLY else f"{purpose}:{base}"


def _recruiter_name(item: dict) -> str:
    names = [item.get("recruiterName") or "", ((item.get("replyPlan") or {}).get("recipient") or {}).get("name") or ""]
    names += [s.get("name") or "" for s in item.get("senders") or []]
    return next((n for n in names if n.strip() and "@" not in n), "")


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return text if len(text) <= limit else text[:limit] + "\n[...]"


def build_context(conn, item: dict, purpose: str = REPLY) -> ReplyContext:
    key = item.get("normalizedKey") or ""
    thread: list[dict[str, str]] = []
    jd_text = review = ""
    if key:
        from job_tracker.pipeline.store import list_job_conversations

        for c in list(list_job_conversations(conn, key))[-_THREAD_MESSAGES:]:
            thread.append(
                {
                    "direction": c["direction"] or "",
                    "channel": c["channel"] or "",
                    "when": c["occurred_at"] or "",
                    "subject": c["summary"] or "",
                    "body": _clip(c["body_text"] or "", _THREAD_CHARS),
                }
            )
        row = conn.execute(
            "SELECT jd_text, llm_verdict, llm_rationale, llm_framing_guidance FROM job_leads WHERE normalized_key = ?",
            (key,),
        ).fetchone()
        if row is not None:
            jd_text = _clip(row["jd_text"] or "", _JD_CHARS)
            if row["llm_verdict"]:
                review = _clip(
                    f"Verdict: {row['llm_verdict']}\n{row['llm_rationale'] or ''}\n"
                    f"Framing guidance: {row['llm_framing_guidance'] or ''}",
                    2_000,
                )
    if not any(t["body"] for t in thread if t["direction"] == "inbound"):
        thread.append(
            {
                "direction": "inbound",
                "channel": item.get("channel") or "",
                "when": "",
                "subject": item.get("subject") or "",
                "body": _clip(item.get("messageBody") or "", _THREAD_CHARS),
            }
        )
    return ReplyContext(
        card_id=_card_id(item, purpose),
        company=item.get("company") or "",
        title=item.get("title") or "",
        channel=(item.get("replyPlan") or {}).get("channel") or item.get("channel") or "email",
        recruiter_name=_recruiter_name(item),
        thread=thread,
        jd_text=jd_text,
        review=review,
        rule_draft=item.get(_field(purpose)) or "",
        purpose=purpose,
    )


NEARBY_TOWNS = (
    "Lehi, American Fork, Pleasant Grove, Lindon, Orem, Vineyard, Alpine, Highland, Cedar Hills, "
    "Saratoga Springs, Eagle Mountain, Bluffdale, Riverton, Herriman, Draper, South Jordan, West Jordan, Sandy"
)

_COMP_RE = re.compile(r"\b(compensation|salary|salaries|pay|hourly|rate)\b", re.I)
_AGENT_WRITTEN = (
    "linkage-engine|healthcare-agentic-snowflake-rag|sushi-rag|ecommerce-semantic-search|resume-parser|"
    "recruiting-automation|campaign asset automation|langgraph-mlops-app|workspace-spexture"
)
_AGENT_PROJECT_RE = re.compile(rf"\b({_AGENT_WRITTEN})\b", re.I)
_HANDS_ON_VERB_RE = re.compile(r"\b(built|wrote|written|implemented|coded)\b", re.I)
_CURRENT_JOB_RE = re.compile(
    r"\bI'?\s?a?m (an employee|employed) (of|by|at)\b|\bI (already )?work (for|at) (?!Spexture)"
    r"|\b(currently|already) (engaged|working|employed)\b",
    re.I,
)
_NARRATION_RE = re.compile(r"\bI assume\b|\bno questions there\b", re.I)
_PROPOSED_TIME_RE = re.compile(
    r"\b(I'?m|I am) (open|free|available)\b[^.\n]*\b(Monday|Tuesday|Wednesday|Thursday|Friday|morning|afternoon)\b"
    r"|\b(Monday|Tuesday|Wednesday|Thursday|Friday)\b[^.\n]*\b(\d{1,2}(:\d\d)?\s*(am|pm)|morning|afternoon)\b",
    re.I,
)
_ATTACHED_RE = re.compile(r"\b(attached|attaching)\b[^.\n]*r[ée]sum[ée]|r[ée]sum[ée][^.\n]*\b(attached|attaching)\b", re.I)
_ASKED_FOR_RESUME_RE = re.compile(r"r[ée]sum[ée]|\bCV\b", re.I)


def _overclaims_agent_work(text: str) -> bool:
    for clause in re.split(r"(?<=[.!?;,])\s+|\n|\s+(?:plus|and I|and)\s+", text):
        if _AGENT_PROJECT_RE.search(clause) and _HANDS_ON_VERB_RE.search(clause) and "direct" not in clause.lower():
            return True
    return False


def _system(ctx: ReplyContext, profile: str, *, dated: bool = True) -> str:
    return _SYSTEM.format(
        today=datetime.now().astimezone().strftime("%B %-d, %Y") if dated else "(date)",
        nearby=NEARBY_TOWNS,
        greeting=greeting_line(ctx.recruiter_name),
        sign_off=SIGN_OFF_LINKEDIN if ctx.is_linkedin else SIGN_OFF_EMAIL,
        length=_FOLLOWUP_LENGTH
        if ctx.purpose == FOLLOWUP
        else "under 120 words (LinkedIn message)"
        if ctx.is_linkedin
        else "80-180 words (email)",
        profile=profile,
    )


def _user(ctx: ReplyContext) -> str:
    parts = [
        _TASKS[ctx.purpose],
        "",
        f"Role: {ctx.title or 'unknown'}",
        f"Company / end client as tracked: {ctx.company or 'unknown'}",
        f"Recruiter: {ctx.recruiter_name or 'unknown'}",
        f"Reply channel: {ctx.channel}",
        "",
        "Thread, oldest first (inbound = from recruiter, outbound = Shawn):",
    ]
    for t in ctx.thread:
        parts.append(f"--- {t['direction']} {t['channel']} {t['when']} | {t['subject']}\n{t['body']}")
    parts += ["", "Job description on file:", ctx.jd_text or "(none beyond the thread)"]
    if ctx.review:
        parts += ["", "AI fit review of this lead:", ctx.review]
    parts += [
        "",
        "A rigid rule-based draft (for reference only: it may re-ask answered questions and is too generic; "
        "do not copy its wording):",
        ctx.rule_draft,
    ]
    return "\n".join(parts)


def input_hash(ctx: ReplyContext, profile: str) -> str:
    h = hashlib.sha256()
    for part in (PROMPT_VERSION, DEFAULT_REPLY_MODEL, profile, _system(ctx, "", dated=False), _user(ctx).split("A rigid rule-based")[0]):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _clean(text: str, ctx: ReplyContext) -> str:
    lines = [ln for ln in text.strip().splitlines() if not re.match(r"^\s*subject\s*:", ln, re.I)]
    text = "\n".join(lines).strip()
    greeting = greeting_line(ctx.recruiter_name)
    first, _, rest = text.partition("\n")
    if re.match(r"^(Hi|Hello|Dear|Hey)\b", first) and first.strip() != greeting:
        text = f"{greeting}\n{rest}"
    return text.strip()


def _violations(text: str, ctx: ReplyContext | None = None) -> list[str]:
    from job_tracker.pipeline.llm_apply import _check_house_rules_text

    problems = list(_check_house_rules_text(text))
    if _COMP_RE.search(text):
        problems.append("mentions rate/pay/compensation; leave the topic out entirely")
    if _overclaims_agent_work(text):
        problems.append(
            "says an AI-agent-written project was built/written/implemented; say he designed and directed it"
        )
    if _CURRENT_JOB_RE.search(text):
        problems.append("describes a current job or engagement; all engagements are in the past")
    if _NARRATION_RE.search(text):
        problems.append("narrates assumptions; drop that sentence")
    if _PROPOSED_TIME_RE.search(text):
        problems.append("proposes specific call days/times; ask the recruiter to send times instead")
    if ctx is not None and ctx.purpose != PACKAGE and _ATTACHED_RE.search(text):
        latest_inbound = next((t["body"] for t in reversed(ctx.thread) if t["direction"] == "inbound"), "")
        if not _ASKED_FOR_RESUME_RE.search(latest_inbound):
            problems.append("says the résumé is attached, but the recruiter didn't ask; offer to send it instead")
    return problems


def generate(ctx: ReplyContext, profile: str, *, client=None, model: str = DEFAULT_REPLY_MODEL) -> tuple[str, float | None]:
    """One reply draft. Raises on API failure or a house-rule violation that survives one retry."""
    from job_tracker.pipeline.llm_apply import _client, _cost_usd

    client = client or _client()
    system = [{"type": "text", "text": _system(ctx, profile), "cache_control": {"type": "ephemeral"}}]
    messages: list[dict[str, Any]] = [{"role": "user", "content": _user(ctx)}]
    total_cost = 0.0
    for attempt in range(2):
        start = time.monotonic()
        # Sonnet 5 thinks by default; 900 tokens went entirely to thinking.
        response = client.messages.create(model=model, max_tokens=6000, system=system, messages=messages)
        raw = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        usage = getattr(response, "usage", None)
        in_tok = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        cost = _cost_usd(model, in_tok + cached // 10, out_tok)
        total_cost += cost or 0.0
        log_call(step="reply_draft", model=model, input_tokens=in_tok + cached, output_tokens=out_tok, cost_usd=cost)
        logger.info("reply draft %s: %.1fs", ctx.card_id, time.monotonic() - start)
        text = _clean(raw, ctx)
        problems = _violations(text, ctx)
        if not problems and text:
            return text, total_cost
        if attempt == 0:
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "Rewrite it fixing: " + "; ".join(problems or ["empty reply"])},
            ]
    raise ValueError(f"reply draft for {ctx.card_id} broke house rules: {problems}")


def _cached(conn, card_id: str, digest: str) -> str | None:
    row = conn.execute(
        "SELECT draft FROM llm_reply_drafts WHERE card_id = ? AND input_hash = ?", (card_id, digest)
    ).fetchone()
    return row[0] if row else None


def _store(conn, card_id: str, digest: str, draft: str, cost: float | None) -> None:
    conn.execute(
        """
        INSERT INTO llm_reply_drafts (card_id, input_hash, draft, model, cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(card_id) DO UPDATE SET input_hash = excluded.input_hash, draft = excluded.draft,
            model = excluded.model, cost_usd = excluded.cost_usd, created_at = excluded.created_at
        """,
        (card_id, digest, draft, DEFAULT_REPLY_MODEL, cost, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def apply_llm_drafts(items: list[dict], conn, *, client=None, budget_s: float | None = None) -> int:
    """Replace each eligible card's rule-based message with an LLM-written one.

    Reply and Send-package cards get a new `draftReply`; follow-up-due Wait
    cards get a new `followupDraft`. Sets `draftSource` / `followupSource`
    ("llm" or "template") and keeps the rule-based text in `templateDraft` /
    `templateFollowup`. Returns how many cards got an LLM draft.
    """
    targets = [(i, p) for i in items if (p := _purpose(i))]
    for item in items:
        item.setdefault("draftSource", "template")
        if item.get("followupDraft"):
            item.setdefault("followupSource", "template")
    if not targets or not enabled():
        return 0
    try:
        profile = _profile_text()
    except Exception as exc:  # noqa: BLE001
        logger.warning("reply drafts skipped: %s", exc)
        return 0
    ensure_table(conn)
    pending: list[tuple[dict, ReplyContext, str]] = []
    done = 0
    for item, purpose in targets:
        ctx = build_context(conn, item, purpose)
        digest = input_hash(ctx, profile)
        hit = _cached(conn, ctx.card_id, digest)
        if hit:
            _use(item, hit, purpose)
            done += 1
        else:
            pending.append((item, ctx, digest))
    if not pending:
        return done
    pool = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
    futures = {pool.submit(generate, ctx, profile, client=client): (item, ctx, digest) for item, ctx, digest in pending}
    finished, _ = wait(futures, timeout=RENDER_BUDGET_S if budget_s is None else budget_s)
    pool.shutdown(wait=False, cancel_futures=True)
    for fut in finished:
        item, ctx, digest = futures[fut]
        try:
            draft, cost = fut.result()
        except Exception as exc:  # noqa: BLE001
            logger.warning("reply draft failed for %s: %s", ctx.card_id, exc)
            continue
        _store(conn, ctx.card_id, digest, draft, cost)
        _use(item, draft, ctx.purpose)
        done += 1
    return done


def _use(item: dict, draft: str, purpose: str = REPLY) -> None:
    if purpose == FOLLOWUP:
        item["templateFollowup"] = item.get("followupDraft") or ""
        item["followupDraft"] = draft
        item["followupSource"] = "llm"
        return
    item["templateDraft"] = item.get("draftReply") or ""
    item["draftReply"] = draft
    item["draftSource"] = "llm"
