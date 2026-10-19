import io
import json

import pytest

from claude_nightshift import __version__, cli, status
from claude_nightshift.cli import (
    build_base_flags,
    build_claude_command,
    build_parser,
    main,
    split_raw_args,
)
from claude_nightshift.runner import Outcome


def parse(argv):
    own, raw = split_raw_args(argv)
    args = build_parser().parse_args(own)
    args.claude_raw = raw
    return args


def test_split_raw_args():
    assert split_raw_args(["-t", "60", "--", "-p", "x"]) == (["-t", "60"], ["-p", "x"])
    assert split_raw_args(["task"]) == (["task"], [])


def test_defaults():
    args = parse(["do it"])
    assert args.task == "do it"
    assert args.threshold == 70.0
    assert args.max_windows is None
    assert args.horizon == 12 * 3600


def test_week_of_flag():
    assert parse(["x"]).week_of is None
    assert parse(["--week-of", "1700000000", "x"]).week_of == 1700000000.0


def test_window_and_horizon_flags():
    args = parse(["-w", "auto", "--horizon", "20h", "x"])
    assert args.max_windows == "auto"
    assert args.horizon == 20 * 3600


def test_simple_command():
    args = parse(["-m", "opus", "--max-turns", "5", "do it"])
    flags = build_base_flags(args, structured=True)
    assert build_claude_command(args, flags, structured=True) == [
        "claude",
        "--model",
        "opus",
        "--max-turns",
        "5",
        "--output-format",
        "stream-json",
        "--verbose",
        "-p",
        "do it",
    ]


def test_resume_command():
    args = parse(["--resume"])
    assert build_claude_command(args, [], structured=False) == [
        "claude",
        "-c",
        "-p",
        "continue",
    ]


def test_raw_command_gets_stream_json():
    args = parse(["--", "-p", "x"])
    assert build_claude_command(args, [], structured=True) == [
        "-p",
        "x",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def test_no_task_means_no_command():
    assert build_claude_command(parse([]), [], structured=False) is None


def test_version(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])
    assert __version__ in capsys.readouterr().out


# -f / --task-file


def test_task_file_is_read(tmp_path, capsys):
    task = tmp_path / "task.md"
    task.write_text("Refactor the auth module.\n\nKeep the API stable.\n", encoding="utf-8")
    assert main(["-f", str(task), "--raw-output", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "-p Refactor the auth module.\n\nKeep the API stable.\n" in out


def test_task_file_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
    assert main(["--task-file", "-", "--raw-output", "--dry-run"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("-p from stdin")


def test_task_file_and_task_argument_conflict(tmp_path, capsys):
    task = tmp_path / "task.md"
    task.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["-f", str(task), "also a task", "--dry-run"])
    assert exc.value.code == 2
    assert "not both" in capsys.readouterr().err


@pytest.mark.parametrize("content", [None, "  \n\n"])
def test_missing_or_empty_task_file_is_a_usage_error(tmp_path, capsys, content):
    task = tmp_path / "task.md"
    if content is not None:
        task.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["-f", str(task), "--dry-run"])
    assert exc.value.code == 2
    assert "task file" in capsys.readouterr().err


# exit codes


@pytest.mark.parametrize(
    "outcome, code",
    [
        (Outcome.COMPLETED, 0),
        (Outcome.FAILED, 1),
        (Outcome.WEEKLY_LIMIT, 3),
        (Outcome.RUN_BUDGET, 4),
        (Outcome.USAGE_UNKNOWN, 5),
        (Outcome.WEEK_ENDED, 6),
        (Outcome.INTERRUPTED, 130),
    ],
)
def test_main_returns_the_outcome_as_exit_code(monkeypatch, outcome, code):
    monkeypatch.setattr(cli, "install_sigint_handler", lambda: None)
    monkeypatch.setattr(cli, "run", lambda *a, **k: outcome)
    assert main(["some task"]) == code


# --usage-json and --status-file


def test_usage_json_prints_both_windows(monkeypatch, capsys):
    from claude_nightshift.usage import Usage

    snapshot = Usage(weekly_pct=42.0, weekly_resets=1800000000, five_hour_pct=10.0,
                     five_hour_resets=1700000000, source="test", observed_at=1.0)
    monkeypatch.setattr(cli, "fetch_usage", lambda attempts=3: snapshot)
    assert main(["--usage-json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {
        "weekly_pct": 42.0,
        "weekly_resets": 1800000000,
        "five_hour_pct": 10.0,
        "five_hour_resets": 1700000000,
        "source": "test",
        "observed_at": 1.0,
        "runs": {},
    }


def test_usage_json_exits_5_when_usage_is_unreadable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_usage", lambda attempts=3: None)
    assert main(["--usage-json"]) == 5
    assert "error" in json.loads(capsys.readouterr().out)


def test_status_file_flag_sets_the_status_file(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "install_sigint_handler", lambda: None)

    def fake_run(*a, **k):
        seen["path"] = status.status_file()
        return Outcome.COMPLETED

    monkeypatch.setattr(cli, "run", fake_run)
    target = str(tmp_path / "s.md")
    assert main(["--status-file", target, "some task"]) == 0
    assert seen["path"] == target
