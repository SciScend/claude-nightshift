"""The status file that carries progress between compressed-context resumes."""

from __future__ import annotations

__all__ = [
    "STATUS_FILE",
    "STATUS_INSTRUCTION",
    "read_status_file",
    "extract_task_from_command",
    "inject_prompt",
]

STATUS_FILE = ".nightshift-status.md"

STATUS_INSTRUCTION = (
    "\n\nBefore your session ends, write a concise progress summary to "
    f"`{STATUS_FILE}`: what's done, key decisions, and next steps."
)


def read_status_file(path: str = STATUS_FILE):
    """Read the compressed-context status file, if it exists."""
    try:
        with open(path) as f:
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
