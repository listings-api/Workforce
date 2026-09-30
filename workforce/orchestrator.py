"""The v1 sequential loop: plan, debate, model pick, then code -> check -> review -> commit -> merge per task.

`step()` performs exactly one state transition, so the loop is deterministic and restart-safe:
the durable position is `state.json` plus a small sidecar (`runs/<run_id>/orchestrator.json`) for
loop bookkeeping that has no home in the state dataclasses (planner session, debate progress,
pending coder feedback, retry counters, question context).
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from workforce import checks, commit, git_ops, prompts, review, schemas
from workforce import config as config_mod
from workforce import workspace as ws_mod
from workforce.agents import guard
from workforce.agents.base import AgentRequest, AgentResult, AgentRunner
from workforce.config import EFFORTS, Config
from workforce.decider import questions
from workforce.decider.base import Decider
from workforce.decider.laya import can_answer, confident
from workforce.errors import GitError, WorkforceError
from workforce.events import EventLog
from workforce.paths import Paths
from workforce.state import IntegrationInfo, Question, Run, StateStore, Task
from workforce.usage.monitor import USAGE_PAUSE_PREFIX, UsageMonitor
from workforce.workspace import RepoInfo, Workspace

INFRA_RETRIES = 2
BACKOFF_BASE_S = 5
BACKOFF_MAX_S = 60
OVERVIEW_MAX_CHARS = 4000
OVERVIEW_FILES = 40
OVERVIEW_README_LINES = 30
PHASE_PREPARING = "preparing"
PHASE_INTEGRATING = "integrating"
PHASE_NOTES = {
    PHASE_PREPARING: "preparing the integration branches",
    PHASE_INTEGRATING: "all tasks merged; reviewing how the repos fit together",
}
SUMMARY_MAX_CHARS = 400
STAT_MAX_CHARS = 600
STATE_SNIPPET_CHARS = 900
PROGRESS_LOG_LINES = 40
PROGRESS_FLAGS = ("stuck", "off_task")
ACTIVE_TASK_STATUSES = ("coding", "testing", "reviewing", "fixing", "awaiting_commit", "committing")
PAUSED_STATUSES = ("paused", "awaiting_models", "awaiting_user", "done", "failed")
YES_WORDS = frozenset({"", "y", "yes", "ok", "okay", "go", "start", "continue", "proceed"})
NO_WORDS = frozenset({"n", "no", "stop", "pause", "wait", "hold"})
TASK_ID = re.compile(r"T[0-9]{1,3}")
NEEDS_HUMAN_QUESTION = "Can the agents settle this themselves, or does it need the user?"
CONVERGED_QUESTION = "Do these two positions now agree?"


class HeadMoved(WorkforceError):
    """An agent other than the committer moved HEAD during a run (SPEC rule 3)."""


@dataclass
class StepOutcome:
    """What one `step()` did. `progressed` is False when the run is waiting on someone else."""

    action: str
    progressed: bool
    task_id: str | None = None
    detail: str = ""


def _allow() -> str | None:
    return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- (none)"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _log_line_text(line: str) -> str:
    """A readable one-liner for a raw agent-stream line (Claude or Codex JSONL); unparseable lines pass through."""
    try:
        event = json.loads(line)
    except ValueError:
        return line.strip()
    if not isinstance(event, dict):
        return line.strip()
    message = event.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        parts = []
        for item in message["content"]:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "tool_use":
                parts.append(f"tool {item.get('name')}: {json.dumps(item.get('input'))}")
        if parts:
            return f"{event.get('type')}: " + " ".join(parts)
    item = event.get("item")
    if isinstance(item, dict) and item.get("text"):
        return f"{item.get('type')}: {item['text']}"
    if event.get("type") == "result":
        return f"result: {event.get('result', '')}"
    return str(event.get("type") or line.strip())


def validate_plan(plan: dict) -> str | None:
    """Return a description of the first structural problem in a plan, or None when it is sound."""
    tasks = plan.get("tasks") or []
    if not tasks:
        return "the plan has no tasks"
    ids = [task["id"] for task in tasks]
    for task_id in ids:
        if not TASK_ID.fullmatch(task_id):
            return f"task id {task_id!r} is not of the form T1, T2, ... (T followed by 1 to 3 digits)"
    if len(set(ids)) != len(ids):
        return "task ids are not unique"
    known = set(ids)
    for task in tasks:
        if not task["title"].strip():
            return f"task {task['id']} has no title"
        if not task["acceptance"]:
            return f"task {task['id']} has no acceptance criteria"
        for dep in task["depends_on"]:
            if dep not in known:
                return f"task {task['id']} depends on unknown task {dep}"
            if dep == task["id"]:
                return f"task {task['id']} depends on itself"
    remaining = {task["id"]: set(task["depends_on"]) for task in tasks}
    while remaining:
        ready = [tid for tid, deps in remaining.items() if not deps]
        if not ready:
            return "task dependencies form a cycle"
        for tid in ready:
            del remaining[tid]
        for deps in remaining.values():
            deps.difference_update(ready)
    return None


LANGUAGE_MARKERS = (
    ("pyproject.toml", "Python"),
    ("requirements.txt", "Python"),
    ("setup.py", "Python"),
    ("package.json", "JavaScript/TypeScript"),
    ("go.mod", "Go"),
    ("Cargo.toml", "Rust"),
    ("Gemfile", "Ruby"),
    ("pom.xml", "Java"),
    ("build.gradle", "Java/Kotlin"),
    ("composer.json", "PHP"),
)


def _git_text(path: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _base_ref_name(info: RepoInfo) -> str:
    if info.base:
        return info.base
    branch = (_git_text(info.path, "symbolic-ref", "--quiet", "--short", "HEAD") or "").strip()
    return branch or "HEAD"


def _repo_section(info: RepoInfo, limit: int) -> str:
    base = _base_ref_name(info)
    ref = base if _git_text(info.path, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}") else "HEAD"
    listing = (_git_text(info.path, "ls-tree", "--name-only", ref) or "").splitlines()
    readme_name = next((n for n in listing if n.lower().startswith("readme")), None)
    readme = (_git_text(info.path, "show", f"{ref}:{readme_name}") or "").splitlines() if readme_name else []
    languages = sorted({lang for marker, lang in LANGUAGE_MARKERS if marker in listing})
    configured = {"test": info.checks.test, "lint": info.checks.lint, "build": info.checks.build}
    found = {k: v for k, v in checks.detect(info.path, configured, env_root=info.path).items() if v}
    check_text = "; ".join(f"{kind}: {' && '.join(cmds)}" for kind, cmds in found.items()) or "none detected"

    def render(files_n: int, readme_n: int) -> str:
        shown = listing[:files_n]
        more = f" (+{len(listing) - files_n} more)" if len(listing) > files_n else ""
        lines = [
            f"### {info.name}",
            f"Path: {info.path}",
            f"Base: {base}",
            f"Languages: {', '.join(languages) or 'unknown'}",
            f"Checks: {check_text}",
            f"Top-level files: {', '.join(shown) or '(none)'}{more}",
        ]
        if readme_n and readme:
            lines += [f"README ({readme_name}, first lines):", *[f"  {line}" for line in readme[:readme_n]]]
        return "\n".join(lines)

    files_n, readme_n = min(len(listing), OVERVIEW_FILES), min(len(readme), OVERVIEW_README_LINES)
    text = render(files_n, readme_n)
    while len(text) > limit and (files_n or readme_n):
        if readme_n:
            readme_n //= 2
        else:
            files_n //= 2
        text = render(files_n, readme_n)
    return _clip(text, limit)


def repo_overview(workspace: Workspace) -> str:
    """What the planner and plan reviewer are told about the workspace, at most 4000 characters.

    Per repo: name, path, base ref, languages, detected checks, top-level files (up to 40) and the first 30 README
    lines. Repos are trimmed evenly to share the budget.
    """
    head = [f"Workspace root: {workspace.root}"]
    if workspace.single:
        head.append("This is a single git repo.")
    else:
        head.append(f"{len(workspace.repos)} git repos: {', '.join(workspace.repos)}.")
    if workspace.focus:
        head.append(f"The user started wf inside `{workspace.focus}`.")
    header = "\n".join(head)
    count = max(1, len(workspace.repos))
    per_repo = max(0, (OVERVIEW_MAX_CHARS - len(header) - 2 * count) // count)
    sections = [_repo_section(info, per_repo) for info in workspace.repos.values()]
    return _clip("\n\n".join([header, *sections]), OVERVIEW_MAX_CHARS)


class Orchestrator:
    """Drives one run through plan, debate, model pick and the per-task loop.

    `target` is a `Workspace`, or a path that is loaded with `workspace.load_workspace` (a single repo
    when `workforce.toml` has no `[workspace]`). `root` is the workspace root, where `.workforce/` lives.
    """

    def __init__(
        self,
        target: Path | Workspace,
        config: Config,
        runners: dict[str, AgentRunner],
        decider: Decider,
        clock: Any = time,
        usage_monitor: UsageMonitor | None = None,
        usage_gate: Callable[[], str | None] | None = None,
        home: Path | None = None,
        environ: dict[str, str] | None = None,
    ):
        self.config = config
        if isinstance(target, Workspace):
            self.workspace = target
        else:
            self.workspace = ws_mod.load_workspace(Path(target).resolve(), config)
        self.root = Path(self.workspace.root).resolve()
        self.repo = self.root
        self.runners = runners
        self.decider = decider
        self.clock = clock
        self.paths = Paths(self.root, home=home)
        self.store = StateStore(self.paths)
        self.events = EventLog(self.paths)
        self.usage_monitor = usage_monitor
        self.usage_gate = usage_gate or (usage_monitor.gate if usage_monitor is not None else _allow)
        self._meta_lock = threading.RLock()
        self._size_hints: dict[tuple, str | None] = {}
        self._task_repos: dict[str, str] = {}
        self.environ = os.environ if environ is None else environ
        self.managed_root = self.paths.home / ".workforce" / "worktrees"

    def start(self, goal: str, mode: str = "auto") -> Run:
        """Preflight, then create a fresh run in `planning`. Raises PreflightError before doing anything."""
        self._preflight()
        existing = self.store.load()
        if existing is not None and existing.status not in ("done", "failed"):
            raise WorkforceError(
                f"run {existing.id} is still {existing.status}; resume it or finish it before starting another"
            )
        self.paths.ensure()
        if not self.paths.decisions_md.exists():
            _atomic_write(self.paths.decisions_md, "")
        now = time.gmtime(self.clock.time())
        run_id = time.strftime("%Y%m%d-%H%M%S", now)
        slug = ws_mod.slugify(goal, date(now.tm_year, now.tm_mon, now.tm_mday))
        self._task_repos.clear()
        run = Run.create(run_id, goal, mode, slug=slug)
        self.store.save(run)
        self._save_meta(run_id, self._new_meta())
        self._emit("run_started", run=run_id, goal=goal, mode=mode)
        return run

    def resume(self) -> Run:
        """Reload workforce.toml, preflight, then clear whatever stop the run is in if it is safe to.

        A usage pause only ends if the usage monitor (when there is one) takes fresh readings that are
        all below the pause line; otherwise this raises with the current readings.
        """
        self._reload_config()
        self._preflight()
        run = self._load()
        if run.status == "paused":
            self._check_usage_resume(run)
            return self._unpause(run)
        if run.status == "awaiting_models":
            missing = [t.id for t in run.tasks if not (t.model and t.effort and t.agent)]
            if missing:
                raise WorkforceError(f"tasks {', '.join(missing)} have no model chosen; call set_models first")

            def fn(r: Run) -> None:
                r.status = "running"

            run = self.store.update(fn)
            self._emit("resumed", run=run.id, status="running")
        elif run.status == "awaiting_user" and not any(q.status == "open" for q in run.questions):

            def fn(r: Run) -> None:
                r.status = "running"

            run = self.store.update(fn)
            self._emit("resumed", run=run.id, status="running")
        return run

    def set_mode(self, mode: str) -> Run:
        """Switch between `auto` and `step` while a run is active."""

        def fn(run: Run) -> None:
            run.mode = mode

        run = self.store.update(fn)
        self._emit("note", text=f"mode set to {mode}")
        return run

    def set_models(self, task_id: str, model: str, effort: str) -> Task:
        """Store the user's coder model and effort for one task (only while models are being picked)."""
        run = self._load()
        if run.status != "awaiting_models":
            raise WorkforceError(f"models can only be set while the run is awaiting_models (it is {run.status})")
        agent = self.config.role("coder").agent
        if not model.strip():
            raise WorkforceError("model must not be empty")
        if effort not in EFFORTS[agent]:
            raise WorkforceError(f"effort '{effort}' is not valid for {agent}; expected one of {list(EFFORTS[agent])}")
        run.task(task_id)

        def fn(r: Run) -> None:
            task = r.task(task_id)
            task.model, task.effort, task.agent = model, effort, agent

        run = self.store.update(fn)
        self._emit("note", text=f"{task_id} will use {agent} {model} · {effort}")
        return run.task(task_id)

    def answer(self, question_id: str, text: str) -> Run:
        """Record the user's answer, add it to decisions.md and release whatever the question held."""
        with self._meta_lock:
            run = self._load()
            question = run.question(question_id)
            if question.status != "open":
                raise WorkforceError(f"{question_id} is already answered")
            meta = self._meta(run.id)
            context = meta["questions"].get(question_id, {"kind": "generic"})
            kind = context["kind"]
            cleaned = text.strip()
            declined = kind == "confirm" and cleaned.lower() in NO_WORDS
            if not (kind == "confirm" and cleaned.lower() in YES_WORDS | NO_WORDS):
                self._append_decision(question_id, question.text, cleaned)

            def edit(m: dict) -> None:
                task_id = context.get("task_id")
                if kind == "confirm" and not declined and task_id not in m["confirmed"]:
                    m["confirmed"].append(task_id)
                if kind == "infra_run":
                    m["plan_failures"] = 0
                if kind == "plan_invalid":
                    m["repo_rejects"] = 0
                if kind == "integration_rounds":
                    m.setdefault("integration", self._new_integration_meta())["round"] = 0
                if not task_id:
                    return
                info = self._task_meta(m, task_id)
                if context.get("feedback") is not None:
                    info["feedback"] = f"{context['feedback']}\n\nThe user answered {question_id}: {cleaned}".strip()
                if kind == "checks_failed":
                    info["check_fixes"] = 0
                elif kind == "commit_failed":
                    info["commit_failures"] = 0
                elif kind == "tree_mismatch":
                    info["drift"] = 0

            self._meta_edit(run.id, edit)

            held: dict[str, str] = {}

            def fn(r: Run) -> None:
                q = r.question(question_id)
                q.answer, q.status = cleaned, "answered"
                if any(other.status == "open" for other in r.questions):
                    return
                task_id = context.get("task_id")
                if task_id:
                    task = r.task(task_id)
                    if context.get("resume_to"):
                        task.status = context["resume_to"]
                    task.infra_failures = 0
                    if kind in ("review_rounds", "blocked", "tree_mismatch"):
                        task.review_round = 0
                    target = "running"
                elif kind == "plan_debate":
                    target = "awaiting_models"
                elif kind == "plan_open_question":
                    target = "debating" if meta.get("agent_questions") else "awaiting_models"
                elif context.get("resume_run"):
                    target = context["resume_run"]
                else:
                    target = "running"
                if r.status == "paused":
                    held["target"] = target
                else:
                    r.status = target

            run = self.store.update(fn)
            if held:
                self._meta_edit(run.id, lambda m: m.__setitem__("paused_from", held["target"]))
        self._emit("answered", question=question_id, answer=cleaned)
        if declined:
            return self.pause(f"declined to start {context.get('task_id')}")
        return run

    def pause(self, reason: str) -> Run:
        """Stop launching new work. An agent already running is left to finish."""
        with self._meta_lock:
            run = self._load()
            if run.status in ("done", "failed"):
                return run
            if run.status != "paused":
                self._meta_edit(run.id, lambda m: m.__setitem__("paused_from", run.status))

            def fn(r: Run) -> None:
                r.status = "paused"
                r.pause_reason = reason

            run = self.store.update(fn)
        self._emit("paused", run=run.id, reason=reason)
        return run

    def step(self) -> StepOutcome:
        """Advance exactly one state transition and say what happened."""
        run = self._load()
        if run.status in PAUSED_STATUSES:
            return StepOutcome(run.status, progressed=False)
        try:
            if run.status == "planning":
                return self._plan(run)
            if run.status == "debating":
                return self._debate(run)
            return self._advance(run)
        except HeadMoved as exc:
            return self._head_moved(run, None, str(exc))
        except GitError as exc:
            self._emit("error", run=run.id, message=f"git error: {exc}")
            self.pause(f"git error: {exc}")
            return StepOutcome("paused", progressed=True, detail=str(exc))

    def run_until_blocked(self, max_steps: int | None = None) -> Run:
        """Step until the run needs the user, is paused, or finishes. Holds the runner lock throughout."""
        with self.store.lock():
            taken = 0
            while max_steps is None or taken < max_steps:
                if not self.step().progressed:
                    break
                taken += 1
        return self._load()

    def _reload_config(self) -> None:
        if not self.paths.config.exists():
            return
        self.config = config_mod.load(self.root)
        self.workspace = dataclasses.replace(
            ws_mod.load_workspace(self.root, self.config), focus=self.workspace.focus
        )
        if self.usage_monitor is not None:
            self.usage_monitor.config = self.config

    def _check_usage_resume(self, run: Run) -> None:
        monitor = self.usage_monitor
        if monitor is None or not (run.pause_reason or "").startswith(USAGE_PAUSE_PREFIX):
            return
        if not monitor.can_resume():
            windows = ", ".join(
                f"{w['source']} {w['name']} {int(w['percent'])}% ({w['freshness']})"
                for w in monitor.readings()["windows"]
            )
            raise WorkforceError(
                f"usage is still at or above the pause line ({run.pause_reason}); current readings: "
                f"{windows or 'none'}"
            )
        monitor.clear_pause()

    def poll_tick(self) -> str:
        """One usage poll for the CLI or console to call on a timer; returns "none", "pause" or "resume"."""
        monitor = self.usage_monitor
        if monitor is None:
            return "none"
        run = self.store.load()
        if run is None or run.status in ("done", "failed"):
            return "none"
        usage_paused = run.status == "paused" and (run.pause_reason or "").startswith(USAGE_PAUSE_PREFIX)
        plan = monitor.resume_plan() if usage_paused else None
        action = monitor.tick(paused=run.status == "paused", pause_reason=run.pause_reason)
        if action == "none" and plan is not None and plan.auto and self.clock.time() >= plan.at:
            if not monitor.resume_plan().blocking_windows and monitor.can_resume():
                monitor.clear_pause()
                action = "resume"
        if action == "pause":
            self.pause(monitor.pause_reason or f"{USAGE_PAUSE_PREFIX} usage limit")
        elif action == "resume":
            self._unpause(self._load())
        return action

    def _current_step_log(self, run_id: str, task_id: str, step: str) -> Path | None:
        directory = self.paths.run_dir(run_id) / task_id
        found = sorted(
            directory.glob(f"{step}-*.jsonl"), key=lambda path: int(path.stem.rsplit("-", 1)[1])
        ) if directory.exists() else []
        return found[-1] if found else None

    def progress_tick(self) -> list[dict]:
        """Ask the decider whether each coding/fixing agent is stuck or off task (call every ~3 minutes).

        Reads the last ~40 lines of the task's current step log. A confident "stuck" or "off_task" emits a
        `decision` event with `flag=True` and is returned; nothing is stopped or changed automatically.
        """
        run = self.store.load()
        if run is None or run.status != "running" or not can_answer(self.decider, "agent_progress"):
            return []
        flagged = []
        for task in run.tasks:
            if task.status not in ("coding", "fixing"):
                continue
            log = self._current_step_log(run.id, task.id, "fix" if task.status == "fixing" else "code")
            if log is None:
                continue
            with log.open(encoding="utf-8", errors="replace") as handle:
                tail = collections.deque(handle, maxlen=PROGRESS_LOG_LINES)
            lines = [_log_line_text(line) for line in tail if line.strip()]
            if not lines:
                continue
            state, question, options = questions.agent_progress(lines, task.title)
            decision = self.decider.ask_choice("agent_progress", state, question, options)
            if not (confident(decision, self.config.decider.confidence) and decision.answer in PROGRESS_FLAGS):
                continue
            item = {
                "key": "agent_progress",
                "task": task.id,
                "answer": decision.answer,
                "confidence": decision.confidence,
                "source": decision.source,
                "flag": True,
                "log": str(log),
            }
            self._emit("decision", **item)
            flagged.append(item)
        return flagged

    def task_size_hints(self) -> dict[str, str | None]:
        """A "small"/"big" hint per task for the model-pick screen, or None where Laya is unsure or unavailable.

        Hint text only: it never changes a model or effort. Answers are cached per task title and description.
        """
        run = self._load()
        hints: dict[str, str | None] = {}
        for task in run.tasks:
            hints[task.id] = self._task_size(task)
        return hints

    def _task_size(self, task: Task) -> str | None:
        if not can_answer(self.decider, "task_size"):
            return None
        cache_key = (task.id, task.title, task.description, tuple(task.acceptance))
        if cache_key not in self._size_hints:
            state, question, options = questions.task_size(task.title, task.description, task.acceptance)
            decision = self.decider.ask_choice("task_size", state, question, options)
            usable = confident(decision, self.config.decider.confidence) and decision.answer in options
            self._size_hints[cache_key] = decision.answer if usable else None
        return self._size_hints[cache_key]

    def _preflight(self) -> None:
        """The env, CLI, login and version checks; a lone repo without a configured base must be on a branch.

        Per-repo checks (base resolves, branch name free, worktree path free) run when the integration
        branches are prepared, because only then is it known which repos the plan touches.
        """
        repo = None
        if self.workspace.single:
            only = next(iter(self.workspace.repos.values()))
            repo = only.path if only.base is None else None
        guard.preflight(Path(self.config.bin.claude), Path(self.config.bin.codex), repo, self.environ)

    def _load(self) -> Run:
        run = self.store.load()
        if run is None:
            raise WorkforceError("no run in progress; start one first")
        return run

    def _emit(self, kind: str, **data: Any) -> None:
        task_id = data.get("task")
        if isinstance(task_id, str) and "repo" not in data:
            repo_name = self._repo_of_task_id(task_id)
            if repo_name:
                data["repo"] = repo_name
        self.events.emit(kind, **guard.redacted(data))

    def _single_repo_name(self) -> str | None:
        return next(iter(self.workspace.repos)) if len(self.workspace.repos) == 1 else None

    def _task_repo(self, task: Task) -> str:
        """The repo a task belongs to; a task from an old state file (no repo) belongs to the single repo."""
        if task.repo:
            return task.repo
        only = self._single_repo_name()
        if only is None:
            raise WorkforceError(f"task {task.id} has no repo and this workspace has several")
        return only

    def _repo_of_task_id(self, task_id: str) -> str | None:
        cached = self._task_repos.get(task_id)
        if cached:
            return cached
        try:
            run = self.store.load()
            task = next((t for t in run.tasks if t.id == task_id), None) if run else None
        except WorkforceError:
            return None
        if task is None:
            return None
        found = task.repo or self._single_repo_name()
        if found:
            self._task_repos[task_id] = found
        return found

    def _repo_info(self, task: Task) -> RepoInfo:
        return self.workspace.repo(self._task_repo(task))

    def _integration_info(self, run: Run, repo_name: str) -> IntegrationInfo:
        try:
            return run.integration[repo_name]
        except KeyError:
            raise WorkforceError(f"the integration branch for repo '{repo_name}' has not been prepared yet") from None

    def _integration_wt(self, run: Run, task: Task) -> Path:
        return Path(self._integration_info(run, self._task_repo(task)).worktree)

    def _new_meta(self) -> dict:
        return {
            "planner_session": None,
            "plan": None,
            "plan_failures": 0,
            "debate": {"round": 0, "phase": "review", "objections": [], "suggested": [], "log": []},
            "tasks": {},
            "questions": {},
            "confirmed": [],
            "merged": {},
            "extended": [],
            "agent_questions": [],
            "open_questions_done": False,
            "paused_from": None,
            "plan_problem": "",
            "repo_rejects": 0,
            "branches": {},
            "phase": "",
            "integration": self._new_integration_meta(),
        }

    @staticmethod
    def _new_integration_meta() -> dict:
        return {"round": 0, "feedback": "", "planner_failures": 0, "problem": ""}

    @staticmethod
    def _task_meta_defaults() -> dict:
        return {
            "feedback": "",
            "check_fixes": 0,
            "fix_round": 0,
            "commit_failures": 0,
            "drift": 0,
            "summary": "",
            "checks": "",
            "committer_shas": [],
        }

    def _meta_path(self, run_id: str) -> Path:
        return self.paths.run_dir(run_id) / "orchestrator.json"

    def _meta(self, run_id: str) -> dict:
        return json.loads(self._meta_path(run_id).read_text())

    def _save_meta(self, run_id: str, meta: dict) -> None:
        _atomic_write(self._meta_path(run_id), json.dumps(meta, indent=2))

    def _write_guard(self) -> Any:
        """The state store's cross-process write lock, so `workforce pause` or `answer` in another process cannot be lost."""
        lock = getattr(self.store, "write_lock", None)
        return lock() if lock is not None else contextlib.nullcontext()

    def _meta_edit(self, run_id: str, fn: Callable[[dict], None]) -> dict:
        with self._meta_lock, self._write_guard():
            meta = self._meta(run_id)
            fn(meta)
            self._save_meta(run_id, meta)
            return meta

    def _task_meta(self, meta: dict, task_id: str) -> dict:
        return meta["tasks"].setdefault(task_id, self._task_meta_defaults())

    def _decisions(self) -> str:
        try:
            text = self.paths.decisions_md.read_text().strip()
        except FileNotFoundError:
            text = ""
        return text or "(none yet)"

    def _append_decision(self, question_id: str, question: str, answer: str) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        with self.paths.decisions_md.open("a", encoding="utf-8") as handle:
            handle.write(f"- User answer to {question_id} ({_clip(question, 200)}): {answer}\n")

    def _skip_git_check(self) -> bool:
        return not (self.root / ".git").exists()

    def _plan_schema(self) -> dict:
        return schemas.PLAN if self.workspace.single else schemas.plan_schema(list(self.workspace.repos))

    def _revision_schema(self) -> dict:
        if self.workspace.single:
            return schemas.PLAN_REVISION
        return schemas.plan_revision_schema(list(self.workspace.repos))

    def _repo_rule(self) -> str:
        if self.workspace.single:
            return ""
        return (
            "\n- Every task belongs to exactly one repo: set `repo` to one of these names: "
            f"{', '.join(self.workspace.repos)}. A goal that spans repos becomes tasks in each repo, and `depends_on` "
            "may point at a task in another repo when it needs that work merged first (for example an API before its client)."
        )

    def _repo_field(self) -> str:
        return "" if self.workspace.single else ", `repo`"

    def _repo_problems(self, plan: dict) -> str | None:
        names = list(self.workspace.repos)
        if self.workspace.single:
            wrong = [
                f"task {t.get('id')} names repo {t['repo']!r}; the only repo is {names[0]!r}"
                for t in plan.get("tasks", [])
                if t.get("repo") not in (None, names[0])
            ]
            return "; ".join(wrong) or None
        return "; ".join(schemas.validate_plan_repos(plan, names)) or None

    def _reject_plan(self, run: Run, problem: str, what: str) -> StepOutcome:
        """A plan that names repos wrongly goes back to the planner; it counts as a debate round, not as an infra failure."""
        count: dict[str, int] = {}

        def bump(m: dict) -> None:
            m["repo_rejects"] = m.get("repo_rejects", 0) + 1
            m["plan_problem"] = problem
            count["n"] = m["repo_rejects"]

        self._meta_edit(run.id, bump)
        self._emit("note", text=f"{what} rejected: {problem}")
        if count["n"] > self.config.limits.debate_rounds:
            return self._ask(
                "plan_invalid",
                f"The planner keeps returning tasks with the wrong repos ({problem}). How should we proceed?",
                {},
                resume_run=run.status,
            )
        return StepOutcome("plan_rejected", progressed=True, detail=problem)

    def _log(self, run_id: str, task_id: str, step: str) -> Path:
        directory = self.paths.run_dir(run_id) / task_id
        existing = len(list(directory.glob(f"{step}-*.jsonl"))) if directory.exists() else 0
        return self.paths.step_log(run_id, task_id, step, existing + 1)

    def _gate_reason(self, agents: tuple[str, ...]) -> str | None:
        if self.usage_monitor is not None and "codex" in agents:
            self.usage_monitor.refresh_codex()
        return self.usage_gate()

    def _after_run(self, agent: str, result: AgentResult) -> None:
        if self.usage_monitor is None:
            return
        if agent == "claude":
            self.usage_monitor.ingest_claude(result)
        elif agent == "codex":
            self.usage_monitor.refresh_codex()

    def _gate_blocked(self, *agents: str) -> StepOutcome | None:
        reason = self._gate_reason(agents)
        if reason:
            self.pause(reason)
            return StepOutcome("paused", progressed=True, detail=reason)
        return None

    def _runner(self, agent: str) -> AgentRunner:
        try:
            return self.runners[agent]
        except KeyError:
            raise WorkforceError(f"no runner configured for agent '{agent}'") from None

    def _role_request(
        self,
        role_name: str,
        prompt: str,
        cwd: Path,
        log_path: Path,
        *,
        schema: dict | None = None,
        resume: str | None = None,
        sandbox: str = "workspace-write",
        browser: bool = True,
    ) -> AgentRequest:
        """A request for a role that works at the workspace root (planner, plan reviewer, side runs)."""
        role = self.config.role(role_name)
        return AgentRequest(
            role=role_name,
            agent=role.agent,
            model=role.model,
            effort=role.effort,
            prompt=prompt,
            cwd=cwd,
            schema=schema,
            resume_session=resume,
            sandbox=sandbox,
            browser=browser and getattr(self.config.browser, role.agent),
            log_path=log_path,
            repo=self.root,
            skip_git_check=self._skip_git_check(),
        )

    @staticmethod
    def _head_or_none(cwd: Path) -> str | None:
        try:
            return git_ops.head_sha(cwd)
        except GitError:
            return None

    def _is_managed_worktree(self, cwd: Path) -> bool:
        """True when `cwd` is inside a WorkForce worktree (under the worktrees root), not the workspace or a user checkout."""
        return self.paths.worktrees_root.resolve() in Path(cwd).resolve().parents

    def _launch(self, request: AgentRequest, task_id: str | None, step: str) -> AgentResult:
        """Run one request. Any non-committer run in a managed worktree that moves HEAD raises `HeadMoved`.

        The coder is checked by `_code`. Runs at the workspace root or in the user's own checkout (planner, plan
        reviewer, integration review, side questions) are not HEAD-checked: their read-only sandbox protects them,
        and the user may commit in their own checkout while a run is going.

        A committer run that moves HEAD successfully has its new HEAD recorded in the sidecar as a commit the
        committer made (this is how a rebase becomes a commit `_commit_exists` will accept).
        """
        watched = request.role != "coder" and self._is_managed_worktree(request.cwd)
        head_before = self._head_or_none(request.cwd) if watched else None
        self._emit("agent_event", phase="start", role=request.role, task=task_id, model=request.model, step=step)
        if request.user_setup:
            result, excluded = git_ops.run_excluding_new_untracked(
                request.cwd, lambda: self._runner(request.agent).run(request)
            )
            if excluded:
                self._emit("note", task=task_id, text=f"excluded files created by your Claude setup: {', '.join(excluded)}")
        else:
            result = self._runner(request.agent).run(request)
        self._after_run(request.agent, result)
        self._emit(
            "agent_event",
            phase="end",
            role=request.role,
            task=task_id,
            model=request.model,
            step=step,
            ok=result.ok,
            error_kind=result.error_kind,
        )
        head_after = self._head_or_none(request.cwd) if head_before is not None else None
        if head_after is not None and head_after != head_before:
            if request.role == "committer":
                if result.ok and task_id:
                    self._record_committer_sha(task_id, head_after)
            else:
                raise HeadMoved(
                    f"{request.role} moved HEAD from {head_before[:8]} to {head_after[:8]} during {step}"
                )
        return result

    def _record_committer_sha(self, task_id: str, sha: str) -> None:
        def edit(m: dict) -> None:
            shas = self._task_meta(m, task_id).setdefault("committer_shas", [])
            if sha not in shas:
                shas.append(sha)

        self._meta_edit(self._load().id, edit)

    def _head_moved(self, run: Run, task: Task | None, detail: str) -> StepOutcome:
        text = (
            f"An agent other than the committer moved HEAD ({detail}). Only the committer may commit, so nothing "
            "from that run counts and any approvals are void. Inspect the branch, undo the stray commit, "
            "and tell us how to proceed."
        )
        self._emit("error", run=run.id, task=task.id if task else None, message=f"approvals voided: {detail}")
        if task is None:
            return self._ask("head_moved", text, {}, resume_run=run.status)

        def void(r: Run) -> None:
            r.task(task.id).reviews = []

        self.store.update(void)
        self._meta_edit(run.id, lambda m: self._task_meta(m, task.id).pop("review_progress", None))
        return self._ask("head_moved", text, {}, task_id=task.id, resume_to="testing")

    def _set_task(self, task_id: str, **fields: Any) -> Task:
        previous: dict[str, str] = {}

        def fn(run: Run) -> None:
            task = run.task(task_id)
            previous["status"] = task.status
            for key, value in fields.items():
                setattr(task, key, value)

        run = self.store.update(fn)
        if "status" in fields and fields["status"] != previous["status"]:
            self._emit("task_status", task=task_id, status=fields["status"], previous=previous["status"])
        return run.task(task_id)

    def _set_run_status(self, status: str) -> Run:
        """Move the run to `status`, unless it is paused: then the pause stays and `resume()` goes to `status`."""
        with self._meta_lock:
            held = {}

            def fn(run: Run) -> None:
                if run.status == "paused":
                    held["paused"] = True
                else:
                    run.status = status

            run = self.store.update(fn)
            if held:
                self._meta_edit(run.id, lambda m: m.__setitem__("paused_from", status))
        return run

    def _unpause(self, run: Run) -> Run:
        with self._meta_lock:
            run = self._load()
            meta = self._meta(run.id)
            target = meta.get("paused_from") or "running"
            if target == "awaiting_user" and not any(q.status == "open" for q in run.questions):
                target = "running"

            def fn(r: Run) -> None:
                r.status = target
                r.pause_reason = None

            run = self.store.update(fn)
            self._meta_edit(run.id, lambda m: m.__setitem__("paused_from", None))
        self._emit("resumed", run=run.id, status=target)
        return run

    def _pause_step(self, reason: str) -> StepOutcome:
        self.pause(reason)
        return StepOutcome("paused", progressed=True, detail=reason)

    def _ask(
        self,
        kind: str,
        text: str,
        positions: dict[str, str],
        *,
        task_id: str | None = None,
        resume_to: str | None = None,
        feedback: str | None = None,
        resume_run: str | None = None,
    ) -> StepOutcome:
        with self._meta_lock:
            question_id = self.store.next_question_id()
            run = self._load()
            self._meta_edit(
                run.id,
                lambda m: m["questions"].__setitem__(
                    question_id,
                    {
                        "kind": kind,
                        "task_id": task_id,
                        "resume_to": resume_to,
                        "feedback": feedback,
                        "resume_run": resume_run,
                    },
                ),
            )
            held: dict[str, bool] = {}

            def fn(r: Run) -> None:
                r.questions.append(Question(id=question_id, task_id=task_id, text=text, positions=positions))
                if r.status == "paused":
                    held["paused"] = True
                else:
                    r.status = "awaiting_user"
                if task_id and resume_to is not None:
                    r.task(task_id).status = "blocked"

            self.store.update(fn)
            if held:
                self._meta_edit(run.id, lambda m: m.__setitem__("paused_from", "awaiting_user"))
        self._emit(
            "question", question=question_id, task=task_id, text=text, positions=positions, question_kind=kind
        )
        return StepOutcome("question", progressed=True, task_id=task_id, detail=question_id)

    def _register_failure(self, run: Run, task: Task | None, message: str) -> StepOutcome:
        self._emit("error", run=run.id, task=task.id if task else None, message=message)
        if task is not None:
            with self._meta_lock:
                failures = self._load().task(task.id).infra_failures + 1
                self._set_task(task.id, infra_failures=failures)
        else:
            count: dict[str, int] = {}

            def bump(m: dict) -> None:
                m["plan_failures"] += 1
                count["n"] = m["plan_failures"]

            self._meta_edit(run.id, bump)
            failures = count["n"]
        if failures > INFRA_RETRIES:
            text = f"{message}. It failed {failures} times in a row; how should we proceed?"
            if task is not None:
                return self._ask("infra", text, {}, task_id=task.id, resume_to=task.status)
            return self._ask("infra_run", text, {}, resume_run=run.status)
        self.clock.sleep(min(BACKOFF_MAX_S, BACKOFF_BASE_S * 4 ** (failures - 1)))
        return StepOutcome("retry", progressed=True, task_id=task.id if task else None, detail=message)

    def _handle_failure(self, run: Run, task: Task | None, request: AgentRequest, result: AgentResult) -> StepOutcome:
        if result.error_kind == "model_unavailable":
            return self._pause_step(f"model_unavailable: {request.model}")
        if guard.is_subscription_violation(result):
            self._emit("error", run=run.id, task=task.id if task else None, message=result.error)
            return self._pause_step(result.error)
        return self._register_failure(
            run, task, f"{request.role} failed ({result.error_kind or 'error'}): {result.error or 'no detail'}"
        )

    def _decider_says_agents(self, scope: str, state: str) -> bool:
        if not can_answer(self.decider, "needs_human"):
            return False
        decision = self.decider.ask_choice(
            "needs_human", _clip(state, STATE_SNIPPET_CHARS), NEEDS_HUMAN_QUESTION, ["agents", "user"]
        )
        self._emit(
            "decision",
            key="needs_human",
            scope=scope,
            answer=decision.answer,
            confidence=decision.confidence,
            source=decision.source,
        )
        return confident(decision, self.config.decider.confidence) and decision.answer == "agents"

    def _agents_can_settle(self, run: Run, scope: str, state: str) -> bool:
        if scope in self._meta(run.id)["extended"]:
            return False
        if self._decider_says_agents(scope, state):
            self._meta_edit(run.id, lambda m: m["extended"].append(scope))
            return True
        return False

    def _plan_markdown(self, run: Run, plan: dict, log: list[dict]) -> str:
        lines = ["# Plan", "", f"Goal: {run.goal}", ""]
        groups: list[tuple[str | None, list[dict]]]
        if self.workspace.single:
            groups = [(None, plan["tasks"])]
        else:
            groups = [
                (name, [t for t in plan["tasks"] if t.get("repo") == name]) for name in self.workspace.repos
            ]
            groups = [(name, tasks) for name, tasks in groups if tasks]
        for name, tasks in groups:
            lines += [f"## Repo: {name}" if name else "## Tasks", ""]
            for task in tasks:
                lines += [f"### {task['id']}: {task['title']}", "", task["description"], ""]
                lines.append(f"Depends on: {', '.join(task['depends_on']) or 'nothing'}")
                lines += ["", "Acceptance:", *[f"- {item}" for item in task["acceptance"]], ""]
        lines += ["## Risks", "", _bullets(plan["risks"]), "", "## Open questions", "", _bullets(plan["open_questions"]), ""]
        if log:
            lines += ["## Debate", ""]
            for entry in log:
                lines += [
                    f"### Round {entry['round']}",
                    "",
                    "Reviewer objections:",
                    _bullets(entry["objections"]),
                    "",
                    f"Planner position: {entry['position']}",
                    "",
                    "Concessions:",
                    _bullets(entry["concessions"]),
                    "",
                    "Remaining disagreements:",
                    _bullets(entry["remaining"]),
                    "",
                ]
        return "\n".join(lines)

    def _apply_plan(self, run: Run, plan: dict, log: list[dict], status: str | None = None) -> None:
        _atomic_write(self.paths.plan_md, self._plan_markdown(run, plan, log))
        held: dict[str, bool] = {}

        def fn(r: Run) -> None:
            r.tasks = [
                Task(
                    id=item["id"],
                    title=item["title"],
                    description=item["description"],
                    acceptance=list(item["acceptance"]),
                    depends_on=list(item["depends_on"]),
                    repo=item.get("repo") or self._single_repo_name(),
                )
                for item in plan["tasks"]
            ]
            if status:
                if r.status == "paused":
                    held["paused"] = True
                else:
                    r.status = status

        self._task_repos.clear()
        with self._meta_lock:
            updated = self.store.update(fn)
            if held:
                self._meta_edit(updated.id, lambda m: m.__setitem__("paused_from", status))

    def _previous_problem(self, run_id: str) -> str:
        problem = self._meta(run_id).get("plan_problem")
        return f"Your previous reply was rejected: {problem}. Fix it and reply again." if problem else "(none)"

    def _plan(self, run: Run) -> StepOutcome:
        blocked = self._gate_blocked(self.config.role("planner").agent)
        if blocked:
            return blocked
        request = self._role_request(
            "planner",
            prompts.render(
                "planner",
                decisions=self._decisions(),
                goal=run.goal,
                repo_overview=repo_overview(self.workspace),
                repo_rule=self._repo_rule(),
                repo_field=self._repo_field(),
                previous_problem=self._previous_problem(run.id),
            ),
            self.root,
            self._log(run.id, "plan", "plan"),
            schema=self._plan_schema(),
            sandbox="read-only",
        )
        result = self._launch(request, None, "plan")
        if not result.ok:
            return self._handle_failure(run, None, request, result)
        problem = validate_plan(result.structured)
        if problem:
            self._meta_edit(run.id, lambda m: m.__setitem__("plan_problem", problem))
            return self._register_failure(run, None, f"planner returned an invalid plan: {problem}")
        repo_problem = self._repo_problems(result.structured)
        if repo_problem:
            return self._reject_plan(run, repo_problem, "the plan")
        self._apply_plan(run, result.structured, [], status="debating")

        def edit(m: dict) -> None:
            m["planner_session"] = result.session_id
            m["plan"] = result.structured
            m["plan_failures"] = 0
            m["repo_rejects"] = 0
            m["plan_problem"] = ""

        self._meta_edit(run.id, edit)
        self._emit("plan_ready", run=run.id, tasks=len(result.structured["tasks"]), round=0)
        return StepOutcome("plan", progressed=True)

    def _debate(self, run: Run) -> StepOutcome:
        debate = self._meta(run.id)["debate"]
        if debate["phase"] == "review":
            return self._debate_review(run)
        return self._debate_revise(run)

    def _debate_review(self, run: Run) -> StepOutcome:
        blocked = self._gate_blocked(self.config.role("plan_reviewer").agent)
        if blocked:
            return blocked
        meta = self._meta(run.id)
        request = self._role_request(
            "plan_reviewer",
            prompts.render(
                "plan_reviewer",
                decisions=self._decisions(),
                goal=run.goal,
                round=meta["debate"]["round"] + 1,
                open_questions=_bullets(meta.get("agent_questions", [])),
                plan=json.dumps(meta["plan"], indent=2),
                repo_overview=repo_overview(self.workspace),
            ),
            self.root,
            self._log(run.id, "plan", "plan_review"),
            schema=schemas.PLAN_REVIEW,
            sandbox="read-only",
            browser=False,
        )
        result = self._launch(request, None, "plan_review")
        if not result.ok:
            return self._handle_failure(run, None, request, result)
        verdict = result.structured
        rounds = meta["debate"]["round"]
        self._emit(
            "debate_round",
            run=run.id,
            round=rounds + 1,
            speaker="plan_reviewer",
            agree=verdict["agree"],
            objections=verdict["objections"],
        )
        def reset(m: dict) -> None:
            m["plan_failures"] = 0
            m["agent_questions"] = []

        self._meta_edit(run.id, reset)
        if verdict["agree"]:
            return self._finish_plan(run)

        debate = self._meta(run.id)["debate"]
        cap = self.config.limits.debate_rounds + (1 if "plan" in self._meta(run.id)["extended"] else 0)
        if rounds >= cap:
            last = debate["log"][-1]
            snapshot = (
                f"Final review after {rounds} revisions. Reviewer objections: "
                f"{'; '.join(verdict['objections'])}. Planner position: {last['position']}. "
                f"Remaining disagreements: {'; '.join(last['remaining']) or 'none'}."
            )
            if not self._agents_can_settle(run, "plan", snapshot):
                positions = {
                    "claude": "Plan reviewer objections (final review):\n" + _bullets(verdict["objections"]),
                    "codex": f"Planner position: {last['position']}\nStill disputed:\n"
                    + _bullets(last["remaining"]),
                }
                return self._ask(
                    "plan_debate",
                    f"The plan reviewer still disagrees after {rounds} revisions and a final review. "
                    "Answer with how you want the plan to go ahead.",
                    positions,
                    resume_run="debating",
                )

        def edit(m: dict) -> None:
            m["debate"]["phase"] = "revise"
            m["debate"]["objections"] = verdict["objections"]
            m["debate"]["suggested"] = verdict["suggested_changes"]

        self._meta_edit(run.id, edit)
        return StepOutcome("plan_objections", progressed=True)

    def _finish_plan(self, run: Run) -> StepOutcome:
        meta = self._meta(run.id)
        questions = (meta["plan"] or {}).get("open_questions") or []
        if meta.get("open_questions_done") or not questions:
            self._set_run_status("awaiting_models")
            self._emit("note", text="plan agreed; waiting for the user to pick models")
            return StepOutcome("plan_agreed", progressed=True)
        for_agents = [
            q for q in questions if self._decider_says_agents("plan", f"Goal: {run.goal}\nOpen question: {q}")
        ]
        for_user = [q for q in questions if q not in for_agents]

        def edit(m: dict) -> None:
            m["open_questions_done"] = True
            m["agent_questions"] = for_agents

        self._meta_edit(run.id, edit)
        if not for_user:
            self._emit("note", text="open plan questions go to the plan reviewer for one more round")
            return StepOutcome("open_questions_to_reviewer", progressed=True)
        outcome = StepOutcome("question", progressed=True)
        for question in for_user:
            outcome = self._ask("plan_open_question", f"The planner left an open question: {question}", {})
        return outcome

    def _debate_revise(self, run: Run) -> StepOutcome:
        blocked = self._gate_blocked(self.config.role("planner").agent)
        if blocked:
            return blocked
        meta = self._meta(run.id)
        debate = meta["debate"]
        request = self._role_request(
            "planner",
            prompts.render(
                "planner_revise",
                decisions=self._decisions(),
                goal=run.goal,
                round=debate["round"] + 1,
                plan=json.dumps(meta["plan"], indent=2),
                objections=_bullets(debate["objections"]),
                suggested_changes=_bullets(debate["suggested"]),
                repo_rule=self._repo_rule(),
                previous_problem=self._previous_problem(run.id),
            ),
            self.root,
            self._log(run.id, "plan", "plan_revise"),
            schema=self._revision_schema(),
            resume=meta["planner_session"],
            sandbox="read-only",
        )
        result = self._launch(request, None, "plan_revise")
        if not result.ok:
            return self._handle_failure(run, None, request, result)
        plan, turn = result.structured["plan"], result.structured["debate"]
        problem = validate_plan(plan)
        if problem:
            self._meta_edit(run.id, lambda m: m.__setitem__("plan_problem", problem))
            return self._register_failure(run, None, f"planner returned an invalid revised plan: {problem}")
        repo_problem = self._repo_problems(plan)
        if repo_problem:
            return self._reject_plan(run, repo_problem, "the revised plan")

        entry = {
            "round": debate["round"] + 1,
            "objections": debate["objections"],
            "position": turn["position"],
            "concessions": turn["concessions"],
            "remaining": turn["remaining_disagreements"],
        }
        log = debate["log"] + [entry]
        self._apply_plan(run, plan, log)

        def edit(m: dict) -> None:
            m["plan"] = plan
            m["plan_failures"] = 0
            m["repo_rejects"] = 0
            m["plan_problem"] = ""
            m["planner_session"] = result.session_id or m["planner_session"]
            m["debate"]["round"] += 1
            m["debate"]["phase"] = "review"
            m["debate"]["log"] = log

        self._meta_edit(run.id, edit)
        self._emit(
            "debate_round",
            run=run.id,
            round=entry["round"],
            speaker="planner",
            position=turn["position"],
            remaining=turn["remaining_disagreements"],
        )
        snapshot = (
            f"Round {entry['round']}. Reviewer objections: {'; '.join(entry['objections'])}. "
            f"Planner position: {turn['position']}. Remaining disagreements: "
            f"{'; '.join(turn['remaining_disagreements']) or 'none'}."
        )
        if self._plans_converged(snapshot):
            self._emit("note", text="plan debate converged")
            return self._finish_plan(run)
        return StepOutcome("plan_revised", progressed=True)

    def _plans_converged(self, snapshot: str) -> bool:
        if not can_answer(self.decider, "debate_converged"):
            return False
        decision = self.decider.ask_bool("debate_converged", _clip(snapshot, STATE_SNIPPET_CHARS), CONVERGED_QUESTION)
        self._emit(
            "decision",
            key="debate_converged",
            answer=decision.answer,
            confidence=decision.confidence,
            source=decision.source,
        )
        return confident(decision, self.config.decider.confidence) and decision.answer is True

    def _current_task(self, run: Run) -> Task | None:
        merged = {t.id for t in run.tasks if t.status == "merged"}
        for task in run.tasks:
            if task.status in ACTIVE_TASK_STATUSES:
                return task
        for task in run.tasks:
            if task.status == "pending" and set(task.depends_on) <= merged:
                return task
        return None

    def _task_handlers(self) -> dict[str, Callable[[Run, Task], StepOutcome]]:
        return {
            "pending": self._begin,
            "coding": lambda r, t: self._code(r, t, fixing=False),
            "fixing": lambda r, t: self._code(r, t, fixing=True),
            "testing": self._check,
            "reviewing": self._review,
            "awaiting_commit": self._pre_commit,
            "committing": self._commit,
        }

    def step_task(self, task_id: str) -> StepOutcome:
        """Advance one task by one transition, whatever its status (used by `_advance` and the scheduler).

        `pending` creates the worktree (or asks for step-mode confirmation), `coding`/`fixing` run the coder,
        `testing` runs the checks, `reviewing` runs the double review, `awaiting_commit` re-verifies the tree,
        and `committing` commits or merges. A task in any other status (`merged`, `blocked`, `failed`) has
        nothing to do: the outcome is `idle` with `progressed=False`. Safe to call from a worker thread for a
        task no other thread is stepping.
        """
        run = self._load()
        task = run.task(task_id)
        handler = self._task_handlers().get(task.status)
        if handler is None:
            return StepOutcome("idle", progressed=False, task_id=task_id)
        return handler(run, task)

    def _advance(self, run: Run) -> StepOutcome:
        outcome = self.prepare(run)
        if outcome is not None:
            return outcome
        task = self._current_task(run)
        if task is None:
            return self._finish(run)
        return self.step_task(task.id)

    def load(self) -> Run:
        """The current run from `state.json`; raises `WorkforceError` if there is none."""
        return self._load()

    def phase(self) -> str:
        """`"preparing"` while integration branches are being created, `"integrating"` from the first integration review, else `""`."""
        run = self.store.load()
        if run is None:
            return ""
        try:
            return self._meta(run.id).get("phase", "")
        except FileNotFoundError:
            return ""

    def _set_phase(self, phase: str) -> None:
        run = self._load()
        if self._meta(run.id).get("phase", "") == phase:
            return
        self._meta_edit(run.id, lambda m: m.__setitem__("phase", phase))
        if phase:
            self._emit("note", text=PHASE_NOTES[phase])

    def _slug(self, run: Run) -> str:
        if run.slug:
            return run.slug
        created = run.created_at[:10]
        try:
            day = date.fromisoformat(created)
        except ValueError:
            day = date.today()
        return ws_mod.slugify(run.goal, day)

    def _involved_repos(self, run: Run) -> list[str]:
        seen: list[str] = []
        for task in run.tasks:
            name = self._task_repo(task)
            if name not in seen:
                seen.append(name)
        return seen

    def _branch_for(self, run: Run, meta: dict, repo_name: str) -> str:
        recorded = run.integration.get(repo_name)
        if recorded is not None:
            return recorded.branch
        return meta.get("branches", {}).get(repo_name) or f"{self.config.branch_prefix}{self._slug(run)}"

    def proposed_branches(self) -> dict[str, str]:
        """Repo name -> the integration branch that will be created (or was), for every repo the plan touches."""
        run = self._load()
        meta = self._meta(run.id)
        return {name: self._branch_for(run, meta, name) for name in self._involved_repos(run)}

    def set_branch(self, repo_name: str, branch_name: str) -> str:
        """Rename the integration branch to be created in `repo_name`; only while the run awaits model picks.

        The name is used exactly as given (the configured prefix is not required or added). It must be a valid
        branch name that does not exist in that repo yet. Returns the stored name.
        """
        run = self._load()
        if run.status != "awaiting_models":
            raise WorkforceError(f"branches can only be renamed while the run is awaiting_models (it is {run.status})")
        involved = self._involved_repos(run)
        if repo_name not in involved:
            raise WorkforceError(f"repo '{repo_name}' has no tasks in this plan; repos with tasks: {involved}")
        recorded = run.integration.get(repo_name)
        if recorded is not None and recorded.created:
            raise WorkforceError(f"the integration branch of '{repo_name}' already exists as {recorded.branch}")
        name = branch_name.strip()
        if not name:
            raise WorkforceError("the branch name must not be empty")
        checked = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True, check=False)
        if checked.returncode != 0 or name.startswith("-"):
            raise WorkforceError(f"'{name}' is not a valid git branch name")
        if git_ops.branch_exists(self.workspace.repo(repo_name).path, name):
            raise WorkforceError(f"branch '{name}' already exists in {repo_name}; pick another name")
        self._meta_edit(run.id, lambda m: m.setdefault("branches", {}).__setitem__(repo_name, name))
        self._emit("note", text=f"{repo_name} will use the integration branch {name}", repo=repo_name)
        return name

    def integration(self, repo_name: str) -> IntegrationInfo | None:
        """The recorded integration branch of `repo_name` for the current run, or None if it is not prepared."""
        run = self.store.load()
        return run.integration.get(repo_name) if run else None

    def branch_summary(self) -> list[dict]:
        """One dict per prepared repo: repo, branch, base_ref, base_sha, commits on top of the base, created."""
        run = self._load()
        summary = []
        for name, info in run.integration.items():
            try:
                commits = git_ops.commits_between(self.workspace.repo(name).path, info.base_sha, info.branch)
            except GitError:
                commits = 0
            summary.append(
                {
                    "repo": name,
                    "branch": info.branch,
                    "base_ref": info.base_ref,
                    "base_sha": info.base_sha,
                    "commits": commits,
                    "created": info.created,
                }
            )
        return summary

    def prepare(self, run: Run) -> StepOutcome | None:
        """Create the integration branch and worktree of every repo the plan touches that has none yet.

        Returns None when nothing needs preparing. Idempotent: repos already prepared are skipped, a half-made one is
        finished, and one that does not match what was recorded becomes a Question. Preflight problems (base does not
        resolve, branch name taken, worktree path in use) become a Question that sends the run back to model
        picking, where branches can be renamed.
        """
        pending = [
            name
            for name in self._involved_repos(run)
            if name not in run.integration or not run.integration[name].created
        ]
        if not pending:
            return None
        try:
            return self._prepare(run, pending)
        except GitError as exc:
            self._emit("error", run=run.id, message=f"git error while preparing: {exc}")
            return self._pause_step(f"git error: {exc}")

    def _prepare(self, run: Run, pending: list[str]) -> StepOutcome:
        previous_phase = self._meta(run.id).get("phase", "")
        self._set_phase(PHASE_PREPARING)
        if not run.slug:
            slug = self._slug(run)
            run = self.store.update(lambda r: setattr(r, "slug", slug))
        meta = self._meta(run.id)
        entries = []
        for name in pending:
            if name in run.integration:
                continue
            repo = self.workspace.repo(name)
            entries.append(
                (name, repo.path, repo.base, self._branch_for(run, meta, name), self.paths.integration_worktree(run.id, name))
            )
        problems = guard.check_workspace_repos(entries)
        if problems:
            return self._ask(
                "preflight",
                "Cannot prepare the integration branches:\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\nFix that (or rename the branch when you pick models), then answer to continue.",
                {},
                resume_run="awaiting_models",
            )
        self.paths.worktree_dir_for(run.id)
        for name in pending:
            repo = self.workspace.repo(name)
            recorded = run.integration.get(name)
            if recorded is None:
                base_ref = repo.base or git_ops.current_branch(repo.path)
                recorded = IntegrationInfo(
                    repo=name,
                    base_ref=base_ref,
                    base_sha=git_ops.resolve_ref(repo.path, base_ref),
                    branch=self._branch_for(run, meta, name),
                    worktree=str(self.paths.integration_worktree(run.id, name)),
                    created=False,
                )
                run = self.store.update(lambda r, info=recorded: r.integration.__setitem__(info.repo, info))
            mismatch = self._materialize(repo, recorded)
            if mismatch:
                return self._ask(
                    "integration_mismatch",
                    f"The integration branch of {name} does not match what this run recorded: {mismatch}. "
                    "Fix it (or remove the leftovers), then answer to try again.",
                    {},
                    resume_run="running",
                )
            run = self.store.update(lambda r, key=name: setattr(r.integration[key], "created", True))
        self._set_phase(previous_phase)
        self._emit("note", text=f"integration branches ready: {', '.join(pending)}")
        return StepOutcome("prepared", progressed=True, detail=", ".join(pending))

    def _materialize(self, repo: RepoInfo, info: IntegrationInfo) -> str | None:
        """Make the recorded integration branch and worktree exist; return a description of any mismatch."""
        worktree = Path(info.worktree)
        exists = git_ops.branch_exists(repo.path, info.branch)
        if not exists:
            if info.created:
                return f"branch {info.branch} was deleted after the run created it"
            git_ops.create_branch(repo.path, info.branch, info.base_sha)
        elif not info.created:
            tip = git_ops.resolve_ref(repo.path, info.branch)
            if tip != info.base_sha:
                return f"branch {info.branch} exists at {tip[:8]} but the run started it at {info.base_sha[:8]}"
        if worktree.exists() and any(worktree.iterdir()):
            try:
                on = git_ops.current_branch(worktree)
            except GitError as exc:
                return f"{worktree} is not a usable worktree ({exc})"
            return None if on == info.branch else f"{worktree} is on {on}, not {info.branch}"
        if not git_ops.worktree_path_free(repo.path, worktree):
            return f"{worktree} is registered as a worktree but missing on disk"
        git_ops.add_worktree_for_branch(repo.path, worktree, info.branch)
        return None

    def emit(self, kind: str, **data: Any) -> None:
        """Append an event (`kind` must be one of `events.KINDS`) and notify subscribers."""
        self._emit(kind, **data)

    def set_task(self, task_id: str, **fields: Any) -> Task:
        """Set fields on one task atomically and emit `task_status` when `status` changes; returns the task."""
        return self._set_task(task_id, **fields)

    def task_meta(self, meta: dict, task_id: str) -> dict:
        """The sidecar's per-task bookkeeping dict inside `meta` (created if missing). Edit it via `meta_edit`."""
        return self._task_meta(meta, task_id)

    def meta_edit(self, run_id: str, fn: Callable[[dict], None]) -> dict:
        """Read-modify-write the sidecar under the lock; `fn` mutates the dict. Returns the saved dict."""
        return self._meta_edit(run_id, fn)

    def gate_reason(self, agents: tuple[str, ...]) -> str | None:
        """A pause reason from the usage gate before launching `agents`, else None (refreshes Codex usage first)."""
        return self._gate_reason(agents)

    def gate_blocked(self, *agents: str) -> StepOutcome | None:
        """Like `gate_reason`, but a reason also pauses the run; returns a `paused` outcome, else None."""
        return self._gate_blocked(*agents)

    def launch(self, request: AgentRequest, task_id: str | None, step: str) -> AgentResult:
        """Run one agent request with `agent_event` events and usage-monitor bookkeeping around it."""
        return self._launch(request, task_id, step)

    def handle_failure(
        self, run: Run, task: Task | None, request: AgentRequest, result: AgentResult
    ) -> StepOutcome:
        """Deal with a failed agent result: model unavailable pauses, anything else is an infra retry then a Question."""
        return self._handle_failure(run, task, request, result)

    def ask(
        self,
        kind: str,
        text: str,
        positions: dict[str, str],
        *,
        task_id: str | None = None,
        resume_to: str | None = None,
        feedback: str | None = None,
        resume_run: str | None = None,
    ) -> StepOutcome:
        """Create a Question and stop the run at `awaiting_user`. Atomic, so worker threads may call it together."""
        return self._ask(
            kind,
            text,
            positions,
            task_id=task_id,
            resume_to=resume_to,
            feedback=feedback,
            resume_run=resume_run,
        )

    def log_path(self, run_id: str, task_id: str, step: str) -> Path:
        """The next raw-stream log path for `step` of `task_id` (`<step>-<n>.jsonl`)."""
        return self._log(run_id, task_id, step)

    def decisions(self) -> str:
        """The current contents of decisions.md ("(none yet)" if empty), as fed into prompts."""
        return self._decisions()

    def commit_exists(self, task: Task) -> bool:
        """True if the task branch already holds a commit whose tree is the approved tree."""
        return self._commit_exists(task)

    def already_merged(self, task: Task) -> bool:
        """True if the task's worktree is gone and its branch is already merged into its integration branch."""
        return self._already_merged(task)

    def finish_task(self, run: Run, task: Task, sha: str, stat: str) -> StepOutcome:
        """Record a task as merged (commit `sha`, diffstat `stat`) and emit `merged`."""
        return self._finish_merge(run, task, sha, stat)

    def finish_run(self, run: Run) -> StepOutcome:
        """When every task is merged: integration review (if due), then mark the run `done`; otherwise `failed`."""
        return self._finish(run)

    def current_task(self, run: Run) -> Task | None:
        """The next task to work on: one already in progress, else the first pending one whose dependencies merged."""
        return self._current_task(run)

    def integration_worktree(self, run: Run, task: Task) -> Path:
        """The worktree of the integration branch `task` merges into."""
        return self._integration_wt(run, task)

    def task_repo(self, task: Task) -> str:
        """The workspace repo `task` belongs to (the single repo for a task from an old state file)."""
        return self._task_repo(task)

    def repo_info(self, task: Task) -> RepoInfo:
        """The workspace `RepoInfo` (path, base, checks) of the task's repo."""
        return self._repo_info(task)

    def integration_branch(self, run: Run, task: Task) -> str:
        """The name of the integration branch `task` merges into."""
        return self._integration_info(run, self._task_repo(task)).branch

    def _finish(self, run: Run) -> StepOutcome:
        if all(t.status == "merged" for t in run.tasks):
            if self._integration_review_due(run):
                return self._integrate(run)
            return self._complete(run)
        self._set_run_status("failed")
        stuck = [f"{t.id} is {t.status}" for t in run.tasks if t.status != "merged"]
        self._emit("error", run=run.id, message="no task can run: " + ", ".join(stuck))
        return StepOutcome("failed", progressed=True, detail=", ".join(stuck))

    def _integration_review_due(self, run: Run) -> bool:
        return not (self.workspace.single and len(run.tasks) == 1)

    def _branch_line(self, item: dict) -> str:
        where = "" if self.workspace.single else f"{item['repo']}: "
        commits = item["commits"]
        return (
            f"{where}branch {item['branch']}: {commits} commit{'' if commits == 1 else 's'} on top of "
            f"{item['base_ref']} ({item['base_sha'][:8]})"
        )

    def _complete(self, run: Run) -> StepOutcome:
        meta = self._meta(run.id)
        branches = self.branch_summary()
        lines = [f"{t.id}: {t.title} ({meta['merged'].get(t.id, {}).get('commit', '')[:8]})" for t in run.tasks]
        lines += [self._branch_line(item) for item in branches]
        lines.append("Run wf publish to push and open PRs.")
        self._remove_integration_worktrees(run)
        self._set_run_status("done")
        self._meta_edit(run.id, lambda m: m.__setitem__("phase", ""))
        self._remove_empty_run_dir(run.id)
        self._emit("run_done", run=run.id, tasks=len(run.tasks), summary=lines, branches=branches)
        return StepOutcome("done", progressed=True)

    def _remove_integration_worktrees(self, run: Run) -> None:
        """Remove the integration worktrees at the end of a run; the branches stay for `wf publish`."""
        for name, info in run.integration.items():
            worktree = Path(info.worktree)
            if not worktree.exists():
                continue
            try:
                git_ops.remove_worktree(self.workspace.repo(name).path, worktree, managed_root=self.managed_root)
            except GitError as exc:
                self._emit("note", text=f"kept the integration worktree of {name} at {worktree}: {exc}", repo=name)

    def _integ_meta(self, run_id: str) -> dict:
        return self._meta(run_id).get("integration") or self._new_integration_meta()

    def _integ_edit(self, run_id: str, **fields: Any) -> None:
        def edit(m: dict) -> None:
            m.setdefault("integration", self._new_integration_meta()).update(fields)

        self._meta_edit(run_id, edit)

    def _plan_for_review(self, run: Run) -> str:
        lines = []
        for name in self._involved_repos(run):
            lines.append(f"Repo {name}:")
            for task in run.tasks:
                if self._task_repo(task) != name:
                    continue
                deps = f" (depends on {', '.join(task.depends_on)})" if task.depends_on else ""
                lines.append(f"- {task.id}: {task.title}{deps}")
                lines.append(f"  {task.description}")
                lines += [f"  Acceptance: {item}" for item in task.acceptance]
        return "\n".join(lines) or "(no tasks)"

    def _integration_repos(self, run: Run) -> list[review.IntegrationRepo]:
        repos = []
        for name in self._involved_repos(run):
            info = self._integration_info(run, name)
            repos.append(review.IntegrationRepo(name, Path(info.worktree), info.branch, info.base_sha))
        return repos

    def _integrate(self, run: Run) -> StepOutcome:
        """One step of the cross-repo review: review the integration branches, or plan fix tasks from its findings."""
        integ = self._integ_meta(run.id)
        self._set_phase(PHASE_INTEGRATING)
        if integ.get("stage") == "fix":
            return self._integrate_fix(run, integ)
        ctx = review.IntegrationContext(
            root=self.root,
            goal=run.goal,
            plan=self._plan_for_review(run),
            repos=self._integration_repos(run),
            decisions=self._decisions(),
            config=self.config,
            runners=self.runners,
            decider=self.decider,
            log_path=lambda step: self._log(run.id, review.INTEGRATION_SCOPE, step),
            emit=self._emit,
            gate=lambda: self._gate_reason(("claude", "codex")),
            after_run=self._after_run,
            repo=self.root,
        )
        outcome = review.integration_review(ctx)
        if outcome.head_moved:
            return self._head_moved(run, None, outcome.head_moved)
        if outcome.pause_reason:
            return self._pause_step(outcome.pause_reason)
        if outcome.model_unavailable:
            return self._pause_step(f"model_unavailable: {outcome.model_unavailable}")
        if outcome.subscription_violation:
            self._emit("error", run=run.id, message=outcome.subscription_violation)
            return self._pause_step(outcome.subscription_violation)
        if outcome.infra_error:
            return self._register_failure(run, None, f"integration review failed: {outcome.infra_error}")
        self._meta_edit(run.id, lambda m: m.__setitem__("plan_failures", 0))
        for item in outcome.final_reviews:
            self._emit(
                "review",
                task=review.INTEGRATION_SCOPE,
                reviewer=item.reviewer,
                model=item.model,
                verdict=item.verdict,
                sha=item.sha,
                findings=len(item.findings),
            )
        if outcome.approved:
            return self._complete(run)
        feedback = review.format_feedback(outcome)
        positions = {
            item.reviewer: f"{item.verdict}: {outcome.summaries.get(item.reviewer, '')}\n"
            + review.format_findings(item.findings)
            for item in outcome.final_reviews
        }
        if outcome.blocked:
            return self._ask(
                "integration_blocked",
                "A reviewer marked the integration BLOCKED: it needs a decision only you can make.",
                positions,
                resume_run="running",
            )
        rounds = integ.get("round", 0) + 1
        if rounds >= self.config.limits.review_rounds:
            self._integ_edit(run.id, round=rounds)
            return self._ask(
                "integration_rounds",
                f"The integration review still requests changes after {rounds} rounds.",
                positions,
                resume_run="running",
            )
        self._integ_edit(run.id, round=rounds, stage="fix", feedback=feedback)
        return StepOutcome("integration_changes_requested", progressed=True)

    def _fix_plan_problem(self, run: Run, new_plan: dict, next_id: str) -> str | None:
        tasks = new_plan.get("tasks") or []
        if not tasks:
            return "the fix plan has no tasks"
        existing = [
            {"id": t.id, "title": t.title or t.id, "acceptance": t.acceptance or [t.id], "depends_on": t.depends_on}
            for t in run.tasks
        ]
        problem = validate_plan({"tasks": existing + tasks})
        if problem:
            return problem
        first = int(next_id[1:])
        for task in tasks:
            if int(task["id"][1:]) < first:
                return f"new task id {task['id']} must be {next_id} or higher (the existing tasks keep their ids)"
        return self._repo_problems(new_plan)

    def _integrate_fix(self, run: Run, integ: dict) -> StepOutcome:
        blocked = self._gate_blocked(self.config.role("planner").agent)
        if blocked:
            return blocked
        meta = self._meta(run.id)
        next_id = self.store.next_task_id()
        existing = "\n".join(
            f"- {task.id} [{self._task_repo(task)}] {task.title} ({task.status})" for task in run.tasks
        )
        request = self._role_request(
            "planner",
            prompts.render(
                "integration_fix",
                decisions=self._decisions(),
                goal=run.goal,
                existing_tasks=existing,
                round=integ.get("round", 1),
                feedback=integ.get("feedback") or "(none)",
                next_id=next_id,
                repo_rule=self._repo_rule(),
                repo_field=self._repo_field(),
                previous_problem=self._previous_problem(run.id),
            ),
            self.root,
            self._log(run.id, review.INTEGRATION_SCOPE, "fix"),
            schema=self._revision_schema(),
            resume=meta["planner_session"],
            sandbox="read-only",
        )
        result = self._launch(request, None, "integration_fix")
        if not result.ok:
            return self._handle_failure(run, None, request, result)
        new_plan, turn = result.structured["plan"], result.structured["debate"]
        problem = self._fix_plan_problem(run, new_plan, next_id)
        if problem:
            return self._reject_plan(run, problem, "the fix plan")
        added = [
            Task(
                id=item["id"],
                title=item["title"],
                description=item["description"],
                acceptance=list(item["acceptance"]),
                depends_on=list(item["depends_on"]),
                repo=item.get("repo") or self._single_repo_name(),
            )
            for item in new_plan["tasks"]
        ]
        self._task_repos.clear()
        run = self.store.update(lambda r: r.tasks.extend(added))

        def edit(m: dict) -> None:
            if m.get("plan"):
                m["plan"]["tasks"] = list(m["plan"]["tasks"]) + list(new_plan["tasks"])
            m["planner_session"] = result.session_id or m["planner_session"]
            m["plan_failures"] = 0
            m["repo_rejects"] = 0
            m["plan_problem"] = ""
            m.setdefault("integration", self._new_integration_meta()).update(stage="review", feedback="")

        meta = self._meta_edit(run.id, edit)
        if meta.get("plan"):
            _atomic_write(self.paths.plan_md, self._plan_markdown(run, meta["plan"], meta["debate"]["log"]))
        ids = ", ".join(task.id for task in added)
        self._set_run_status("awaiting_models")
        self._emit(
            "note",
            text=f"integration review asked for changes; fix tasks {ids} planned ({turn['position']}). "
            "Waiting for the user to pick models",
        )
        return StepOutcome("integration_fix_planned", progressed=True, detail=ids)

    def _begin(self, run: Run, task: Task) -> StepOutcome:
        meta = self._meta(run.id)
        if run.mode == "step" and task.id not in meta["confirmed"]:
            return self._ask(
                "confirm",
                f"Start {task.id}: {task.title}? Answer yes to start, or no to pause.",
                {},
                task_id=task.id,
            )
        repo_name = self._task_repo(task)
        if repo_name not in run.integration or not run.integration[repo_name].created:
            prepared = self.prepare(run)
            if prepared is not None:
                return prepared
            run = self._load()
        repo = self.workspace.repo(repo_name)
        integration = self._integration_info(run, repo_name)
        path = self.paths.worktree(run.id, task.id, repo_name)
        branch = f"{self.config.branch_prefix}{self._slug(run)}-{task.id}"
        self._clear_stale_worktree(repo.path, path, branch)
        self.paths.worktree_dir_for(run.id)
        base_sha = git_ops.create_worktree(repo.path, path, branch, base=f"refs/heads/{integration.branch}")
        self._set_task(task.id, status="coding", branch=branch, worktree=str(path), base_sha=base_sha, repo=repo_name)
        return StepOutcome("worktree", progressed=True, task_id=task.id)

    def _clear_stale_worktree(self, repo_path: Path, path: Path, branch: str) -> None:
        if path.exists():
            git_ops.remove_worktree(repo_path, path, branch=branch, force_branch=True, managed_root=self.managed_root)
        elif git_ops.branch_exists(repo_path, branch):
            proc = subprocess.run(
                ["git", "-C", str(repo_path), "branch", "-D", branch], capture_output=True, text=True, check=False
            )
            if proc.returncode != 0:
                raise GitError(f"cannot delete stale branch {branch}: {proc.stderr.strip()}")

    def _merged_summary(self, run: Run, meta: dict) -> str:
        lines = []
        for task in run.tasks:
            info = meta["merged"].get(task.id)
            if task.status != "merged" or not info:
                continue
            where = "" if self.workspace.single else f" [{self._task_repo(task)}]"
            lines.append(f"- {task.id}{where} {task.title} (commit {info['commit'][:8]}): {info['summary']}")
            if info["stat"]:
                lines.append("  " + info["stat"].replace("\n", "\n  "))
        return "\n".join(lines) or "(none yet)"

    def _code(self, run: Run, task: Task, fixing: bool) -> StepOutcome:
        blocked = self._gate_blocked(task.agent or self.config.role("coder").agent)
        if blocked:
            return blocked
        if not (task.model and task.effort and task.agent):
            return self._pause_step(f"{task.id} has no model chosen")
        meta = self._meta(run.id)
        tm = self._task_meta(meta, task.id)
        repo_name = self._task_repo(task)
        dependencies = self._dependency_worktrees(run, task)
        common = dict(
            decisions=self._decisions(),
            task_id=task.id,
            title=task.title,
            description=task.description,
            acceptance=_bullets(task.acceptance),
            repo=repo_name,
            worktree=task.worktree,
            dependency_worktrees="\n".join(f"- {name}: {path} (branch {branch})" for name, path, branch in dependencies)
            or "- (none: this task does not depend on work in other repos)",
        )
        if fixing:
            prompt = prompts.render(
                "coder_fix", round=tm["fix_round"] + 1, feedback=tm["feedback"] or "(none)", **common
            )
        else:
            prompt = prompts.render("coder", merged_summary=self._merged_summary(run, meta), **common)
        step = "fix" if fixing else "code"
        worktree = Path(task.worktree)
        request = AgentRequest(
            role="coder",
            agent=task.agent,
            model=task.model,
            effort=task.effort,
            prompt=prompt,
            cwd=worktree,
            resume_session=task.coder_session if fixing else None,
            browser=getattr(self.config.browser, task.agent),
            computer_use=task.computer_use,
            log_path=self._log(run.id, task.id, step),
            repo=self.root,
            add_dirs=[path for _, path, _ in dependencies] if task.agent == "claude" else [],
        )
        head_before = git_ops.head_sha(worktree)
        result = self._launch(request, task.id, step)
        if not result.ok:
            return self._handle_failure(run, task, request, result)
        if git_ops.head_sha(worktree) != head_before:
            return self._ask(
                "coder_committed",
                f"The coder for {task.id} committed although only the committer may. "
                "Inspect the branch and tell us how to proceed.",
                {},
                task_id=task.id,
                resume_to="testing",
            )

        def edit(m: dict) -> None:
            info = self._task_meta(m, task.id)
            info["feedback"] = ""
            info["summary"] = _clip(result.text, SUMMARY_MAX_CHARS)
            info["fix_round"] += 1 if fixing else 0

        self._meta_edit(run.id, edit)
        self._set_task(
            task.id,
            status="testing",
            coder_session=result.session_id or task.coder_session,
            infra_failures=0,
        )
        return StepOutcome("fix" if fixing else "code", progressed=True, task_id=task.id)

    def _dependency_worktrees(self, run: Run, task: Task) -> list[tuple[str, Path, str]]:
        """(repo, integration worktree, branch) of every other repo that the task's dependencies (transitively) live in."""
        own = self._task_repo(task)
        seen: set[str] = set()
        stack = list(task.depends_on)
        found: list[tuple[str, Path, str]] = []
        while stack:
            task_id = stack.pop(0)
            if task_id in seen:
                continue
            seen.add(task_id)
            try:
                dep = run.task(task_id)
            except WorkforceError:
                continue
            stack.extend(dep.depends_on)
            name = self._task_repo(dep)
            info = run.integration.get(name)
            if name != own and info is not None and all(name != f[0] for f in found):
                found.append((name, Path(info.worktree), info.branch))
        return found

    def _check(self, run: Run, task: Task) -> StepOutcome:
        worktree = Path(task.worktree)
        repo = self._repo_info(task)
        configured = {"test": repo.checks.test, "lint": repo.checks.lint, "build": repo.checks.build}
        report = checks.run(worktree, configured, env_root=repo.path)
        text = checks.summary(report)
        self._emit("check_result", task=task.id, ok=report.ok, no_checks=report.no_checks, summary=text)
        if report.ok:
            tree = git_ops.tree_hash(worktree)
            self._meta_edit(
                run.id,
                lambda m: self._task_meta(m, task.id).update(check_fixes=0, checks=text),
            )
            self._set_task(task.id, status="reviewing", head_sha=tree)
            return StepOutcome("checks_passed", progressed=True, task_id=task.id)

        meta = self._meta(run.id)
        tm = self._task_meta(meta, task.id)
        feedback = "The checks failed. Fix the code so they pass.\n\n" + text
        limit = self.config.limits.review_rounds
        if tm["check_fixes"] >= limit and not self._agents_can_settle(run, f"{task.id}:checks", text):
            return self._ask(
                "checks_failed",
                f"The checks for {task.id} still fail after {tm['check_fixes']} fix attempts.",
                {"checks": _clip(text, STATE_SNIPPET_CHARS)},
                task_id=task.id,
                resume_to="fixing",
                feedback=feedback,
            )
        if tm["check_fixes"] >= limit:
            tm["check_fixes"] = limit - 1

        def edit(m: dict) -> None:
            info = self._task_meta(m, task.id)
            info["feedback"] = feedback
            info["check_fixes"] = tm["check_fixes"] + 1
            info["checks"] = text

        self._meta_edit(run.id, edit)
        self._set_task(task.id, status="fixing")
        return StepOutcome("checks_failed", progressed=True, task_id=task.id)

    def _review(self, run: Run, task: Task) -> StepOutcome:
        worktree = Path(task.worktree)
        current = git_ops.tree_hash(worktree)
        if current != task.head_sha:
            self._emit("note", task=task.id, text="the worktree changed after the checks ran; checking again")
            self._set_task(task.id, status="testing")
            return StepOutcome("resnapshot", progressed=True, task_id=task.id)
        meta = self._meta(run.id)
        ctx = review.ReviewContext(
            worktree=worktree,
            task=task,
            base_sha=task.base_sha,
            tree=current,
            checks_summary=self._task_meta(meta, task.id)["checks"] or "(no check results recorded)",
            decisions=self._decisions(),
            config=self.config,
            runners=self.runners,
            decider=self.decider,
            log_path=lambda step: self._log(run.id, task.id, step),
            emit=self._emit,
            gate=lambda: self._gate_reason(("claude", "codex")),
            progress=self._task_meta(meta, task.id).get("review_progress"),
            after_run=self._after_run,
            repo=self.root,
        )
        outcome = review.double_review(ctx)
        if outcome.head_moved:
            return self._head_moved(run, task, outcome.head_moved)
        if outcome.pause_reason or outcome.model_unavailable or outcome.infra_error or outcome.subscription_violation:
            self._meta_edit(
                run.id,
                lambda m: self._task_meta(m, task.id).__setitem__("review_progress", outcome.progress),
            )
        if outcome.pause_reason:
            return self._pause_step(outcome.pause_reason)
        if outcome.model_unavailable:
            return self._pause_step(f"model_unavailable: {outcome.model_unavailable}")
        if outcome.subscription_violation:
            self._emit("error", run=run.id, task=task.id, message=outcome.subscription_violation)
            return self._pause_step(outcome.subscription_violation)
        if outcome.infra_error:
            return self._register_failure(run, task, f"review failed: {outcome.infra_error}")
        self._meta_edit(run.id, lambda m: self._task_meta(m, task.id).pop("review_progress", None))
        if outcome.tree_changed:
            return self._drifted(run, task, "a reviewer changed the worktree during review")
        return self._record_review(run, task, outcome)

    def _record_review(self, run: Run, task: Task, outcome: review.ReviewOutcome) -> StepOutcome:
        def append(r: Run) -> None:
            r.task(task.id).reviews.extend(outcome.reviews)

        self.store.update(append)
        for item in outcome.final_reviews:
            self._emit(
                "review",
                task=task.id,
                reviewer=item.reviewer,
                model=item.model,
                verdict=item.verdict,
                sha=item.sha,
                findings=len(item.findings),
            )
        feedback = review.format_feedback(outcome)
        positions = {
            item.reviewer: f"{item.verdict}: {outcome.summaries.get(item.reviewer, '')}\n"
            + review.format_findings(item.findings)
            for item in outcome.final_reviews
        }
        if outcome.approved:
            self._set_task(task.id, status="awaiting_commit", infra_failures=0)
            return StepOutcome("approved", progressed=True, task_id=task.id)
        if outcome.blocked:
            return self._ask(
                "blocked",
                f"A reviewer marked {task.id} BLOCKED: it needs a decision only you can make.",
                positions,
                task_id=task.id,
                resume_to="fixing",
                feedback=feedback,
            )
        rounds = task.review_round + 1
        self._set_task(task.id, review_round=rounds, infra_failures=0)
        limit = self.config.limits.review_rounds
        if rounds >= limit and not self._agents_can_settle(run, f"{task.id}:review", feedback):
            return self._ask(
                "review_rounds",
                f"{task.id} still has requested changes after {rounds} review rounds.",
                positions,
                task_id=task.id,
                resume_to="fixing",
                feedback=feedback,
            )
        if rounds >= limit:
            self._set_task(task.id, review_round=limit - 1)
        self._meta_edit(run.id, lambda m: self._task_meta(m, task.id).update(feedback=feedback))
        self._set_task(task.id, status="fixing")
        return StepOutcome("changes_requested", progressed=True, task_id=task.id)

    def _drifted(self, run: Run, task: Task, why: str) -> StepOutcome:
        meta = self._meta(run.id)
        count = self._task_meta(meta, task.id)["drift"] + 1
        self._meta_edit(run.id, lambda m: self._task_meta(m, task.id).update(drift=count))
        self._emit("error", run=run.id, task=task.id, message=f"approvals voided: {why}")
        new_base = self._rebased_base(task)

        def void(r: Run) -> None:
            item = r.task(task.id)
            item.reviews = []
            if new_base:
                item.base_sha = new_base

        self.store.update(void)
        if new_base:
            self._emit(
                "note",
                task=task.id,
                text=f"{task.branch} now contains the integration branch @ {new_base[:7]}; reviews use it as the base",
            )
        if count > self.config.limits.review_rounds:
            return self._ask(
                "tree_mismatch",
                f"{task.id}: approvals were voided {count} times ({why}). How should we proceed?",
                {},
                task_id=task.id,
                resume_to="testing",
            )
        self._set_task(task.id, status="testing")
        return StepOutcome("voided", progressed=True, task_id=task.id)

    def _rebased_base(self, task: Task) -> str | None:
        """The integration branch's HEAD when the task branch was rebased onto it after `base_sha` was recorded, else None."""
        if not task.branch or not task.base_sha:
            return None
        integration = self._integration_wt(self._load(), task)
        main = git_ops.head_sha(integration)
        if main == task.base_sha or not git_ops.can_fast_forward(integration, task.branch):
            return None
        return main

    @staticmethod
    def _is_approved(task: Task, tree: str) -> bool:
        latest = {item.reviewer: item for item in task.reviews}
        return set(latest) == set(review.REVIEWER_NAMES) and all(
            item.verdict == "APPROVE" and item.sha == tree and item.base_sha == task.base_sha
            for item in latest.values()
        )

    def _pre_commit(self, run: Run, task: Task) -> StepOutcome:
        tree = git_ops.tree_hash(Path(task.worktree))
        if tree != task.head_sha or not self._is_approved(task, tree):
            return self._drifted(run, task, "the tree changed after it was approved")
        self._set_task(task.id, status="committing")
        return StepOutcome("ready_to_commit", progressed=True, task_id=task.id)

    def _git_lines(self, cwd: Path, *args: str) -> list[str] | None:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
        return proc.stdout.splitlines() if proc.returncode == 0 else None

    def _commit_exists(self, task: Task) -> bool:
        """True if the branch holds only commits the committer made, on top of `base_sha`, ending in the approved tree.

        Every commit on the branch that is not already in the integration branch (`base_sha..HEAD`, or `integration..HEAD` after a rebase)
        must be single-parent and recorded in the sidecar by a committer step
        (`committer_shas`); a commit anyone else made anywhere on the branch means it does not count.
        """
        worktree = Path(task.worktree)
        head = git_ops.head_sha(worktree)
        if (
            head == task.base_sha
            or git_ops.commit_tree(worktree) != task.head_sha
            or git_ops.current_branch(worktree) != task.branch
        ):
            return False
        recorded = set(self._task_meta(self._meta(self._load().id), task.id).get("committer_shas", []))
        rebased = self._rebased_base(task)
        starts = [rebased, task.base_sha] if rebased else [task.base_sha]
        if not any(self._only_committer_commits(worktree, since, recorded) for since in starts):
            return False
        return commit.signature_problem(worktree) is None

    def _only_committer_commits(self, worktree: Path, since: str, recorded: set[str]) -> bool:
        """True if `since..HEAD` is non-empty and every commit in it is single-parent and a recorded committer commit.

        After a rebase, `since` is the integration branch's HEAD, so its own commits are not counted. The plain
        `base_sha` range is tried too, because once the integration branch has been fast-forwarded to the branch tip
        the rebased range is empty.
        """
        lines = self._git_lines(worktree, "rev-list", "--parents", f"{since}..HEAD")
        if not lines:
            return False
        for line in lines:
            sha, *parents = line.split()
            if len(parents) != 1 or sha not in recorded:
                return False
        return True

    def _already_merged(self, task: Task) -> bool:
        """True if the worktree is gone and the branch, which must hold the approved tree, is already in the integration branch."""
        if Path(task.worktree).exists():
            return False
        integration = self._integration_wt(self._load(), task)
        tip = self._git_lines(integration, "rev-parse", "--verify", f"refs/heads/{task.branch}")
        if not tip or tip[0] == task.base_sha:
            return False
        if git_ops.commit_tree(integration, task.branch) != task.head_sha:
            return False
        proc = subprocess.run(
            ["git", "-C", str(integration), "merge-base", "--is-ancestor", task.branch, "HEAD"],
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0

    def _commit(self, run: Run, task: Task) -> StepOutcome:
        if self._already_merged(task):
            return self._finish_merge(run, task, git_ops.head_sha(self._integration_wt(run, task)), "")
        if self._commit_exists(task):
            return self._merge(run, task)
        blocked = self._gate_blocked(self.config.role("committer").agent)
        if blocked:
            return blocked
        worktree = Path(task.worktree)
        ctx = commit.CommitContext(
            worktree=worktree,
            branch=task.branch,
            task=task,
            approved_tree=task.head_sha,
            role=self.config.role("committer"),
            runner=self._runner(self.config.role("committer").agent),
            decisions=self._decisions(),
            log_path=self._log(run.id, task.id, "commit"),
            emit=self._emit,
            after_run=self._after_run,
            repo=self.root,
            on_commit=lambda sha: self._record_committer_sha(task.id, sha),
            repo_name=self._task_repo(task),
        )
        outcome = commit.commit_task(ctx)
        if outcome.ok:
            self._meta_edit(run.id, lambda m: self._task_meta(m, task.id).update(commit_failures=0))
            return StepOutcome("committed", progressed=True, task_id=task.id, detail=outcome.sha or "")
        if outcome.model_unavailable:
            return self._pause_step(f"model_unavailable: {outcome.model_unavailable}")
        if outcome.subscription_violation:
            self._emit("error", run=run.id, task=task.id, message=outcome.error)
            return self._pause_step(outcome.error)
        if outcome.tree_mismatch:
            return self._drifted(run, task, outcome.error or "the committed tree differs from the approved tree")

        meta = self._meta(run.id)
        failures = self._task_meta(meta, task.id)["commit_failures"] + 1
        self._meta_edit(run.id, lambda m: self._task_meta(m, task.id).update(commit_failures=failures))
        self._emit("error", run=run.id, task=task.id, message=f"commit failed: {outcome.error}")
        if failures >= 2:
            return self._ask(
                "commit_failed",
                f"The commit for {task.id} failed twice ({outcome.error}). "
                "Was the Touch ID prompt cancelled? Answer when you are ready to try again.",
                {},
                task_id=task.id,
                resume_to="awaiting_commit",
            )
        return StepOutcome("commit_failed", progressed=True, task_id=task.id, detail=outcome.error or "")

    def _merge(self, run: Run, task: Task) -> StepOutcome:
        worktree = Path(task.worktree)
        integration = self._integration_wt(run, task)

        def blocked_merge(reason: str) -> StepOutcome:
            self._emit("error", run=run.id, task=task.id, message=reason)
            return self._ask(
                "merge_blocked",
                f"{task.id} is committed on {task.branch} but could not be merged: {reason}",
                {},
                task_id=task.id,
                resume_to="committing",
            )

        try:
            if not git_ops.can_fast_forward(integration, task.branch):
                return blocked_merge("the integration branch moved, so a fast-forward is not possible")
            git_ops.merge_ff_only_in(integration, task.branch)
        except GitError as exc:
            return blocked_merge(str(exc))
        if git_ops.commit_tree(integration) != task.head_sha:
            return blocked_merge("the merged tree does not match the approved tree")
        stat = git_ops.diff_stat(worktree, task.base_sha)
        return self._finish_merge(run, task, git_ops.head_sha(integration), stat)

    def _finish_merge(self, run: Run, task: Task, sha: str, stat: str) -> StepOutcome:
        def edit(m: dict) -> None:
            m["merged"][task.id] = {
                "commit": sha,
                "summary": self._task_meta(m, task.id)["summary"],
                "stat": _clip(stat, STAT_MAX_CHARS),
            }

        self._meta_edit(run.id, edit)
        self._set_task(task.id, status="merged", worktree=None)
        target = run.integration.get(task.repo) if task.repo else None
        self._emit(
            "merged",
            task=task.id,
            branch=task.branch,
            into=target.branch if target else None,
            sha=sha,
            tree=task.head_sha,
        )
        self._clean_up_merged(run, task)
        return StepOutcome("merged", progressed=True, task_id=task.id, detail=sha)

    def _clean_up_merged(self, run: Run, task: Task) -> None:
        """Remove a merged task's worktree and delete its branch with the safe `-d`; never forces anything.

        The branch is deleted from the integration worktree, because `-d` judges "merged" against the HEAD it runs
        in. Runs after the task is recorded as merged, so a failure here only leaves clutter behind: it is reported
        as a `note` event and the branch is kept.
        """
        worktree = Path(task.worktree) if task.worktree else None
        repo_path = self._repo_info(task).path
        integration = self._integration_wt(run, task)
        try:
            if worktree is not None and worktree.exists():
                git_ops.remove_worktree(repo_path, worktree, managed_root=self.managed_root)
            if task.branch and git_ops.branch_exists(integration, task.branch):
                git_ops.delete_branch(integration, task.branch)
        except GitError as exc:
            kept = task.branch and git_ops.branch_exists(repo_path, task.branch)
            self._emit(
                "note",
                task=task.id,
                text=f"kept {'branch ' + task.branch if kept else 'the leftovers'} of merged {task.id}: {exc}",
            )

    def _remove_empty_run_dir(self, run_id: str) -> None:
        """Remove the run's worktree folders that ended up empty (never a folder that still holds anything)."""
        base = self.paths.worktrees_root / run_id
        if not base.is_dir():
            return
        for directory, _, _ in os.walk(base, topdown=False):
            try:
                os.rmdir(directory)
            except OSError:
                continue
