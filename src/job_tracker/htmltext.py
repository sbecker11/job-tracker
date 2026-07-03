"""Shared HTML -> text conversion, structure-preserving.

Used both for ATS-resolved job descriptions (ats/jd_resolver.py) and as the
Gmail HTML-part fallback (email/gmail_reader.py) when a message has no
text/plain part.

Job descriptions in particular are semi-structured, not a flat blob: they
have headers, and bullet lists of responsibilities/requirements that carry
real signal (e.g. for future section-aware scoring, or just human
readability when reviewing a stored lead later). A naive tag-strip that
collapses everything to one line/space-run destroys that structure, so only
inline tags (<b>, <i>, <a>, <span>) are stripped in place; block-level tags
become line breaks instead.
"""

from __future__ import annotations

import html
import re

try:
    from bs4 import BeautifulSoup

    _HAS_BS4 = True
except ImportError:  # pragma: no cover
    _HAS_BS4 = False

# Dropped entirely, content included — raw CSS/JS has no place in extracted
# text and some senders' markup lets it leak straight into a message body.
_STYLE_OR_SCRIPT_BLOCK = re.compile(r"<(style|script)\b[^>]*>.*?</\1>", re.I | re.S)

_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|br|li|div|tr|h[1-6]|ul|ol|table|section|article|header|footer)[^>]*>",
    re.IGNORECASE,
)


def html_to_text(raw: str) -> str:
    """Convert (possibly entity-escaped) HTML into clean, semi-structured plain text.

    Only block-level tags introduce line breaks; inline tags are stripped
    without splitting the surrounding sentence. Blank-line runs are
    collapsed to a single blank line so paragraph/section breaks survive
    without leaving a wall of empty lines.
    """
    if not raw:
        return ""
    # Some sources (e.g. Greenhouse's API) return content as an
    # HTML-entity-escaped string; unescape before doing anything else.
    unescaped = html.unescape(raw)
    without_style_script = _STYLE_OR_SCRIPT_BLOCK.sub(" ", unescaped)
    blocked = _BLOCK_TAG_RE.sub("\n", without_style_script)
    if _HAS_BS4:
        text = BeautifulSoup(blocked, "html.parser").get_text("")
    else:
        text = re.sub(r"<[^>]+>", "", blocked)
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()
