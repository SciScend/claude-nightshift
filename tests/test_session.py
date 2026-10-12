import pytest

from claude_nightshift.session import (
    SessionResult,
    classify,
    usage_from_rate_limit_event,
)
from claude_nightshift.usage import Usage


def test_rate_limit_event_fractions_become_percentages():
    usage = usage_from_rate_limit_event(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "unifiedWindows": {
                    "seven_day": {"utilization": 0.42, "resetsAt": 1000},
                    "five_hour": {
                        "utilization": 0.9,
                        "resetsAt": "1970-01-01T00:01:00Z",
                    },
                },
            },
        }
    )
    assert usage.weekly_pct == pytest.approx(42)
    assert usage.five_hour_pct == pytest.approx(90)
    assert usage.weekly_resets == 1000
    assert usage.five_hour_resets == 60
    assert usage.status == "allowed"
    assert usage.source == "stream"


def test_rate_limit_event_falls_back_to_top_level_reset():
    usage = usage_from_rate_limit_event(
        {
            "rate_limit_info": {
                "rateLimitType": "five_hour",
                "resetsAt": "500",
                "unifiedWindows": {"five_hour": {"utilization": 1.0}},
            }
        }
    )
    assert usage.five_hour_resets == 500


def test_rate_limit_event_without_windows():
    assert usage_from_rate_limit_event({"rate_limit_info": {}}) is None


def make_session(result=None, returncode=1, text="", usage=None, text_limit=False):
    session = SessionResult()
    session.result = result
    session.returncode = returncode
    session.output_text = text
    session.usage = usage
    session.text_limit_seen = text_limit
    return session


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        (make_session({"subtype": "success", "is_error": False}), "success"),
        (make_session({"subtype": "error_max_turns"}), "max_turns"),
        (make_session({"subtype": "error", "api_error_status": 429}), "limit"),
        (make_session({"subtype": "error", "errors": ["Usage limit reached"]}), "limit"),
        (make_session(usage=Usage(weekly_pct=10, status="rejected")), "limit"),
        (make_session(text_limit=True, returncode=0), "limit"),
        (make_session(returncode=0), "success"),
        (make_session(text="Error: Reached max turns (5)"), "max_turns"),
        (make_session(text="API Error: 503 Service Unavailable"), "transient"),
        (make_session(text="something broke"), "failed"),
    ],
)
def test_classify(session, expected):
    assert classify(session) == expected
