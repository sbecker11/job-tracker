"""The one-lead page update keeps every other card and puts the new one first."""

from job_tracker.pipeline.lead_card_cache import place_card_first


def test_place_card_first_keeps_cached_cards_and_ranks_the_new_one_at_zero():
    payload = {
        "pipeline": [
            {"id": "clarify", "count": 1},
            {"id": "send_resume", "count": 0},
            {"id": "wait_schedule", "count": 1},
            {"id": "decide_apply", "count": 0},
        ],
        "stages": {
            "clarify": [
                {"normalizedKey": "old::role", "company": "Old", "todayRank": 0, "draftReply": "cached"}
            ],
            "sendResume": [],
            "waitSchedule": [
                {"normalizedKey": "wait::role", "company": "Wait", "todayRank": 4, "draftReply": "also cached"}
            ],
            "decideApply": {},
        },
    }
    card = {"normalizedKey": "esolutions inc::ai engineer", "stage": "clarify", "company": "E-solutions INC"}
    place_card_first(payload, card)
    clarify = payload["stages"]["clarify"]
    assert clarify[0]["company"] == "E-solutions INC"
    assert clarify[0]["todayRank"] == 0
    assert clarify[1]["draftReply"] == "cached"
    assert clarify[1]["todayRank"] == 1
    assert payload["stages"]["waitSchedule"][0]["draftReply"] == "also cached"
    assert payload["stages"]["waitSchedule"][0]["todayRank"] == 2
    assert payload["pipeline"][0]["count"] == 2
