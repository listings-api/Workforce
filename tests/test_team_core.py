import json
import subprocess
import time
from pathlib import Path

import pytest

from workforce.errors import ConfigError
from workforce.team import approvals, config, usage_cache
from workforce.usage.claude_limits import Window


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))  # approvals are signed with the key under $HOME/.workforce
    return path


@pytest.fixture(autouse=True)
def _no_real_home(tmp_path, monkeypatch):
    """Tests that use approvals without a `home` fixture still never touch the real home."""
    guard = tmp_path / "home-guard"
    guard.mkdir()
    monkeypatch.setenv("HOME", str(guard))


def test_config_creates_defaults_on_first_use(home):
    assert not (home / ".workforce" / "team.toml").exists()
    cfg = config.load(home)
    assert cfg == config.TeamConfig(
        claude="~/.local/bin/claude",
        codex="/opt/homebrew/bin/codex",
        codex_model="gpt-6-sol",
        codex_effort="high",
        alert_percent=50,
        stop_percent=60,
    )
    assert (home / ".workforce" / config.TEAM_TOML).exists()
    assert config.load(home) == cfg


def test_config_missing_keys_take_defaults_and_values_are_read(home):
    path = home / ".workforce" / "team.toml"
    path.parent.mkdir()
    path.write_text('codex = "/x/codex"\nstop_percent = 70\n')
    cfg = config.load(home)
    assert cfg.codex == "/x/codex" and cfg.stop_percent == 70 and cfg.codex_model == "gpt-6-sol"


@pytest.mark.parametrize(
    "text",
    ['codex_effort = "turbo"\n', "alert_percent = 60\nstop_percent = 50\n", 'alert_percent = "50"\n', "stop_percent = 101\n", "not toml [\n"],
)
def test_config_rejects_bad_files(home, text):
    path = home / ".workforce" / "team.toml"
    path.parent.mkdir()
    path.write_text(text)
    with pytest.raises(ConfigError):
        config.load(home)


def test_set_codex_persists_and_keeps_other_keys(home):
    path = home / ".workforce" / "team.toml"
    path.parent.mkdir()
    path.write_text('codex = "/x/codex"\n')
    cfg = config.set_codex("gpt-6-astra", "xhigh", home)
    assert (cfg.codex_model, cfg.codex_effort, cfg.codex) == ("gpt-6-astra", "xhigh", "/x/codex")
    assert config.load(home) == cfg
    assert config.set_codex(None, "ultra", home).codex_model == "gpt-6-astra"
    assert config.set_codex("gpt-6-luna", None, home).codex_effort == "ultra"
    assert config.set_codex(None, None, home).codex_model == "gpt-6-luna"
    assert path.read_text().count("codex_model") == 1


def test_set_codex_validates(home):
    with pytest.raises(ConfigError):
        config.set_codex(None, "warp", home)
    with pytest.raises(ConfigError):
        config.set_codex('bad "model"', None, home)
    with pytest.raises(ConfigError):
        config.set_codex("", None, home)
    assert config.load(home).codex_effort == "high"


def test_current_tree_changes_with_working_state(git_repo):
    first = approvals.current_tree(git_repo)
    assert approvals.current_tree(git_repo) == first
    (git_repo / "new.txt").write_text("untracked\n")
    second = approvals.current_tree(git_repo)
    assert second != first
    (git_repo / "README.md").write_text("changed\n")
    assert approvals.current_tree(git_repo) != second


def test_approvals_need_both_and_a_tree_change_voids_them(git_repo):
    (git_repo / "a.txt").write_text("a\n")
    start = approvals.status(git_repo)
    assert start["commit_allowed"] is False and start["claude"] is None and start["codex"] is None
    assert len(start["missing"]) == 2

    approvals.record(git_repo, "claude", "APPROVE", "fine")
    half = approvals.status(git_repo)
    assert half["claude"]["verdict"] == "APPROVE" and half["codex"] is None
    assert half["commit_allowed"] is False and half["missing"] == ["codex: no review of the current changes"]

    approvals.record(git_repo, "codex", "APPROVE", "ok", [{"severity": "nit", "file": "a.txt", "line": None, "message": "m"}])
    both = approvals.status(git_repo)
    assert both["commit_allowed"] is True and both["missing"] == []
    assert both["codex"]["findings"][0]["message"] == "m"

    (git_repo / "a.txt").write_text("a2\n")
    voided = approvals.status(git_repo)
    assert voided["tree"] != both["tree"]
    assert voided["commit_allowed"] is False and voided["claude"] is None

    (git_repo / "a.txt").write_text("a\n")
    assert approvals.status(git_repo)["commit_allowed"] is True


def test_non_approve_verdicts_block(git_repo):
    (git_repo / "a.txt").write_text("a\n")
    approvals.record(git_repo, "claude", "APPROVE", "fine")
    approvals.record(git_repo, "codex", "REQUEST_CHANGES", "no")
    status = approvals.status(git_repo)
    assert status["commit_allowed"] is False and status["missing"] == ["codex: REQUEST_CHANGES (needs APPROVE)"]
    approvals.record(git_repo, "codex", "APPROVE", "now yes")
    assert approvals.status(git_repo)["commit_allowed"] is True


def test_record_validates_inputs(git_repo):
    with pytest.raises(ValueError):
        approvals.record(git_repo, "laya", "APPROVE", "x")
    with pytest.raises(ValueError):
        approvals.record(git_repo, "claude", "LGTM", "x")
    with pytest.raises(ValueError):
        approvals.record(git_repo, "claude", "APPROVE", "x", findings="nope")


def test_approvals_file_lives_in_common_dir_shared_by_worktrees(git_repo, tmp_path):
    (git_repo / "a.txt").write_text("a\n")
    assert approvals.approvals_path(git_repo) == (git_repo / ".git").resolve() / "wf-approvals.json"
    worktree = tmp_path / "wt"
    git(git_repo, "worktree", "add", "-b", "feature", str(worktree))
    assert approvals.approvals_path(worktree) == approvals.approvals_path(git_repo)

    (worktree / "a.txt").write_text("a\n")
    approvals.record(worktree, "claude", "APPROVE", "wt")
    approvals.record(worktree, "codex", "APPROVE", "wt")
    assert approvals.status(worktree)["commit_allowed"] is True
    assert approvals.status(git_repo)["commit_allowed"] is True
    assert (git_repo / ".git" / "wf-approvals.json").exists()
    assert not (worktree / "wf-approvals.json").exists()
    assert "wf-approvals" not in subprocess.run(
        ["git", "-C", str(git_repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout


def test_approvals_from_a_subdirectory_use_the_same_file(git_repo):
    sub = git_repo / "pkg"
    sub.mkdir()
    (sub / "m.py").write_text("x = 1\n")
    assert approvals.approvals_path(sub) == approvals.approvals_path(git_repo)


def test_approvals_are_pruned_to_the_last_fifty_trees(git_repo):
    for i in range(53):
        (git_repo / "f.txt").write_text(f"{i}\n")
        approvals.record(git_repo, "claude", "APPROVE", str(i))
    data = json.loads(approvals.approvals_path(git_repo).read_text())
    assert len(data) == approvals.KEEP_TREES
    assert approvals.status(git_repo)["claude"]["summary"] == "52"
    (git_repo / "f.txt").write_text("0\n")
    assert approvals.status(git_repo)["claude"] is None


def test_record_can_target_an_explicit_tree(git_repo):
    (git_repo / "a.txt").write_text("a\n")
    old = approvals.current_tree(git_repo)
    (git_repo / "a.txt").write_text("b\n")
    approvals.record(git_repo, "codex", "APPROVE", "reviewed the old one", tree=old)
    assert approvals.status(git_repo)["codex"] is None
    (git_repo / "a.txt").write_text("a\n")
    assert approvals.status(git_repo)["codex"]["summary"] == "reviewed the old one"


CFG = config.TeamConfig("c", "x", "gpt-6-sol", "high", 50, 60)
NOW = 1_000_000.0


def claude_windows(five, week, resets=NOW + 3600):
    return {
        "five_hour": {"used_percentage": five, "resets_at": resets},
        "seven_day": {"used_percentage": week, "resets_at": resets + 86400},
    }


def test_usage_read_is_none_before_anything_is_written(home):
    assert usage_cache.read(home) == {"claude": None, "codex": None}
    assert usage_cache.state(CFG, home, NOW)["level"] == "ok"
    assert usage_cache.state(CFG, home, NOW)["window"] is None


def test_usage_write_and_read_roundtrip(home):
    usage_cache.write_claude(claude_windows(23.5, None), home, now=NOW)
    windows = [
        Window("codex", "seven_day", 3.0, int(NOW) + 5, NOW - 1, "weekly"),
        Window("codex", "300m", 1.0, None, NOW - 1, "other"),
    ]
    usage_cache.write_codex(windows, home, now=NOW)
    cache = usage_cache.read(home)
    assert cache["claude"]["five_hour"] == {"used_percentage": 23.5, "resets_at": int(NOW) + 3600, "read_at": NOW}
    assert cache["claude"]["seven_day"]["used_percentage"] is None
    assert cache["codex"][0] == {"name": "seven_day", "kind": "weekly", "percent": 3.0, "resets_at": int(NOW) + 5, "read_at": NOW - 1}
    assert usage_cache.codex_read_at(home) == NOW
    assert not list((home / ".workforce").glob(".*.*"))


def test_empty_codex_write_is_a_reading_not_a_missing_cache(home):
    usage_cache.write_codex([], home, now=NOW)
    assert usage_cache.read(home)["codex"] == []
    assert usage_cache.codex_read_at(home) == NOW


@pytest.mark.parametrize(
    "percent,level", [(0, "ok"), (49, "ok"), (49.9, "ok"), (50, "alert"), (59, "alert"), (60, "stop"), (100, "stop")]
)
def test_usage_state_thresholds(home, percent, level):
    usage_cache.write_claude(claude_windows(percent, 1), home, now=NOW)
    state = usage_cache.state(CFG, home, NOW)
    assert state["level"] == level
    if level != "ok":
        assert state["window"] == "claude five_hour" and state["percent"] == percent
        assert state["resets_at"] == int(NOW) + 3600
        assert f"{percent:.0f}%" in state["text"]


def test_usage_state_picks_the_worst_window_across_claude_and_codex(home):
    usage_cache.write_claude(claude_windows(51, 40), home, now=NOW)
    usage_cache.write_codex([Window("codex", "seven_day", 61.0, int(NOW) + 100, NOW, "weekly")], home, now=NOW)
    state = usage_cache.state(CFG, home, NOW)
    assert state["level"] == "stop" and state["window"] == "codex seven_day"
    assert state["window_key"] == "codex:seven_day"


def test_override_silences_stop_for_that_reset_only(home):
    usage_cache.write_claude(claude_windows(72, 10), home, now=NOW)
    assert usage_cache.state(CFG, home, NOW)["level"] == "stop"
    reset = int(NOW) + 3600
    assert not usage_cache.override_active("claude:five_hour", reset, home)
    usage_cache.override_set("claude:five_hour", reset, home)
    assert usage_cache.override_active("claude:five_hour", reset, home)
    assert usage_cache.override_active("claude five_hour", reset, home)
    assert not usage_cache.override_active("claude:five_hour", reset + 18000, home)
    assert not usage_cache.override_active("claude:seven_day", reset, home)
    state = usage_cache.state(CFG, home, NOW)
    assert state["level"] == "alert" and state["overridden"] is True

    usage_cache.write_claude(claude_windows(72, 10, resets=NOW + 20000), home, now=NOW)
    assert usage_cache.state(CFG, home, NOW)["level"] == "stop"


def test_override_for_one_window_does_not_hide_another(home):
    usage_cache.write_claude(claude_windows(65, 66), home, now=NOW)
    usage_cache.override_set("claude:seven_day", int(NOW) + 3600 + 86400, home)
    state = usage_cache.state(CFG, home, NOW)
    assert state["level"] == "stop" and state["window"] == "claude five_hour"


def test_readings_past_their_reset_are_ignored(home):
    usage_cache.write_claude(claude_windows(90, 10, resets=NOW - 5), home, now=NOW - 10000)
    assert usage_cache.state(CFG, home, NOW)["level"] == "ok"


def test_unreadable_cache_files_read_as_missing(home):
    (home / ".workforce").mkdir()
    (home / ".workforce" / "usage.json").write_text("{nope")
    (home / ".workforce" / "codex_usage.json").write_text("[]")
    assert usage_cache.read(home) == {"claude": None, "codex": None}
    assert usage_cache.state(CFG, home, NOW)["level"] == "ok"


def test_state_accepts_a_clock_object_and_real_time(home):
    class Clock:
        def time(self):
            return NOW

    usage_cache.write_claude(claude_windows(55, 1), home, now=NOW)
    assert usage_cache.state(CFG, home, Clock())["level"] == "alert"
    usage_cache.write_claude(claude_windows(55, 1, resets=int(time.time()) + 100), home)
    assert usage_cache.state(CFG, home)["level"] == "alert"
