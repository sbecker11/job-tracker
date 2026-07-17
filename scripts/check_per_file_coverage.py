#!/usr/bin/env python3
"""Soft (or optionally hard) per-file line-coverage check against coverage.json.

Default: report-only (exit 0). Pass --fail-under N to exit 1 when any measured
file or the package total is below N.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


def _color(pct: float) -> str:
    if pct >= 90:
        return GREEN
    if pct >= 70:
        return YELLOW
    return RED


def _fmt(pct: float) -> str:
    return f"{_color(pct)}{pct:5.1f}%{RESET}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--coverage-json",
        type=Path,
        default=Path("coverage.json"),
        help="Path to coverage.py JSON report (default: ./coverage.json)",
    )
    ap.add_argument(
        "--threshold",
        type=float,
        default=90.0,
        help="Per-file / overall target for reporting (default: 90)",
    )
    ap.add_argument(
        "--fail-under",
        type=float,
        default=None,
        metavar="N",
        help="If set, exit 1 when any file or totals fall below N (soft gate). "
        "Omit for report-only (exit 0).",
    )
    args = ap.parse_args(argv)

    path = args.coverage_json
    if not path.exists():
        print(f"No coverage JSON at {path} — run ./scripts/coverage.sh first.", file=sys.stderr)
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    totals = data["totals"]
    stmts = totals["num_statements"]
    miss = totals["missing_lines"]
    overall = 100.0 * (stmts - miss) / stmts if stmts else 0.0

    rows: list[tuple[float, str, int, int]] = []
    for fname, finfo in data["files"].items():
        s = finfo["summary"]
        n = s["num_statements"]
        c = s["covered_lines"]
        # Line-only % (ignore branches) — matches the ≥90% line target.
        pct = 100.0 * c / n if n else 100.0
        rows.append((pct, fname, c, n))
    rows.sort()

    below = [r for r in rows if r[0] < args.threshold]
    print("--- per-file line coverage ---")
    print(f"  Overall: {_fmt(overall)}  ({stmts - miss}/{stmts} statements)")
    print(f"  Target:  {args.threshold:.0f}%  |  files below: {len(below)}/{len(rows)}")
    if below:
        print()
        for pct, fname, c, n in below:
            print(f"  {_fmt(pct)}  {c}/{n}  {fname}")
    else:
        print("  All measured files meet the target.")

    if args.fail_under is not None:
        gate = args.fail_under
        failing = [r for r in rows if r[0] < gate]
        if overall < gate or failing:
            print(
                f"\nFAIL: overall or {len(failing)} file(s) below fail-under={gate:.0f}% "
                f"(overall {_fmt(overall)})",
                file=sys.stderr,
            )
            return 1
        print(f"\nPASS: overall and all files ≥ {gate:.0f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
