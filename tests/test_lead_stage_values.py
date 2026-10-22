import re
from pathlib import Path

from job_tracker.pipeline.models import LEAD_STAGES

SRC = Path(__file__).resolve().parents[1] / "src" / "job_tracker"
_STATUS_LIST_RE = re.compile(r"status\s+(?:NOT\s+)?IN\s*\(([^)?]*)\)", re.I)
_STATUS_EQ_RE = re.compile(r"status\s*(?:=|!=|<>)\s*'(\w+)'", re.I)
# Old names that store.py's migration renames to pursued / skipped.
_MIGRATED = {"approved", "passed"}


def test_every_status_in_sql_is_a_lead_stage():
    unknown = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        values = [v for m in _STATUS_LIST_RE.finditer(text) for v in re.findall(r"'(\w+)'", m.group(1))]
        values += _STATUS_EQ_RE.findall(text)
        unknown += [f"{path.name}: {v}" for v in values if v not in LEAD_STAGES and v not in _MIGRATED]
    assert unknown == []
