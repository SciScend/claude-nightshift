"""Logging configuration and raw Claude output passthrough."""

from __future__ import annotations

import logging
import sys

__all__ = ["get_logger", "setup_logging", "log_output", "close_log_file"]

_PACKAGE_LOGGER = "claude_nightshift"

# Open file handle when file logging is enabled, else None.
_log_file = None


def get_logger(name: str) -> logging.Logger:
    """Return a package logger; handlers live on the package root logger."""
    return logging.getLogger(name)


def setup_logging(log_path=None, level=logging.INFO) -> None:
    """Configure logging to stderr, and optionally also to a file."""
    global _log_file

    logger = logging.getLogger(_PACKAGE_LOGGER)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)
    logger.setLevel(level)

    if log_path:
        close_log_file()
        _log_file = open(log_path, "a", buffering=1)  # line-buffered
        file_handler = logging.StreamHandler(_log_file)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)


def log_output(line: str) -> None:
    """Write a line of Claude's own output to the log file, if one is open."""
    if _log_file:
        _log_file.write(line)
        _log_file.flush()


def close_log_file() -> None:
    """Close the file handle opened by :func:`setup_logging`, if any."""
    global _log_file
    if _log_file:
        try:
            _log_file.close()
        finally:
            _log_file = None
