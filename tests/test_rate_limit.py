"""A rate-limited usage endpoint (HTTP 429) at the gate.

The failure seen on 2026-11-24: a run woke at the 5-hour reset, the usage
endpoint answered 429 for three minutes, and the run stopped with exit 5
("weekly usage could not be determined") with hours of work ahead of it.
"""

import io
import time
import urllib.error

import pytest

from claude_nightshift import control, runner
from claude_nightshift import usage as usage_mod
from claude_nightshift.budget import Budget
from claude_nightshift.runner import (
    RATE_LIMIT_MAX_STEP,
    RATE_LIMIT_PATIENCE,
    RATE_LIMIT_STEP,
    Outcome,
    rate_limit_wait,
    run,
)
from claude_nightshift.session import SessionResult
from claude_nightshift.usage import Usage


def http_error(code, headers=None):
    return urllib.error.HTTPError(usage_mod.USAGE_ENDPOINT, code, "err", headers or {},
                                  io.BytesIO(b""))


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(usage_mod, "interruptible_sleep", lambda _s: False)


def failing_native(monkeypatch, error):
    def native():
        raise error

    monkeypatch.setattr(usage_mod, "fetch_usage_native", native)
    monkeypatch.setattr(usage_mod, "fetch_usage_cclimits",
                        lambda: (_ for _ in ()).throw(RuntimeError("no cclimits")))


# 1. fetch_usage says why it failed


def test_a_429_is_reported_with_its_retry_after(monkeypatch, no_sleep):
    error = http_error(429, {"Retry-After": "120"})
    failing_native(monkeypatch, error)
    try:
        assert usage_mod.fetch_usage(attempts=2) is None
    finally:
        error.close()
    assert usage_mod.rate_limited_retry_after() == 120


def test_a_429_without_retry_after_reports_zero(monkeypatch, no_sleep):
    error = http_error(429)
    failing_native(monkeypatch, error)
    try:
        assert usage_mod.fetch_usage(attempts=1) is None
    finally:
        error.close()
    assert usage_mod.rate_limited_retry_after() == 0


def test_other_failures_are_not_rate_limits(monkeypatch, no_sleep):
    error = http_error(500)
    failing_native(monkeypatch, error)
    try:
        assert usage_mod.fetch_usage(attempts=1) is None
    finally:
        error.close()
    assert usage_mod.rate_limited_retry_after() is None


def test_a_reading_clears_the_rate_limit(monkeypatch, no_sleep):
    monkeypatch.setattr(usage_mod, "_rate_limited_retry_after", 0)
    monkeypatch.setattr(usage_mod, "fetch_usage_native",
                        lambda: Usage(weekly_pct=5, source="test"))
    assert usage_mod.fetch_usage().weekly_pct == 5
    assert usage_mod.rate_limited_retry_after() is None


def test_retry_after_as_an_http_date():
    from email.utils import formatdate

    seconds = usage_mod._retry_after_seconds(
        {"Retry-After": formatdate(time.time() + 600, usegmt=True)})
    assert 590 <= seconds <= 600
    assert usage_mod._retry_after_seconds({"Retry-After": "soon"}) == 0


# 2. How long the gate waits


def test_waits_grow_to_a_ceiling_and_end_with_the_patience():
    waited, waits, steps = 0.0, 0, []
    while (wait := rate_limit_wait(waited, waits, 0)) is not None:
        steps.append(wait)
        waited += wait
        waits += 1
    assert steps[:3] == [RATE_LIMIT_STEP, 2 * RATE_LIMIT_STEP, 3 * RATE_LIMIT_STEP]
    assert max(steps) == RATE_LIMIT_MAX_STEP
    assert sum(steps) == RATE_LIMIT_PATIENCE


def test_a_longer_retry_after_is_honoured_within_the_patience():
    assert rate_limit_wait(0, 0, 1200) == 1200
    assert rate_limit_wait(RATE_LIMIT_PATIENCE - 60, 5, 1200) == 60


# 3. The run


def success_session():
    s = SessionResult()
    s.returncode = 0
    s.result = {"type": "result", "subtype": "success", "is_error": False,
                "num_turns": 3, "duration_ms": 1000}
    s.duration = 1
    return s


class Gate:
    """Feeds run() gate readings; None stands for a lookup that failed, with
    ``limited`` telling whether it failed on a 429."""

    def __init__(self, monkeypatch, readings, limited=True):
        self.readings = iter(readings)
        self.limited = limited
        self.last = None
        self.sleeps = []
        monkeypatch.setattr(control, "interruptible_sleep",
                            lambda secs: self.sleeps.append(secs) or False)
        monkeypatch.setattr(runner, "fetch_usage", self.fetch)
        monkeypatch.setattr(runner, "rate_limited_retry_after",
                            lambda: 0 if self.last is None and self.limited else None)
        monkeypatch.setattr(runner, "run_session",
                            lambda cmd, structured=True, ledger=None: success_session())

    def fetch(self, *a, **k):
        self.last = next(self.readings)
        return self.last


def reading(weekly=40.0):
    return Usage(weekly_pct=weekly, five_hour_pct=0.0, source="test")


def test_regression_a_rate_limited_endpoint_does_not_end_the_run(monkeypatch):
    gate = Gate(monkeypatch, [None, None, reading()])
    outcome = run(["claude", "-p", "do the thing"], Budget(100.0))
    assert outcome == Outcome.COMPLETED
    assert gate.sleeps == [RATE_LIMIT_STEP, 2 * RATE_LIMIT_STEP]


def test_the_run_gives_up_after_the_patience(monkeypatch):
    gate = Gate(monkeypatch, [None] * 50)
    outcome = run(["claude", "-p", "do the thing"], Budget(100.0))
    assert outcome == Outcome.USAGE_UNKNOWN
    assert sum(gate.sleeps) == RATE_LIMIT_PATIENCE


def test_an_unreadable_usage_that_is_not_a_rate_limit_still_stops(monkeypatch):
    gate = Gate(monkeypatch, [None], limited=False)
    outcome = run(["claude", "-p", "do the thing"], Budget(100.0))
    assert outcome == Outcome.USAGE_UNKNOWN
    assert gate.sleeps == []
