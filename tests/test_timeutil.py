import argparse

import pytest

from claude_nightshift.timeutil import parse_duration, parse_iso_epoch


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("20h", 72000),
        ("90m", 5400),
        ("2d", 172800),
        ("600s", 600),
        ("1.5", 5400),
        (" 3H ", 10800),
    ],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "h", "20x", "-5h", "twenty"])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration(text)


def test_parse_iso_epoch_reads_z_and_naive_as_utc():
    assert parse_iso_epoch("1970-01-01T00:01:00Z") == 60
    assert parse_iso_epoch("1970-01-01T00:01:00") == 60
    assert parse_iso_epoch("1970-01-01T02:01:00+02:00") == 60


@pytest.mark.parametrize("value", [None, "", "not a date"])
def test_parse_iso_epoch_returns_none_for_bad_input(value):
    assert parse_iso_epoch(value) is None
