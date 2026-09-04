"""Reading rate-limit usage without spending tokens.

Three sources, in priority order:

1. ``rate_limit_event`` messages in Claude's own ``stream-json`` output — free,
   already part of the session, and carrying both windows plus the exact reset
   time (see :mod:`claude_nightshift.session`).
2. ``https://api.anthropic.com/api/oauth/usage`` — one HTTPS request authorised
   with the OAuth token Claude Code maintains in ``~/.claude/.credentials.json``.
   This is the endpoint ``/usage`` itself uses.
3. ``cclimits --claude --json`` — fallback, only if the two above fail.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from claude_nightshift.control import interruptible_sleep
from claude_nightshift.logsetup import get_logger
from claude_nightshift.timeutil import fmt_epoch, parse_iso_epoch

__all__ = [
    "Usage",
    "CREDENTIALS_PATH",
    "USAGE_ENDPOINT",
    "fetch_usage",
    "fetch_usage_native",
    "fetch_usage_cclimits",
    "parse_7day_usage",
    "cclimits_command",
]

logger = get_logger(__name__)

CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"

RATE_LIMITED_MARKERS = ("429", "Rate limit", "Too Many Requests")


class Usage:
    """A snapshot of both rate-limit windows. Percentages are 0-100."""

    def __init__(
        self,
        weekly_pct=None,
        weekly_resets=None,
        five_hour_pct=None,
        five_hour_resets=None,
        status=None,
        source="unknown",
        observed_at=None,
    ):
        self.weekly_pct = weekly_pct
        self.weekly_resets = weekly_resets  # unix epoch seconds, or None
        self.five_hour_pct = five_hour_pct
        self.five_hour_resets = five_hour_resets  # unix epoch seconds, or None
        self.status = status  # allowed|allowed_warning|rejected
        self.source = source
        self.observed_at = observed_at if observed_at is not None else time.time()

    @property
    def age(self) -> float:
        return time.time() - self.observed_at

    def describe(self) -> str:
        parts = []
        if self.five_hour_pct is not None:
            parts.append(f"5h {self.five_hour_pct:.0f}%")
        if self.weekly_pct is not None:
            parts.append(f"7d {self.weekly_pct:.0f}%")
        if self.five_hour_resets:
            parts.append("5h resets " + fmt_epoch(self.five_hour_resets))
        prefix = ", ".join(parts)
        return f"{prefix} (via {self.source})" if prefix else f"no data (via {self.source})"

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Usage {self.describe()}>"


def _as_float(value):
    """Coerce '15.0%', '15.0' or 15.0 to a float, or None."""
    if value is None:
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except (ValueError, TypeError):
        return None


def _read_oauth_token() -> str:
    """Read the OAuth access token Claude Code keeps refreshed on disk."""
    with open(CREDENTIALS_PATH) as f:
        return json.load(f)["claudeAiOauth"]["accessToken"]


def fetch_usage_native(timeout=15):
    """Query the same endpoint ``/usage`` and cclimits use. Returns Usage or None.

    The endpoint reports ``utilization`` as a percentage (0-100).
    """
    token = _read_oauth_token()
    req = urllib.request.Request(
        USAGE_ENDPOINT,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)

    def window(name):
        w = (data or {}).get(name) or {}
        return _as_float(w.get("utilization")), parse_iso_epoch(w.get("resets_at"))

    weekly_pct, weekly_resets = window("seven_day")
    five_pct, five_resets = window("five_hour")

    if weekly_pct is None and five_pct is None:
        return None

    return Usage(
        weekly_pct=weekly_pct,
        weekly_resets=weekly_resets,
        five_hour_pct=five_pct,
        five_hour_resets=five_resets,
        source="oauth/usage",
    )


def parse_7day_usage(cclimits_output: str):
    """Extract the 7-day Used percentage from human-readable cclimits output.

    Looks for the block::

        7-Day Window:
          Used:      41.0%

    Returns float or None.
    """
    match = re.search(
        r"7-Day Window.*?Used:\s+([\d.]+)%",
        cclimits_output,
        re.DOTALL | re.IGNORECASE,
    )
    return _as_float(match.group(1)) if match else None


def cclimits_command():
    """How to invoke cclimits: the binary on PATH if installed, else via npx."""
    binary = shutil.which("cclimits")
    return [binary] if binary else ["npx", "cclimits"]


def _parse_cclimits_json(output: str):
    """Parse ``cclimits --claude --json``. Returns Usage or None."""
    claude = (json.loads(output) or {}).get("claude") or {}
    if claude.get("status") not in (None, "ok"):
        raise RuntimeError(f"cclimits status: {claude.get('status')}")

    def window(name):
        w = claude.get(name) or {}
        return _as_float(w.get("used")), parse_iso_epoch(w.get("resets_at"))

    weekly_pct, weekly_resets = window("seven_day")
    five_pct, five_resets = window("five_hour")

    if weekly_pct is None and five_pct is None:
        return None

    return Usage(
        weekly_pct=weekly_pct,
        weekly_resets=weekly_resets,
        five_hour_pct=five_pct,
        five_hour_resets=five_resets,
        source="cclimits",
    )


def _raise_if_rate_limited(output: str) -> None:
    if any(marker in output for marker in RATE_LIMITED_MARKERS):
        raise RuntimeError("cclimits: usage API rate limited")


def fetch_usage_cclimits(timeout=60):
    """Fallback: shell out to cclimits.

    Prefers ``--json``, which carries reset times for both windows; falls back to
    scraping the human-readable output for a cclimits too old to support it.
    Returns Usage or None.
    """
    base = cclimits_command()

    result = subprocess.run(
        base + ["--claude", "--json"], capture_output=True, text=True, timeout=timeout
    )
    _raise_if_rate_limited(result.stdout + result.stderr)

    try:
        usage = _parse_cclimits_json(result.stdout)
        if usage:
            return usage
    except ValueError:
        pass  # not JSON — old cclimits, fall through to text scraping

    result = subprocess.run(
        base + ["--claude"], capture_output=True, text=True, timeout=timeout
    )
    output = result.stdout + result.stderr
    _raise_if_rate_limited(output)

    weekly = parse_7day_usage(output)
    if weekly is None:
        return None
    return Usage(weekly_pct=weekly, source="cclimits")


def fetch_usage(attempts=3):
    """Get a fresh usage snapshot, trying the native endpoint then cclimits.

    Returns Usage, or None if every source failed.
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
        except Exception as e:  # noqa: BLE001 - any failure falls through to cclimits
            last_error = f"usage endpoint error: {e}"

        try:
            usage = fetch_usage_cclimits()
            if usage:
                return usage
        except Exception as e:  # noqa: BLE001 - report and retry
            detail = f"cclimits error: {e}"
            last_error = f"{last_error}; {detail}" if last_error else detail

        if attempt < attempts:
            logger.warning(
                f"[usage] lookup failed ({last_error}) — retry {attempt}/{attempts - 1}"
            )
            if interruptible_sleep(5 * attempt):
                return None

    logger.warning(f"[usage] could not determine usage: {last_error}")
    return None
