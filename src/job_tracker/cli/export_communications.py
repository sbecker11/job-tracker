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
"""

from __future__ import annotations

import argparse
import sys
import zipfile
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
    parts: list[str] = [
        _p(f"{title} @ {company}", style="Title"),
        _p(
            f"Communications history — {len(conversations)} entries — job_key: {job_key}",
            style="Subtitle",
        ),
        _p(""),
    ]

    docs = [d for d in documents if d["doc_type"] != _DOC_TYPE_SELF]
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

    for convo in conversations:
        contact = contacts_by_id.get(convo["contact_id"])
        contact_label = (
            contact["email"] or contact["name"] if contact else "(no contact on file)"
        )
        direction_label = (
            "OUTBOUND (you wrote)"
            if convo["direction"] == "outbound"
            else "INBOUND (they wrote)"
        )
        parts.append(
            _p(
                f"{convo['occurred_at']} — {direction_label} — {contact_label}",
                style="Heading",
            )
        )
        parts.append(_p(convo["summary"] or "(no summary)", style="Summary"))
        body = (convo["body_text"] or "").strip()
        if body:
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--company", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        job = get_job(conn, args.company, args.title)
        if job is None:
            print(f"No job found for {args.title!r} @ {args.company!r}.", file=sys.stderr)
            return 1
        job_key = job["normalized_key"]

        conversations = list_job_conversations(conn, job_key)
        if not conversations:
            print(
                f"No conversations logged yet for {args.title!r} @ {args.company!r} — nothing to export."
            )
            return 0

        contacts_by_id = {c["id"]: c for c in list_job_contacts(conn, job_key)}
        documents = list_job_documents(conn, job_key)
        multi_lead = len(get_sibling_titles(conn, args.company, exclude_title=args.title)) > 0
        job_dir = _job_folder(
            args.output_root, company=args.company, title=args.title, multi_lead=multi_lead
        )
        out_path = job_dir / "communications" / f"Communications_{_safe_filename(args.title)}.odt"

        _render_odt(job_key, args.company, args.title, conversations, contacts_by_id, documents, out_path)

        add_job_document(
            conn,
            JobDocument(job_key=job_key, doc_type="communications_export", path_or_url=str(out_path)),
        )
        print(f"Exported {len(conversations)} conversation(s) to {out_path}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
