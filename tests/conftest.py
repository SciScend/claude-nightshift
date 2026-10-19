import pytest

from claude_nightshift import control, status
from claude_nightshift.budget import STATE_PATH_ENV
from claude_nightshift.ledger import LEDGER_PATH_ENV


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.claude/nightshift-state.json."""
    path = tmp_path / "nightshift-state.json"
    monkeypatch.setenv(STATE_PATH_ENV, str(path))
    monkeypatch.setenv(LEDGER_PATH_ENV, str(tmp_path / "nightshift-ledger.json"))
    control.reset()
    status.set_status_file(None)
    yield path
    control.reset()
    status.set_status_file(None)
