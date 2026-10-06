import io
import stat
import subprocess
from pathlib import Path

import pytest

from workforce.errors import ConfigError
from workforce.team import browser, config, detect, doctor, hooks, launch, relay


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    (path / ".workforce").mkdir(parents=True)
    monkeypatch.setattr(detect, "WELL_KNOWN_DIRS", ("~/.local/bin",))
    monkeypatch.setattr(detect, "_npm_global_bin", lambda environ: None)
    monkeypatch.setattr(browser, "SYSTEM_APP_DIRS", ())
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return path


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho ego\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def skill(folder: Path) -> Path:
    (folder / "ego-browser").mkdir(parents=True, exist_ok=True)
    (folder / "ego-browser" / "SKILL.md").write_text("---\nname: ego-browser\n---\n")
    return folder / "ego-browser"


def install_ego(home: Path) -> Path:
    cli = executable(home / ".local" / "bin" / "ego-browser")
    skill(home / ".claude" / "skills")
    skill(home / ".agents" / "skills")
    return cli


def env_for(home: Path) -> dict:
    return {"PATH": str(home.parent / "empty-bin")}


def set_browser(home: Path, mode: str) -> None:
    path = home / ".workforce" / "team.toml"
    text = path.read_text() if path.exists() else 'claude = "/x/claude"\ncodex = "/x/codex"\n'
    kept = [line for line in text.splitlines() if not line.startswith("browser =")]
    path.write_text("\n".join([*kept, f'browser = "{mode}"']) + "\n")


# ------------------------------------------------------------------ the setting


def test_the_browser_is_off_unless_the_user_turns_it_on(home):
    assert config.load(home).browser == "off"
    set_browser(home, "ego")
    assert config.load(home).browser == "ego"


def test_an_unknown_browser_value_is_a_clear_error(home):
    set_browser(home, "chrome")
    with pytest.raises(ConfigError, match="browser must be one of off, ego"):
        config.load(home)


# ------------------------------------------------------------------ detection


def test_off_checks_nothing(home):
    state = browser.status("off", env_for(home), home)
    assert not state.enabled and not state.problems and not state.claude_ready and not state.codex_ready


def test_without_ego_lite_every_missing_piece_is_named(home):
    state = browser.status("ego", env_for(home), home)
    assert not state.claude_ready and not state.codex_ready
    assert len(state.problems) == 3
    assert "Ego Lite is not installed" in state.problems[0]
    assert "Claude Code" in state.problems[1] and "Codex" in state.problems[2]


def test_with_ego_lite_installed_both_agents_are_ready(home):
    cli = install_ego(home)
    state = browser.status("ego", env_for(home), home)
    assert state.problems == [] and state.cli == cli
    assert state.claude_ready and state.codex_ready


def test_the_cli_inside_the_app_is_found_when_it_is_not_on_path(home):
    helper = executable(home / "Applications" / "ego lite.app" / "Contents" / "Frameworks" / "Ego.framework" / "Versions" / "Current" / "Helpers" / "ego-browser")
    assert browser.find_cli(env_for(home), home) == helper
    assert browser.find_app(home) == home / "Applications" / "ego lite.app"


def test_custom_claude_and_codex_homes_are_respected(home, tmp_path):
    executable(home / ".local" / "bin" / "ego-browser")
    skill(tmp_path / "claude-config" / "skills")
    skill(tmp_path / "codex-home" / "skills")
    environ = {**env_for(home), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"), "CODEX_HOME": str(tmp_path / "codex-home")}
    state = browser.status("ego", environ, home)
    assert state.claude_ready and state.codex_ready


def test_a_missing_skill_is_reported_even_when_the_cli_exists(home):
    executable(home / ".local" / "bin" / "ego-browser")
    skill(home / ".claude" / "skills")
    state = browser.status("ego", env_for(home), home)
    assert state.claude_ready and not state.codex_ready
    assert state.problems == ["the `ego-browser` skill for Codex is missing (~/.agents/skills/ego-browser)"]


# ------------------------------------------------------------------ spaces and rules


def test_every_agent_gets_its_own_space(tmp_path):
    names = {browser.space_name(tmp_path / "my app!", agent) for agent in ("claude", "claude", "codex plan", "codex ask")}
    assert len(names) == 4
    assert all(name.startswith("wf my-app ") for name in names)


def test_claude_rules_name_its_space_and_keep_the_limits(tmp_path):
    text = browser.claude_text(tmp_path / "shop", Path("/x/ego-browser"), tag="abc123")
    assert 'taskSpace("wf shop claude abc123")' in text and "task.finish({ keep: [] })" in text
    assert "Never list, read, use or close the user's tabs" in text
    for limit in ("sending messages", "publishing", "purchases", "changing account settings"):
        assert limit in text
    assert "a commit still needs both approvals" in text
    assert "ego-browser upgrade" in text and "import profiles" in text


def test_codex_is_told_not_to_get_around_its_sandbox(tmp_path):
    text = browser.codex_text(tmp_path / "shop", Path("/x/ego-browser"), "team_plan")
    assert "wf shop codex plan " in text and "don't try to get around the sandbox" in text


# ------------------------------------------------------------------ Claude: wf launch


@pytest.fixture
def launcher(home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repair_binaries", lambda home: [], raising=False)
    claude = executable(tmp_path / "bin" / "claude")
    (home / ".workforce" / "team.toml").write_text(f'claude = "{claude}"\ncodex = "/x/codex"\n')
    project = tmp_path / "shop"
    project.mkdir()
    monkeypatch.chdir(project)
    return claude


def launch_wf(home, capsys):
    calls = []
    code = launch.main([], home=home, environ=env_for(home), execvp=lambda file, args: calls.append(args), stdout=io.StringIO())
    assert code == 0 and calls
    argv = calls[0]
    return argv[argv.index("--append-system-prompt") + 1], capsys.readouterr().err


def test_wf_runs_as_before_with_the_browser_off(home, launcher, capsys):
    install_ego(home)
    rules, err = launch_wf(home, capsys)
    assert "Browser (Ego Lite)" not in rules and "ego" not in err.lower()


def test_wf_adds_the_browser_rules_when_ego_lite_is_ready(home, launcher, capsys):
    cli = install_ego(home)
    set_browser(home, "ego")
    rules, err = launch_wf(home, capsys)
    assert "# Browser (Ego Lite)" in rules and str(cli) in rules and 'taskSpace("wf shop claude ' in rules
    assert "ego" not in err.lower()


def test_wf_starts_without_the_browser_and_says_why_when_ego_lite_is_missing(home, launcher, capsys):
    set_browser(home, "ego")
    rules, err = launch_wf(home, capsys)
    assert "Browser (Ego Lite)" not in rules
    assert "`ego-browser` command was not found" in err and "https://lite.ego.app/" in err and "Starting without the browser" in err


# ------------------------------------------------------------------ Codex prompts


@pytest.mark.parametrize("role, gets_note", [("team_plan", True), ("team_ask", True), ("team_direct", True), ("team_review", False), ("team_plan_review", False)])
def test_codex_gets_the_browser_note_only_for_roles_that_may_browse(home, tmp_path, role, gets_note):
    install_ego(home)
    cfg = config.TeamConfig("c", "x", "m", "high", 50, 60, browser="ego")
    assert bool(relay.browser_note(cfg, tmp_path / "shop", role)) is gets_note


def test_codex_prompts_are_unchanged_when_the_browser_is_off_or_missing(home, tmp_path):
    assert relay.browser_note(config.TeamConfig("c", "x", "m", "high", 50, 60, browser="ego"), tmp_path, "team_plan") == ""
    install_ego(home)
    assert relay.browser_note(config.TeamConfig("c", "x", "m", "high", 50, 60), tmp_path, "team_plan") == ""


def test_the_codex_sandbox_is_not_loosened_for_the_browser():
    cfg = config.TeamConfig("c", "x", "m", "high", 50, 60, browser="ego")
    assert relay.sandbox_for(cfg, "plan") == "read-only" and relay.sandbox_for(cfg, "review") == "read-only"
    assert relay.sandbox_for(cfg, "ask") == "read-only"


# ------------------------------------------------------------------ wf doctor


def test_doctor_says_off_when_the_browser_is_off(home):
    check = doctor.check_browser(home, env_for(home))
    assert check.status == doctor.OK and check.detail.startswith("browser: off")


def test_doctor_warns_with_setup_steps_and_never_fails(home):
    set_browser(home, "ego")
    check = doctor.check_browser(home, env_for(home))
    assert check.status == doctor.WARN and "Download Ego Lite from https://lite.ego.app/" in check.fix


def test_doctor_is_happy_when_ego_lite_is_ready(home):
    cli = install_ego(home)
    set_browser(home, "ego")
    check = doctor.check_browser(home, env_for(home))
    assert check.status == doctor.OK and str(cli) in check.detail


def test_doctor_does_not_create_team_toml(tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    doctor.check_browser(fresh, {"PATH": ""})
    assert not (fresh / ".workforce" / "team.toml").exists()


# ------------------------------------------------------------------ the gates still apply


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


SMOKE = """ego-browser nodejs <<'EOF'
const task = await taskSpace("wf repo claude abc123");
const page = task.page("p1");
await page.goto("http://localhost:3000");
console.log(await page.title());
await task.finish({ keep: [] });
EOF"""


def test_a_browser_command_passes_the_hooks_like_any_other_command(repo, home):
    payload = {"tool_name": "Bash", "tool_input": {"command": SMOKE}, "cwd": str(repo), "session_id": "s1"}
    assert hooks.evaluate("pretooluse", payload, env={"CLAUDE_PROJECT_DIR": str(repo)}, home=home) is None


def test_browsing_does_not_open_the_commit_gate(repo, home):
    command = SMOKE + "\ngit commit -m 'checked in the browser'"
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo), "session_id": "s1"}
    output = hooks.evaluate("pretooluse", payload, env={"CLAUDE_PROJECT_DIR": str(repo)}, home=home)
    assert output and output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_settings_view_shows_the_browser(home):
    from workforce.team import mcp_server

    assert mcp_server._browser_line(config.TeamConfig("c", "x", "m", "high", 50, 60)).startswith("off")
    assert "not ready" in mcp_server._browser_line(config.TeamConfig("c", "x", "m", "high", 50, 60, browser="ego"))
    install_ego(home)
    assert mcp_server._browser_line(config.TeamConfig("c", "x", "m", "high", 50, 60, browser="ego")) == "ego · Ego Lite ready for Claude and Codex"
