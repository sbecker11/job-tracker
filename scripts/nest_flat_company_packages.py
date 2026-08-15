#!/usr/bin/env python3
"""One-time migration (2026-08-14): nest every leftover flat company package
under `<Company>/<Role>/`.

Background: package layout is now always `<Company>/<Title>/` (see
`llm_apply._job_folder`). `rename_role_subfolders.py` already dropped the
redundant `<Company>_<Role>` prefix on multi-lead subfolders. This script
handles the much larger set of *single-lead* companies that still have
`JobDescription.docx` / reviews / weblocs / résumés sitting directly in
`<Company>/`, plus stray company-level `communications/` folders.

Title resolution (first hit wins):
  1. `Title @ Company` heading in JobDescription.docx / no-LLM-review.docx /
     full-LLM-review.docx
  2. Exactly one distinct title for this company folder in leads.db
  3. Filename tokens on résumé/cover letter (`Shawn_Becker_Resume_<Co>_<Title>`)

Company-level `communications/` moves into the same role folder when a
title is resolved. Ambiguous cases (flat files + multiple DB titles and no
usable heading) are reported and left untouched.

Usage:
    python scripts/nest_flat_company_packages.py [--root PATH] [--db PATH]
                                                 [--dry-run] [--yes]
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _safe_filename  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect  # noqa: E402

SKIP_TOP_LEVEL = {"Templates"}
NON_ROLE_DIR_NAMES = {"communications", "keep"}
PACKAGE_DOC_NAMES = {
    "jobdescription.docx",
    "no-llm-review.docx",
    "full-llm-review.docx",
    "applyurl.webloc",
}


def _heading_title(path: Path) -> str | None:
    """Parse `Title @ Company` from the first non-empty DOCX paragraph."""
    if path.suffix.casefold() != ".docx":
        return None
    try:
        from docx import Document

        for para in Document(str(path)).paragraphs:
            text = (para.text or "").strip()
            if not text:
                continue
            if " @ " not in text:
                return None
            title, _, _company = text.rpartition(" @ ")
            title = title.strip()
            return title or None
    except Exception:
        return None
    return None


def _titles_from_flat_docs(company_dir: Path) -> list[str]:
    titles: list[str] = []
    for name in ("JobDescription.docx", "no-LLM-review.docx", "full-LLM-review.docx"):
        path = company_dir / name
        if not path.is_file():
            continue
        title = _heading_title(path)
        if title and title not in titles:
            titles.append(title)
    return titles


_RESUME_RE = re.compile(
    r"(?i)^Shawn_Becker_(?:Resume|coverLetter|Cover_Letter)_(.+)\.docx$"
)


def _title_from_resume_filename(filename: str, *, company_safe: str) -> str | None:
    m = _RESUME_RE.match(filename)
    if not m:
        return None
    rest = m.group(1)
    # Prefer stripping a leading company_safe_ prefix when present.
    prefix = company_safe + "_"
    if rest.startswith(prefix):
        rest = rest[len(prefix) :]
    # Filename used underscores for spaces; keep as folder name via _safe_filename later.
    return rest.replace("_", " ").strip() or None


def _db_titles_by_company_safe(db_path: Path) -> dict[str, list[str]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT company, title FROM job_leads WHERE deleted_at IS NULL"
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, list[str]] = {}
    for r in rows:
        key = _safe_filename(r["company"])
        title = (r["title"] or "").strip()
        if not title:
            continue
        bucket = out.setdefault(key, [])
        if title not in bucket:
            bucket.append(title)
    return out


def _existing_role_dirs(company_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in company_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in NON_ROLE_DIR_NAMES
    )


def _resolve_title(
    company_dir: Path,
    *,
    db_titles: list[str],
) -> tuple[str | None, str]:
    """Return (title, reason) or (None, why_skipped)."""
    from_docs = _titles_from_flat_docs(company_dir)
    if len(from_docs) == 1:
        return from_docs[0], "docx_heading"
    if len(from_docs) > 1:
        # Prefer intersection with DB titles when possible.
        overlap = [t for t in from_docs if t in db_titles]
        if len(overlap) == 1:
            return overlap[0], "docx_heading+db"
        return None, f"ambiguous_docx_headings={from_docs!r}"

    if len(db_titles) == 1:
        return db_titles[0], "db_single_title"

    # Try résumé/cover filenames.
    company_safe = company_dir.name
    from_names: list[str] = []
    for p in company_dir.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        t = _title_from_resume_filename(p.name, company_safe=company_safe)
        if t and t not in from_names:
            from_names.append(t)
    if len(from_names) == 1:
        return from_names[0], "resume_filename"
    if len(from_names) > 1:
        return None, f"ambiguous_resume_filenames={from_names!r}"

    # Sole existing role subfolder — fold leftovers into it.
    role_dirs = _existing_role_dirs(company_dir)
    if len(role_dirs) == 1:
        # Prefer the on-disk folder name as the title token (already safe).
        return role_dirs[0].name.replace("_", " "), "sole_existing_role_dir"

    if not db_titles:
        flat = [p.name for p in company_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
        if not flat and not (company_dir / "communications").is_dir():
            return None, "empty"
        # Catch-all role folder so company roots never keep loose files.
        return "_Unassigned", "unassigned_catchall"

    # Multi-title companies with leftover flat/comms and no sole role dir —
    # still clear the company root into a catch-all role folder.
    flat = [p.name for p in company_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    if flat or (company_dir / "communications").is_dir():
        return "_Unassigned", "unassigned_catchall_multi_title"

    return None, f"ambiguous_db_titles={db_titles!r}"


def _flat_files(company_dir: Path) -> list[Path]:
    return sorted(
        p for p in company_dir.iterdir() if p.is_file() and not p.name.startswith(".")
    )


def _plan_moves(
    root: Path,
    *,
    db_by_safe: dict[str, list[str]],
) -> tuple[list[tuple[Path, Path]], list[str], list[str]]:
    """Return (moves, migrated_company_summaries, skipped_summaries)."""
    moves: list[tuple[Path, Path]] = []
    migrated: list[str] = []
    skipped: list[str] = []

    for company_dir in sorted(root.iterdir()):
        if not company_dir.is_dir() or company_dir.name.startswith("."):
            continue
        if company_dir.name in SKIP_TOP_LEVEL:
            continue

        flat = _flat_files(company_dir)
        company_comms = company_dir / "communications"
        has_company_comms = company_comms.is_dir()

        if not flat and not has_company_comms:
            continue

        db_titles = db_by_safe.get(company_dir.name, [])
        title, reason = _resolve_title(company_dir, db_titles=db_titles)
        if title is None:
            skipped.append(f"{company_dir.name}: {reason}")
            continue

        # If we resolved via an existing role dir name, use that folder as-is
        # (avoid re-_safe_filename transforming an already-safe name).
        if reason == "sole_existing_role_dir":
            role_dirs = _existing_role_dirs(company_dir)
            role_dir = role_dirs[0]
        else:
            role_dir = company_dir / _safe_filename(title)
        file_moves = 0
        for src in flat:
            dest = role_dir / src.name
            moves.append((src, dest))
            file_moves += 1

        comm_moves = 0
        if has_company_comms:
            dest_comms = role_dir / "communications"
            for src in sorted(company_comms.rglob("*")):
                if not src.is_file():
                    continue
                rel = src.relative_to(company_comms)
                moves.append((src, dest_comms / rel))
                comm_moves += 1
            # Also record removing empty communications dir after moves
            # (handled in apply by rmdir).

        migrated.append(
            f"{company_dir.name} -> {role_dir.name}/  "
            f"({file_moves} file(s), {comm_moves} comm file(s); via {reason})"
        )

    return moves, migrated, skipped


def _apply_moves(moves: list[tuple[Path, Path]]) -> tuple[int, list[str]]:
    done = 0
    notes: list[str] = []
    comm_dirs_to_prune: set[Path] = set()

    for src, dest in moves:
        if not src.exists():
            continue
        if dest.exists():
            try:
                if src.stat().st_size == dest.stat().st_size and src.read_bytes() == dest.read_bytes():
                    src.unlink()
                    notes.append(f"removed duplicate flat copy: {src.name} under {src.parent.name}")
                    done += 1
                    continue
            except Exception:
                pass
            alt = dest.with_name(f"_from_company_flat__{dest.name}")
            if alt.exists():
                notes.append(f"collision left: {src}")
                continue
            dest = alt
            notes.append(f"kept differing flat copy as {alt.name}")

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        done += 1
        parts = src.parts
        if "communications" in parts:
            try:
                idx = parts.index("communications")
                comm_dirs_to_prune.add(Path(*parts[: idx + 1]))
            except ValueError:
                pass

    for d in sorted(comm_dirs_to_prune, key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            try:
                d.rmdir()
            except OSError:
                pass

    return done, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--dry-run", action="store_true", help="Report plan, write nothing")
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = ap.parse_args(argv)

    if not args.root.is_dir():
        print(f"No such directory: {args.root}")
        return 1

    db_by_safe = _db_titles_by_company_safe(args.db)
    moves, migrated, skipped = _plan_moves(args.root, db_by_safe=db_by_safe)

    print(f"Companies to nest: {len(migrated)}")
    print(f"File moves planned: {len(moves)}")
    print(f"Skipped: {len(skipped)}\n")

    for line in migrated[:40]:
        print(f"  {line}")
    if len(migrated) > 40:
        print(f"  ... +{len(migrated) - 40} more companies")

    if skipped:
        print(f"\nSkipped ({len(skipped)}):")
        for line in skipped[:60]:
            print(f"  {line}")
        if len(skipped) > 60:
            print(f"  ... +{len(skipped) - 60} more")

    if args.dry_run:
        print(f"\nDry run — would move {len(moves)} path(s) across {len(migrated)} company folder(s).")
        return 0

    if not moves:
        print("\nNothing to move.")
        return 0

    if not args.yes:
        answer = input(f"\nApply {len(moves)} move(s) across {len(migrated)} company folder(s)? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Not applied.")
            return 0

    done, notes = _apply_moves(moves)
    print(f"\nMoved/resolved {done} path(s).")
    if notes:
        print(f"{len(notes)} note(s):")
        for n in notes[:40]:
            print(f"  {n}")
        if len(notes) > 40:
            print(f"  ... +{len(notes) - 40} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
