import json
import shutil
import stat
import time
from pathlib import Path

import pytest

from workforce.agents.base import AgentResult
from workforce.usage import claude_limits, codex_limits
from workforce.usage.claude_limits import Window
from workforce.usage.codex_limits import UsageReadError

HERE = Path(__file__).parent
FAKE_CLAUDE = HERE / "fakes" / "fake_claude.py"
FAKE_CODEX = HERE / "fakes" / "fake_codex.py"
SCENARIOS = HERE / "scenarios"

T0 = 1_790_000_000.0
HOUR = 3600


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def load_scenario(name):
    return json.loads((SCENARIOS / name).read_text())


def result_with(**windows):
    return AgentResult(
        ok=True,
        text="OK",
        structured=None,
        session_id="s",
        model="m",
        rate_limits={
            name: {"utilization": util, "resetsAt": resets}
            for name, (util, resets) in windows.items()
        },
    )


def ping_step(five_hour, seven_day, reset_5h, reset_7d):
    return {
        "text": "OK",
        "rate_limits": {"five_hour": five_hour, "seven_day": seven_day},
        "resets_at": {"five_hour": reset_5h, "seven_day": reset_7d},
    }


def by_name(windows):
    return {(w.source, w.name): w for w in windows}


def test_claude_from_rate_limits_parses_both_windows():
    windows = claude_limits.from_rate_limits(
        {
            "five_hour": {"utilization": 0.03, "resetsAt": 1790676000},
            "seven_day": {"utilization": 0.31, "resetsAt": 1790838000},
        },
        T0,
    )
    found = by_name(windows)
    five = found[("claude", "five_hour")]
    assert five == Window("claude", "five_hour", 3.0, 1790676000, T0, "5h")
    week = found[("claude", "seven_day")]
    assert (week.percent, week.resets_at, week.kind) == (31.0, 1790838000, "weekly")


def test_claude_from_rate_limits_accepts_clock_and_skips_bad_entries():
    windows = claude_limits.from_rate_limits(
        {
            "five_hour": {"utilization": 0.29, "resetsAt": 5},
            "seven_day": {"resetsAt": 5},
            "seven_day_opus": {"utilization": 0.5, "resetsAt": 6},
            "junk": "nope",
            "flag": {"utilization": True},
        },
        FakeClock(42.0),
    )
    found = by_name(windows)
    assert set(found) == {("claude", "five_hour"), ("claude", "seven_day_opus")}
    assert found[("claude", "five_hour")].percent == 29.0
    assert found[("claude", "five_hour")].read_at == 42.0
    assert found[("claude", "seven_day_opus")].kind == "other"
    assert claude_limits.from_rate_limits(None, T0) == []


def test_codex_read_from_fake_app_server(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.json"
    shutil.copy(SCENARIOS / "usage_codex_low.json", scenario)
    monkeypatch.setenv("WF_FAKE_SCENARIO", str(scenario))
    found = by_name(codex_limits.read(FAKE_CODEX, timeout_s=10))
    five = found[("codex", "five_hour")]
    assert (five.percent, five.resets_at, five.kind) == (12.0, 1790676000, "5h")
    week = found[("codex", "seven_day")]
    assert (week.percent, week.resets_at, week.kind) == (4.0, 1791265456, "weekly")


def test_codex_read_weekly_only_when_primary_is_weekly_and_secondary_null(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.json"
    shutil.copy(SCENARIOS / "usage_codex_weekly_only.json", scenario)
    monkeypatch.setenv("WF_FAKE_SCENARIO", str(scenario))
    windows = codex_limits.read(FAKE_CODEX, timeout_s=10)
    assert [(w.name, w.kind, w.percent) for w in windows] == [("seven_day", "weekly", 4.0)]


def test_codex_read_odd_durations_are_other(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.json"
    shutil.copy(SCENARIOS / "usage_codex_odd_window.json", scenario)
    monkeypatch.setenv("WF_FAKE_SCENARIO", str(scenario))
    found = by_name(codex_limits.read(FAKE_CODEX, timeout_s=10))
    assert (found[("codex", "45m")].kind, found[("codex", "45m")].percent) == ("other", 30.0)
    secondary = found[("codex", "secondary")]
    assert (secondary.kind, secondary.percent, secondary.resets_at) == ("other", 7.5, None)


def test_codex_read_error_reply_raises(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.json"
    shutil.copy(SCENARIOS / "usage_codex_no_limits.json", scenario)
    monkeypatch.setenv("WF_FAKE_SCENARIO", str(scenario))
    with pytest.raises(UsageReadError, match="unsupported"):
        codex_limits.read(FAKE_CODEX, timeout_s=10)


def test_codex_read_missing_binary_raises(tmp_path):
    with pytest.raises(UsageReadError, match="cannot start"):
        codex_limits.read(tmp_path / "nope", timeout_s=2)


def make_script(path, body):
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_codex_read_timeout_raises_and_kills_process(tmp_path):
    pidfile = tmp_path / "pid"
    script = make_script(
        tmp_path / "hang_codex",
        f"import os, sys, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stdin.readline()\ntime.sleep(60)\n",
    )
    started = time.monotonic()
    with pytest.raises(UsageReadError, match="did not answer"):
        codex_limits.read(script, timeout_s=1)
    assert time.monotonic() - started < 10
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            import os

            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("app-server process was not killed")


def test_codex_read_early_exit_raises(tmp_path):
    script = make_script(tmp_path / "dead_codex", "import sys\nsys.exit(1)\n")
    with pytest.raises(UsageReadError):
        codex_limits.read(script, timeout_s=5)


class StubRunner:
    """Answers every request with a canned result and records what it was asked."""

    def __init__(self, text="Claude 5-hour usage is at 12%.", ok=True, rate_limits=None, raises=None):
        self.text, self.ok, self.rate_limits, self.raises = text, ok, rate_limits, raises
        self.requests = []

    def run(self, req, on_event=None):
        self.requests.append(req)
        if self.raises:
            raise self.raises
        return AgentResult(
            ok=self.ok,
            text=self.text if self.ok else "",
            structured=None,
            session_id="s",
            model=req.model,
            rate_limits=self.rate_limits,
            error_kind=None if self.ok else "crash",
            error=None if self.ok else "boom",
        )


def reading(percent, resets=None):
    resets = T0 + HOUR if resets is None else resets
    return {
        "generated_at": T0,
        "thresholds": {"alert_percent": 50, "pause_percent": 60},
        "sources": {
            "claude": {"freshness": "fresh", "read_at": T0, "error": None},
            "codex": {"freshness": "unknown", "read_at": None, "error": None},
        },
        "windows": [
            {
                "source": "claude",
                "name": "five_hour",
                "kind": "5h",
                "percent": percent,
                "resets_at": resets,
                "read_at": T0,
                "freshness": "fresh",
                "expired": False,
            }
        ],
    }


