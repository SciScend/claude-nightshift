#!/usr/bin/env python3
import subprocess
import sys
import re
import time
import datetime
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def parse_renewal_time(output):
    """
    Parses renewal time from output.
    """
    # 1. 12-hour format variants: "renewed at 10:30 PM", "resets 12am", "resets at 5:00 AM"
    # This regex matches "renewed at", "resets", "resets at" followed by a time like "12am", "10:30 PM", etc.
    match = re.search(r"(?:renewed at|resets\s+(?:at\s+)?)(\d{1,2}(?::\d{2})?\s*[AP]M)", output, re.IGNORECASE)
    if match:
        time_str = match.group(1).strip().upper()
        # Clean up space between numbers and AM/PM if missing
        time_str = re.sub(r"(\d)([AP]M)", r"\1 \2", time_str)
        
        now = datetime.datetime.now()
        target_time = None
        
        # Try different format strings
        for fmt in ["%I:%M %p", "%I %p"]:
            try:
                target_time = datetime.datetime.strptime(time_str, fmt).time()
                break
            except ValueError:
                continue
                
        if target_time:
            target_datetime = datetime.datetime.combine(now.date(), target_time)
            
            # If it's earlier than now, it's either tomorrow or within the same minute
            # For "12am", if now is 11pm, it's obviously tomorrow.
            if target_datetime < now:
                # If the target is more than 30 mins ago, it's almost certainly tomorrow
                if (now - target_datetime).total_seconds() > 1800:
                    target_datetime += datetime.timedelta(days=1)
                else:
                    # Same minute/recent, just wait a tiny bit
                    target_datetime = now + datetime.timedelta(seconds=2)
            
            return target_datetime

    # ISO format: "resets at 2026-03-07T05:00:00Z"
    match = re.search(r"resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", output, re.IGNORECASE)
    if match:
        time_str = match.group(1)
        try:
            return datetime.datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass

    return None

def kill_claude_instances():
    """
    Kills all running instances of the Claude CLI.
    Be careful not to kill unrelated processes like Antigravity.
    """
    logging.info("Cleaning up existing Claude processes...")
    try:
        # Use pgrep -x to match exactly the command 'claude'
        # This avoids killing 'claude-wrapper' or paths containing 'claude'
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
            except Exception as e:
                logging.error(f"Failed to kill {pid}: {e}")
    except subprocess.CalledProcessError:
        # No processes found
        pass

def get_current_limit():
    """
    Runs a minimal claude command to trigger the limit message and extract renewal time.
    """
    logging.info("Checking current usage status...")
    
    claude_cmd = os.environ.get("CLAUDE_COMMAND", "claude")
    
    # We try with -p first as it's cleaner
    variations = [
        ["-p", "status"],
        ["-p", "hi"],
        [] # Interactive mode as last resort
    ]
    
    for args in variations:
        if " " in claude_cmd:
            cmd = claude_cmd.split() + args
        else:
            cmd = [claude_cmd] + args
            
        logging.debug(f"Trying status check with: {' '.join(cmd)}")
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                # Use a pseudo-terminal for interactive mode if needed, 
                # but Popen with pipe usually works for reading.
            )
            
            full_output = []
            start_time = time.time()
            
            # Use a slightly longer timeout for the first attempt
            timeout = 10 if args else 5 
            
            while time.time() - start_time < timeout:
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                
                print(f"  [Output] {line.strip()}")
                full_output.append(line)
                
                # Check for renewal time immediately
                time_found = parse_renewal_time(line)
                if time_found:
                    process.terminate()
                    return time_found

            process.terminate()
            
            # Check the whole captured output buffer
            combined = "".join(full_output)
            time_found = parse_renewal_time(combined)
            if time_found:
                return time_found
                
        except Exception as e:
            logging.error(f"Error during variation {args}: {e}")
            
    return None

def main():
    # 1. Kill existing instances
    kill_claude_instances()
    time.sleep(1) # Give OS time to clean up

    # 2. Get renewal time
    renewal_datetime = get_current_limit()
    
    if not renewal_datetime:
        logging.error("Could not determine renewal time automatically.")
        logging.info("Could not find 'renewed at' or 'resets at' in output.")
        sys.exit(1)

    # 3. Wait
    now = datetime.datetime.now()
    wait_seconds = (renewal_datetime - now).total_seconds()
    
    # Add buffer (use 2s for testing if env set, otherwise 30s)
    buffer = 2 if os.environ.get("TEST_MODE") else 30
    wait_seconds += buffer
    
    if wait_seconds > 0:
        logging.info(f"Target renewal time: {renewal_datetime}")
        logging.info(f"Waiting for {wait_seconds:.0f} seconds...")
        time.sleep(wait_seconds)

    # 4. Resume
    logging.info("Resuming Claude Code session...")
    
    claude_cmd = os.environ.get("CLAUDE_COMMAND", "claude")
    if " " in claude_cmd: 
        # Handle cases like "python3 mock.py"
        parts = claude_cmd.split()
        os.execvp(parts[0], parts + ["-c"])
    else:
        os.execlp(claude_cmd, claude_cmd, "-c")

if __name__ == "__main__":
    main()
