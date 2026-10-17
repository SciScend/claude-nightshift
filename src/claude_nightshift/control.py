"""Shared run-control state: the live child process and the stop flag.

Kept in one module so the main thread, the optional hard-stop watchdog and the
SIGINT handler all mutate the same guarded state instead of module globals
scattered across the runner.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

__all__ = [
    "set_current_process",
    "get_current_process",
    "set_stop",
    "should_stop",
    "stop_reason",
    "stop_kind",
    "reset",
    "interruptible_sleep",
    "terminate_process",
]

_lock = threading.Lock()
_current_process = None  # the live claude subprocess
_stop_flag = False  # set when the watchdog or the user asks us to stop
_stop_reason = ""  # human-readable reason for stopping
_stop_kind = ""  # machine-readable: "interrupted" or "weekly_limit"


def set_current_process(proc) -> None:
    global _current_process
    with _lock:
        _current_process = proc


def get_current_process():
    with _lock:
        return _current_process


def set_stop(reason: str, kind: str = "interrupted") -> None:
    global _stop_flag, _stop_reason, _stop_kind
    with _lock:
        _stop_flag = True
        _stop_reason = reason
        _stop_kind = kind


def should_stop() -> bool:
    with _lock:
        return _stop_flag


def stop_reason() -> str:
    with _lock:
        return _stop_reason


def stop_kind() -> str:
    with _lock:
        return _stop_kind


def reset() -> None:
    """Clear all run-control state. Used by tests and by repeated in-process runs."""
    global _current_process, _stop_flag, _stop_reason, _stop_kind
    with _lock:
        _current_process = None
        _stop_flag = False
        _stop_reason = ""
        _stop_kind = ""


def interruptible_sleep(seconds: float) -> bool:
    """Sleep, waking early if a stop was requested. True if it was."""
    end_time = time.time() + seconds
    while time.time() < end_time:
        if should_stop():
            return True
        time.sleep(min(5, max(0.1, end_time - time.time())))
    return should_stop()


def terminate_process(proc, grace: float = 10) -> None:
    """Terminate a process group, escalating to SIGKILL after `grace` seconds."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
