"""Tests for job_tracker.pipeline.db_lock — the shared advisory lock that
comms_fast_cycle.py, triage_imap_now.py, and scripts/with_db_lock.py all
use (see that module's docstring for why it exists as one canonical
implementation instead of three copy-pasted ones)."""

from __future__ import annotations

import time
from pathlib import Path

from job_tracker.pipeline import db_lock


def test_try_acquire_then_release_allows_reacquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"

    fh = db_lock.try_acquire(lock_path)
    assert fh is not None
    assert lock_path.is_file()

    # A second attempt while the first is still held must fail.
    assert db_lock.try_acquire(lock_path) is None

    db_lock.release(fh)

    # Freed after release.
    fh2 = db_lock.try_acquire(lock_path)
    assert fh2 is not None
    db_lock.release(fh2)


def test_acquire_returns_none_when_still_busy_after_wait(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    holder = db_lock.try_acquire(lock_path)
    assert holder is not None
    try:
        start = time.monotonic()
        result = db_lock.acquire(lock_path, wait_seconds=0.3, poll_interval=0.05)
        elapsed = time.monotonic() - start
        assert result is None
        # Waited roughly the full budget, not an instant give-up.
        assert elapsed >= 0.25
    finally:
        db_lock.release(holder)


def test_acquire_zero_wait_seconds_is_non_blocking(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    holder = db_lock.try_acquire(lock_path)
    assert holder is not None
    try:
        start = time.monotonic()
        result = db_lock.acquire(lock_path, wait_seconds=0.0)
        elapsed = time.monotonic() - start
        assert result is None
        assert elapsed < 0.2
    finally:
        db_lock.release(holder)


def test_acquire_succeeds_once_holder_releases(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    holder = db_lock.try_acquire(lock_path)
    assert holder is not None

    import threading

    def release_soon() -> None:
        time.sleep(0.2)
        db_lock.release(holder)

    t = threading.Thread(target=release_soon)
    t.start()
    try:
        result = db_lock.acquire(lock_path, wait_seconds=2.0, poll_interval=0.05)
        assert result is not None
        db_lock.release(result)
    finally:
        t.join()


def test_default_lock_path_lives_under_var() -> None:
    assert db_lock.DEFAULT_LOCK_PATH.name == "comms_fast.lock"
    assert db_lock.DEFAULT_LOCK_PATH.parent.name == "var"
