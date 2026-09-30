"""Durable run state: dataclasses, atomic JSON persistence and the single-runner lock."""

import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from workforce.errors import StateLockedError, WorkforceError
from workforce.paths import Paths

RUN_STATUSES = frozenset(
    {"planning", "debating", "awaiting_models", "preparing", "running", "integrating", "paused", "awaiting_user", "done", "failed"}
)
TASK_STATUSES = frozenset(
    {
        "pending",
        "coding",
        "testing",
        "reviewing",
        "fixing",
        "awaiting_commit",
        "committing",
        "merged",
        "failed",
        "blocked",
    }
)
RUN_MODES = frozenset({"auto", "step"})
REVIEWERS = frozenset({"claude", "codex"})
VERDICTS = frozenset({"APPROVE", "REQUEST_CHANGES", "BLOCKED"})
SEVERITIES = frozenset({"blocker", "major", "minor", "nit"})
QUESTION_STATUSES = frozenset({"open", "answered"})


class StateError(WorkforceError):
    """state.json is malformed or holds an invalid value."""


def _check_choice(owner: str, name: str, value: Any, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise StateError(f"{owner}.{name} '{value}' is invalid; expected one of {sorted(allowed)}")


def _build(cls: type, data: Any, owner: str) -> Any:
    if not isinstance(data, dict):
        raise StateError(f"{owner} must be an object")
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise StateError(f"{owner} has unknown keys: {sorted(unknown)}")
    required = {f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING}
    missing = required - set(data)
    if missing:
        raise StateError(f"{owner} is missing keys: {sorted(missing)}")
    return data


@dataclass
class Finding:
    severity: str
    file: str
    line: int | None
    message: str

    def validate(self) -> None:
        _check_choice("Finding", "severity", self.severity, SEVERITIES)

    @classmethod
    def from_dict(cls, data: Any, owner: str = "Finding") -> "Finding":
        _build(cls, data, owner)
        item = cls(**data)
        item.validate()
        return item


@dataclass
class Review:
    reviewer: str
    model: str
    session: str | None
    sha: str
    base_sha: str
    verdict: str
    findings: list[Finding] = field(default_factory=list)
    at: str = ""

    def validate(self) -> None:
        _check_choice("Review", "reviewer", self.reviewer, REVIEWERS)
        _check_choice("Review", "verdict", self.verdict, VERDICTS)
        for finding in self.findings:
            finding.validate()

    @classmethod
    def from_dict(cls, data: Any, owner: str = "Review") -> "Review":
        _build(cls, data, owner)
        payload = dict(data)
        payload["findings"] = [
            Finding.from_dict(item, f"{owner}.findings[{i}]") for i, item in enumerate(data.get("findings", []))
        ]
        item = cls(**payload)
        item.validate()
        return item


@dataclass
class Question:
    id: str
    task_id: str | None
    text: str
    positions: dict[str, str] = field(default_factory=dict)
    answer: str | None = None
    status: str = "open"

    def validate(self) -> None:
        _check_choice("Question", "status", self.status, QUESTION_STATUSES)

    @classmethod
    def from_dict(cls, data: Any, owner: str = "Question") -> "Question":
        _build(cls, data, owner)
        item = cls(**data)
        item.validate()
        return item


@dataclass
class Task:
    id: str
    title: str
    description: str
    acceptance: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    agent: str | None = None
    status: str = "pending"
    branch: str | None = None
    worktree: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    coder_session: str | None = None
    review_round: int = 0
    reviews: list[Review] = field(default_factory=list)
    infra_failures: int = 0
    computer_use: bool = False
    repo: str | None = None

    def validate(self) -> None:
        _check_choice("Task", "status", self.status, TASK_STATUSES)
        for review in self.reviews:
            review.validate()

    @classmethod
    def from_dict(cls, data: Any, owner: str = "Task") -> "Task":
        _build(cls, data, owner)
        payload = dict(data)
        payload["reviews"] = [
            Review.from_dict(item, f"{owner}.reviews[{i}]") for i, item in enumerate(data.get("reviews", []))
        ]
        item = cls(**payload)
        item.validate()
        return item


@dataclass
class IntegrationInfo:
    repo: str
    base_ref: str
    base_sha: str
    branch: str
    worktree: str
    created: bool

    @classmethod
    def from_dict(cls, data: Any, owner: str = "IntegrationInfo") -> "IntegrationInfo":
        _build(cls, data, owner)
        return cls(**data)


@dataclass
class Run:
    id: str
    goal: str
    created_at: str
    status: str = "planning"
    pause_reason: str | None = None
    tasks: list[Task] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    mode: str = "auto"
    slug: str | None = None
    integration: dict[str, IntegrationInfo] = field(default_factory=dict)

    def validate(self) -> None:
        _check_choice("Run", "status", self.status, RUN_STATUSES)
        _check_choice("Run", "mode", self.mode, RUN_MODES)
        for repo_name, info in self.integration.items():
            if info.repo != repo_name:
                raise StateError(f"Run.integration['{repo_name}'] describes repo '{info.repo}'")
        for task in self.tasks:
            task.validate()
        for question in self.questions:
            question.validate()

    @classmethod
    def create(cls, run_id: str, goal: str, mode: str = "auto", slug: str | None = None) -> "Run":
        run = cls(id=run_id, goal=goal, created_at=datetime.now(timezone.utc).isoformat(), mode=mode, slug=slug)
        run.validate()
        return run

    @classmethod
    def from_dict(cls, data: Any) -> "Run":
        _build(cls, data, "Run")
        payload = dict(data)
        payload["tasks"] = [Task.from_dict(item, f"Run.tasks[{i}]") for i, item in enumerate(data.get("tasks", []))]
        payload["questions"] = [
            Question.from_dict(item, f"Run.questions[{i}]") for i, item in enumerate(data.get("questions", []))
        ]
        integration = data.get("integration", {})
        if not isinstance(integration, dict):
            raise StateError("Run.integration must be an object")
        payload["integration"] = {
            key: IntegrationInfo.from_dict(item, f"Run.integration['{key}']") for key, item in integration.items()
        }
        run = cls(**payload)
        run.validate()
        return run

    def task(self, task_id: str) -> Task:
        for item in self.tasks:
            if item.id == task_id:
                return item
        raise StateError(f"no task {task_id} in run {self.id}")

    def question(self, question_id: str) -> Question:
        for item in self.questions:
            if item.id == question_id:
                return item
        raise StateError(f"no question {question_id} in run {self.id}")


def _next_number(prefix: str, ids: list[str]) -> str:
    highest = 0
    for item in ids:
        if item.startswith(prefix) and item[len(prefix):].isdigit():
            highest = max(highest, int(item[len(prefix):]))
    return f"{prefix}{highest + 1}"


WRITE_LOCK_NAME = "state.write.lock"


class _WriteLock:
    """One per write-lock file in this process: a re-entrant thread lock plus the cross-process flock."""

    def __init__(self, path: Path):
        self.path = path
        self.rlock = threading.RLock()
        self.depth = 0
        self.handle: Any = None

    def acquire(self) -> None:
        self.rlock.acquire()
        try:
            if self.depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.path, "a+")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except BaseException:
                    handle.close()
                    raise
                self.handle = handle
            self.depth += 1
        except BaseException:
            self.rlock.release()
            raise

    def release(self) -> None:
        try:
            self.depth -= 1
            if self.depth == 0:
                handle, self.handle = self.handle, None
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
        finally:
            self.rlock.release()


_write_locks: dict[str, _WriteLock] = {}
_write_locks_guard = threading.Lock()


def _write_lock_for(path: Path) -> _WriteLock:
    key = os.path.abspath(path)
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = _write_locks[key] = _WriteLock(Path(key))
        return lock


class StateStore:
    """Reads and writes `.workforce/state.json`; only this class ever writes that file."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self._write = _write_lock_for(paths.root / WRITE_LOCK_NAME)
        self._mutex = self._write.rlock

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        """Hold the short cross-process write lock (`state.write.lock`); re-entrant within a thread.

        Take it around any load-modify-save of a file next to state.json, such as the orchestrator sidecar.
        """
        self._write.acquire()
        try:
            yield
        finally:
            self._write.release()

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the exclusive runner lock; raise StateLockedError if another process has it."""
        self.paths.root.mkdir(parents=True, exist_ok=True)
        handle = open(self.paths.lock, "a+")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateLockedError(
                    f"another workforce process holds {self.paths.lock}; only one run can be active"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def load(self) -> Run | None:
        with self._mutex:
            try:
                text = self.paths.state.read_text()
            except FileNotFoundError:
                return None
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise StateError(f"{self.paths.state} is not valid JSON: {exc}") from exc
            return Run.from_dict(data)

    def save(self, run: Run) -> None:
        with self.write_lock():
            run.validate()
            payload = json.dumps(asdict(run), indent=2, sort_keys=False).encode("utf-8")
            self._atomic_write(payload)

    def update(self, fn: Callable[[Run], Any]) -> Run:
        """Load the run under the write lock, apply `fn` to it, save, and return the saved run."""
        with self.write_lock():
            run = self.load()
            if run is None:
                raise StateError("no run in progress")
            fn(run)
            self.save(run)
            return run

    def next_task_id(self) -> str:
        run = self.load()
        return _next_number("T", [t.id for t in run.tasks] if run else [])

    def next_question_id(self) -> str:
        run = self.load()
        return _next_number("Q", [q.id for q in run.questions] if run else [])

    def _atomic_write(self, payload: bytes) -> None:
        target = self.paths.state
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        self._fsync_dir(target.parent)

    @staticmethod
    def _fsync_dir(directory: Any) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
