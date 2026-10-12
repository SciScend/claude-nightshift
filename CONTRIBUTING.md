# Contributing

Issues and pull requests are welcome.

## Setup

```bash
git clone https://github.com/SciScend/claude-nightshift
cd claude-nightshift
uv sync --extra dev
uv run pytest
```

Without uv: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`.

## Before you open a pull request

- `uv run pytest` passes. New behaviour comes with a test.
- Tests never touch your real state file: `tests/conftest.py` points
  `$NIGHTSHIFT_STATE_PATH` at a temporary file.
- Tests never call Claude or the usage endpoint; mock them instead.

## Releases

1. Bump `__version__` in `src/claude_nightshift/__init__.py`.
2. Move the changes under a new heading in `CHANGELOG.md`.
3. Tag the commit `vX.Y.Z` and push the tag.
