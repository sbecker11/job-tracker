#!/usr/bin/env python3
"""Move résumé/cover-letter ("application package") files out of any lead
folder that doesn't have a `full-LLM-review.docx` sitting next to them.

Scope: only résumé/cover-letter files move (filename contains "resume" or
"cover", case-insensitive) — `JobDescription.docx`, `no-LLM-review.docx`,
and anything else (e.g. a signed NDA) are left in place untouched.

Files move (not copy/delete) to a mirror of their original relative path
under the destination root, so nothing collides and everything stays easy
to trace back / move back.

Usage:
    python scripts/move_packages_without_full_llm_review.py [--dry-run]
        [--output-root PATH] [--dest PATH]
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

DEFAULT_OUTPUT_ROOT = Path.home() / "Desktop" / "Resumes" / "2026"
DEFAULT_DEST = Path.home() / "tmp" / "Resumes_2026_packages_without_full_LLM_review"

_EXCLUDE_EXACT = {"JobDescription.docx", "no-LLM-review.docx", "full-LLM-review.docx"}


def _is_package_file(name: str) -> bool:
    if name in _EXCLUDE_EXACT:
        return False
    lower = name.lower()
    return "resume" in lower or "cover" in lower


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    # Every directory anywhere under output-root that directly contains at
    # least one package-looking file.
    candidate_dirs = sorted({p.parent for p in args.output_root.rglob("*") if p.is_file() and _is_package_file(p.name)})

    moved = 0
    skipped_has_review = 0
    moved_files: list[Path] = []

    for folder in candidate_dirs:
        if (folder / "full-LLM-review.docx").is_file():
            skipped_has_review += 1
            continue

        rel = folder.relative_to(args.output_root)
        dest_dir = args.dest / rel
        for f in sorted(folder.iterdir()):
            if f.is_file() and _is_package_file(f.name):
                dest_path = dest_dir / f.name
                print(f"{'[dry-run] would move' if args.dry_run else 'moving'}: {f.relative_to(args.output_root)} -> {dest_path}")
                if not args.dry_run:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(dest_path))
                moved += 1
                moved_files.append(f)

    print()
    print(f"lead folders with package files: {len(candidate_dirs)}")
    print(f"skipped (already has full-LLM-review.docx): {skipped_has_review}")
    print(f"files {'that would be' if args.dry_run else ''} moved: {moved}")
    print(f"destination: {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
