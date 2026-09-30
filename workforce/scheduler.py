"""The v3 scheduler: independent tasks run in parallel worktrees and merge through one queue per repo.

The per-task pipeline (code, checks, review, commit, merge) is the orchestrator's own; this module only
decides which task steps run when, and owns the merge queues. One `parallel` pool is shared by every repo.
Within a repo, commits, rebases and merges all happen while holding that repo's queue, so its integration
branch moves one merge at a time; different repos never conflict, so their queues run side by side.
Commits and committer rebases are serialised across ALL repos by one global lock, so the user never gets two
confirmation prompts at once.
A task whose dependency lives in another repo starts only after that dependency is merged.
"""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from workforce import checks, git_ops, prompts, schemas
from workforce.agents.base import AgentRequest
from workforce.errors import GitError, WorkforceError
from workforce.orchestrator import ACTIVE_TASK_STATUSES, Orchestrator, StepOutcome
from workforce.state import Run, Task

POLL_S = 0.2
QUEUE_POLL_S = 0.1
MAIN_TASK_TAG = "integration"
STALL_LIMIT = 3
CHECKS_CLIP_CHARS = 900
MERGE_BLOCKED = "merge_blocked"
MAIN_CHECKS_FAILED = "main_checks_failed"
INTERRUPTED_REASON = "interrupted"


class MergeQueue:
    """First come, first served: a task waits here until it is at the head and nobody else is merging."""

    def __init__(self, poll_s: float = QUEUE_POLL_S) -> None:
        self._cond = threading.Condition()
        self._waiting: list[str] = []
        self._active: str | None = None
        self._poll_s = poll_s

    def join(self, task_id: str) -> None:
        """Take a place at the back of the line unless the task already has one."""
        with self._cond:
            if task_id not in self._waiting and self._active != task_id:
                self._waiting.append(task_id)
                self._cond.notify_all()

    def acquire(self, task_id: str, keep_waiting: Callable[[], bool]) -> bool:
        """Block until `task_id` may merge; False (and the place is given up) once `keep_waiting()` says stop."""
        with self._cond:
            while True:
                if not keep_waiting():
                    if task_id in self._waiting:
                        self._waiting.remove(task_id)
                    self._cond.notify_all()
                    return False
                if self._active is None and self._waiting and self._waiting[0] == task_id:
                    self._waiting.pop(0)
                    self._active = task_id
                    return True
                self._cond.wait(self._poll_s)

    def release(self, task_id: str) -> None:
        with self._cond:
            if self._active == task_id:
                self._active = None
            self._cond.notify_all()

    def order(self) -> list[str]:
        """Task ids in the order they will merge: the one merging now, then the ones waiting."""
        with self._cond:
            return ([self._active] if self._active else []) + list(self._waiting)


class Scheduler:
    """Runs a run's ready tasks concurrently, up to `parallel`, and merges them one at a time."""

    def __init__(self, orch: Orchestrator, parallel: int):
        if parallel < 1:
            raise WorkforceError(f"parallel must be at least 1 (got {parallel})")
        self.orch = orch
        self.parallel = parallel
        self.queues: dict[str, MergeQueue] = {}
        self._queues_lock = threading.Lock()
        self._commit_lock = threading.Lock()
        self._halt = threading.Event()
        self._rebases: dict[str, int] = {}
        self._stalls: dict[str, int] = {}

    def queue_for(self, repo_name: str) -> MergeQueue:
        """The merge queue of one repo (created on first use)."""
        with self._queues_lock:
            if repo_name not in self.queues:
                self.queues[repo_name] = MergeQueue()
            return self.queues[repo_name]

    def run_until_blocked(self) -> Run:
        """Plan and debate sequentially, prepare the integration branches, then run tasks in parallel.

        Stops when the run is done, paused or waiting on the user. The integration review at the end runs as
        ordinary orchestrator steps. Holds the runner lock throughout, like `Orchestrator.run_until_blocked`.
        """
        orch = self.orch
        with orch.store.lock():
            self._halt.clear()
            self._stalls.clear()
            while True:
                run = orch.load()
                if run.status in ("planning", "debating"):
                    if not orch.step().progressed:
                        break
                    continue
                if run.status == "running":
                    if orch.prepare(run) is not None:
                        continue
                    if self._run_parallel():
                        continue
                break
        return orch.load()

    def _run_parallel(self) -> bool:
        """Run ready tasks until none is active. True when every task merged and the run needs more finishing steps."""
        orch = self.orch
        active: dict[str, tuple[Future, tuple]] = {}
        with ThreadPoolExecutor(max_workers=self.parallel, thread_name_prefix="wf-task") as pool:
            try:
                while True:
                    self._reap(active)
                    run = orch.load()
                    launching = run.status == "running" and not self._halt.is_set()
                    started = self._launch_ready(run, pool, active) if launching else False
                    if not active:
                        if launching and not started:
                            return self._settle(run)
                        return False
                    wait([future for future, _ in active.values()], timeout=POLL_S, return_when=FIRST_COMPLETED)
            except BaseException as exc:
                self._abort(active, exc)
                raise

    def _abort(self, active: dict[str, tuple[Future, tuple]], exc: BaseException) -> None:
        """Stop launching, drop queued work and pause the run; running steps end at their next check."""
        self._halt.set()
        for future, _ in active.values():
            future.cancel()
        reason = f"internal error: {exc}" if isinstance(exc, Exception) else INTERRUPTED_REASON
        try:
            self.orch.pause(reason)
        except Exception as pause_exc:
            self.orch.emit("error", message=f"could not pause the run after {type(exc).__name__}: {pause_exc}")

    def _reap(self, active: dict[str, tuple[Future, tuple]]) -> None:
        for task_id, (future, launched_at) in list(active.items()):
            if not future.done():
                continue
            del active[task_id]
            task = self.orch.load().task(task_id)
            if (task.status, task.head_sha, task.base_sha) == launched_at and task.status not in ("merged", "blocked"):
                self._stalls[task_id] = self._stalls.get(task_id, 0) + 1
                if self._stalls[task_id] >= STALL_LIMIT and self.orch.load().status == "running":
                    self.orch.pause(f"{task_id} is not making progress ({task.status})")
            else:
                self._stalls.pop(task_id, None)

    def _settle(self, run: Run) -> bool:
        """Nothing is running and nothing can start: either every task merged or the run is stuck.

        Finishing (integration review, fix planning, done) is the orchestrator's; returns True when it left the run
        `running`, so the caller loops and lets it continue.
        """
        run = self.orch.load()
        if run.status != "running" or any(task.status not in ("merged", "pending") for task in run.tasks):
            return False
        merged = {task.id for task in run.tasks if task.status == "merged"}
        if any(task.status == "pending" and set(task.depends_on) <= merged for task in run.tasks):
            return False
        outcome = self.orch.finish_run(run)
        return outcome.progressed and self.orch.load().status == "running"

    def _launch_ready(self, run: Run, pool: ThreadPoolExecutor, active: dict[str, tuple[Future, tuple]]) -> bool:
        orch = self.orch
        merged = {task.id for task in run.tasks if task.status == "merged"}
        started = False
        for task in run.tasks:
            if len(active) >= self.parallel:
                break
            if task.id in active:
                continue
            if task.status == "pending":
                if not set(task.depends_on) <= merged:
                    continue
                agent = task.agent or orch.config.role("coder").agent
                reason = orch.gate_reason((agent,))
                if reason:
                    orch.pause(reason)
                    return started
                outcome = orch.step_task(task.id)
                if outcome.action != "worktree":
                    return started
                run = orch.load()
                task = run.task(task.id)
            elif task.status not in ACTIVE_TASK_STATUSES:
                continue
            launched_at = (task.status, task.head_sha, task.base_sha)
            active[task.id] = (pool.submit(self._worker, task.id), launched_at)
            started = True
        return started

    def _worker(self, task_id: str) -> None:
        orch = self.orch
        try:
            self._drive_task(task_id)
        except GitError as exc:
            orch.emit("error", run=orch.load().id, task=task_id, message=f"git error: {exc}")
            orch.pause(f"git error: {exc}")
        except Exception as exc:
            orch.emit("error", run=orch.load().id, task=task_id, message=f"{type(exc).__name__}: {exc}")
            orch.pause(f"internal error in {task_id}: {exc}")

    def _may_continue(self) -> bool:
        run = self.orch.store.load()
        return run is not None and run.status == "running" and not self._halt.is_set()

    def _drive_task(self, task_id: str) -> None:
        orch = self.orch
        while self._may_continue():
            run = orch.load()
            task = run.task(task_id)
            if task.status in ("pending", "merged", "blocked", "failed"):
                return
            if task.status == "committing":
                if self._merge_turn(task_id) == "stopped":
                    return
                continue
            if not orch.step_task(task_id).progressed:
                return

    def _merge_turn(self, task_id: str) -> str:
        queue = self.queue_for(self.orch.task_repo(self.orch.load().task(task_id)))
        queue.join(task_id)
        if not queue.acquire(task_id, self._may_continue):
            return "stopped"
        try:
            return self._merge_locked(task_id)
        finally:
            queue.release(task_id)

    def _merge_locked(self, task_id: str) -> str:
        orch = self.orch
        if not self._may_continue():
            return "stopped"
        run = orch.load()
        task = run.task(task_id)
        if task.status != "committing":
            return "continue"
        if self._rebased_since_approval(task):
            return self._recheck_after_rebase(run, task)
        if orch.commit_exists(task) and not orch.already_merged(task):
            return self._merge_committed(run, task)
        with self._commit_lock:
            if not self._may_continue():
                return "stopped"
            outcome = orch.step_task(task_id)
        if outcome.action == "committed":
            run = orch.load()
            return self._merge_committed(run, run.task(task_id))
        if outcome.action == "merged":
            self._check_main(task_id)
        return "continue"

    def _merge_committed(self, run: Run, task: Task) -> str:
        """Merge a task whose commit exists, after checking the approvals cover exactly what is on the branch."""
        orch = self.orch
        problem = self._approval_problem(task)
        if problem:
            self._void_approvals(run, task, problem)
            return "continue"
        if not self._can_fast_forward(task):
            return self._rebase(run, task)
        if orch.step_task(task.id).action == "merged":
            self._check_main(task.id)
        return "continue"

    def _approval_problem(self, task: Task) -> str | None:
        latest = {item.reviewer: item for item in task.reviews}
        if set(latest) != {"claude", "codex"}:
            return "both reviewers have not approved"
        for item in latest.values():
            if item.verdict != "APPROVE" or item.sha != task.head_sha or item.base_sha != task.base_sha:
                return f"the {item.reviewer} approval is not for the current tree and base"
        branch_tree = git_ops.commit_tree(Path(task.worktree))
        if branch_tree != task.head_sha:
            return f"the branch HEAD tree {branch_tree[:8]} is not the approved tree {(task.head_sha or '')[:8]}"
        return None

    def _void_approvals(self, run: Run, task: Task, problem: str) -> None:
        orch = self.orch
        orch.emit("error", run=run.id, task=task.id, message=f"approvals voided before the merge: {problem}")

        def void(r: Run) -> None:
            item = r.task(task.id)
            item.reviews = []
            item.head_sha = None

        orch.store.update(void)
        orch.set_task(task.id, status="testing")

    def _can_fast_forward(self, task: Task) -> bool:
        return git_ops.can_fast_forward(self._integration(task), task.branch)

    def _integration(self, task: Task) -> Path:
        return self.orch.integration_worktree(self.orch.load(), task)

    def _rebase(self, run: Run, task: Task) -> str:
        orch = self.orch
        limit = orch.config.limits.review_rounds
        self._rebases[task.id] = self._rebases.get(task.id, 0) + 1
        if self._rebases[task.id] > limit:
            self._rebases[task.id] = 0
            orch.ask(
                MERGE_BLOCKED,
                f"{task.id} had to be rebased more than {limit} times because main kept moving. "
                "Tell us how to proceed.",
                {},
                task_id=task.id,
                resume_to="committing",
            )
            return "stopped"
        role = orch.config.role("committer")
        blocked = orch.gate_blocked(role.agent)
        if blocked:
            return "stopped"
        main_sha = git_ops.head_sha(self._integration(task))
        main_branch = orch.integration_branch(run, task)
        request = AgentRequest(
            role="committer",
            agent=role.agent,
            model=role.model,
            effort=role.effort,
            prompt=prompts.render(
                "rebase",
                decisions=orch.decisions(),
                task_id=task.id,
                title=task.title,
                description=task.description,
                branch=task.branch,
                main_branch=main_branch,
                main_sha=main_sha,
                repo_name=orch.task_repo(task),
            ),
            cwd=Path(task.worktree),
            schema=schemas.COMMIT_RESULT,
            user_setup=True,
            log_path=orch.log_path(run.id, task.id, "rebase"),
            repo=orch.root,
        )
        orch.emit(
            "note",
            task=task.id,
            text=f"{main_branch} moved on; the committer is rebasing {task.branch} (confirm if your git asks…)",
        )
        with self._commit_lock:
            if not self._may_continue():
                return "stopped"
            result = orch.launch(request, task.id, "rebase")
        if not result.ok:
            failure = orch.handle_failure(run, task, request, result)
            return "continue" if failure.action == "retry" else "stopped"
        problem = self._rebase_problem(task, main_sha)
        if problem:
            orch.emit("error", run=run.id, task=task.id, message=problem)
            orch.ask(
                MERGE_BLOCKED,
                f"{task.id} could not be rebased onto {main_branch}: {problem}",
                {},
                task_id=task.id,
                resume_to="committing",
            )
            return "stopped"
        self._void_and_recheck(run, task, main_sha, main_branch)
        return "rebased"

    def _rebased_since_approval(self, task: Task) -> bool:
        """True when the branch was already rebased onto a newer main but its re-test and re-review never ran.

        This is the state a run resumes into after a Question raised by the post-rebase check: the rebase
        commit exists, so committing again would be wrong; the approvals must be voided and redone instead.
        """
        if not task.worktree or not task.base_sha or not Path(task.worktree).exists():
            return False
        worktree = Path(task.worktree)
        main_sha = git_ops.head_sha(self._integration(task))
        return (
            main_sha != task.base_sha
            and git_ops.head_sha(worktree) != task.base_sha
            and self._can_fast_forward(task)
            and git_ops.commit_tree(worktree) != task.head_sha
        )

    def _recheck_after_rebase(self, run: Run, task: Task) -> str:
        orch = self.orch
        main_sha = git_ops.head_sha(self._integration(task))
        main_branch = orch.integration_branch(run, task)
        problem = self._rebase_problem(task, main_sha)
        if problem:
            orch.emit("error", run=run.id, task=task.id, message=problem)
            orch.ask(
                MERGE_BLOCKED,
                f"{task.id} was rebased onto {main_branch} but cannot be re-checked: {problem}",
                {},
                task_id=task.id,
                resume_to="committing",
            )
            return "stopped"
        orch.emit("note", task=task.id, text=f"{task.branch} is already rebased onto {main_branch}; re-testing and re-reviewing")
        self._void_and_recheck(run, task, main_sha, main_branch)
        return "continue"

    def _rebase_problem(self, task: Task, main_sha: str) -> str | None:
        worktree = Path(task.worktree)
        if git_ops.current_branch(worktree) != task.branch:
            return f"the worktree is no longer on {task.branch} after the rebase"
        if git_ops.tree_hash(worktree) != git_ops.commit_tree(worktree):
            return "the worktree has uncommitted changes after the rebase"
        if not self._can_fast_forward(task):
            return f"{task.branch} is still not based on the current integration branch ({main_sha[:8]}) after the rebase"
        return None

    def _void_and_recheck(self, run: Run, task: Task, main_sha: str, main_branch: str) -> None:
        orch = self.orch

        def edit(meta: dict) -> None:
            info = orch.task_meta(meta, task.id)
            info.update(check_fixes=0, drift=0, feedback="", checks="")
            info.pop("review_progress", None)

        orch.meta_edit(run.id, edit)

        def void(r: Run) -> None:
            item = r.task(task.id)
            item.reviews = []
            item.base_sha = main_sha
            item.head_sha = None
            item.review_round = 0
            item.infra_failures = 0

        orch.store.update(void)
        orch.set_task(task.id, status="testing")
        orch.emit(
            "note",
            task=task.id,
            text=f"rebased onto {main_branch} @ {main_sha[:7]}; both approvals are void, checks and reviews run again",
        )

    def _check_main(self, task_id: str) -> None:
        """Run the repo's full checks in its integration worktree after a merge; a failure stops the queues."""
        orch = self.orch
        run = orch.load()
        task = run.task(task_id)
        repo = orch.repo_info(task)
        report = checks.run(
            orch.integration_worktree(run, task),
            {"test": repo.checks.test, "lint": repo.checks.lint, "build": repo.checks.build},
            env_root=repo.path,
        )
        text = checks.summary(report)
        orch.emit(
            "check_result", task=MAIN_TASK_TAG, repo=repo.name, ok=report.ok, no_checks=report.no_checks, summary=text
        )
        if report.ok:
            return
        self._halt.set()
        clipped = text if len(text) <= CHECKS_CLIP_CHARS else text[-CHECKS_CLIP_CHARS:]
        orch.ask(
            MAIN_CHECKS_FAILED,
            f"The checks on the integration branch {orch.integration_branch(run, task)} of {repo.name} fail after "
            f"merging {task_id}, so the merge queues are stopped. Fix that branch, or tell us how to proceed.",
            {"checks": clipped},
            resume_run="running",
        )
