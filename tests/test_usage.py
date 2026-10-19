import io
import json
import subprocess
import urllib.error

from claude_nightshift import usage as usage_mod
from claude_nightshift.usage import Usage, parse_7day_usage

TEXT_OUTPUT = """
Claude
  5-Hour Window:
    Used:      12.0%
  7-Day Window:
    Used:      41.5%
"""


def test_parse_7day_usage():
    assert parse_7day_usage(TEXT_OUTPUT) == 41.5
    assert parse_7day_usage("no numbers") is None


def test_usage_describe():
    assert Usage(weekly_pct=41.4, five_hour_pct=12, source="x").describe() == (
        "5h 12%, 7d 41% (via x)"
    )
    assert Usage(source="x").describe() == "no data (via x)"


def test_cclimits_json_is_preferred(monkeypatch):
    payload = {
        "claude": {
            "status": "ok",
            "seven_day": {"used": "33%", "resets_at": "1970-01-01T00:01:00Z"},
            "five_hour": {"used": 5},
        }
    }
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(usage_mod, "cclimits_command", lambda: ["cclimits"])
    monkeypatch.setattr(usage_mod.subprocess, "run", fake_run)
    result = usage_mod.fetch_usage_cclimits()
    assert result.weekly_pct == 33
    assert result.weekly_resets == 60
    assert result.five_hour_pct == 5
    assert calls == [["cclimits", "--claude", "--json"]]


def test_cclimits_falls_back_to_text_output(monkeypatch):
    def fake_run(cmd, **_kwargs):
        out = "not json" if "--json" in cmd else TEXT_OUTPUT
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(usage_mod, "cclimits_command", lambda: ["cclimits"])
    monkeypatch.setattr(usage_mod.subprocess, "run", fake_run)
    assert usage_mod.fetch_usage_cclimits().weekly_pct == 41.5


def test_fetch_usage_falls_back_when_the_endpoint_fails(monkeypatch):
    # With a body of its own and closed at the end: an HTTPError left to the
    # garbage collector warns.
    error = urllib.error.HTTPError(usage_mod.USAGE_ENDPOINT, 500, "boom", {},
                                   io.BytesIO(b""))

    def broken():
        raise error

    monkeypatch.setattr(usage_mod, "fetch_usage_native", broken)
    monkeypatch.setattr(
        usage_mod, "fetch_usage_cclimits", lambda: Usage(weekly_pct=7, source="cclimits")
    )
    try:
        assert usage_mod.fetch_usage().source == "cclimits"
    finally:
        error.close()


def test_fetch_usage_gives_up_with_none(monkeypatch):
    def missing():
        raise FileNotFoundError

    def failing():
        raise RuntimeError("no cclimits")

    monkeypatch.setattr(usage_mod, "fetch_usage_native", missing)
    monkeypatch.setattr(usage_mod, "fetch_usage_cclimits", failing)
    monkeypatch.setattr(usage_mod, "interruptible_sleep", lambda _s: False)
    assert usage_mod.fetch_usage(attempts=2) is None
