"""Running one `claude` invocation and deciding what its outcome means."""

from __future__ import annotations

import json
import re
import subprocess
import time

from claude_nightshift.control import set_current_process
from claude_nightshift.limits import looks_like_limit
from claude_nightshift.logsetup import get_logger, log_output
from claude_nightshift.timeutil import parse_iso_epoch
from claude_nightshift.usage import Usage

__all__ = ["SessionResult", "run_session", "classify", "usage_from_rate_limit_event"]

logger = get_logger(__name__)

TRANSIENT_PATTERNS = (
    "authentication_error",
    "token has expired",
    "econnrefused",
    "network error",
    "502 bad gateway",
    "503 service",
    "socket hang up",
)

_MAX_TURNS_RE = re.compile(r"reached max turns", re.IGNORECASE)


class SessionResult:
    """What happened during one `claude` invocation."""

    def __init__(self):
        self.returncode = None
        self.session_id = None
        self.result = None  # the final stream-json result object, if any
        self.usage = None  # Usage built from the last rate_limit_event
        self.output_text = ""
        self.text_limit_seen = False
        self.duration = 0.0  # wall-clock seconds the claude process ran


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
                detail = (
                    args.get("command")
                    or args.get("file_path")
                    or args.get("pattern")
                    or args.get("path")
                    or ""
                )
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


def _reset_epoch(value):
    """Normalise a stream reset marker to unix epoch seconds.

    The stream has used both a raw epoch number and an ISO-8601 string for
    ``resetsAt`` across Claude Code versions; accept either.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return parse_iso_epoch(value)


def usage_from_rate_limit_event(event):
    """Build a :class:`Usage` from a stream ``rate_limit_event``.

    Note the scale difference: ``unifiedWindows`` utilization is a FRACTION
    (0-1), whereas ``/api/oauth/usage`` reports a PERCENTAGE (0-100).
    """
    info = event.get("rate_limit_info") or {}
    windows = info.get("unifiedWindows") or {}

    def window(name):
        w = windows.get(name) or {}
        util = w.get("utilization")
        pct = float(util) * 100.0 if util is not None else None
        return pct, _reset_epoch(w.get("resetsAt"))

    weekly_pct, weekly_resets = window("seven_day")
    five_pct, five_resets = window("five_hour")

    if five_resets is None and info.get("rateLimitType") == "five_hour":
        five_resets = _reset_epoch(info.get("resetsAt"))

    if weekly_pct is None and five_pct is None:
        return None

    return Usage(
        weekly_pct=weekly_pct,
        weekly_resets=weekly_resets,
        five_hour_pct=five_pct,
        five_hour_resets=five_resets,
        status=info.get("status"),
        source="stream",
    )


def run_session(cmd, structured=True) -> SessionResult:
    """Launch one claude process, stream its output, and collect what we need."""
    session = SessionResult()
    started_at = time.time()

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            start_new_session=True,  # own process group, so we can kill children
        )
    except (FileNotFoundError, PermissionError) as e:
        logger.error(f"Could not start process: {e}")
        session.returncode = 127
        return session

    set_current_process(proc)
    collected = []

    # readline() rather than iteration: guarantees we see each line as it lands
    for line in iter(proc.stdout.readline, ""):
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
            print(line, end="", flush=True)
            log_output(line)
            if looks_like_limit(line):
                session.text_limit_seen = True
            continue

        etype = event.get("type")
        if event.get("session_id"):
            session.session_id = event["session_id"]

        if etype == "rate_limit_event":
            usage = usage_from_rate_limit_event(event)
            if usage:
                session.usage = usage
            continue

        if etype == "result":
            session.result = event

        rendered = _render_stream_event(event)
        if rendered:
            out = rendered + "\n"
            print(out, end="", flush=True)
            log_output(out)

    proc.wait()
    set_current_process(None)

    session.returncode = proc.returncode
    session.output_text = "".join(collected)
    session.duration = time.time() - started_at
    return session


def classify(session: SessionResult) -> str:
    """Decide what a finished session means.

    Returns one of ``success``, ``limit``, ``max_turns``, ``transient``, ``failed``.
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
    if _MAX_TURNS_RE.search(session.output_text):
        return "max_turns"

    lowered = session.output_text.lower()
    if any(pattern in lowered for pattern in TRANSIENT_PATTERNS):
        return "transient"

    return "failed"
