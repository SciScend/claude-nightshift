"""Tests for the 5-hour gate and window accounting.

Focused on the failure seen on 2026-09-04: a run started nine minutes before a
5-hour reset, died on the limit after 337s, and had that stub charged to a
one-window budget - ending a 10-hour run after five minutes and recording the
stub as the measured price of a window.
"""

import json
import time

import pytest

from claude_nightshift import control, runner
from claude_nightshift.budget import (
    DEFAULT_MIN_WINDOW_LEFT,
    FIVE_HOURS,
    MIN_WINDOW_SECONDS,
    Budget,
)
from claude_nightshift.runner import MAX_WINDOW_DEFERRALS, Outcome, run, window_ready
from claude_nightshift.session import SessionResult
from claude_nightshift.status import STATUS_FILE
from claude_nightshift.usage import Usage


def usage(weekly=24.0, five=0.0, five_resets_in=None, weekly_resets_in=None, status=None):
    """A Usage snapshot with reset times expressed relative to now."""
    now = time.time()
    return Usage(
        weekly_pct=weekly,
        weekly_resets=now + weekly_resets_in if weekly_resets_in else None,
        five_hour_pct=five,
        five_hour_resets=now + five_resets_in if five_resets_in else None,
        status=status,
        source="test",
    )


@pytest.fixture
def saved_samples(isolated_state):
    def read():
        if not isolated_state.exists():
            return []
        return json.loads(isolated_state.read_text()).get("window_cost_samples", [])

    return read


# 1. The 5-hour gate


def test_the_2026_09_04_window_is_refused():
    """The actual failure: 5h at 100%, resetting in nine minutes."""
    ready, why, reset_at = window_ready(
        usage(five=100.0, five_resets_in=9 * 60), DEFAULT_MIN_WINDOW_LEFT
    )
    assert not ready
    assert "100% spent" in why
    assert reset_at == pytest.approx(time.time() + 9 * 60, abs=5)


def test_fresh_window_is_ready():
    ready, _, _ = window_ready(
        usage(five=0.0, five_resets_in=18000), DEFAULT_MIN_WINDOW_LEFT
    )
    assert ready


def test_boundary_is_inclusive():
    """Exactly min_window_left remaining still counts as enough."""
    assert window_ready(usage(five=95.0, five_resets_in=600), 5.0)[0]
    assert not window_ready(usage(five=95.1, five_resets_in=600), 5.0)[0]


def test_disabled_by_zero():
    assert window_ready(usage(five=100.0, five_resets_in=600), 0)[0]


def test_unknown_five_hour_figure_reads_as_ready():
    """Nothing new may block a run just because a figure is missing."""
    assert window_ready(usage(five=None), DEFAULT_MIN_WINDOW_LEFT)[0]
    assert window_ready(None, DEFAULT_MIN_WINDOW_LEFT)[0]


@pytest.mark.parametrize("resets_in", [None, -60, FIVE_HOURS + 3600])
def test_spent_window_without_a_reset_time_runs_anyway(resets_in):
    """No credible time to wait for: behave as before the gate existed."""
    assert window_ready(
        usage(five=100.0, five_resets_in=resets_in), DEFAULT_MIN_WINDOW_LEFT
    )[0]


# 2. Window accounting


def test_stub_session_is_not_charged_or_sampled(saved_samples):
    """337 seconds is not a 5-hour window."""
    b = Budget(70.0, max_windows=1)
    b.note_window(
        usage(weekly=24.0, five=100.0), usage(weekly=25.0, five=100.0), duration=337
    )
    assert b.windows_used == 0
    assert saved_samples() == []
    assert not b.cost_is_measured


def test_real_window_is_charged_and_sampled(saved_samples):
    b = Budget(70.0, max_windows=1)
    b.note_window(
        usage(weekly=24.0, five=2.0), usage(weekly=31.0, five=100.0), duration=4 * 3600
    )
    assert b.windows_used == 1
    assert saved_samples() == [7.0]
    assert b.cost_is_measured
    assert b.window_cost == 7.0


def test_window_entered_half_spent_counts_but_does_not_price(saved_samples):
    """Charged (it ran), but its delta is a fragment, not a window's price."""
    b = Budget(70.0, max_windows=1)
    b.note_window(
        usage(weekly=24.0, five=60.0), usage(weekly=27.0, five=100.0), duration=2 * 3600
    )
    assert b.windows_used == 1
    assert saved_samples() == []
    assert not b.cost_is_measured


def test_boundary_duration():
    b = Budget(70.0, max_windows=1)
    b.note_window(
        usage(five=0.0), usage(weekly=30.0, five=100.0), duration=MIN_WINDOW_SECONDS - 1
    )
    assert b.windows_used == 0
    b.note_window(
        usage(five=0.0), usage(weekly=30.0, five=100.0), duration=MIN_WINDOW_SECONDS
    )
    assert b.windows_used == 1


def test_existing_sample_sanity_checks_still_apply(saved_samples):
    b = Budget(70.0, max_windows=1)
    # weekly window reset mid-run (negative delta)
    b.note_window(
        usage(weekly=90.0, five=0.0), usage(weekly=2.0, five=100.0), duration=4 * 3600
    )
    # absurd delta
    b.note_window(
        usage(weekly=1.0, five=0.0), usage(weekly=99.0, five=100.0), duration=4 * 3600
    )
    # missing readings
    b.note_window(None, usage(weekly=30.0), duration=4 * 3600)
    b.note_window(usage(weekly=24.0, five=0.0), None, duration=4 * 3600)
    assert saved_samples() == []
    assert b.windows_used == 4  # all four ran; none priced a window


def test_duration_omitted_keeps_old_behaviour(saved_samples):
    b = Budget(70.0, max_windows=1)
    b.note_window(usage(weekly=24.0, five=0.0), usage(weekly=32.0, five=100.0))
    assert b.windows_used == 1
    assert saved_samples() == [8.0]


def test_auto_plan_matches_the_documented_arithmetic():
    b = Budget(70.0, max_windows="auto", horizon=10 * 3600)
    b.plan(usage(weekly=24.0, weekly_resets_in=89 * 3600))
    # 76% left x 10/89 = 8.5% allowance; 8.5 // 8.0 (default cost) = 1 window
    assert b.max_windows == 1
    assert b.run_ceiling == pytest.approx(32.5, abs=0.05)


# 3. End to end through run()


def limit_session(duration, five_resets_in=9 * 60, weekly_after=25.0):
    """A session killed by the 5-hour limit, shaped like the real 2026-09-04 one."""
    s = SessionResult()
    s.returncode = 0
    s.session_id = "abc12345-dead-beef"
    s.result = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "num_turns": 27,
        "duration_ms": duration * 1000,
    }
    s.usage = usage(
        weekly=weekly_after, five=100.0, five_resets_in=five_resets_in, status="rejected"
    )
    s.duration = duration
    return s


def success_session(duration=1800):
    s = SessionResult()
    s.returncode = 0
    s.session_id = "abc12345-dead-beef"
    s.result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 40,
        "duration_ms": duration * 1000,
    }
    s.duration = duration
    return s


class Script:
    """Feeds run() a fixed sequence of gate readings and session outcomes."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.sleeps = []
        self.commands = []
        monkeypatch.setattr(
            control, "interruptible_sleep", lambda secs: self.sleeps.append(secs) or False
        )

    def __call__(self, usages, sessions):
        gate = iter(usages)
        turns = iter(sessions)
        self.monkeypatch.setattr(runner, "fetch_usage", lambda *a, **k: next(gate))

        def fake_run_session(cmd, structured=True, ledger=None):
            self.commands.append(cmd)
            return next(turns)

        self.monkeypatch.setattr(runner, "run_session", fake_run_session)


@pytest.fixture
def script(monkeypatch):
    return Script(monkeypatch)


def test_regression_run_survives_a_spent_window(script):
    """The 2026-09-04 failure, replayed: 5h at 100% with nine minutes to reset
    and a one-window budget. It must wait, then do the work - not burn the
    budget on a stub and stop.
    """
    script(
        usages=[
            usage(
                weekly=24.0, five=100.0, five_resets_in=9 * 60, weekly_resets_in=89 * 3600
            ),
            usage(
                weekly=24.0, five=0.0, five_resets_in=FIVE_HOURS, weekly_resets_in=89 * 3600
            ),
        ],
        sessions=[success_session()],
    )
    budget = Budget(70.0, max_windows="auto", horizon=10 * 3600)

    outcome = run(["claude", "-p", "do the thing"], budget, max_retries=10)
    assert outcome == Outcome.COMPLETED
    assert budget.max_windows == 1
    assert budget.windows_used == 0, "a wait must not cost a window"
    assert len(script.commands) == 1, "exactly one session should run"
    assert len(script.sleeps) == 1
    assert script.sleeps[0] == pytest.approx(9 * 60 + 30, abs=5)


def test_stub_session_does_not_exhaust_the_window_budget(script, saved_samples):
    """With the 5-hour gate disabled, a stub is still not charged a window, so
    the run continues into the next one instead of ending on its first minute.
    """
    script(
        usages=[
            usage(
                weekly=24.0, five=100.0, five_resets_in=9 * 60, weekly_resets_in=89 * 3600
            ),
            usage(
                weekly=25.0, five=0.0, five_resets_in=FIVE_HOURS, weekly_resets_in=89 * 3600
            ),
        ],
        sessions=[limit_session(337), success_session()],
    )
    budget = Budget(70.0, max_windows="auto", horizon=20 * 3600)

    outcome = run(
        ["claude", "-p", "do the thing"], budget, max_retries=10, min_window_left=0
    )
    assert outcome == Outcome.COMPLETED
    assert budget.windows_used == 0
    assert saved_samples() == [], "a stub prices nothing"
    assert len(script.commands) == 2, "the run must reach a real window"


def test_stub_still_counts_against_the_weekly_ceiling(script):
    """A stub consumes no *window* but it does spend real weekly budget, so a
    tight ceiling may still stop the run after one. This is why the 5-hour
    gate is on by default: it stops the stub from happening in the first place.
    """
    script(
        usages=[
            usage(
                weekly=24.0, five=100.0, five_resets_in=9 * 60, weekly_resets_in=89 * 3600
            ),
            usage(
                weekly=25.0, five=0.0, five_resets_in=FIVE_HOURS, weekly_resets_in=89 * 3600
            ),
        ],
        sessions=[limit_session(337), success_session()],
    )
    budget = Budget(70.0, max_windows="auto", horizon=10 * 3600)

    outcome = run(
        ["claude", "-p", "do the thing"], budget, max_retries=10, min_window_left=0
    )
    assert outcome == Outcome.RUN_BUDGET, "the auto run ceiling, not the weekly limit"
    assert budget.windows_used == 0, "still not a window"
    assert budget.run_ceiling == pytest.approx(32.5, abs=0.05)


def test_a_real_window_is_still_counted_and_stops_the_run(script, saved_samples):
    """The budget must still bind: one full window, one window's budget."""
    script(
        usages=[
            usage(
                weekly=24.0, five=0.0, five_resets_in=FIVE_HOURS, weekly_resets_in=89 * 3600
            ),
            usage(
                weekly=32.0, five=0.0, five_resets_in=FIVE_HOURS, weekly_resets_in=80 * 3600
            ),
        ],
        sessions=[limit_session(4 * 3600, weekly_after=32.0)],
    )
    budget = Budget(70.0, max_windows="auto", horizon=10 * 3600)

    outcome = run(["claude", "-p", "do the thing"], budget, max_retries=10)
    assert outcome == Outcome.RUN_BUDGET
    assert budget.windows_used == 1
    assert saved_samples() == [8.0]


def test_deferrals_are_bounded(script):
    """A stuck usage source must not loop forever waiting."""
    script(
        usages=[
            usage(weekly=24.0, five=100.0, five_resets_in=60, weekly_resets_in=89 * 3600)
            for _ in range(MAX_WINDOW_DEFERRALS + 1)
        ],
        sessions=[success_session()],
    )
    outcome = run(["claude", "-p", "do the thing"], Budget(70.0), max_retries=10)
    assert outcome == Outcome.COMPLETED
    assert len(script.sleeps) == MAX_WINDOW_DEFERRALS
    assert len(script.commands) == 1


def test_status_instruction_is_still_injected(script):
    script(
        usages=[usage(weekly=24.0, five=0.0, five_resets_in=FIVE_HOURS)],
        sessions=[success_session()],
    )
    run(["claude", "-p", "do the thing"], Budget(70.0), max_retries=10)
    assert STATUS_FILE in script.commands[0][-1]


# 4. How a run ends: the outcome is the exit code


def failed_session():
    s = SessionResult()
    s.returncode = 1
    s.output_text = "Error: something went badly wrong"
    s.duration = 60
    return s


def test_weekly_threshold_at_the_gate_is_a_weekly_limit(script):
    script(usages=[usage(weekly=71.0, five=0.0)], sessions=[])
    outcome = run(["claude", "-p", "do the thing"], Budget(70.0), max_retries=10)
    assert outcome == Outcome.WEEKLY_LIMIT
    assert int(outcome) == 3
    assert script.commands == []


def test_overshooting_the_threshold_is_a_weekly_limit(script):
    """-w N implies --no-overshoot against the threshold itself."""
    script(usages=[usage(weekly=65.0, five=0.0)], sessions=[])
    outcome = run(["claude", "-p", "x"], Budget(70.0, max_windows=2), max_retries=10)
    assert outcome == Outcome.WEEKLY_LIMIT


def test_max_windows_spent_is_a_run_budget_stop(script):
    script(
        usages=[
            usage(weekly=24.0, five=0.0, five_resets_in=FIVE_HOURS),
            usage(weekly=32.0, five=0.0, five_resets_in=FIVE_HOURS),
        ],
        sessions=[limit_session(4 * 3600, weekly_after=32.0)],
    )
    outcome = run(["claude", "-p", "x"], Budget(70.0, max_windows=1), max_retries=10)
    assert outcome == Outcome.RUN_BUDGET
    assert int(outcome) == 4


def test_unreadable_usage_has_its_own_outcome(script):
    script(usages=[None], sessions=[])
    outcome = run(["claude", "-p", "x"], Budget(70.0), max_retries=10)
    assert outcome == Outcome.USAGE_UNKNOWN
    assert int(outcome) == 5


def test_claude_error_is_a_failure(script):
    script(usages=[usage(weekly=24.0, five=0.0)], sessions=[failed_session()])
    outcome = run(["claude", "-p", "x"], Budget(70.0), max_retries=10)
    assert outcome == Outcome.FAILED
    assert int(outcome) == 1


def test_watchdog_stop_is_a_weekly_limit(script):
    control.set_stop("7-day usage 71% reached threshold 70%", kind="weekly_limit")
    script(usages=[], sessions=[])
    outcome = run(["claude", "-p", "x"], Budget(70.0), max_retries=10)
    assert outcome == Outcome.WEEKLY_LIMIT


def test_user_stop_is_an_interrupt(script):
    control.set_stop("interrupted by user")
    script(usages=[], sessions=[])
    outcome = run(["claude", "-p", "x"], Budget(70.0), max_retries=10)
    assert outcome == Outcome.INTERRUPTED
    assert int(outcome) == 130
