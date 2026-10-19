"""Command-line interface for claude-nightshift."""

from __future__ import annotations

import argparse
import datetime
import json
import signal
import sys
import threading

from claude_nightshift import __version__, control
from claude_nightshift.budget import (
    DEFAULT_MIN_WINDOW_LEFT,
    Budget,
    max_windows_arg,
    spend_arg,
)
from claude_nightshift.ledger import Ledger, read_runs
from claude_nightshift.logsetup import get_logger, setup_logging
from claude_nightshift.runner import (
    SKIP_PERMISSIONS_FLAG,
    Outcome,
    run,
    spend_watch_thread,
    watchdog_thread,
)
from claude_nightshift.status import set_status_file
from claude_nightshift.timeutil import parse_duration
from claude_nightshift.usage import fetch_usage

__all__ = [
    "build_parser",
    "build_claude_command",
    "load_task_file",
    "usage_as_json",
    "main",
]

logger = get_logger(__name__)

DESCRIPTION = (
    "Run Claude Code autonomously until done. Waits out each 5-hour session "
    "limit and resumes, as long as weekly usage stays below --threshold."
)

EPILOG = """Examples:
  claude-nightshift "fix all lint errors"
  claude-nightshift -f task.md -w auto --horizon 10h
  claude-nightshift -t 85 --model opus "refactor auth"
  claude-nightshift -t 90 --effort medium "long refactor"
  claude-nightshift -w auto --horizon 20h "big refactor"
  claude-nightshift -w 2 "spend at most two 5h windows"
  claude-nightshift --run-id a --spend 5 "spend at most 5% of the week"
  claude-nightshift --run-id b --spend auto --spend-share 2 "one of two runs"
  claude-nightshift --resume
  claude-nightshift --usage-json
  claude-nightshift --runs-json
  claude-nightshift -- claude -p "task" --allowedTools Edit

The weekly threshold is checked before each session starts; a running
session is never interrupted unless you pass --hard-stop.
-w/--max-windows caps what a single run may spend; 'auto' spreads the
remaining weekly budget evenly over the time left before it resets.
--spend caps this run's own share of the weekly pool, also while other
runs share it; it is enforced during a session too.
Usage figures are read from Claude's own stream, then the OAuth usage
endpoint, then cclimits — none of them cost tokens.

Exit codes:
  0    task completed
  1    failed: Claude error or retries exhausted
  2    usage error (bad arguments)
  3    weekly limit: --threshold reached, no budget left this week
  4    run budget spent (-w N, -w auto or --spend), task not finished yet
  5    weekly usage could not be read, no session started
  130  interrupted
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
    parser.add_argument("-f", "--task-file", type=str, default=None, metavar="FILE",
                        help="Read the task prompt from FILE ('-' for stdin) instead "
                             "of the positional argument")
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
    parser.add_argument("--run-id", type=str, default=None, metavar="ID",
                        help="Name this run in the shared ledger "
                             "(~/.claude/nightshift-ledger.json), which splits the "
                             "weekly usage between runs that are going at the same "
                             "time. Needed for --spend")
    parser.add_argument("--spend", type=spend_arg, default=None, metavar="PCT|auto",
                        help="Stop once this run has spent PCT%% of the weekly pool, "
                             "counting only its own share when several runs go at "
                             "once. Stops a running session too. 'auto' gives it an "
                             "even share of what is left below --threshold "
                             "(see --spend-share)")
    parser.add_argument("--spend-share", type=int, default=1, metavar="N",
                        help="With --spend auto: how many runs share what is left "
                             "below --threshold (default: 1)")
    parser.add_argument("--spend-interval", type=int, default=120, metavar="SEC",
                        help="How often --spend is checked during a session "
                             "(default: 120)")
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
    parser.add_argument("--status-file", type=str, default=None, metavar="FILE",
                        help="Where Claude writes its progress summary between "
                             "sessions (default: .nightshift-status.md in the "
                             "working directory)")
    parser.add_argument("--usage-json", action="store_true",
                        help="Print the current 5-hour and 7-day usage as JSON and "
                             "exit (exit 5 if it cannot be read). Costs no tokens")
    parser.add_argument("--runs-json", action="store_true",
                        help="Print the runs in the shared ledger (what each spent of "
                             "the weekly pool and its --spend budget) as JSON and "
                             "exit. Reads only the local ledger")
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


def load_task_file(path):
    """Read a task prompt from ``path``, or from stdin when it is ``-``.

    Raises ``ValueError`` with a user-facing message when the file cannot be
    read or holds nothing but whitespace.
    """
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8") as f:
                text = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read task file {path!r}: {exc}") from None
    text = text.strip()
    if not text:
        raise ValueError(f"task file {path!r} is empty")
    return text


def usage_as_json(usage, runs=None):
    """The ``--usage-json`` document for a Usage snapshot and the ledger's runs."""
    return json.dumps({
        "weekly_pct": usage.weekly_pct,
        "weekly_resets": usage.weekly_resets,
        "five_hour_pct": usage.five_hour_pct,
        "five_hour_resets": usage.five_hour_resets,
        "source": usage.source,
        "observed_at": usage.observed_at,
        "runs": runs or {},
    })


def print_usage_json() -> int:
    usage = fetch_usage(attempts=1)
    if usage is None or usage.weekly_pct is None:
        print(json.dumps({"error": "usage could not be read"}))
        return int(Outcome.USAGE_UNKNOWN)
    print(usage_as_json(usage, read_runs()))
    return 0


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
    if args.run_id:
        logger.info(f"  Run id        : {args.run_id}")
    if args.spend == "auto":
        logger.info(f"  Spend budget  : auto, split {args.spend_share} way(s)")
    elif args.spend:
        logger.info(f"  Spend budget  : {args.spend:g}% of the weekly pool")
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
    if args.status_file:
        logger.info(f"  Status file   : {args.status_file}")
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
        sys.exit(int(Outcome.INTERRUPTED))

    signal.signal(signal.SIGINT, handle_sigint)


def main(argv=None) -> int:
    argv, claude_raw = split_raw_args(sys.argv[1:] if argv is None else list(argv))

    parser = build_parser()
    args = parser.parse_args(argv)
    args.claude_raw = claude_raw

    if args.usage_json:
        return print_usage_json()
    if args.runs_json:
        print(json.dumps(read_runs()))
        return 0

    if args.spend is not None and not args.run_id:
        parser.error("--spend needs --run-id")
    if args.spend_share < 1:
        parser.error("--spend-share must be at least 1")

    set_status_file(args.status_file)

    if args.task_file is not None:
        if args.task is not None:
            parser.error("give the task either as an argument or with -f, not both")
        try:
            args.task = load_task_file(args.task_file)
        except ValueError as exc:
            parser.error(str(exc))

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

    ledger = Ledger(args.run_id) if args.run_id else None
    budget = Budget(
        args.threshold,
        max_windows=args.max_windows,
        horizon=args.horizon,
        predictive=args.no_overshoot,
        spend=args.spend,
        spend_share=args.spend_share,
        ledger=ledger,
    )

    log_banner(args, budget, claude_cmd)

    if args.hard_stop:
        threading.Thread(
            target=watchdog_thread,
            args=(args.threshold, args.interval),
            daemon=True,
        ).start()

    if ledger:
        ledger.start(budget.spend_cap)
    if ledger and args.spend is not None:
        threading.Thread(
            target=spend_watch_thread,
            args=(budget, args.spend_interval),
            daemon=True,
        ).start()

    install_sigint_handler()

    try:
        outcome = run(
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
    finally:
        if ledger:
            ledger.finish()
            logger.info(f"Spent by this run: {ledger.spent:.1f}% of the weekly pool")
    return int(outcome)


def run_cli() -> None:
    """Console-script entry point."""
    sys.exit(main())
