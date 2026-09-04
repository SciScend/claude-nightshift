"""Time parsing and formatting helpers shared across the package."""

from __future__ import annotations

import argparse
import datetime
import re

__all__ = ["fmt_epoch", "parse_duration", "parse_iso_epoch"]

_DURATION_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", re.IGNORECASE)
_DURATION_SCALE = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 3600}


def fmt_epoch(epoch: float) -> str:
    """Format a unix timestamp in local time."""
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def parse_iso_epoch(value):
    """Convert an ISO-8601 timestamp to unix epoch seconds, or None.

    The usage APIs emit UTC timestamps, usually with a trailing ``Z`` that
    ``datetime.fromisoformat`` only learned to accept in Python 3.11. Timestamps
    without an offset are read as UTC rather than local time, which is what the
    API means and what naive ``fromisoformat`` would get wrong.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def parse_duration(text) -> float:
    """Parse '20h', '90m', '2d', '600s', or a bare number of hours, into seconds."""
    match = _DURATION_RE.fullmatch(str(text).lower())
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid duration {text!r} — use e.g. 20h, 90m, 2d"
        )
    return float(match.group(1)) * _DURATION_SCALE[match.group(2)]
