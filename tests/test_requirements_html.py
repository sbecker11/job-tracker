"""docs/requirements.html stays in step with docs/REQUIREMENTS.md."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_requirements.py"
_spec = importlib.util.spec_from_file_location("render_requirements", _SCRIPT)
rr = importlib.util.module_from_spec(_spec)
sys.modules["render_requirements"] = rr
_spec.loader.exec_module(rr)


def test_html_is_up_to_date():
    assert rr.main(["--check"]) == 0, "run scripts/render_requirements.py and commit docs/requirements.html"


def test_every_rule_row_is_rendered():
    md = rr.SOURCE.read_text(encoding="utf-8")
    codes = [line.split("|")[1].strip() for line in md.splitlines() if rr._RULE_ROW_RE.match(line)]
    page = rr.render(md)
    assert codes and all(f'id="{c}"' in page for c in codes)


def test_status_and_inline_markup():
    assert rr.status_kind("**BROKEN** — x") == "BROKEN"
    assert rr.status_kind("OK (2026-10-22)") == "OK"
    assert rr.inline("`a<b>` and **c**") == "<code>a&lt;b&gt;</code> and <strong>c</strong>"
    assert 'href="../tests/test_x.py"' in rr.enforced_html("test: `test_x.py`")
