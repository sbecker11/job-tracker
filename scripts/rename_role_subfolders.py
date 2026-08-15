#!/usr/bin/env python3
"""One-time migration (2026-08-14): rename every existing multi-lead role
subfolder from `<Company>/<Company>_<Role>/` to `<Company>/<Role>/`, now that
`llm_apply._job_folder` drops the redundant company prefix for new packages
too (see that function's docstring). Only touches subfolders whose name
starts with `<company-folder-name>_` — anything else (e.g. a `communications`
subfolder, or a company whose role names happen not to be prefixed) is left
untouched and reported for a manual look.

Usage:
    python scripts/rename_role_subfolders.py [--root PATH] [--dry-run] [--yes]
"""

from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_ROOT = Path.home() / "Desktop" / "Resumes" / "2026"


def find_renames(root: Path) -> tuple[list[tuple[Path, Path]], list[Path]]:
    renames: list[tuple[Path, Path]] = []
    skipped: list[Path] = []

    for company_dir in sorted(root.iterdir()):
        if not company_dir.is_dir():
            continue
        subdirs = [p for p in company_dir.iterdir() if p.is_dir()]
        if not subdirs:
            continue
        prefix = company_dir.name + "_"
        for sub in sorted(subdirs):
            if sub.name.startswith(prefix):
                new_name = sub.name[len(prefix):]
                if not new_name:
                    skipped.append(sub)
                    continue
                target = company_dir / new_name
                renames.append((sub, target))
            else:
                skipped.append(sub)

    return renames, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--dry-run", action="store_true", help="Report what would be renamed, rename nothing")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = ap.parse_args(argv)

    if not args.root.is_dir():
        print(f"No such directory: {args.root}")
        return 1

    renames, skipped = find_renames(args.root)

    print(f"{len(renames)} subfolder(s) to rename, {len(skipped)} left as-is.\n")

    collisions = [(old, new) for old, new in renames if new.exists()]
    if collisions:
        print("COLLISIONS (target already exists — will NOT touch these):")
        for old, new in collisions:
            print(f"  {old.relative_to(args.root)}  ->  {new.relative_to(args.root)}  [target exists]")
        print()
    safe_renames = [(old, new) for old, new in renames if not new.exists()]

    for old, new in safe_renames:
        print(f"  {old.relative_to(args.root)}  ->  {new.relative_to(args.root)}")

    if skipped:
        print(f"\nSkipped (no company-name prefix found, left untouched):")
        for s in skipped:
            print(f"  {s.relative_to(args.root)}")

    if args.dry_run:
        print(f"\nDry run — {len(safe_renames)} rename(s) would be made, {len(collisions)} collision(s) skipped.")
        return 0

    if not safe_renames:
        print("\nNothing to rename.")
        return 0

    if not args.yes:
        answer = input(f"\nRename {len(safe_renames)} subfolder(s)? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Not renamed.")
            return 0

    for old, new in safe_renames:
        old.rename(new)

    print(f"\nRenamed {len(safe_renames)} subfolder(s).")
    if collisions:
        print(f"{len(collisions)} collision(s) left untouched — resolve those by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
