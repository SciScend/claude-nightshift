"""claude-nightshift — run Claude Code tasks autonomously across usage limits.

Public surface is deliberately small: :func:`claude_nightshift.cli.main` is the
entry point, and the modules below are importable for testing and for embedding
the budget/usage logic in other tooling.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
