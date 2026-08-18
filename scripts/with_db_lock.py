#!/usr/bin/env python3
"""Flock-wrapper for bash callers: acquire the shared leads.db lock file,
run the given command, then release (2026-08-18).

Why this exists: `comms_fast_cycle.py` (the 3-minute LaunchAgent tick) and
`triage_imap_now.py` (the "Check inbox now" button) both already acquire
the shared lock (see job_tracker.pipeline.db_lock) before touching
leads.db — but the hourly `run_cycle.sh` (a zsh script) never did, since
zsh has no built-in equivalent and macOS ships no `flock(1)` binary. That
gap let the hourly cycle's `triage_recruiter_inbox` step collide with a
fast tick's write on 2026-08-18: one `conn.commit()` lost the SQLite
busy-timeout race and HALTed the whole schedule
(`sqlite3.OperationalError: database is locked`). `store.connect()` also
picked up a WAL + 30s-timeout fix the same day as a defense-in-depth
backstop, but this closes the actual gap by giving run_cycle.sh a way to
join the same lock.

IMPORTANT: do not add this same lock acquisition *inside* the individual
job-tracker CLI scripts (triage_recruiter_inbox.py etc.) — comms_fast_cycle.py
already holds this lock in its own process while it calls those scripts as
subprocesses with --offline/--no-generate; a script that also tried to
acquire the lock for itself would deadlock waiting on a lock its own parent
already holds. This wrapper is only for callers (like run_cycle.sh) that
invoke those scripts *without* already holding the lock themselves.

Usage:
  with_db_lock.py --lock PATH [--wait-seconds N] -- CMD [ARGS...]

Forwards SIGTERM/SIGINT to the wrapped command (so run_cycle.sh's
per-step `timeout` kill still reaches the real work) and propagates its
exit code. Exits 75 (job_tracker.pipeline.db_lock.LOCK_BUSY_EXIT_CODE,
the same "someone else has the lock" sentinel comms_fast_cycle.py /
triage_imap_now.py use) if the lock isn't free within --wait-seconds.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline import db_lock  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lock", type=Path, required=True)
    ap.add_argument(
        "--wait-seconds",
        type=float,
        default=180.0,
        help="Wait up to N seconds for the lock (default covers one full comms_fast_cycle.py tick).",
    )
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="-- CMD [ARGS...]")
    args = ap.parse_args(argv)

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("with_db_lock: no command given after --", file=sys.stderr)
        return 2

    lock_fh = db_lock.acquire(args.lock, wait_seconds=args.wait_seconds)
    if lock_fh is None:
        print(
            f"with_db_lock: could not acquire {args.lock} within {args.wait_seconds}s "
            "(comms_fast_cycle tick still running?)",
            file=sys.stderr,
        )
        return db_lock.LOCK_BUSY_EXIT_CODE

    try:
        proc = subprocess.Popen(cmd)
    except Exception as exc:
        print(f"with_db_lock: failed to launch {cmd!r}: {exc}", file=sys.stderr)
        db_lock.release(lock_fh)
        return 1

    def _forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    try:
        return proc.wait()
    finally:
        db_lock.release(lock_fh)


if __name__ == "__main__":
    raise SystemExit(main())
