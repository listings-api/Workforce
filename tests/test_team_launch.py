import io
import json
import re
import stat
import sys
from pathlib import Path

import pytest

from workforce.team import banner, launch, plugin_runtime

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from workforce.team import config

    monkeypatch.setattr(config, "repair_binaries", lambda home: [], raising=False)
    root = tmp_path / "agent-team"
    root.mkdir()
    home = tmp_path / "home"
    (home / ".workforce").mkdir(parents=True)
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\n")
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    (home / ".workforce" / "team.toml").write_text(f'claude = "{claude}"\ncodex = "/x/codex"\n')
    calls = []
    return root, home, claude, calls


class Tty(io.StringIO):
    """A stand-in for a terminal: the banner is only printed to one."""

    def isatty(self):
        return True


def run(setup, argv, environ=None, stdout=None):
    root, home, _, calls = setup
    code = launch.main(
        argv,
        home=home,
        environ={} if environ is None else environ,
        execvp=lambda file, args: calls.append((file, args)),
        stdout=stdout or io.StringIO(),
    )
    return code


def test_build_argv_shape():
    argv = launch.build_argv("/bin/claude", "/p/plugin", "/p/tool/bin/python", "RULES", ["fix it"])
    assert argv[0] == "/bin/claude"
    assert argv[1:3] == ["--plugin-dir", "/p/plugin"]
    assert argv[3] == "--settings"
    settings = json.loads(argv[4])
    assert settings == {"statusLine": {"type": "command", "command": "/p/tool/bin/python -m workforce.team.statusline"}, "env": {"CLAUDE_CODE_HIDE_CWD": "1"}}
    assert argv[5:7] == ["--append-system-prompt", "RULES"]
    assert argv[7:] == ["fix it"]


def test_build_argv_quotes_python_with_spaces():
    argv = launch.build_argv("c", "p", "/a b/python", "t")
    assert json.loads(argv[4])["statusLine"]["command"] == "'/a b/python' -m workforce.team.statusline"


@pytest.mark.parametrize("args", [[], ["fix the flaky test in billing"], ["--resume"], ["-c"], ["--model", "opus", "hello"]])
def test_wf_execs_claude_with_user_args_unchanged(setup, args):
    _, home, claude, calls = setup
    assert run(setup, args) == 0
    (file, argv), = calls
    plugin = home / ".workforce" / "plugin"
    assert file == str(claude)
    assert argv[argv.index("--append-system-prompt") + 1] == (plugin / "TEAM.md").read_text()
    assert argv[argv.index("--plugin-dir") + 1] == str(plugin)
    assert argv[argv.index("--settings") + 1].count(sys.executable) == 1
    assert argv[-len(args):] == args if args else True


def test_wf_prints_banner_before_exec(setup):
    out = Tty()
    run(setup, [], stdout=out)
    text = ANSI.sub("", out.getvalue())
    assert "WorkForce · Claude + Codex team" in text
    assert "Codex gpt-6-sol · high" in text


@pytest.mark.parametrize(
    "command", ["run", "status", "usage", "questions", "answer", "models", "pause", "resume", "log", "init", "repos", "publish"]
)
def test_the_removed_pipeline_commands_say_so_and_start_nothing(setup, command, capsys):
    assert run(setup, [command, "some", "--flag"]) == 3
    assert f"`wf {command}` was removed" in capsys.readouterr().err
    assert setup[3] == []


@pytest.mark.parametrize("var", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"])
def test_api_key_refused_with_exit_3(setup, capsys, var):
    assert run(setup, ["hello"], environ={var: "sk-test"}) == 3
    assert var in capsys.readouterr().err
    assert setup[3] == []


def test_empty_api_key_value_is_not_refused(setup):
    assert run(setup, [], environ={"ANTHROPIC_API_KEY": ""}) == 0


def test_wf_needs_no_venv_next_to_the_package(setup):
    assert not (setup[0] / ".venv").exists()
    assert run(setup, []) == 0
    assert len(setup[3]) == 1


def test_plugin_files_missing_from_the_package_is_a_clear_error(setup, capsys, monkeypatch, tmp_path):
    empty = tmp_path / "empty-plugin"
    empty.mkdir()
    monkeypatch.setattr(plugin_runtime, "source_dir", lambda: empty)
    assert run(setup, []) == 3
    err = capsys.readouterr().err
    assert "plugin files are missing" in err and "Reinstall workforce" in err
    assert setup[3] == []


def test_missing_claude_is_a_clear_error(setup, capsys):
    setup[2].unlink()
    assert run(setup, []) == 3
    assert "claude not found" in capsys.readouterr().err
    assert setup[3] == []


def test_first_run_creates_team_toml_in_the_given_home(tmp_path):
    home = tmp_path / "home"
    launch.main([], home=home, environ={}, execvp=lambda *a: None, stdout=io.StringIO())
    assert (home / ".workforce" / "team.toml").exists()
    assert (home / ".workforce" / "plugin" / "TEAM.md").is_file()


def test_the_prepared_plugin_points_at_the_running_python(setup):
    _, home, _, _ = setup
    assert run(setup, []) == 0
    plugin = home / ".workforce" / "plugin"
    assert json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["codex"]["command"] == sys.executable


def test_wf_home_env_decides_where_the_plugin_is_prepared(setup, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".workforce").mkdir(parents=True)
    (elsewhere / ".workforce" / "team.toml").write_text(f'claude = "{setup[2]}"\ncodex = "/x/codex"\n')
    monkeypatch.setenv("WF_HOME", str(elsewhere))
    calls = []
    assert launch.main([], environ={}, execvp=lambda f, a: calls.append(a), stdout=io.StringIO()) == 0
    assert calls[0][calls[0].index("--plugin-dir") + 1] == str(tmp_path / "elsewhere" / ".workforce" / "plugin")


def test_githook_scripts_run_the_current_python(setup):
    _, home, _, _ = setup
    run(setup, [])
    script = (home / ".workforce" / "git-hooks" / "pre-commit").read_text()
    assert sys.executable in script


@pytest.mark.parametrize("name", ["doctor", "demo"])
def test_doctor_and_demo_hand_the_rest_of_the_line_to_their_modules(setup, monkeypatch, name):
    import types

    seen = []
    module = types.ModuleType(f"workforce.team.{name}")
    module.main = lambda argv: seen.append(list(argv)) or 5
    monkeypatch.setitem(sys.modules, f"workforce.team.{name}", module)
    assert run(setup, [name, "--flag", "x"]) == 5
    assert seen == [["--flag", "x"]]
    assert setup[3] == []


@pytest.mark.parametrize("name", ["doctor", "demo"])
def test_doctor_and_demo_missing_module_is_exit_3(setup, monkeypatch, capsys, name):
    monkeypatch.setitem(sys.modules, f"workforce.team.{name}", None)
    assert run(setup, [name]) == 3
    assert f"`wf {name}` is not available" in capsys.readouterr().err


def test_repair_binaries_notes_are_printed_and_config_is_reloaded(setup, monkeypatch, capsys):
    from workforce.team import config

    _, home, claude, calls = setup
    (home / ".workforce" / "team.toml").write_text('claude = "/gone/claude"\ncodex = "/x/codex"\n')

    def repair(given_home):
        (home / ".workforce" / "team.toml").write_text(f'claude = "{claude}"\ncodex = "/x/codex"\n')
        return ["fixed claude path"]

    monkeypatch.setattr(config, "repair_binaries", repair, raising=False)
    assert run(setup, []) == 0
    assert "fixed claude path" in capsys.readouterr().err
    assert calls[0][0] == str(claude)


def test_repair_binaries_failure_does_not_stop_wf(setup, monkeypatch, capsys):
    from workforce.team import config

    def boom(home):
        raise ValueError("bad")

    monkeypatch.setattr(config, "repair_binaries", boom, raising=False)
    assert run(setup, []) == 0
    assert "could not check" in capsys.readouterr().err


# ---- banner


def test_banner_has_five_rows_with_truecolor_gradient_and_text():
    rows = banner.render(True, "gpt-6-sol", "high", "/work/billing")
    assert len(rows) == 5
    assert "\x1b[38;2;90;168;255m" in rows[0]
    assert "\x1b[38;2;47;111;224m" in rows[-1]
    plain = [ANSI.sub("", row) for row in rows]
    assert plain[0].startswith("██╗    ██╗███████╗")
    joined = "\n".join(plain)
    assert "WorkForce · Claude + Codex team" in joined
    assert "Claude (as configured in Claude Code)  │  Codex gpt-6-sol · high" in joined
    assert "/work/billing" in joined
    assert all(row.endswith("\x1b[0m") or ANSI.sub("", row) for row in rows)


def test_banner_256_colour_fallback():
    rows = banner.render(False, cwd="/x")
    assert all("\x1b[38;5;" in row and "38;2;" not in row for row in rows)


def test_banner_defaults_to_the_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert str(tmp_path.resolve()) in ANSI.sub("", "\n".join(banner.render(True)))


def test_print_banner_picks_truecolor_from_colorterm():
    out = io.StringIO()
    banner.print_banner("m", "high", {"COLORTERM": "truecolor"}, out)
    assert "38;2;" in out.getvalue()
    out = io.StringIO()
    banner.print_banner("m", "high", {}, out)
    assert "38;5;" in out.getvalue()


def test_print_banner_flushes_before_exec():
    class Stream(io.StringIO):
        flushed = False

        def flush(self):
            self.flushed = True

    out = Stream()
    banner.print_banner("m", "high", {}, out)
    assert out.flushed
