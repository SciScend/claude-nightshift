#!/usr/bin/env python3
"""
claude_nightshift.py

Runs a Claude Code task non-stop:
- Auto-resumes after 5-hour session limit (using `claude -c`)
- Stops automatically if 7-day usage hits the threshold (default 70%)

Usage:
    python3 claude_nightshift.py [--threshold 70] [--interval 60] -- claude --dangerously-skip-permissions -p "your task"
    // claude_nightshift --threshold 85 --interval 60 -- claude --dangerously-skip-permissions -c -p "continue"
Arguments:
    --threshold   7-day usage % at which to stop (default: 70)
    --interval    How often in seconds to check 7-day usage (default: 60)
    --            Everything after this is the claude command to run
"""

import subprocess
import sys
import re
import time
import datetime
import logging
import os
import threading
import argparse
import signal

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

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
    logging.info(f"[watchdog] Started — threshold={threshold}%, interval={interval}s")

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
            logging.warning(f"[watchdog] cclimits error: {e}")
            continue

        used = parse_7day_usage(output)

        if used is None:
            logging.warning("[watchdog] Could not parse 7-day usage from cclimits output")
            continue

        logging.info(f"[watchdog] 7-day usage: {used}%")

        if used >= threshold:
            reason = f"7-day usage {used}% reached threshold {threshold}%"
            logging.warning(f"[watchdog] 🛑 {reason} — stopping Claude")
            set_stop(reason)

            proc = get_current_process()
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

            break

    logging.info("[watchdog] Exited")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(command, threshold, max_retries=10):
    first_run = True
    retries = 0

    while retries < max_retries:
        if should_stop():
            logging.info(f"Stopped before retry: {_stop_reason}")
            return False

        # Use -c for all runs after the very first one
        if first_run:
            cmd = list(command)
            # Ensure autonomous flags are always present on the initial command
            if "--dangerously-skip-permissions" not in cmd:
                cmd.insert(1, "--dangerously-skip-permissions")
            first_run = False
        else:
            resume_cmd = ["claude", "-c", "--dangerously-skip-permissions"]
            resume_cmd.extend(["-p", "continue"])
            cmd = resume_cmd

        logging.info(f"Running: {' '.join(cmd)}")

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
            logging.error(f"Could not start process: {e}")
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
            logging.info(f"\n🛑 Stopped by watchdog: {_stop_reason}")
            return False

        # UPDATED: If limit was reached, we IGNORE the exit code and wait
        if limit_reached:
            if not renewal_datetime:
                # Try parsing the whole buffer if it wasn't on the specific "limit" line
                renewal_datetime = parse_renewal_time("".join(full_output))

            if renewal_datetime:
                now = datetime.datetime.now()
                wait_seconds = max(0, (renewal_datetime - now).total_seconds()) + 30

                # Dynamic log message based on what will actually run next
                next_action = "claude -c" if not first_run else " ".join(command)
                
                logging.warning(
                    f"Limit reached. Resuming at {renewal_datetime} "
                    f"(~{wait_seconds/60:.1f} minutes). Next: {next_action}"
                )
                
                end_time = time.time() + wait_seconds
                while time.time() < end_time:
                    if should_stop():
                        logging.info(f"Stopped during wait: {_stop_reason}")
                        return False
                    time.sleep(5)
            else:
                logging.error("Could not parse renewal time. Waiting 10 minutes as fallback.")
                time.sleep(600)

            retries += 1
            continue

        # Normal exit (Only if no limit was hit)
        if proc.returncode == 0:
            logging.info("✅ Task completed successfully.")
            return True
        else:
            logging.error(f"Claude exited with code {proc.returncode}.")
            return False

    logging.error("Max retries reached.")
    return False

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run Claude Code non-stop, auto-resuming on 5-hour limit, stopping at 7-day threshold."
    )
    parser.add_argument("--threshold", type=float, default=70.0,
                        help="7-day usage %% at which to stop (default: 70)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Seconds between 7-day usage checks (default: 60)")
    parser.add_argument("claude_args", nargs=argparse.REMAINDER,
                        help="Claude command and args, e.g.: -- claude -p 'task'")

    args = parser.parse_args()

    # Strip leading '--' separator if present
    claude_args = args.claude_args
    if claude_args and claude_args[0] == "--":
        claude_args = claude_args[1:]

    if not claude_args:
        parser.print_help()
        sys.exit(1)

    logging.info(f"Starting claude_nightshift.py")
    logging.info(f"  Command   : {' '.join(claude_args)}")
    logging.info(f"  Threshold : {args.threshold}%")
    logging.info(f"  Interval  : {args.interval}s")

    # Start watchdog in background
    t = threading.Thread(
        target=watchdog_thread,
        args=(args.threshold, args.interval),
        daemon=True
    )
    t.start()

    # Handle Ctrl+C gracefully
    def handle_sigint(sig, frame):
        logging.info("\nInterrupted by user.")
        proc = get_current_process()
        if proc and proc.poll() is None:
            proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    success = run(claude_args, args.threshold)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()