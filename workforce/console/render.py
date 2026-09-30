"""Pure text builders for the console: header, event lines, footer and status (SPEC section 10)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workforce import render_text
from workforce.config import Config, Role
from workforce.console.logo import render_logo
from workforce.render_text import EventFormatter, clip, sanitize, short_model
from workforce.state import Run

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
LEFT_MARGIN = "  "
LOGO_GAP = "   "
RULE_CHAR = "─"
MODE_HINT = "shift+tab: auto ⇄ step"
ACTIVE_STATUSES = ("coding", "testing", "reviewing", "fixing", "awaiting_commit", "committing")
FINISHED_RUN_STATUSES = ("done", "failed")
KIND_LABELS = {"5h": "5h", "weekly": "wk"}
POSITION_CLIP = 300
PAUSE_CLIP = 48
MIN_PATH_CHARS = 8
STYLES = {
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "cyan": "\x1b[36m",
}
RESET = "\x1b[0m"
EVENT_STYLES = {
    "alert": "yellow",
    "paused": "yellow",
    "error": "red",
    "run_done": "green",
    "commit_waiting": "cyan",
    "question": "yellow",
}
KIND_NAMES = {"5h": "5-hour window", "weekly": "weekly window"}


@dataclass(frozen=True)
class PreflightSummary:
    """Result of the start-up checks, split so the header can say which part failed."""

    claude_ok: bool = True
    codex_ok: bool = True
    keys_ok: bool = True
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.claude_ok and self.codex_ok and self.keys_ok and not self.problems


def strip_ansi(text: str) -> str:
    return ANSI_PATTERN.sub("", text)


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def style(text: str, name: str | None, color: bool = True) -> str:
    if not color or not name:
        return text
    return f"{STYLES[name]}{text}{RESET}"


def home_relative(path: Path | str, home: Path | str) -> str:
    """`path` with the home directory replaced by `~`."""
    text = str(path)
    home_text = str(home).rstrip("/")
    if text == home_text:
        return "~"
    if text.startswith(home_text + "/"):
        return "~" + text[len(home_text) :]
    return text


def role_text(role: Role) -> str:
    return sanitize(f"{short_model(role.model)} · {role.effort}")


def default_roles(config: Config) -> tuple[Role, Role]:
    """The Claude and Codex roles shown as the defaults (and used for `@claude` / `@codex` side questions)."""
    return config.role("plan_reviewer"), config.role("planner")


def rule(width: int) -> str:
    return LEFT_MARGIN[:1] + RULE_CHAR * max(1, width - 2)


def preflight_line(summary: PreflightSummary) -> str:
    claude = "Claude subscription" if summary.claude_ok else "Claude subscription ✗"
    codex = "ChatGPT subscription" if summary.codex_ok else "ChatGPT subscription ✗"
    keys = "no API keys ✓" if summary.keys_ok else "API key set ✗"
    return f"{claude} · {codex} · {keys}"


def location_text(repo: Path | str, home: Path | str, repo_count: int | None = None) -> str:
    """The repo path, or `Workspace ~/code · 3 repos` when `repo_count` says this is a workspace."""
    path = home_relative(repo, home)
    if repo_count is None:
        return path
    return f"Workspace {path} · {repo_count} {'repo' if repo_count == 1 else 'repos'}"


def header_lines(
    version: str,
    config: Config,
    preflight: PreflightSummary,
    repo: Path | str,
    home: Path | str,
    truecolor: bool,
    color: bool = True,
    repo_count: int | None = None,
) -> list[str]:
    """The logo with the product name, default models, subscription line and repo (or workspace) path beside it."""
    claude, codex = default_roles(config)
    text = [
        style(f"WorkForce Agent v{version}", "bold", color),
        f"Claude {role_text(claude)} │ Codex {role_text(codex)}",
        style(preflight_line(preflight), None if preflight.ok else "red", color),
        location_text(repo, home, repo_count),
    ]
    logo = render_logo(truecolor) if color else [strip_ansi(line) for line in render_logo(truecolor)]
    lines = []
    for index, art in enumerate(logo):
        beside = LOGO_GAP + text[index] if index < len(text) else ""
        lines.append(LEFT_MARGIN + art + beside)
    lines += [style(f"{LEFT_MARGIN}✗ {sanitize(problem)}", "red", color) for problem in preflight.problems]
    return lines


def _question_lines(event: dict[str, Any]) -> list[str]:
    lines = []
    for agent, position in (event.get("positions") or {}).items():
        lines.append(f"  {sanitize(agent)}: {clip(position, POSITION_CLIP)}")
    lines.append(f"  ↳ type /answer {sanitize(event.get('question'))} <your answer>")
    return lines


def event_lines(formatter: EventFormatter, event: dict[str, Any], color: bool = True) -> list[str]:
    """Display lines for one event: the shared formatter's lines, indented, coloured, with a question prompt."""
    lines = formatter.format(event)
    if event.get("kind") == "decision" and event.get("flag"):
        lines = [f"⚠ {sanitize(formatter._named(event) or 'an agent')} looks {sanitize(event.get('answer'))}: flagged by Laya, nothing was stopped"]
    if event.get("kind") == "question":
        lines = lines + _question_lines(event)
    name = EVENT_STYLES.get(event.get("kind", ""))
    return [style(LEFT_MARGIN + sanitize(line), name, color) if line else line for line in lines]


def load_windows(readings: dict | None, source: str) -> list[dict]:
    if not readings:
        return []
    return [w for w in readings.get("windows", []) if w.get("source") == source]


def _window_text(window: dict) -> str:
    label = KIND_LABELS.get(window.get("kind", ""), window.get("name", "?"))
    percent = window.get("percent")
    if not isinstance(percent, (int, float)):
        return f"{label} ?"
    marker = "~" if window.get("freshness") == "last_known" or window.get("expired") else ""
    return f"{label} {marker}{round(percent)}%"


def source_usage_text(readings: dict | None, source: str) -> str:
    windows = load_windows(readings, source)
    if not windows:
        return "?"
    order = {"5h": 0, "weekly": 1}
    windows.sort(key=lambda w: (order.get(w.get("kind", ""), 2), w.get("name", "")))
    return " · ".join(_window_text(w) for w in windows)


def usage_text(readings: dict | None) -> str:
    """`Claude 5h 51% · wk 33% │ Codex wk 4%`. `?` means unknown; a `~` prefix means the last known value."""
    return f"Claude {source_usage_text(readings, 'claude')} │ Codex {source_usage_text(readings, 'codex')}"


def usage_alerts(readings: dict | None, alert_percent: int, color: bool = True) -> list[str]:
    """Banner lines for fresh windows at or above the alert line (the console shows them at start-up)."""
    lines = []
    for source in ("claude", "codex"):
        for window in load_windows(readings, source):
            percent = window.get("percent")
            if window.get("freshness") != "fresh" or window.get("expired") or not isinstance(percent, (int, float)):
                continue
            if percent >= alert_percent:
                name = KIND_NAMES.get(window.get("kind", ""), window.get("name", "usage"))
                text = f"⚠ {source.capitalize()} {name} at {round(percent)}%. Still working."
                lines.append(style(LEFT_MARGIN + text, "yellow", color))
    return lines


def active_task_ids(run: Run | None) -> list[str]:
    if run is None:
        return []
    return [task.id for task in run.tasks if task.status in ACTIVE_STATUSES]


def open_question_ids(run: Run | None) -> list[str]:
    if run is None:
        return []
    return [q.id for q in run.questions if q.status == "open"]


def is_active(run: Run | None) -> bool:
    return run is not None and run.status not in FINISHED_RUN_STATUSES


def state_text(run: Run | None) -> str:
    if run is None:
        return "idle"
    if run.status == "running":
        active = active_task_ids(run)
        return f"running {sanitize(', '.join(active))}" if active else "running"
    if run.status == "planning":
        return "planning"
    if run.status == "debating":
        return "plan debate"
    if run.status == "awaiting_models":
        return "pick models"
    if run.status == "preparing":
        return "preparing branches"
    if run.status == "integrating":
        return "integration review"
    if run.status == "awaiting_user":
        ids = open_question_ids(run)
        return f"waiting for you ({sanitize(', '.join(ids))})" if ids else "waiting for you"
    if run.status == "paused":
        return f"⏸ paused: {clip(run.pause_reason or 'no reason recorded', PAUSE_CLIP)}"
    return run.status


def mode_line(run: Run | None, mode: str) -> str:
    return LEFT_MARGIN + " · ".join([f"▸▸ {mode}", state_text(run), MODE_HINT, "/help"])


def footer_lines(
    config: Config,
    run: Run | None,
    readings: dict | None,
    repo: Path | str,
    home: Path | str,
    mode: str,
    width: int,
    focus: str | None = None,
) -> list[str]:
    """The two footer lines: model, repo (plus the focus repo in a workspace) and usage on the first; mode, running tasks and hints on the second."""
    current = None
    if run is not None:
        for task in run.tasks:
            if task.status in ACTIVE_STATUSES and task.model and task.effort:
                current = f"{short_model(task.model)} · {task.effort}"
                break
    right = usage_text(readings)
    prefix = f"{LEFT_MARGIN}{current or role_text(default_roles(config)[0])} │ "
    repo_text = home_relative(repo, home)
    focus_text = f" · {sanitize(focus)}" if focus else ""
    room = width - 1 - len(prefix) - len(right) - 2 - len(focus_text)
    if len(repo_text) > room:
        repo_text = "…" + repo_text[-max(room - 1, MIN_PATH_CHARS) :]
    left = prefix + repo_text + focus_text
    gap = max(2, width - 1 - len(left) - len(right))
    return [left + " " * gap + right, mode_line(run, mode)]


def branch_lines(branches: list[dict]) -> list[str]:
    """One line per integration branch: `app  wf/x-0929  3 commits on top of main (1a2b3c4d)`."""
    lines = []
    for entry in branches:
        noun = "commit" if entry.get("commits") == 1 else "commits"
        lines.append(
            LEFT_MARGIN
            + f"{sanitize(entry.get('repo'))}  {sanitize(entry.get('branch'))}  {entry.get('commits', 0)} {noun} "
            + f"on top of {sanitize(entry.get('base_ref'))} ({sanitize(str(entry.get('base_sha', ''))[:8])})"
        )
    return lines


def repos_lines(rows: list[dict]) -> list[str]:
    """What `/repos` prints, from `render_text.repo_rows`: name, base, branch, dirty flag and checks per repo."""
    lines = []
    for row in rows:
        dirty = "?" if row["dirty"] is None else "dirty" if row["dirty"] else "clean"
        base = row["base"] if row["base_configured"] else f"{row['base']} (current)"
        lines.append(
            f"{LEFT_MARGIN}{sanitize(row['name'])}  base {sanitize(base)} · on {sanitize(row['branch'])} · {dirty} · "
            f"{sanitize(row['checks'])}"
        )
    return lines


def status_lines(
    run: Run | None,
    readings: dict | None,
    mode: str,
    workspace: bool = False,
    branches: list[dict] | None = None,
) -> list[str]:
    """What `/status` prints: the run, one line per task (with its repo in a workspace), branches, open questions and usage."""
    if run is None:
        return [f"{LEFT_MARGIN}No run yet. Type a goal to start one.", f"{LEFT_MARGIN}{usage_text(readings)}"]
    lines = [
        f"{LEFT_MARGIN}run {run.id} · {run.status} · mode {run.mode} · {clip(run.goal, render_text.GOAL_MAX_CHARS)}"
    ]
    if run.status == "paused":
        lines.append(f"{LEFT_MARGIN}⏸ {sanitize(run.pause_reason)}")
    for task in run.tasks:
        chosen = sanitize(f"{short_model(task.model)} · {task.effort}") if task.model else "model not chosen"
        deps = sanitize(f" after {', '.join(task.depends_on)}") if task.depends_on else ""
        tag = sanitize(render_text.task_label(task.repo, task.id, workspace))
        lines.append(LEFT_MARGIN + render_text.tag_line(tag, task.status, f"{chosen}  {clip(task.title, 60)}{deps}", 12 if workspace else 8))
    if branches:
        lines.append(f"{LEFT_MARGIN}Integration branches:")
        lines += branch_lines(branches)
    for question in run.questions:
        if question.status == "open":
            lines.append(f"{LEFT_MARGIN}❓ {sanitize(question.id)}: {clip(question.text, 160)}  (/answer {sanitize(question.id)} …)")
    lines.append(f"{LEFT_MARGIN}{usage_text(readings)}")
    return lines


def plan_lines(run: Run, hints: dict[str, str | None], default: Role, workspace: bool = False) -> list[str]:
    """The model-pick screen: every task (as `app/T1` in a workspace) with its dependencies and the Laya size hint."""
    lines = [f"{LEFT_MARGIN}Plan agreed. Pick the coder for each task."]
    for task in run.tasks:
        hint = hints.get(task.id)
        suffix = f" · looks {hint} (hint from Laya)" if hint else ""
        deps = f" (after {', '.join(task.depends_on)})" if task.depends_on else ""
        name = sanitize(render_text.task_label(task.repo, task.id, workspace))
        lines.append(f"{LEFT_MARGIN}{name}: {clip(task.title, 80)}{sanitize(deps)}{sanitize(suffix)}")
    lines.append(
        f"{LEFT_MARGIN}Enter keeps {role_text(default)}; or type a model, or a model and an effort."
    )
    return lines


HELP_LINES = (
    "Type a goal to start a run. While a run is active, plain text is added to decisions.md as a note.",
    "@claude <question> / @codex <question>   ask a read-only side question without disturbing the team",
    "/run <goal>                start a run",
    "/status                    the run, its tasks and open questions",
    "/answer <Q> <text>         answer a question",
    "/pause  /resume            stop launching new work / continue",
    "/models                    roles and per-task models",
    "/effort <role|task> <lvl>  change an effort level",
    "/log [task]                recent events",
    "/repos                     the workspace's repos: base, branch, dirty, checks",
    "/publish [repo]            push the run's integration branches and open PRs (asks first)",
    "/computer on|off <task>    computer use for a task",
    "/help  /quit",
    "shift+tab                  toggle auto ⇄ step (step asks before each task)",
)


def help_lines() -> list[str]:
    return [LEFT_MARGIN + line for line in HELP_LINES]
