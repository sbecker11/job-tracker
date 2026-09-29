"""Strip `[skill:Name]` and/or `[1]` / `[1.2.3]` tags from a DOCX.

`--no-skill-tags` unwraps `[skill:Java]` → `Java`.
`--no-index-tags` removes `[1]` / `[1.2.3]` markers.
Pass either or both. With neither flag, both kinds are stripped.

  python -m job_tracker.scripts.index_stripper IN.docx -o CLEAN.docx
  python -m job_tracker.scripts.index_stripper IN.docx --no-skill-tags
  python -m job_tracker.scripts.index_stripper IN.docx --no-index-tags
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.skill_tags import (
    count_tags,
    strip_skill_tags_file,
)


def _strip_flags(no_skill_tags: bool, no_index_tags: bool) -> tuple[bool, bool]:
    """If neither flag is set, strip both. Otherwise strip only the named kinds."""
    if no_skill_tags or no_index_tags:
        return no_skill_tags, no_index_tags
    return True, True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("docx", type=Path, help="Input .docx path")
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write here instead of overwriting the input file",
    )
    ap.add_argument(
        "--no-skill-tags",
        action="store_true",
        help="Unwrap `[skill:…]` tags (leave the inner name)",
    )
    ap.add_argument(
        "--no-index-tags",
        action="store_true",
        help="Remove `[1]` / `[1.2.3]` job-index markers",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print how many tags would be stripped; do not write",
    )
    args = ap.parse_args(argv)

    src = args.docx.expanduser()
    if not src.is_file():
        print(f"not found: {src}", file=sys.stderr)
        return 1
    if src.suffix.lower() != ".docx":
        print(f"expected a .docx: {src}", file=sys.stderr)
        return 1

    strip_skills, strip_indexes = _strip_flags(
        args.no_skill_tags, args.no_index_tags
    )

    if args.dry_run:
        from docx import Document

        skills = 0
        indexes = 0
        doc = Document(src)
        for para in doc.paragraphs:
            s, i = count_tags(para.text)
            if strip_skills:
                skills += s
            if strip_indexes:
                indexes += i
        parts = []
        if strip_skills:
            parts.append(f"{skills} [skill:…] tag(s)")
        if strip_indexes:
            parts.append(f"{indexes} index tag(s)")
        print(f"{', '.join(parts)} in {src}")
        return 0

    dest = args.output
    n_skills, n_indexes = strip_skill_tags_file(
        src, dest, strip_skills=strip_skills, strip_indexes=strip_indexes
    )
    written = dest if dest is not None else src
    parts = []
    if strip_skills:
        parts.append(f"{n_skills} [skill:…] tag(s)")
    if strip_indexes:
        parts.append(f"{n_indexes} index tag(s)")
    print(f"stripped {', '.join(parts)} → {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
