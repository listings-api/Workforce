import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from workforce.team import config, plugin_runtime

ROOT = Path(__file__).resolve().parent.parent
UV = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
CFG = config.TeamConfig("c", "x", "m", "high", 50, 60)
slow = pytest.mark.skipif(os.environ.get("WF_SLOW") != "1", reason="set WF_SLOW=1 to build and install the package for real")


def source_files() -> list[str]:
    source = plugin_runtime.source_dir()
    return sorted(
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.relative_to(source).parts[0] != "agents"
    )


def copy_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(ROOT / "pyproject.toml", project)
    shutil.copytree(ROOT / "workforce", project / "workforce", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return project


def build_wheel(project: Path, out: Path) -> Path:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    probe = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, env=env)
    if probe.returncode == 0:
        command = [sys.executable, "-m", "pip", "wheel", str(project), "--no-deps", "-w", str(out)]
    else:
        command = [UV, "build", "--wheel", "-o", str(out), str(project)]
    done = subprocess.run(command, capture_output=True, text=True, env=env, cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    (wheel,) = out.glob("workforce-*.whl")
    return wheel


def test_the_plugin_lives_in_the_package_and_ships_no_generated_agents():
    files = source_files()
    assert ".claude-plugin/plugin.json" in files and ".mcp.json" in files and "TEAM.md" in files
    assert "hooks/hooks.json" in files and "agent-templates/fast-coder.md" in files
    assert any(name.startswith("commands/") for name in files)
    assert not (plugin_runtime.source_dir() / "agents").exists()
    assert not (ROOT / "wf-plugin").exists()


def test_the_wheel_contains_every_plugin_file(tmp_path):
    wheel = build_wheel(copy_project(tmp_path), tmp_path / "dist")
    inside = set(zipfile.ZipFile(wheel).namelist())
    missing = [name for name in source_files() if f"workforce/team/plugin/{name}" not in inside]
    assert not missing, missing
    assert not [name for name in inside if name.startswith("workforce/team/plugin/agents/")]
    assert "workforce/team/plugin_runtime.py" in inside


def test_the_project_is_version_0_3_0_with_a_repository_url():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == "0.3.0"
    assert project["urls"]["Repository"] == "https://github.com/listings-api/Workforce"


def test_prepare_writes_a_runnable_copy_under_wf_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WF_HOME", str(tmp_path))
    target = plugin_runtime.prepare(cfg=CFG)
    assert target == tmp_path / ".workforce" / "plugin"
    assert (target / ".claude-plugin" / "plugin.json").is_file() and (target / "TEAM.md").is_file()
    assert (target / "agents" / "fast-coder.md").is_file()
    assert not list(target.rglob("*.pyc")) and not [p for p in target.rglob("*") if p.name.startswith(".wf-tmp-")]


def test_prepare_quotes_a_python_path_with_spaces(tmp_path):
    target = plugin_runtime.prepare(tmp_path, CFG, python="/opt/my tools/bin/python")
    hooks = json.loads((target / "hooks" / "hooks.json").read_text())["hooks"]
    for event in ("PreToolUse", "UserPromptSubmit"):
        command = hooks[event][0]["hooks"][0]["command"]
        assert shlex.split(command)[:3] == ["/opt/my tools/bin/python", "-m", "workforce.team.hooks"]
    mcp = json.loads((target / ".mcp.json").read_text())
    assert mcp["mcpServers"]["codex"]["command"] == "/opt/my tools/bin/python"


def test_prepare_is_idempotent_and_leaves_unchanged_files_alone(tmp_path):
    target = plugin_runtime.prepare(tmp_path, CFG)
    before = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}
    assert plugin_runtime.prepare(tmp_path, CFG) == target
    assert {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()} == before


def test_prepare_repairs_edited_files_and_removes_ones_no_longer_in_the_source(tmp_path):
    target = plugin_runtime.prepare(tmp_path, CFG)
    (target / "TEAM.md").write_text("tampered")
    (target / "commands" / "gone.md").write_text("old command")
    (target / "old-dir" / "deep").mkdir(parents=True)
    (target / "old-dir" / "deep" / "x.txt").write_text("x")
    (target / "agents" / "reviewer.md").write_text("old reviewer")
    plugin_runtime.prepare(tmp_path, CFG)
    assert (target / "TEAM.md").read_text() == (plugin_runtime.source_dir() / "TEAM.md").read_text()
    assert not (target / "commands" / "gone.md").exists()
    assert not (target / "old-dir").exists()
    assert not (target / "agents" / "reviewer.md").exists()


def test_prepare_follows_the_models_in_team_toml(tmp_path):
    target = plugin_runtime.prepare(tmp_path, config.TeamConfig("c", "x", "m", "high", 50, 60, fast_coder_model="claude-haiku-4-5-20251001", fast_coder_effort="low"))
    text = (target / "agents" / "fast-coder.md").read_text()
    assert "model: claude-haiku-4-5-20251001" in text and "effort: low" in text
    config.set_team_models(fast_coder_model="claude-sonnet-5-5", home=tmp_path)
    plugin_runtime.prepare(tmp_path)
    assert "model: claude-sonnet-5-5" in (target / "agents" / "fast-coder.md").read_text()


def test_two_wf_starting_at_once_leave_a_complete_plugin(tmp_path):
    errors = []

    def go():
        try:
            for _ in range(15):
                plugin_runtime.prepare(tmp_path, CFG)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    target = tmp_path / ".workforce" / "plugin"
    expected = set(plugin_runtime.desired_files(CFG))
    assert {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} == expected


def test_an_abandoned_temp_file_is_removed_but_a_fresh_one_is_left_for_its_writer(tmp_path):
    target = plugin_runtime.prepare(tmp_path, CFG)
    fresh, abandoned = target / f"{plugin_runtime.TEMP_PREFIX}fresh", target / f"{plugin_runtime.TEMP_PREFIX}old"
    fresh.write_text("x")
    abandoned.write_text("x")
    old = abandoned.stat().st_mtime - plugin_runtime.STALE_TEMP_SECONDS - 10
    os.utime(abandoned, (old, old))
    plugin_runtime.prepare(tmp_path, CFG)
    assert fresh.exists() and not abandoned.exists()


def test_prepare_says_so_when_the_package_lacks_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_runtime, "source_dir", lambda: tmp_path / "nothing")
    with pytest.raises(plugin_runtime.PluginMissing, match="Reinstall workforce"):
        plugin_runtime.prepare(tmp_path, CFG)


@slow
def test_uv_tool_install_gives_a_working_wf_and_a_plugin_that_uses_that_tool_python(tmp_path):
    tool_dir, bin_dir, home = tmp_path / "tools", tmp_path / "bin", tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(tool_dir),
        "UV_TOOL_BIN_DIR": str(bin_dir),
        "WF_HOME": str(home),
        "HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    project = copy_project(tmp_path)
    done = subprocess.run([UV, "tool", "install", "--force", "--from", str(project), "workforce"], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    helped = subprocess.run([str(bin_dir / "wf"), "run", "--help"], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert helped.returncode == 0 and "usage" in (helped.stdout + helped.stderr).lower()
    tool_python = next((tool_dir / "workforce" / "bin").glob("python*"))
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n")
    fake_claude.chmod(0o755)
    (home / ".workforce").mkdir()
    (home / ".workforce" / "team.toml").write_text(f'claude = "{fake_claude}"\ncodex = "/x/codex"\n')
    started = subprocess.run([str(bin_dir / "wf"), "hi"], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert started.returncode == 0, started.stderr
    mcp = json.loads((home / ".workforce" / "plugin" / ".mcp.json").read_text())
    command = mcp["mcpServers"]["codex"]["command"]
    assert Path(command).parent == tool_python.parent and str(tool_dir) in command
    hooks = (home / ".workforce" / "plugin" / "hooks" / "hooks.json").read_text()
    assert str(tool_dir) in hooks and ".venv" not in hooks
