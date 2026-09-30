"""`wf`: start the real interactive Claude Code with the WorkForce plugin, status line and team rules added.

The old pipeline subcommands (`wf run`, `status`, `init`, …) still go to `workforce.cli`.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

import workforce
from workforce.team import agents_gen, banner

EXIT_OK = 0
EXIT_PREFLIGHT = 3
OLD_COMMANDS = frozenset(
    {"run", "status", "usage", "questions", "answer", "models", "pause", "resume", "log", "init", "repos", "publish"}
)
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
STEERING_VARS = ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
PRINT_FLAGS = ("-p", "--print")
PLUGIN_DIRNAME = "wf-plugin"
TEAM_FILE = "TEAM.md"
STATUSLINE_MODULE = "workforce.team.statusline"


def project_root() -> Path:
    """The agent-team folder that holds `workforce/`, `wf-plugin/` and `.venv/`."""
    return Path(workforce.__file__).resolve().parent.parent


def plugin_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / PLUGIN_DIRNAME


def venv_python(root: Path | None = None) -> Path:
    """The interpreter the plugin's hooks and MCP server use: `${CLAUDE_PLUGIN_ROOT}/../.venv/bin/python`."""
    return (root or project_root()) / ".venv" / "bin" / "python"


def settings_json(python: str | Path) -> str:
    command = f"{shlex.quote(str(python))} -m {STATUSLINE_MODULE}"
    return json.dumps({"statusLine": {"type": "command", "command": command}, "env": {"CLAUDE_CODE_HIDE_CWD": "1"}})


def build_argv(
    claude: str | Path,
    plugin: str | Path,
    python: str | Path,
    team_text: str,
    user_args: Sequence[str] = (),
) -> list[str]:
    """The exact argv `wf` execs: claude, the plugin, the status-line settings, the team rules, then the user's args."""
    return [
        str(claude),
        "--plugin-dir",
        str(plugin),
        "--settings",
        settings_json(python),
        "--append-system-prompt",
        team_text,
        *user_args,
    ]


def api_key_vars(environ: Mapping[str, str]) -> list[str]:
    return [name for name in API_KEY_VARS if environ.get(name)]


def banner_wanted(args: Sequence[str], stream) -> bool:
    """The banner is for a person at a terminal: never when stdout is not a TTY or in print mode (`-p` / `--print`)."""
    if any(arg in PRINT_FLAGS for arg in args):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _error(message: str) -> int:
    print(f"✗ {message}", file=sys.stderr)
    return EXIT_PREFLIGHT


def codex_argv(codex: str | Path, session: str, extra: Sequence[str] = ()) -> list[str]:
    """The exact argv `wf codex` execs: the interactive Codex CLI resuming the team's session."""
    return [str(codex), "resume", session, *extra]


def new_codex_argv(codex: str | Path, model: str, effort: str, extra: Sequence[str] = ()) -> list[str]:
    """The argv `wf codex` execs when the project has no team session yet: a new interactive Codex on the team's model."""
    return [str(codex), "-m", model, "-c", f"model_reasoning_effort={effort}", *extra]


def codex_main(
    args: Sequence[str],
    *,
    home: Path | None,
    environ: Mapping[str, str],
    execvp: Callable[[str, list[str]], object],
    cwd: Path | None = None,
) -> int:
    """`wf codex [codex-args]`: resume this project's team Codex session (`.workforce/team/session.json`), or start a new Codex chat."""
    from workforce.team import config as team_config
    from workforce.team import relay

    bad = api_key_vars(environ)
    if bad:
        return _error(f"{', '.join(bad)} is set. WorkForce uses your Claude and ChatGPT subscriptions only, never API keys. Unset it and try again.")
    here = Path(cwd) if cwd is not None else Path.cwd()
    folder = relay.find_session_dir(here)
    session = relay.session_id(folder) if folder is not None else None
    cfg = team_config.load(home)
    codex = Path(cfg.codex).expanduser()
    if not (codex.is_file() and os.access(codex, os.X_OK)):
        return _error(f"codex not found at {codex}. Set `codex = \"…\"` in ~/.workforce/team.toml.")
    if session is None:
        print(f"No team Codex session in {here} yet, so this starts a new Codex chat ({cfg.codex_model} · {cfg.codex_effort}).", file=sys.stderr)
        command = new_codex_argv(codex, cfg.codex_model, cfg.codex_effort, args)
    else:
        command = codex_argv(codex, session, args)
    execvp(command[0], command)
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    execvp: Callable[[str, list[str]], object] = os.execvp,
    root: Path | None = None,
    stdout=None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in OLD_COMMANDS:
        from workforce import cli

        return cli.main(args)

    environ = os.environ if environ is None else environ
    if args and args[0] == "codex":
        return codex_main(args[1:], home=home, environ=environ, execvp=execvp)
    bad = api_key_vars(environ)
    if bad:
        return _error(
            f"{', '.join(bad)} is set. WorkForce uses your Claude and ChatGPT subscriptions only, never API keys. "
            "Unset it and try again."
        )

    from workforce.errors import ConfigError
    from workforce.team import config as team_config

    try:
        cfg = team_config.load(home)
    except ConfigError as exc:
        return _error(str(exc))
    steering = [name for name in STEERING_VARS if environ.get(name)]
    if steering:
        print(
            f"! {', '.join(steering)} is set, which can steer Claude Code off your subscription login. "
            "WorkForce uses the subscription only; unset it unless you mean it.",
            file=sys.stderr,
        )
    claude = Path(cfg.claude).expanduser()
    python = venv_python(root)
    plugin = plugin_dir(root)
    if not python.is_file():
        return _error(
            f"{python} is missing. The plugin's hooks and Codex server run from the agent-team virtualenv. "
            "Create it with: uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -e '.[dev]'"
        )
    if not (plugin / ".claude-plugin" / "plugin.json").is_file() or not (plugin / TEAM_FILE).is_file():
        return _error(f"the WorkForce plugin at {plugin} is incomplete (needs .claude-plugin/plugin.json and {TEAM_FILE}).")
    if not (claude.is_file() and os.access(claude, os.X_OK)):
        return _error(f"claude not found at {claude}. Set `claude = \"…\"` in ~/.workforce/team.toml.")

    try:
        agents_gen.regenerate(plugin, cfg)
    except OSError as exc:
        print(f"! could not update the plugin's agent files from team.toml ({exc}); using the existing ones.", file=sys.stderr)
    team_text = (plugin / TEAM_FILE).read_text(encoding="utf-8")
    stream = stdout if stdout is not None else sys.stdout
    if banner_wanted(args, stream):
        try:
            from workforce.team import claude_info

            claude_label = claude_info.banner_label(args, environ, home=home, wf_home=home)
        except Exception:
            claude_label = None
        banner.print_banner(cfg.codex_model, cfg.codex_effort, dict(environ), stream, claude=claude_label)
    command = build_argv(claude, plugin, python, team_text, args)
    execvp(command[0], command)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
