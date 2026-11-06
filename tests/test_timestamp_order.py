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


def test_linkedin_reply_notice_after_shawns_reply_is_unanswered():
    chrome = "\n\nAlfher Hidalgo\nReply\nhttps://www.linkedin.com/messaging/thread/abc/\n"

    def notice(occurred_at, text, mid):
        return {
            "direction": "inbound",
            "occurred_at": occurred_at,
            "message_id": mid,
            "summary": "Remote Senior Full-Stack Engineer role",
            "body_text": "Remote Senior Full-Stack Engineer role" + chrome + text,
        }

    pitch = notice("2026-11-05T20:19:13+00:00", "I work with Stefanini Group and manage a remote role.", "m1")
    reply = {**_conv("outbound", "2026-11-06T19:29:00+00:00"), "message_id": "o1"}
    answer = notice("2026-11-06T19:33:39+00:00", "Hi Shawn, it is fully remote", "m2")
    assert _unanswered_inbound([pitch, reply, answer]) is answer
    assert _unanswered_inbound([pitch, reply]) is None
