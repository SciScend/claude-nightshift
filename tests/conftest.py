import pytest

from claude_nightshift import control
from claude_nightshift.budget import STATE_PATH_ENV


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.claude/nightshift-state.json."""
    path = tmp_path / "nightshift-state.json"
    monkeypatch.setenv(STATE_PATH_ENV, str(path))
    control.reset()
    yield path
    control.reset()
