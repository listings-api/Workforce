"""Optional Ego Lite browser support: find its official `ego-browser` CLI and agent skill, and tell each agent how to use them.

WorkForce never installs Ego Lite, imports a browser profile or changes the default browser. It only checks what the
user installed (the app's onboarding puts `ego-browser` on PATH and the `ego-browser` skill into each agent's skills
folder) and, when `browser = "ego"` is set in team.toml, adds the rules below to Claude's and Codex's instructions.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from workforce.team import detect

MODES = ("off", "ego")
CLI = "ego-browser"
SKILL = "ego-browser"
APP_NAME = "ego lite.app"
APP_HELPER_GLOB = "Contents/Frameworks/*.framework/Versions/Current/Helpers/ego-browser"
CODEX_ROLES = frozenset({"team_plan", "team_ask", "team_direct"})
SETUP_URL = "https://lite.ego.app/"
SETUP_STEPS = (
    f"Install Ego Lite from {SETUP_URL} and open it once to finish its onboarding: that puts `ego-browser` on your PATH "
    "and adds the `ego-browser` skill for Claude Code and Codex. Importing Chrome data and making it your default "
    "browser are optional and not needed. Check with: ego-browser nodejs -e \"console.log('ego-browser ready')\""
)


@dataclass(frozen=True)
class Status:
    enabled: bool
    cli: Path | None = None
    app: Path | None = None
    claude_skill: Path | None = None
    codex_skill: Path | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def claude_ready(self) -> bool:
        return self.enabled and self.cli is not None and self.claude_skill is not None

    @property
    def codex_ready(self) -> bool:
        return self.enabled and self.cli is not None and self.codex_skill is not None


def _home(home: Path | None) -> Path:
    return Path(home) if home is not None else Path.home()


def find_app(home: Path | None = None) -> Path | None:
    for folder in (Path("/Applications"), _home(home) / "Applications"):
        if (folder / APP_NAME).is_dir():
            return folder / APP_NAME
    return None


def find_cli(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path | None:
    """`ego-browser` on PATH or in the usual folders, else the copy inside the Ego Lite app; None if neither exists."""
    found = detect.find_cli(CLI, environ, home)
    if found is not None:
        return found
    app = find_app(home)
    if app is not None:
        for candidate in sorted(app.glob(APP_HELPER_GLOB)):
            if os.access(candidate, os.X_OK):
                return candidate
    return None


def _skill(folders: list[Path]) -> Path | None:
    for folder in folders:
        if (folder / SKILL / "SKILL.md").is_file():
            return folder / SKILL
    return None


def claude_skill(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path | None:
    environ = os.environ if environ is None else environ
    base = Path(environ["CLAUDE_CONFIG_DIR"]).expanduser() if environ.get("CLAUDE_CONFIG_DIR") else _home(home) / ".claude"
    return _skill([base / "skills"])


def codex_skill(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path | None:
    environ = os.environ if environ is None else environ
    codex_home = Path(environ["CODEX_HOME"]).expanduser() if environ.get("CODEX_HOME") else _home(home) / ".codex"
    return _skill([_home(home) / ".agents" / "skills", codex_home / "skills"])


def status(mode: str, environ: Mapping[str, str] | None = None, home: Path | None = None) -> Status:
    """What is installed for `browser = mode`; `problems` say what is missing, in words a user can act on."""
    if mode != "ego":
        return Status(enabled=False)
    cli = find_cli(environ, home)
    app = find_app(home)
    claude = claude_skill(environ, home)
    codex = codex_skill(environ, home)
    problems = []
    if cli is None:
        problems.append("the `ego-browser` command was not found" + ("" if app else " and Ego Lite is not installed"))
    if claude is None:
        problems.append("the `ego-browser` skill for Claude Code is missing (~/.claude/skills/ego-browser)")
    if codex is None:
        problems.append("the `ego-browser` skill for Codex is missing (~/.agents/skills/ego-browser)")
    return Status(enabled=True, cli=cli, app=app, claude_skill=claude, codex_skill=codex, problems=problems)


def space_name(project: Path | str, agent: str, tag: str | None = None) -> str:
    """A Space name no other agent uses: `wf <project> <agent> <random tag>`."""
    folder = re.sub(r"[^A-Za-z0-9]+", "-", Path(project).name).strip("-") or "project"
    return f"wf {folder} {agent} {tag or secrets.token_hex(3)}"


def _rules(space: str, cli: Path) -> str:
    return (
        f"- Use the `ego-browser` skill (command: {cli}). Work only in your own Space: `await taskSpace(\"{space}\")`. "
        "Never list, read, use or close the user's tabs or other Spaces, and don't call profiles(), handOff() or "
        "takeOverTaskSpace() unless the user asks. Close your Space when done: `await task.finish({ keep: [] })`.\n"
        "- The browser shares the user's logins. Browsing never authorizes sending messages or emails, posting, "
        "publishing, purchases or payments, accepting terms, or changing account settings or passwords. If a step "
        "would do any of that, stop and ask the user to do it themselves.\n"
        "- Don't run `ego-browser upgrade`, clear browser data, import profiles or install anything; tell the user instead. "
        "Save screenshots inside the project or a temp folder. Don't start git or other programs from inside a browser script.\n"
    )


def claude_text(project: Path | str, cli: Path, tag: str | None = None) -> str:
    """The browser rules added to Claude's team rules when the browser is ready."""
    space = space_name(project, "claude", tag)
    return (
        "\n\n# Browser (Ego Lite)\n"
        "You may use the browser to test local web apps, check pages and research.\n"
        + _rules(space, cli)
        + f"- A sub-agent uses its own Space: `{space} <its task in 2-3 words>`.\n"
        "- Browser checks don't replace the reviews: a commit still needs both approvals.\n"
    )


def codex_text(project: Path | str, cli: Path, role: str) -> str:
    """The browser note added to a Codex prompt; Codex's sandbox is not loosened for it."""
    space = space_name(project, f"codex {role.removeprefix('team_')}")
    return (
        "\n\n## Browser (Ego Lite)\n"
        "You may use the browser to check pages or a locally running app if it helps.\n"
        + _rules(space, cli)
        + "- If your sandbox blocks `ego-browser`, say so in your reply and carry on without it; don't try to get around the sandbox.\n"
    )
