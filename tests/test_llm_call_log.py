import json

from job_tracker.cli.spend_report import monthly_call_spend
from job_tracker.pipeline.llm_call_log import log_call, log_path


def test_each_call_appends_one_line_and_the_report_totals_by_month_and_source(tmp_path):
    log_call(step="evaluate", model="claude-sonnet-5", input_tokens=1000, output_tokens=200, cost_usd=0.004)
    log_call(step="extract", model="claude-haiku-4-5", input_tokens=500, output_tokens=50, cost_usd=None)
    with log_path().open("a") as f:
        f.write(json.dumps({"ts": "2026-09-30T23:59:00+00:00", "source": "comms-migration", "cost_usd": 0.0024}) + "\n")
        f.write("not json\n")

    lines = log_path().read_text().splitlines()
    first = json.loads(lines[0])
    assert (first["source"], first["step"], first["input_tokens"], first["cost_usd"]) == ("job-tracker", "evaluate", 1000, 0.004)
    assert json.loads(lines[1])["cost_usd"] == 0.0

    report = monthly_call_spend()
    month = first["ts"][:7]
    assert report[month]["job-tracker"] == {"calls": 2, "costUsd": 0.004}
    assert report["2026-09"]["comms-migration"] == {"calls": 1, "costUsd": 0.0024}
