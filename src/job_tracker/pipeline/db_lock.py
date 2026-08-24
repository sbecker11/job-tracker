"""Shared advisory lock serializing writes to leads.db across the
independent processes that touch it: the hourly recruiting-automation
run_cycle.sh (via scripts/with_db_lock.py — zsh has no flock, so it shells
out to a tiny wrapper that imports this module), the 3-minute
comms_fast_cycle.py LaunchAgent tick, and the "Check inbox now" button
(scripts/triage_imap_now.py).

One canonical implementation (2026-08-18) — this ~15-line acquire/wait/
release dance was previously copy-pasted into each of those three call
sites independently, discovered right after a real HALT: the hourly
cycle's triage_recruiter_inbox step and the 3-minute tick both wrote to
leads.db with no coordination, and one commit() lost the SQLite
busy-timeout race (see store.connect()'s matching WAL + 30s-timeout fix,
same date, as a defense-in-depth backstop).

Deliberately just a single advisory file lock (fcntl.flock), not a
full "DB agent" daemon that owns the only connection and brokers every
query over IPC — considered and set aside 2026-08-18 as disproportionate
for this project's actual scale (a handful of short-lived CLI invocations
running hourly / every 3 minutes, not a service architecture). Revisit if
that changes.
"""

from __future__ import annotations

import fcntl
import time
from pathlib import Path
from typing import IO, Optional

# job-tracker/src/job_tracker/pipeline/db_lock.py -> job-tracker/var/comms_fast.lock
DEFAULT_LOCK_PATH = Path(__file__).resolve().parents[3] / "var" / "comms_fast.lock"

# Sentinel exit code every lock-guarded entry point uses when it gives up
# waiting — "someone else is using the DB right now, try again shortly."
LOCK_BUSY_EXIT_CODE = 75


def try_acquire(lock_path: Path) -> Optional[IO[str]]:
    """Single non-blocking attempt. Returns an open handle holding the
    lock, or None if another process already holds it. Caller owns
    releasing it (see release())."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def acquire(
    lock_path: Path,
    *,
    wait_seconds: float = 0.0,
    poll_interval: float = 0.5,
) -> Optional[IO[str]]:
    """Blocking (up to wait_seconds) attempt. Returns the held handle, or
    None once wait_seconds has elapsed with the lock still unavailable.
    wait_seconds=0 (the default) matches an unattended launchd tick: skip
    immediately rather than let ticks pile up behind each other."""
    deadline = time.monotonic() + max(0.0, wait_seconds)
    started = time.monotonic()
    while True:
        fh = try_acquire(lock_path)
        if fh is not None:
            waited = time.monotonic() - started
            if waited >= 1.0:
                import sys

                print(
                    f"[db_lock] acquired {lock_path.name} after {waited:.1f}s wait",
                    file=sys.stderr,
                )
            return fh
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval)


def release(fh: IO[str]) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    fh.close()
