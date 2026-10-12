"""Command-line interface for claude-nightshift."""

from __future__ import annotations

import argparse
import datetime
import signal
import sys
import threading

from claude_nightshift import __version__, control
from claude_nightshift.budget import DEFAULT_MIN_WINDOW_LEFT, Budget, max_windows_arg
from claude_nightshift.logsetup import get_logger, setup_logging
from claude_nightshift.runner import SKIP_PERMISSIONS_FLAG, run, watchdog_thread
from claude_nightshift.timeutil import parse_duration

__all__ = ["build_parser", "build_claude_command", "main"]

logger = get_logger(__name__)

DESCRIPTION = (
    "Run Claude Code autonomously until done. Waits out each 5-hour session "
    "limit and resumes, as long as weekly usage stays below --threshold."
)

EPILOG = """Examples:
  claude-nightshift "fix all lint errors"
  claude-nightshift -t 85 --model opus "refactor auth"
  claude-nightshift -t 90 --effort medium "long refactor"
  claude-nightshift -w auto --horizon 20h "big refactor"
  claude-nightshift -w 2 "spend at most two 5h windows"
  claude-nightshift --resume
  claude-nightshift -- claude -p "task" --allowedTools Edit

The weekly threshold is checked before each session starts; a running
session is never interrupted unless you pass --hard-stop.
-w/--max-windows caps what a single run may spend; 'auto' spreads the
remaining weekly budget evenly over the time left before it resets.
Usage figures are read from Claude's own stream, then the OAuth usage
endpoint, then cclimits — none of them cost tokens.
"""


def split_raw_args(argv):
    """Split argv on a bare ``--`` so argparse does not consume the claude args."""
    if "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1 :]
    return list(argv), []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-nightshift",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task", nargs="?", default=None,
                        help="Task prompt for Claude (quoted string)")
    parser.add_argument("-V", "--version", action="version",
                        version=f"claude-nightshift {__version__}")
    parser.add_argument("-t", "--threshold", type=float, default=70.0,
                        help="7-day usage %% at or above which no new session is "
                             "started (default: 70)")
    parser.add_argument("-w", "--max-windows", type=max_windows_arg, default=None,
                        metavar="N|auto",
                        help="Cap this run at N five-hour windows, so one task cannot "
                             "spend the whole week. 'auto' derives N from the weekly "
                             "budget left and --horizon (default: no cap)")
    parser.add_argument("--horizon", type=parse_duration, default=12 * 3600,
                        metavar="DURATION",
                        help="How long you are leaving nightshift unattended, e.g. 20h, "
                             "90m, 2d. Only used by --max-windows auto (default: 12h)")
    parser.add_argument("--min-window-left", type=float,
                        default=DEFAULT_MIN_WINDOW_LEFT, metavar="PCT",
                        help="Do not start a session unless at least PCT%% of the "
                             "5-hour window is left; wait for the reset instead of "
                             "spending a budgeted window on a few minutes of work "
                             "(default: 5, 0 disables)")
    parser.add_argument("--no-overshoot", action="store_true",
                        help="Do not start a window that is predicted to cross the "
                             "threshold. Implied by --max-windows")
    parser.add_argument("-i", "--interval", type=int, default=300,
                        help="Seconds between usage checks, --hard-stop only "
                             "(default: 300)")
    parser.add_argument("-r", "--max-retries", type=int, default=10,
                        help="Max resume attempts across limits and turn caps "
                             "(default: 10)")
    parser.add_argument("-m", "--model", type=str, default=None,
                        help="Claude model: alias (opus, sonnet, haiku, fable) or full "
                             "name (claude-opus-5, claude-sonnet-5, claude-fable-5-1, "
                             "claude-haiku-4-5-20251001)")
    parser.add_argument("-e", "--effort", type=str, default=None,
                        choices=["low", "medium", "high", "xhigh", "max"],
                        help="Effort level for each session. Lower effort spends fewer "
                             "thinking tokens, so more work fits in one 5-hour window")
    parser.add_argument("--resume", action="store_true",
                        help="Resume the last Claude session (claude -c -p continue)")
    parser.add_argument("-l", "--log", action="store_true",
                        help="Log all output to nightshift_YYYYMMDD_HHMMSS.log")
    parser.add_argument("--log-file", type=str, default=None, metavar="FILE",
                        help="Log all output to a specific file")
    parser.add_argument("--max-turns", type=int, default=None, metavar="N",
                        help="Max agentic turns per Claude session. Hitting it ends the "
                             "session and nightshift resumes, it is not an error")
    parser.add_argument("--compress-after", type=int, default=3, metavar="N",
                        help="After N resumes, switch from full context (--resume "
                             "<session-id>) to compressed context via the status file "
                             "(default: 3)")
    parser.add_argument("--hard-stop", action="store_true",
                        help="Also poll usage every --interval seconds and terminate a "
                             "RUNNING session once --threshold is reached. Loses "
                             "in-progress work; off by default")
    parser.add_argument("--allow-unknown-usage", action="store_true",
                        help="Start a session even when weekly usage cannot be read. "
                             "By default an unreadable usage figure stops the run")
    parser.add_argument("--usage-max-age", type=int, default=120, metavar="SEC",
                        help="How long a usage reading from Claude's stream stays "
                             "usable before the endpoint is queried again (default: 120)")
    parser.add_argument("--raw-output", action="store_true",
                        help="Do not use stream-json; show Claude's plain text output "
                             "and fall back to matching limit messages as text")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the command that would be run, then exit")
    return parser


def build_base_flags(args, structured):
    """Flags that apply to every session, including resumes."""
    flags = []
    if args.model:
        flags.extend(["--model", args.model])
    if args.effort:
        flags.extend(["--effort", args.effort])
    if args.max_turns:
        flags.extend(["--max-turns", str(args.max_turns)])
    if structured:
        flags.extend(["--output-format", "stream-json", "--verbose"])
    return flags


def build_claude_command(args, base_flags, structured):
    """Build the claude command list from parsed args.

    Two modes: raw claude args after ``--`` (full control), or the simple form
    built from the task prompt and nightshift's own flags.
    """
    if args.claude_raw:
        cmd = list(args.claude_raw)
        if structured and "--output-format" not in cmd:
            cmd.extend(["--output-format", "stream-json", "--verbose"])
        return cmd

    if args.resume:
        return ["claude"] + list(base_flags) + ["-c", "-p", "continue"]

    if not args.task:
        return None

    return ["claude"] + list(base_flags) + ["-p", args.task]


def resolve_log_path(args):
    if args.log_file:
        return args.log_file
    if args.log:
        return f"nightshift_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
    return None


def log_banner(args, budget, claude_cmd) -> None:
    logger.info("Starting claude-nightshift")
    logger.info(f"  Command       : {' '.join(claude_cmd)}")
    logger.info(f"  Weekly gate   : start a session only below {args.threshold}%")
    if args.max_windows == "auto":
        logger.info(f"  Window budget : auto, over a {args.horizon / 3600:.0f}h horizon")
    elif args.max_windows:
        logger.info(f"  Window budget : {args.max_windows} × 5h window(s)")
    if budget.predictive:
        estimate = "measured" if budget.cost_is_measured else "estimated, no samples yet"
        logger.info(
            f"  Window cost   : ~{budget.window_cost:.1f}% of weekly ({estimate})"
        )
    if args.min_window_left > 0:
        logger.info(
            f"  5-hour gate   : need {args.min_window_left:.0f}% of the "
            f"window left to start"
        )
    logger.info(f"  Max retries   : {args.max_retries}")
    logger.info(f"  Compress after: {args.compress_after} resumes")
    if args.effort:
        logger.info(f"  Effort        : {args.effort}")
    if args.max_turns:
        logger.info(f"  Max turns     : {args.max_turns} (resumable)")
    if args.hard_stop:
        logger.info(f"  Hard stop     : on, polling every {args.interval}s")


def install_sigint_handler() -> None:
    def handle_sigint(_sig, _frame):
        logger.info("\nInterrupted by user.")
        control.set_stop("interrupted by user")
        control.terminate_process(control.get_current_process(), grace=5)
        sys.exit(130)

    signal.signal(signal.SIGINT, handle_sigint)


def main(argv=None) -> int:
    argv, claude_raw = split_raw_args(sys.argv[1:] if argv is None else list(argv))

    parser = build_parser()
    args = parser.parse_args(argv)
    args.claude_raw = claude_raw

    structured = not args.raw_output
    base_flags = build_base_flags(args, structured)
    claude_cmd = build_claude_command(args, base_flags, structured)

    if not claude_cmd:
        parser.print_help()
        return 1

    if args.dry_run:
        preview = list(claude_cmd)
        if SKIP_PERMISSIONS_FLAG not in preview:
            preview.insert(1, SKIP_PERMISSIONS_FLAG)
        print(" ".join(preview))
        return 0

    log_path = resolve_log_path(args)
    setup_logging(log_path)
    if log_path:
        logger.info(f"Logging to {log_path}")

    budget = Budget(
        args.threshold,
        max_windows=args.max_windows,
        horizon=args.horizon,
        predictive=args.no_overshoot,
    )

    log_banner(args, budget, claude_cmd)

    if args.hard_stop:
        threading.Thread(
            target=watchdog_thread,
            args=(args.threshold, args.interval),
            daemon=True,
        ).start()

    install_sigint_handler()

    success = run(
        claude_cmd,
        budget,
        max_retries=args.max_retries,
        compress_after=args.compress_after,
        base_flags=base_flags,
        structured=structured,
        usage_max_age=args.usage_max_age,
        allow_unknown=args.allow_unknown_usage,
        min_window_left=args.min_window_left,
    )
    return 0 if success else 1


def run_cli() -> None:
    """Console-script entry point."""
    sys.exit(main())
