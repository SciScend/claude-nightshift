#!/usr/bin/env python3
"""
claude-nightshift — run Claude Code tasks autonomously until done.

Auto-resumes after 5-hour session limits, stops when 7-day usage hits threshold.
Uses a hybrid resume strategy to minimize token usage on long-running tasks.

Usage:
    claude-nightshift "your task"
    claude-nightshift -t 85 "refactor the auth module"
    claude-nightshift -t 90 -i 120 --model opus "fix all lint errors"
    claude-nightshift --max-turns 50 "fix all lint errors"
    claude-nightshift --compress-after 2 "large refactor"
    claude-nightshift --resume                       # resume last session
    claude-nightshift -l "some task"                 # log output to nightshift_*.log
    claude-nightshift --log-file run.log "some task" # log output to specific file
    claude-nightshift --dry-run "some task"          # preview command without running

Advanced (full control over claude args):
    claude-nightshift -- claude -p "task" --model opus --allowedTools Edit,Write

Resume strategy (token optimization):
    On 5-hour limit hits, the script resumes automatically. The first N resumes
    (default 3, set via --compress-after) use `claude -c` which carries the full
    conversation history. After that, it switches to compressed context: a fresh
    Claude session seeded with the original task and a progress summary that
    Claude writes to `.nightshift-status.md` before each session ends. This
    avoids unbounded context growth on multi-session tasks.

Notes:
    - --dangerously-skip-permissions is always added automatically
    - Requires `npx cclimits --claude` for 7-day usage monitoring
"""

import subprocess
import sys
import re
import time
import datetime
import logging
import threading
import argparse
import signal

logger = logging.getLogger(__name__)

# Will be set to an open file handle when --log is used, else None
_log_file = None


def setup_logging(log_path=None):
    """Configure logging to stderr, and optionally also to a file."""
    global _log_file
    fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    if log_path:
        _log_file = open(log_path, "a", buffering=1)  # line-buffered
        fh = logging.StreamHandler(_log_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)


def log_output(line):
    """Write a Claude output line to the log file (if open)."""
    if _log_file:
        _log_file.write(line)
        _log_file.flush()

# Shared state between main thread and watchdog thread
_lock = threading.Lock()
_current_process = None   # the live claude subprocess
_stop_flag = False        # set to True when 7-day limit hit
_stop_reason = ""         # human-readable reason for stopping


def set_current_process(proc):
    global _current_process
    with _lock:
        _current_process = proc


def get_current_process():
    with _lock:
        return _current_process


def set_stop(reason):
    global _stop_flag, _stop_reason
    with _lock:
        _stop_flag = True
        _stop_reason = reason


def should_stop():
    with _lock:
        return _stop_flag


# ---------------------------------------------------------------------------
# Status file & prompt helpers (for compressed context resumes)
# ---------------------------------------------------------------------------

STATUS_FILE = ".nightshift-status.md"

STATUS_INSTRUCTION = (
    "\n\nBefore your session ends, write a concise progress summary to "
    f"`{STATUS_FILE}`: what's done, key decisions, and next steps."
)


def read_status_file():
    """Read the compressed context status file, if it exists."""
    try:
        with open(STATUS_FILE, "r") as f:
            return f.read().strip()
    except (FileNotFoundError, OSError):
        return None


def extract_task_from_command(cmd):
    """Extract the -p argument value from a claude command list."""
    try:
        idx = cmd.index("-p")
        return cmd[idx + 1] if idx + 1 < len(cmd) else None
    except ValueError:
        return None


def inject_prompt(cmd, suffix):
    """Append text to the -p argument in a command list. Returns a new list."""
    cmd = list(cmd)
    try:
        idx = cmd.index("-p")
        if idx + 1 < len(cmd):
            cmd[idx + 1] = cmd[idx + 1] + suffix
    except ValueError:
        pass
    return cmd


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_7day_usage(cclimits_output):
    """
    Extract the 7-day Used percentage from cclimits --claude output.
    Looks for the block:
      7-Day Window:
        Used:      41.0%
    Returns float or None.
    """
    match = re.search(
        r"7-Day Window.*?Used:\s+([\d.]+)%",
        cclimits_output,
        re.DOTALL | re.IGNORECASE
    )
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


def parse_renewal_time(output):
    """
    Extract 5-hour reset time from Claude's limit message.
    Handles:
      - "renewed at 10:30 PM"
      - "resets at 5:00 AM"
      - "resets 12am"
      - "resets at 2026-03-07T05:00:00Z"
    Returns datetime or None.
    """
    # 12-hour format
    match = re.search(
        r"(?:renewed at|resets\s+(?:at\s+)?)(\d{1,2}(?::\d{2})?\s*[AP]M)",
        output,
        re.IGNORECASE
    )
    if match:
        time_str = match.group(1).strip().upper()
        time_str = re.sub(r"(\d)([AP]M)", r"\1 \2", time_str)
        now = datetime.datetime.now()
        for fmt in ["%I:%M %p", "%I %p"]:
            try:
                t = datetime.datetime.strptime(time_str, fmt).time()
                dt = datetime.datetime.combine(now.date(), t)
                if dt < now:
                    dt += datetime.timedelta(days=1) if (now - dt).total_seconds() > 1800 \
                          else datetime.timedelta(seconds=2)
                return dt
            except ValueError:
                continue

    # ISO format
    match = re.search(
        r"resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)",
        output,
        re.IGNORECASE
    )
    if match:
        try:
            return datetime.datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# Watchdog thread
# ---------------------------------------------------------------------------

def watchdog_thread(threshold, interval):
    """
    Runs in background. Checks 7-day usage every `interval` seconds.
    If usage >= threshold, kills the current claude process and sets stop flag.
    """
    logger.info(f"[watchdog] Started — threshold={threshold}%, interval={interval}s")

    while True:
        time.sleep(interval)

        if should_stop():
            break

        try:
            result = subprocess.run(
                ["npx", "cclimits", "--claude"],
                capture_output=True,
                text=True,
                timeout=30
            )
            output = result.stdout + result.stderr
        except Exception as e:
            logger.warning(f"[watchdog] cclimits error: {e}")
            continue

        used = parse_7day_usage(output)

        if used is None:
            logger.warning("[watchdog] Could not parse 7-day usage from cclimits output")
            continue

        logger.info(f"[watchdog] 7-day usage: {used}%")

        if used >= threshold:
            reason = f"7-day usage {used}% reached threshold {threshold}%"
            logger.warning(f"[watchdog] 🛑 {reason} — stopping Claude")
            set_stop(reason)

            proc = get_current_process()
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

            break

    logger.info("[watchdog] Exited")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(command, threshold, max_retries=10, compress_after=3, max_turns=None):
    original_task = extract_task_from_command(command)

    # Inject status-file instruction so Claude writes progress before session ends
    if original_task:
        command = inject_prompt(command, STATUS_INSTRUCTION)

    first_run = True
    retries = 0

    while retries < max_retries:
        if should_stop():
            logger.info(f"Stopped before retry: {_stop_reason}")
            return False

        if first_run:
            cmd = list(command)
            if "--dangerously-skip-permissions" not in cmd:
                cmd.insert(1, "--dangerously-skip-permissions")
            first_run = False
        elif retries < compress_after:
            # Full context resume — carries conversation history
            cmd = ["claude", "-c", "--dangerously-skip-permissions"]
            if max_turns:
                cmd.extend(["--max-turns", str(max_turns)])
            cmd.extend(["-p", "continue"])
        else:
            # Compressed context resume — fresh start with status summary
            status = read_status_file()
            parts = []
            if original_task:
                parts.append(f"Original task: {original_task}")
            if status:
                parts.append(f"Progress from previous session:\n\n{status}")
            parts.append("Continue where you left off." + STATUS_INSTRUCTION)
            prompt = "\n\n".join(parts)
            cmd = ["claude", "--dangerously-skip-permissions"]
            if max_turns:
                cmd.extend(["--max-turns", str(max_turns)])
            cmd.extend(["-p", prompt])
            logger.info(f"Resuming with compressed context (resume {retries})")

        logger.info(f"Running: {' '.join(cmd)}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
        except FileNotFoundError as e:
            logger.error(f"Could not start process: {e}")
            return False

        set_current_process(proc)

        full_output = []
        limit_reached = False
        renewal_datetime = None

        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line:
                print(line, end='', flush=True)
                log_output(line)
                full_output.append(line)

                # UPDATED: Catch more phrasing variants
                low_line = line.lower()
                if "usage limit reached" in low_line or "hit your limit" in low_line:
                    limit_reached = True

                if limit_reached and not renewal_datetime:
                    renewal_datetime = parse_renewal_time(line)

        proc.wait()
        set_current_process(None)

        # Watchdog killed the process
        if should_stop():
            logger.info(f"\n🛑 Stopped by watchdog: {_stop_reason}")
            return False

        # UPDATED: If limit was reached, we IGNORE the exit code and wait
        if limit_reached:
            if not renewal_datetime:
                # Try parsing the whole buffer if it wasn't on the specific "limit" line
                renewal_datetime = parse_renewal_time("".join(full_output))

            if renewal_datetime:
                now = datetime.datetime.now()
                wait_seconds = max(0, (renewal_datetime - now).total_seconds()) + 30

                next_strategy = "full context (-c)" if retries + 1 < compress_after \
                    else "compressed context (fresh)"

                logger.warning(
                    f"Limit reached. Resuming at {renewal_datetime} "
                    f"(~{wait_seconds/60:.1f} minutes). Next: {next_strategy}"
                )
                
                end_time = time.time() + wait_seconds
                while time.time() < end_time:
                    if should_stop():
                        logger.info(f"Stopped during wait: {_stop_reason}")
                        return False
                    time.sleep(5)
            else:
                logger.error("Could not parse renewal time. Waiting 10 minutes as fallback.")
                time.sleep(600)

            retries += 1
            continue

        # Normal exit (Only if no limit was hit)
        if proc.returncode == 0:
            logger.info("✅ Task completed successfully.")
            return True
        else:
            logger.error(f"Claude exited with code {proc.returncode}.")
            return False

    logger.error("Max retries reached.")
    return False

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_claude_command(args):
    """
    Build the claude command list from parsed args.

    Supports two modes:
      1. Simple: task prompt as positional arg (with optional --model, --resume)
      2. Advanced: raw claude args passed via args.claude_raw
    """
    if args.claude_raw:
        return args.claude_raw

    # --resume mode: no prompt needed
    if args.resume:
        return ["claude", "-c", "-p", "continue"]

    # Simple mode: build from task + optional flags
    if not args.task:
        return None

    cmd = ["claude"]
    if args.model:
        cmd.extend(["--model", args.model])
    if args.max_turns:
        cmd.extend(["--max-turns", str(args.max_turns)])
    cmd.extend(["-p", args.task])
    return cmd


def main():
    # Pre-split sys.argv on '--' so argparse doesn't consume it
    argv = sys.argv[1:]
    claude_raw = []
    if "--" in argv:
        idx = argv.index("--")
        claude_raw = argv[idx + 1:]
        argv = argv[:idx]

    parser = argparse.ArgumentParser(
        prog="claude-nightshift",
        description="Run Claude Code autonomously until done. Auto-resumes on 5-hour limits, stops at 7-day threshold.",
        epilog="Examples:\n"
               '  claude-nightshift "fix all lint errors"\n'
               '  claude-nightshift -t 85 --model opus "refactor auth"\n'
               '  claude-nightshift --resume\n'
               '  claude-nightshift -- claude -p "task" --allowedTools Edit\n',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task", nargs="?", default=None,
                        help="Task prompt for Claude (quoted string)")
    parser.add_argument("-t", "--threshold", type=float, default=70.0,
                        help="7-day usage %% at which to stop (default: 70)")
    parser.add_argument("-i", "--interval", type=int, default=60,
                        help="Seconds between 7-day usage checks (default: 60)")
    parser.add_argument("-r", "--max-retries", type=int, default=10,
                        help="Max resume retries on 5-hour limits (default: 10)")
    parser.add_argument("-m", "--model", type=str, default=None,
                        help="Claude model: alias (opus, sonnet, haiku) or full name (claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5-20251001)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume the last Claude session (claude -c -p continue)")
    parser.add_argument("-l", "--log", action="store_true",
                        help="Log all output to nightshift_YYYYMMDD_HHMMSS.log")
    parser.add_argument("--log-file", type=str, default=None, metavar="FILE",
                        help="Log all output to a specific file")
    parser.add_argument("--max-turns", type=int, default=None, metavar="N",
                        help="Max agentic turns per Claude session (passed to claude --max-turns)")
    parser.add_argument("--compress-after", type=int, default=3, metavar="N",
                        help="After N resumes, switch from full context (-c) to compressed "
                             "context via status file (default: 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the command that would be run, then exit")

    args = parser.parse_args(argv)
    args.claude_raw = claude_raw

    claude_cmd = build_claude_command(args)

    if not claude_cmd:
        parser.print_help()
        sys.exit(1)

    if args.dry_run:
        # Show what would run (with --dangerously-skip-permissions added)
        preview = list(claude_cmd)
        if "--dangerously-skip-permissions" not in preview:
            preview.insert(1, "--dangerously-skip-permissions")
        print(" ".join(preview))
        sys.exit(0)

    # Resolve log path
    log_path = None
    if args.log_file:
        log_path = args.log_file
    elif args.log:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = f"nightshift_{stamp}.log"

    setup_logging(log_path)

    if log_path:
        logger.info(f"Logging to {log_path}")

    logger.info(f"Starting claude-nightshift")
    logger.info(f"  Command       : {' '.join(claude_cmd)}")
    logger.info(f"  Threshold     : {args.threshold}%")
    logger.info(f"  Interval      : {args.interval}s")
    logger.info(f"  Max retries   : {args.max_retries}")
    logger.info(f"  Compress after: {args.compress_after} resumes")
    if args.max_turns:
        logger.info(f"  Max turns     : {args.max_turns}")

    # Start watchdog in background
    t = threading.Thread(
        target=watchdog_thread,
        args=(args.threshold, args.interval),
        daemon=True
    )
    t.start()

    # Handle Ctrl+C gracefully
    def handle_sigint(sig, frame):
        logger.info("\nInterrupted by user.")
        proc = get_current_process()
        if proc and proc.poll() is None:
            proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    success = run(claude_cmd, args.threshold, max_retries=args.max_retries,
                  compress_after=args.compress_after, max_turns=args.max_turns)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()