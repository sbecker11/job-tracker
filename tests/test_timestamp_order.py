from datetime import datetime, timezone

from job_tracker.pipeline.pending_workflow import _unanswered_days, _unanswered_inbound

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def _days(ts, now):
    return (now - datetime.fromisoformat(ts)).days


def _conv(direction, occurred_at):
    return {
        "direction": direction,
        "occurred_at": occurred_at,
        "summary": "Re: Data Engineer role",
        "body_text": "Thanks, let's set up a call.",
    }


def test_unanswered_inbound_compares_times_not_strings():
    # 10:00 Mountain is 16:00 UTC, after the 12:00 UTC outbound; as strings it sorts first.
    inbound = _conv("inbound", "2026-10-20T10:00:00-06:00")
    outbound = _conv("outbound", "2026-10-20T12:00:00+00:00")
    assert _unanswered_inbound([outbound, inbound]) is inbound

    later_reply = _conv("outbound", "2026-10-20T17:00:00+00:00")
    assert _unanswered_inbound([outbound, inbound, later_reply]) is None


def test_unanswered_inbound_picks_latest_inbound_by_time():
    early = _conv("inbound", "2026-10-20T15:00:00+00:00")
    late = _conv("inbound", "2026-10-20T10:00:00-06:00")
    assert _unanswered_inbound([late, early]) is late


def test_unanswered_days_counts_from_oldest_inbound_after_last_outbound():
    convs = [
        _conv("inbound", "2026-10-01T12:00:00+00:00"),
        _conv("outbound", "2026-10-05T12:00:00+00:00"),
        _conv("inbound", "2026-10-12T12:00:00+00:00"),
        _conv("inbound", "2026-10-20T12:00:00+00:00"),
    ]
    assert _unanswered_days(convs, _days, NOW) == 10
    assert _unanswered_days(convs[:2], _days, NOW) == 0
    assert _unanswered_days([convs[0], convs[3]], _days, NOW) == 21
