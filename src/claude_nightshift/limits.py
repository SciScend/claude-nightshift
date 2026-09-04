"""Text fallbacks for limit detection.

Structured detection via ``rate_limit_event`` / the usage endpoint is always
preferred; everything here exists for ``--raw-output`` and for output that
predates the structured events.
"""

from __future__ import annotations

import datetime
import re

__all__ = ["LIMIT_PHRASES", "looks_like_limit", "parse_renewal_time"]

LIMIT_PHRASES = (
    "usage limit reached",  # older Claude Code wording
    "hit your limit",  # "You've hit your limit · resets 3pm"
    "hit your usage limit",
    "hit your session limit",
    "reached your usage limit",
    "reached your weekly usage limit",
    "out of usage credits",
)

_CLOCK_RE = re.compile(
    r"(?:renewed at|resets\s+(?:at\s+)?)(\d{1,2}(?::\d{2})?\s*[AP]M)", re.IGNORECASE
)
_ISO_RE = re.compile(
    r"resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", re.IGNORECASE
)


def looks_like_limit(line: str) -> bool:
    """True if a raw output line looks like a usage-limit message."""
    low = line.lower()
    return any(phrase in low for phrase in LIMIT_PHRASES)


def parse_renewal_time(output: str):
    """Extract the 5-hour reset time from Claude's limit message.

    Handles ``renewed at 10:30 PM``, ``resets at 5:00 AM``, ``resets 12am`` and
    ``resets at 2026-03-07T05:00:00Z``. Returns a naive local datetime, or None.
    """
    match = _CLOCK_RE.search(output)
    if match:
        time_str = re.sub(r"(\d)([AP]M)", r"\1 \2", match.group(1).strip().upper())
        now = datetime.datetime.now()
        for fmt in ("%I:%M %p", "%I %p"):
            try:
                parsed = datetime.datetime.strptime(time_str, fmt).time()
            except ValueError:
                continue
            target = datetime.datetime.combine(now.date(), parsed)
            if target < now:
                # More than half an hour in the past means tomorrow; a few
                # minutes means the window has just turned over.
                target += (
                    datetime.timedelta(days=1)
                    if (now - target).total_seconds() > 1800
                    else datetime.timedelta(seconds=2)
                )
            return target

    match = _ISO_RE.search(output)
    if match:
        try:
            parsed = datetime.datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone().replace(tzinfo=None)

    return None
