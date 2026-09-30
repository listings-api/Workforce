import io
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from workforce.team import detect, doctor

CLAUDE_OK = """case "$1" in
  --version) echo "2.1.300 (Claude Code)" ;;
  auth) echo '{"loggedIn": true, "authMethod": "claude.ai"}' ;;
esac"""
CODEX_OK = """case "$1" in
  --version) echo "codex-cli 0.160.0" ;;
  login) echo "Logged in using ChatGPT" ;;
esac"""


def make_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(detect, "WELL_KNOWN_DIRS", ("~/.local/bin",))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").symlink_to(shutil.which("git"))
    make_exe(bindir / "claude", CLAUDE_OK)
    make_exe(bindir / "codex", CODEX_OK)
    return type("Env", (), {"home": home, "bin": bindir, "environ": {"PATH": str(bindir)}})()


def run(env, *args, environ=None, models=None):
    out = io.StringIO()
    calls = []

    def models_probe(binary, home):
        calls.append(binary)
        return models if models is not None else (3, None)

    code = doctor.main(
        list(args),
        home=env.home,
        environ=env.environ if environ is None else environ,
        stdout=out,
        models_probe=models_probe,
    )
    return code, out.getvalue(), calls


def by_name(text):
    return json.loads(text)["checks"]


def find(checks, name):
    return next(c for c in checks if c["name"] == name)


def test_everything_ok_exits_zero_and_points_to_the_demo(env):
    code, out, calls = run(env)
    assert code == 0, out
    assert "✗" not in out
    assert "✓ claude 2.1.300" in out and "✓ codex 0.160.0" in out
    assert "signed in with your Claude subscription" in out
    assert "signed in with your ChatGPT subscription" in out
    assert out.rstrip().endswith("Next: run `wf demo` to try it, or `cd your-project && wf`.")
    assert calls == [env.bin / "codex"]


def test_json_output(env):
    code, out, _ = run(env, "--json")
    data = json.loads(out)
    assert code == 0 and data["ok"] is True
    assert data["next"].startswith("Next: run `wf demo`")
    names = [c["name"] for c in data["checks"]]
    assert {"python", "git", "claude", "claude login", "codex", "codex login", "api keys", "plugin", "workforce folder"} <= set(names)
    assert {c["status"] for c in data["checks"]} <= {"ok", "warn", "fail"}


def test_json_output_reports_failure_and_exit_one(env):
    (env.bin / "claude").unlink()
    code, out, _ = run(env, "--json")
    data = json.loads(out)
    assert code == 1 and data["ok"] is False
    failed = find(data["checks"], "claude")
    assert failed["status"] == "fail" and failed["fix"]


def test_claude_missing_has_a_fix(env):
    (env.bin / "claude").unlink()
    code, out, _ = run(env)
    assert code == 1
    assert "✗ claude not found" in out and "fix: install Claude Code" in out
    assert "Fix the ✗ items above" in out
    assert "claude login" not in out


def test_claude_too_old_points_at_update(env):
    make_exe(env.bin / "claude", CLAUDE_OK.replace("2.1.300", "2.1.100"))
    code, out, _ = run(env)
    assert code == 1
    assert "✗ claude 2.1.100" in out and "fix: upgrade it with `claude update`" in out


def test_claude_api_key_login_is_a_failure(env):
    make_exe(env.bin / "claude", CLAUDE_OK.replace("claude.ai", "apiKey"))
    code, out, _ = run(env)
    assert code == 1
    assert "authMethod is 'apiKey'" in out and "claude auth login" in out


def test_claude_auth_status_not_json_is_a_failure(env):
    make_exe(env.bin / "claude", 'case "$1" in --version) echo "2.1.300";; auth) echo nope;; esac')
    code, out, _ = run(env)
    assert code == 1 and "did not return JSON" in out


def test_claude_version_broken(env):
    make_exe(env.bin / "claude", "exit 4")
    code, out, _ = run(env)
    assert code == 1 and "failed `--version`" in out


def test_codex_not_logged_in_with_chatgpt(env):
    make_exe(env.bin / "codex", CODEX_OK.replace("Logged in using ChatGPT", "Logged in using an API key"))
    code, out, calls = run(env)
    assert code == 1
    assert "not signed in with ChatGPT" in out and "fix: run `codex login`" in out
    assert calls == []


def test_codex_too_old_and_missing(env):
    make_exe(env.bin / "codex", CODEX_OK.replace("0.160.0", "0.100.0"))
    code, out, _ = run(env)
    assert code == 1 and "codex 0.100.0" in out and "`codex update`" in out
    (env.bin / "codex").unlink()
    code, out, calls = run(env)
    assert code == 1 and "✗ codex not found" in out and "fix: install the Codex CLI" in out
    assert calls == []


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"])
def test_api_key_variables_fail(env, name):
    code, out, _ = run(env, environ={**env.environ, name: "set"})
    assert code == 1
    assert f"✗ {name} is set" in out and f"fix: unset {name}" in out


def test_api_key_value_is_never_printed(env):
    code, out, _ = run(env, "--json", environ={**env.environ, "OPENAI_API_KEY": "sk-secret-value-123456"})
    assert code == 1 and "sk-secret-value-123456" not in out


@pytest.mark.parametrize("name", ["ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"])
def test_steering_variables_only_warn(env, name):
    code, out, _ = run(env, environ={**env.environ, name: "1"})
    assert code == 0
    assert f"! {name} is set" in out


def test_git_missing_old_and_clean_merge_note(env):
    (env.bin / "git").unlink()
    code, out, _ = run(env)
    assert code == 1 and "✗ git not found" in out
    make_exe(env.bin / "git", 'echo "git version 2.30.2"')
    code, out, _ = run(env)
    assert code == 1 and "✗ git 2.30.2" in out and "session hooks" in out
    make_exe(env.bin / "git", 'echo "git version 2.35.1"')
    code, out, _ = run(env)
    assert code == 0 and "! git 2.35.1" in out and "clean-merge detection" in out


def test_python_too_old():
    check = doctor.check_python((3, 10, 4))
    assert check.status == "fail" and "3.10.4" in check.detail and check.fix
    assert doctor.check_python((3, 11, 0)).status == "ok"


def test_plugin_missing_is_a_failure(env, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "plugin_source", lambda: None)
    code, out, _ = run(env)
    assert code == 1 and "✗ the WorkForce plugin files were not found" in out
    incomplete = tmp_path / "plugin"
    incomplete.mkdir()
    monkeypatch.setattr(doctor, "plugin_source", lambda: incomplete)
    code, out, _ = run(env)
    assert code == 1 and "plugin.json" in out and "fix: reinstall WorkForce" in out


def test_packaged_plugin_is_found():
    check = doctor.check_plugin()
    assert check.status == "ok", check


def test_unwritable_workforce_folder_fails(env):
    (env.home / ".workforce").write_text("a file, not a folder")
    code, out, _ = run(env)
    assert code == 1 and "✗ cannot write to" in out and "fix: make" in out


def test_optional_checks_never_fail(env):
    code, out, _ = run(env, models=(0, "could not read the Codex model list (boom)"))
    assert code == 0
    assert "! could not read the Codex model list (boom)" in out


def test_optional_models_probe_exception_is_contained(env):
    out = io.StringIO()

    def boom(binary, home):
        raise RuntimeError("kaput")

    code = doctor.main([], home=env.home, environ=env.environ, stdout=out, models_probe=boom)
    assert code == 0 and "RuntimeError: kaput" in out.getvalue()


def test_configured_path_is_used_when_it_works(env, tmp_path):
    mine = make_exe(tmp_path / "mine" / "claude", CLAUDE_OK.replace("2.1.300", "2.1.301"))
    toml = env.home / ".workforce" / "team.toml"
    toml.parent.mkdir()
    toml.write_text(f'claude = "{mine}"\n')
    code, out, _ = run(env)
    assert code == 0 and f"claude 2.1.301 at {mine}" in out


def test_configured_path_missing_but_detected_warns(env):
    toml = env.home / ".workforce" / "team.toml"
    toml.parent.mkdir()
    toml.write_text('claude = "/nowhere/claude"\n')
    code, out, _ = run(env)
    assert code == 0
    assert "! claude 2.1.300" in out and "team.toml lists /nowhere/claude, which is missing" in out


def test_doctor_does_not_create_team_toml(env):
    run(env)
    assert not (env.home / ".workforce" / "team.toml").exists()


def test_bad_option_returns_two(env, capsys):
    out = io.StringIO()
    assert doctor.main(["--nope"], home=env.home, environ=env.environ, stdout=out) == 2
