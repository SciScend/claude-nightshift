"""How much of the weekly pool a single run may consume."""

from __future__ import annotations

import argparse
import json
import os
import time

from claude_nightshift.logsetup import get_logger

__all__ = [
    "Budget",
    "FIVE_HOURS",
    "DEFAULT_WINDOW_COST",
    "MAX_COST_SAMPLES",
    "state_path",
    "load_cost_samples",
    "save_cost_samples",
    "max_windows_arg",
    "median",
]

logger = get_logger(__name__)

FIVE_HOURS = 5 * 3600
MAX_COST_SAMPLES = 20

# Fallback used only until the first full window has been measured.
DEFAULT_WINDOW_COST = 8.0  # % of the weekly pool one 5-hour window costs

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

    Three independent rules, all checked before a session starts:

      1. Threshold    — never cross ``--threshold``. Always on.
      2. Run ceiling  — with ``--max-windows auto``, this run gets only the share
                        of the remaining pool that its ``--horizon`` is worth, so
                        one long task cannot eat the whole week.
      3. Window count — stop after ``--max-windows`` five-hour windows.

    Rules 2 and 3 need to know what one full 5-hour window costs in weekly
    percent. That is measured, not guessed: the weekly figure is read before and
    after every window that ends on a limit, and the median of recent samples is
    kept in the state file so later runs start out calibrated.
    """

    def __init__(self, threshold, max_windows=None, horizon=None, predictive=False):
        self.threshold = threshold
        self.horizon = horizon or 12 * 3600
        self.requested_windows = max_windows  # int, "auto", or None
        self.max_windows = None if max_windows == "auto" else max_windows
        self.windows_used = 0

        # Tightened by plan() in auto mode; otherwise the threshold is the ceiling.
        self.run_ceiling = threshold

        # Refuse to *start* a window predicted to overshoot, rather than noticing
        # only once it already has.
        self.predictive = bool(predictive or max_windows is not None)

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

    def check(self, usage):
        """May another session start? Returns ``(allowed, reason)``."""
        pct = usage.weekly_pct

        if self.max_windows is not None and self.windows_used >= self.max_windows:
            return False, (
                f"window budget spent — {self.windows_used} of "
                f"{self.max_windows} five-hour window(s) used"
            )

        name = "run ceiling" if self.run_ceiling < self.threshold else "threshold"

        if pct >= self.run_ceiling:
            return False, (
                f"7-day usage {pct:.1f}% is at or above the {name} "
                f"{self.run_ceiling:.1f}%"
            )

        if self.predictive:
            projected = pct + self.window_cost
            if projected > self.run_ceiling:
                note = (
                    ""
                    if self.cost_is_measured
                    else " — estimated, refined after the first full window"
                )
                return False, (
                    f"another window would reach ~{projected:.1f}% "
                    f"({pct:.1f}% + {self.window_cost:.1f}%{note}), past the "
                    f"{name} {self.run_ceiling:.1f}%"
                )

        detail = f"7-day usage {pct:.1f}%, {name} {self.run_ceiling:.1f}%"
        if self.max_windows is not None:
            detail += f", window {self.windows_used + 1}/{self.max_windows}"
        return True, detail

    def note_window(self, before, after) -> None:
        """Count one consumed 5-hour window and learn what it cost.

        ``before`` is the gate reading, ``after`` the last rate_limit_event of the
        session. When either is unusable the window still counts, but no sample
        is recorded.
        """
        self.windows_used += 1

        if not (before and after):
            return
        if before.weekly_pct is None or after.weekly_pct is None:
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
