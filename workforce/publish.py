"""`wf publish`: the Claude committer pushes each integration branch and opens a PR, using the user's own setup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from workforce import git_ops, prompts, schemas
from workforce.agents import guard
from workforce.agents.base import AgentRequest
from workforce.errors import GitError, WorkforceError

PUBLISH_DIR = "_publish"
PUBLISH_STEP_DIR = "publish"
FINISHED_STATUSES = ("done", "failed")
STATUS_PUSHED = "pushed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_NOT_TRIED = "not tried"
STATUS_ABORTED = "aborted"


@dataclass
class PublishRow:
    """The outcome for one integration branch."""

    repo: str
    branch: str
    commits: int
    status: str
    pushed: bool = False
    pr_url: str | None = None
    error: str | None = None


def refusal(run: Any) -> str | None:
    """Why nothing can be published right now (a run is still going), or None."""
    if run is None:
        return "there is no run yet, so there is nothing to publish"
    if run.status not in FINISHED_STATUSES:
        return f"run {run.id} is {run.status}; finish it (or let it fail) before publishing"
    return None


def publishable(orch: Any, repo_name: str | None = None) -> list[dict]:
    """The last run's integration branches that hold commits, optionally only `repo_name`'s.

    Raises WorkforceError when `repo_name` is not a workspace repo or nothing has commits to publish.
    """
    if repo_name is not None:
        orch.workspace.repo(repo_name)
    entries = [e for e in orch.branch_summary() if e.get("commits", 0) > 0 and repo_name in (None, e["repo"])]
    if not entries:
        where = f" for {repo_name}" if repo_name else ""
        raise WorkforceError(f"no integration branch{where} has commits to publish")
    return entries


def _log_path(orch: Any, run_id: str, repo_name: str) -> Path:
    step = repo_name.replace("/", "-")
    n = 1
    while True:
        path = orch.paths.step_log(run_id, PUBLISH_STEP_DIR, step, n)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        n += 1


def _failure_text(result: Any) -> str:
    return result.error or f"the publisher failed ({result.error_kind or 'error'})"


def _record_usage(orch: Any, result: Any) -> None:
    monitor = getattr(orch, "usage_monitor", None)
    if monitor is not None:
        monitor.ingest_claude(result)


def _base_moved(repo: Path, base_ref: str, before: str | None) -> bool:
    try:
        return before is not None and git_ops.resolve_ref(repo, base_ref) != before
    except GitError:
        return False


def publish_branch(orch: Any, run_id: str, entry: dict) -> PublishRow:
    """Push one integration branch from a temporary worktree of it; the worktree is always removed afterwards."""
    name, branch = entry["repo"], entry["branch"]
    row = PublishRow(repo=name, branch=branch, commits=entry["commits"], status=STATUS_FAILED)
    info = orch.workspace.repo(name)
    role = orch.config.role("committer")
    if role.agent != "claude":
        row.error = f"roles.committer must be a Claude role to publish, not {role.agent}"
        return row
    path = orch.paths.worktree(run_id, PUBLISH_DIR, name)
    try:
        git_ops.add_worktree_for_branch(info.path, path, branch)
    except GitError as exc:
        row.error = f"cannot check the branch out to publish it: {exc}"
        return row
    try:
        return _run_publisher(orch, run_id, entry, row, path, role)
    finally:
        try:
            git_ops.remove_worktree(info.path, path, managed_root=orch.paths.home / ".workforce" / "worktrees")
        except GitError as exc:
            orch.events.emit("error", source="publish", message=f"could not remove {path}: {exc}")


def _run_publisher(orch: Any, run_id: str, entry: dict, row: PublishRow, path: Path, role: Any) -> PublishRow:
    info = orch.workspace.repo(row.repo)
    try:
        tip_before = git_ops.head_sha(path)
        base_before = git_ops.resolve_ref(info.path, entry["base_ref"])
    except GitError as exc:
        row.error = f"cannot read the branch before publishing: {exc}"
        return row
    prompt = prompts.render(
        "publisher",
        decisions=orch.decisions(),
        repo=row.repo,
        branch=row.branch,
        base_ref=entry["base_ref"],
        base_sha=entry["base_sha"][:8],
        commits=row.commits,
    )
    request = AgentRequest(
        role="committer",
        agent=role.agent,
        model=role.model,
        effort=role.effort,
        prompt=prompt,
        cwd=path,
        schema=schemas.PUBLISH_RESULT,
        user_setup=True,
        log_path=_log_path(orch, run_id, row.repo),
        repo=info.path,
    )
    result = orch.runners[role.agent].run(request)
    _record_usage(orch, result)
    if guard.is_subscription_violation(result):
        row.error = _failure_text(result)
        row.status = STATUS_ABORTED
        return row
    problems = []
    try:
        if git_ops.head_sha(path) != tip_before:
            problems.append(f"the publisher moved the branch tip from {tip_before[:8]}; review it before trusting the push")
    except GitError as exc:
        problems.append(f"cannot re-read the branch tip: {exc}")
    if _base_moved(info.path, entry["base_ref"], base_before):
        problems.append(f"the base branch {entry['base_ref']} moved during publish; WorkForce never writes to it, check `git log`")
    if not result.ok:
        row.error = "; ".join([_failure_text(result), *problems])
        return row
    structured = result.structured or {}
    row.pushed = bool(structured.get("pushed"))
    row.pr_url = structured.get("pr_url") or None
    reported = structured.get("error")
    row.error = "; ".join([e for e in (reported, *problems) if e]) or None
    row.status = STATUS_PUSHED if row.pushed and not problems else STATUS_FAILED
    return row


def publish(
    orch: Any,
    *,
    repo_name: str | None = None,
    confirm: Callable[[dict], bool] | None = None,
    say: Callable[[str], None] | None = None,
) -> list[PublishRow]:
    """Publish the last run's integration branches, one at a time, holding the runner lock throughout.

    Refuses while a run is active (WorkforceError) or another process holds the lock (StateLockedError).
    `confirm(entry)` is asked before each branch; without it every branch is published.
    A branch whose publisher breaks the subscription rules stops the rest ("not tried").
    """
    tell = say or (lambda text: None)
    with orch.store.lock():
        problem = refusal(orch.store.load())
        if problem:
            raise WorkforceError(problem)
        entries = publishable(orch, repo_name)
        reason = orch.usage_gate()
        if reason:
            raise WorkforceError(f"not publishing while usage is paused: {reason}")
        run_id = orch.store.load().id
        rows: list[PublishRow] = []
        aborted = False
        for entry in entries:
            if aborted:
                rows.append(PublishRow(entry["repo"], entry["branch"], entry["commits"], STATUS_NOT_TRIED))
                continue
            if confirm is not None and not confirm(entry):
                rows.append(PublishRow(entry["repo"], entry["branch"], entry["commits"], STATUS_SKIPPED))
                continue
            tell(f"publishing {entry['branch']} in {entry['repo']} (confirm if git asks)…")
            orch.events.emit("note", text=f"publishing {entry['branch']} in {entry['repo']}")
            row = publish_branch(orch, run_id, entry)
            rows.append(row)
            aborted = row.status == STATUS_ABORTED
        return rows
