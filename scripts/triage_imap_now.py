#!/usr/bin/env python3
"""One-click "process this Spexture IMAP message now" helper (2026-08-18)
for the Pending actions page's "Check inbox now" button.

The 3-minute comms_fast_cycle.py LaunchAgent tick already sweeps this
mailbox, but deliberately runs `triage_imap_inbox.py --offline
--no-generate` there — cheap, no live JD resolution, no LLM extraction
fallback, no package generation, just enough to park a stub lead quickly
(see that script's `run_steps`). The full treatment (live JD resolve, LLM
extraction fallback, LLM scoring, auto-generate on "pursue") otherwise only
happens on the next hourly `run_cycle.sh` tick.

This script runs that full treatment for whatever's new in the mailbox
right now, then regenerates pending-actions.json — for the case where you
want the real verdict (and package, if any) immediately, e.g. while still
on a call with the recruiter who just sent it.

Shares comms_fast_cycle.py's lock file (both write to the same leads.db
via IMAP reads that mutate folder state) so this never runs concurrently
with that 3-minute tick — waits briefly for it rather than failing outright,
since "click while on the call" is exactly the moment a background tick
might also be mid-run.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline import db_lock  # noqa: E402

REPO_ROOT = _REPO_ROOT
DEFAULT_DB = REPO_ROOT / "var" / "leads.db"
# Same lock file comms_fast_cycle.py uses — shared on purpose, not
# duplicated, so the two scripts serialize against each other rather than
# racing on the same mailbox/DB.
DEFAULT_LOCK = db_lock.DEFAULT_LOCK_PATH


def _venv_python() -> str:
    candidate = REPO_ROOT / ".venv" / "bin" / "python"
    return str(candidate) if candidate.is_file() else sys.executable


def _run(cmd: list[str], *, timeout: int) -> int:
    print(f"+ {' '.join(cmd)}", flush=True)
    try:
        return subprocess.run(cmd, cwd=str(REPO_ROOT), timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        print(f"! timed out after {timeout}s: {' '.join(cmd)}", file=sys.stderr, flush=True)
        return 124


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    ap.add_argument("--imap-prefix", default="SPEXTURE")
    ap.add_argument(
        "--wait-lock-seconds",
        type=float,
        default=90.0,
        help="If the comms_fast_cycle tick holds the lock, wait up to N seconds before giving up.",
    )
    args = ap.parse_args(argv)

    if not args.db.is_file():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    py = _venv_python()
    lock_fh = db_lock.acquire(args.lock, wait_seconds=args.wait_lock_seconds)
    if lock_fh is None:
        print("triage_imap_now: comms_fast tick still running — try again in a few seconds", flush=True)
        # Same sentinel exit code as comms_fast_cycle.py's own lock
        # timeout, so the native helper app can surface one consistent
        # "busy, try again" message for either script.
        return db_lock.LOCK_BUSY_EXIT_CODE

    try:
        # Full treatment: live JD resolution (no --offline), LLM extraction
        # fallback, package generation on "pursue" (no --no-generate) — the
        # same defaults the hourly run_cycle.sh tick uses for this mailbox.
        # No message cap / wall budget: this is a deliberate, one-off,
        # human-initiated check, not an unattended bounded sweep.
        rc = _run(
            [
                py,
                "scripts/triage_imap_inbox.py",
                "--db",
                str(args.db),
                "--imap-prefix",
                args.imap_prefix,
                "--llm-fallback",
            ],
            timeout=600,
        )
        if rc != 0:
            print(f"triage_imap_inbox.py exited {rc}", file=sys.stderr, flush=True)

        render_rc = _run(
            [py, "scripts/render_pending_actions.py", "--db", str(args.db), "--no-rescore"],
            timeout=90,
        )
        if render_rc != 0:
            print(f"render_pending_actions.py exited {render_rc}", file=sys.stderr, flush=True)
            return render_rc

        return rc
    finally:
        db_lock.release(lock_fh)


if __name__ == "__main__":
    raise SystemExit(main())
