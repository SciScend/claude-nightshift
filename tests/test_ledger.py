import argparse
import json
import os

import pytest

from claude_nightshift import cli, control, runner
from claude_nightshift.budget import Budget, spend_arg
from claude_nightshift.ledger import (
    Ledger,
    ledger_path,
    message_cost,
    read_runs,
)
from claude_nightshift.runner import Outcome, run, spend_watch_thread
from claude_nightshift.session import SessionResult
from claude_nightshift.usage import Usage

RESETS = 1_900_000_000


def reading(pct, resets=RESETS):
    return Usage(weekly_pct=pct, weekly_resets=resets, five_hour_pct=10.0, source="test")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def load():
    with open(ledger_path()) as f:
        return json.load(f)


def message(msg_id, output, model="claude-sonnet-5-5"):
    return {"id": msg_id, "model": model,
            "usage": {"input_tokens": 0, "output_tokens": output}}


def test_spend_arg():
    assert spend_arg("auto") == "auto"
    assert spend_arg("5") == 5.0
    for bad in ("0", "-1", "101", "lots"):
        with pytest.raises(argparse.ArgumentTypeError):
            spend_arg(bad)


def test_message_cost_is_priced_by_model():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
             "cache_read_input_tokens": 1_000_000}
    assert message_cost("claude-opus-5-5", usage) == pytest.approx(5 + 25 + 0.5)
    assert message_cost("claude-haiku-5-5", usage) == pytest.approx(1 + 5 + 0.1)
    # An unknown family is priced like the middle one.
    assert message_cost("something-new", usage) == message_cost("claude-sonnet-5", usage)


def test_a_rise_is_split_by_the_weight_each_run_gained():
    a, b = Ledger("a"), Ledger("b")
    for ledger in (a, b):
        ledger.start(cap=10)
        ledger.session(True)
    a.observe(reading(40))  # first reading: nothing to split yet
    a.message(message("m1", 300_000))
    b.message(message("m2", 100_000))
    a._safe_update(lambda d, r: None)
    b._safe_update(lambda d, r: None)
    a.observe(reading(44))
    assert a.spent == pytest.approx(3)
    assert b.refresh() == pytest.approx(1)


def test_repeated_message_ids_are_counted_once():
    a = Ledger("a")
    a.start()
    a.message(message("m1", 100))
    a.message(message("m1", 300))
    assert a.weight == pytest.approx(message_cost("sonnet", {"output_tokens": 300}))


def test_the_result_cost_replaces_the_estimates():
    a = Ledger("a")
    a.start()
    a.session(True)
    a.message(message("m1", 100))
    a.result({"total_cost_usd": 2.5})
    a.session(False)
    assert a.weight == 2.5


def test_without_weight_the_rise_goes_to_the_runs_in_a_session():
    a, b, c = Ledger("a"), Ledger("b"), Ledger("c")
    for ledger in (a, b, c):
        ledger.start()
    a.session(True)
    b.session(True)
    a.observe(reading(40))
    a.observe(reading(42))
    assert a.spent == pytest.approx(1)
    assert b.refresh() == pytest.approx(1)
    assert c.refresh() == 0


def test_a_rise_while_no_run_is_in_a_session_is_nobodys():
    a = Ledger("a")
    a.start()
    a.observe(reading(40))
    a.observe(reading(50))  # the user's own use, between sessions
    assert a.spent == 0


def test_a_small_drop_is_ignored_and_a_weekly_reset_is_not_charged():
    a = Ledger("a")
    a.start()
    a.session(True)
    a.observe(reading(40))
    a.observe(reading(39))  # another source, a point lower
    a.observe(reading(41))
    assert a.spent == pytest.approx(1)
    a.observe(reading(2, resets=RESETS + 7 * 86400))
    a.observe(reading(3, resets=RESETS + 7 * 86400))
    assert a.spent == pytest.approx(2)


def test_finished_runs_take_no_share_and_are_listed_as_finished():
    a, b = Ledger("a"), Ledger("b")
    a.start()
    b.start(cap=4)
    b.session(True)
    b.finish()
    a.session(True)
    a.observe(reading(10))
    a.observe(reading(12))
    assert a.spent == pytest.approx(2)
    runs = read_runs()
    assert runs["b"] == {"spent": 0, "cap": 4, "active": False, "finished": True}
    assert runs["a"]["active"] and not runs["a"]["finished"]


def test_old_finished_runs_are_pruned():
    clock = Clock()
    a = Ledger("a", clock=clock)
    a.start()
    a.finish()
    clock.now += 8 * 86400
    Ledger("b", clock=clock).start()
    assert set(read_runs()) == {"b"}


def test_a_corrupt_ledger_starts_over():
    path = ledger_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{oops")
    a = Ledger("a")
    a.start(cap=3)
    assert load()["runs"]["a"]["cap"] == 3


# The budget


def test_auto_spend_splits_what_is_left_below_the_threshold():
    ledger = Ledger("a")
    ledger.start()
    budget = Budget(60, spend="auto", spend_share=2, ledger=ledger)
    budget.plan(reading(40))
    assert budget.spend_cap == 10
    assert read_runs()["a"]["cap"] == 10


def test_a_spent_budget_refuses_the_next_session_with_run_budget():
    ledger = Ledger("a")
    ledger.start()
    budget = Budget(90, spend=2, ledger=ledger)
    ledger.session(True)
    ledger.observe(reading(40))
    assert budget.check(reading(41))[0]
    ledger.observe(reading(42))
    allowed, reason = budget.check(reading(42))
    assert not allowed
    assert "spend budget used" in reason
    assert budget.stop_kind == "run_budget"


def test_the_threshold_wins_over_the_spend_budget():
    ledger = Ledger("a")
    ledger.start()
    budget = Budget(40, spend="auto", ledger=ledger)
    budget.plan(reading(45))
    assert not budget.check(reading(45))[0]
    assert budget.stop_kind == "weekly_limit"


def test_run_counts_the_session_and_stops_at_the_budget(monkeypatch):
    monkeypatch.setattr(control, "interruptible_sleep", lambda secs: False)
    monkeypatch.setattr(runner, "fetch_usage", lambda *a, **k: reading(40))
    seen = {}

    def fake_session(cmd, structured=True, ledger=None):
        seen["active"] = read_runs()["a"]["active"]
        ledger.message(message("m1", 1000))
        s = SessionResult()
        s.usage = reading(43)
        ledger.observe(s.usage)
        s.returncode = 1
        s.result = {"subtype": "error_max_turns"}
        return s

    monkeypatch.setattr(runner, "run_session", fake_session)
    ledger = Ledger("a")
    ledger.start()
    budget = Budget(90, spend=3, ledger=ledger)
    assert run(["claude", "-p", "x"], budget) == Outcome.RUN_BUDGET
    assert seen["active"]
    assert ledger.spent == pytest.approx(3)


def test_the_watcher_stops_a_session_that_runs_past_the_budget(monkeypatch):
    ledger = Ledger("a")
    ledger.start()
    ledger.session(True)
    ledger.observe(reading(40))
    budget = Budget(90, spend=1, ledger=ledger)
    sleeps = iter([False, True])
    monkeypatch.setattr(control, "interruptible_sleep", lambda secs: next(sleeps))
    monkeypatch.setattr(control, "get_current_process", lambda: object())
    killed = []
    monkeypatch.setattr(control, "terminate_process", lambda proc: killed.append(proc))
    monkeypatch.setattr(runner, "fetch_usage", lambda *a, **k: reading(42))
    data = load()
    data["last"]["at"] -= 1000  # old enough to ask the endpoint
    with open(ledger_path(), "w") as f:
        json.dump(data, f)
    spend_watch_thread(budget, 60)
    assert killed
    assert control.stop_kind() == "run_budget"


# The CLI


def test_spend_needs_a_run_id(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--spend", "5", "task"])


def test_cli_registers_and_finishes_the_run(monkeypatch):
    monkeypatch.setattr(cli, "install_sigint_handler", lambda: None)
    seen = {}

    def fake_run(cmd, budget, **kwargs):
        seen["cap"] = budget.spend_cap
        seen["runs"] = read_runs()
        return Outcome.COMPLETED

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli.threading, "Thread", lambda **k: type(
        "T", (), {"start": lambda self: None})())
    assert cli.main(["--run-id", "t1", "--spend", "4", "task"]) == 0
    assert seen["cap"] == 4
    assert "t1" in seen["runs"]
    assert read_runs()["t1"]["finished"]


def test_usage_json_lists_the_runs(monkeypatch, capsys):
    Ledger("t1").start(cap=5)
    monkeypatch.setattr(cli, "fetch_usage", lambda attempts=3: reading(40))
    assert cli.main(["--usage-json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["runs"]["t1"]["cap"] == 5
