#!/usr/bin/env python3
import subprocess
import sys
import re
import time
import datetime
import logging
import os
import argparse  # Added for CLI arguments

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ... [parse_renewal_time, kill_claude_instances, get_current_limit functions remain the same] ...

def main():
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Wait for Claude CLI limits to reset.")
    parser.add_argument(
        "--dangerously-skip-permissions", 
        action="store_true", 
        help="Skip permissions when resuming Claude"
    )
    args_parsed = parser.parse_args()

    # 1. Kill existing instances
    kill_claude_instances()
    time.sleep(1)

    # 2. Get renewal time
    renewal_datetime = get_current_limit()
    
    if not renewal_datetime:
        logging.error("Could not determine renewal time automatically.")
        sys.exit(1)

    # 3. Wait
    now = datetime.datetime.now()
    wait_seconds = (renewal_datetime - now).total_seconds()
    buffer = 2 if os.environ.get("TEST_MODE") else 30
    wait_seconds += buffer
    
    if wait_seconds > 0:
        logging.info(f"Target renewal time: {renewal_datetime}")
        logging.info(f"Waiting for {wait_seconds:.0f} seconds...")
        time.sleep(wait_seconds)

    # 4. Resume
    logging.info("Resuming Claude Code session...")
    
    claude_cmd = os.environ.get("CLAUDE_COMMAND", "claude")
    
    # Construct the base arguments
    exec_args = ["-c"]
    if args_parsed.dangerously_skip_permissions:
        exec_args.append("--dangerously-skip-permissions")

    if " " in claude_cmd: 
        parts = claude_cmd.split()
        # parts[0] is the executable, the rest are arguments
        os.execvp(parts[0], parts + exec_args)
    else:
        os.execlp(claude_cmd, claude_cmd, *exec_args)

if __name__ == "__main__":
    main()