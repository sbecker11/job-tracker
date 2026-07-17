# Coverage policy (job-tracker)

Target: **≥90% line coverage per file** under `src/job_tracker/`, plus overall package ≥90% when feasible. Branch coverage is tracked but not gated yet.

## How to run

```bash
./scripts/coverage.sh          # pytest + term/JSON report + soft per-file check
./scripts/check_per_file_coverage.py   # report-only against coverage.json
./scripts/check_per_file_coverage.py --fail-under 70   # interim soft gate
```

Hard `fail_under=90` is **not** enabled until most files clear 90%. Interim soft reporting (and optional `--fail-under 70`) is intentional.

## Measured source

- Package: `job_tracker` (`src/job_tracker/`)
- Config: `[tool.coverage.*]` in `pyproject.toml`

## Omit allowlist

Only omit what cannot reasonably be exercised in unit tests:

| Pattern | Why |
|---|---|
| `*/__main__.py` | Entry wrappers; CLIs are tested via `main(argv=...)` |
| `if __name__ == "__main__":` blocks | Covered by pragma on those lines where present |
| Optional-import failure paths already marked `# pragma: no cover` | Defensive ImportError branches for optional Google libs |

Do **not** omit CLIs, network clients, or helpers to game the percentage — mock them instead.

## Color thresholds (scripts / canvases)

- Green ≥90%
- Yellow ≥70%
- Red &lt;70%
