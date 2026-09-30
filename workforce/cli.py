"""Command line entry point: `workforce` and `wf`."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from rich.console import Console

from workforce import config as config_mod
from workforce import git_ops, publish as publish_mod, render_text, workspace as workspace_mod
from workforce.agents import guard
from workforce.agents.base import clear_shutdown, kill_all, request_shutdown
from workforce.agents.claude import ClaudeRunner
from workforce.agents.codex import CodexRunner
from workforce.config import EFFORTS, ROLE_NAMES, Config
from workforce.decider.laya import build_decider
from workforce.errors import ConfigError, GitError, PreflightError, StateLockedError, WorkforceError
from workforce.events import EventLog
from workforce.orchestrator import Orchestrator
from workforce.paths import Paths
from workforce.scheduler import INTERRUPTED_REASON, Scheduler
from workforce.state import Run, StateStore
from workforce.usage.monitor import USAGE_PAUSE_PREFIX, UsageMonitor
from workforce.workspace import NoWorkspace, NotInitialized, Workspace

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PREFLIGHT = 3
EXIT_LOCKED = 4
EXIT_INTERRUPTED = 130
TAIL_INTERVAL_S = 0.2
PROGRESS_INTERVAL_S = 180
ACTIVE_TASK_STATUSES = ("coding", "fixing", "testing", "reviewing", "awaiting_commit", "committing")
VERSION_TIMEOUT_S = 30
VERSION_PATTERN = re.compile(r"\d+\.\d+(?:\.\d+)*")
MODES = ("auto", "step")
PAUSE_ON_INTERRUPT_STATUSES = ("planning", "debating", "running")
TERMINATION_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@dataclass
class Context:
    """What every command needs: the workspace root, where home is, and where to print.

    `repo` is the workspace root (the repo itself in single-repo mode). `ws` is the resolved workspace;
    it stays unset for `wf init`, which runs before there is one.
    """

    repo: Path
    home: Path | None
    console: Console
    clock: Any = time
    ws: Workspace | None = None

    @property
    def paths(self) -> Paths:
        return Paths(self.repo, home=self.home)

    @property
    def workspace(self) -> Workspace:
        if self.ws is None:
            self.ws = workspace_mod.find_workspace(self.repo)
        return self.ws


class RunnerLock:
    """The runner lock, held for a whole drive loop; `suspended()` hands it to the scheduler for a stretch."""

    def __init__(self, orch: Orchestrator):
        self.orch = orch
        self._stack: ExitStack | None = None

    def acquire(self) -> None:
        stack = ExitStack()
        stack.enter_context(self.orch.store.lock())
        self._stack = stack

    def release(self) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None

    @contextmanager
    def suspended(self):
        """Give the lock up, then take it back (raises StateLockedError if another process got it meanwhile)."""
        self.release()
        try:
            yield
        finally:
            self.acquire()

    def __enter__(self) -> "RunnerLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class Ticker:
    """Runs the timers that belong to an active run: usage polling and the agent progress check."""

    def __init__(self, orch: Orchestrator, progress_interval_s: float = PROGRESS_INTERVAL_S):
        self.orch = orch
        self.poll_interval_s = orch.config.limits.poll_minutes * 60
        self.progress_interval_s = progress_interval_s
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _every(self, interval_s: float, source: str, tick: Any) -> None:
        while not self._stop.wait(interval_s):
            try:
                tick()
            except Exception as exc:
                self.orch.events.emit("error", source=source, message=str(exc))

    def __enter__(self) -> "Ticker":
        timers = [(self.progress_interval_s, "progress_tick", self.orch.progress_tick)]
        if self.orch.usage_monitor is not None:
            timers.append((self.poll_interval_s, "usage_poll", self.orch.poll_tick))
        for interval, source, tick in timers:
            thread = threading.Thread(target=self._every, args=(interval, source, tick), daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join()
        self._threads.clear()


class EventTail:
    """Prints events as they are appended to `events.jsonl`, from a background thread."""

    def __init__(self, events: EventLog, console: Console, workspace: bool = False):
        self.events = events
        self.console = console
        self.formatter = render_text.EventFormatter(workspace)
        self.offset = self._size()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _size(self) -> int:
        try:
            return self.events.paths.events.stat().st_size
        except FileNotFoundError:
            return 0

    def _with_task(self, event: dict) -> dict:
        """Name the task on a blocked call: the event's `task_id`, else the one task in flight, else none."""
        blocked = event.get("kind") == "decision" and event.get("key") == "risk_gate" and event.get("allowed") is False
        if not blocked:
            return event
        named = event.get("task_id") or event.get("task")
        if named:
            return {**event, "task": named}
        run = StateStore(self.events.paths).load()
        active = [t for t in run.tasks if t.status in ACTIVE_TASK_STATUSES] if run else []
        if len(active) != 1:
            return event
        return {"repo": active[0].repo, **event, "task": active[0].id}

    def drain(self) -> None:
        new, self.offset = self.events.read(self.offset)
        for event in new:
            render_text.print_lines(self.console, self.formatter.format(self._with_task(event)))

    def _loop(self) -> None:
        while not self._stop.wait(TAIL_INTERVAL_S):
            self.drain()

    def __enter__(self) -> "EventTail":
        self.drain()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self.drain()


NO_WORKSPACE_MESSAGE = "not inside a git repo or a workspace; cd into one, or run wf init in a folder of repos"
INIT_SHADOW_MESSAGE = (
    "{found} already configures a workspace above {target}; a workforce.toml here would shadow it. "
    "Run wf init --force to create one anyway"
)


def resolve_workspace(start: Path | None = None) -> Workspace:
    """The workspace containing `start` (the current directory by default).

    NotInitialized and NoWorkspace become PreflightError with the user-facing message, so they exit 3.
    """
    try:
        return workspace_mod.find_workspace(start or Path.cwd())
    except NotInitialized as exc:
        raise PreflightError(str(exc)) from exc
    except NoWorkspace:
        raise PreflightError(NO_WORKSPACE_MESSAGE) from None


def multi_repo(orch: Orchestrator) -> bool:
    """True when the run's event lines and tables should name the repo of each task."""
    return not orch.workspace.single


def build_runners(config: Config) -> dict:
    return {"claude": ClaudeRunner(Path(config.bin.claude)), "codex": CodexRunner(Path(config.bin.codex))}


def build_monitor(ctx: Context, config: Config, runners: dict, events: EventLog) -> UsageMonitor:
    return UsageMonitor(
        ctx.paths,
        config,
        str(Path(config.bin.codex).expanduser()),
        runners["claude"],
        events,
        ctx.clock,
    )


_orchestrators: list[Orchestrator] = []


def build_orchestrator(ctx: Context, config: Config) -> Orchestrator:
    """An orchestrator with real runners from `config` and a usage monitor that gates every launch.

    It is remembered so that `main` can pause its run on an interrupt and close its decider on the way out.
    """
    runners = build_runners(config)
    events = EventLog(ctx.paths)
    decider = build_decider(config.decider, events)
    monitor = build_monitor(ctx, config, runners, events)
    orch = Orchestrator(ctx.workspace, config, runners, decider, ctx.clock, home=ctx.home, usage_monitor=monitor)
    _orchestrators.append(orch)
    return orch


def close_orchestrator(orch: Orchestrator) -> None:
    """Release what an orchestrator holds open: its decider's HTTP client."""
    close = getattr(orch.decider, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def close_orchestrators() -> None:
    while _orchestrators:
        close_orchestrator(_orchestrators.pop())


def pause_interrupted(orch: Orchestrator) -> None:
    """Pause a run that was doing work when the process was told to stop; other states are left as they are."""
    try:
        run = orch.store.load()
        if run is not None and run.status in PAUSE_ON_INTERRUPT_STATUSES:
            orch.pause(INTERRUPTED_REASON)
    except Exception:
        pass


@contextmanager
def shutdown_guard(pause: Callable[[], None]):
    """Turn SIGINT and SIGTERM into an orderly stop: kill every agent and check process, pause the run, then unwind.

    SIGINT surfaces as KeyboardInterrupt and SIGTERM as SystemExit(143). The run is paused once inside the
    handler and again after the stack has unwound, so a signal landing in the middle of a state write cannot
    lose the pause. Handlers can only be installed from the main thread; elsewhere this is a no-op.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, frame: Any) -> None:
        request_shutdown()
        kill_all()
        pause()
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, handler) for sig in TERMINATION_SIGNALS}
    try:
        yield
    except (KeyboardInterrupt, SystemExit):
        kill_all()
        pause()
        raise
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
        clear_shutdown()


def _version(binary: str) -> str:
    try:
        proc = subprocess.run(
            [str(Path(binary).expanduser()), "--version"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "?"
    match = VERSION_PATTERN.search(proc.stdout + proc.stderr)
    return match.group(0) if match else "?"


def _read_line(prompt: str) -> str:
    return input(prompt)


def git_toplevel(cwd: Path) -> Path | None:
    """The top level of the git repo containing `cwd`, or None when it is not inside one."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        raise PreflightError("git is not installed or not on PATH") from None
    return Path(proc.stdout.strip()) if proc.returncode == 0 else None


def _shadowed_config(target: Path) -> Path | None:
    """A `workforce.toml` in a folder above `target`, which a new one in `target` would take over from."""
    for folder in target.resolve().parents:
        if (folder / config_mod.CONFIG_NAME).is_file():
            return folder / config_mod.CONFIG_NAME
    return None


def _init_target(cwd: Path) -> tuple[Path, list[str] | None]:
    """Where `wf init` writes its config, and the repo names when that is a folder of repos (None for a repo)."""
    cwd = cwd.resolve()
    if (cwd / config_mod.CONFIG_NAME).is_file():
        return cwd, None
    toplevel = git_toplevel(cwd)
    if toplevel is not None:
        return toplevel.resolve(), None
    found = workspace_mod.discover(cwd)
    if not found:
        raise PreflightError(NO_WORKSPACE_MESSAGE)
    return cwd, [ref.name for ref in found]


def _print_workspace_repos(console: Console, ws: Workspace) -> None:
    console.print()
    console.print(f"Workspace {ws.root} · {len(ws.repos)} repos", markup=False, soft_wrap=True, style="bold")
    for row in render_text.repo_rows(ws):
        detached = row["branch"] == "(detached)"
        render_text.print_check(
            console, not detached, f"{row['name']}  on {row['branch']}  ·  checks: {row['checks']}"
        )


def _back_up_config(path: Path) -> Path:
    backup = path.with_name(path.name + ".bak")
    n = 1
    while backup.exists():
        n += 1
        backup = path.with_name(f"{path.name}.bak{n}")
    path.rename(backup)
    return backup


def cmd_init(ctx: Context, args: argparse.Namespace) -> int:
    console = ctx.console
    existing = ctx.repo.resolve() / config_mod.CONFIG_NAME
    if args.force and existing.is_file():
        backup = _back_up_config(existing)
        render_text.print_check(console, True, f"moved the old config to {backup.name}")
    root, names = _init_target(ctx.repo)
    paths = Paths(root, home=ctx.home)
    if not paths.config.exists():
        shadow = _shadowed_config(root)
        if shadow is not None and not args.force:
            console.print(
                "✗ " + INIT_SHADOW_MESSAGE.format(found=shadow, target=root),
                markup=False,
                soft_wrap=True,
                style="red",
            )
            return EXIT_PREFLIGHT
    if paths.config.exists():
        wrote = "workforce.toml already exists, kept as it is"
    elif names is not None:
        config_mod.write_workspace_default(root, names)
        wrote = f"wrote workforce.toml ({len(names)} repos)"
    else:
        config_mod.write_default(root)
        wrote = "wrote workforce.toml"
    try:
        config = config_mod.load(root)
        ws = workspace_mod.load_workspace(root, config)
    except ConfigError as exc:
        raise ConfigError(
            f"{exc}\nTo regenerate workforce.toml from the repos found here, run: wf init --force "
            "(the current file is kept as workforce.toml.bak)"
        ) from exc
    paths.ensure()

    env_problems = guard.check_env(os.environ)
    claude_problems = guard.check_claude(Path(config.bin.claude))
    codex_problems = guard.check_codex(Path(config.bin.codex))
    repo_problems = [problem for info in ws.repos.values() for problem in guard.check_repo(info.path)]
    no_key = "no API key" if not env_problems else "API key set"
    if claude_problems:
        render_text.print_check(console, False, f"claude: {claude_problems[0]}")
    else:
        render_text.print_check(console, True, f"claude {_version(config.bin.claude)} (claude.ai login, {no_key})")
    if codex_problems:
        render_text.print_check(console, False, f"codex: {codex_problems[0]}")
    else:
        render_text.print_check(console, True, f"codex {_version(config.bin.codex)} (ChatGPT login, {no_key})")
    for problem in env_problems:
        render_text.print_check(console, False, problem)
    if ws.single:
        if repo_problems:
            render_text.print_check(console, False, repo_problems[0])
        else:
            info = next(iter(ws.repos.values()))
            render_text.print_check(console, True, f"repo on branch {git_ops.current_branch(info.path)}")
    else:
        for problem in repo_problems:
            render_text.print_check(console, False, problem)
    render_text.print_check(console, True, wrote)
    render_text.print_check(console, True, "created .workforce/")
    if not ws.single:
        _print_workspace_repos(console, ws)
        _, skipped = workspace_mod.discover_with_skipped(root)
        if skipped:
            console.print(
                f"Skipped {len(skipped)} git worktrees of repos already listed (they share branches):",
                markup=False,
                soft_wrap=True,
                style="dim",
            )
            for item in skipped:
                console.print(f"  - {item.name} (worktree of {item.of})", markup=False, soft_wrap=True, style="dim")
        if names is not None and "wrote" in wrote:
            console.print(
                "Edit [workspace] repos in workforce.toml to trim the list",
                markup=False,
                soft_wrap=True,
                style="dim",
            )
    problems = claude_problems + codex_problems + env_problems + repo_problems

    if args.check_models:
        if problems:
            render_text.print_check(console, False, "skipped the model check because the checks above failed")
        else:
            problems += _check_models(ctx, config, next(iter(ws.repos.values())).path)
    if problems:
        console.print("preflight failed", markup=False, style="red")
        return EXIT_PREFLIGHT
    return EXIT_OK


def _check_models(ctx: Context, config: Config, cwd: Path) -> list[str]:
    runners = build_runners(config)
    probes = {"claude": guard.probe_claude_model, "codex": guard.probe_codex_model}
    seen: dict[tuple[str, str, str], tuple[bool, str]] = {}
    failures: list[str] = []
    for name, role in config.roles.items():
        key = (role.agent, role.model, role.effort)
        if key not in seen:
            try:
                ok = probes[role.agent](runners[role.agent], role.model, role.effort, cwd=cwd)
                seen[key] = (ok, "" if ok else "not available on this subscription")
            except PreflightError as exc:
                seen[key] = (False, str(exc))
        ok, detail = seen[key]
        label = f"{name}: {role.agent} {role.model} · {role.effort}"
        render_text.print_check(ctx.console, ok, label + (f" ({detail})" if detail else ""))
        if not ok:
            failures.append(label)
    return failures


def parse_model_choice(text: str, default_model: str, default_effort: str, agent: str) -> tuple[str, str]:
    """`''` keeps the defaults, `model` overrides the model, `model effort` overrides both."""
    parts = text.split()
    if not parts:
        return default_model, default_effort
    if len(parts) > 2:
        raise ValueError("type a model, or a model and an effort, or press Enter for the default")
    model = parts[0]
    effort = parts[1] if len(parts) == 2 else default_effort
    if effort not in EFFORTS[agent]:
        raise ValueError(f"effort '{effort}' is not valid for {agent}; choose one of {', '.join(EFFORTS[agent])}")
    return model, effort


def set_coder(orch: Orchestrator, task_id: str, model: str, effort: str) -> None:
    """Store a task's coder model and effort; the first time, also give it `[browser] computer_use` as its default."""
    first_pick = not orch.store.load().task(task_id).model
    orch.set_models(task_id, model, effort)
    default = orch.config.browser.computer_use
    if first_pick and default:

        def fn(run: Run) -> None:
            run.task(task_id).computer_use = default

        orch.store.update(fn)


def pick_models(orch: Orchestrator, console: Console) -> None:
    """Show the agreed plan and ask, per task, which coder model and effort to use."""
    run = orch.store.load()
    coder = orch.config.role("coder")
    hints = orch.task_size_hints()
    workspace = multi_repo(orch)
    console.print()
    console.print("Plan agreed. Choose the coder for each task.", markup=False, style="bold")
    console.print(
        f"Press Enter to keep the default, or type a model, or a model and an effort ({', '.join(EFFORTS[coder.agent])}).",
        markup=False,
        style="dim",
    )
    for task in run.tasks:
        hint = f"   (Laya: {hints[task.id]})" if hints.get(task.id) else ""
        label = render_text.task_label(task.repo, task.id, workspace)
        prompt = f"{label}  {task.title}{hint}   coder [{coder.model} · {coder.effort}]: "
        while True:
            answer = _read_line(prompt)
            try:
                model, effort = parse_model_choice(answer, coder.model, coder.effort, coder.agent)
            except ValueError as exc:
                render_text.print_check(console, False, str(exc))
                continue
            set_coder(orch, task.id, model, effort)
            break
    pick_branches(orch, console)


def pick_branches(orch: Orchestrator, console: Console) -> None:
    """Show each involved repo's integration branch and let the user rename it (Enter keeps it)."""
    proposed = orch.proposed_branches()
    if not proposed:
        return
    console.print()
    console.print("Each repo's work lands on its own branch; your checkout is never touched.", markup=False, style="dim")
    for repo_name, default in proposed.items():
        while True:
            answer = _read_line(f"branch for {repo_name} [{default}]: ").strip()
            if not answer:
                break
            try:
                orch.set_branch(repo_name, answer)
            except WorkforceError as exc:
                render_text.print_check(console, False, str(exc))
                continue
            break


def answer_questions(orch: Orchestrator, console: Console) -> bool:
    """Prompt for every open question; False if the user left one open (Ctrl-C or end of input)."""
    run = orch.store.load()
    for question in [q for q in run.questions if q.status == "open"]:
        if orch.store.load().question(question.id).status != "open":
            continue
        console.print()
        render_text.print_question(console, question)
        while True:
            try:
                text = _read_line(f"{question.id} answer (Ctrl-C to leave it open): ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print()
                console.print(
                    f'{question.id} is still open. Answer it later with: workforce answer {question.id} "<text>"',
                    markup=False,
                    soft_wrap=True,
                )
                return False
            if text:
                break
            render_text.print_check(console, False, "type an answer, or press Ctrl-C to leave the question open")
        try:
            orch.answer(question.id, text)
        except WorkforceError as exc:
            console.print(render_text.sanitize(exc), markup=False, style="dim")
    return True


def step_until_blocked(orch: Orchestrator) -> Run:
    """Step until the run needs the user, is paused, or finishes. The caller holds the runner lock."""
    while orch.step().progressed:
        pass
    return orch.store.load()


def advance(orch: Orchestrator, lock: RunnerLock) -> Run:
    """Move the run forward until it is blocked: sequentially, or through the Scheduler when `parallel > 1`.

    `Scheduler.run_until_blocked` takes the runner lock itself, so the CLI's lock is handed over for
    that call and taken back afterwards.
    """
    parallel = orch.config.limits.parallel
    if parallel <= 1:
        return step_until_blocked(orch)
    scheduler = Scheduler(orch, parallel)
    with lock.suspended():
        return scheduler.run_until_blocked()


def _usage_wait_is_automatic(orch: Orchestrator, run: Run) -> bool:
    monitor = orch.usage_monitor
    return monitor is not None and (run.pause_reason or "").startswith(USAGE_PAUSE_PREFIX) and monitor.resume_plan().auto


def wait_for_auto_resume(orch: Orchestrator, ctx: Context, tail: EventTail) -> str:
    """Stay in the foreground until a 5-hour usage pause lifts.

    Returns "resumed", "stopped" (Ctrl-C, the run stays paused) or "manual" (the pause stopped being automatic).
    """
    console = ctx.console
    monitor = orch.usage_monitor
    poll_s = orch.config.limits.poll_minutes * 60
    run = orch.store.load()
    plan = monitor.resume_plan()
    console.print()
    console.print(
        f"⏸ Paused: {render_text.sanitize(run.pause_reason)}. Resumes automatically at {render_text.format_clock(plan.at)} "
        f"(in {render_text.format_wait(plan.at - ctx.clock.time())}) if all limits are under "
        f"{orch.config.limits.pause_percent}%. Ctrl-C to stop waiting (the run stays paused).",
        markup=False,
        soft_wrap=True,
        style="yellow",
    )
    first = True
    try:
        while True:
            plan = monitor.resume_plan()
            if not plan.auto:
                return "manual"
            now = ctx.clock.time()
            if now < plan.at:
                delay = min(poll_s, plan.at - now)
            else:
                delay = 0 if first else poll_s
            first = False
            with tail:
                if delay:
                    ctx.clock.sleep(delay)
                orch.poll_tick()
            if orch.store.load().status != "paused":
                return "resumed"
    except KeyboardInterrupt:
        console.print()
        console.print(
            "Stopped waiting. The run stays paused; continue later with: workforce resume", markup=False, style="dim"
        )
        return "stopped"


def print_pause_hint(console: Console, reason: str) -> None:
    console.print()
    console.print(f"⏸ Paused: {render_text.sanitize(reason)}", markup=False, soft_wrap=True, style="yellow")
    if reason.startswith(USAGE_PAUSE_PREFIX):
        hint = "This limit does not reset soon enough to wait for. Once it has, run: workforce resume"
    elif reason.startswith("model_unavailable"):
        hint = "Pick an available model with: workforce models set <role> <model> [effort], then run: workforce resume"
    else:
        hint = "Fix the cause, then run: workforce resume"
    console.print(hint, markup=False, soft_wrap=True, style="dim")


def drive(orch: Orchestrator, ctx: Context, lock: RunnerLock) -> int:
    """Drive the run to done, a pause, or a question the user leaves open. The caller holds the runner lock."""
    console = ctx.console
    tail = EventTail(orch.events, console, multi_repo(orch))
    while True:
        with tail, Ticker(orch):
            run = advance(orch, lock)
        if run.status == "awaiting_models":
            pick_models(orch, console)
            orch.resume()
        elif run.status == "awaiting_user":
            if not answer_questions(orch, console):
                return EXIT_OK
        elif run.status == "paused":
            if _usage_wait_is_automatic(orch, run):
                outcome = wait_for_auto_resume(orch, ctx, tail)
                if outcome == "resumed":
                    continue
                if outcome == "stopped":
                    return EXIT_OK
                run = orch.store.load()
            print_pause_hint(console, run.pause_reason or "")
            return EXIT_OK
        elif run.status == "done":
            console.print()
            console.print(render_text.tasks_table(run, multi_repo(orch)))
            return EXIT_OK
        else:
            console.print()
            console.print(f"✗ Run {run.id} ended as {run.status}.", markup=False, style="red")
            console.print(render_text.tasks_table(run, multi_repo(orch)))
            return EXIT_FAILED


def _prepare_resume(orch: Orchestrator, ctx: Context) -> bool:
    """Clear whatever stop the run is in; False when there is nothing to continue."""
    run = orch.store.load()
    if run is None:
        raise WorkforceError("no run in progress; start one with: workforce run \"<goal>\"")
    if run.status in ("done", "failed"):
        ctx.console.print(f"Run {run.id} is already {run.status}.", markup=False)
        return False
    if run.status == "awaiting_models" and any(not (t.model and t.effort and t.agent) for t in run.tasks):
        return True
    if run.status == "paused" and _usage_wait_is_automatic(orch, run):
        guard.preflight(Path(orch.config.bin.claude), Path(orch.config.bin.codex), None, os.environ)
        return True
    try:
        orch.resume()
    except PreflightError:
        raise
    except WorkforceError:
        if not _refused_for_usage(orch, ctx):
            raise
        return False
    return True


def _refused_for_usage(orch: Orchestrator, ctx: Context) -> bool:
    """After a refused resume of a usage pause: show the readings and when to try again. False if it was not that."""
    run = orch.store.load()
    monitor = orch.usage_monitor
    if run is None or run.status != "paused" or monitor is None or not (run.pause_reason or "").startswith(USAGE_PAUSE_PREFIX):
        return False
    pause = orch.config.limits.pause_percent
    readings = monitor.readings()
    high = [w for w in readings.get("windows", []) if w.get("percent", 0) >= pause]
    if not high:
        return False
    console = ctx.console
    render_text.print_usage(console, readings, None)
    resets = [w["resets_at"] for w in high if isinstance(w.get("resets_at"), (int, float))]
    when = f"try again after {render_text.format_reset(max(resets))}" if resets else "try again later"
    console.print(f"still above {pause}%; {when}", markup=False, soft_wrap=True, style="yellow")
    return True


def cmd_run(ctx: Context, args: argparse.Namespace) -> int:
    if not args.resume and not args.goal:
        ctx.console.print('give a goal: workforce run "<goal>", or continue one with --resume', markup=False, style="red")
        return EXIT_USAGE
    config = config_mod.load(ctx.repo)
    orch = build_orchestrator(ctx, config)
    with RunnerLock(orch) as lock:
        if args.resume:
            if not _prepare_resume(orch, ctx):
                return EXIT_OK
        else:
            with EventTail(orch.events, ctx.console, multi_repo(orch)):
                orch.start(args.goal, args.mode)
        return drive(orch, ctx, lock)


def cmd_resume(ctx: Context, args: argparse.Namespace) -> int:
    config = config_mod.load(ctx.repo)
    orch = build_orchestrator(ctx, config)
    with RunnerLock(orch) as lock:
        if not _prepare_resume(orch, ctx):
            return EXIT_OK
        return drive(orch, ctx, lock)


def _load_run(ctx: Context) -> Run | None:
    return StateStore(ctx.paths).load()


def _integration_branches(ctx: Context, run: Run | None) -> list[dict]:
    """The run's integration branches with commit counts; empty when there is no run or none has been created."""
    if run is None or not run.integration:
        return []
    orch = build_orchestrator(ctx, config_mod.load(ctx.repo))
    return orch.branch_summary()


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    run = _load_run(ctx)
    render_text.print_status(ctx.console, run, not ctx.workspace.single, _integration_branches(ctx, run))
    return EXIT_OK


def cmd_usage(ctx: Context, args: argparse.Namespace) -> int:
    paths = ctx.paths
    readings = None
    if paths.usage_json.exists():
        try:
            readings = json.loads(paths.usage_json.read_text())
        except json.JSONDecodeError as exc:
            raise WorkforceError(f"{paths.usage_json} is not valid JSON: {exc}") from exc
    summary = paths.usage_md.read_text() if paths.usage_md.exists() else None
    render_text.print_usage(ctx.console, readings, summary)
    return EXIT_OK


def cmd_questions(ctx: Context, args: argparse.Namespace) -> int:
    render_text.print_questions(ctx.console, _load_run(ctx), args.all)
    return EXIT_OK


def cmd_answer(ctx: Context, args: argparse.Namespace) -> int:
    config = config_mod.load(ctx.repo)
    orch = build_orchestrator(ctx, config)
    run = orch.answer(args.question, args.text)
    render_text.print_check(ctx.console, True, f"answered {args.question}")
    if run.status in ("running", "awaiting_models"):
        ctx.console.print("Continue with: workforce run --resume", markup=False, style="dim")
    elif run.status == "awaiting_user":
        ctx.console.print("Other questions are still open: workforce questions", markup=False, style="dim")
    return EXIT_OK


def cmd_models_task(ctx: Context, args: argparse.Namespace) -> int:
    config = config_mod.load(ctx.repo)
    store = StateStore(ctx.paths)
    model = args.model.strip()
    if not model:
        raise WorkforceError("model must not be empty")
    chosen: dict[str, str] = {}

    def fn(run: Run) -> None:
        task = run.task(args.task_id.upper())
        if task.status == "merged":
            raise WorkforceError(f"{task.id} is already merged; its model can no longer change")
        if task.status != "pending" and run.status != "paused":
            raise WorkforceError(
                f"{task.id} is {task.status} in a {run.status} run; pause the run first with: workforce pause"
            )
        agent = task.agent or config.role("coder").agent
        effort = args.effort or task.effort or config.role("coder").effort
        if effort not in EFFORTS[agent]:
            raise WorkforceError(f"effort '{effort}' is not valid for {agent}; choose one of {', '.join(EFFORTS[agent])}")
        task.model, task.effort, task.agent = model, effort, agent
        chosen.update(task=task.id, agent=agent, effort=effort)

    store.update(fn)
    EventLog(ctx.paths).emit("note", text=f"{chosen['task']} will use {chosen['agent']} {model} · {chosen['effort']}")
    render_text.print_check(ctx.console, True, f"{chosen['task']}: {chosen['agent']} {model} · {chosen['effort']}")
    return EXIT_OK


def cmd_models(ctx: Context, args: argparse.Namespace) -> int:
    if getattr(args, "models_command", None) == "task":
        return cmd_models_task(ctx, args)
    if getattr(args, "models_command", None) == "set":
        config = config_mod.load(ctx.repo)
        effort = args.effort or config.role(args.role).effort
        updated = config_mod.save_role(ctx.repo, args.role, args.model, effort)
        role = updated.role(args.role)
        render_text.print_check(ctx.console, True, f"{args.role}: {role.agent} {role.model} · {role.effort}")
        return EXIT_OK
    render_text.print_roles(ctx.console, config_mod.load(ctx.repo))
    return EXIT_OK


def cmd_pause(ctx: Context, args: argparse.Namespace) -> int:
    config = config_mod.load(ctx.repo)
    orch = build_orchestrator(ctx, config)
    run = orch.pause("paused by the user")
    if run.status == "paused":
        ctx.console.print("⏸ Paused. A step already running will finish first. Continue with: workforce resume", markup=False)
    else:
        ctx.console.print(f"Run {run.id} is already {run.status}.", markup=False)
    return EXIT_OK


def cmd_log(ctx: Context, args: argparse.Namespace) -> int:
    events, _ = EventLog(ctx.paths).read(0)
    if args.task:
        events = [e for e in events if render_text.event_matches_task(e, args.task)]
    if not events:
        ctx.console.print(f"No events for {args.task}." if args.task else "No events yet.", markup=False)
        return EXIT_OK
    formatter = render_text.EventFormatter(not ctx.workspace.single)
    for event in events:
        for line in formatter.format(event):
            ctx.console.print(f"{render_text.clock_time(event.get('ts'))}  {line}", markup=False, soft_wrap=True)
    return EXIT_OK


def cmd_repos(ctx: Context, args: argparse.Namespace) -> int:
    ws = ctx.workspace
    title = f"Workspace {ws.root} · {len(ws.repos)} repos" if not ws.single else f"Repo {ws.root}"
    ctx.console.print(title, markup=False, soft_wrap=True, style="bold")
    ctx.console.print(render_text.repos_table(ws))
    return EXIT_OK


def _confirm_publish(console: Console, entry: dict) -> bool:
    noun = "commit" if entry["commits"] == 1 else "commits"
    prompt = f"Push {entry['branch']} ({entry['commits']} {noun}) in {entry['repo']} and open a PR? [y/N] "
    try:
        return _read_line(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        console.print()
        return False


def cmd_publish(ctx: Context, args: argparse.Namespace) -> int:
    config = config_mod.load(ctx.repo)
    orch = build_orchestrator(ctx, config)
    console = ctx.console
    confirm = None if args.yes else (lambda entry: _confirm_publish(console, entry))
    with EventTail(orch.events, console, multi_repo(orch)):
        rows = publish_mod.publish(
            orch,
            repo_name=args.repo,
            confirm=confirm,
            say=lambda text: console.print(text, markup=False, soft_wrap=True, style="dim"),
        )
    console.print()
    console.print(render_text.publish_table(rows))
    return EXIT_OK if all(row.pushed or row.status == publish_mod.STATUS_SKIPPED for row in rows) else EXIT_FAILED


def open_console(ctx: Context) -> int:
    """Called when no subcommand is given: the interactive console."""
    from workforce.console.app import run_console

    return run_console(ctx.repo, home=ctx.home, clock=ctx.clock, workspace=ctx.workspace)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workforce",
        description="WorkForce Agent: Codex plans, Claude codes, both review, you decide.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    init = sub.add_parser("init", help="write workforce.toml and check the setup")
    init.add_argument("--check-models", action="store_true", help="also probe every configured model with a 1-turn run")
    init.add_argument("--force", action="store_true", help="regenerate workforce.toml (the old one is kept as workforce.toml.bak), or write one even if a config above would be shadowed by it")

    run = sub.add_parser("run", help="start a run for a goal, or continue one with --resume")
    run.add_argument("goal", nargs="?", help="what the team should build")
    run.add_argument("--resume", action="store_true", help="continue the run recorded in .workforce/state.json")
    run.add_argument("--mode", choices=MODES, default="auto", help="step asks before starting each task")

    sub.add_parser("status", help="show the run, its tasks and open questions")
    sub.add_parser("repos", help="list the workspace's repos with base, branch, dirty flag and checks")

    publish = sub.add_parser("publish", help="push each integration branch and open PRs (Claude, with your own setup)")
    publish.add_argument("--repo", metavar="NAME", help="only this repo's branch")
    publish.add_argument("--yes", action="store_true", help="do not ask before publishing each branch")
    sub.add_parser("usage", help="show the latest Claude and Codex usage readings")

    questions = sub.add_parser("questions", help="list open questions")
    questions.add_argument("--all", action="store_true", help="include answered questions")

    answer = sub.add_parser("answer", help="answer a question")
    answer.add_argument("question", metavar="id", help="question id, for example Q1")
    answer.add_argument("text", help="your answer")

    models = sub.add_parser("models", help="show the configured models, or change one role")
    models_sub = models.add_subparsers(dest="models_command", metavar="set")
    models_set = models_sub.add_parser("set", help="set a role's model and optionally its effort")
    models_set.add_argument("role", choices=ROLE_NAMES)
    models_set.add_argument("model")
    models_set.add_argument("effort", nargs="?", help="keeps the role's current effort when left out")
    models_task = models_sub.add_parser("task", help="change the coder model of a task that has not started, or of a paused run")
    models_task.add_argument("task_id", metavar="task_id", help="task id, for example T2")
    models_task.add_argument("model")
    models_task.add_argument("effort", nargs="?", help="keeps the task's current effort when left out")

    sub.add_parser("pause", help="stop launching new work")
    sub.add_parser("resume", help="continue a paused run")

    log = sub.add_parser("log", help="show the event log")
    log.add_argument("task", nargs="?", help="only events for this task id")
    return parser


COMMANDS = {
    "init": cmd_init,
    "run": cmd_run,
    "status": cmd_status,
    "repos": cmd_repos,
    "publish": cmd_publish,
    "usage": cmd_usage,
    "questions": cmd_questions,
    "answer": cmd_answer,
    "models": cmd_models,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "log": cmd_log,
}


def with_default_command(parser: argparse.ArgumentParser, argv: Sequence[str]) -> list[str]:
    """`wf "some goal"` means `wf run "some goal"`: a first word that is neither a command nor an option is a goal."""
    args = list(argv)
    commands = next(a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    if args and not args[0].startswith("-") and args[0] not in commands:
        return ["run", *args]
    return args


def main(argv: Sequence[str] | None = None, *, home: Path | None = None, clock: Any = None) -> int:
    """Run one CLI command and return its exit code.

    0 ok, paused or done. 1 failed run or other error. 2 usage error. 3 preflight or config problem.
    4 another workforce process holds the state lock. `home` and `clock` (needs `.time()` and `.sleep()`) are
    injected by tests.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(with_default_command(parser, sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    console = render_text.make_console()
    try:
        with shutdown_guard(lambda: [pause_interrupted(orch) for orch in list(_orchestrators)]):
            clock = clock if clock is not None else time
            if args.command == "init":
                ctx = Context(repo=Path.cwd(), home=home, console=console, clock=clock)
            else:
                ws = resolve_workspace()
                ctx = Context(repo=ws.root, home=home, console=console, clock=clock, ws=ws)
            handler = COMMANDS.get(args.command)
            return handler(ctx, args) if handler else open_console(ctx)
    except (ConfigError, PreflightError) as exc:
        console.print(f"✗ {render_text.sanitize(exc)}", markup=False, soft_wrap=True, style="red")
        return EXIT_PREFLIGHT
    except StateLockedError as exc:
        console.print(f"✗ {render_text.sanitize(exc)}", markup=False, soft_wrap=True, style="red")
        return EXIT_LOCKED
    except (GitError, WorkforceError) as exc:
        console.print(f"✗ {render_text.sanitize(exc)}", markup=False, soft_wrap=True, style="red")
        return EXIT_FAILED
    except KeyboardInterrupt:
        console.print()
        console.print("Interrupted. State is saved; continue with: workforce run --resume", markup=False)
        return EXIT_INTERRUPTED
    finally:
        close_orchestrators()


if __name__ == "__main__":
    sys.exit(main())
