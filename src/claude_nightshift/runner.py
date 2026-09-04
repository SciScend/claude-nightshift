"""The retry loop: gate, run a session, interpret it, wait, resume."""

from __future__ import annotations

import time

from claude_nightshift import control
from claude_nightshift.logsetup import get_logger
from claude_nightshift.session import classify, run_session
from claude_nightshift.status import (
    STATUS_INSTRUCTION,
    extract_task_from_command,
    inject_prompt,
    read_status_file,
)
from claude_nightshift.timeutil import fmt_epoch
from claude_nightshift.usage import fetch_usage

__all__ = [
    "weekly_gate",
    "resolve_wait_target",
    "build_resume_command",
    "watchdog_thread",
    "run",
]

logger = get_logger(__name__)

SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"

# How long to wait when the reset time cannot be determined at all.
FALLBACK_WAIT_SECONDS = 600
# Slack added after a known reset time, so we never wake a moment too early.
RESET_GRACE_SECONDS = 30


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
            control.set_stop(reason)
            control.terminate_process(control.get_current_process())
            break

    logger.info("[watchdog] Exited")


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

    budget.plan(usage)
    allowed, reason = budget.check(usage)
    return allowed, reason, usage


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
        cmd.extend(["--resume", session_id, "-p", "continue" + STATUS_INSTRUCTION])
        return cmd, f"full context (--resume {session_id[:8]})"

    if retries < compress_after:
        # No session id captured; fall back to "most recent in this directory".
        cmd.extend(["-c", "-p", "continue" + STATUS_INSTRUCTION])
        return cmd, "full context (-c)"

    # Compressed context resume — fresh start seeded with the status summary.
    status = read_status_file()
    parts = []
    if original_task:
        parts.append(f"Original task: {original_task}")
    if status:
        parts.append(f"Progress from previous session:\n\n{status}")
    parts.append("Continue where you left off." + STATUS_INSTRUCTION)
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
):
    """Run `command` to completion, waiting out limits. True if the task finished."""
    original_task = extract_task_from_command(command)

    # Inject the status-file instruction so Claude writes progress before the
    # session ends; that summary is what a compressed resume is seeded with.
    if original_task:
        command = inject_prompt(command, STATUS_INSTRUCTION)

    first_run = True
    retries = 0
    session_id = None
    cached_usage = None
    window_start_usage = None

    while retries < max_retries:
        if control.should_stop():
            logger.info(f"Stopped before retry: {control.stop_reason()}")
            return False

        # --- the gate: may we start another session? ---
        allowed, reason, usage = weekly_gate(
            budget, cached_usage, usage_max_age, allow_unknown
        )
        if usage:
            cached_usage = usage
            window_start_usage = usage
        if not allowed:
            logger.warning(f"🛑 Not starting another session: {reason}")
            return False
        logger.info(f"Gate open: {reason}")

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

        session = run_session(cmd, structured=structured)
        if session.session_id:
            session_id = session.session_id
        if session.usage:
            cached_usage = session.usage
            logger.info(f"[usage] {session.usage.describe()}")

        if control.should_stop():
            logger.info(f"\n🛑 Stopped by watchdog: {control.stop_reason()}")
            return False

        outcome = classify(session)

        if outcome == "success":
            logger.info("✅ Task completed successfully.")
            return True

        if outcome == "limit":
            budget.note_window(window_start_usage, session.usage)
            target = resolve_wait_target(session)
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
                return False

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
                return False
            continue

        logger.error(f"Claude exited with code {session.returncode}.")
        return False

    logger.error("Max retries reached.")
    return False
