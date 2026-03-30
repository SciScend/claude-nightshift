#!/usr/bin/env python3
import subprocess
import sys
import re
import time
import datetime
import logging
import os
import argparse

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def parse_renewal_time(output):
    """Parses renewal time from output."""
    match = re.search(r"(?:renewed at|resets\s+(?:at\s+)?)(\d{1,2}(?::\d{2})?\s*[AP]M)", output, re.IGNORECASE)
    if match:
        time_str = match.group(1).strip().upper()
        time_str = re.sub(r"(\d)([AP]M)", r"\1 \2", time_str)
        now = datetime.datetime.now()
        target_time = None
        for fmt in ["%I:%M %p", "%I %p"]:
            try:
                target_time = datetime.datetime.strptime(time_str, fmt).time()
                break
            except ValueError:
                continue
        if target_time:
            target_datetime = datetime.datetime.combine(now.date(), target_time)
            if target_datetime < now:
                if (now - target_datetime).total_seconds() > 1800:
                    target_datetime += datetime.timedelta(days=1)
                else:
                    target_datetime = now + datetime.timedelta(seconds=2)
            return target_datetime

    match = re.search(r"resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", output, re.IGNORECASE)
    if match:
        time_str = match.group(1)
        try:
            return datetime.datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return None

def kill_claude_instances():
    """Kills all running instances of the Claude CLI."""
    logging.info("Cleaning up existing Claude processes...")
    try:
        stdout = subprocess.check_output(["pgrep", "-x", "claude"]).decode()
        pids = stdout.strip().split()
        my_pid = str(os.getpid())
        for pid in pids:
            if pid == my_pid:
                continue
            try:
                logging.info(f"Killing process {pid}...")
                os.kill(int(pid), 9)
            except ProcessLookupError:
                pass
    except subprocess.CalledProcessError:
        pass

def get_current_limit():
    """Runs a minimal claude command to trigger the limit message."""
    logging.info("Checking current usage status...")
    claude_cmd = os.environ.get("CLAUDE_COMMAND", "claude")
    variations = [["-p", "status"], ["-p", "hi"], []]
    
    for args in variations:
        cmd = claude_cmd.split() + args if " " in claude_cmd else [claude_cmd] + args
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            full_output = []
            start_time = time.time()
            timeout = 10 if args else 5 
            
            while time.time() - start_time < timeout:
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None: break
                    time.sleep(0.1)
                    continue
                print(f"  [Output] {line.strip()}")
                full_output.append(line)
                time_found = parse_renewal_time(line)
                if time_found:
                    process.terminate()
                    return time_found
            process.terminate()
            time_found = parse_renewal_time("".join(full_output))
            if time_found: return time_found
        except Exception as e:
            logging.error(f"Error during check: {e}")
    return None

def main():
    parser = argparse.ArgumentParser(
        description="Automates waiting for Claude CLI limits to reset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  claude-resume\n  claude-resume --dangerously-skip-permissions"
    )
    parser.add_argument("--dangerously-skip-permissions", action="store_true", help="Skip approvals on resume.")
    args_parsed = parser.parse_args()

    kill_claude_instances()
    time.sleep(1)

    renewal_datetime = get_current_limit()
    if not renewal_datetime:
        logging.error("Could not determine renewal time. Are you sure you are rate-limited?")
        sys.exit(1)

    now = datetime.datetime.now()
    wait_seconds = (renewal_datetime - now).total_seconds()
    buffer = 2 if os.environ.get("TEST_MODE") else 30
    wait_seconds += buffer
    
    if wait_seconds > 0:
        logging.info(f"Target renewal time: {renewal_datetime}")
        logging.info(f"Waiting for {wait_seconds:.0f} seconds...")
        time.sleep(wait_seconds)

    logging.info("Resuming Claude Code session...")
    claude_cmd = os.environ.get("CLAUDE_COMMAND", "claude")
    exec_args = ["-c"]
    if args_parsed.dangerously_skip_permissions:
        exec_args.append("--dangerously-skip-permissions")

    if " " in claude_cmd: 
        parts = claude_cmd.split()
        os.execvp(parts[0], parts + exec_args)
    else:
        os.execlp(claude_cmd, claude_cmd, *exec_args)

if __name__ == "__main__":
    main()