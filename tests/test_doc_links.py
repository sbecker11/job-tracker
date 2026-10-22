"""Relative links in this repo's markdown docs resolve. Links that leave the
repo (siblings, the umbrella workspace) are checked by the workspace's
check-docs.sh instead, since CI only has this repo."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_LINK = re.compile(r"\]\(([^)#\s]+)")
_FENCE = re.compile(r"```.*?```", re.S)


def test_relative_doc_links_resolve():
    files = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.md"], capture_output=True, text=True, check=True).stdout.split()
    broken = []
    for rel in files:
        path = REPO / rel
        for link in _LINK.findall(_FENCE.sub("", path.read_text(encoding="utf-8"))):
            if "://" in link or link.startswith(("mailto:", "~")):
                continue
            target = (path.parent / link).resolve()
            if REPO not in target.parents and target != REPO:
                continue
            if not target.exists():
                broken.append(f"{rel} -> {link}")
    assert not broken, "broken relative links:\n" + "\n".join(broken)
