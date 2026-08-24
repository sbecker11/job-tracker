"""Tests for verify_framework_sync (Phase 0 guardrail)."""

from __future__ import annotations

from pathlib import Path

import yaml

from job_tracker.cli.verify_framework_sync import verify_framework_sync

_CLAUDE_FIXTURE = """\
## 2. Targeting Parameters

**Compensation floor (internal only — never write into materials):**
- Contract: **$75/hr W2** minimum
- Permanent: **~$115K base** minimum

## 3. Dealbreakers — auto-flag before writing anything

| Signal | Verdict |
|---|---|
| **C2C-only** (no W2 option) | Structural mismatch |
| **Go / Golang** as load-bearing requirement | Dealbreaker |
| **Django** as load-bearing requirement | Dealbreaker |
| **PHP** as load-bearing requirement | Dealbreaker |
| **Angular** as load-bearing requirement | Dealbreaker |
| **.NET / C#** as load-bearing requirement | Dealbreaker |
| **Onsite-only, not in/near Lehi, UT** | Dealbreaker |
| **W2-only** | NOT a concern |
| **1099 direct-to-individual** | NOT a concern |
| **US citizen required** | NOT a concern |

## 4. House Rules
"""


def _minimal_framework() -> dict:
    return {
        "candidate": {
            "compensation_floor": {
                "contract_hourly_w2_usd": 75,
                "permanent_base_usd": 115000,
            }
        },
        "dealbreakers": [
            {"id": "c2c_only", "label": "C2C", "keywords": ["c2c only"]},
            {"id": "golang", "label": "Go", "keywords": ["golang"]},
            {"id": "django", "label": "Django", "keywords": ["django"]},
            {"id": "php", "label": "PHP", "keywords": ["php"]},
            {"id": "angular", "label": "Angular", "keywords": ["angular"]},
            {"id": "dotnet", "label": ".NET", "keywords": [".net"]},
            {"id": "onsite_only", "label": "Onsite", "keywords": ["onsite only"]},
        ],
        "not_dealbreakers": [
            {"id": "w2_only", "label": "W2-only"},
            {"id": "us_citizen_or_no_sponsorship", "label": "US citizen"},
        ],
        "skills": [{"keyword": "python", "weight": 3}],
    }


def test_verify_ok_when_aligned(tmp_path: Path):
    fw = tmp_path / "framework.yaml"
    fw.write_text(yaml.dump(_minimal_framework()))
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(_CLAUDE_FIXTURE)

    result = verify_framework_sync(framework_path=fw, claude_path=claude)
    assert result["ok"] is True
    assert result["errors"] == []


def test_verify_fails_on_compensation_drift(tmp_path: Path):
    data = _minimal_framework()
    data["candidate"]["compensation_floor"]["contract_hourly_w2_usd"] = 90
    fw = tmp_path / "framework.yaml"
    fw.write_text(yaml.dump(data))
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(_CLAUDE_FIXTURE)

    result = verify_framework_sync(framework_path=fw, claude_path=claude)
    assert result["ok"] is False
    assert any("contract hourly" in e for e in result["errors"])


def test_verify_fails_when_dealbreaker_missing_from_framework(tmp_path: Path):
    data = _minimal_framework()
    data["dealbreakers"] = [d for d in data["dealbreakers"] if d["id"] != "django"]
    fw = tmp_path / "framework.yaml"
    fw.write_text(yaml.dump(data))
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(_CLAUDE_FIXTURE)

    result = verify_framework_sync(framework_path=fw, claude_path=claude)
    assert result["ok"] is False
    assert any("django" in e for e in result["errors"])
