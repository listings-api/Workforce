import io
import json
import re
import time
from pathlib import Path

import pytest

from workforce.team import config, statusline, usage_cache
from workforce.usage.claude_limits import Window

NOW = 1_800_000_000.0
ANSI = re.compile(r"\x1b\[[0-9;]*m")
YELLOW, RED = "\x1b[33m", "\x1b[31m"


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def cfg(home):
    return config.load(home)


def stdin_json(five=23.0, week=44.0):
    return {
        "model": {"display_name": "Opus 5.5"},
        "workspace": {"current_dir": "/w/billing"},
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": int(NOW) + 3600},
            "seven_day": {"used_percentage": week, "resets_at": int(NOW) + 86400},
        },
    }


def cache(codex=None):
    return {"claude": None, "codex": codex}


def codex_window(kind, percent, read_at=NOW):
    return {"name": kind, "kind": kind, "percent": percent, "resets_at": int(NOW) + 5000, "read_at": read_at}


def plain(text):
    return ANSI.sub("", text)


def test_all_fields(cfg):
    line = statusline.render(stdin_json(), cfg, cache([codex_window("weekly", 3.0)]), NOW)
    assert plain(line) == "WF │ Claude 5h 23% · wk 44% │ Codex wk 3% │ claude Opus 5.5 │ codex gpt-6-sol·high"
    with_effort = {**stdin_json(), "effort": {"level": "xhigh"}}
    assert plain(statusline.render(with_effort, cfg, cache([codex_window("weekly", 3.0)]), NOW)).endswith("│ claude Opus 5.5·xhigh │ codex gpt-6-sol·high")


def test_codex_both_windows(cfg):
    line = statusline.render(stdin_json(), cfg, cache([codex_window("5h", 9.0), codex_window("weekly", 3.0)]), NOW)
    assert "Codex 5h 9% · wk 3%" in plain(line)


def test_missing_stdin_fields_show_question_marks(cfg):
    line = plain(statusline.render({}, cfg, cache(None), NOW))
    assert line == "WF │ Claude 5h ? · wk ? │ Codex ? │ codex gpt-6-sol·high"
    line = plain(statusline.render({"rate_limits": {"five_hour": {"used_percentage": 12}}}, cfg, cache(None), NOW))
    assert "Claude 5h 12% · wk ?" in line
    line = plain(statusline.render({"rate_limits": {"five_hour": {"used_percentage": "abc"}, "seven_day": None}}, cfg, cache(None), NOW))
    assert "Claude 5h ? · wk ?" in line
    assert plain(statusline.render(None, cfg, None, NOW)).startswith("WF │ Claude 5h ? · wk ?")


@pytest.mark.parametrize(
    "percent, colour",
    [(0, None), (49.4, None), (50, YELLOW), (59.9, YELLOW), (60, RED), (100, RED)],
)
def test_threshold_colours(cfg, percent, colour):
    line = statusline.render(stdin_json(five=percent, week=1), cfg, cache(None), NOW)
    shown = f"5h {round(percent)}%"
    if colour is None:
        assert shown in line and YELLOW not in line and RED not in line
    else:
        assert f"{colour}{shown}\x1b[0m" in line
        assert (RED if colour == YELLOW else YELLOW) not in line


def test_codex_colour_and_thresholds_follow_config(home):
    cfg = config.TeamConfig("c", "x", "m", "high", 20, 30)
    line = statusline.render(stdin_json(five=25, week=35), cfg, cache([codex_window("weekly", 31)]), NOW)
    assert f"{YELLOW}5h 25%" in line and f"{RED}wk 35%" in line and f"{RED}wk 31%" in line


def test_stale_codex_cache_is_marked_with_tilde(cfg):
    fresh = statusline.render(stdin_json(), cfg, cache([codex_window("weekly", 3.0, read_at=NOW - 100)]), NOW)
    stale = statusline.render(stdin_json(), cfg, cache([codex_window("weekly", 3.0, read_at=NOW - 500)]), NOW)
    assert "Codex wk 3% " in plain(fresh) or "Codex wk 3%│" in plain(fresh)
    assert "Codex wk 3%~" in plain(stale)


def run_main(home, payload, spawn=None, now=NOW):
    out = io.StringIO()
    spawned = []
    code = statusline.main(
        [], stdin=io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)), stdout=out, home=home,
        now=now, spawn=spawn or (lambda: spawned.append(1)),
    )
    assert code == 0
    return out.getvalue().strip(), spawned


def test_main_writes_claude_cache_and_prints(home):
    line, _ = run_main(home, stdin_json(five=23, week=44))
    assert plain(line).startswith("WF │ Claude 5h 23% · wk 44%")
    cached = usage_cache.read(home)["claude"]
    assert cached["five_hour"]["used_percentage"] == 23
    assert cached["five_hour"]["resets_at"] == int(NOW) + 3600
    assert cached["seven_day"]["used_percentage"] == 44
    assert not list((home / ".workforce").glob(".usage.json.*"))  # atomic write left no temp files


def test_main_feeds_the_hook_state(home):
    run_main(home, stdin_json(five=61, week=1), now=time.time())
    payload = stdin_json(five=61)
    payload["rate_limits"]["five_hour"]["resets_at"] = int(time.time()) + 3600
    run_main(home, payload, now=time.time())
    assert usage_cache.state(config.load(home), home)["level"] == "stop"


def test_main_does_not_overwrite_cache_when_stdin_has_no_usage(home):
    run_main(home, stdin_json(five=23, week=44))
    line, _ = run_main(home, {"model": {"display_name": "x"}})
    assert usage_cache.read(home)["claude"]["five_hour"]["used_percentage"] == 23
    assert "Claude 5h ? · wk ?" in plain(line)


def test_main_survives_garbage_stdin_and_never_raises(home):
    line, _ = run_main(home, "not json")
    assert plain(line).startswith("WF │ Claude 5h ?")
    line, _ = run_main(home, "[1, 2]")
    assert plain(line).startswith("WF │ Claude 5h ?")
    line, _ = run_main(home, "")
    assert plain(line).startswith("WF │ Claude 5h ?")


def test_main_error_prints_fallback_line(home, monkeypatch):
    monkeypatch.setattr(usage_cache, "read", lambda h=None: 1 / 0)
    line, _ = run_main(home, stdin_json())
    assert plain(line) == "WF │ status unavailable"
    assert "ZeroDivisionError" in (home / ".workforce" / "team.log").read_text()


def test_codex_refresh_spawned_once_per_window_when_stale(home):
    _, spawned = run_main(home, stdin_json())
    assert spawned == [1]
    _, spawned = run_main(home, stdin_json(), now=time.time() + 5)
    assert spawned == []  # attempt marker is fresh
    usage_cache.write_codex([Window("codex", "weekly", 3.0, int(NOW) + 5000, NOW, "weekly")], home)
    marker = home / ".workforce" / statusline.ATTEMPT_FILE
    marker.unlink()
    _, spawned = run_main(home, stdin_json(), now=time.time())
    assert spawned == []  # cache is fresh


def test_fresh_codex_cache_is_shown_from_disk(home):
    usage_cache.write_codex([Window("codex", "weekly", 3.0, int(time.time()) + 5000, time.time(), "weekly")], home)
    line, spawned = run_main(home, stdin_json(), now=time.time())
    assert "Codex wk 3%" in plain(line) and "~" not in plain(line)
    assert spawned == []


def test_refresh_flag_reads_codex_and_writes_cache(home, monkeypatch):
    from workforce.usage import codex_limits

    seen = {}

    def fake_read(binary, timeout_s=20):
        seen["binary"] = binary
        return [Window("codex", "weekly", 7.0, int(time.time()) + 100, time.time(), "weekly")]

    monkeypatch.setattr(codex_limits, "read", fake_read)
    assert statusline.main([statusline.REFRESH_FLAG], home=home) == 0
    assert seen["binary"] == Path("/opt/homebrew/bin/codex")
    assert usage_cache.read(home)["codex"][0]["percent"] == 7.0


def test_refresh_failure_is_logged_not_raised(home, monkeypatch):
    from workforce.usage import codex_limits

    monkeypatch.setattr(codex_limits, "read", lambda *a, **k: 1 / 0)
    assert statusline.main([statusline.REFRESH_FLAG], home=home) == 1
    assert "ZeroDivisionError" in (home / ".workforce" / "team.log").read_text()


def test_the_status_line_records_the_model_claude_code_reports(tmp_path):
    from workforce.team import claude_info

    home = tmp_path / "wfhome"
    payload = {**stdin_json(), "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, "effort": {"level": "high"}}
    statusline.main([], stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO(), home=home, now=NOW, spawn=lambda: None)
    assert claude_info.last_seen(home) == {"id": "claude-opus-5-5", "display_name": "Opus 5.5", "effort": "high"}
