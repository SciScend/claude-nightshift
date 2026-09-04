#!/usr/bin/env python3
"""
claude-nightshift — run Claude Code tasks autonomously until done (Pro/Max plans).

Runs a task, and each time the 5-hour session limit is hit it waits for the
window to reset and resumes — but only as long as your 7-day (weekly) usage is
still below a threshold you choose.

Usage:
    claude-nightshift "your task"
    claude-nightshift -t 85 "refactor the auth module"
    claude-nightshift -t 90 --model opus --effort medium "fix all lint errors"
    claude-nightshift --max-turns 50 "fix all lint errors"
    claude-nightshift --compress-after 2 "large refactor"
    claude-nightshift --resume                       # resume last session
    claude-nightshift -l "some task"                 # log output to nightshift_*.log
    claude-nightshift --log-file run.log "some task" # log output to specific file
    claude-nightshift --dry-run "some task"          # preview command without running

Advanced (full control over claude args):
    claude-nightshift -- claude -p "task" --model opus --allowedTools Edit,Write

How the weekly threshold works (-t/--threshold):
    The threshold is a GATE checked before each session starts, not a killer that
    interrupts a running session. Before launching (or relaunching) Claude, the
    weekly usage is checked; if it is at or above the threshold, nightshift stops
    and does not start another session. A session already in flight is always
    allowed to finish, so in-progress work and the status file survive.

    Pass --hard-stop for the old behaviour: a background thread polls usage every
    --interval seconds and terminates Claude mid-session once the threshold is
    reached. This loses unsaved work; it is off by default.

Where usage numbers come from (no tokens are spent on any of these):
    1. `rate_limit_event` messages in Claude's own stream-json output — free,
       already part of the session, and carries both windows plus the exact
       reset time. Used whenever it is fresher than --usage-max-age seconds.
    2. https://api.anthropic.com/api/oauth/usage — one ~0.4s HTTPS request,
       authorised with the OAuth token Claude Code maintains in
       ~/.claude/.credentials.json. This is the same endpoint /usage uses.
    3. `cclimits --claude --json` (or `npx cclimits` if not installed globally)
       — fallback, only if the two above fail.

    In a typical overnight run this means one HTTPS request at startup and one
    after each 5-hour wait; the rest is read out of the stream for free.

Resume strategy (token optimization):
    On 5-hour limit hits, the script resumes automatically. The first N resumes
    (default 3, set via --compress-after) resume the real conversation by session
    id, carrying full history. After that, it switches to compressed context: a
    fresh Claude session seeded with the original task and a progress summary
    that Claude writes to `.nightshift-status.md` before each session ends. This
    avoids unbounded context growth on multi-session tasks.

Notes:
    - --dangerously-skip-permissions is always added automatically
    - Runs use --output-format stream-json so limits are detected structurally
      (api_error_status 429 / rate_limit_event) instead of by matching English
      text. Use --raw-output to opt out and fall back to text matching.
    - Hitting --max-turns is treated as "session over, resume", not as a failure.
"""

import subprocess
import shutil
import sys
import os
import re
import json
import time
import datetime
import logging
import threading
import argparse
import signal
import urllib.request
import urllib.error

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
_stop_flag = False        # set to True when the hard-stop watchdog fires
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


def interruptible_sleep(seconds):
    """Sleep, waking early if the hard-stop watchdog fires. True if stopped."""
    end_time = time.time() + seconds
    while time.time() < end_time:
        if should_stop():
            return True
        time.sleep(min(5, max(0.1, end_time - time.time())))
    return False


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


def _prompt_flag_index(cmd):
    """Index of the -p/--print flag in a claude command list, or None."""
    for flag in ("-p", "--print"):
        try:
            return cmd.index(flag)
        except ValueError:
            continue
    return None


def extract_task_from_command(cmd):
    """Extract the prompt argument value from a claude command list."""
    idx = _prompt_flag_index(cmd)
    if idx is None or idx + 1 >= len(cmd):
        return None
    value = cmd[idx + 1]
    return None if value.startswith("-") else value


def inject_prompt(cmd, suffix):
    """Append text to the prompt argument in a command list. Returns a new list."""
    cmd = list(cmd)
    idx = _prompt_flag_index(cmd)
    if idx is not None and idx + 1 < len(cmd) and not cmd[idx + 1].startswith("-"):
        cmd[idx + 1] = cmd[idx + 1] + suffix
    return cmd


# ---------------------------------------------------------------------------
# Usage lookup
# ---------------------------------------------------------------------------

CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"


class Usage:
    """A snapshot of both rate-limit windows. Percentages are 0-100."""

    def __init__(self, weekly_pct=None, weekly_resets=None,
                 five_hour_pct=None, five_hour_resets=None,
                 status=None, source="unknown", observed_at=None):
        self.weekly_pct = weekly_pct
        self.weekly_resets = weekly_resets          # unix epoch seconds, or None
        self.five_hour_pct = five_hour_pct
        self.five_hour_resets = five_hour_resets    # unix epoch seconds, or None
        self.status = status                        # allowed|allowed_warning|rejected
        self.source = source
        self.observed_at = observed_at if observed_at is not None else time.time()

    @property
    def age(self):
        return time.time() - self.observed_at

    def describe(self):
        parts = []
        if self.five_hour_pct is not None:
            parts.append(f"5h {self.five_hour_pct:.0f}%")
        if self.weekly_pct is not None:
            parts.append(f"7d {self.weekly_pct:.0f}%")
        if self.five_hour_resets:
            parts.append("5h resets " + fmt_epoch(self.five_hour_resets))
        return ", ".join(parts) + f" (via {self.source})"


def fmt_epoch(epoch):
    """Format a unix timestamp in local time."""
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _read_oauth_token():
    """Read the OAuth access token Claude Code keeps refreshed on disk."""
    with open(CREDENTIALS_PATH, "r") as f:
        return json.load(f)["claudeAiOauth"]["accessToken"]


def fetch_usage_native(timeout=15):
    """
    Query the same endpoint /usage and cclimits use. Returns Usage or None.

    The endpoint reports `utilization` as a percentage (0-100).
    """
    token = _read_oauth_token()
    req = urllib.request.Request(
        USAGE_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)

    def window(name):
        w = data.get(name) or {}
        pct = w.get("utilization")
        resets = w.get("resets_at")
        epoch = None
        if resets:
            try:
                epoch = datetime.datetime.fromisoformat(resets).timestamp()
            except ValueError:
                epoch = None
        return (float(pct) if pct is not None else None), epoch

    weekly_pct, weekly_resets = window("seven_day")
    five_pct, five_resets = window("five_hour")

    if weekly_pct is None and five_pct is None:
        return None

    return Usage(weekly_pct=weekly_pct, weekly_resets=weekly_resets,
                 five_hour_pct=five_pct, five_hour_resets=five_resets,
                 source="oauth/usage")


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


def cclimits_command():
    """
    How to invoke cclimits: the binary on PATH if it is installed
    (`npm i -g cclimits`), otherwise via npx.
    """
    binary = shutil.which("cclimits")
    return [binary] if binary else ["npx", "cclimits"]


def _parse_cclimits_json(output):
    """
    Parse `cclimits --claude --json`, which reports both windows as
    {"used": "15.0%", "resets_at": "<iso8601>"}. Returns Usage or None.
    """
    claude = (json.loads(output) or {}).get("claude") or {}
    if claude.get("status") not in (None, "ok"):
        raise RuntimeError(f"cclimits status: {claude.get('status')}")

    def window(name):
        w = claude.get(name) or {}
        used = w.get("used")
        pct = None
        if used is not None:
            try:
                pct = float(str(used).rstrip("%"))
            except ValueError:
                pct = None
        epoch = None
        if w.get("resets_at"):
            try:
                epoch = datetime.datetime.fromisoformat(w["resets_at"]).timestamp()
            except ValueError:
                epoch = None
        return pct, epoch

    weekly_pct, weekly_resets = window("seven_day")
    five_pct, five_resets = window("five_hour")

    if weekly_pct is None and five_pct is None:
        return None

    return Usage(weekly_pct=weekly_pct, weekly_resets=weekly_resets,
                 five_hour_pct=five_pct, five_hour_resets=five_resets,
                 source="cclimits")


def fetch_usage_cclimits(timeout=60):
    """
    Fallback: shell out to cclimits. Prefers --json, which carries reset times
    for both windows; falls back to scraping the human-readable output for a
    cclimits too old to support it. Returns Usage or None.
    """
    base = cclimits_command()

    result = subprocess.run(
        base + ["--claude", "--json"],
        capture_output=True, text=True, timeout=timeout,
    )
    output = result.stdout + result.stderr
    if "429" in output or "Rate limit" in output or "Too Many Requests" in output:
        raise RuntimeError("cclimits: usage API rate limited")

    try:
        usage = _parse_cclimits_json(result.stdout)
        if usage:
            return usage
    except ValueError:
        pass  # not JSON — old cclimits, fall through to text scraping

    result = subprocess.run(
        base + ["--claude"],
        capture_output=True, text=True, timeout=timeout,
    )
    output = result.stdout + result.stderr
    if "429" in output or "Rate limit" in output or "Too Many Requests" in output:
        raise RuntimeError("cclimits: usage API rate limited")
    weekly = parse_7day_usage(output)
    if weekly is None:
        return None
    return Usage(weekly_pct=weekly, source="cclimits")


def fetch_usage(attempts=3):
    """
    Get a fresh usage snapshot, trying the native endpoint then cclimits.
    Returns Usage or None if every source failed.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            usage = fetch_usage_native()
            if usage:
                return usage
        except FileNotFoundError:
            last_error = f"no credentials at {CREDENTIALS_PATH}"
        except urllib.error.HTTPError as e:
            last_error = f"usage endpoint HTTP {e.code}"
            if e.code == 429:
                logger.warning("[usage] endpoint rate limited — backing off 60s")
                if interruptible_sleep(60):
                    return None
                continue
        except Exception as e:
            last_error = f"usage endpoint error: {e}"

        try:
            usage = fetch_usage_cclimits()
            if usage:
                return usage
        except Exception as e:
            last_error = f"{last_error}; cclimits error: {e}" if last_error else f"cclimits error: {e}"

        if attempt < attempts:
            logger.warning(f"[usage] lookup failed ({last_error}) — retry {attempt}/{attempts - 1}")
            if interruptible_sleep(5 * attempt):
                return None

    logger.warning(f"[usage] could not determine usage: {last_error}")
    return None


# ---------------------------------------------------------------------------
# Parsing helpers (legacy fallbacks for --raw-output / text mode)
# ---------------------------------------------------------------------------

LIMIT_PHRASES = (
    "usage limit reached",      # older Claude Code wording
    "hit your limit",           # "You've hit your limit · resets 3pm"
    "hit your usage limit",
    "hit your session limit",
    "reached your usage limit",
    "reached your weekly usage limit",
    "out of usage credits",
)


def looks_like_limit(line):
    """True if a raw output line looks like a usage-limit message."""
    low = line.lower()
    return any(p in low for p in LIMIT_PHRASES)


def parse_renewal_time(output):
    """
    Extract 5-hour reset time from Claude's limit message. Fallback only —
    structured detection via rate_limit_event / the usage endpoint is preferred.
    Handles:
      - "renewed at 10:30 PM"
      - "resets at 5:00 AM"
      - "resets 12am"           (current wording is lowercase and unspaced)
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

    # ISO format — parsed as UTC, then converted to local
    match = re.search(
        r"resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)",
        output,
        re.IGNORECASE
    )
    if match:
        try:
            dt = datetime.datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ")
            dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone().replace(tzinfo=None)
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# Hard-stop watchdog thread (opt-in via --hard-stop)
# ---------------------------------------------------------------------------

def watchdog_thread(threshold, interval):
    """
    Runs in background when --hard-stop is set. Checks weekly usage every
    `interval` seconds and terminates the running claude process once the
    threshold is reached. Off by default: it destroys in-progress work.
    """
    logger.info(f"[watchdog] Hard stop armed — threshold={threshold}%, interval={interval}s")

    while True:
        if interruptible_sleep(interval):
            break

        usage = fetch_usage(attempts=1)
        if usage is None or usage.weekly_pct is None:
            continue

        logger.info(f"[watchdog] 7-day usage: {usage.weekly_pct:.1f}%")

        if usage.weekly_pct >= threshold:
            reason = f"7-day usage {usage.weekly_pct:.1f}% reached threshold {threshold}%"
            logger.warning(f"[watchdog] 🛑 {reason} — stopping Claude")
            set_stop(reason)

            proc = get_current_process()
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        proc.kill()
            break

    logger.info("[watchdog] Exited")


# ---------------------------------------------------------------------------
# Session execution
# ---------------------------------------------------------------------------

class SessionResult:
    """What happened during one `claude` invocation."""

    def __init__(self):
        self.returncode = None
        self.session_id = None
        self.result = None       # the final stream-json result object, if any
        self.usage = None        # Usage built from the last rate_limit_event
        self.output_text = ""
        self.text_limit_seen = False


def _render_stream_event(event):
    """Turn one stream-json event into a human-readable line, or None to skip."""
    etype = event.get("type")

    if etype == "assistant":
        out = []
        for block in event.get("message", {}).get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    out.append(text)
            elif btype == "tool_use":
                name = block.get("name", "tool")
                args = block.get("input", {}) or {}
                detail = (args.get("command") or args.get("file_path")
                          or args.get("pattern") or args.get("path") or "")
                detail = str(detail).replace("\n", " ")
                if len(detail) > 100:
                    detail = detail[:97] + "..."
                out.append(f"⏺ {name}({detail})")
        return "\n".join(out) if out else None

    if etype == "result":
        subtype = event.get("subtype")
        turns = event.get("num_turns")
        secs = (event.get("duration_ms") or 0) / 1000.0
        return f"— session ended: {subtype} ({turns} turns, {secs:.0f}s)"

    return None


def _usage_from_rate_limit_event(event):
    """
    Build a Usage from a stream `rate_limit_event`.

    Note the scale difference: unifiedWindows utilization is a FRACTION (0-1),
    whereas the /api/oauth/usage endpoint reports a PERCENTAGE (0-100).
    """
    info = event.get("rate_limit_info") or {}
    windows = info.get("unifiedWindows") or {}

    def window(name):
        w = windows.get(name) or {}
        util = w.get("utilization")
        return (float(util) * 100.0 if util is not None else None), w.get("resetsAt")

    weekly_pct, weekly_resets = window("seven_day")
    five_pct, five_resets = window("five_hour")

    if five_resets is None and info.get("rateLimitType") == "five_hour":
        five_resets = info.get("resetsAt")

    if weekly_pct is None and five_pct is None:
        return None

    return Usage(weekly_pct=weekly_pct, weekly_resets=weekly_resets,
                 five_hour_pct=five_pct, five_hour_resets=five_resets,
                 status=info.get("status"), source="stream")


def run_session(cmd, structured=True):
    """
    Launch one claude process, stream its output, and collect what we need.
    Returns a SessionResult.
    """
    session = SessionResult()

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            start_new_session=True,   # own process group, so we can kill children
        )
    except FileNotFoundError as e:
        logger.error(f"Could not start process: {e}")
        session.returncode = 127
        return session

    set_current_process(proc)
    collected = []

    # readline() rather than iteration: guarantees we see each line as it lands
    for line in iter(proc.stdout.readline, ''):
        collected.append(line)

        event = None
        if structured:
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    event = json.loads(stripped)
                except ValueError:
                    event = None

        if event is None:
            # Non-JSON line (or raw text mode): pass through untouched.
            print(line, end='', flush=True)
            log_output(line)
            if looks_like_limit(line):
                session.text_limit_seen = True
            continue

        etype = event.get("type")
        if event.get("session_id"):
            session.session_id = event["session_id"]

        if etype == "rate_limit_event":
            usage = _usage_from_rate_limit_event(event)
            if usage:
                session.usage = usage
            continue

        if etype == "result":
            session.result = event

        rendered = _render_stream_event(event)
        if rendered:
            out = rendered + "\n"
            print(out, end='', flush=True)
            log_output(out)

    proc.wait()
    set_current_process(None)

    session.returncode = proc.returncode
    session.output_text = "".join(collected)
    return session


def classify(session):
    """
    Decide what a finished session means.
    Returns one of: 'success', 'limit', 'max_turns', 'transient', 'failed'.
    """
    result = session.result

    if result is not None:
        # Structured result available — trust it over any text matching.
        if result.get("subtype") == "success" and not result.get("is_error"):
            return "success"
        if result.get("subtype") == "error_max_turns":
            return "max_turns"
        if result.get("api_error_status") == 429:
            # Transient 429s unrelated to your plan limit are already retried
            # internally for subscribers, so a 429 surfacing here is the real one.
            return "limit"
        errors = " ".join(str(e) for e in (result.get("errors") or []))
        if errors and looks_like_limit(errors):
            return "limit"

    if session.usage and session.usage.status == "rejected":
        return "limit"

    if session.text_limit_seen:
        return "limit"

    if session.returncode == 0:
        return "success"

    # "Error: Reached max turns (N)" in raw text mode
    if re.search(r"reached max turns", session.output_text, re.IGNORECASE):
        return "max_turns"

    transient_patterns = [
        "authentication_error", "token has expired",
        "econnrefused", "network error", "502 bad gateway",
        "503 service", "socket hang up",
    ]
    if any(p in session.output_text.lower() for p in transient_patterns):
        return "transient"

    return "failed"


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def weekly_gate(threshold, cached, max_age, allow_unknown):
    """
    Check whether another session may start.

    Uses `cached` (from the last rate_limit_event) when it is younger than
    max_age seconds, otherwise queries the usage endpoint.
    Returns (allowed: bool, reason: str, usage: Usage or None).
    """
    usage = cached if (cached and cached.weekly_pct is not None
                       and cached.age <= max_age) else fetch_usage()

    if usage is None or usage.weekly_pct is None:
        if allow_unknown:
            return True, "weekly usage unknown — proceeding (--allow-unknown-usage)", None
        return False, "weekly usage could not be determined (use --allow-unknown-usage to proceed anyway)", None

    if usage.weekly_pct >= threshold:
        return False, f"7-day usage {usage.weekly_pct:.1f}% is at or above threshold {threshold}%", usage

    return True, f"7-day usage {usage.weekly_pct:.1f}% (threshold {threshold}%)", usage


def resolve_wait_target(session):
    """
    Work out when the 5-hour window resets. Returns unix epoch seconds or None.
    Prefers the exact reset time Claude streamed to us, then the usage endpoint,
    then falls back to parsing the message text.
    """
    if session.usage and session.usage.five_hour_resets:
        return session.usage.five_hour_resets

    usage = fetch_usage(attempts=2)
    if usage and usage.five_hour_resets:
        return usage.five_hour_resets

    parsed = parse_renewal_time(session.output_text)
    if parsed:
        return parsed.timestamp()

    return None


def build_resume_command(retries, compress_after, session_id, original_task,
                         base_flags):
    """Build the command for resume attempt number `retries`."""
    cmd = ["claude", "--dangerously-skip-permissions"] + list(base_flags)

    if retries < compress_after and session_id:
        # Full context resume — continue the real conversation by id.
        cmd.extend(["--resume", session_id, "-p", "continue" + STATUS_INSTRUCTION])
        return cmd, f"full context (--resume {session_id[:8]})"

    if retries < compress_after:
        # No session id captured; fall back to "most recent in this directory".
        cmd.extend(["-c", "-p", "continue" + STATUS_INSTRUCTION])
        return cmd, "full context (-c)"

    # Compressed context resume — fresh start with status summary.
    status = read_status_file()
    parts = []
    if original_task:
        parts.append(f"Original task: {original_task}")
    if status:
        parts.append(f"Progress from previous session:\n\n{status}")
    parts.append("Continue where you left off." + STATUS_INSTRUCTION)
    cmd.extend(["-p", "\n\n".join(parts)])
    return cmd, "compressed context (fresh)"


def run(command, threshold, max_retries=10, compress_after=3, base_flags=(),
        structured=True, usage_max_age=120, allow_unknown=False):
    original_task = extract_task_from_command(command)

    # Inject status-file instruction so Claude writes progress before session ends
    if original_task:
        command = inject_prompt(command, STATUS_INSTRUCTION)

    first_run = True
    retries = 0
    session_id = None
    cached_usage = None

    while retries < max_retries:
        if should_stop():
            logger.info(f"Stopped before retry: {_stop_reason}")
            return False

        # --- the gate: may we start another session? ---
        allowed, reason, usage = weekly_gate(threshold, cached_usage,
                                             usage_max_age, allow_unknown)
        if usage:
            cached_usage = usage
        if not allowed:
            logger.warning(f"🛑 Not starting another session: {reason}")
            return False
        logger.info(f"Gate open: {reason}")

        if first_run:
            cmd = list(command)
            if "--dangerously-skip-permissions" not in cmd:
                cmd.insert(1, "--dangerously-skip-permissions")
            first_run = False
            strategy = "initial run"
        else:
            cmd, strategy = build_resume_command(
                retries, compress_after, session_id, original_task, base_flags)
            logger.info(f"Resume {retries} using {strategy}")

        logger.info(f"Running: {' '.join(cmd)}")

        session = run_session(cmd, structured=structured)
        if session.session_id:
            session_id = session.session_id
        if session.usage:
            cached_usage = session.usage
            logger.info(f"[usage] {session.usage.describe()}")

        if should_stop():
            logger.info(f"\n🛑 Stopped by watchdog: {_stop_reason}")
            return False

        outcome = classify(session)

        if outcome == "success":
            logger.info("✅ Task completed successfully.")
            return True

        if outcome == "limit":
            target = resolve_wait_target(session)
            if target:
                wait_seconds = max(0, target - time.time()) + 30
                logger.warning(
                    f"5-hour limit reached. Resuming at {fmt_epoch(target)} "
                    f"(~{wait_seconds / 60:.1f} minutes)."
                )
            else:
                wait_seconds = 600
                logger.error("Could not determine reset time. Waiting 10 minutes as fallback.")

            if interruptible_sleep(wait_seconds):
                logger.info(f"Stopped during wait: {_stop_reason}")
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
            if interruptible_sleep(backoff):
                logger.info(f"Stopped during backoff: {_stop_reason}")
                return False
            continue

        logger.error(f"Claude exited with code {session.returncode}.")
        return False

    logger.error("Max retries reached.")
    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_claude_command(args, base_flags, structured):
    """
    Build the claude command list from parsed args.

    Supports two modes:
      1. Simple: task prompt as positional arg (with optional --model, --resume)
      2. Advanced: raw claude args passed via args.claude_raw
    """
    if args.claude_raw:
        cmd = list(args.claude_raw)
        if structured and "--output-format" not in cmd:
            cmd.extend(["--output-format", "stream-json", "--verbose"])
        return cmd

    # --resume mode: no prompt needed
    if args.resume:
        return ["claude"] + list(base_flags) + ["-c", "-p", "continue"]

    # Simple mode: build from task + optional flags
    if not args.task:
        return None

    return ["claude"] + list(base_flags) + ["-p", args.task]


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
        description="Run Claude Code autonomously until done. Waits out each 5-hour "
                    "session limit and resumes, as long as weekly usage stays below "
                    "--threshold.",
        epilog="Examples:\n"
               '  claude-nightshift "fix all lint errors"\n'
               '  claude-nightshift -t 85 --model opus "refactor auth"\n'
               '  claude-nightshift -t 90 --effort medium "long refactor"\n'
               '  claude-nightshift --resume\n'
               '  claude-nightshift -- claude -p "task" --allowedTools Edit\n'
               "\n"
               "The weekly threshold is checked before each session starts; a running\n"
               "session is never interrupted unless you pass --hard-stop.\n"
               "Usage figures are read from Claude's own stream, then the OAuth usage\n"
               "endpoint, then cclimits — none of them cost tokens.\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task", nargs="?", default=None,
                        help="Task prompt for Claude (quoted string)")
    parser.add_argument("-t", "--threshold", type=float, default=70.0,
                        help="7-day usage %% at or above which no new session is "
                             "started (default: 70)")
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

    args = parser.parse_args(argv)
    args.claude_raw = claude_raw

    structured = not args.raw_output

    # Flags that apply to every session, including resumes.
    base_flags = []
    if args.model:
        base_flags.extend(["--model", args.model])
    if args.effort:
        base_flags.extend(["--effort", args.effort])
    if args.max_turns:
        base_flags.extend(["--max-turns", str(args.max_turns)])
    if structured:
        base_flags.extend(["--output-format", "stream-json", "--verbose"])

    claude_cmd = build_claude_command(args, base_flags, structured)

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

    logger.info("Starting claude-nightshift")
    logger.info(f"  Command       : {' '.join(claude_cmd)}")
    logger.info(f"  Weekly gate   : start a session only below {args.threshold}%")
    logger.info(f"  Max retries   : {args.max_retries}")
    logger.info(f"  Compress after: {args.compress_after} resumes")
    if args.effort:
        logger.info(f"  Effort        : {args.effort}")
    if args.max_turns:
        logger.info(f"  Max turns     : {args.max_turns} (resumable)")
    if args.hard_stop:
        logger.info(f"  Hard stop     : on, polling every {args.interval}s")

    # Start the hard-stop watchdog only when explicitly requested
    if args.hard_stop:
        t = threading.Thread(
            target=watchdog_thread,
            args=(args.threshold, args.interval),
            daemon=True
        )
        t.start()

    # Handle Ctrl+C gracefully
    def handle_sigint(sig, frame):
        logger.info("\nInterrupted by user.")
        set_stop("interrupted by user")
        proc = get_current_process()
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    success = run(claude_cmd, args.threshold,
                  max_retries=args.max_retries,
                  compress_after=args.compress_after,
                  base_flags=base_flags,
                  structured=structured,
                  usage_max_age=args.usage_max_age,
                  allow_unknown=args.allow_unknown_usage)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
