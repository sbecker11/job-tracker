"""One reply plan per Pending Actions card (REQUIREMENTS ROUTE-1).

The page renders `item["replyPlan"]` and never re-derives the channel, the
recipient, or which routes exist. Route order follows ROUTE-2; ROUTE-3 keeps
LinkedIn conversations on LinkedIn routes; CON-3 keeps office lines
call-only.
"""

from __future__ import annotations

import re

from job_tracker.pipeline.signature import parse_signature
from job_tracker.pipeline.store import is_person_email

_OWN_PHONE_DIGITS = "8578910896"
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_PROFILE_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.I)
_OFFICE_LABEL_RE = re.compile(r"(?i)\b(office|main|desk|fax|switchboard|tel)\b[^0-9\n]{0,6}$")
_OFFICE_SUFFIX_RE = re.compile(r"(?i)^\s*\(?\s*(office|main|desk|fax|switchboard)\b")
_SIGNOFF_CHARS = 600


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _own_phone(phone: str) -> bool:
    return _digits(phone).endswith(_OWN_PHONE_DIGITS)


def _person(email: str) -> str:
    e = (email or "").strip().rstrip(".,;")
    return e if is_person_email(e) else ""


def _signoff_details(body: str) -> tuple[str, str, str]:
    """(email, phone, LinkedIn profile) from the sender's sign-off — never from
    an address quoted higher up in a JD (ROUTE-5)."""
    if not body:
        return "", "", ""
    sig = parse_signature(body)
    tail = body[-_SIGNOFF_CHARS:]
    email = _person(sig.email) or next(
        (e for e in (_person(m) for m in reversed(_EMAIL_RE.findall(tail))) if e), ""
    )
    phone = sig.phone if sig.phone and not _own_phone(sig.phone) else ""
    if not phone:
        phone = next((p for p in _PHONE_RE.findall(tail) if not _own_phone(p)), "")
    profile = (_PROFILE_RE.findall(body) or [""])[0]
    return email, phone.strip(), profile


def is_office_line(phone: str, body: str = "") -> bool:
    """CON-3: a number labelled office / main / desk / fax is callable, not textable."""
    first = _PHONE_RE.search(phone or "")
    if first and _OFFICE_SUFFIX_RE.search((phone or "")[first.end():]):
        return True
    digits = _digits(phone)[-10:]
    if len(digits) < 10 or not body:
        return False
    for m in _PHONE_RE.finditer(body):
        if _digits(m.group(0))[-10:] == digits and _OFFICE_LABEL_RE.search(body[max(0, m.start() - 30) : m.start()]):
            return True
    return False


def _first_phone(phone: str) -> str:
    m = _PHONE_RE.search(phone or "")
    return m.group(0).strip() if m else (phone or "").strip()


def reply_plan(item: dict) -> dict:
    """Channel, recipient and ordered routes for one card. Route kinds:
    compose (Gmail compose to a real recruiter email), thread (LinkedIn or email
    thread link), profile (LinkedIn profile), notification (LinkedIn's
    notification email, only when nothing else reaches them), gmail_thread
    (the recruiter's own email thread), company (company inbox, only when no
    recruiter owns the lead), text / call (phone)."""
    body = item.get("messageBody") or ""
    sig_email, sig_phone, sig_profile = _signoff_details(body)

    lead_email = "" if item.get("emailIsLinkedInRelay") else _person(item.get("recruiterEmail") or "")
    email = lead_email or _person(item.get("resolvedEmail") or "") or sig_email
    via = "" if lead_email else (item.get("resolvedVia") or ("their signature" if email else ""))
    thread = (item.get("threadUrl") or item.get("resolvedThreadUrl") or "").strip()
    profile = (item.get("recruiterLinkedinUrl") or item.get("resolvedLinkedinUrl") or sig_profile or "").strip()
    phone_field = (item.get("recruiterPhone") or "").strip()
    phone = _first_phone(phone_field) if phone_field and not _own_phone(phone_field) else sig_phone
    gmail_url = (item.get("gmailUrl") or "").strip()
    name = (item.get("recruiterName") or "").strip()

    if item.get("channel") == "linkedin" or (item.get("emailIsLinkedInRelay") and not email):
        channel = "linkedin"
    elif item.get("channel") in ("email", "sms"):
        channel = item["channel"]
    else:
        channel = "email" if email else "other"
    linkedin_only = channel == "linkedin" and not email

    routes: list[dict] = []
    if email:
        routes.append({"kind": "compose", "channel": "email", "to": email})
    if thread:
        li = "linkedin.com" in thread.lower()
        routes.append({"kind": "thread", "channel": "linkedin" if li else channel, "to": thread, "linkedin": li})
    if profile:
        routes.append({"kind": "profile", "channel": "linkedin", "to": profile})
    if gmail_url and linkedin_only and not routes:
        routes.append({"kind": "notification", "channel": "linkedin", "to": gmail_url})
    if gmail_url and not linkedin_only:
        routes.append({"kind": "gmail_thread", "channel": "email", "to": gmail_url})
    company_email = (item.get("companyEmail") or "").strip()
    if company_email and not item.get("hasRecruiter"):
        routes.append(
            {
                "kind": "company",
                "channel": "email",
                "to": company_email,
                "name": (item.get("companyContactName") or "").strip(),
            }
        )
    if phone:
        office = is_office_line(phone_field or phone, body)
        routes.append({"kind": "call" if office else "text", "channel": "other", "to": phone})

    subject = " ".join(p for p in (item.get("company") or "", item.get("title") or "") if p)
    return {
        "channel": channel,
        "recipient": {
            "name": name,
            "email": email,
            "phone": phone,
            "threadUrl": thread,
            "linkedinUrl": profile,
            "via": via,
        },
        "subject": f"RE: {subject}",
        "hasRecruiter": bool(name or item.get("recruiterEmail") or thread),
        "recruiterOwned": bool(item.get("hasRecruiter")),
        "routes": routes,
    }
