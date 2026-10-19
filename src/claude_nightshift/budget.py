"""How much of the weekly pool a single run may consume."""

from __future__ import annotations

import argparse
import json
import os
import time

from claude_nightshift.logsetup import get_logger
from claude_nightshift.timeutil import fmt_epoch

__all__ = [
    "Budget",
    "FIVE_HOURS",
    "WEEK",
    "DEFAULT_MIN_WINDOW_LEFT",
    "MAX_START_UTIL_FOR_SAMPLE",
    "MIN_WINDOW_SECONDS",
    "DEFAULT_WINDOW_COST",
    "MAX_COST_SAMPLES",
    "state_path",
    "load_cost_samples",
    "save_cost_samples",
    "max_windows_arg",
    "spend_arg",
    "week_of_arg",
    "median",
]

logger = get_logger(__name__)

FIVE_HOURS = 5 * 3600
WEEK = 7 * 24 * 3600
MAX_COST_SAMPLES = 20

# Fallback used only until the first full window has been measured.
DEFAULT_WINDOW_COST = 8.0  # % of the weekly pool one 5-hour window costs

# A session that hits the 5-hour limit sooner than this did not get a window -
# it got the tail of one that was already spent before the run started. It is
# neither counted against the window budget nor used as a cost sample.
MIN_WINDOW_SECONDS = 900

# A cost sample is only the price of a *whole* window if the window was near
# empty when the session started. Above this 5-hour utilisation at the gate,
# the weekly delta measures a fragment and would drag the median down.
MAX_START_UTIL_FOR_SAMPLE = 25.0

# How much of the 5-hour window must be left for a session to be worth starting.
DEFAULT_MIN_WINDOW_LEFT = 5.0

DEFAULT_STATE_PATH = "~/.claude/nightshift-state.json"
STATE_PATH_ENV = "NIGHTSHIFT_STATE_PATH"


def state_path() -> str:
    """Where measured window costs are persisted.

    Overridable with ``$NIGHTSHIFT_STATE_PATH`` so tests — and users with an
    unusual ``$HOME`` — do not have to touch the real file.
    """
    return os.path.expanduser(os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def max_windows_arg(value):
    """Parse --max-windows: a positive integer, or the literal 'auto'."""
    if str(value).strip().lower() == "auto":
        return "auto"
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--max-windows takes a positive integer or 'auto', got {value!r}"
        ) from None
    if count < 1:
        raise argparse.ArgumentTypeError("--max-windows must be at least 1")
    return count


def spend_arg(value):
    """Parse --spend: a share of the weekly pool in percent, or 'auto'."""
    if str(value).strip().lower() == "auto":
        return "auto"
    try:
        pct = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--spend takes a percentage or 'auto', got {value!r}"
        ) from None
    if not 0 < pct <= 100:
        raise argparse.ArgumentTypeError("--spend must be above 0 and at most 100")
    return pct


def week_of_arg(value):
    """Parse --week-of: unix epoch seconds, or 'now'."""
    if str(value).strip().lower() == "now":
        return time.time()
    try:
        return float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--week-of takes unix epoch seconds or 'now', got {value!r}"
        ) from None


def median(values):
    """Median of a list, or None if empty. Robust to the odd runaway session."""
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return None
    mid = count // 2
    if count % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def load_cost_samples():
    """Previously measured per-window costs, in weekly percent."""
    try:
        with open(state_path()) as f:
            data = json.load(f) or {}
        samples = [float(x) for x in data.get("window_cost_samples", [])]
        return [s for s in samples if 0 < s < 50]
    except (OSError, ValueError, TypeError):
        return []


def save_cost_samples(samples) -> None:
    """Persist the rolling cost samples, keeping any other state intact."""
    path = state_path()
    try:
        data = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f) or {}
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data["window_cost_samples"] = samples[-MAX_COST_SAMPLES:]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug(f"Could not persist window-cost state: {exc}")


class Budget:
    """How much of the weekly pool a single run may consume.

    Five independent rules, all checked before a session starts:

      1. Threshold    — never cross ``--threshold``. Always on.
      2. Run ceiling  — with ``--max-windows auto``, this run gets only the share
                        of the remaining pool that its ``--horizon`` is worth, so
                        one long task cannot eat the whole week.
      3. Window count — stop after ``--max-windows`` five-hour windows.
      4. Spend        - with ``--spend``, stop once this run's own share of the
                        weekly pool (see :mod:`claude_nightshift.ledger`) reaches
                        the cap. ``auto`` splits what is left below the
                        threshold evenly between ``--spend-share`` runs.
      5. Week         - with ``--week-of``, stop once the weekly limit has reset
                        after that moment, so the run does not go on into the
                        next week's pool.

    Rules 2 and 3 need to know what one full 5-hour window costs in weekly
    percent. That is measured, not guessed: the weekly figure is read before and
    after every window that ends on a limit, and the median of recent samples is
    kept in the state file so later runs start out calibrated.
    """

    def __init__(self, threshold, max_windows=None, horizon=None, predictive=False,
                 spend=None, spend_share=1, ledger=None, week_of=None):
        self.threshold = threshold
        self.week_of = week_of  # unix epoch seconds, or None
        self.requested_spend = spend  # float, "auto", or None
        self.spend_cap = spend if isinstance(spend, (int, float)) else None
        self.spend_share = max(1, int(spend_share or 1))
        self.ledger = ledger
        self.horizon = horizon or 12 * 3600
        self.requested_windows = max_windows  # int, "auto", or None
        self.max_windows = None if max_windows == "auto" else max_windows
        self.windows_used = 0

        # Tightened by plan() in auto mode; otherwise the threshold is the ceiling.
        self.run_ceiling = threshold

        # Refuse to *start* a window predicted to overshoot, rather than noticing
        # only once it already has.
        self.predictive = bool(predictive or max_windows is not None)

        # Why the last check() refused: "run_budget" (this run's share is spent:
        # --max-windows or the auto run ceiling), "weekly_limit" (--threshold)
        # or "week_ended" (--week-of).
        self.stop_kind = None

        self.samples = load_cost_samples()
        measured = median(self.samples)
        self.window_cost = measured if measured else DEFAULT_WINDOW_COST
        self.cost_is_measured = measured is not None
        self._planned = False

    def plan(self, usage) -> None:
        """Derive the run ceiling and window count from the first usage reading.

        Called at the first gate, once, and only does real work in auto mode.
        """
        if self._planned or usage is None or usage.weekly_pct is None:
            return
        self._planned = True

        if self.requested_spend == "auto":
            left = max(0.0, self.threshold - usage.weekly_pct)
            self.spend_cap = round(left / self.spend_share, 2)
            logger.info(
                f"  Spend budget  : {self.spend_cap:.1f}% of the weekly pool "
                f"({left:.1f}% left below the threshold, split {self.spend_share} way(s))"
            )
            if self.ledger:
                self.ledger.set_cap(self.spend_cap)

        if self.requested_windows != "auto":
            return

        remaining = max(0.0, 100.0 - usage.weekly_pct)
        time_left = usage.weekly_resets - time.time() if usage.weekly_resets else None

        # An even spread of what is left over the time that is left: this run is
        # entitled to the fraction of the pool that its horizon is of the window.
        share = 1.0 if not time_left or time_left <= 0 else min(1.0, self.horizon / time_left)
        allowance = remaining * share

        self.run_ceiling = min(self.threshold, usage.weekly_pct + allowance)

        # --threshold may have clamped the ceiling below what the even spread
        # allowed; the window count has to follow the ceiling that will actually
        # be enforced, not the allowance we started from.
        allowance = self.run_ceiling - usage.weekly_pct

        # Never plan more windows than the budget buys, nor more than fit in the
        # horizon; but always allow at least one, and let --threshold be the thing
        # that says "no, not even one".
        by_budget = int(allowance // self.window_cost)
        by_horizon = int(self.horizon // FIVE_HOURS)
        self.max_windows = max(1, min(by_budget, max(1, by_horizon)))

        left = f"{time_left / 3600:.0f}h" if time_left else "unknown time"
        estimate = "measured" if self.cost_is_measured else "estimated"
        logger.info(
            f"  Auto budget   : {allowance:.1f}% of the weekly pool "
            f"({remaining:.1f}% left, {self.horizon / 3600:.0f}h horizon of {left} "
            f"to reset)"
        )
        logger.info(
            f"  Window budget : {self.max_windows} × 5h window(s) at "
            f"~{self.window_cost:.1f}%/window ({estimate})"
        )
        logger.info(
            f"  Run ceiling   : stop before 7-day usage reaches {self.run_ceiling:.1f}%"
        )

    def week_ends(self, usage):
        """When the week this run belongs to ends: the weekly reset time while
        that week is still current, else None. Only with ``--week-of``."""
        if self.week_of is None or usage is None or not usage.weekly_resets:
            return None
        if usage.weekly_resets - WEEK > self.week_of:
            return None
        return usage.weekly_resets

    def check(self, usage):
        """May another session start? Returns ``(allowed, reason)``."""
        pct = usage.weekly_pct
        self.stop_kind = None

        # The current week began after --week-of: the week this run was
        # started for is over, and the new one is not its to spend.
        if self.week_of is not None and usage.weekly_resets and not self.week_ends(usage):
            self.stop_kind = "week_ended"
            return False, (
                f"the weekly limit reset at {fmt_epoch(usage.weekly_resets - WEEK)}, "
                f"after this run's week began (--week-of)"
            )

        if self.max_windows is not None and self.windows_used >= self.max_windows:
            self.stop_kind = "run_budget"
            return False, (
                f"window budget spent — {self.windows_used} of "
                f"{self.max_windows} five-hour window(s) used"
            )

        name = "run ceiling" if self.run_ceiling < self.threshold else "threshold"
        refused_kind = "run_budget" if name == "run ceiling" else "weekly_limit"

        if pct >= self.run_ceiling:
            self.stop_kind = "weekly_limit" if pct >= self.threshold else refused_kind
            return False, (
                f"7-day usage {pct:.1f}% is at or above the {name} "
                f"{self.run_ceiling:.1f}%"
            )

        if self.spend_cap is not None and self.ledger:
            spent = self.ledger.spent
            if spent >= self.spend_cap:
                self.stop_kind = "run_budget"
                return False, (
                    f"spend budget used - this run spent {spent:.1f}% of the "
                    f"weekly pool, its budget is {self.spend_cap:.1f}%"
                )

        if self.predictive:
            projected = pct + self.window_cost
            if projected > self.run_ceiling:
                note = (
                    ""
                    if self.cost_is_measured
                    else " — estimated, refined after the first full window"
                )
                self.stop_kind = refused_kind
                return False, (
                    f"another window would reach ~{projected:.1f}% "
                    f"({pct:.1f}% + {self.window_cost:.1f}%{note}), past the "
                    f"{name} {self.run_ceiling:.1f}%"
                )

        detail = f"7-day usage {pct:.1f}%, {name} {self.run_ceiling:.1f}%"
        if self.max_windows is not None:
            detail += f", window {self.windows_used + 1}/{self.max_windows}"
        if self.spend_cap is not None and self.ledger:
            detail += f", spent {self.ledger.spent:.1f}% of {self.spend_cap:.1f}%"
        return True, detail

    def note_window(self, before, after, duration=None) -> None:
        """Count one consumed 5-hour window and learn what it cost.

        ``before`` is the gate reading, ``after`` the last rate_limit_event of the
        session, ``duration`` its wall-clock seconds. When a reading is unusable
        the window still counts, but no sample is recorded.

        A session that hit the limit within ``MIN_WINDOW_SECONDS`` ran in a
        window that was already spent. Charging the budget for it can end a run
        on its first minute, so it is not counted at all.
        """
        if duration is not None and duration < MIN_WINDOW_SECONDS:
            logger.info(
                f"[budget] limit hit after {duration:.0f}s - the 5-hour window was "
                f"already spent, not counting it as a window"
            )
            return

        self.windows_used += 1

        if not (before and after):
            return
        if before.weekly_pct is None or after.weekly_pct is None:
            return

        # Only a window entered near-empty prices a whole window.
        if (
            before.five_hour_pct is not None
            and before.five_hour_pct > MAX_START_UTIL_FOR_SAMPLE
        ):
            return

        delta = after.weekly_pct - before.weekly_pct
        # A negative delta means the weekly window reset mid-run; an absurd one
        # means the readings are not comparable. Neither is a usable sample.
        if not 0 < delta < 50:
            return

        self.samples.append(round(delta, 2))
        save_cost_samples(self.samples)
        self.window_cost = median(self.samples[-MAX_COST_SAMPLES:])
        self.cost_is_measured = True
        logger.info(
            f"[budget] window {self.windows_used} cost {delta:.1f}% of the weekly "
            f"pool (estimate now {self.window_cost:.1f}% from "
            f"{len(self.samples[-MAX_COST_SAMPLES:])} sample(s))"
        )
