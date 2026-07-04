"""job_tracker package.

Loads `.env` (project root, next to pyproject.toml) as soon as the package is
imported, so every CLI entry point picks up secrets like ANTHROPIC_API_KEY
without each script having to remember to call load_dotenv() itself. Existing
environment variables are never overridden.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_PROJECT_ROOT_ENV)
