#!/usr/bin/env bash
# Run the job-tracker test suite with line + branch coverage.
# Usage: ./scripts/coverage.sh   (from repo root, or any cwd)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
  PIP="$ROOT/.venv/bin/pip"
else
  PY="${PYTHON:-python3}"
  PIP=("$PY" -m pip)
fi

if ! "$PY" -c "import pytest" 2>/dev/null; then
  echo "error: pytest not installed. Activate .venv or: $PY -m pip install 'pytest>=8'" >&2
  exit 1
fi
if ! "$PY" -c "import pytest_cov" 2>/dev/null; then
  echo "Installing pytest-cov into the active environment..."
  if [[ -x "$ROOT/.venv/bin/pip" ]]; then
    "$PIP" install -q pytest-cov
  else
    "${PIP[@]}" install -q pytest-cov
  fi
fi

echo "=== job-tracker coverage ==="
echo "Python: $PY"
echo

set +e
"$PY" -m pytest tests/ \
  --cov=job_tracker \
  --cov-branch \
  --cov-report=term \
  --cov-report=json:coverage.json \
  -q --tb=line
pytest_rc=$?
set -e

if [[ -f coverage.json ]]; then
  "$PY" - <<'PY'
import json
from pathlib import Path

totals = json.loads(Path("coverage.json").read_text())["totals"]
stmts = totals["num_statements"]
miss = totals["missing_lines"]
line_pct = 100.0 * (stmts - miss) / stmts if stmts else 0.0
branches = totals.get("num_branches") or 0
br_miss = totals.get("missing_branches") or 0
br_pct = (100.0 * (branches - br_miss) / branches) if branches else None
combined = totals.get("percent_covered_display") or f"{totals.get('percent_covered', 0):.0f}"

print()
print("--- job-tracker summary ---")
print(f"  Line coverage:     {line_pct:.1f}%  ({stmts - miss}/{stmts} statements)")
if br_pct is not None:
    print(f"  Branch coverage:   {br_pct:.1f}%  ({branches - br_miss}/{branches} branches)")
print(f"  Combined (cov):    {combined}%")
PY
fi

exit "$pytest_rc"
