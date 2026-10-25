#!/usr/bin/env python3
"""Post the copied LinkedIn thread to the local Pending Actions API."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
# `python3 scripts/paste_linkedin_lead.py` is often the system interpreter,
# which does not have this repo's dependencies. Switch to the venv before
# importing the package.
_VENV_PYTHON = _REPO_ROOT / ".venv" / "bin" / "python"
if (
    __name__ == "__main__"
    and _VENV_PYTHON.is_file()
    and Path(sys.executable).resolve() != _VENV_PYTHON.resolve()
):
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), *sys.argv])

_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.cli.paste_linkedin_lead import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
