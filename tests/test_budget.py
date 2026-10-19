import argparse
import json
import time

import pytest

from claude_nightshift.budget import (
    DEFAULT_WINDOW_COST,
    MAX_COST_SAMPLES,
    WEEK,
    Budget,
    load_cost_samples,
    max_windows_arg,
    median,
    save_cost_samples,
    week_of_arg,
)
from claude_nightshift.usage import Usage


def test_max_windows_arg():
    assert max_windows_arg("auto") == "auto"
    assert max_windows_arg(" AUTO ") == "auto"
    assert max_windows_arg("3") == 3
    for bad in ("0", "-1", "two"):
        with pytest.raises(argparse.ArgumentTypeError):
            max_windows_arg(bad)


def test_median():
    assert median([]) is None
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5


def test_cost_samples_round_trip_and_keep_other_state(isolated_state):
    isolated_state.write_text(json.dumps({"other": 1}))
    save_cost_samples(list(range(1, 30)))
    data = json.loads(isolated_state.read_text())
    assert data["other"] == 1
    assert len(data["window_cost_samples"]) == MAX_COST_SAMPLES
    # Samples outside (0, 50) are dropped on load.
    assert all(0 < s < 50 for s in load_cost_samples())


def test_load_cost_samples_survives_a_corrupt_file(isolated_state):
    isolated_state.write_text("{not json")
    assert load_cost_samples() == []


def test_threshold_blocks_a_new_session():
    budget = Budget(threshold=70)
    allowed, reason = budget.check(Usage(weekly_pct=71))
    assert not allowed
    assert "threshold" in reason
    assert budget.check(Usage(weekly_pct=50))[0]


def test_window_count_is_enforced():
    budget = Budget(threshold=90, max_windows=2)
    usage = Usage(weekly_pct=10)
    assert budget.check(usage)[0]
    budget.note_window(None, None)
    assert budget.check(usage)[0]
    budget.note_window(None, None)
    allowed, reason = budget.check(usage)
    assert not allowed
    assert "window budget spent" in reason


def test_predictive_check_refuses_a_window_that_would_overshoot():
    budget = Budget(threshold=70, max_windows=5)
    allowed, reason = budget.check(Usage(weekly_pct=70 - DEFAULT_WINDOW_COST + 1))
    assert not allowed
    assert "another window would reach" in reason


def test_auto_plan_spreads_the_remaining_pool_over_the_horizon():
    budget = Budget(threshold=70, max_windows="auto", horizon=20 * 3600)
    usage = Usage(weekly_pct=40, weekly_resets=time.time() + 100 * 3600)
    budget.plan(usage)
    # 60% left, 20h of ~100h to reset -> ~12% allowance -> ceiling ~52%.
    assert budget.run_ceiling == pytest.approx(52, abs=0.1)
    assert budget.max_windows == 1
    assert budget.check(usage)[0]


def test_auto_plan_is_clamped_by_the_threshold():
    budget = Budget(threshold=45, max_windows="auto", horizon=100 * 3600)
    budget.plan(Usage(weekly_pct=20, weekly_resets=time.time() + 50 * 3600))
    assert budget.run_ceiling == 45
    assert budget.max_windows == 3  # 25% allowance / 8% per window


def test_note_window_learns_the_cost(isolated_state):
    budget = Budget(threshold=70)
    budget.note_window(Usage(weekly_pct=10), Usage(weekly_pct=16))
    assert budget.cost_is_measured
    assert budget.window_cost == 6
    assert json.loads(isolated_state.read_text())["window_cost_samples"] == [6]
    # A later run starts out calibrated.
    assert Budget(threshold=70).window_cost == 6


def test_note_window_ignores_a_weekly_reset():
    budget = Budget(threshold=70)
    budget.note_window(Usage(weekly_pct=60), Usage(weekly_pct=2))
    assert budget.windows_used == 1
    assert not budget.cost_is_measured


def test_week_of_arg():
    assert week_of_arg("1700000000") == 1700000000.0
    assert week_of_arg("now") == pytest.approx(time.time(), abs=5)
    with pytest.raises(argparse.ArgumentTypeError):
        week_of_arg("tomorrow")


def test_week_of_stops_once_the_weekly_limit_has_reset():
    now = time.time()
    budget = Budget(threshold=80, week_of=now - 3600)
    # Still the week the run began in: the reset is ahead.
    old_week = Usage(weekly_pct=63, weekly_resets=now + 20 * 3600)
    assert budget.check(old_week)[0]
    assert budget.week_ends(old_week) == old_week.weekly_resets
    # The limit reset ten minutes ago; the next reset is a week after it.
    new_week = Usage(weekly_pct=1, weekly_resets=now - 600 + WEEK)
    allowed, reason = budget.check(new_week)
    assert not allowed
    assert budget.stop_kind == "week_ended"
    assert "--week-of" in reason
    assert budget.week_ends(new_week) is None


def test_week_of_needs_a_reset_time_and_is_off_by_default():
    now = time.time()
    new_week = Usage(weekly_pct=1, weekly_resets=now - 600 + WEEK)
    assert Budget(threshold=80).check(new_week)[0]
    assert Budget(threshold=80, week_of=now - 3600).check(Usage(weekly_pct=1))[0]
