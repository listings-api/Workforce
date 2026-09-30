import json
import shutil
import stat
import time
from pathlib import Path

import pytest

from workforce import config as wf_config
from workforce.agents.base import AgentResult
from workforce.agents.claude import ClaudeRunner
from workforce.events import EventLog
from workforce.paths import Paths
from workforce.usage import claude_limits, codex_limits, summary
from workforce.usage.claude_limits import Window
from workforce.usage.codex_limits import UsageReadError
from workforce.usage.monitor import UsageMonitor

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


class RoutingRunner:
    """Pings go to the fake claude binary; summary requests go to a stub so scripted steps stay in order."""

    def __init__(self, ping_runner, summary_runner):
        self.ping_runner, self.summary_runner = ping_runner, summary_runner

    def run(self, req, on_event=None):
        target = self.ping_runner if req.prompt == "Reply OK" else self.summary_runner
        return target.run(req, on_event)


class Harness:
    """A monitor wired to the fake claude/codex binaries with a fake clock."""

    def __init__(self, tmp_path, monkeypatch, codex="usage_codex_low.json", claude_steps=None):
        repo = tmp_path / "repo"
        repo.mkdir()
        self.paths = Paths(repo, home=tmp_path)
        self.config = wf_config.parse(wf_config.default_toml())
        self.events = EventLog(self.paths)
        self.clock = FakeClock()
        self.scenario_path = tmp_path / "scenario.json"
        monkeypatch.setenv("WF_FAKE_SCENARIO", str(self.scenario_path))
        self.claude_steps = claude_steps or []
        self.set_codex(load_scenario(codex)["codex_limits"] if codex else None)
        self.summary_stub = StubRunner()
        self.runner = RoutingRunner(ClaudeRunner(FAKE_CLAUDE), self.summary_stub)
        self.monitor = UsageMonitor(self.paths, self.config, FAKE_CODEX, self.runner, self.events, self.clock)

    def set_codex(self, limits):
        data = {"claude": self.claude_steps}
        if limits is not None:
            data["codex_limits"] = limits
        self.scenario_path.write_text(json.dumps(data))

    def claude_calls(self):
        calls = Path(str(self.scenario_path) + ".calls.jsonl")
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    def kinds(self, kind):
        events, _ = self.events.read()
        return [e for e in events if e["kind"] == kind]


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


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


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


def test_freshness_transitions_with_fake_clock(harness):
    m = harness.monitor
    before = m.readings()
    assert before["sources"]["claude"]["freshness"] == "unknown"
    assert before["sources"]["codex"]["freshness"] == "unknown"
    assert before["windows"] == []

    assert m.ingest_claude(result_with(five_hour=(0.2, T0 + HOUR), seven_day=(0.3, T0 + 7 * 24 * HOUR)))
    assert m.refresh_codex()
    fresh = m.readings()
    assert {w["freshness"] for w in fresh["windows"]} == {"fresh"}
    assert fresh["sources"]["claude"]["freshness"] == "fresh"

    harness.clock.advance(599)
    assert {w["freshness"] for w in m.readings()["windows"]} == {"fresh"}

    harness.clock.advance(1)
    old = m.readings()
    assert {w["freshness"] for w in old["windows"]} == {"last_known"}
    claude_five = next(w for w in old["windows"] if (w["source"], w["name"]) == ("claude", "five_hour"))
    assert claude_five["read_at"] == T0 and claude_five["percent"] == 20.0
    assert old["sources"]["codex"]["freshness"] == "last_known"

    assert m.refresh_codex()
    mixed = m.readings()
    freshness = {(w["source"], w["name"]): w["freshness"] for w in mixed["windows"]}
    assert freshness[("codex", "five_hour")] == "fresh"
    assert freshness[("claude", "five_hour")] == "last_known"


def test_readings_persist_atomically_and_reload(harness, tmp_path):
    m = harness.monitor
    m.ingest_claude(result_with(five_hour=(0.2, T0 + HOUR)))
    data = json.loads(harness.paths.usage_json.read_text())
    assert data["windows"][0]["percent"] == 20.0
    assert data["windows"][0]["freshness"] == "fresh"
    assert [p.name for p in harness.paths.root.iterdir() if p.name.startswith(".usage.json.")] == []

    harness.clock.advance(HOUR)
    reloaded = UsageMonitor(
        harness.paths, harness.config, FAKE_CODEX, ClaudeRunner(FAKE_CLAUDE), harness.events, harness.clock
    )
    windows = reloaded.readings()["windows"]
    assert windows[0]["percent"] == 20.0 and windows[0]["freshness"] == "last_known"
    assert windows[0]["read_at"] == T0


def test_ingest_without_rate_limits_changes_nothing(harness):
    result = AgentResult(ok=False, text="", structured=None, session_id=None, model=None, error_kind="crash")
    assert harness.monitor.ingest_claude(result) is False
    assert harness.monitor.readings()["windows"] == []
    assert not harness.paths.usage_json.exists()


def test_failed_run_that_carries_rate_limits_is_still_recorded(harness):
    result = result_with(five_hour=(0.9, T0 + HOUR))
    result.ok, result.error_kind = False, "rate_limited"
    assert harness.monitor.ingest_claude(result) is True
    assert harness.monitor.readings()["windows"][0]["percent"] == 90.0


def test_alert_at_50_percent_once_per_window_and_reset(harness):
    m = harness.monitor
    m.ingest_claude(result_with(five_hour=(0.49, T0 + HOUR)))
    assert harness.kinds("alert") == []

    m.ingest_claude(result_with(five_hour=(0.50, T0 + HOUR)))
    alerts = harness.kinds("alert")
    assert len(alerts) == 1
    assert (alerts[0]["source"], alerts[0]["window"], alerts[0]["percent"]) == ("claude", "five_hour", 50.0)

    m.ingest_claude(result_with(five_hour=(0.55, T0 + HOUR)))
    m.ingest_claude(result_with(five_hour=(0.52, T0 + HOUR)))
    assert len(harness.kinds("alert")) == 1

    m.ingest_claude(result_with(five_hour=(0.51, T0 + 6 * HOUR)))
    assert len(harness.kinds("alert")) == 2

    m.ingest_claude(result_with(seven_day=(0.7, T0 + 100 * HOUR)))
    assert {a["window"] for a in harness.kinds("alert")} == {"five_hour", "seven_day"}
    assert len(harness.kinds("alert")) == 3


def test_alert_not_repeated_after_reload(harness):
    harness.monitor.ingest_claude(result_with(five_hour=(0.55, T0 + HOUR)))
    reloaded = UsageMonitor(
        harness.paths, harness.config, FAKE_CODEX, ClaudeRunner(FAKE_CLAUDE), harness.events, harness.clock
    )
    reloaded.ingest_claude(result_with(five_hour=(0.56, T0 + HOUR)))
    assert len(harness.kinds("alert")) == 1


def test_codex_alert_uses_codex_source(harness):
    harness.set_codex({"primary": {"usedPercent": 51, "windowDurationMins": 300, "resetsAt": int(T0) + HOUR}, "secondary": None})
    assert harness.monitor.refresh_codex()
    alerts = harness.kinds("alert")
    assert [(a["source"], a["window"]) for a in alerts] == [("codex", "five_hour")]
    harness.monitor.refresh_codex()
    assert len(harness.kinds("alert")) == 1


def test_gate_proceeds_below_60_and_pauses_at_or_above(harness):
    m = harness.monitor
    assert m.gate() is None
    m.ingest_claude(result_with(five_hour=(0.59, T0 + HOUR)))
    assert m.gate() is None
    m.ingest_claude(result_with(five_hour=(0.61, T0 + HOUR)))
    assert m.gate() == "usage: claude five_hour 61% ≥ 60%"


def test_gate_at_exactly_pause_percent_and_picks_worst_window(harness):
    m = harness.monitor
    m.ingest_claude(result_with(five_hour=(0.60, T0 + HOUR), seven_day=(0.75, T0 + 100 * HOUR)))
    assert m.gate() == "usage: claude seven_day 75% ≥ 60%"


def test_gate_covers_codex_windows(harness):
    harness.set_codex({"primary": {"usedPercent": 64, "windowDurationMins": 10080, "resetsAt": int(T0) + 100 * HOUR}, "secondary": None})
    harness.monitor.refresh_codex()
    assert harness.monitor.gate() == "usage: codex seven_day 64% ≥ 60%"


def test_gate_ignores_reading_whose_window_has_already_reset(harness):
    m = harness.monitor
    m.ingest_claude(result_with(five_hour=(0.9, T0 + HOUR)))
    assert m.gate() is not None
    harness.clock.advance(HOUR + 1)
    assert m.gate() is None
    assert m.readings()["windows"][0]["expired"] is True


def test_five_hour_pause_auto_resumes_at_reset_plus_60_only_when_fresh_readings_are_low(tmp_path, monkeypatch):
    reset = T0 + HOUR
    fresh_after_reset = ping_step(0.10, 0.33, reset + 5 * HOUR, T0 + 100 * HOUR)
    h = Harness(tmp_path, monkeypatch, claude_steps=[fresh_after_reset])
    m = h.monitor

    m.ingest_claude(result_with(five_hour=(0.65, reset), seven_day=(0.33, T0 + 100 * HOUR)))
    reason = m.gate()
    assert reason == "usage: claude five_hour 65% ≥ 60%"

    plan = m.resume_plan()
    assert plan.auto is True
    assert plan.at == reset + 60
    assert [w.name for w in plan.blocking_windows] == ["five_hour"]

    h.clock.advance(HOUR - 10)
    assert m.tick() == "none"
    h.clock.advance(50)
    assert h.clock.time() == reset + 40
    assert m.tick() == "none"
    assert h.claude_calls() == []

    h.clock.advance(20)
    assert h.clock.time() == reset + 60
    assert m.tick() == "resume"
    assert len(h.claude_calls()) == 1
    assert m.gate() is None
    assert m.pause_reason is None
    assert m.tick() == "none"


def test_auto_resume_waits_when_fresh_claude_reading_is_still_high(tmp_path, monkeypatch):
    reset = T0 + HOUR
    still_high = ping_step(0.70, 0.33, reset + 5 * HOUR, T0 + 100 * HOUR)
    low = ping_step(0.05, 0.33, reset + 10 * HOUR, T0 + 100 * HOUR)
    h = Harness(tmp_path, monkeypatch, claude_steps=[still_high, low])
    m = h.monitor
    m.ingest_claude(result_with(five_hour=(0.65, reset)))
    assert m.gate()

    h.clock.advance(HOUR + 60)
    assert m.tick() == "none"
    assert m.resume_plan().at == reset + 5 * HOUR + 60
    assert len(h.claude_calls()) == 1

    h.clock.advance(5 * HOUR)
    assert m.tick() == "resume"
    assert len(h.claude_calls()) == 2


def test_auto_resume_blocked_when_codex_is_high_at_reset_time(tmp_path, monkeypatch):
    reset = T0 + HOUR
    h = Harness(tmp_path, monkeypatch, claude_steps=[ping_step(0.05, 0.33, reset + 5 * HOUR, T0 + 100 * HOUR)])
    m = h.monitor
    m.ingest_claude(result_with(five_hour=(0.65, reset)))
    assert m.gate()
    h.set_codex({"primary": {"usedPercent": 75, "windowDurationMins": 10080, "resetsAt": int(T0) + 100 * HOUR}, "secondary": None})
    h.clock.advance(HOUR + 60)
    assert m.tick() == "none"


def test_auto_resume_refuses_when_claude_cannot_be_read(tmp_path, monkeypatch):
    reset = T0 + HOUR
    h = Harness(tmp_path, monkeypatch, claude_steps=[{"error": "crash"}])
    m = h.monitor
    m.ingest_claude(result_with(five_hour=(0.65, reset)))
    assert m.gate()
    h.clock.advance(HOUR + 60)
    assert m.tick() == "none"
    assert m.readings()["sources"]["claude"]["error"] is not None
    assert h.kinds("error")


def test_auto_resume_refuses_when_ping_omits_the_blocking_window(tmp_path, monkeypatch):
    reset = T0 + HOUR
    seven_only = {"text": "OK", "rate_limits": {"seven_day": 0.2}, "resets_at": {"seven_day": T0 + 100 * HOUR}}
    h = Harness(tmp_path, monkeypatch, claude_steps=[seven_only])
    m = h.monitor
    m.ingest_claude(result_with(five_hour=(0.65, reset)))
    assert m.gate()
    h.clock.advance(HOUR + 60)
    assert m.tick() == "none"


def test_weekly_pause_never_auto_resumes_even_after_five_hour_reset(tmp_path, monkeypatch):
    week_reset = T0 + 100 * HOUR
    h = Harness(
        tmp_path,
        monkeypatch,
        claude_steps=[ping_step(0.05, 0.65, T0 + 12 * HOUR, week_reset)] * 3,
    )
    m = h.monitor
    m.ingest_claude(result_with(five_hour=(0.30, T0 + HOUR), seven_day=(0.65, week_reset)))
    assert m.gate() == "usage: claude seven_day 65% ≥ 60%"

    plan = m.resume_plan()
    assert plan.auto is False and plan.at is None
    assert [w.name for w in plan.blocking_windows] == ["seven_day"]

    h.clock.advance(HOUR + 120)
    assert m.resume_plan().auto is False
    assert m.tick() == "none"
    h.clock.advance(20 * HOUR)
    assert m.tick() == "none"
    assert h.claude_calls() == []


def test_mixed_five_hour_and_weekly_pause_does_not_auto_resume(tmp_path, monkeypatch):
    week_reset = T0 + 100 * HOUR
    h = Harness(tmp_path, monkeypatch, claude_steps=[ping_step(0.05, 0.65, T0 + 12 * HOUR, week_reset)])
    m = h.monitor
    m.ingest_claude(result_with(five_hour=(0.72, T0 + HOUR), seven_day=(0.65, week_reset)))
    assert m.gate()
    plan = m.resume_plan()
    assert plan.auto is False and plan.at is None
    assert {w.name for w in plan.blocking_windows} == {"five_hour", "seven_day"}
    h.clock.advance(HOUR + 120)
    assert m.tick() == "none"
    assert h.claude_calls() == []


def test_mixed_pause_across_sources_needs_all_five_hour(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    m = h.monitor
    m.ingest_claude(result_with(five_hour=(0.7, T0 + HOUR)))
    h.set_codex({"primary": {"usedPercent": 66, "windowDurationMins": 300, "resetsAt": int(T0) + 2 * HOUR}, "secondary": None})
    m.refresh_codex()
    plan = m.resume_plan()
    assert plan.auto is True
    assert plan.at == T0 + 2 * HOUR + 60


def test_manual_can_resume_after_weekly_reset_uses_fresh_readings(tmp_path, monkeypatch):
    week_reset = T0 + 100 * HOUR
    still = ping_step(0.05, 0.65, T0 + 12 * HOUR, week_reset)
    reset_done = ping_step(0.05, 0.02, T0 + 12 * HOUR, week_reset + 7 * 24 * HOUR)
    h = Harness(tmp_path, monkeypatch, claude_steps=[still, reset_done])
    m = h.monitor
    m.ingest_claude(result_with(seven_day=(0.65, week_reset)))
    assert m.gate()
    assert m.can_resume() is False
    assert m.can_resume() is True
    m.clear_pause()
    assert m.gate() is None


def test_codex_timeout_is_unknown_and_never_invents_a_number(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, codex=None)
    hang = make_script(tmp_path / "hang_codex", "import sys, time\nsys.stdin.readline()\ntime.sleep(60)\n")
    m = UsageMonitor(h.paths, h.config, hang, ClaudeRunner(FAKE_CLAUDE), h.events, h.clock, codex_timeout_s=1)

    assert m.refresh_codex() is False
    readings = m.readings()
    assert readings["sources"]["codex"]["freshness"] == "unknown"
    assert readings["sources"]["codex"]["read_at"] is None
    assert "did not answer" in readings["sources"]["codex"]["error"]
    assert [w for w in readings["windows"] if w["source"] == "codex"] == []
    assert m.gate() is None
    assert json.loads(h.paths.usage_json.read_text())["sources"]["codex"]["freshness"] == "unknown"

    assert m.refresh_codex() is False
    assert len(h.kinds("error")) == 1


def test_codex_failure_after_success_keeps_old_reading_as_last_known(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    m = h.monitor
    assert m.refresh_codex()
    h.set_codex(None)
    h.clock.advance(11 * 60)
    assert m.refresh_codex() is False
    readings = m.readings()
    codex = [w for w in readings["windows"] if w["source"] == "codex"]
    assert {w["freshness"] for w in codex} == {"last_known"}
    assert {w["percent"] for w in codex} == {12.0, 4.0}
    assert readings["sources"]["codex"]["freshness"] == "last_known"
    assert readings["sources"]["codex"]["error"]


def test_refresh_claude_if_stale_only_pings_after_poll_interval(tmp_path, monkeypatch):
    steps = [ping_step(0.1, 0.2, T0 + 5 * HOUR, T0 + 100 * HOUR), ping_step(0.15, 0.2, T0 + 5 * HOUR, T0 + 100 * HOUR)]
    h = Harness(tmp_path, monkeypatch, claude_steps=steps)
    m = h.monitor
    assert m.refresh_claude_if_stale() is True
    call = h.claude_calls()[0]
    assert "claude-haiku-4-5-20251001" in call["argv"]
    assert call["prompt"] == "Reply OK"
    assert m.refresh_claude_if_stale() is False
    h.clock.advance(599)
    assert m.refresh_claude_if_stale() is False
    h.clock.advance(1)
    assert m.refresh_claude_if_stale() is True
    assert len(h.claude_calls()) == 2
    assert {w["percent"] for w in m.readings()["windows"] if w["name"] == "five_hour"} == {15.0}


def test_tick_returns_pause_once_when_usage_crosses_the_line(harness):
    m = harness.monitor
    m.ingest_claude(result_with(five_hour=(0.3, T0 + HOUR)))
    assert m.tick() == "none"
    m.ingest_claude(result_with(five_hour=(0.61, T0 + HOUR)))
    assert m.tick() == "pause"
    assert m.pause_reason == "usage: claude five_hour 61% ≥ 60%"
    assert m.tick() == "none"


def test_tick_with_run_status_never_resumes_non_usage_pauses(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    m = h.monitor
    m.ingest_claude(result_with(five_hour=(0.1, T0 + HOUR)))
    assert m.tick(paused=True, pause_reason="model unavailable: gpt-6-astra") == "none"


def test_tick_with_run_status_resumes_a_usage_pause_after_restart(tmp_path, monkeypatch):
    reset = T0 + HOUR
    h = Harness(tmp_path, monkeypatch, claude_steps=[ping_step(0.05, 0.2, reset + 5 * HOUR, T0 + 100 * HOUR)])
    h.monitor.ingest_claude(result_with(five_hour=(0.65, reset)))
    restarted = UsageMonitor(h.paths, h.config, FAKE_CODEX, h.runner, h.events, h.clock)
    h.clock.advance(HOUR + 60)
    reason = "usage: claude five_hour 65% ≥ 60%"
    assert restarted.tick(paused=True, pause_reason=reason) == "resume"


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


@pytest.fixture
def summary_env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = Paths(repo, home=tmp_path)
    paths.root.mkdir()
    return paths, EventLog(paths), FakeClock(), wf_config.parse(wf_config.default_toml()).role("usage_summary")


def test_summary_writes_usage_md_with_time_on_top_and_haiku_request(summary_env):
    paths, events, clock, role = summary_env
    runner, state = StubRunner(), summary.SummaryState()
    assert summary.write_summary(reading(12.0), runner, role, paths, clock, state, events=events) is True
    text = paths.usage_md.read_text()
    assert text.startswith("Updated 2026-")
    assert "Claude 5-hour usage is at 12%." in text
    request = runner.requests[0]
    assert (request.role, request.model, request.effort) == ("usage_summary", "claude-haiku-4-5-20251001", "low")
    assert '"percent_used": 12.0' in request.prompt
    assert "never guess or invent" in request.prompt.lower()
    assert "do not decide anything" in request.prompt.lower()
    assert "unknown" in request.prompt


def test_summary_skipped_when_readings_did_not_change(summary_env):
    paths, events, clock, role = summary_env
    runner, state = StubRunner(), summary.SummaryState()
    assert summary.write_summary(reading(12.0), runner, role, paths, clock, state, events=events)
    clock.advance(3600)
    changed_only_in_timestamps = reading(12.0)
    changed_only_in_timestamps["windows"][0]["read_at"] = clock.time()
    changed_only_in_timestamps["windows"][0]["freshness"] = "last_known"
    assert summary.write_summary(changed_only_in_timestamps, runner, role, paths, clock, state, events=events) is False
    assert len(runner.requests) == 1


def test_summary_rate_limited_to_once_per_ten_minutes(summary_env):
    paths, events, clock, role = summary_env
    runner, state = StubRunner(), summary.SummaryState()
    assert summary.write_summary(reading(12.0), runner, role, paths, clock, state, events=events)
    clock.advance(599)
    assert summary.write_summary(reading(30.0), runner, role, paths, clock, state, events=events) is False
    assert len(runner.requests) == 1
    clock.advance(1)
    runner.text = "Claude 5-hour usage is at 30%."
    assert summary.write_summary(reading(30.0), runner, role, paths, clock, state, events=events) is True
    assert "at 30%" in paths.usage_md.read_text()
    assert len(runner.requests) == 2


@pytest.mark.parametrize(
    "runner",
    [
        StubRunner(ok=False),
        StubRunner(text="   "),
        StubRunner(raises=RuntimeError("no binary")),
    ],
    ids=["failed-run", "empty-text", "runner-raises"],
)
def test_summary_failure_keeps_old_file_and_emits_error(summary_env, runner):
    paths, events, clock, role = summary_env
    paths.usage_md.write_text("Updated earlier\n\nold text\n")
    state = summary.SummaryState()
    assert summary.write_summary(reading(40.0), runner, role, paths, clock, state, events=events) is False
    assert paths.usage_md.read_text() == "Updated earlier\n\nold text\n"
    errors = [e for e in events.read()[0] if e["kind"] == "error"]
    assert len(errors) == 1 and errors[0]["source"] == "usage_summary"
    assert state.last_digest is None


def test_summary_failure_is_retried_only_after_ten_minutes(summary_env):
    paths, events, clock, role = summary_env
    bad, state = StubRunner(ok=False), summary.SummaryState()
    summary.write_summary(reading(40.0), bad, role, paths, clock, state, events=events)
    clock.advance(60)
    assert summary.write_summary(reading(40.0), bad, role, paths, clock, state, events=events) is False
    assert len(bad.requests) == 1
    clock.advance(600)
    good = StubRunner(text="All quiet.")
    assert summary.write_summary(reading(40.0), good, role, paths, clock, state, events=events) is True
    assert "All quiet." in paths.usage_md.read_text()


def test_tick_updates_usage_md_once_and_leaves_usage_json_authoritative(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    stub = StubRunner(
        rate_limits={"five_hour": {"utilization": 0.2, "resetsAt": int(T0) + HOUR}},
        text="Claude is at 20%.",
    )
    m = UsageMonitor(h.paths, h.config, FAKE_CODEX, stub, h.events, h.clock)
    assert m.tick() == "none"
    assert "Claude is at 20%." in h.paths.usage_md.read_text()
    summary_requests = [r for r in stub.requests if r.prompt != "Reply OK"]
    pings = [r for r in stub.requests if r.prompt == "Reply OK"]
    assert len(summary_requests) == 1 and len(pings) == 1
    assert json.loads(h.paths.usage_json.read_text())["summary"]["digest"]

    h.clock.advance(60)
    m.tick()
    assert len([r for r in stub.requests if r.prompt != "Reply OK"]) == 1


def test_tick_summary_failure_does_not_break_tick(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    stub = StubRunner(ok=False)
    m = UsageMonitor(h.paths, h.config, FAKE_CODEX, stub, h.events, h.clock)
    assert m.tick() == "none"
    assert not h.paths.usage_md.exists()
    assert h.kinds("error")


def test_summary_ingesting_a_low_reading_after_reset_does_not_strand_the_pause(tmp_path, monkeypatch):
    reset = T0 + HOUR
    next_reset = reset + 5 * HOUR
    h = Harness(tmp_path, monkeypatch, claude_steps=[ping_step(0.20, 0.33, next_reset, T0 + 100 * HOUR)])
    h.summary_stub.rate_limits = {
        "five_hour": {"utilization": 0.20, "resetsAt": next_reset},
        "seven_day": {"utilization": 0.33, "resetsAt": T0 + 100 * HOUR},
    }
    m = h.monitor
    m.ingest_claude(result_with(five_hour=(0.61, reset), seven_day=(0.33, T0 + 100 * HOUR)))
    assert m.gate() == "usage: claude five_hour 61% ≥ 60%"

    h.clock.advance(HOUR + 60)
    assert h.clock.time() == reset + 60
    assert m.tick(paused=True, pause_reason=m.pause_reason) == "resume"

    assert h.summary_stub.requests, "the summary run must have happened before the resume decision"
    assert h.paths.usage_md.exists()
    five = next(w for w in m.readings()["windows"] if w["name"] == "five_hour")
    assert five["percent"] == 20.0
    assert json.loads(h.paths.usage_json.read_text())["pause_state"] is None
    assert m.pause_reason is None and m.gate() is None


def test_resume_plan_is_remembered_after_a_low_reading_replaces_the_blocking_window(harness):
    m = harness.monitor
    reset = T0 + HOUR
    m.ingest_claude(result_with(five_hour=(0.61, reset)))
    assert m.gate()
    m.ingest_claude(result_with(five_hour=(0.20, reset + 5 * HOUR)))
    plan = m.resume_plan()
    assert plan.auto is True and plan.at == reset + 60
    assert [(w.name, w.percent, w.resets_at) for w in plan.blocking_windows] == [("five_hour", 61.0, reset)]

    stored = json.loads(harness.paths.usage_json.read_text())["pause_state"]
    assert stored["reason"] == "usage: claude five_hour 61% ≥ 60%"
    assert [(w["name"], w["kind"], w["resets_at"]) for w in stored["windows"]] == [("five_hour", "5h", reset)]

    reloaded = UsageMonitor(
        harness.paths, harness.config, FAKE_CODEX, harness.runner, harness.events, harness.clock
    )
    assert reloaded.pause_reason == "usage: claude five_hour 61% ≥ 60%"
    assert reloaded.resume_plan().auto is True and reloaded.resume_plan().at == reset + 60

    m.clear_pause()
    assert json.loads(harness.paths.usage_json.read_text())["pause_state"] is None
    assert m.resume_plan().auto is False


def test_weekly_window_crossing_the_line_during_a_five_hour_pause_ends_auto_resume(harness):
    m = harness.monitor
    m.ingest_claude(result_with(five_hour=(0.65, T0 + HOUR), seven_day=(0.2, T0 + 100 * HOUR)))
    assert m.gate()
    assert m.resume_plan().auto is True
    m.ingest_claude(result_with(seven_day=(0.7, T0 + 100 * HOUR)))
    plan = m.resume_plan()
    assert plan.auto is False and plan.at is None
    assert {w.name for w in plan.blocking_windows} == {"five_hour", "seven_day"}


def test_summary_request_is_read_only_and_carries_the_repo_for_the_hook(summary_env):
    paths, events, clock, role = summary_env
    runner = StubRunner()
    summary.write_summary(reading(12.0), runner, role, paths, clock, summary.SummaryState(), events=events)
    request = runner.requests[0]
    assert request.sandbox == "read-only" and request.repo == paths.repo


def test_usage_ping_request_is_read_only_and_carries_the_repo_for_the_hook(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    stub = StubRunner(rate_limits={"five_hour": {"utilization": 0.1, "resetsAt": T0 + HOUR}})
    harness.monitor.claude_runner = stub
    harness.monitor._ping_claude()
    request = stub.requests[0]
    assert request.prompt == "Reply OK"
    assert request.sandbox == "read-only" and request.repo == harness.paths.repo
