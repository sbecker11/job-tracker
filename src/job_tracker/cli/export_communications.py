"""CLI: render one job's full communications history (job_conversations) to
a single ODT under that job's folder.

Deliberately on-demand, not automatic (2026-07-17 design decision — see
chat history): every inbound/outbound message is already archived as text
in `job_conversations.body_text` the moment it's linked (by
`triage_recruiter_inbox.py` or `scan_communications.py`), which is cheap,
searchable, and needs no rendering step. An ODT is only generated when you
actually want a paper trail to hand someone — this command builds it fresh
from whatever's in the DB right now, every time; it does not accumulate
separate dated snapshots.

ODT (not PDF) so the thread opens in Pages / LibreOffice / Word with normal
dark-mode reading, copy/paste, and editing — Preview's PDF dark mode is
chrome-only.

Every export (CLI, Find Mail, and ``viewcomms://``) collapses IMAP+Gmail
twins after stripping LinkedIn notification chrome, then replaces later
copies of the same cleaned body with a pointer to the first instance.
Non-human mailer tracking (pixel padding, ``midToken`` / ``lipi`` /
unsubscribe URLs, encoded LinkedIn thread links) is diverted to a sidecar
``.log`` next to the ODT.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import zipfile
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from xml.sax.saxutils import escape

from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _job_folder, _safe_filename
from job_tracker.pipeline.models import JobDocument
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    add_job_document,
    connect,
    get_job,
    get_sibling_titles,
    list_job_contacts,
    list_job_conversations,
    list_job_documents,
)

# Not listed in the "Documents on file" section — it's this export itself,
# and listing it would just be the file linking to its own prior versions.
_DOC_TYPE_SELF = "communications_export"

# IMAP vs Gmail copies of the same message can land minutes apart.
_DUP_WINDOW_SECONDS = 30 * 60
_DUP_BODY_RATIO = 0.82

_SUBJECT_PREFIX_RE = re.compile(r"^(re|fw|fwd):\s*", re.I)
_QUOTE_RE = re.compile(
    r"(?is)"
    r"(?:\nOn\s+\w.{8,400}?wrote:\s*\n)"
    r"|(?:\nFrom:\s+.+\nSent:\s+)"
    r"|(?:\n-----Original Message-----)"
    r"|(?:\n_{5,}\s*\n)"
)
_BOILERPLATE_RE = re.compile(
    r"(?is)"
    r"(?:This e-mail is intended solely for the person)"
    r"|(?:Caution:\s*The sender of this message is external)"
    r"|(?:8\(A\) Certified Business)"
    r"|(?:Arthur Grand is an Equal Opportunity Employer)"
    r"|(?:The information contained in this (?:e-?mail|message))"
)
_SIGNATURE_RE = re.compile(
    r"(?is)\n(?:Thanks(?:\s*&\s*Regards)?|Best(?:\s*regards)?|Regards|Sincerely)\s*,?\s*\n"
    r".{0,80}(?:\|.{0,80})?(?:@|\+1\s*\d|\bInc\.|\bLLC\b)"
)
_MAILTO_RE = re.compile(r"<mailto:[^>]+>", re.I)
_WRAPPED_URL_RE = re.compile(r"<(https?://[^>]+)>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_INVISIBLE_RE = re.compile(r"[\u00ad\u034f\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]|͏")
_LINKEDIN_FOOTER_RE = re.compile(
    r"(?:\n[-–—]{5,}\s*\nThis email was intended for\b.*"
    r"|\nThis email was intended for\b.*)",
    re.I | re.S,
)
_LINKEDIN_CHROME_LINE_RE = re.compile(
    r"(?im)^\s*(?:InMail:\s*You have a new message|Reply|Message replied:.*|"
    r"You are receiving LinkedIn notification emails\.?)\s*$"
)
_MAIL_LIKE_CHANNELS = frozenset({"email", "linkedin"})
_DUP_POINTER_MIN_CHARS = 150
_DUP_POINTER_RATIO = 0.82
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_TRACKING_URL_HINTS = (
    "trackingid=",
    "midtoken=",
    "otptoken=",
    "trkemail=",
    "lipi=",
    "loid=",
    "midsig=",
    "trk=",
    "&eid=",
    "?eid=",
    "emailunsub",
    "/emimp/",
    "/li/track",
    "textfooterglimmer",
    "linkedin.com/messaging/thread/",
    "calendar.google.com/calendar/appointments/booking/",
)
_CALENDAR_MARKER_RE = re.compile(r"(?m)^\s*~~//~~\s*$")
_DOC_TYPES_HIDDEN = frozenset({_DOC_TYPE_SELF, "communications_tracking_log"})


def _row_get(row, key: str, default: str = "") -> str:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = None
    if value is None:
        return default
    return str(value)


def _fold_subject(summary: str) -> str:
    text = re.sub(r"\s+", " ", summary or "").strip().lower()
    for _ in range(3):
        nxt = _SUBJECT_PREFIX_RE.sub("", text)
        if nxt == text:
            break
        text = nxt.strip()
    return text


def _parse_when(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_linkedin_notification_chrome(text: str) -> str:
    """IMAP/Gmail LinkedIn copies sandwich the real InMail in notification chrome."""
    text = _INVISIBLE_RE.sub("", text or "")
    text = _LINKEDIN_FOOTER_RE.sub("", text)
    hopes = list(re.finditer(r"(?im)^I hope you are well\b.*", text))
    if hopes:
        first_line = hopes[0].group(0).strip()
        # Gmail often prefixes a truncated snippet ending in "...".
        if first_line.endswith("...") and len(hopes) > 1:
            text = text[hopes[-1].start() :]
        else:
            text = text[hopes[0].start() :]
    else:
        thread = re.search(r"linkedin\.com/messaging/thread/\S+", text)
        if thread:
            prefix = text[: thread.start()]
            rest = text[thread.end() :].lstrip(" \t\n>")
            chromeish = bool(
                _LINKEDIN_CHROME_LINE_RE.search(prefix)
                or re.search(r"(?im)^\s*Reply\s*$", prefix)
            )
            if chromeish and len(rest) > 80:
                text = rest
    kept: list[str] = []
    prev_blank = False
    for line in text.split("\n"):
        if _LINKEDIN_CHROME_LINE_RE.match(line):
            continue
        if re.match(r"(?i)^\s*https://www\.linkedin\.com/messaging/thread/\S+\s*$", line):
            continue
        blank = not line.strip()
        if blank and prev_blank:
            continue
        kept.append(line)
        prev_blank = blank
    text = "\n".join(kept).strip()
    text = re.sub(r"(?m)^(.+\n.+\n)\1+", r"\1", text)
    text = re.sub(r"(?m)^(.+\n.+)\n+\1(?:\n|$)", r"\1\n", text)
    return text.strip()


def _is_tracking_url(url: str) -> bool:
    low = (url or "").lower()
    return any(hint in low for hint in _TRACKING_URL_HINTS)


def _human_url(url: str) -> str | None:
    """Keep a readable URL in the ODT, or None if the whole URL belongs in the log."""
    raw = (url or "").rstrip(").,;")
    low = raw.lower()
    if "meet.google.com/" in low or "://tel.meet/" in low:
        return raw.split("?", 1)[0]
    if _is_tracking_url(raw):
        return None
    return raw


def collect_tracking_log_lines(raw: str) -> list[str]:
    """Classify non-human mailer tracking in the stored body (export-only)."""
    lines: list[str] = []
    n_inv = len(_INVISIBLE_RE.findall(raw or ""))
    if n_inv:
        lines.append(f"invisible_padding_chars: {n_inv}")
    seen: set[str] = set()
    for url in _URL_RE.findall(raw or ""):
        url = url.rstrip(").,;")
        if url in seen:
            continue
        low = url.lower()
        if ("meet.google.com/" in low or "://tel.meet/" in low) and "?" in url:
            seen.add(url)
            lines.append(f"meet_tracking_url: {url}")
            continue
        if not _is_tracking_url(url):
            continue
        seen.add(url)
        if "messaging/thread/" in low:
            kind = "linkedin_thread"
        elif "appointments/booking/" in low:
            kind = "calendar_booking"
        else:
            kind = "tracking_url"
        lines.append(f"{kind}: {url}")
    return lines


def _divert_tracking(text: str) -> str:
    """Strip tracking URLs/markers from already-cleaned body text."""

    def _replace(match: re.Match[str]) -> str:
        kept = _human_url(match.group(0))
        return kept or ""

    text = _URL_RE.sub(_replace, text or "")
    text = _CALENDAR_MARKER_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def write_tracking_log(
    log_path: Path,
    *,
    company: str,
    title: str,
    job_key: str,
    sections: list[tuple[str, str, str, list[str]]],
) -> None:
    """Sidecar log next to the ODT: tracking that was removed from the readable copy."""
    lines = [
        f"# Communications tracking log — {title} @ {company}",
        f"# job_key: {job_key}",
        "# Stored job_conversations.body_text is unchanged; this file holds mailer",
        "# tracking (pixel padding, LinkedIn midToken/lipi URLs, encoded thread links)",
        "# diverted out of the human-readable ODT.",
        "",
    ]
    wrote = False
    for when, summary, message_id, items in sections:
        if not items:
            continue
        wrote = True
        lines.append(f"## {when} — {summary or '(no summary)'}")
        if message_id:
            lines.append(f"message_id: {message_id}")
        lines.extend(items)
        lines.append("")
    if not wrote:
        lines.append("No non-human tracking data extracted.")
        lines.append("")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines), encoding="utf-8")


def _drop_duplicate_snippet(text: str) -> str:
    """Gmail often stores a one-line plain snippet, then the same email again."""
    stripped = (text or "").strip()
    if "\n" not in stripped:
        return stripped
    first, rest = stripped.split("\n", 1)
    rest = rest.lstrip("\n")
    first_flat = re.sub(r"\s+", " ", first).strip()
    rest_flat = re.sub(r"\s+", " ", rest).strip()
    if len(first_flat) > 40 and rest_flat.startswith(first_flat[:50]):
        return rest.strip()
    parts = re.split(r"\n\s*\n", stripped, maxsplit=1)
    if len(parts) == 2:
        head_flat = re.sub(r"\s+", " ", parts[0]).strip()
        tail_flat = re.sub(r"\s+", " ", parts[1]).strip()
        if len(head_flat) > 40 and tail_flat.startswith(head_flat[:50]):
            return parts[1].strip()
    return stripped


def clean_body_for_history(body: str, *, summary: str = "") -> str:
    """Drop quoted history, vendor footers, and HTML/mailto wrapping.

    Stored `job_conversations.body_text` is left untouched — this is
    export-only so the ODT is readable without deleting the archive.
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = html.unescape(text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    text = _MAILTO_RE.sub("", text)
    text = _WRAPPED_URL_RE.sub(r"\1", text)
    text = _strip_linkedin_notification_chrome(text)
    folded_summary = (summary or "").strip()
    if folded_summary and text.startswith(folded_summary):
        text = text[len(folded_summary) :].lstrip("\n")
    first, rest = (text.split("\n", 1) + [""])[:2]
    if folded_summary and first.strip() == folded_summary:
        text = rest.lstrip("\n")
    text = _drop_duplicate_snippet(text)
    quote = _QUOTE_RE.search("\n" + text)
    if quote:
        text = text[: max(0, quote.start() - 1)].rstrip()
    boiler = _BOILERPLATE_RE.search(text)
    if boiler:
        text = text[: boiler.start()].rstrip()
    text = re.split(r"(?is)\n?Thanks\s*&\s*Regards\b", text, maxsplit=1)[0].rstrip()
    sig = _SIGNATURE_RE.search(text)
    if sig:
        closing = re.match(
            r"(?i)\n?(Thanks(?:\s*&\s*Regards)?|Best(?:\s*regards)?|Regards|Sincerely)\s*,?",
            sig.group(0),
        )
        kept = closing.group(0).strip() if closing else ""
        text = text[: sig.start()].rstrip()
        if kept:
            text = f"{text}\n\n{kept}".strip()
    text = _HTML_TAG_RE.sub("", text)
    text = _divert_tracking(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _body_fingerprint(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text or "").strip().lower()
    return collapsed[:800]


def _mail_like(row) -> bool:
    return (_row_get(row, "channel") or "email").lower() in _MAIL_LIKE_CHANNELS


def _near_duplicate(a, b) -> bool:
    if not _mail_like(a) or not _mail_like(b):
        return False
    if _fold_subject(_row_get(a, "summary")) != _fold_subject(_row_get(b, "summary")):
        return False
    same_direction = _row_get(a, "direction") == _row_get(b, "direction")
    ta, tb = _parse_when(_row_get(a, "occurred_at")), _parse_when(_row_get(b, "occurred_at"))
    close = bool(ta and tb and abs((ta - tb).total_seconds()) <= _DUP_WINDOW_SECONDS)
    fa = _body_fingerprint(clean_body_for_history(_row_get(a, "body_text"), summary=_row_get(a, "summary")))
    fb = _body_fingerprint(clean_body_for_history(_row_get(b, "body_text"), summary=_row_get(b, "summary")))
    if not fa and not fb:
        return close
    if not fa or not fb:
        return close and same_direction
    similar = SequenceMatcher(None, fa, fb).ratio() >= _DUP_BODY_RATIO
    if not similar and fa and fb:
        shorter, longer = (fa, fb) if len(fa) <= len(fb) else (fb, fa)
        similar = len(shorter) >= 80 and shorter[:120] in longer
    return close and similar


def _prefer_copy(a, b):
    """Keep the copy with usable body; prefer Gmail hex ids over imap:."""
    ca = clean_body_for_history(_row_get(a, "body_text"), summary=_row_get(a, "summary"))
    cb = clean_body_for_history(_row_get(b, "body_text"), summary=_row_get(b, "summary"))
    if bool(ca) != bool(cb):
        return b if cb else a
    a_imap = _row_get(a, "message_id").startswith("imap:")
    b_imap = _row_get(b, "message_id").startswith("imap:")
    if a_imap != b_imap and abs(len(cb) - len(ca)) < 80:
        return a if not a_imap else b
    if len(cb) != len(ca):
        return b if len(cb) > len(ca) else a
    if a_imap != b_imap:
        return a if not a_imap else b
    return a


def collapse_conversations_for_history(rows: list) -> list:
    """One row per real message: IMAP+Gmail twins and send+inbox copies drop out."""
    kept: list = []
    for row in rows:
        if not _mail_like(row):
            kept.append(row)
            continue
        for i, prev in enumerate(kept):
            if _near_duplicate(prev, row):
                kept[i] = _prefer_copy(prev, row)
                break
        else:
            kept.append(row)
    return kept


def omit_empty_email_husk(row) -> bool:
    """Drop quote-only email copies whose cleaned body is empty and the
    summary is just a mail subject, not a logged note."""
    if (_row_get(row, "channel") or "email") != "email":
        return False
    summary = _row_get(row, "summary").strip()
    cleaned = clean_body_for_history(_row_get(row, "body_text"), summary=summary)
    if cleaned:
        return False
    if summary.startswith("Inbound ") or "Shawn " in summary:
        return False
    return len(summary) < 80


_JD_START_RE = re.compile(r"(?im)^(?:Job role:|Job Description:)\s")
_JD_POINTER = "see first JD above (or below)"
_JD_MIN_CHARS = 400
_JD_DUP_RATIO = 0.75


def _fold_jd(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def replace_duplicate_job_descriptions(bodies: list[str]) -> list[str]:
    """Keep the first pasted JD; later copies become a pointer to it."""
    first_fold = ""
    out: list[str] = []
    for body in bodies:
        match = _JD_START_RE.search(body or "")
        if not match:
            out.append(body)
            continue
        jd = (body[match.start() :] or "").rstrip()
        if len(jd) < _JD_MIN_CHARS:
            out.append(body)
            continue
        folded = _fold_jd(jd)
        if not first_fold:
            first_fold = folded
            out.append(body)
            continue
        if SequenceMatcher(None, folded[:2000], first_fold[:2000]).ratio() >= _JD_DUP_RATIO:
            prefix = (body[: match.start()] or "").rstrip()
            out.append(f"{prefix}\n\n{_JD_POINTER}".strip() if prefix else _JD_POINTER)
        else:
            out.append(body)
    return out


def replace_duplicate_bodies(
    bodies: list[str], *, occurred_at: list[str], summaries: list[str]
) -> list[str]:
    """Later copies of the same cleaned body become a pointer to the first."""
    first_fold: list[tuple[str, str, str]] = []
    out: list[str] = []
    for body, when, summary in zip(bodies, occurred_at, summaries, strict=True):
        folded = re.sub(r"\s+", " ", body or "").strip().lower()
        if len(folded) < _DUP_POINTER_MIN_CHARS:
            out.append(body)
            continue
        hit = None
        for prev_fold, prev_when, prev_summary in first_fold:
            ratio = SequenceMatcher(None, folded[:2000], prev_fold[:2000]).ratio()
            contained = folded[:160] in prev_fold or prev_fold[:160] in folded
            if ratio >= _DUP_POINTER_RATIO or contained:
                hit = (prev_when, prev_summary)
                break
        if hit is None:
            first_fold.append((folded, when, summary))
            out.append(body)
            continue
        prev_when, prev_summary = hit
        label = re.sub(r"\s+", " ", prev_summary or "").strip() or "(no summary)"
        out.append(f"see first instance above ({prev_when} — {label})")
    return out


def _xml_text(text: str) -> str:
    """Escape XML specials; map bare newlines to ODF soft line-breaks."""
    # escape() leaves newlines alone; soft-break each one so multi-line
    # email bodies stay readable inside a single text:p.
    return escape(text or "").replace("\r\n", "\n").replace("\r", "\n").replace(
        "\n", "<text:line-break/>"
    )


def _p(text: str, *, style: str | None = None) -> str:
    style_attr = f' text:style-name="{style}"' if style else ""
    return f"<text:p{style_attr}>{_xml_text(text)}</text:p>"


def _render_odt(
    job_key: str,
    company: str,
    title: str,
    conversations,
    contacts_by_id: dict,
    documents,
    out_path: Path,
) -> None:
    """Write a minimal ODF Text document (no odfpy — stdlib zip + XML)."""
    log_name = f"Communications_{_safe_filename(title)}.log"
    parts: list[str] = [
        _p(f"{title} @ {company}", style="Title"),
        _p(
            f"Communications history — {len(conversations)} entries — job_key: {job_key}",
            style="Subtitle",
        ),
        _p(
            f"Non-human mailer tracking was moved to {log_name}.",
            style="Subtitle",
        ),
        _p(""),
    ]

    docs = [d for d in documents if d["doc_type"] not in _DOC_TYPES_HIDDEN]
    if docs:
        parts.append(_p("Documents on file", style="Heading"))
        for doc in docs:
            parts.append(
                _p(
                    f"{doc['doc_type']} (v{doc['version']}), attached {doc['created_at']}: "
                    f"{doc['path_or_url']}",
                    style="Summary",
                )
            )
        parts.append(_p(""))

    tracking_sections: list[tuple[str, str, str, list[str]]] = []
    cleaned_bodies = []
    for convo in conversations:
        raw = _row_get(convo, "body_text")
        summary = _row_get(convo, "summary")
        tracking_sections.append(
            (
                _row_get(convo, "occurred_at"),
                summary,
                _row_get(convo, "message_id"),
                collect_tracking_log_lines(raw),
            )
        )
        cleaned_bodies.append(clean_body_for_history(raw, summary=summary))
    cleaned_bodies = replace_duplicate_job_descriptions(cleaned_bodies)
    cleaned_bodies = replace_duplicate_bodies(
        cleaned_bodies,
        occurred_at=[_row_get(c, "occurred_at") for c in conversations],
        summaries=[_row_get(c, "summary") for c in conversations],
    )

    for convo, body in zip(conversations, cleaned_bodies, strict=True):
        contact = contacts_by_id.get(convo["contact_id"])
        contact_label = (
            contact["email"] or contact["name"] if contact else "(no contact on file)"
        )
        direction_label = (
            "OUTBOUND (you wrote)"
            if convo["direction"] == "outbound"
            else "INBOUND (they wrote)"
        )
        summary = _row_get(convo, "summary") or "(no summary)"
        parts.append(
            _p(
                f"{convo['occurred_at']} — {direction_label} — {contact_label}",
                style="Heading",
            )
        )
        parts.append(_p(summary, style="Summary"))
        if body and body.strip() != summary.strip():
            parts.append(_p(body, style="Body"))
        parts.append(_p(""))  # spacer between messages

    content_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
        'office:version="1.2">\n'
        "<office:automatic-styles>\n"
        '<style:style style:name="Title" style:family="paragraph">'
        '<style:text-properties fo:font-size="16pt" fo:font-weight="bold"/>'
        "</style:style>\n"
        '<style:style style:name="Subtitle" style:family="paragraph">'
        '<style:text-properties fo:font-size="10pt"/>'
        "</style:style>\n"
        '<style:style style:name="Heading" style:family="paragraph">'
        '<style:text-properties fo:font-size="11pt" fo:font-weight="bold"/>'
        "</style:style>\n"
        '<style:style style:name="Summary" style:family="paragraph">'
        '<style:text-properties fo:font-size="10pt" fo:font-style="italic"/>'
        "</style:style>\n"
        '<style:style style:name="Body" style:family="paragraph">'
        '<style:text-properties fo:font-size="9pt" style:font-name="Courier New"/>'
        "</style:style>\n"
        "</office:automatic-styles>\n"
        "<office:body><office:text>\n"
        + "\n".join(parts)
        + "\n</office:text></office:body>\n"
        "</office:document-content>\n"
    )

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'office:version="1.2">\n'
        "<office:styles/>\n"
        "</office:document-styles>\n"
    )

    meta_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'office:version="1.2">\n'
        "<office:meta>"
        f"<dc:title>{escape(f'{title} @ {company} — communications')}</dc:title>"
        "</office:meta>\n"
        "</office:document-meta>\n"
    )

    manifest_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.2">\n'
        '<manifest:file-entry manifest:full-path="/" '
        'manifest:media-type="application/vnd.oasis.opendocument.text"/>\n'
        '<manifest:file-entry manifest:full-path="content.xml" '
        'manifest:media-type="text/xml"/>\n'
        '<manifest:file-entry manifest:full-path="styles.xml" '
        'manifest:media-type="text/xml"/>\n'
        '<manifest:file-entry manifest:full-path="meta.xml" '
        'manifest:media-type="text/xml"/>\n'
        "</manifest:manifest>\n"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_tracking_log(
        out_path.with_suffix(".log"),
        company=company,
        title=title,
        job_key=job_key,
        sections=tracking_sections,
    )
    # ODF requires `mimetype` as the first zip entry, stored (not deflated).
    with zipfile.ZipFile(out_path, "w") as zf:
        zf.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/manifest.xml", manifest_xml)
        zf.writestr("content.xml", content_xml)
        zf.writestr("styles.xml", styles_xml)
        zf.writestr("meta.xml", meta_xml)


def export_job_history(
    *,
    company: str,
    title: str,
    db_path: Path = DEFAULT_DB_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict:
    """Write the communications ODT and return a JSON-serializable result.

    Does not open the file — the URL-scheme helper and the local HTTP API
    decide whether to `open` it.
    """
    company = (company or "").strip()
    title = (title or "").strip()
    if not company or not title:
        return {"ok": False, "error": "company and title are required"}
    conn = connect(db_path)
    try:
        job = get_job(conn, company, title)
        if job is None:
            return {"ok": False, "error": "not found", "company": company, "title": title}
        job_key = job["normalized_key"]
        raw_conversations = list_job_conversations(conn, job_key)
        if not raw_conversations:
            return {"ok": False, "error": "no conversations", "company": company, "title": title}
        conversations = [
            row
            for row in collapse_conversations_for_history(raw_conversations)
            if not omit_empty_email_husk(row)
        ]
        contacts_by_id = {c["id"]: c for c in list_job_contacts(conn, job_key)}
        documents = list_job_documents(conn, job_key)
        multi_lead = len(get_sibling_titles(conn, company, exclude_title=title)) > 0
        job_dir = _job_folder(output_root, company=company, title=title, multi_lead=multi_lead)
        out_path = job_dir / "communications" / f"Communications_{_safe_filename(title)}.odt"
        _render_odt(job_key, company, title, conversations, contacts_by_id, documents, out_path)
        add_job_document(
            conn,
            JobDocument(job_key=job_key, doc_type="communications_export", path_or_url=str(out_path)),
        )
        return {
            "ok": True,
            "path": str(out_path),
            "log_path": str(out_path.with_suffix(".log")),
            "count": len(conversations),
            "raw_count": len(raw_conversations),
            "company": company,
            "title": title,
        }
    finally:
        conn.close()


def discover_on_disk_communications(output_root: Path) -> list[Path]:
    """Every Communications_*.odt under a job's communications/ folder."""
    root = Path(output_root)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.rglob("Communications_*.odt"):
        if path.name.startswith(".") or ".~lock" in path.name:
            continue
        if path.parent.name != "communications":
            continue
        found.append(path)
    return sorted(found)


def _lead_folder_index(conn) -> dict[tuple[str, str], tuple[str, str]]:
    index: dict[tuple[str, str], tuple[str, str]] = {}
    for row in conn.execute("SELECT company, title FROM job_leads"):
        index[(_safe_filename(row["company"]), _safe_filename(row["title"]))] = (
            row["company"],
            row["title"],
        )
    return index


def _heading_from_odt(path: Path) -> tuple[str, str] | None:
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("content.xml").decode("utf-8", errors="ignore")
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    text = re.sub(r"<text:line-break/>", " ", xml)
    text = re.sub(r"<[^>]+>", "", text)
    for line in text.splitlines():
        line = html.unescape(line).strip()
        if " @ " not in line:
            continue
        title, company = line.split(" @ ", 1)
        title, company = title.strip(), company.strip()
        if title and company:
            return title, company
    return None


def resolve_odt_lead(
    path: Path, *, index: dict[tuple[str, str], tuple[str, str]]
) -> tuple[str, str] | None:
    """Map an on-disk ODT back to job_leads company/title."""
    if path.parent.name == "communications":
        title_folder = path.parent.parent.name
        company_folder = path.parent.parent.parent.name
        hit = index.get((company_folder, title_folder))
        if hit:
            return hit
    heading = _heading_from_odt(path)
    if heading:
        title, company = heading
        hit = index.get((_safe_filename(company), _safe_filename(title)))
        if hit:
            return hit
        return company, title
    return None


def export_on_disk_histories(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict:
    """Re-export every on-disk communications ODT with the current processing."""
    conn = connect(db_path)
    try:
        index = _lead_folder_index(conn)
    finally:
        conn.close()
    paths = discover_on_disk_communications(output_root)
    results: list[dict] = []
    for path in paths:
        lead = resolve_odt_lead(path, index=index)
        if lead is None:
            results.append(
                {"ok": False, "error": "unresolved", "path": str(path), "company": "", "title": ""}
            )
            continue
        company, title = lead
        result = export_job_history(
            company=company, title=title, db_path=db_path, output_root=output_root
        )
        result.setdefault("path", str(path))
        results.append(result)
    ok = bool(paths) and all(r.get("ok") for r in results)
    return {
        "ok": ok,
        "found": len(paths),
        "exported": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "results": results,
    }


def _print_one_export(result: dict) -> int:
    if not result.get("ok"):
        err = result.get("error") or "export failed"
        company, title = result.get("company") or "", result.get("title") or ""
        if err == "unresolved":
            print(f"Could not match on-disk ODT to a lead: {result.get('path')}", file=sys.stderr)
            return 1
        if err == "not found":
            print(f"No job found for {title!r} @ {company!r}.", file=sys.stderr)
            return 1
        print(f"No conversations logged yet for {title!r} @ {company!r} — nothing to export.")
        return 0
    n = result["count"]
    raw = result["raw_count"]
    print(
        f"Exported {n} conversation(s)"
        + (f" (collapsed from {raw})" if n != raw else "")
        + f" to {result['path']}"
        + (f" (tracking log: {result['log_path']})" if result.get("log_path") else "")
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--company")
    ap.add_argument("--title")
    ap.add_argument(
        "--all-on-disk",
        action="store_true",
        help="Re-export every Communications_*.odt under --output-root.",
    )
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = ap.parse_args(argv)

    if args.all_on_disk:
        if args.company or args.title:
            print("--all-on-disk cannot be combined with --company/--title.", file=sys.stderr)
            return 2
        batch = export_on_disk_histories(db_path=args.db, output_root=args.output_root)
        if batch["found"] == 0:
            print(f"No communications ODTs found under {args.output_root}.")
            return 0
        rc = 0
        for result in batch["results"]:
            if _print_one_export(result) != 0:
                rc = 1
        print(
            f"Batch: {batch['exported']} exported, {batch['failed']} failed "
            f"({batch['found']} on-disk ODT(s))."
        )
        return rc

    if not args.company or not args.title:
        print("Provide --company and --title, or --all-on-disk.", file=sys.stderr)
        return 2
    return _print_one_export(
        export_job_history(
            company=args.company, title=args.title, db_path=args.db, output_root=args.output_root
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
