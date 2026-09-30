"""Plain rich-based text output for the non-console commands."""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from typing import Any, Iterable

from rich.console import Console
from rich.table import Table

from workforce import checks as checks_mod
from workforce import git_ops
from workforce.config import Config
from workforce.errors import GitError
from workforce.state import Question, Run
from workforce.workspace import Workspace

TAG_WIDTH = 8
LABEL_WIDTH = 8
WIDE_CONSOLE_COLUMNS = 120
WORKSPACE_TAG_WIDTH = 12
SHA_CHARS = 7
POSITION_MAX_CHARS = 1200
CHECK_CMD_MAX_CHARS = 48
GOAL_MAX_CHARS = 120
STEP_LABELS = {
    "plan": "plan",
    "plan_review": "plan-rv",
    "plan_revise": "plan-rv",
    "code": "coding",
    "fix": "fixing",
    "review": "review",
    "reconcile": "review",
    "commit": "commit",
}
ANNOUNCED_BY_AGENT_EVENTS = frozenset({"coding", "fixing", "reviewing", "committing", "merged"})
ROLE_LABELS = {
    "planner": "plan",
    "plan_reviewer": "plan-rv",
    "coder": "coding",
    "reviewer_claude": "review",
    "reviewer_codex": "review",
    "committer": "commit",
    "usage_summary": "usage",
}


_ESCAPE_SEQUENCES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_].*?(?:\x1b\\|$)"
    r"|\x1b\[[0-?]*[ -/]*[@-~]?"
    r"|\x1b[ -/]*[0-~]?"
    r"|\x9b[0-?]*[ -/]*[@-~]?",
    re.DOTALL,
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize(text: object) -> str:
    """Drop terminal control sequences (ESC, CSI, OSC, DCS) and other control characters, keeping newline and tab.

    Use it on anything that came from a model or a repo before it is printed.
    """
    return _CONTROL_CHARS.sub("", _ESCAPE_SEQUENCES.sub("", str(text)))


def make_console() -> Console:
    """A console that never wraps to 80 columns when its output is not a terminal."""
    width = None if sys.stdout.isatty() else WIDE_CONSOLE_COLUMNS
    return Console(highlight=False, width=width)


def short_model(model: str | None) -> str:
    if not model:
        return "?"
    return model[len("claude-") :] if model.startswith("claude-") else model


def clip(text: str, limit: int) -> str:
    text = " ".join(sanitize(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def clock_time(value: str | None) -> str:
    stamp = _parse_ts(value)
    return time.strftime("%H:%M:%S", time.localtime(stamp)) if stamp is not None else "--:--:--"


def tag_line(tag: str, label: str, text: str = "", width: int = TAG_WIDTH) -> str:
    head = f"[{tag}]"
    return (head.ljust(max(width, len(head) + 1)) + label.ljust(LABEL_WIDTH) + text).rstrip()


def task_label(repo: str | None, task_id: str | None, workspace: bool) -> str | None:
    """`app/T1` in workspace mode when the repo is known, else the bare task id (`T1`)."""
    if workspace and repo and task_id:
        return f"{repo}/{task_id}"
    return task_id


def normalize_task_name(text: str) -> str:
    """`t1` -> `T1`, `app/t1` -> `app/T1` (repo names keep their case)."""
    repo, _, task = text.strip().rpartition("/")
    return f"{repo}/{task.upper()}" if repo else task.upper()


def event_matches_task(event: dict[str, Any], name: str) -> bool:
    """True if `event` belongs to task `name`, given as `T1` or as `app/T1`."""
    task = event.get("task")
    return bool(task) and name in (task, f"{event.get('repo')}/{task}")


class EventFormatter:
    """Turns events into the one-line formats of the live view; keeps start times for elapsed values."""

    def __init__(self, workspace: bool = False) -> None:
        self._starts: dict[tuple, float] = {}
        self.workspace = workspace
        self.tag_width = WORKSPACE_TAG_WIDTH if workspace else TAG_WIDTH

    def _line(self, tag: str, label: str, text: str = "") -> str:
        return tag_line(tag, label, text, self.tag_width)

    def _named(self, e: dict[str, Any]) -> str | None:
        return task_label(e.get("repo"), e.get("task"), self.workspace)

    def format(self, event: dict[str, Any]) -> list[str]:
        """Zero or more display lines for one event (some events only repeat what another already said)."""
        kind = event.get("kind", "?")
        handler = getattr(self, f"_{kind}", None)
        if handler is None:
            return [f"[{sanitize(kind)}] " + json.dumps({k: v for k, v in event.items() if k not in ("ts", "kind")}, default=str)]
        return [sanitize(line) for line in handler(event)]

    def _tag(self, event: dict[str, Any], default: str = "run") -> str:
        return self._named(event) or default

    def _run_started(self, e: dict) -> list[str]:
        return [self._line("run", "started", f"{e.get('run', '')} · {e.get('mode', '')} · {clip(e.get('goal', ''), GOAL_MAX_CHARS)}")]

    def _plan_ready(self, e: dict) -> list[str]:
        return [self._line("plan", "✓", f"plan.md: {e.get('tasks', '?')} tasks")]

    def _debate_round(self, e: dict) -> list[str]:
        prefix = f"round {e.get('round', '?')}"
        if e.get("speaker") == "plan_reviewer":
            if e.get("agree"):
                return [self._line("plan", "debate", f"{prefix}: plan reviewer agrees")]
            objections = e.get("objections") or []
            return [self._line("plan", "debate", f"{prefix}: plan reviewer objects ({len(objections)})")]
        remaining = e.get("remaining") or []
        return [self._line("plan", "debate", f"{prefix}: planner replied, {len(remaining)} still disputed")]

    def _question(self, e: dict) -> list[str]:
        where = f" ({self._named(e)})" if e.get("task") else ""
        return [f"❓ {e.get('question')}{where}: {clip(e.get('text', ''), 300)}"]

    def _answered(self, e: dict) -> list[str]:
        return [self._line(e.get("question", "Q"), "answered", clip(e.get("answer", ""), 200))]

    def _task_status(self, e: dict) -> list[str]:
        if e.get("status") in ANNOUNCED_BY_AGENT_EVENTS:
            return []
        return [self._line(self._tag(e), "→", e.get("status", "?"))]

    def _agent_event(self, e: dict) -> list[str]:
        key = (e.get("task"), e.get("role"), e.get("step"))
        label = ROLE_LABELS.get(e.get("role", ""), e.get("role", "agent"))
        tag = self._tag(e, "plan")
        stamp = _parse_ts(e.get("ts"))
        if e.get("phase") == "start":
            if stamp is not None:
                self._starts[key] = stamp
            return [self._line(tag, label, short_model(e.get("model")))]
        started = self._starts.pop(key, None)
        elapsed = f" {duration(stamp - started)}" if stamp is not None and started is not None else ""
        if e.get("ok"):
            return [self._line(tag, label, f"✓{elapsed}")]
        return [self._line(tag, label, f"✗ failed ({e.get('error_kind') or 'error'}){elapsed}")]

    def _check_result(self, e: dict) -> list[str]:
        tag = self._tag(e)
        if e.get("no_checks"):
            return [self._line(tag, "checks", "no checks detected")]
        return [self._line(tag, "checks", "✓ passed" if e.get("ok") else "✗ failed")]

    def _review(self, e: dict) -> list[str]:
        findings = e.get("findings") or 0
        sha = (e.get("sha") or "")[:SHA_CHARS]
        return [
            self._line(
                self._tag(e),
                "review",
                f"{short_model(e.get('model'))} → {e.get('verdict', '?')} @ {sha} ({findings} findings)",
            )
        ]

    def _commit_waiting(self, e: dict) -> list[str]:
        return [self._line(self._tag(e), "commit", e.get("message", ""))]

    def _committed(self, e: dict) -> list[str]:
        return [self._line(self._tag(e), "commit", f"✓ {(e.get('sha') or '')[:SHA_CHARS]}")]

    def _merged(self, e: dict) -> list[str]:
        target = e.get("into") or e.get("branch", "")
        return [self._line(self._tag(e), "merged", f"{(e.get('sha') or '')[:SHA_CHARS]} into {target}")]

    def _alert(self, e: dict) -> list[str]:
        return [f"⚠ {e.get('message') or 'usage alert'}. Still working."]

    def _paused(self, e: dict) -> list[str]:
        return [f"⏸ paused: {e.get('reason', '')}"]

    def _resumed(self, e: dict) -> list[str]:
        return ["▶ Resumed"]

    def _decision(self, e: dict) -> list[str]:
        if e.get("key") == "risk_gate":
            return self._risk_gate(e)
        if e.get("key") == "agent_progress" and e.get("flag"):
            return self._agent_progress(e)
        confidence = e.get("confidence")
        conf = f" ({confidence:.2f})" if isinstance(confidence, (int, float)) else ""
        return [self._line("laya", str(e.get("key", "?")), f"{e.get('answer')}{conf} · {e.get('source', '?')}")]

    def _risk_gate(self, e: dict) -> list[str]:
        if e.get("allowed") is not False:
            return []
        where = f" [{self._named(e)}]" if e.get("task") else ""
        return [f"⛔{where} blocked: {clip(e.get('summary') or e.get('tool') or 'a tool call', 200)} ({e.get('reason', 'no reason given')})"]

    def _agent_progress(self, e: dict) -> list[str]:
        task = self._named(e) or "?"
        state = "off task" if e.get("answer") == "off_task" else "stuck"
        confidence = e.get("confidence")
        conf = f" (conf {confidence:.2f})" if isinstance(confidence, (int, float)) else ""
        return [f"⚠ [{task}] Laya thinks the coder may be {state}{conf}. Check with: workforce log {task}"]

    def _note(self, e: dict) -> list[str]:
        text = e.get("text") or e.get("message") or ""
        return [self._line(self._tag(e, "note"), "note", text)]

    def _error(self, e: dict) -> list[str]:
        return [f"✗ {self._named(e) + ': ' if e.get('task') else ''}{e.get('message', '')}"]

    def _run_done(self, e: dict) -> list[str]:
        lines = [f"✓ run done: {e.get('tasks', '?')} tasks merged"]
        lines += [f"    {item}" for item in e.get("summary") or []]
        return lines


def line_style(line: str) -> str | None:
    """Colour for a live-view line: red for failures and blocked calls, yellow for warnings and pauses."""
    if line.startswith(("✗", "⛔")):
        return "red"
    if line.startswith(("⚠", "⏸")):
        return "yellow"
    return None


def format_clock(unix_seconds: float) -> str:
    return time.strftime("%H:%M", time.localtime(unix_seconds))


def format_reset(unix_seconds: float) -> str:
    """`Sat 30 Sep 14:20`, local time."""
    return time.strftime("%a %d %b %H:%M", time.localtime(unix_seconds))


def format_wait(seconds: float) -> str:
    """`1h41m`, or `41m` under an hour."""
    total_minutes = max(0, int(seconds // 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def print_lines(console: Console, lines: Iterable[str], style: str | None = None) -> None:
    for line in lines:
        line = sanitize(line)
        console.print(line, markup=False, soft_wrap=True, style=style or line_style(line))


def check_line(ok: bool, text: str) -> str:
    return f"{'✓' if ok else '✗'} {text}"


def print_check(console: Console, ok: bool, text: str) -> None:
    console.print(check_line(ok, sanitize(text)), markup=False, soft_wrap=True, style="green" if ok else "red")


def tasks_table(run: Run, workspace: bool = False) -> Table:
    """One row per task; in workspace mode a `repo` column names the repo each task belongs to."""
    table = Table(box=None, pad_edge=False, header_style="bold")
    columns = ("id", "repo", "title", "status", "model · effort", "review") if workspace else (
        "id", "title", "status", "model · effort", "review"
    )
    for column in columns:
        table.add_column(column, overflow="fold")
    for task in run.tasks:
        model = f"{short_model(task.model)} · {task.effort}" if task.model else "-"
        cells = [sanitize(task.id)]
        if workspace:
            cells.append(sanitize(task.repo or "-"))
        cells += [sanitize(task.title), task.status, sanitize(model), str(task.review_round)]
        table.add_row(*cells)
    return table


def plan_table(run: Run, workspace: bool = False) -> Table:
    table = Table(box=None, pad_edge=False, header_style="bold")
    columns = ("id", "repo", "title", "depends on") if workspace else ("id", "title", "depends on")
    for column in columns:
        table.add_column(column, overflow="fold")
    for task in run.tasks:
        cells = [sanitize(task.id)]
        if workspace:
            cells.append(sanitize(task.repo or "-"))
        cells += [sanitize(task.title), sanitize(", ".join(task.depends_on)) or "-"]
        table.add_row(*cells)
    return table


def checks_text(detected: dict[str, list[str]]) -> str:
    """`test: pytest -q; lint: npm run lint`, the first command per kind, or `none detected`."""
    parts = [f"{kind}: {clip(cmds[0], CHECK_CMD_MAX_CHARS)}" for kind, cmds in detected.items() if cmds]
    return "; ".join(parts) if parts else "none detected"


def repo_rows(ws: Workspace) -> list[dict[str, Any]]:
    """Facts about each workspace repo: base (configured or current branch), current branch, dirty flag, checks."""
    rows = []
    for info in ws.repos.values():
        try:
            branch = git_ops.current_branch(info.path)
        except GitError:
            branch = "(detached)"
        try:
            dirty: bool | None = not git_ops.is_clean(info.path)
        except GitError:
            dirty = None
        configured = {kind: getattr(info.checks, kind) for kind in checks_mod.KINDS}
        detected = checks_mod.detect(info.path, configured, env_root=info.path)
        rows.append(
            {
                "name": info.name,
                "path": info.path,
                "base": info.base or branch,
                "base_configured": info.base is not None,
                "branch": branch,
                "dirty": dirty,
                "checks": checks_text(detected),
            }
        )
    return rows


def repos_table(ws: Workspace) -> Table:
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("repo", "base", "branch", "dirty", "checks"):
        table.add_column(column, overflow="fold")
    for row in repo_rows(ws):
        dirty = "?" if row["dirty"] is None else "yes" if row["dirty"] else "no"
        base = row["base"] if row["base_configured"] else f"{row['base']} (current)"
        table.add_row(sanitize(row["name"]), sanitize(base), sanitize(row["branch"]), dirty, sanitize(row["checks"]))
    return table


def branches_table(branches: list[dict]) -> Table:
    """The integration branches of a run: repo, branch, base and how many commits sit on top of it."""
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("repo", "branch", "base", "commits"):
        table.add_column(column, overflow="fold")
    for entry in branches:
        base = f"{entry.get('base_ref', '?')} ({str(entry.get('base_sha', ''))[:8]})"
        table.add_row(sanitize(entry.get("repo")), sanitize(entry.get("branch")), sanitize(base), str(entry.get("commits", 0)))
    return table


def publish_table(rows: list) -> Table:
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("repo", "branch", "pushed", "PR / error"):
        table.add_column(column, overflow="fold")
    for row in rows:
        pushed = "yes" if row.pushed else "no"
        detail = row.pr_url or row.error or row.status
        if row.pr_url and row.error:
            detail = f"{row.pr_url} ({row.error})"
        table.add_row(sanitize(row.repo), sanitize(row.branch), pushed, sanitize(detail))
    return table


def print_question(console: Console, question: Question) -> None:
    where = f" ({question.task_id})" if question.task_id else ""
    console.print(f"❓ {question.id}{where}: {sanitize(question.text)}", markup=False, soft_wrap=True, style="bold")
    for agent, position in question.positions.items():
        console.print(f"  {sanitize(agent)}:", markup=False, style="cyan")
        for line in sanitize(position)[:POSITION_MAX_CHARS].splitlines():
            console.print(f"    {line}", markup=False, soft_wrap=True)


def print_status(
    console: Console, run: Run | None, workspace: bool = False, branches: list[dict] | None = None
) -> None:
    if run is None:
        console.print('No run yet. Start one with: workforce run "<goal>"', markup=False)
        return
    console.print(f"Run {run.id} · {run.status} · {run.mode} mode", markup=False, style="bold")
    console.print(f"Goal: {sanitize(run.goal)}", markup=False, soft_wrap=True)
    if run.pause_reason:
        console.print(f"Paused: {sanitize(run.pause_reason)}", markup=False, soft_wrap=True, style="yellow")
    if run.tasks:
        console.print()
        console.print(tasks_table(run, workspace))
    if branches:
        console.print()
        console.print("Integration branches", markup=False, style="bold")
        console.print(branches_table(branches))
    open_questions = [q for q in run.questions if q.status == "open"]
    if open_questions:
        console.print()
        for question in open_questions:
            print_question(console, question)
        first = open_questions[0].id
        console.print(f'Answer with: workforce answer {first} "<text>"', markup=False, style="dim")


def print_questions(console: Console, run: Run | None, include_answered: bool) -> None:
    if run is None:
        console.print("No run yet.", markup=False)
        return
    shown = [q for q in run.questions if include_answered or q.status == "open"]
    if not shown:
        console.print("No open questions.", markup=False)
        return
    for question in shown:
        print_question(console, question)
        if question.status == "answered":
            console.print(f"  answered: {sanitize(question.answer)}", markup=False, soft_wrap=True, style="green")


def print_roles(console: Console, config: Config) -> None:
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("role", "agent", "model", "effort"):
        table.add_column(column)
    for name, role in config.roles.items():
        table.add_row(sanitize(name), role.agent, sanitize(role.model), role.effort)
    console.print(table)


def _when(value: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value)) if isinstance(value, (int, float)) else "-"


def print_usage(console: Console, readings: dict | None, summary: str | None) -> None:
    if readings is None and summary is None:
        console.print("no usage readings yet", markup=False)
        return
    if readings is not None:
        sources = readings.get("sources") or {}
        windows = readings.get("windows") or []
        table = Table(box=None, pad_edge=False, header_style="bold")
        for column in ("source", "window", "used", "state", "read at", "resets at"):
            table.add_column(column)
        for window in windows:
            table.add_row(
                sanitize(window.get("source")),
                sanitize(window.get("name")),
                f"{window.get('percent'):g}%" if isinstance(window.get("percent"), (int, float)) else "-",
                sanitize(window.get("freshness", "unknown")) + (" (expired)" if window.get("expired") else ""),
                _when(window.get("read_at")),
                _when(window.get("resets_at")),
            )
        for name, info in sources.items():
            if not any(w.get("source") == name for w in windows):
                table.add_row(sanitize(name), "-", "-", sanitize(info.get("freshness", "unknown")), _when(info.get("read_at")), "-")
        console.print(table)
        thresholds = readings.get("thresholds") or {}
        if thresholds:
            console.print(
                f"alert at {thresholds.get('alert_percent')}% · pause at {thresholds.get('pause_percent')}%",
                markup=False,
                style="dim",
            )
        for name, info in sources.items():
            if info.get("error"):
                console.print(f"{sanitize(name)}: {sanitize(info['error'])}", markup=False, soft_wrap=True, style="yellow")
    if summary:
        console.print()
        console.print(sanitize(summary).strip(), markup=False, soft_wrap=True)
