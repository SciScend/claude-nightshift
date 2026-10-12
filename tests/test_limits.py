import datetime

import pytest

from claude_nightshift.limits import looks_like_limit, parse_renewal_time


@pytest.mark.parametrize(
    "line",
    [
        "Claude usage limit reached. Your limit will reset at 5pm",
        "You've hit your limit - resets 3pm",
        "You have reached your weekly usage limit",
    ],
)
def test_looks_like_limit(line):
    assert looks_like_limit(line)


def test_ordinary_output_is_not_a_limit():
    assert not looks_like_limit("Fixed the rate limiter in api/limits.py")


def test_parse_renewal_time_clock_is_in_the_future():
    target = parse_renewal_time("You've hit your limit - resets 3pm")
    assert target is not None
    assert target.hour == 15
    assert target.minute == 0
    # Either today or rolled over to tomorrow, never left in the past.
    assert target > datetime.datetime.now() - datetime.timedelta(minutes=31)


def test_parse_renewal_time_with_minutes():
    target = parse_renewal_time("Your limit will be renewed at 10:30 PM")
    assert (target.hour, target.minute) == (22, 30)


def test_parse_renewal_time_iso_is_converted_to_local():
    target = parse_renewal_time("resets at 2030-03-07T05:00:00Z")
    expected = (
        datetime.datetime(2030, 3, 7, 5, tzinfo=datetime.timezone.utc)
        .astimezone()
        .replace(tzinfo=None)
    )
    assert target == expected


def test_parse_renewal_time_without_a_time():
    assert parse_renewal_time("nothing to see here") is None
