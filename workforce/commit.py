"""The Claude commit step: one signed commit per task, verified against the approved tree."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from workforce import git_ops, prompts, schemas
from workforce.agents import guard
from workforce.agents.base import AgentRequest, AgentResult, AgentRunner
from workforce.config import Role
from workforce.errors import GitError
from workforce.state import Task

TOUCH_ID_MESSAGE = "👆 tap Touch ID to sign…"


ACCEPTED_SIGNATURES = ("G", "U")


def _no_run_hook(agent: str, result: AgentResult) -> None:
    return None


def _no_commit_hook(sha: str) -> None:
    return None


def _git(worktree: Path, *args: str, ok_codes: tuple[int, ...] = (0,)) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise GitError(f"git executable not found: {exc}") from exc
    if proc.returncode not in ok_codes:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def signature_problem(worktree: Path) -> str | None:
    """Why HEAD is not acceptably signed, or None. Only checked when `commit.gpgsign` is on for the repo."""
    if _git(worktree, "config", "--type=bool", "--get", "commit.gpgsign", ok_codes=(0, 1)) != "true":
        return None
    status = _git(worktree, "log", "-1", "--format=%G?", "HEAD")
    if status in ACCEPTED_SIGNATURES:
        return None
    return f"commit.gpgsign is on but the commit's signature status is '{status}' (expected G or U)"


def structure_problem(worktree: Path, head_before: str) -> str | None:
    """Why the commits added since `head_before` are not exactly one commit on top of it, or None."""
    added = _git(worktree, "rev-list", "--count", f"{head_before}..HEAD")
    if added != "1":
        return f"the committer added {added} commits; exactly one is allowed"
    parents = _git(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    if parents != [head_before]:
        return f"the new commit's parent is {', '.join(p[:8] for p in parents) or 'nothing'}, not {head_before[:8]}"
    return None


@dataclass
class CommitContext:
    """Inputs for one commit attempt. `approved_tree` is the tree hash both reviewers approved.

    `repo_name` is the workspace repo the task belongs to; `repo` is the workspace root used by the risk-gate hook.
    """

    worktree: Path
    branch: str
    task: Task
    approved_tree: str
    role: Role
    runner: AgentRunner
    decisions: str
    log_path: Path | None
    emit: Callable[..., None]
    after_run: Callable[[str, AgentResult], None] = _no_run_hook
    repo: Path | None = None
    on_commit: Callable[[str], None] = _no_commit_hook
    repo_name: str = ""


@dataclass
class CommitOutcome:
    ok: bool
    sha: str | None = None
    tree_mismatch: bool = False
    error: str | None = None
    error_kind: str | None = None
    model_unavailable: str | None = None
    subscription_violation: bool = False


def commit_task(ctx: CommitContext) -> CommitOutcome:
    """Ask the Claude committer to commit the worktree, then verify what git actually recorded.

    Success means HEAD moved by exactly one commit whose parent is the old HEAD, it sits on the task
    branch, it is signed when `commit.gpgsign` is on, and its tree equals `approved_tree`. A different
    tree is reported as `tree_mismatch` so the caller can void the approvals. `on_commit(sha)` is called
    once the run is seen to have added exactly one commit on the right branch, whatever its tree or signature,
    so the caller can record that the committer made it (the tree and signature are re-checked before any merge).
    """
    ctx.emit("commit_waiting", task=ctx.task.id, message=TOUCH_ID_MESSAGE)
    try:
        head_before = git_ops.head_sha(ctx.worktree)
    except GitError as exc:
        return CommitOutcome(ok=False, error=f"cannot read HEAD before committing: {exc}")

    prompt = prompts.render(
        "committer",
        decisions=ctx.decisions,
        task_id=ctx.task.id,
        title=ctx.task.title,
        description=ctx.task.description,
        branch=ctx.branch,
        repo_name=ctx.repo_name or "(this repo)",
        tree=ctx.approved_tree,
    )
    request = AgentRequest(
        role="committer",
        agent=ctx.role.agent,
        model=ctx.role.model,
        effort=ctx.role.effort,
        prompt=prompt,
        cwd=ctx.worktree,
        schema=schemas.COMMIT_RESULT,
        user_setup=True,
        log_path=ctx.log_path,
        repo=ctx.repo,
    )
    ctx.emit("agent_event", phase="start", role="committer", task=ctx.task.id, model=ctx.role.model, step="commit")
    result, excluded = git_ops.run_excluding_new_untracked(ctx.worktree, lambda: ctx.runner.run(request))
    if excluded:
        ctx.emit("note", task=ctx.task.id, text=f"excluded files created by your Claude setup: {', '.join(excluded)}")
    ctx.after_run(ctx.role.agent, result)
    ctx.emit(
        "agent_event",
        phase="end",
        role="committer",
        task=ctx.task.id,
        model=ctx.role.model,
        step="commit",
        ok=result.ok,
        error_kind=result.error_kind,
    )
    if result.error_kind == "model_unavailable":
        return CommitOutcome(
            ok=False, error=result.error, error_kind=result.error_kind, model_unavailable=ctx.role.model
        )

    if guard.is_subscription_violation(result):
        return CommitOutcome(
            ok=False, error=result.error, error_kind=result.error_kind, subscription_violation=True
        )

    try:
        head_after = git_ops.head_sha(ctx.worktree)
        branch_now = git_ops.current_branch(ctx.worktree)
        tree_after = git_ops.commit_tree(ctx.worktree)
    except GitError as exc:
        return CommitOutcome(ok=False, error=f"cannot verify the commit: {exc}", error_kind=result.error_kind)

    if head_after == head_before:
        reason = result.error or _reported_error(result.structured) or "HEAD did not move: nothing was committed"
        return CommitOutcome(ok=False, error=reason, error_kind=result.error_kind)
    if branch_now != ctx.branch:
        return CommitOutcome(
            ok=False,
            sha=head_after,
            error=f"the commit landed on branch '{branch_now}', expected '{ctx.branch}'",
        )
    try:
        problem = structure_problem(ctx.worktree, head_before)
        if problem:
            return CommitOutcome(ok=False, sha=head_after, error=problem)
        ctx.on_commit(head_after)
        problem = signature_problem(ctx.worktree)
    except GitError as exc:
        return CommitOutcome(ok=False, sha=head_after, error=f"cannot verify the commit: {exc}")
    if problem:
        return CommitOutcome(ok=False, sha=head_after, error=problem)
    if tree_after != ctx.approved_tree:
        return CommitOutcome(
            ok=False,
            sha=head_after,
            tree_mismatch=True,
            error=f"committed tree {tree_after} differs from the approved tree {ctx.approved_tree}",
        )
    ctx.emit("committed", task=ctx.task.id, sha=head_after, tree=tree_after)
    return CommitOutcome(ok=True, sha=head_after)


def _reported_error(structured: dict | None) -> str | None:
    if not structured:
        return None
    if structured.get("committed") is False:
        return structured.get("error") or "the committer reported committed=false"
    return None
