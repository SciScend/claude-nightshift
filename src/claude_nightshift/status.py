"""The status file that carries progress between compressed-context resumes."""

from __future__ import annotations

__all__ = [
    "STATUS_FILE",
    "STATUS_INSTRUCTION",
    "set_status_file",
    "status_file",
    "status_instruction",
    "read_status_file",
    "extract_task_from_command",
    "inject_prompt",
]

STATUS_FILE = ".nightshift-status.md"

# The file actually used; --status-file changes it.
_status_path = STATUS_FILE


def _instruction(path):
    return (
        "\n\nBefore your session ends, write a concise progress summary to "
        f"`{path}`: what's done, key decisions, and next steps."
    )


STATUS_INSTRUCTION = _instruction(STATUS_FILE)


def set_status_file(path=None):
    """Use ``path`` as the status file (``None`` restores the default)."""
    global _status_path
    _status_path = path or STATUS_FILE


def status_file():
    """The status file in use."""
    return _status_path


def status_instruction():
    """The instruction appended to prompts, naming the status file in use."""
    return _instruction(_status_path)


def read_status_file(path=None):
    """Read the compressed-context status file, if it exists."""
    if path is None:
        path = _status_path
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
