"""Phase 5 — draft market-withdrawal notes when a job is accepted (never auto-send).

  python scripts/market_withdrawal_draft.py --accepted-company Acme --accepted-title "Sr Eng"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, list_job_contacts, list_leads

_DRAFT = """Hi {recruiter_name},

Thank you for keeping me in mind for the {title} role at {company}. I've accepted another offer and am stepping out of active interviews for now. I'd welcome staying in touch for future opportunities.

Best,
Shawn
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--accepted-company", required=True)
    ap.add_argument("--accepted-title", required=True)
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    drafts: list[tuple[str, str, str]] = []
    try:
        terminal = {"skipped", "rejected", "deleted", "unavailable", "hired", "accepted", "duplicate"}
        for lead in list_leads(conn):
            d = dict(lead)
            if (d.get("company") or "").lower() == args.accepted_company.lower() and (
                d.get("title") or ""
            ).lower() == args.accepted_title.lower():
                continue
            if (d.get("status") or "") in terminal:
                continue
            key = d["normalized_key"]
            contacts = [dict(c) for c in list_job_contacts(conn, key)]
            recruiter = contacts[0].get("name") if contacts else "there"
            email = contacts[0].get("email") if contacts else ""
            body = _DRAFT.format(
                recruiter_name=recruiter,
                company=d.get("company") or "?",
                title=d.get("title") or "?",
            )
            drafts.append((f"{d.get('company')} / {d.get('title')}", email or "?", body))
    finally:
        conn.close()

    out_dir = args.out_dir or Path.home() / "Desktop" / "Resumes" / "2026" / "_withdrawal_drafts"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Market withdrawal drafts ({len(drafts)} active jobs) ===")
    print("Review each draft before sending — pipeline never auto-sends.\n")
    for idx, (label, email, body) in enumerate(drafts, 1):
        path = out_dir / f"withdraw_{idx:02d}.txt"
        header = f"To: {email}\nRe: {label}\n\n"
        path.write_text(header + body, encoding="utf-8")
        print(f"--- {label} → {path}")
        print(body)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
