"""Best-effort extraction of a recruiter's real contact details (name,
email, phone) from the free-text signature block at the end of a LinkedIn
InMail message.

Background (2026-07-17): these details often sit right in the message
body, but nothing previously parsed them out into a JobContact -- every
InMail's `From:` header is a generic relay address
(`inmail-hit-reply@linkedin.com`), and `GENERIC_RELAY_ADDRESSES` correctly
refuses to store that as *the* contact, which meant the pipeline had no
automatic contact signal for LinkedIn traffic at all, even when a real
name/email/phone was sitting in the text.

Two independent signal sources, used together:
  1. LinkedIn's own template always renders a short "sender block" right
     after the subject-repeated preview text:
         <Name>
           Reply
           https://www.linkedin.com/messaging/thread/...
     `<Name>` is LinkedIn's own display name for the sender -- reliable,
     but sometimes truncated ("Manish K." instead of "Manish Khemnani") if
     the sender's own LinkedIn display name is itself abbreviated.
  2. A free-text signature block the recruiter typed themselves, which may
     include a fuller name plus an "Email:"/"Cell:"/"Desk:" line -- present
     in only a minority of messages (entirely at the recruiter's
     discretion), but a strictly better signal than #1 when present.

Deliberately does NOT attempt to recover a LinkedIn profile URL: the only
link in these messages is a private "reply to this thread" URL, not the
sender's public profile -- there's nothing to parse there.

Pure regex, no LLM call -- safe to run on every message for free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SENDER_BLOCK_RE = re.compile(
    r"\n\s*([A-Z][\w.'-]+(?: [A-Z][\w.'-]+){0,3})\n\s*Reply\n\s*(https://www\.linkedin\.com/messaging/thread/\S+)"
)
_EMAIL_LINE_RE = re.compile(r"(?:Email|E-mail)\s*[:\-]?\s*([\w.+\-]+@[\w\-]+\.[\w.\-]+)", re.I)
_PHONE_LINE_RE = re.compile(r"(?:Cell|Desk|Phone|Mobile|Direct|Tel)\s*[:\-]?\s*(\+?[\d][\d\s().\-]{6,17}\d)", re.I)
_FULL_NAME_LINE_RE = re.compile(r"^[A-Z][\w.'-]+(?: [A-Z][\w.'-]+){1,3}$")

# A plain "Firstname Lastname"-shaped line can just as easily be a job
# title or a company name ("Talent Acquisition Lead", "WaferWire Cloud
# Technologies") -- this blocklist keeps _nearby_full_name from mistaking
# either for the recruiter's actual name.
_TITLE_OR_COMPANY_WORDS = frozenset(
    {
        "lead", "specialist", "manager", "director", "recruiter", "recruiting",
        "acquisition", "talent", "engineer", "consultant", "president", "officer",
        "coordinator", "associate", "analyst", "executive", "staffing", "solutions",
        "technologies", "group", "global", "cloud", "inc", "llc", "corp",
        "corporation", "company", "partners", "regards", "sincerely", "thanks",
    }
)

# Both can legitimately appear in an archived message without being the
# recruiter's own address: the platform in footer boilerplate, Shawn's own
# address in the `To:` header baked into the combined text.
_EXCLUDED_EMAIL_DOMAINS = ("linkedin.com",)
_EXCLUDED_EMAILS = frozenset({"shawn.becker@spexture.com"})


@dataclass
class SignatureInfo:
    name: str = ""
    email: str = ""
    phone: str = ""

    def __bool__(self) -> bool:
        return bool(self.name or self.email or self.phone)


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _nearby_full_name(text: str, anchor_pos: int) -> str:
    """A recruiter's own typed signature ("Manish Khemnani | Centraprise
    Global") is usually fuller than LinkedIn's sender-block display name --
    look a few lines above an email/phone match for a plain 'Firstname
    Lastname' line, optionally followed by '| Company'. Signature blocks
    consistently put the name ABOVE the title/company/contact lines, so
    scan the window top-to-bottom (not nearest-first) and take the first
    hit that doesn't look like a title or company."""
    prefix = text[:anchor_pos]
    window = prefix.splitlines()[-8:]
    for line in window:
        candidate = line.strip().split("|")[0].strip()
        if not _FULL_NAME_LINE_RE.match(candidate):
            continue
        words = {w.lower() for w in candidate.split()}
        if words & _TITLE_OR_COMPANY_WORDS:
            continue
        return candidate
    return ""


def parse_signature(text: str) -> SignatureInfo:
    """Returns an empty (falsy) SignatureInfo if nothing usable was found --
    callers should treat that the same as "no signature," not an error."""
    if not text:
        return SignatureInfo()
    normalized = _normalize(text)

    name = ""
    sender_match = _SENDER_BLOCK_RE.search(normalized)
    if sender_match:
        name = sender_match.group(1).strip()

    email = ""
    for match in _EMAIL_LINE_RE.finditer(normalized):
        candidate = match.group(1).strip().lower()
        if candidate in _EXCLUDED_EMAILS or any(candidate.endswith(f"@{d}") for d in _EXCLUDED_EMAIL_DOMAINS):
            continue
        email = match.group(1).strip()
        name = _nearby_full_name(normalized, match.start()) or name
        break  # first non-excluded hit is the recruiter's own sign-off

    phone = ""
    phone_match = _PHONE_LINE_RE.search(normalized)
    if phone_match:
        phone = phone_match.group(1).strip()
        if not email:
            name = _nearby_full_name(normalized, phone_match.start()) or name

    return SignatureInfo(name=name, email=email, phone=phone)
