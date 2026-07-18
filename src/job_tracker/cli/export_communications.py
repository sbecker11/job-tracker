"""CLI: render one job's full communications history (job_conversations) to
a single PDF under that job's folder.

Deliberately on-demand, not automatic (2026-07-17 design decision — see
chat history): every inbound/outbound message is already archived as text
in `job_conversations.body_text` the moment it's linked (by
`triage_recruiter_inbox.py` or `scan_communications.py`), which is cheap,
searchable, and needs no rendering step. A PDF is only generated when you
actually want a paper trail to hand someone — this command builds it fresh
from whatever's in the DB right now, every time; it does not accumulate
separate dated snapshots.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
)


def _sanitize(text: str) -> str:
    """fpdf2's core (non-TTF) fonts are Latin-1 only; recruiting mail
    regularly carries characters outside that range (curly quotes, emoji,
    the invisible tracking glyphs some ATS digests pad their text with).
    This is an internal archival document, not a polished deliverable, so
    losing an occasional unusual character to "?" is an acceptable trade
    for never crashing on real-world email content."""
    return (text or "").encode("latin-1", errors="replace").decode("latin-1")


def _render_pdf(job_key: str, company: str, title: str, conversations, contacts_by_id: dict, out_path: Path) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def line(text: str, *, font: str = "Helvetica", style: str = "", size: int = 10, height: float = 6) -> None:
        # fpdf2's multi_cell defaults to leaving the cursor at the RIGHT edge
        # of the text it just wrote (new_x=XPos.RIGHT) rather than back at
        # the left margin — harmless for a single call, but the SECOND
        # w=0 ("use remaining width") call then computes almost no width
        # left on the line and raises FPDFException. Forcing new_x back to
        # the left margin after every line is what makes repeated w=0
        # multi_cell calls (one per email field) actually work.
        pdf.set_font(font, style, size)
        pdf.multi_cell(0, height, _sanitize(text), new_x="LMARGIN", new_y="NEXT")

    line(f"{title} @ {company}", style="B", size=16, height=10)
    line(f"Communications history — {len(conversations)} entries — job_key: {job_key}", size=10)
    pdf.ln(4)

    for convo in conversations:
        contact = contacts_by_id.get(convo["contact_id"])
        contact_label = contact["email"] or contact["name"] if contact else "(no contact on file)"

        direction_label = "OUTBOUND (you wrote)" if convo["direction"] == "outbound" else "INBOUND (they wrote)"
        line(f"{convo['occurred_at']} — {direction_label} — {contact_label}", style="B", size=11)
        line(convo["summary"] or "(no summary)", style="I", size=10)

        body = (convo["body_text"] or "").strip()
        if body:
            line(body, size=9, height=5)
        pdf.ln(4)
        pdf.set_draw_color(200, 200, 200)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))


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
            print(f"No conversations logged yet for {args.title!r} @ {args.company!r} — nothing to export.")
            return 0

        contacts_by_id = {c["id"]: c for c in list_job_contacts(conn, job_key)}
        multi_lead = len(get_sibling_titles(conn, args.company, exclude_title=args.title)) > 0
        job_dir = _job_folder(args.output_root, company=args.company, title=args.title, multi_lead=multi_lead)
        out_path = job_dir / "communications" / f"Communications_{_safe_filename(args.title)}.pdf"

        _render_pdf(job_key, args.company, args.title, conversations, contacts_by_id, out_path)

        add_job_document(conn, JobDocument(job_key=job_key, doc_type="communications_export", path_or_url=str(out_path)))
        print(f"Exported {len(conversations)} conversation(s) to {out_path}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
