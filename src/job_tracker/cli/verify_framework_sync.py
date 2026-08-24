"""Verify config/framework.yaml stays aligned with ~/CLAUDE.md.

Checks dealbreaker coverage (§3), compensation floor (§2), and that the
framework file's canonical ids match what the profile documents. Fails loudly
on drift so scoring does not silently diverge from the candidate profile.

  python scripts/verify_framework_sync.py
  verify-framework-sync          # console script after pip install -e .
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from job_tracker.scoring.scorer import DEFAULT_FRAMEWORK_PATH, load_framework

_DEFAULT_CLAUDE_MD = Path.home() / "CLAUDE.md"

# framework.yaml ids ↔ phrases that must appear in CLAUDE.md §3.
_DEALBREAKER_MARKERS: dict[str, tuple[str, ...]] = {
    "c2c_only": ("c2c-only", "c2c only"),
    "golang": ("golang", "go / golang"),
    "django": ("django",),
    "php": ("php",),
    "angular": ("angular",),
    "dotnet": (".net", "c#"),
    "onsite_only": ("onsite-only", "onsite only"),
}

_NOT_DEALBREAKER_MARKERS: dict[str, tuple[str, ...]] = {
    "w2_only": ("w2-only", "w2 only"),
    "us_citizen_or_no_sponsorship": (
        "us citizen",
        "without sponsorship",
        "authorized to work",
    ),
}

# Documented in CLAUDE §3 but not encoded as a framework not_dealbreaker row yet.
_CLAUDE_ONLY_FITS = (
    "1099 direct-to-individual",
    "1099 direct",
)


def _section_slice(text: str, heading: str, next_heading: str | None = None) -> str:
    """Return body under a `## heading` until the next `##` (or EOF)."""
    pattern = rf"^##\s+{re.escape(heading)}(?:\s|$)"
    match = re.search(pattern, text, flags=re.MULTILINE | re.IGNORECASE)
    if not match:
        return ""
    start = match.end()
    if next_heading:
        nxt = re.search(
            rf"^##\s+{re.escape(next_heading)}(?:\s|$)",
            text[start:],
            flags=re.MULTILINE | re.IGNORECASE,
        )
        end = start + nxt.start() if nxt else len(text)
    else:
        nxt = re.search(r"^##\s+", text[start:], flags=re.MULTILINE)
        end = start + nxt.start() if nxt else len(text)
    return text[start:end]


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    low = haystack.lower()
    return any(n.lower() in low for n in needles)


def _parse_compensation_floor(claude_text: str) -> tuple[int | None, int | None]:
    """Return (contract_hourly_w2_usd, permanent_base_usd) from §2."""
    section = _section_slice(claude_text, "2. Targeting Parameters", "3. Dealbreakers")
    contract = None
    permanent = None
    m = re.search(r"\$(\d+)\s*/\s*hr\s*w2", section, flags=re.IGNORECASE)
    if m:
        contract = int(m.group(1))
    m = re.search(r"\$?\s*(\d{2,3})\s*k\s*base", section, flags=re.IGNORECASE)
    if m:
        permanent = int(m.group(1)) * 1000
    return contract, permanent


def verify_framework_sync(
    *,
    framework_path: Path = DEFAULT_FRAMEWORK_PATH,
    claude_path: Path = _DEFAULT_CLAUDE_MD,
) -> dict[str, Any]:
    """Compare framework.yaml to CLAUDE.md. Returns a result dict; see `ok`."""
    errors: list[str] = []
    warnings: list[str] = []

    if not claude_path.is_file():
        errors.append(f"CLAUDE.md not found: {claude_path}")
        return {"ok": False, "errors": errors, "warnings": warnings}

    claude_text = claude_path.read_text(encoding="utf-8")
    dealbreaker_section = _section_slice(claude_text, "3. Dealbreakers", "4. House Rules")
    if not dealbreaker_section.strip():
        errors.append("CLAUDE.md §3 (Dealbreakers) not found")

    framework = load_framework(framework_path)
    fw_dealbreakers = {d.get("id") for d in (framework.get("dealbreakers") or []) if d.get("id")}
    fw_not = {d.get("id") for d in (framework.get("not_dealbreakers") or []) if d.get("id")}

    missing_ids = set(_DEALBREAKER_MARKERS) - fw_dealbreakers
    extra_ids = fw_dealbreakers - set(_DEALBREAKER_MARKERS)
    if missing_ids:
        errors.append(f"framework.yaml missing dealbreaker id(s): {sorted(missing_ids)}")
    if extra_ids:
        warnings.append(f"framework.yaml has undeclared dealbreaker id(s): {sorted(extra_ids)}")

    for db_id, markers in _DEALBREAKER_MARKERS.items():
        if db_id not in fw_dealbreakers:
            continue
        if not _contains_any(dealbreaker_section, markers):
            errors.append(f"CLAUDE.md §3 missing marker for dealbreaker {db_id!r}: {markers}")

    for nd_id, markers in _NOT_DEALBREAKER_MARKERS.items():
        if nd_id not in fw_not:
            errors.append(f"framework.yaml missing not_dealbreaker id {nd_id!r}")
        elif not _contains_any(dealbreaker_section, markers):
            errors.append(f"CLAUDE.md §3 missing marker for not-dealbreaker {nd_id!r}: {markers}")

    for phrase in _CLAUDE_ONLY_FITS:
        if phrase.lower() in dealbreaker_section.lower() and not any(
            phrase.lower() in (row.get("label") or "").lower() or phrase.lower() in (row.get("note") or "").lower()
            for row in (framework.get("not_dealbreakers") or [])
        ):
            warnings.append(
                f"CLAUDE.md documents {phrase!r} as a fit but framework.yaml has no matching not_dealbreaker row"
            )

    candidate = framework.get("candidate") or {}
    comp = candidate.get("compensation_floor") or {}
    claude_contract, claude_perm = _parse_compensation_floor(claude_text)
    fw_contract = comp.get("contract_hourly_w2_usd")
    fw_perm = comp.get("permanent_base_usd")
    if claude_contract is not None and fw_contract != claude_contract:
        errors.append(
            f"compensation floor contract hourly mismatch: framework={fw_contract} CLAUDE.md={claude_contract}"
        )
    if claude_perm is not None and fw_perm != claude_perm:
        errors.append(
            f"compensation floor permanent base mismatch: framework={fw_perm} CLAUDE.md={claude_perm}"
        )

    skills = framework.get("skills") or []
    if not skills:
        warnings.append("framework.yaml has no skills: entries (§8/§9 vocabulary)")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "frameworkPath": str(framework_path),
        "claudePath": str(claude_path),
        "dealbreakerIds": sorted(fw_dealbreakers),
        "notDealbreakerIds": sorted(fw_not),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--framework", type=Path, default=DEFAULT_FRAMEWORK_PATH)
    ap.add_argument("--claude-md", type=Path, default=_DEFAULT_CLAUDE_MD)
    ap.add_argument("--json", action="store_true", help="Emit JSON result")
    args = ap.parse_args(argv)

    result = verify_framework_sync(framework_path=args.framework, claude_path=args.claude_md)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["ok"]:
            print(f"OK — {args.framework.name} aligned with {args.claude_md}")
        else:
            print(f"DRIFT — {args.framework.name} vs {args.claude_md}", file=sys.stderr)
        for msg in result["errors"]:
            print(f"  ERROR: {msg}", file=sys.stderr)
        for msg in result["warnings"]:
            print(f"  warn: {msg}", file=sys.stderr)

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
