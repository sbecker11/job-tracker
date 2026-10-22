"""ROUTE-5 page side — pending-actions-ui's `isPersonEmail` / `extractSenderDetails`
agree with `store.is_person_email`, so the page never offers a role mailbox."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from job_tracker.pipeline.store import is_person_email

LINKS_TS = Path(__file__).resolve().parents[1] / "pending-actions-ui" / "src" / "lib" / "links.ts"

ADDRESSES = [
    "priya.nair@acmestaffing.example",
    "karth@ngt.com",
    "careers@bigco.com",
    "hr@bigco.com",
    "jobs@bigco.com",
    "talent@bigco.com",
    "recruiting@agency.io",
    "no-reply@ats.example",
    "noreply@ats.example",
    "marketing@aws.example",
    "hit-reply@linkedin.com",
    "someone@e.linkedin.com",
    "alerts@talent.com",
    "x@greenhouse-mail.io",
    "x@ashbyhq.com",
    "list@googlegroups.com",
    "shawn.becker@spexture.com",
    "not-an-email",
]

_SCRIPT = """
import { isPersonEmail, extractSenderDetails } from %s
const addrs = JSON.parse(process.argv[1])
const sig = 'Apply at careers@bigco.com or hr@bigco.com.\\nThanks,\\nPriya Nair\\npriya.nair@acmestaffing.example.'
console.log(JSON.stringify({ verdicts: addrs.map(isPersonEmail), sig: extractSenderDetails(sig).email }))
"""


def _run_node() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    proc = subprocess.run(
        [node, "--input-type=module", "-e", _SCRIPT % json.dumps(LINKS_TS.as_uri()), json.dumps(ADDRESSES)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0 and "Unknown file extension" in proc.stderr:
        pytest.skip("this node can't import .ts directly")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_page_and_backend_agree_on_person_emails():
    out = _run_node()
    expected = [is_person_email(a) for a in ADDRESSES]
    mismatches = [a for a, js, py in zip(ADDRESSES, out["verdicts"], expected) if js != py]
    assert not mismatches, f"page and backend disagree on: {mismatches}"


def test_signature_skips_role_mailboxes():
    assert _run_node()["sig"] == "priya.nair@acmestaffing.example"
