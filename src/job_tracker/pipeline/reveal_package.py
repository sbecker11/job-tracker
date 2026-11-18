"""Open a lead's résumé and cover letter in Finder.

LinkedIn's file picker does not start in the new lead's package folder.
Selecting the two documents in Finder lets them be dragged onto the reply.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT

_REVEAL_SCRIPT = """
on run argv
  set fileList to {}
  repeat with p in argv
    set end of fileList to (POSIX file (contents of p) as alias)
  end repeat
  tell application "Finder"
    reveal fileList
    activate
  end tell
end run
"""


def package_attachments(folder: Path, *, root: Path = DEFAULT_OUTPUT_ROOT) -> list[Path]:
    """Résumé and cover letter in `folder`, if that folder is under `root`."""
    resolved = folder.expanduser().resolve()
    root_resolved = root.expanduser().resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("not a package folder")
    if not resolved.is_dir():
        raise ValueError("package folder not found")
    docx = sorted(resolved.glob("*.docx"))
    resume = next((p for p in docx if "resume" in p.name.lower()), None)
    cover = next((p for p in docx if "cover" in p.name.lower()), None)
    return [p for p in (resume, cover) if p is not None]


def reveal_package(folder: Path, *, root: Path = DEFAULT_OUTPUT_ROOT, opener=None) -> dict:
    """Select the résumé and cover letter in Finder. Opens the folder if either is missing."""
    files = package_attachments(folder, root=root)
    show = files or [folder.expanduser().resolve()]
    if opener is not None:
        opener(show)
    else:
        _open_in_finder(show)
    return {
        "ok": True,
        "folder": str(folder.expanduser().resolve()),
        "files": [p.name for p in files],
    }


def _open_in_finder(paths: list[Path]) -> None:
    if len(paths) == 1 and paths[0].is_dir():
        subprocess.Popen(["open", str(paths[0])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    subprocess.Popen(
        ["osascript", "-e", _REVEAL_SCRIPT, *[str(p) for p in paths]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
