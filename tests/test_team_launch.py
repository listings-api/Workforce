import io
import re
import shutil
import stat
from pathlib import Path

import pytest

from workforce.team import banner, launch

REAL_PLUGIN = Path(launch.__file__).resolve().parents[2] / "wf-plugin"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "agent-team"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("")
    shutil.copytree(REAL_PLUGIN, root / "wf-plugin")
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
        root=root,
        stdout=stdout or io.StringIO(),
    )
    return code


def test_build_argv_shape():
    argv = launch.build_argv("/bin/claude", "/p/wf-plugin", "/p/.venv/bin/python", "RULES", ["fix it"])
    assert argv[0] == "/bin/claude"
    assert argv[1:3] == ["--plugin-dir", "/p/wf-plugin"]
    assert argv[3] == "--settings"
    import json

    settings = json.loads(argv[4])
    assert settings == {"statusLine": {"type": "command", "command": "/p/.venv/bin/python -m workforce.team.statusline"}, "env": {"CLAUDE_CODE_HIDE_CWD": "1"}}
    assert argv[5:7] == ["--append-system-prompt", "RULES"]
    assert argv[7:] == ["fix it"]


def test_build_argv_quotes_python_with_spaces():
    import json

    argv = launch.build_argv("c", "p", "/a b/python", "t")
    assert json.loads(argv[4])["statusLine"]["command"] == "'/a b/python' -m workforce.team.statusline"


@pytest.mark.parametrize("args", [[], ["fix the flaky test in billing"], ["--resume"], ["-c"], ["--model", "opus", "hello"]])
def test_wf_execs_claude_with_user_args_unchanged(setup, args):
    root, _, claude, calls = setup
    assert run(setup, args) == 0
    (file, argv), = calls
    assert file == str(claude)
    assert argv[argv.index("--append-system-prompt") + 1] == (root / "wf-plugin" / "TEAM.md").read_text()
    assert argv[argv.index("--plugin-dir") + 1] == str(root / "wf-plugin")
    assert argv[argv.index("--settings") + 1].count(str(root / ".venv" / "bin" / "python")) == 1
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
def test_old_subcommands_delegate_to_workforce_cli(setup, monkeypatch, command):
    seen = []
    monkeypatch.setattr("workforce.cli.main", lambda argv: seen.append(list(argv)) or 7)
    assert run(setup, [command, "some", "--flag"]) == 7
    assert seen == [[command, "some", "--flag"]]
    assert setup[3] == []


@pytest.mark.parametrize("var", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"])
def test_api_key_refused_with_exit_3(setup, capsys, var):
    assert run(setup, ["hello"], environ={var: "sk-test"}) == 3
    assert var in capsys.readouterr().err
    assert setup[3] == []


def test_empty_api_key_value_is_not_refused(setup):
    assert run(setup, [], environ={"ANTHROPIC_API_KEY": ""}) == 0


def test_missing_venv_python_is_a_clear_error(setup, capsys):
    root = setup[0]
    (root / ".venv" / "bin" / "python").unlink()
    assert run(setup, []) == 3
    err = capsys.readouterr().err
    assert ".venv/bin/python" in err and "missing" in err
    assert setup[3] == []


def test_missing_claude_is_a_clear_error(setup, capsys):
    setup[2].unlink()
    assert run(setup, []) == 3
    assert "claude not found" in capsys.readouterr().err
    assert setup[3] == []


def test_first_run_creates_team_toml_in_the_given_home(tmp_path):
    root = tmp_path / "agent-team"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("")
    shutil.copytree(REAL_PLUGIN, root / "wf-plugin")
    home = tmp_path / "home"
    launch.main([], home=home, environ={}, execvp=lambda *a: None, root=root, stdout=io.StringIO())
    assert (home / ".workforce" / "team.toml").exists()


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
