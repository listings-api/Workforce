"""`wf`: start the real interactive Claude Code with the WorkForce plugin, status line and team rules added.

WorkForce only runs as a chat: the unattended pipeline commands (`wf run`, `status`, `init`, …) and `wf -p` were removed.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

from workforce.team import banner, plugin_runtime

EXIT_OK = 0
EXIT_PREFLIGHT = 3
OLD_COMMANDS = frozenset(
    {"run", "status", "usage", "questions", "answer", "models", "pause", "resume", "log", "init", "repos", "publish"}
)
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
STEERING_VARS = ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
PRINT_FLAGS = ("-p", "--print")
TEAM_FILE = plugin_runtime.TEAM_FILE
SUBCOMMANDS = {"doctor": "workforce.team.doctor", "demo": "workforce.team.demo"}
STATUSLINE_MODULE = "workforce.team.statusline"


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


def _browser_text(cfg, environ: Mapping[str, str], home: Path | None) -> str:
    """Claude's browser rules when `browser = "ego"` and Ego Lite is ready; otherwise nothing, with a note saying what is missing."""
    if getattr(cfg, "browser", "off") != "ego":
        return ""
    from workforce.team import browser

    try:
        state = browser.status(cfg.browser, environ, home)
    except OSError as exc:
        print(f"! browser = \"ego\" in team.toml, but Ego Lite could not be checked ({exc}); starting without the browser.", file=sys.stderr)
        return ""
    if state.problems:
        print(f"! browser = \"ego\" in team.toml, but {'; '.join(state.problems)}.", file=sys.stderr)
        print(f"  {browser.SETUP_STEPS}", file=sys.stderr)
    if not state.claude_ready:
        print("  Starting without the browser. `wf doctor` shows the browser check.", file=sys.stderr)
        return ""
    return browser.claude_text(Path.cwd(), state.cli)


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


def subcommand_main(name: str, args: Sequence[str]) -> int:
    """`wf doctor` / `wf demo`: hand the rest of the command line to that module's `main`."""
    import importlib

    module = SUBCOMMANDS[name]
    try:
        entry = importlib.import_module(module).main
    except (ImportError, AttributeError) as exc:
        return _error(f"`wf {name}` is not available: could not load {module} ({exc}).")
    return entry(list(args))


def _repair_binaries(home: Path | None) -> bool:
    """Let config fix a stale claude/codex path before the check below; True when it changed something."""
    from workforce.team import config as team_config

    repair = getattr(team_config, "repair_binaries", None)
    if repair is None:
        return False
    try:
        notes = repair(home)
    except Exception as exc:
        print(f"! could not check the claude/codex paths in team.toml ({exc}).", file=sys.stderr)
        return False
    for note in notes or []:
        print(note, file=sys.stderr)
    return bool(notes)


def main(
    argv: Sequence[str] | None = None,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    execvp: Callable[[str, list[str]], object] = os.execvp,
    stdout=None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in OLD_COMMANDS:
        return _error(
            f"`wf {args[0]}` was removed: WorkForce only runs as a chat. Start `wf` in your project and type the task, "
            'or give it as the first message: wf "your task".'
        )

    environ = os.environ if environ is None else environ
    if args and args[0] in SUBCOMMANDS:
        return subcommand_main(args[0], args[1:])
    if args and args[0] == "codex":
        return codex_main(args[1:], home=home, environ=environ, execvp=execvp)
    if any(arg in PRINT_FLAGS for arg in args):
        return _error(
            "WorkForce only runs as a chat, so `wf -p` / `wf --print` is not supported. "
            'Start `wf`, optionally with your first message: wf "fix the failing test".'
        )
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
    if _repair_binaries(home):
        try:
            cfg = team_config.load(home)
        except ConfigError as exc:
            return _error(str(exc))
    claude = Path(cfg.claude).expanduser()
    if not (claude.is_file() and os.access(claude, os.X_OK)):
        return _error(f"claude not found at {claude}. Set `claude = \"…\"` in ~/.workforce/team.toml.")
    try:
        plugin = plugin_runtime.prepare(home, cfg)
    except plugin_runtime.PluginMissing as exc:
        return _error(str(exc))
    except OSError as exc:
        return _error(f"could not write the WorkForce plugin to {plugin_runtime.prepared_dir(home)} ({exc}).")
    python = sys.executable
    team_text = (plugin / TEAM_FILE).read_text(encoding="utf-8") + _browser_text(cfg, environ, home)
    stream = stdout if stdout is not None else sys.stdout
    if banner_wanted(args, stream):
        try:
            from workforce.team import claude_info

            claude_label = claude_info.banner_label(args, environ, home=home, wf_home=home)
        except Exception:
            claude_label = None
        banner.print_banner(cfg.codex_model, cfg.codex_effort, dict(environ), stream, claude=claude_label)
    try:
        from workforce.team import githook

        extra = githook.session_env(environ, githook.install(python, home))
    except OSError as exc:
        return _error(f"could not set up WorkForce's commit check in {githook.hooks_dir(home)} ({exc}).")
    if isinstance(environ, dict) or environ is os.environ:
        environ.update(extra)
    command = build_argv(claude, plugin, python, team_text, args)
    execvp(command[0], command)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
