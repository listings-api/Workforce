"""Helpers for the workspace CLI tests: a small stub Orchestrator that speaks the brief-16 public API, real-git
helpers to make integration branches, and a snapshot of what a user's checkout must keep."""

import subprocess
from pathlib import Path

from workforce import git_ops
from workforce.errors import WorkforceError
from workforce.events import EventLog
from workforce.paths import Paths
from workforce.state import IntegrationInfo, Run, StateStore, Task


def _git(repo, *args) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def make_branch(repo: Path, branch: str, files: dict[str, str] | None = None, base: str = "HEAD") -> tuple[str, str]:
    """Create `branch` in `repo` off `base` without touching the checkout, optionally with one commit adding `files`.

    Returns (base_sha, branch_tip_sha).
    """
    base_sha = _git(repo, "rev-parse", base)
    _git(repo, "branch", branch, base_sha)
    if files:
        tmp = repo.parent / f".mk-{branch.replace('/', '-')}"
        _git(repo, "worktree", "add", str(tmp), branch)
        try:
            for rel, text in files.items():
                (tmp / rel).write_text(text)
            _git(tmp, "add", "-A")
            _git(tmp, "commit", "-q", "-m", f"work on {branch}")
        finally:
            _git(repo, "worktree", "remove", "--force", str(tmp))
    return base_sha, _git(repo, "rev-parse", branch)


def snapshot(repo: Path) -> dict:
    """What must never change in a user's checkout: HEAD, branch, status and the branch list."""
    return {
        "head": _git(repo, "rev-parse", "HEAD"),
        "branch": _git(repo, "symbolic-ref", "--short", "HEAD"),
        "status": _git(repo, "status", "--porcelain"),
        "worktrees": _git(repo, "worktree", "list", "--porcelain"),
    }


def branch_summary_of(orch) -> list[dict]:
    run = orch.store.load()
    entries = []
    for name, info in ((run.integration if run else None) or {}).items():
        repo = orch.workspace.repo(name).path
        exists = git_ops.branch_exists(repo, info.branch)
        entries.append(
            {
                "repo": name,
                "branch": info.branch,
                "base_ref": info.base_ref,
                "base_sha": info.base_sha,
                "commits": git_ops.commits_between(repo, info.base_sha, info.branch) if exists else 0,
                "created": info.created,
            }
        )
    return entries


class StubOrch:
    """The public orchestrator API the CLI and console call, backed by a real state store and real git repos."""

    def __init__(self, ws, config, home, runners=None, proposed=None):
        self.workspace = ws
        self.root = ws.root
        self.repo = ws.root
        self.config = config
        self.paths = Paths(ws.root, home=home)
        self.paths.ensure()
        self.store = StateStore(self.paths)
        self.events = EventLog(self.paths)
        self.runners = runners or {}
        self.decider = None
        self.usage_monitor = None
        self.proposed = dict(proposed or {})
        self.set_branch_calls: list[tuple[str, str]] = []
        self.model_calls: list[tuple[str, str, str]] = []
        self.resumed = 0
        self.gate_reason: str | None = None

    def usage_gate(self):
        return self.gate_reason

    def decisions(self) -> str:
        return "(none yet)"

    def task_size_hints(self) -> dict:
        return {}

    def proposed_branches(self) -> dict:
        return dict(self.proposed)

    def set_branch(self, repo_name, branch_name) -> None:
        self.workspace.repo(repo_name)
        if not branch_name.strip() or " " in branch_name:
            raise WorkforceError(f"'{branch_name}' is not a valid branch name")
        self.set_branch_calls.append((repo_name, branch_name))
        self.proposed[repo_name] = branch_name

    def branch_summary(self) -> list[dict]:
        return branch_summary_of(self)

    def set_models(self, task_id, model, effort):
        self.model_calls.append((task_id, model, effort))

        def fn(run):
            task = run.task(task_id)
            task.model, task.effort, task.agent = model, effort, "claude"

        self.store.update(fn)

    def resume(self):
        self.resumed += 1
        return self.store.load()

    def set_mode(self, mode):
        return self.store.load()

    def pause(self, reason):
        return self.store.load()


def seed_run(
    orch,
    *,
    status="done",
    tasks=(("alpha", "T1"),),
    branches: dict[str, tuple[str, str, str]] | None = None,
    slug="add-things-0929",
) -> Run:
    """Write a run into the store: one task per (repo, id), and `branches` = {repo: (branch, base_ref, base_sha)}."""
    run = Run.create("20260929-120000", "add things", slug=slug)
    run.status = status
    for repo_name, task_id in tasks:
        run.tasks.append(
            Task(id=task_id, title=f"Add {task_id.lower()}", description="d", acceptance=["a"], repo=repo_name)
        )
    for repo_name, (branch, base_ref, base_sha) in (branches or {}).items():
        run.integration[repo_name] = IntegrationInfo(
            repo=repo_name, base_ref=base_ref, base_sha=base_sha, branch=branch, worktree="", created=True
        )
    orch.store.save(run)
    return run
