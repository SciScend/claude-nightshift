import pytest

from claude_nightshift import __version__
from claude_nightshift.cli import (
    build_base_flags,
    build_claude_command,
    build_parser,
    split_raw_args,
)


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
