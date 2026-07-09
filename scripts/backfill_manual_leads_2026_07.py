#!/usr/bin/env python3
"""One-off backfill (2026-07-09): ingest the job-search backlog the user
tracked manually (by hand, and via Claude on Chrome for a few packages)
during the ~24h shawnbecker.recruiting@gmail.com was suspended by Google and
the pipeline couldn't run. Sources:

  1. The user's own hand-kept status list (dates, role/company, contact,
     outcome) covering 18 applications from 6/2 through 7/10.
  2. Six ~/Desktop/Resumes/2026/<Company>/ folders containing résumés/cover
     letters/JDs generated manually outside the pipeline (Aligner[r], ZimZee,
     META, PostSilo, Hyatt, Orion).

Not meant to be re-run as a general tool — company/title/contact/status/date
values are hardcoded from that one-time review. Safe to re-run though:
upsert_lead/add_job_contact/add_job_document are all idempotent-ish (a
second run just refreshes last_seen/last_contacted_at and adds a harmless
duplicate document/conversation row rather than corrupting anything), but
there's no reason to.
"""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline import store
from job_tracker.pipeline.models import JobContact, JobConversation, JobDocument, JobLead, normalize_key

DESKTOP = Path.home() / "Desktop" / "Resumes" / "2026"

# --- status -> LEAD_STAGES mapping -----------------------------------------
# "skipped" is LEAD_STAGES' explicit off-ramp for "decided not to pursue (or
# rejected)" — covers rejections and reqs that quietly stopped hiring.
_STATUS_STAGE = {
    "rejected": "skipped",
    "rejected_unqualified": "skipped",
    "not_hiring": "skipped",
    "still_waiting": "applied",
    "interviewing": "interviewing",
}


def _ingest(
    conn,
    *,
    company: str,
    title: str,
    status: str,
    when: str,
    contact_name: str = "",
    contact_email: str = "",
    contact_role: str = "recruiter",
    note: str = "",
    apply_url: str = "",
    jd_text: str = "",
    documents: list[tuple[str, Path]] | None = None,
) -> str:
    stage = _STATUS_STAGE[status]
    lead = JobLead(
        company=company,
        title=title,
        source_message_id=f"manual-backfill-{company.lower().replace(' ', '-')}",
        source_label="manual",
        apply_url=apply_url,
        jd_source="manual" if jd_text else "",
        jd_text=jd_text,
        verdict="pursue",  # the user already applied — clearly a "pursue" by definition
    )
    key = lead.normalized_key
    store.upsert_lead(conn, lead)
    store.advance_status(conn, key, stage, when=when)

    if contact_name or contact_email:
        contact_id = store.add_job_contact(
            conn,
            JobContact(job_key=key, name=contact_name, email=contact_email, role=contact_role),
        )
    else:
        contact_id = None

    summary = f"status: {status}" + (f" — {note}" if note else "")
    store.add_job_conversation(
        conn,
        JobConversation(
            job_key=key,
            contact_id=contact_id,
            direction="inbound" if status != "applied" else "outbound",
            summary=summary,
            occurred_at=when,
        ),
    )

    for doc_type, path in documents or []:
        if path.exists():
            store.add_job_document(
                conn, JobDocument(job_key=key, doc_type=doc_type, path_or_url=str(path))
            )

    return key


def main() -> None:
    conn = store.connect()

    # --- 1. The user's hand-kept status list (6/2 - 7/10) -------------------
    _ingest(
        conn,
        company="People Inc.",
        title="Senior Data Engineer",
        status="rejected",
        when="2026-06-02",
        contact_email="No-Reply-WD@people.inc",
    )
    _ingest(
        conn,
        company="Zions Bank",
        title="Backend Integration Engineer",
        status="still_waiting",
        when="2026-06-02",
        contact_name="Mark (Red Group Recruiting)",
        contact_email="mark@redgrouprecruiting.com",
        note="via Red Group Recruiting, agency placement",
    )
    _ingest(
        conn,
        company="FiSec Global Inc.",
        title="Backend/Cloud Engineer (Healthcare)",
        status="still_waiting",
        when="2026-06-02",
        contact_email="vinayv@fisecglobal.net",
    )
    _ingest(
        conn,
        company="Genesis 10",
        title="Senior Data Engineer",
        status="still_waiting",
        when="2026-06-02",
        contact_email="24dba1c8-aba5-4f7c-afda-cc5c6670d5dd@reply.linkedin.com",
        note="applied via LinkedIn relay address",
    )
    _ingest(
        conn,
        company="HAN IT Staffing Inc.",
        title="AI/ML Software Engineer",
        status="still_waiting",
        when="2026-06-05",
        contact_name="Rishabh Pandey",
        contact_email="rishabhpandey@hanstaffing.com",
    )
    _ingest(
        conn,
        company="Illumination Works",
        title="Senior Full-Stack Engineer",
        status="rejected",
        when="2026-06-12",
        contact_name="Danielle Hurt",
        contact_email="linkedin.com/in/danielle-hurt-191a4625b",
        note="contact identified by LinkedIn profile, not email",
    )
    _ingest(
        conn,
        company="NetDocuments",
        title="Data Architect",
        status="rejected",
        when="2026-06-14",
        contact_email="no-reply@us.greenhouse-mail.io",
    )
    _ingest(
        conn,
        company="Nextiva",
        title="Data Engineer",
        status="still_waiting",
        when="2026-06-17",
        contact_name="Jose Cortes",
        contact_email="jose.cortes@nextiva.com",
    )
    _ingest(
        conn,
        company="Alignerr",
        title="Full-Stack Software Engineer",
        status="still_waiting",
        when="2026-06-22",
        contact_name="Darius Thomas",
        contact_email="darius.thomas@usealignerrskill.com",
        apply_url="https://app.alignerr.com/signup",
        documents=[
            ("resume", DESKTOP / "Aligner" / "Shawn_Becker_Resume_Senior_SWE.docx"),
        ],
    )
    _ingest(
        conn,
        company="LDS Service Mission Office",
        title="Missionary Experience Group Lead",
        status="rejected",
        when="2026-06-22",
        contact_name="Marsha Ard",
        contact_email="linkedin.com/in/marsha-ard-9a7594251",
        contact_role="hiring_manager",
    )
    _ingest(
        conn,
        company="Mainz Brady Group",
        title="Data Engineer, Agentic AI",
        status="not_hiring",
        when="2026-06-23",
        contact_name="E. Florido",
        contact_email="eflorido@mbg.com",
        note="req closed / not actually hiring",
    )
    # PostSilo: two list rows (7/1 first contact, 7/10 interview scheduled)
    # are the same job — one lead, two conversation entries.
    postsilo_key = _ingest(
        conn,
        company="PostSilo",
        title="Founding Engineer, Data & Retrieval",
        status="interviewing",
        when="2026-07-01",
        contact_name="Andrew Case",
        contact_email="andrew@dfir.org",
        contact_role="hiring_manager",
        note="applied 7/1",
        documents=[
            ("resume", DESKTOP / "PostSilo" / "Shawn_Becker_Resume_PostSilo_Data_and_Retrieval.docx"),
            ("jd_snapshot", DESKTOP / "PostSilo" / "postsilo-founding-engineer-data-retrieval.pdf"),
            ("nda", DESKTOP / "PostSilo" / "PostSilo-Inc-NDA-June-2026-signed.docx"),
        ],
    )
    store.add_job_conversation(
        conn,
        JobConversation(
            job_key=postsilo_key,
            direction="inbound",
            summary="interview scheduled for 7/10",
            occurred_at="2026-07-10",
        ),
    )
    _ingest(
        conn,
        company="Meta",
        title="Data Engineer",
        status="still_waiting",
        when="2026-07-08",
        contact_name="Prisha Singh",
        contact_email="linkedin.com/in/prisha-singh-102457257",
        note="contract role via Allegis Group / TEK Systems",
        documents=[
            ("resume", DESKTOP / "META" / "Shawn_Becker_Resume_Meta_Data_Engineer.docx"),
            ("cover_letter", DESKTOP / "META" / "Shawn_Becker_Cover_Letter_Meta_Data_Engineer.docx"),
        ],
    )
    _ingest(
        conn,
        company="DiverseLynx",
        title="Technical Lead - Salesforce Financial Services Cloud (FSC)",
        status="rejected_unqualified",
        when="2026-07-08",
        contact_name="Deepak Kumar",
        contact_email="deepak.kumar@diverselynx.com",
    )
    _ingest(
        conn,
        company="Equal Experts",
        title="Data Engineer",
        status="interviewing",
        when="2026-07-09",
        contact_name="Brandon Cinquegrana",
        contact_email="brandon.cinquegrana@equalexperts.com",
    )
    _ingest(
        conn,
        company="ZimZee Recruiting",
        title="Data Engineer",
        status="interviewing",
        when="2026-07-09",
        contact_name="Jim Niemela",
        contact_email="jim@zimzeerecruiting.com",
        note="placing for an unnamed medical device company client in Lehi, UT",
        documents=[
            ("cover_letter", DESKTOP / "ZimZee" / "Shawn-Becker-Cover-Letter-Data-Engineer.docx"),
        ],
    )
    _ingest(
        conn,
        company="Quantum World Technologies",
        title="NodeJS AWS Application Lead or Architect",
        status="interviewing",
        when="2026-07-10",
        contact_name="Sachin Radhad",
    )

    # --- 2. Manual-folder packages not on the hand-kept list ---------------
    # Hyatt: package generated 7/8 but never appears on the user's own
    # application-status list — treated as drafted-but-not-yet-sent rather
    # than assumed "applied".
    hyatt_lead = JobLead(
        company="Hyatt",
        title="Machine Learning Engineer",
        source_message_id="manual-backfill-hyatt",
        source_label="manual",
    )
    store.upsert_lead(conn, hyatt_lead)
    hyatt_key = normalize_key("Hyatt", "Machine Learning Engineer")
    store.advance_status(conn, hyatt_key, "package_generated", when="2026-07-08")
    for doc_type, path in [
        ("resume", DESKTOP / "Hyatt" / "Shawn_Becker_Resume_Hyatt_ML_Engineer.docx"),
        ("cover_letter", DESKTOP / "Hyatt" / "Shawn_Becker_Cover_Letter_Hyatt_ML_Engineer.docx"),
    ]:
        if path.exists():
            store.add_job_document(conn, JobDocument(job_key=hyatt_key, doc_type=doc_type, path_or_url=str(path)))

    # Orion Advisor Solutions: both roles are stale (Feb/Mar 2026 files, not
    # on the current hand-kept list) — added as historical/backdated leads
    # per explicit user decision (2.A), not treated as currently active.
    _ingest(
        conn,
        company="Orion Advisor Solutions",
        title="AI Engineer",
        status="still_waiting",
        when="2026-03-10",
        documents=[
            ("resume", DESKTOP / "Orion" / "Shawn_Becker_Resume_Orion.docx"),
            ("cover_letter", DESKTOP / "Orion" / "Shawn_Becker_CoverLetter_Orion.docx"),
            ("jd_snapshot", DESKTOP / "Orion" / "AI Engineer - Orion.pdf"),
        ],
    )
    _ingest(
        conn,
        company="Orion Advisor Solutions",
        title="Principal Software Engineer (IC6)",
        status="still_waiting",
        when="2026-02-06",
        documents=[
            ("cover_letter", DESKTOP / "Orion" / "Shawn_Becker_Orion_Cover_Letter.docx"),
        ],
    )

    conn.close()
    print("Backfill complete.")


if __name__ == "__main__":
    main()
