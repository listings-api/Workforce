"""The interactive console: `workforce` / `wf` with no arguments.

The UI thread only reads and writes through `StateStore` / `EventLog` and the orchestrator's public methods;
the team itself runs in a `Driver` thread, and events reach the screen from a timer that tails `events.jsonl`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app_or_none
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from workforce import __version__, git_ops, prompts, render_text
from workforce import config as config_mod
from workforce import publish as publish_mod
from workforce.agents import guard
from workforce.agents.base import AgentRequest
from workforce.cli import (
    Context,
    build_orchestrator,
    close_orchestrator,
    parse_model_choice,
    pause_interrupted,
    set_coder,
    shutdown_guard,
)
from workforce.config import EFFORTS, ROLE_NAMES
from workforce.console import render
from workforce.console.logo import supports_truecolor
from workforce.console.render import PreflightSummary
from workforce.errors import ConfigError, GitError, WorkforceError
from workforce.events import EventLog
from workforce.orchestrator import Orchestrator
from workforce.render_text import EventFormatter, sanitize
from workforce.scheduler import Scheduler
from workforce.state import Run
from workforce.workspace import Workspace

HISTORY_NAME = ".workforce_history"
PLACEHOLDER = 'Try "add CSV export to the reports page" or @codex / @claude …'
COMMAND_NAMES = (
    "run",
    "status",
    "answer",
    "pause",
    "resume",
    "models",
    "effort",
    "log",
    "repos",
    "publish",
    "computer",
    "help",
    "quit",
)
SIDE_AGENTS = ("claude", "codex")
SIDE_ROLES = {"claude": "plan_reviewer", "codex": "planner"}
TICK_S = 0.5
SLOW_LOOP_S = 1.0
PROGRESS_INTERVAL_S = 180
LOG_TAIL = 30
JOIN_POLL_S = 0.5
QUIT_CHOICES = {"p": "pause", "pause": "pause", "l": "leave", "leave": "leave", "c": "cancel", "cancel": "cancel"}
QUIT_PAUSE_REASON = "paused by the user when closing the console"
QUIT_LEAVE_REASON = "console closed with the team running; there is no background daemon in v3, so it paused"
NOTE_PREFIX = "- User note (console): "


@dataclass(frozen=True)
class Action:
    """One parsed line of input. `kind` is empty, goal, note, side, command or unknown."""

    kind: str
    name: str = ""
    args: str = ""


def parse_input(text: str, run_active: bool) -> Action:
    """Route a typed line: `/command`, `@agent question`, or plain text (a goal when idle, else a note)."""
    line = text.strip()
    if not line:
        return Action("empty")
    if line.startswith("/"):
        name, _, rest = line[1:].partition(" ")
        name = name.lower()
        return Action("command" if name in COMMAND_NAMES else "unknown", name, rest.strip())
    if line.startswith("@"):
        name, _, rest = line[1:].partition(" ")
        name = name.lower()
        if name in SIDE_AGENTS:
            return Action("side", name, rest.strip())
        return Action("unknown", name, rest.strip())
    return Action("note" if run_active else "goal", "", line)


@dataclass(frozen=True)
class CompletionContext:
    task_ids: tuple[str, ...] = ()
    question_ids: tuple[str, ...] = ()
    roles: tuple[str, ...] = ROLE_NAMES
    repo_names: tuple[str, ...] = ()
    efforts: dict[str, tuple[str, ...]] = field(default_factory=dict)


def completions(text: str, ctx: CompletionContext) -> tuple[list[str], str]:
    """Candidates for the word being typed and that word (so the caller knows how much to replace)."""
    if " " not in text:
        if text.startswith("/"):
            return [c for c in (f"/{n}" for n in COMMAND_NAMES) if c.startswith(text.lower())], text
        if text.startswith("@"):
            return [c for c in (f"@{a}" for a in SIDE_AGENTS) if c.startswith(text.lower())], text
        return [], text
    if not text.startswith("/"):
        return [], ""
    tokens = text.split(" ")
    command, fragment, position = tokens[0][1:].lower(), tokens[-1], len(tokens) - 2
    options: list[str] = []
    if command == "answer" and position == 0:
        options = list(ctx.question_ids)
    elif command == "log" and position == 0:
        options = list(ctx.task_ids)
    elif command == "publish" and position == 0:
        options = list(ctx.repo_names)
    elif command == "computer":
        options = ["on", "off"] if position == 0 else list(ctx.task_ids) if position == 1 else []
    elif command == "effort":
        if position == 0:
            options = list(ctx.roles) + list(ctx.task_ids)
        elif position == 1:
            options = list(ctx.efforts.get(tokens[1], ()))
    return [o for o in options if o.lower().startswith(fragment.lower())], fragment


class WorkforceCompleter(Completer):
    """Tab completion for `/commands`, `@agents`, task ids and question ids."""

    def __init__(self, context_source: Callable[[], CompletionContext]):
        self._context_source = context_source

    def get_completions(self, document, complete_event):
        candidates, fragment = completions(document.text_before_cursor, self._context_source())
        for candidate in candidates:
            yield Completion(candidate, start_position=-len(fragment))


class Driver:
    """Runs the scheduler in a background thread; at most one at a time."""

    def __init__(self, make_scheduler: Callable[[], Scheduler], report: Callable[[str], None]):
        self._make_scheduler = make_scheduler
        self._report = report
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Start the team unless it is already running; True if a new thread was started."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=self._run, name="wf-driver", daemon=True)
            self._thread.start()
            return True

    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        try:
            self._make_scheduler().run_until_blocked()
        except WorkforceError as exc:
            self._report(f"✗ {exc}")
        except Exception as exc:
            self._report(f"✗ the team stopped on an unexpected error: {type(exc).__name__}: {exc}")


@dataclass
class PickState:
    """The model-pick stop: which task is being asked about and what has been chosen so far."""

    tasks: list[str]
    index: int = 0
    chosen: dict[str, tuple[str, str]] = field(default_factory=dict)
    branches: list[tuple[str, str]] = field(default_factory=list)
    branch_index: int = 0


@dataclass
class QuitState:
    """Waiting for the user to say what to do with a running team."""


@dataclass
class PublishState:
    """Waiting for the user to confirm pushing these integration branches and opening PRs."""

    repo: str | None
    entries: list[dict]


def _default_print(line: str) -> None:
    print(line, flush=True)


def run_preflight(
    config: config_mod.Config, repo: Path | list[Path], environ: dict[str, str] | None = None
) -> PreflightSummary:
    """The start-up checks behind the header's subscription line; `repo` is a repo path or the workspace's repo paths."""
    env = os.environ if environ is None else environ
    env_problems = guard.check_env(env)
    claude_problems = guard.check_claude(Path(config.bin.claude))
    codex_problems = guard.check_codex(Path(config.bin.codex))
    repos = repo if isinstance(repo, list) else [repo]
    repo_problems = [problem for path in repos for problem in guard.check_repo(path)]
    return PreflightSummary(
        claude_ok=not claude_problems,
        codex_ok=not codex_problems,
        keys_ok=not env_problems,
        problems=claude_problems + codex_problems + env_problems + repo_problems,
    )


class ConsoleController:
    """Everything the console does that is not drawing: input handling, the event pump and the team driver."""

    def __init__(
        self,
        orch: Orchestrator,
        preflight: PreflightSummary,
        *,
        printer: Callable[[str], None] | None = None,
        truecolor: bool | None = None,
        version: str = __version__,
        color: bool = True,
    ):
        self.orch = orch
        self.preflight = preflight
        self.workspace: Workspace = orch.workspace
        self.paths = orch.paths
        self.store = orch.store
        self.events: EventLog = orch.events
        self.printer = printer or _default_print
        self.truecolor = supports_truecolor() if truecolor is None else truecolor
        self.version = version
        self.color = color
        self.formatter = EventFormatter(self.multi_repo)
        self.driver = Driver(self._make_scheduler, lambda text: self.say([render.LEFT_MARGIN + render.style(sanitize(text), "red", self.color)]))
        self.mode = "auto"
        self.modal: PickState | QuitState | PublishState | None = None
        self.done = False
        self.offset = self._events_size()
        self.side_threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        self._eof_count = 0

    @property
    def config(self) -> config_mod.Config:
        return self.orch.config

    @property
    def multi_repo(self) -> bool:
        return not self.workspace.single

    @property
    def focus(self) -> str | None:
        return self.workspace.focus if self.multi_repo else None

    def say(self, lines: list[str]) -> None:
        for line in lines:
            self.printer(line)

    def _events_size(self) -> int:
        try:
            return self.paths.events.stat().st_size
        except FileNotFoundError:
            return 0

    def _make_scheduler(self) -> Scheduler:
        return Scheduler(self.orch, self.orch.config.limits.parallel)

    def load_readings(self) -> dict | None:
        try:
            return json.loads(self.paths.usage_json.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def current_mode(self) -> str:
        run = self.store.load()
        return run.mode if render.is_active(run) else self.mode

    def show_header(self) -> None:
        lines = render.header_lines(
            self.version,
            self.config,
            self.preflight,
            self.paths.repo,
            self.paths.home,
            self.truecolor,
            self.color,
            len(self.workspace.repos) if self.multi_repo else None,
        )
        readings = self.load_readings()
        lines += [""] + render.usage_alerts(readings, self.config.limits.alert_percent, self.color)
        lines += self._startup_state_lines() + [""]
        self.say(lines)

    def _startup_state_lines(self) -> list[str]:
        run = self.store.load()
        margin = render.LEFT_MARGIN
        if run is None:
            return []
        if run.status in render.FINISHED_RUN_STATUSES:
            return [f"{margin}Last run {run.id} is {run.status}. Type a goal to start another."]
        lines = [f"{margin}Run {run.id} is {run.status}: {render_text.clip(run.goal, render_text.GOAL_MAX_CHARS)}"]
        open_ids = render.open_question_ids(run)
        if open_ids:
            noun = "question" if len(open_ids) == 1 else "questions"
            lines.append(
                render.style(
                    f"{margin}❓ {len(open_ids)} {noun} waiting ({', '.join(open_ids)}), type /answer {open_ids[0]} …",
                    "yellow",
                    self.color,
                )
            )
        if run.status == "paused":
            lines.append(f"{margin}⏸ paused: {sanitize(run.pause_reason)}. Type /resume to continue.")
        elif run.status in ("running", "planning", "debating"):
            lines.append(f"{margin}No team is attached (there is no background daemon in v3). Type /resume to continue.")
        return lines

    def completion_context(self) -> CompletionContext:
        run = self.store.load()
        coder_agent = self.config.role("coder").agent
        efforts: dict[str, tuple[str, ...]] = {name: tuple(EFFORTS[self.config.role(name).agent]) for name in ROLE_NAMES}
        if run is not None:
            for task in run.tasks:
                efforts[task.id] = tuple(EFFORTS[task.agent or coder_agent])
        return CompletionContext(
            task_ids=tuple(t.id for t in run.tasks) if run else (),
            question_ids=tuple(render.open_question_ids(run)),
            roles=ROLE_NAMES,
            repo_names=tuple(self.workspace.repos),
            efforts=efforts,
        )

    def prompt_label(self) -> str:
        modal = self.modal
        if isinstance(modal, PickState) and modal.index < len(modal.tasks):
            coder = self.config.role("coder")
            return f"coder {self._task_label(modal.tasks[modal.index])} [{render.role_text(coder)}] ❯ "
        if isinstance(modal, PickState) and modal.branch_index < len(modal.branches):
            repo_name, default = modal.branches[modal.branch_index]
            return f"branch for {repo_name} [{default}] ❯ "
        if isinstance(modal, QuitState):
            return "pause / leave / cancel [p/l/c] ❯ "
        if isinstance(modal, PublishState):
            return "publish? [y/N] ❯ "
        return "❯ "

    def _task_label(self, task_id: str) -> str:
        run = self.store.load()
        task = next((t for t in run.tasks if t.id == task_id), None) if run else None
        return render_text.task_label(task.repo if task else None, task_id, self.multi_repo) or task_id

    def toolbar_lines(self, width: int) -> list[str]:
        run = self.store.load()
        footer = render.footer_lines(
            self.config, run, self.load_readings(), self.paths.repo, self.paths.home, self.current_mode(), width, self.focus
        )
        return [render.rule(width)] + footer

    def toggle_mode(self) -> str:
        """Shift+Tab: switch between auto and step; returns the new mode."""
        new = "step" if self.current_mode() == "auto" else "auto"
        self.mode = new
        if render.is_active(self.store.load()):
            self.orch.set_mode(new)
        return new

    def tick(self) -> None:
        """One timer beat: print new events, then react to state changes (the model-pick stop)."""
        events, self.offset = self.events.read(self.offset)
        lines: list[str] = []
        for event in events:
            lines += render.event_lines(self.formatter, event, self.color)
        if lines:
            self.say(lines)
        self._sync_state()

    def _sync_state(self) -> None:
        run = self.store.load()
        with self._lock:
            if isinstance(self.modal, PickState) and (run is None or run.status != "awaiting_models"):
                self.modal = None
        if run is not None and run.status == "awaiting_models" and self.modal is None and not self.driver.alive():
            self.enter_model_pick(run)

    def enter_model_pick(self, run: Run) -> None:
        try:
            hints = self.orch.task_size_hints()
        except Exception:
            hints = {}
        lines = render.plan_lines(run, hints, self.config.role("coder"), self.multi_repo)
        with self._lock:
            if self.modal is not None:
                return
            self.modal = PickState(tasks=[task.id for task in run.tasks], branches=list(self.orch.proposed_branches().items()))
        self.say(lines)

    def handle_line(self, text: str) -> None:
        """Handle one submitted line. Errors are printed, never raised."""
        try:
            self._dispatch(text)
        except WorkforceError as exc:
            self.say([render.style(f"{render.LEFT_MARGIN}✗ {sanitize(exc)}", "red", self.color)])

    def _dispatch(self, text: str) -> None:
        line = text.strip()
        is_command = line.startswith("/") or line.startswith("@")
        if not is_command:
            if isinstance(self.modal, QuitState):
                return self._quit_answer(line)
            if isinstance(self.modal, PickState):
                return self._pick_answer(line)
            if isinstance(self.modal, PublishState):
                return self._publish_answer(line)
        run = self.store.load()
        action = parse_input(text, render.is_active(run))
        if action.kind == "empty":
            return
        if action.kind == "unknown":
            self.say([f"{render.LEFT_MARGIN}Unknown command: {line.split()[0]}. Type /help."])
        elif action.kind == "goal":
            self.start_goal(action.args)
        elif action.kind == "note":
            self.add_note(action.args)
        elif action.kind == "side":
            self.ask_side(action.name, action.args)
        else:
            getattr(self, f"cmd_{action.name}")(action.args)

    def start_goal(self, goal: str) -> None:
        run = self.store.load()
        if render.is_active(run):
            raise WorkforceError(
                f"run {run.id} is {run.status}; plain text is added as a note while a run is active. "
                "Pause or finish it before starting a new goal"
            )
        self.orch.start(goal, self.mode)
        self.driver.start()

    def add_note(self, text: str) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        with self.paths.decisions_md.open("a", encoding="utf-8") as handle:
            handle.write(f"{NOTE_PREFIX}{text}\n")
        self.orch.events.emit("note", text=f"added to decisions.md: {render_text.clip(text, 120)}")

    def ask_side(self, agent: str, question: str) -> None:
        margin = render.LEFT_MARGIN
        if not question:
            self.say([f"{margin}Usage: @{agent} <question>"])
            return
        reason = self.orch.usage_gate()
        if reason:
            self.say([render.style(f"{margin}⏸ not asking @{agent} while usage is paused: {sanitize(reason)}", "yellow", self.color)])
            return
        thread = threading.Thread(target=self._side_worker, args=(agent, question), name=f"wf-side-{agent}", daemon=True)
        self.side_threads = [t for t in self.side_threads if t.is_alive()] + [thread]
        thread.start()
        self.say([f"{margin}[@{agent}] thinking…"])

    def _side_worker(self, agent: str, question: str) -> None:
        role = self.config.role(SIDE_ROLES[agent])
        request = AgentRequest(
            role="side_question",
            agent=role.agent,
            model=role.model,
            effort=role.effort,
            prompt=prompts.render(
                "side_question", agent=agent, decisions=self.orch.decisions(), question=question
            ),
            cwd=self.paths.repo,
            sandbox="read-only",
            browser=False,
            repo=self.paths.repo,
            add_dirs=[info.path for info in self.workspace.repos.values()] if self.multi_repo and role.agent == "claude" else [],
            skip_git_check=self.multi_repo and role.agent == "codex",
        )
        margin = render.LEFT_MARGIN
        heads_before = self._heads()
        try:
            result = self.orch.runners[role.agent].run(request)
            self._record_usage(role.agent, result)
        except Exception as exc:
            self._check_heads_unmoved(agent, heads_before)
            self.say([render.style(f"{margin}[@{agent}] failed: {sanitize(exc)}", "red", self.color)])
            return
        self._check_heads_unmoved(agent, heads_before)
        if not result.ok:
            detail = sanitize(result.error or "no detail")
            self.say([render.style(f"{margin}[@{agent}] failed ({result.error_kind or 'error'}): {detail}", "red", self.color)])
            return
        body = [f"{margin}  {line}" if line else "" for line in sanitize(result.text).strip().splitlines()]
        self.say([f"{margin}[@{agent}] {render.role_text(role)}"] + body)

    def _heads(self) -> dict[str, str]:
        heads: dict[str, str] = {}
        for name, info in self.workspace.repos.items():
            try:
                heads[name] = git_ops.head_sha(info.path)
            except GitError:
                continue
        return heads

    def _check_heads_unmoved(self, agent: str, before: dict[str, str]) -> None:
        """A side question is read-only; if any repo's HEAD moved during it, say so loudly and log an error event."""
        after = self._heads()
        for name, sha in before.items():
            if after.get(name, sha) == sha:
                continue
            where = f" in {name}" if self.multi_repo else ""
            message = (
                f"[@{agent}] moved HEAD{where} from {sha[:7]} to {after[name][:7]} during a read-only side question; "
                "only the Claude committer may commit. Check `git log` and undo it if it was not you"
            )
            self.orch.events.emit("error", source="side_question", message=message)
            self.say([render.style(f"{render.LEFT_MARGIN}⚠ {message}", "red", self.color)])

    def _record_usage(self, agent: str, result) -> None:
        monitor = self.orch.usage_monitor
        if monitor is None:
            return
        if agent == "claude":
            monitor.ingest_claude(result)
        else:
            monitor.refresh_codex()

    def cmd_help(self, args: str) -> None:
        self.say(render.help_lines())

    def cmd_status(self, args: str) -> None:
        run = self.store.load()
        branches = self.orch.branch_summary() if run is not None and run.integration else []
        self.say(render.status_lines(run, self.load_readings(), self.current_mode(), self.multi_repo, branches))

    def cmd_repos(self, args: str) -> None:
        self.say(render.repos_lines(render_text.repo_rows(self.workspace)))

    def cmd_publish(self, args: str) -> None:
        repo_name = args.strip() or None
        run = self.store.load()
        problem = publish_mod.refusal(run)
        if problem is None and self.driver.alive():
            problem = "the team is still working; wait for it to stop before publishing"
        if problem:
            raise WorkforceError(problem)
        entries = publish_mod.publishable(self.orch, repo_name)
        margin = render.LEFT_MARGIN
        self.say(
            [f"{margin}Ready to publish (push, then open a PR):"]
            + render.branch_lines(entries)
            + [f"{margin}Type y to go ahead, anything else cancels."]
        )
        with self._lock:
            self.modal = PublishState(repo=repo_name, entries=entries)

    def _publish_answer(self, line: str) -> None:
        state = self.modal
        assert isinstance(state, PublishState)
        self.modal = None
        if line.lower() not in ("y", "yes"):
            self.say([f"{render.LEFT_MARGIN}Publish cancelled."])
            return
        thread = threading.Thread(target=self._publish_worker, args=(state.repo,), name="wf-publish", daemon=True)
        self.side_threads = [t for t in self.side_threads if t.is_alive()] + [thread]
        thread.start()

    def _publish_worker(self, repo_name: str | None) -> None:
        margin = render.LEFT_MARGIN
        try:
            rows = publish_mod.publish(
                self.orch, repo_name=repo_name, say=lambda text: self.say([f"{margin}{sanitize(text)}"])
            )
        except Exception as exc:
            self.say([render.style(f"{margin}✗ publish failed: {sanitize(exc)}", "red", self.color)])
            return
        lines = []
        for row in rows:
            detail = row.pr_url or row.error or row.status
            mark = "✓" if row.pushed else "✗"
            lines.append(render.style(f"{margin}{mark} {sanitize(row.repo)}  {sanitize(row.branch)}  {sanitize(detail)}", None if row.pushed else "red", self.color))
        self.say(lines)

    def cmd_run(self, args: str) -> None:
        if not args:
            raise WorkforceError("usage: /run <goal>")
        self.start_goal(args)

    def cmd_answer(self, args: str) -> None:
        question_id, _, text = args.partition(" ")
        if not question_id or not text.strip():
            raise WorkforceError("usage: /answer <question id> <your answer>")
        run = self.orch.answer(question_id.upper(), text.strip())
        self.say([f"{render.LEFT_MARGIN}✓ answered {question_id.upper()}"])
        if run.status == "running":
            self.driver.start()
        elif run.status == "paused":
            self.say([f"{render.LEFT_MARGIN}The run is still paused. Type /resume when you want the team to continue."])

    def cmd_pause(self, args: str) -> None:
        before = self.store.load()
        if before is None:
            raise WorkforceError("there is no run to pause")
        if before.status in ("paused", "done", "failed"):
            self.say([f"{render.LEFT_MARGIN}Run {before.id} is already {before.status}."])
            return
        self.orch.pause("paused by the user")
        self.say([f"{render.LEFT_MARGIN}⏸ Pausing. Steps already running will finish first. /resume continues."])

    def cmd_resume(self, args: str) -> None:
        run = self.store.load()
        if run is None:
            raise WorkforceError("there is no run to resume; type a goal to start one")
        if run.status == "awaiting_models" and any(not (t.model and t.effort and t.agent) for t in run.tasks):
            self.enter_model_pick(run)
            return
        run = self.orch.resume()
        if run.status in ("running", "planning", "debating"):
            self.driver.start()
        else:
            self.say([f"{render.LEFT_MARGIN}Run {run.id} is {run.status}; nothing to continue."])

    def cmd_models(self, args: str) -> None:
        margin = render.LEFT_MARGIN
        lines = [f"{margin}{name:<16} {role.agent:<7} {render.role_text(role)}" for name, role in self.config.roles.items()]
        run = self.store.load()
        if run is not None:
            lines += [
                f"{margin}{task.id:<16} {task.agent or '-':<7} "
                + (f"{render.short_model(task.model)} · {task.effort}" if task.model else "not chosen yet")
                for task in run.tasks
            ]
        self.say(lines)

    def cmd_effort(self, args: str) -> None:
        parts = args.split()
        if len(parts) != 2:
            raise WorkforceError("usage: /effort <role|task> <level>")
        target, level = parts[0], parts[1].lower()
        if target in ROLE_NAMES:
            role = self.config.role(target)
            if level not in EFFORTS[role.agent]:
                raise WorkforceError(f"effort '{level}' is not valid for {role.agent}; use one of {', '.join(EFFORTS[role.agent])}")
            updated = config_mod.save_role(self.paths.repo, target, role.model, level)
            self.orch.config = updated
            if self.orch.usage_monitor is not None:
                self.orch.usage_monitor.config = updated
            self.say([f"{render.LEFT_MARGIN}✓ {target}: {render.role_text(updated.role(target))} (for steps that have not started)"])
            return
        run = self.store.load()
        if run is None:
            raise WorkforceError(f"'{target}' is neither a role nor a task of the current run")
        task = run.task(target.upper())
        agent = task.agent or self.config.role("coder").agent
        if level not in EFFORTS[agent]:
            raise WorkforceError(f"effort '{level}' is not valid for {agent}; use one of {', '.join(EFFORTS[agent])}")
        if run.status == "awaiting_models":
            self.orch.set_models(task.id, task.model or self.config.role("coder").model, level)
        elif task.status == "pending":

            def fn(r: Run) -> None:
                r.task(task.id).effort = level

            self.store.update(fn)
        else:
            raise WorkforceError(f"{task.id} has already started ({task.status}); its effort can no longer change")
        self.say([f"{render.LEFT_MARGIN}✓ {task.id}: effort {level}"])

    def cmd_log(self, args: str) -> None:
        events, _ = self.events.read(0)
        name = render_text.normalize_task_name(args)
        if args:
            events = [e for e in events if render_text.event_matches_task(e, name)]
        if not events:
            self.say([f"{render.LEFT_MARGIN}No events{' for ' + name if args else ' yet'}."])
            return
        formatter = EventFormatter(self.multi_repo)
        lines: list[str] = []
        for event in events:
            stamp = render_text.clock_time(event.get("ts"))
            lines += [f"{render.LEFT_MARGIN}{stamp}  {line}" for line in formatter.format(event)]
        self.say(lines[-LOG_TAIL:])

    def cmd_computer(self, args: str) -> None:
        parts = args.split()
        if len(parts) != 2 or parts[0].lower() not in ("on", "off"):
            raise WorkforceError("usage: /computer on|off <task>")
        enabled = parts[0].lower() == "on"
        run = self.store.load()
        if run is None:
            raise WorkforceError("there is no run yet")
        task = run.task(parts[1].upper())
        if task.status == "merged":
            raise WorkforceError(f"{task.id} is already merged")

        def fn(r: Run) -> None:
            r.task(task.id).computer_use = enabled

        self.store.update(fn)
        self.say([f"{render.LEFT_MARGIN}✓ {task.id}: computer use {'on' if enabled else 'off'} (from its next coder step)"])

    def cmd_quit(self, args: str) -> None:
        if not self.driver.alive():
            self.done = True
            return
        with self._lock:
            self.modal = QuitState()
        margin = render.LEFT_MARGIN
        self.say(
            [
                f"{margin}The team is still working. What should happen to it?",
                f"{margin}  p  pause the team: running steps finish, then it stops (/resume continues it later)",
                f"{margin}  l  leave it running: there is no background daemon in v3, so this also pauses it after the current step",
                f"{margin}  c  cancel and stay",
            ]
        )

    def on_eof(self) -> None:
        """Ctrl-D: quit, pausing a running team; a second one in a row quits regardless."""
        self._eof_count += 1
        if self._eof_count > 1:
            self.done = True
        elif isinstance(self.modal, QuitState):
            self._quit_answer("pause")
        else:
            self.cmd_quit("")

    def _quit_answer(self, line: str) -> None:
        choice = QUIT_CHOICES.get(line.lower())
        if choice is None:
            self.say([f"{render.LEFT_MARGIN}Type p to pause, l to leave, or c to cancel."])
        elif choice == "cancel":
            self.modal = None
            self.say([f"{render.LEFT_MARGIN}Staying."])
        else:
            self.modal = None
            self._stop_team(QUIT_PAUSE_REASON if choice == "pause" else QUIT_LEAVE_REASON)

    def _stop_team(self, reason: str) -> None:
        self.orch.pause(reason)
        self.say([f"{render.LEFT_MARGIN}Waiting for the current step to finish (Ctrl-C quits now, state is saved)…"])
        try:
            while self.driver.alive():
                self.driver.join(JOIN_POLL_S)
        except KeyboardInterrupt:
            self.say([f"{render.LEFT_MARGIN}Quitting now; a running agent step may be cut off."])
        self.tick()
        self.done = True

    def _pick_answer(self, line: str) -> None:
        state = self.modal
        assert isinstance(state, PickState)
        if state.index >= len(state.tasks):
            return self._pick_branch_answer(state, line)
        coder = self.config.role("coder")
        try:
            model, effort = parse_model_choice(line, coder.model, coder.effort, coder.agent)
        except ValueError as exc:
            self.say([render.style(f"{render.LEFT_MARGIN}✗ {exc}", "red", self.color)])
            return
        task_id = state.tasks[state.index]
        state.chosen[task_id] = (model, effort)
        state.index += 1
        if state.index < len(state.tasks):
            return
        if state.branches:
            self.say([f"{render.LEFT_MARGIN}Each repo's work lands on its own branch; your checkout is never touched. Enter keeps a name."])
            return
        self._finish_pick(state)

    def _pick_branch_answer(self, state: PickState, line: str) -> None:
        repo_name, _ = state.branches[state.branch_index]
        if line:
            try:
                self.orch.set_branch(repo_name, line)
            except WorkforceError as exc:
                self.say([render.style(f"{render.LEFT_MARGIN}✗ {sanitize(exc)}", "red", self.color)])
                return
        state.branch_index += 1
        if state.branch_index >= len(state.branches):
            self._finish_pick(state)

    def _finish_pick(self, state: PickState) -> None:
        self.modal = None
        for chosen_id, (chosen_model, chosen_effort) in state.chosen.items():
            set_coder(self.orch, chosen_id, chosen_model, chosen_effort)
        self.orch.resume()
        self.say([f"{render.LEFT_MARGIN}✓ models set, starting the team"])
        self.driver.start()


class Background:
    """The console's timers: the event pump, the usage poll and Laya's stuck-agent check."""

    def __init__(self, controller: ConsoleController, tick_s: float = TICK_S):
        self.controller = controller
        self.tick_s = tick_s
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._pump, name="wf-pump", daemon=True),
            threading.Thread(target=self._slow, name="wf-slow", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(2)

    def _pump(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.controller.tick()
            except Exception as exc:
                self.controller.say([f"{render.LEFT_MARGIN}✗ console refresh failed: {exc}"])

    def _slow(self) -> None:
        controller = self.controller
        orch = controller.orch
        poll_s = orch.config.limits.poll_minutes * 60
        next_poll = time.monotonic() + poll_s
        next_progress = time.monotonic() + PROGRESS_INTERVAL_S
        while not self._stop.wait(SLOW_LOOP_S):
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + poll_s
                self._guarded("usage_poll", self._poll)
            if now >= next_progress:
                next_progress = now + PROGRESS_INTERVAL_S
                self._guarded("progress_tick", orch.progress_tick)

    def _poll(self) -> None:
        if self.controller.orch.poll_tick() == "resume":
            self.controller.driver.start()

    def _guarded(self, source: str, tick: Callable[[], Any]) -> None:
        """Run one timer job; any failure is logged as an `error` event and the timer thread carries on."""
        try:
            tick()
        except Exception as exc:
            try:
                self.controller.orch.events.emit("error", source=source, message=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass


def _terminal_width() -> int:
    app = get_app_or_none()
    if app is not None:
        return app.output.get_size().columns
    return os.get_terminal_size().columns if os.isatty(1) else 80


def build_session(controller: ConsoleController, *, input=None, output=None) -> PromptSession:
    """The bordered prompt: a rule above and below the input line, the footer beneath, history and completion."""
    bindings = KeyBindings()

    @bindings.add("s-tab")
    def _toggle(event) -> None:
        controller.toggle_mode()
        event.app.invalidate()

    def message() -> FormattedText:
        return FormattedText(
            [("class:rule", render.rule(_terminal_width()) + "\n"), ("class:prompt", controller.prompt_label())]
        )

    def toolbar() -> FormattedText:
        lines = controller.toolbar_lines(_terminal_width())
        return FormattedText([("class:rule", lines[0] + "\n"), ("", "\n".join(lines[1:]))])

    history_path = controller.paths.home / HISTORY_NAME
    history_path.parent.mkdir(parents=True, exist_ok=True)
    style = Style.from_dict(
        {
            "bottom-toolbar": "noreverse",
            "bottom-toolbar.text": "noreverse",
            "rule": "ansibrightblack",
            "prompt": "bold ansicyan",
            "placeholder": "ansibrightblack",
        }
    )
    return PromptSession(
        message,
        history=FileHistory(str(history_path)),
        completer=WorkforceCompleter(controller.completion_context),
        complete_while_typing=False,
        bottom_toolbar=toolbar,
        placeholder=FormattedText([("class:placeholder", PLACEHOLDER)]),
        key_bindings=bindings,
        refresh_interval=TICK_S,
        style=style,
        input=input,
        output=output,
    )


def run_console(
    repo: Path,
    *,
    home: Path | None = None,
    input=None,
    output=None,
    orchestrator: Orchestrator | None = None,
    preflight: Callable[[], PreflightSummary] | None = None,
    printer: Callable[[str], None] | None = None,
    background: bool = True,
    clock: Any = time,
    workspace: Workspace | None = None,
) -> int:
    """Open the interactive console for the workspace rooted at `repo` (a single repo, or a folder of repos).

    `workspace` carries the folder the user started in (its focus repo); without it the workspace is found from `repo`.
    `input`, `output`, `orchestrator`, `preflight` and `printer` exist so tests can drive it without a terminal.
    """
    repo = Path(repo)
    if orchestrator is None:
        try:
            config = config_mod.load(repo)
        except ConfigError as exc:
            raise ConfigError(f"{exc}. Run `workforce init` in this repo first") from exc
        orchestrator = build_orchestrator(
            Context(repo=repo, home=home, console=render_text.make_console(), clock=clock, ws=workspace), config
        )
    repo_paths = [info.path for info in orchestrator.workspace.repos.values()]
    summary = preflight() if preflight is not None else run_preflight(orchestrator.config, repo_paths)
    controller = ConsoleController(orchestrator, summary, printer=printer)
    session = build_session(controller, input=input, output=output)
    timers = Background(controller) if background else None
    with shutdown_guard(lambda: pause_interrupted(orchestrator)), patch_stdout(raw=True):
        controller.show_header()
        if timers is not None:
            timers.start()
        try:
            while not controller.done:
                try:
                    text = session.prompt()
                except KeyboardInterrupt:
                    controller.say([f"{render.LEFT_MARGIN}(Ctrl-C clears the line. Type /quit to leave.)"])
                    continue
                except EOFError:
                    controller.on_eof()
                    continue
                controller._eof_count = 0
                controller.handle_line(text)
        finally:
            if timers is not None:
                timers.stop()
            close_orchestrator(orchestrator)
    return 0
