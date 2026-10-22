"""Sanity checks over every Pending Actions card after a regenerate.

Each check flags a card state that has already reached Shawn as a bug — an
email route on a LinkedIn message, a nameless greeting, a draft re-asking
something the recruiter answered — so problems surface in the regenerate
output instead of on the page.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from job_tracker.pipeline.store import is_person_email, is_same_inbound_message

CHECKS: dict[str, str] = {
    "email_on_linkedin": "LinkedIn message shown as an email card",
    "missing_thread": "LinkedIn card with no thread link, though the message has one",
    "nameless_greeting": "Draft greets no one by name",
    "email_as_name": "Recruiter name is an email address",
    "bad_email": "Recruiter email isn't an email address",
    "text_to_office_line": "Text route offered for an office / main / desk number",
    "reasks_answered": "Draft asks something the recruiter already answered",
    "first_touch_on_followup": "Draft thanks them for reaching out, but you've already replied",
    "duplicate_inbound": "Same inbound message logged more than once",
    "two_stages": "Same lead shown in more than one stage",
}

_EMAIL_RE = re.compile(r"^[\w.+'-]+@[\w-]+(?:\.[\w-]+)+$")
_LI_THREAD_RE = re.compile(r"linkedin\.com/(?:comm/)?messaging/thread/", re.I)
_OFFICE_LABEL_RE = re.compile(r"(?i)\b(office|main|desk|fax|switchboard|tel)\b[^0-9\n]{0,6}$")
_ANSWER_PATTERNS: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {
    "remote": (
        re.compile(r"(?i)fully remote|onsite/hybrid"),
        re.compile(r"(?i)\b(remote|on-?site|hybrid|in[- ]office)\b"),
    ),
    "engagement": (
        re.compile(r"(?i)W2 or 1099"),
        re.compile(r"(?i)\b(w-?2|1099|c2c|corp[- ]to[- ]corp|full[- ]time|fte|permanent)\b"),
    ),
    "rate": (
        re.compile(r"(?i)rate or salary band"),
        re.compile(r"(?i)\$\s?\d|\b\d+\s?(?:/\s?hr|per hour|k\b)|\bsalary\b"),
    ),
}


@dataclass
class CardIssue:
    check: str
    card: str
    detail: str


def _digits(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else d


def _card_label(item: dict) -> str:
    who = item.get("recruiterName") or ""
    what = " — ".join(x for x in (item.get("company"), item.get("title")) if x) or item.get("subject") or ""
    return f"{what} ({who})" if who and what else what or who or item.get("messageId") or "?"


def iter_cards(payload: dict):
    seen: set[str] = set()
    for items in (payload.get("stages") or {}).values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("id") or item.get("replyId") or item.get("normalizedKey") or item.get("messageId") or ""
            if key and key in seen:
                continue
            seen.add(key)
            yield item


def _stage_cards(payload: dict):
    for stage, items in (payload.get("stages") or {}).items():
        buckets = items.values() if isinstance(items, dict) else [items]
        for bucket in buckets:
            for item in bucket if isinstance(bucket, list) else []:
                if isinstance(item, dict):
                    yield stage, item


def _office_number(item: dict) -> bool:
    phone = _digits(item.get("recruiterPhone") or "")
    body = item.get("messageBody") or ""
    if len(phone) < 10 or not body:
        return False
    for m in re.finditer(r"\+?\d[\d\s().-]{8,}\d", body):
        if _digits(m.group(0)) == phone and _OFFICE_LABEL_RE.search(body[max(0, m.start() - 30) : m.start()]):
            return True
    return False


def _conversations(conn: sqlite3.Connection | None, key: str) -> list[sqlite3.Row]:
    if conn is None or not key:
        return []
    return list(
        conn.execute(
            "SELECT direction, occurred_at, message_id, summary, body_text, coalesce(body_text, summary, '') AS text "
            "FROM job_conversations WHERE job_key = ? ORDER BY occurred_at",
            (key,),
        )
    )



def _duplicate_inbound(convs: list[sqlite3.Row]) -> str:
    inbound = [c for c in convs if c["direction"] == "inbound" and c["occurred_at"]]
    for i, a in enumerate(inbound):
        for b in inbound[i + 1 :]:
            if (a["message_id"] and a["message_id"] == b["message_id"]) or is_same_inbound_message(
                (a["summary"], a["body_text"], a["occurred_at"]), (b["summary"], b["body_text"], b["occurred_at"])
            ):
                return f"two copies at {a['occurred_at'][:16]}"
    return ""


def audit_cards(payload: dict, conn: sqlite3.Connection | None = None) -> list[CardIssue]:
    issues: list[CardIssue] = []

    def flag(check: str, item: dict, detail: str = "") -> None:
        issues.append(CardIssue(check, _card_label(item), detail))

    stages_by_key: dict[str, list[str]] = {}
    first_card: dict[str, dict] = {}
    for stage, item in _stage_cards(payload):
        key = item.get("normalizedKey") or ""
        if key:
            stages_by_key.setdefault(key, []).append(stage)
            first_card.setdefault(key, item)
    for key, stages in stages_by_key.items():
        if len(stages) > 1:
            flag("two_stages", first_card[key], ", ".join(stages))

    for item in iter_cards(payload):
        body = item.get("messageBody") or ""
        draft = item.get("draftReply") or ""
        thread = item.get("threadUrl") or item.get("resolvedThreadUrl") or ""
        name = item.get("recruiterName") or ""
        email = (item.get("recruiterEmail") or "").strip()
        real_email = is_person_email(email)
        is_li = bool(item.get("emailIsLinkedInRelay") or _LI_THREAD_RE.search(body) or _LI_THREAD_RE.search(thread))

        if is_li and not real_email and item.get("channel") not in ("linkedin", "sms"):
            flag("email_on_linkedin", item, f"channel={item.get('channel')}")
        if item.get("channel") == "linkedin" and not thread and _LI_THREAD_RE.search(body):
            flag("missing_thread", item)
        if "@" in name:
            flag("email_as_name", item, name)
        if email and not _EMAIL_RE.match(email):
            flag("bad_email", item, email)
        if draft and (draft.startswith("Hi,") or re.match(r"Hi [^,\n]*@", draft)):
            flag("nameless_greeting", item, draft.splitlines()[0])
        if _office_number(item):
            flag("text_to_office_line", item, item.get("recruiterPhone") or "")

        if not item.get("normalizedKey"):
            continue
        convs = _conversations(conn, item["normalizedKey"])
        dup = _duplicate_inbound(convs)
        if dup:
            flag("duplicate_inbound", item, dup)
        if not draft or item.get("stage") != "clarify":
            continue
        if "Thanks for reaching out" in draft and any(c["direction"] == "outbound" for c in convs):
            flag("first_touch_on_followup", item)
        inbound = [c["text"] for c in convs if c["direction"] == "inbound"]
        replies = "\n".join(inbound[1:])
        if replies:
            answered = [k for k, (ask, ans) in _ANSWER_PATTERNS.items() if ask.search(draft) and ans.search(replies)]
            if answered:
                flag("reasks_answered", item, ", ".join(answered))
    return issues


def summarize(issues: list[CardIssue]) -> str:
    if not issues:
        return "Card check: no problems found."
    lines = [f"Card check: {len(issues)} problem(s)."]
    for check, label in CHECKS.items():
        hits = [i for i in issues if i.check == check]
        if not hits:
            continue
        lines.append(f"  {label} ({len(hits)}):")
        for i in hits:
            lines.append(f"    - {i.card}" + (f"  [{i.detail}]" if i.detail else ""))
    return "\n".join(lines)


def issues_json(issues: list[CardIssue]) -> list[dict]:
    return [asdict(i) | {"label": CHECKS[i.check]} for i in issues]
