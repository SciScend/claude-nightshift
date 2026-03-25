#!/usr/bin/env python3
import subprocess
import sys
import re
import time
import datetime
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def parse_renewal_time(output):
    """
    Parses renewal time from output.
    Expected formats: 
    - "renewed at 10:30 PM"
    - "renewed at 05:00 AM"
    - "resets at 2026-03-07T05:00:00Z" (ISO format)
    """
    # 1. 12-hour format variants: "renewed at 10:30 PM", "resets 12am", "resets at 5:00 AM"
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

    # Try ISO format
    match = re.search(r"resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", output, re.IGNORECASE)
    if match:
        time_str = match.group(1)
        try:
            return datetime.datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass

    return None

def run_claude(command, max_retries=5):
    retries = 0
    while retries < max_retries:
        logging.info(f"Running command: {' '.join(command)}")
        
        # Start the process
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        full_output = []
        limit_reached = False
        renewal_datetime = None

        # Read output in real-time
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                print(line, end='', flush=True)
                full_output.append(line)
                
                # Check for limit message
                if "Usage limit reached" in line or "limit reached" in line.lower():
                    limit_reached = True
                
                # Try to extract time even if "limit reached" wasn't in the same line
                if limit_reached and not renewal_datetime:
                    renewal_datetime = parse_renewal_time(line)

        process.wait()
        
        if limit_reached:
            # If we didn't get the time from the specific line, check the whole output
            if not renewal_datetime:
                renewal_datetime = parse_renewal_time("".join(full_output))
            
            if renewal_datetime:
                now = datetime.datetime.now()
                wait_seconds = (renewal_datetime - now).total_seconds()
                
                # Add a small buffer (e.g., 30 seconds)
                wait_seconds += 30
                
                if wait_seconds > 0:
                    logging.warning(f"Rate limit reached. Waiting until {renewal_datetime} ({wait_seconds:.0f} seconds)...")
                    time.sleep(wait_seconds)
                    retries += 1
                    continue
                else:
                    logging.info("Renewal time has already passed. Retrying immediately.")
                    retries += 1
                    continue
            else:
                logging.error("Rate limit reached but could not parse renewal time. Waiting 5 minutes by default.")
                time.sleep(300)
                retries += 1
                continue
        
        # If we reach here, either it succeeded or died for another reason
        if process.returncode == 0:
            logging.info("Command completed successfully.")
            return True
        else:
            logging.error(f"Command failed with exit code {process.returncode}.")
            return False

    logging.error("Max retries reached.")
    return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 claude_wrapper.py <command> [args...]")
        sys.exit(1)
    
    success = run_claude(sys.argv[1:])
    sys.exit(0 if success else 1)
