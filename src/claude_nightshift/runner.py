"""The retry loop: gate, run a session, interpret it, wait, resume."""

from __future__ import annotations

import enum
import time

from claude_nightshift import control
from claude_nightshift.budget import DEFAULT_MIN_WINDOW_LEFT, FIVE_HOURS
from claude_nightshift.logsetup import get_logger
from claude_nightshift.session import classify, run_session
from claude_nightshift.status import (
    extract_task_from_command,
    inject_prompt,
    read_status_file,
    status_instruction,
)
from claude_nightshift.timeutil import fmt_epoch
from claude_nightshift.usage import fetch_usage

__all__ = [
    "Outcome",
    "weekly_gate",
    "window_ready",
    "resolve_wait_target",
    "build_resume_command",
    "watchdog_thread",
    "spend_watch_thread",
    "run",
]

logger = get_logger(__name__)

SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"

# How long to wait when the reset time cannot be determined at all.
FALLBACK_WAIT_SECONDS = 600
# Slack added after a known reset time, so we never wake a moment too early.
RESET_GRACE_SECONDS = 30
# Consecutive pre-session waits before giving up on the 5-hour gate and running
# anyway. Bounds the loop if a usage source is stuck reporting a spent window.
MAX_WINDOW_DEFERRALS = 3


class Outcome(enum.IntEnum):
    """How a run ended. The value is the process exit code.

    1 and 2 keep their usual meanings (generic failure, and the usage error
    argparse already exits with); 130 is the shell convention for SIGINT.
    """

    COMPLETED = 0  # the task finished
    FAILED = 1  # Claude failed or retries ran out
    WEEKLY_LIMIT = 3  # --threshold reached: no budget left this week
    RUN_BUDGET = 4  # this run's window budget is spent, task unfinished
    USAGE_UNKNOWN = 5  # weekly usage could not be read, so no session was started
    WEEK_ENDED = 6  # --week-of: the weekly limit reset, the new week is not this run's
    INTERRUPTED = 130  # stopped by the user


def _stopped_outcome():
    """Map a stop request (watchdog or SIGINT) to an Outcome."""
    if control.stop_kind() == "weekly_limit":
        return Outcome.WEEKLY_LIMIT
    if control.stop_kind() == "run_budget":
        return Outcome.RUN_BUDGET
    return Outcome.INTERRUPTED


def watchdog_thread(threshold, interval) -> None:
    """Poll usage and kill a *running* session at the threshold (``--hard-stop``).

    Off by default: it destroys in-progress work and prevents the status file
    from being written.
    """
    logger.info(
        f"[watchdog] Hard stop armed — threshold={threshold}%, interval={interval}s"
    )

    while True:
        if control.interruptible_sleep(interval):
            break

        usage = fetch_usage(attempts=1)
        if usage is None or usage.weekly_pct is None:
            continue

        logger.info(f"[watchdog] 7-day usage: {usage.weekly_pct:.1f}%")

        if usage.weekly_pct >= threshold:
            reason = (
                f"7-day usage {usage.weekly_pct:.1f}% reached threshold {threshold}%"
            )
            logger.warning(f"[watchdog] 🛑 {reason} — stopping Claude")
            control.set_stop(reason, kind="weekly_limit")
            control.terminate_process(control.get_current_process())
            break

    logger.info("[watchdog] Exited")


def spend_watch_thread(budget, interval) -> None:
    """Stop a *running* session once this run's ``--spend`` budget is used.

    Unlike the threshold, a run's spend budget is enforced during a session:
    with several runs sharing the pool, one that ran on past its budget would
    spend what the others were given. The usage endpoint is asked only when no
    run has recorded a reading in the last ``interval`` seconds.
    """
    ledger = budget.ledger
    while not control.interruptible_sleep(interval):
        if control.get_current_process() is None or budget.spend_cap is None:
            continue
        age = ledger.last_reading_age()
        if age is None or age >= interval:
            ledger.observe(fetch_usage(attempts=1))
        else:
            # Pick up what the other runs' readings attributed to this one.
            ledger.refresh()
        if ledger.spent >= budget.spend_cap:
            reason = (
                f"this run spent {ledger.spent:.1f}% of the weekly pool, its "
                f"budget is {budget.spend_cap:.1f}%"
            )
            logger.warning(f"[spend] {reason} - stopping Claude")
            control.set_stop(reason, kind="run_budget")
            control.terminate_process(control.get_current_process())
            break


def _week_ends_before(budget, usage, target) -> bool:
    """Would waiting until ``target`` outlast this run's week (``--week-of``)?
    Then there is nothing to wait for: the run would only stop when it wakes."""
    ends = budget.week_ends(usage)
    if not (ends and target and target >= ends):
        return False
    logger.warning(
        f"The weekly limit resets at {fmt_epoch(ends)}, before the wait ends "
        f"at {fmt_epoch(target)}; this run's week is over (--week-of)"
    )
    return True


def weekly_gate(budget, cached, max_age, allow_unknown):
    """Check whether another session may start.

    Uses ``cached`` (from the last rate_limit_event) when it is younger than
    ``max_age`` seconds, otherwise queries the usage endpoint.
    Returns ``(allowed, reason, usage_or_None)``.
    """
    fresh_enough = cached and cached.weekly_pct is not None and cached.age <= max_age
    usage = cached if fresh_enough else fetch_usage()

    if usage is None or usage.weekly_pct is None:
        if allow_unknown:
            return True, "weekly usage unknown — proceeding (--allow-unknown-usage)", None
        return (
            False,
            "weekly usage could not be determined "
            "(use --allow-unknown-usage to proceed anyway)",
            None,
        )

    if budget.ledger:
        budget.ledger.observe(usage)
    budget.plan(usage)
    allowed, reason = budget.check(usage)
    return allowed, reason, usage


def window_ready(usage, min_left):
    """Is there enough of the 5-hour window left to be worth starting a session?

    The weekly gate answers "is there budget this week?"; it says nothing about
    the window the next session would actually run in. Starting nine minutes
    before a reset buys a session that dies on the limit having done nothing.

    Returns ``(ready, reason, reset_at)``. Anything this cannot establish reads
    as ready - the gate only ever fires on a figure it really has, so an
    unreadable 5-hour window behaves as it did before.
    """
    if min_left <= 0 or usage is None or usage.five_hour_pct is None:
        return True, "", None

    left = 100.0 - usage.five_hour_pct
    if left >= min_left:
        return True, "", None

    reset_at = usage.five_hour_resets
    now = time.time()
    # No credible reset time to wait for: let the session run and let the limit
    # handling deal with it, exactly as before this gate existed.
    if not reset_at or reset_at <= now or reset_at - now > FIVE_HOURS + 600:
        return True, "", None

    return False, (
        f"5-hour window is {usage.five_hour_pct:.0f}% spent "
        f"({left:.0f}% left, need {min_left:.0f}%)"
    ), reset_at


def resolve_wait_target(session):
    """Work out when the 5-hour window resets. Unix epoch seconds, or None.

    Prefers the exact reset time Claude streamed to us, then the usage endpoint,
    then falls back to parsing the message text.
    """
    if session.usage and session.usage.five_hour_resets:
        return session.usage.five_hour_resets

    usage = fetch_usage(attempts=2)
    if usage and usage.five_hour_resets:
        return usage.five_hour_resets

    from claude_nightshift.limits import parse_renewal_time

    parsed = parse_renewal_time(session.output_text)
    return parsed.timestamp() if parsed else None


def build_resume_command(retries, compress_after, session_id, original_task, base_flags):
    """Build the command for resume attempt number ``retries``."""
    cmd = ["claude", SKIP_PERMISSIONS_FLAG] + list(base_flags)

    if retries < compress_after and session_id:
        # Full context resume — continue the real conversation by id.
        cmd.extend(["--resume", session_id, "-p", "continue" + status_instruction()])
        return cmd, f"full context (--resume {session_id[:8]})"

    if retries < compress_after:
        # No session id captured; fall back to "most recent in this directory".
        cmd.extend(["-c", "-p", "continue" + status_instruction()])
        return cmd, "full context (-c)"

    # Compressed context resume — fresh start seeded with the status summary.
    status = read_status_file()
    parts = []
    if original_task:
        parts.append(f"Original task: {original_task}")
    if status:
        parts.append(f"Progress from previous session:\n\n{status}")
    parts.append("Continue where you left off." + status_instruction())
    cmd.extend(["-p", "\n\n".join(parts)])
    return cmd, "compressed context (fresh)"


def run(
    command,
    budget,
    max_retries=10,
    compress_after=3,
    base_flags=(),
    structured=True,
    usage_max_age=120,
    allow_unknown=False,
    min_window_left=DEFAULT_MIN_WINDOW_LEFT,
):
    """Run `command` to completion, waiting out limits. Returns an :class:`Outcome`."""
    original_task = extract_task_from_command(command)

    # Inject the status-file instruction so Claude writes progress before the
    # session ends; that summary is what a compressed resume is seeded with.
    if original_task:
        command = inject_prompt(command, status_instruction())

    first_run = True
    retries = 0
    deferrals = 0
    session_id = None
    cached_usage = None
    window_start_usage = None

    while retries < max_retries:
        if control.should_stop():
            logger.info(f"Stopped before retry: {control.stop_reason()}")
            return _stopped_outcome()

        # --- the gate: may we start another session? ---
        allowed, reason, usage = weekly_gate(
            budget, cached_usage, usage_max_age, allow_unknown
        )
        if usage:
            cached_usage = usage
            window_start_usage = usage
        if not allowed:
            logger.warning(f"🛑 Not starting another session: {reason}")
            if budget.stop_kind == "run_budget":
                return Outcome.RUN_BUDGET
            if budget.stop_kind == "weekly_limit":
                return Outcome.WEEKLY_LIMIT
            if budget.stop_kind == "week_ended":
                return Outcome.WEEK_ENDED
            if usage is None:
                return Outcome.USAGE_UNKNOWN
            return Outcome.FAILED
        logger.info(f"Gate open: {reason}")

        # --- the 5-hour gate: is there a window to run in? ---
        ready, why, reset_at = window_ready(usage, min_window_left)
        if not ready and deferrals >= MAX_WINDOW_DEFERRALS:
            logger.warning(
                f"{why}, but {deferrals} wait(s) did not clear it - starting anyway"
            )
        elif not ready and _week_ends_before(budget, usage, reset_at):
            return Outcome.WEEK_ENDED
        elif not ready:
            deferrals += 1
            wait_seconds = max(0.0, reset_at - time.time()) + RESET_GRACE_SECONDS
            logger.warning(
                f"{why}. Waiting for it to reset at {fmt_epoch(reset_at)} "
                f"(~{wait_seconds / 60:.1f} minutes) rather than spending a "
                f"window on it."
            )
            if control.interruptible_sleep(wait_seconds):
                logger.info(f"Stopped during wait: {control.stop_reason()}")
                return _stopped_outcome()
            cached_usage = None  # stale after the wait; force a fresh reading
            continue
        else:
            deferrals = 0

        if first_run:
            cmd = list(command)
            if SKIP_PERMISSIONS_FLAG not in cmd:
                cmd.insert(1, SKIP_PERMISSIONS_FLAG)
            first_run = False
        else:
            cmd, strategy = build_resume_command(
                retries, compress_after, session_id, original_task, base_flags
            )
            logger.info(f"Resume {retries} using {strategy}")

        logger.info(f"Running: {' '.join(cmd)}")

        ledger = budget.ledger
        if ledger:
            ledger.session(True)
        try:
            session = run_session(cmd, structured=structured, ledger=ledger)
        finally:
            if ledger:
                ledger.session(False)
        if session.session_id:
            session_id = session.session_id
        if session.usage:
            cached_usage = session.usage
            logger.info(f"[usage] {session.usage.describe()}")

        if control.should_stop():
            logger.info(f"\n🛑 Stopped by watchdog: {control.stop_reason()}")
            return _stopped_outcome()

        outcome = classify(session)

        if outcome == "success":
            logger.info("✅ Task completed successfully.")
            return Outcome.COMPLETED

        if outcome == "limit":
            budget.note_window(window_start_usage, session.usage, session.duration)
            target = resolve_wait_target(session)
            # The stream's reading may lack the weekly reset time; the gate's has it.
            if (_week_ends_before(budget, session.usage, target)
                    or _week_ends_before(budget, window_start_usage, target)):
                return Outcome.WEEK_ENDED
            if target:
                wait_seconds = max(0, target - time.time()) + RESET_GRACE_SECONDS
                logger.warning(
                    f"5-hour limit reached. Resuming at {fmt_epoch(target)} "
                    f"(~{wait_seconds / 60:.1f} minutes)."
                )
            else:
                wait_seconds = FALLBACK_WAIT_SECONDS
                logger.error(
                    "Could not determine reset time. Waiting 10 minutes as fallback."
                )

            if control.interruptible_sleep(wait_seconds):
                logger.info(f"Stopped during wait: {control.stop_reason()}")
                return _stopped_outcome()

            # Usage is stale after a multi-hour sleep; force a fresh check.
            cached_usage = None
            retries += 1
            continue

        if outcome == "max_turns":
            logger.warning(
                f"Turn limit reached — resuming to continue "
                f"(attempt {retries + 1}/{max_retries})."
            )
            retries += 1
            continue

        if outcome == "transient":
            retries += 1
            backoff = min(300, 60 * retries)
            logger.warning(
                f"Transient error (exit code {session.returncode}). "
                f"Retrying in {backoff}s (attempt {retries}/{max_retries})..."
            )
            if control.interruptible_sleep(backoff):
                logger.info(f"Stopped during backoff: {control.stop_reason()}")
                return _stopped_outcome()
            continue

        logger.error(f"Claude exited with code {session.returncode}.")
        return Outcome.FAILED

    logger.error("Max retries reached.")
    return Outcome.FAILED
