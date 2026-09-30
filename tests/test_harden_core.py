"""Hardening of the core loop: HEAD movement, the commit rule, plan ids, read-only roles, sidecar locking."""

import contextlib
import re
import subprocess
import threading
from pathlib import Path

import pytest

from workforce import config as config_mod
from workforce import git_ops
from workforce.agents import guard
from workforce.agents.base import AgentRequest, AgentResult
from workforce.decider.base import NullDecider
from workforce.events import EventLog
from workforce.orchestrator import HeadMoved, Orchestrator, validate_plan
from workforce.state import Task

TASK_PATTERN = re.compile(r"## Task (T\d+):")
PLAN_TASK = {
    "id": "T1",
    "title": "Add t1",
    "description": "Create t1.txt",
    "acceptance": ["the file exists"],
    "depends_on": [],
}


def git(cwd, *args):
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class NoSleep:
    def __init__(self):
        self.sleeps = []

    def time(self):
        return 1_800_000_000.0

    def sleep(self, seconds):
        self.sleeps.append(seconds)


class Team:
    """Both CLIs in one object. Each kind can be overridden by a callable in `overrides`."""

    def __init__(self, repo, plan_tasks=None):
        self.repo = repo
        self.plan_tasks = [PLAN_TASK] if plan_tasks is None else plan_tasks
        self.overrides = {}
        self.requests = []

    def run(self, req, on_event=None):
        kind = self._kind(req)
        self.requests.append((kind, req))
        if kind in self.overrides:
            return self.overrides[kind](req)
        return getattr(self, f"_{kind}")(req)

    def of(self, kind):
        return [req for k, req in self.requests if k == kind]

    @staticmethod
    def _kind(req):
        prompt = req.prompt
        if "# Role: plan reviewer" in prompt:
            return "plan_review"
        if "# Role: planner" in prompt:
            return "plan"
        if "# Role: committer (rebase)" in prompt:
            return "rebase"
        if "# Role: committer" in prompt:
            return "commit"
        if req.role.startswith("reviewer_"):
            return "reviewer"
        return "coder"

    @staticmethod
    def ok(req, text="", structured=None):
        return AgentResult(ok=True, text=text, structured=structured, session_id=f"s-{req.role}", model=req.model)

    def _plan(self, req):
        return self.ok(req, structured={"tasks": self.plan_tasks, "risks": [], "open_questions": []})

    def _plan_review(self, req):
        return self.ok(req, structured={"agree": True, "objections": [], "suggested_changes": []})

    def _coder(self, req):
        (Path(req.cwd) / "t1.txt").write_text("T1\n")
        return self.ok(req, text="Created t1.txt")

    def _reviewer(self, req):
        return self.ok(req, structured={"verdict": "APPROVE", "summary": "fine", "findings": []})

    def _commit(self, req):
        git(req.cwd, "add", "-A")
        git(req.cwd, "commit", "-q", "-m", "T1: change")
        sha = git(req.cwd, "rev-parse", "HEAD")
        return self.ok(req, structured={"committed": True, "sha": sha, "message": "m", "error": None})

    def _rebase(self, req):
        sha = git(req.cwd, "rev-parse", "HEAD")
        return self.ok(req, structured={"committed": True, "sha": sha, "message": "m", "error": None})


def failed(kind, message):
    return AgentResult(
        ok=False, text="", structured=None, session_id=None, model=None, error_kind=kind, error=message
    )


@pytest.fixture
def make(tmp_path, git_repo, monkeypatch):
    def build(team=None):
        team = team or Team(git_repo)
        config = config_mod.parse(config_mod.default_toml().replace('backend = "laya"', 'backend = "off"'))
        orch = Orchestrator(
            git_repo,
            config,
            {"claude": team, "codex": team},
            NullDecider(),
            clock=NoSleep(),
            home=tmp_path / "home",
            environ={},
        )
        monkeypatch.setattr(orch, "_preflight", lambda: None)
        return orch, team

    return build


def seed_task(orch, model="claude-sonnet-5-5"):
    orch.start("build things", "auto")

    def fn(run):
        run.tasks = [
            Task(
                id="T1",
                title="Add t1",
                description="Create t1.txt",
                acceptance=["the file exists"],
                model=model,
                effort="high",
                agent="claude",
            )
        ]
        run.status = "running"

    orch.store.update(fn)
    assert orch.prepare(orch.load()).action == "prepared"


def merge_into_integration(orch, task):
    """Fast-forward the task branch into the run's integration branch, as the merge step does."""
    git_ops.merge_ff_only_in(orch.integration_worktree(orch.load(), task), task.branch)


def drive(orch, limit=60):
    for _ in range(limit):
        if not orch.step().progressed:
            break
    return orch.load()


def question_kinds(orch):
    run = orch.load()
    return [orch._meta(run.id)["questions"][q.id]["kind"] for q in run.questions]


class TestPlanIds:
    @pytest.mark.parametrize(
        "bad", ["../../../../tmp/evil", "", "T 3;$(x)", "T1/x", "t1", "T1234", "T", "T-1", "T1\n", " T1"]
    )
    def test_validate_plan_rejects_ids_that_are_not_t_and_up_to_three_digits(self, bad):
        plan = {"tasks": [{**PLAN_TASK, "id": bad}]}
        problem = validate_plan(plan)
        assert problem and "task id" in problem

    @pytest.mark.parametrize("good", ["T1", "T12", "T999", "T007"])
    def test_validate_plan_accepts_well_formed_ids(self, good):
        assert validate_plan({"tasks": [{**PLAN_TASK, "id": good}]}) is None

    def test_planner_is_told_why_its_plan_was_rejected_and_retries(self, make):
        team = Team(Path("."), plan_tasks=[{**PLAN_TASK, "id": "../evil"}])
        orch, team = make(team)
        orch.start("goal", "auto")
        assert orch.step().action == "retry"
        assert orch.load().tasks == []
        team.plan_tasks = [PLAN_TASK]
        assert orch.step().action == "plan"
        first, second = team.of("plan")
        assert "rejected" not in first.prompt
        assert "Your previous reply was rejected" in second.prompt and "../evil" in second.prompt
        assert [t.id for t in orch.load().tasks] == ["T1"]
        assert orch._meta(orch.load().id)["plan_problem"] == ""


class TestReadOnlyRoles:
    def test_planner_and_plan_reviewer_run_read_only(self, make):
        orch, team = make()
        orch.start("goal", "auto")
        orch.step()
        orch.step()
        assert [r.sandbox for r in team.of("plan")] == ["read-only"]
        assert [r.sandbox for r in team.of("plan_review")] == ["read-only"]

    def test_reviewers_run_read_only(self, make):
        orch, team = make()
        seed_task(orch)
        drive(orch)
        assert team.of("reviewer") and all(r.sandbox == "read-only" for r in team.of("reviewer"))
        assert all(r.sandbox == "workspace-write" for r in team.of("coder") + team.of("commit"))


class TestHeadMovement:
    def test_a_user_commit_in_the_checkout_during_a_slow_plan_review_is_not_a_question(self, make, git_repo):
        orch, team = make()
        committed = threading.Event()

        def user_works_meanwhile():
            (git_repo / "user.txt").write_text("the user's own work\n")
            git(git_repo, "add", "-A")
            git(git_repo, "commit", "-q", "-m", "user commit")
            committed.set()

        def slow_review(req):
            threading.Thread(target=user_works_meanwhile).start()
            assert committed.wait(10)
            return team.ok(req, structured={"agree": True, "objections": [], "suggested_changes": []})

        team.overrides["plan_review"] = slow_review
        orch.start("goal", "auto")
        orch.step()
        outcome = orch.step()
        assert outcome.action != "question"
        run = orch.load()
        assert run.questions == [] and run.status == "awaiting_models"
        assert git(git_repo, "log", "-1", "--format=%s") == "user commit"
        assert not events(orch, "error")

    def test_a_user_commit_during_the_planner_run_is_not_a_question_either(self, make, git_repo):
        orch, team = make()

        def user_commits(req):
            git(git_repo, "commit", "-q", "--allow-empty", "-m", "user commit")
            return team._plan(req)

        team.overrides["plan"] = user_commits
        orch.start("goal", "auto")
        assert orch.step().action == "plan"
        assert orch.load().questions == []

    def test_a_reviewer_that_commits_is_a_question_and_nothing_is_merged(self, make, git_repo):
        orch, team = make()
        seed_task(orch)
        main_before = git_ops.head_sha(git_repo)

        def rogue(req):
            if req.role == "reviewer_claude":
                git(req.cwd, "add", "-A")
                git(req.cwd, "commit", "-q", "-m", "rogue reviewer commit")
            return team._reviewer(req)

        team.overrides["reviewer"] = rogue
        run = drive(orch)
        assert run.status == "awaiting_user"
        assert question_kinds(orch) == ["head_moved"]
        task = run.task("T1")
        assert task.status == "blocked" and task.reviews == []
        assert "reviewer_claude moved HEAD" in run.questions[0].text
        assert team.of("commit") == []
        assert git_ops.head_sha(git_repo) == main_before
        assert not [e for e in events(orch, "merged")]

    def test_a_reviewer_commit_is_never_taken_as_the_committers_even_after_the_user_answers(self, make, git_repo):
        orch, team = make()
        seed_task(orch)
        moved = []
        main_before = git_ops.head_sha(git_repo)

        def rogue(req):
            if req.role == "reviewer_claude" and not moved:
                git(req.cwd, "add", "-A")
                git(req.cwd, "commit", "-q", "-m", "rogue reviewer commit")
                moved.append(True)
            return team._reviewer(req)

        def nothing_left_to_commit(req):
            return team.ok(
                req, structured={"committed": False, "sha": None, "message": None, "error": "nothing to commit"}
            )

        team.overrides["reviewer"] = rogue
        team.overrides["commit"] = nothing_left_to_commit
        drive(orch)
        orch.answer(orch.load().questions[0].id, "go on")
        run = drive(orch)
        task = run.task("T1")
        assert task.status != "merged"
        assert len(team.of("commit")) >= 1
        assert git_ops.head_sha(git_repo) == main_before
        assert not events(orch, "merged")
        assert not orch.commit_exists(task)

    def test_launch_raises_for_a_non_committer_that_moves_head(self, make, git_repo):
        orch, team = make()
        seed_task(orch)

        def mover(req):
            git(req.cwd, "commit", "-q", "--allow-empty", "-m", "moved")
            return team.ok(req, text="hi")

        team.overrides["coder"] = mover
        task = orch.load().task("T1")
        request = AgentRequest(
            role="plan_reviewer", agent="claude", model="m", effort="high", prompt="x",
            cwd=Path(task.worktree or orch.integration_worktree(orch.load(), task)), sandbox="read-only",
        )
        assert orch._is_managed_worktree(request.cwd)
        with pytest.raises(HeadMoved, match="plan_reviewer moved HEAD"):
            orch.launch(request, None, "plan_review")

    def test_a_run_at_the_workspace_root_or_in_a_user_checkout_is_never_head_checked(self, make, git_repo):
        orch, team = make()
        seed_task(orch)

        def mover(req):
            git(git_repo, "commit", "-q", "--allow-empty", "-m", "user commit")
            return team.ok(req, text="hi")

        team.overrides["coder"] = mover
        request = AgentRequest(
            role="plan_reviewer", agent="claude", model="m", effort="high", prompt="x", cwd=git_repo, sandbox="read-only"
        )
        assert not orch._is_managed_worktree(git_repo)
        assert orch.launch(request, None, "plan_review").ok


def events(orch, kind):
    log, _ = EventLog(orch.paths).read()
    return [e for e in log if e["kind"] == kind]


def make_branch(orch, git_repo, tmp_path, commits=1):
    """A task worktree on wf/x/T1 with `commits` empty-message commits; returns the task."""
    base = git_ops.head_sha(git_repo)
    worktree = orch.paths.worktrees_root / "run-x" / "repo" / "T1"
    git_ops.create_worktree(git_repo, worktree, "wf/x/T1")
    shas = []
    for index in range(commits):
        (worktree / f"f{index}.txt").write_text(f"{index}\n")
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", f"c{index}")
        shas.append(git_ops.head_sha(worktree))
    task = Task(
        id="T1",
        title="t",
        description="d",
        branch="wf/x/T1",
        worktree=str(worktree),
        base_sha=base,
        head_sha=git_ops.commit_tree(worktree),
    )
    return task, shas


class TestCommitExists:
    def record(self, orch, sha):
        orch._record_committer_sha("T1", sha)

    def test_a_commit_nobody_recorded_does_not_count(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, shas = make_branch(orch, git_repo, tmp_path)
        assert orch.commit_exists(task) is False

    def test_a_recorded_single_commit_on_the_base_counts(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, shas = make_branch(orch, git_repo, tmp_path)
        self.record(orch, shas[-1])
        assert orch.commit_exists(task) is True

    def test_a_recorded_head_on_top_of_an_unrecorded_commit_does_not_count(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, shas = make_branch(orch, git_repo, tmp_path, commits=2)
        self.record(orch, shas[-1])
        assert orch.commit_exists(task) is False

    def test_a_chain_of_committer_commits_counts(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, shas = make_branch(orch, git_repo, tmp_path, commits=2)
        for sha in shas:
            self.record(orch, sha)
        assert orch.commit_exists(task) is True

    def test_the_tree_must_still_be_the_approved_one(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, shas = make_branch(orch, git_repo, tmp_path)
        self.record(orch, shas[-1])
        task.head_sha = git_ops.commit_tree(git_repo)
        assert orch.commit_exists(task) is False

    def test_no_commit_on_the_base_is_no_commit(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, shas = make_branch(orch, git_repo, tmp_path, commits=0)
        self.record(orch, task.base_sha)
        assert orch.commit_exists(task) is False

    def test_a_merge_commit_does_not_count(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, shas = make_branch(orch, git_repo, tmp_path)
        worktree = Path(task.worktree)
        git(git_repo, "commit", "-q", "--allow-empty", "-m", "main moved")
        git(worktree, "merge", "-q", "--no-ff", "main", "-m", "merge main")
        merge = git_ops.head_sha(worktree)
        task.head_sha = git_ops.commit_tree(worktree)
        self.record(orch, shas[-1])
        self.record(orch, merge)
        assert orch.commit_exists(task) is False

    def test_unsigned_commit_does_not_count_when_signing_is_on(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, shas = make_branch(orch, git_repo, tmp_path)
        self.record(orch, shas[-1])
        git(git_repo, "config", "commit.gpgsign", "true")
        assert orch.commit_exists(task) is False

    def test_the_commit_step_records_the_committers_sha_and_merges(self, make, git_repo):
        orch, team = make()
        seed_task(orch)
        run = drive(orch)
        assert run.status == "done"
        info = orch._meta(run.id)["merged"]["T1"]
        assert info["commit"] in orch._meta(run.id)["tasks"]["T1"]["committer_shas"]

    def test_a_rebase_by_the_committer_is_recorded_through_launch(self, make, git_repo, tmp_path):
        orch, team = make()
        seed_task(orch)
        task, shas = make_branch(orch, git_repo, tmp_path)
        assert orch.commit_exists(task) is False

        def rebase(req):
            git(req.cwd, "commit", "-q", "--allow-empty", "-m", "rebased")
            return team.ok(req, text="ok")

        team.overrides["rebase"] = rebase
        request = AgentRequest(
            role="committer",
            agent="claude",
            model="m",
            effort="high",
            prompt="# Role: committer (rebase)",
            cwd=Path(task.worktree),
            user_setup=True,
        )
        orch.launch(request, "T1", "rebase")
        assert git_ops.head_sha(Path(task.worktree)) in orch._meta(orch.load().id)["tasks"]["T1"]["committer_shas"]

    def test_a_failed_committer_run_that_moved_head_is_not_recorded(self, make, git_repo, tmp_path):
        orch, team = make()
        seed_task(orch)
        task, _ = make_branch(orch, git_repo, tmp_path)

        def broken(req):
            git(req.cwd, "commit", "-q", "--allow-empty", "-m", "half a rebase")
            return failed("crash", "boom")

        team.overrides["rebase"] = broken
        request = AgentRequest(
            role="committer", agent="claude", model="m", effort="high",
            prompt="# Role: committer (rebase)", cwd=Path(task.worktree), user_setup=True,
        )
        orch.launch(request, "T1", "rebase")
        assert orch._meta(orch.load().id)["tasks"].get("T1", {}).get("committer_shas", []) == []


class TestAlreadyMerged:
    def test_a_branch_that_never_got_a_commit_is_not_merged(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, _ = make_branch(orch, git_repo, tmp_path, commits=0)
        git_ops.remove_worktree(git_repo, task.worktree, managed_root=tmp_path / "elsewhere")
        assert not Path(task.worktree).exists() and git_ops.branch_exists(git_repo, task.branch)
        assert orch.already_merged(task) is False

    def test_a_merged_branch_with_the_approved_tree_is_merged(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, _ = make_branch(orch, git_repo, tmp_path)
        git_ops.remove_worktree(git_repo, task.worktree, managed_root=tmp_path / "elsewhere")
        merge_into_integration(orch, task)
        assert orch.already_merged(task) is True

    def test_a_merged_branch_with_a_different_tree_is_not_the_approved_work(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, _ = make_branch(orch, git_repo, tmp_path)
        git_ops.remove_worktree(git_repo, task.worktree, managed_root=tmp_path / "elsewhere")
        merge_into_integration(orch, task)
        task.head_sha = "0" * 40
        assert orch.already_merged(task) is False

    def test_an_unmerged_branch_with_commits_is_not_merged(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, _ = make_branch(orch, git_repo, tmp_path)
        git_ops.remove_worktree(git_repo, task.worktree, managed_root=tmp_path / "elsewhere")
        assert orch.already_merged(task) is False

    def test_a_missing_branch_is_not_merged(self, make, git_repo, tmp_path):
        orch, _ = make()
        seed_task(orch)
        task, _ = make_branch(orch, git_repo, tmp_path)
        git_ops.remove_worktree(git_repo, task.worktree, branch=task.branch, force_branch=True, managed_root=tmp_path / "elsewhere")
        assert orch.already_merged(task) is False

    def test_a_task_whose_worktree_vanished_before_committing_is_not_recorded_merged(self, make, git_repo):
        orch, team = make()
        seed_task(orch)
        for _ in range(40):
            if orch.load().task("T1").status == "committing":
                break
            orch.step()
        task = orch.load().task("T1")
        assert task.status == "committing"
        git_ops.remove_worktree(git_repo, task.worktree, managed_root=orch.managed_root)
        assert orch.already_merged(task) is False


class TestSubscriptionViolation:
    def violation(self):
        return failed("auth", f"{guard.SUBSCRIPTION_VIOLATION}: claude reported apiKeySource='ANTHROPIC_API_KEY'")

    def test_the_coder_is_never_retried_and_the_run_pauses_with_the_message(self, make):
        orch, team = make()
        seed_task(orch)
        team.overrides["coder"] = lambda req: self.violation()
        orch.step()
        outcome = orch.step()
        assert outcome.action == "paused"
        run = orch.load()
        assert run.status == "paused" and run.pause_reason.startswith(guard.SUBSCRIPTION_VIOLATION)
        assert run.task("T1").infra_failures == 0
        assert orch.clock.sleeps == []
        assert len(team.of("coder")) == 1

    def test_a_planner_violation_pauses_the_run_too(self, make):
        orch, team = make()
        team.overrides["plan"] = lambda req: self.violation()
        orch.start("goal", "auto")
        assert orch.step().action == "paused"
        assert orch.load().pause_reason.startswith(guard.SUBSCRIPTION_VIOLATION)
        assert orch.clock.sleeps == []

    def test_a_reviewer_violation_pauses_without_retrying(self, make):
        orch, team = make()
        seed_task(orch)
        team.overrides["reviewer"] = lambda req: self.violation() if req.role == "reviewer_claude" else team._reviewer(req)
        run = drive(orch)
        assert run.status == "paused"
        assert "reviewer_claude" in run.pause_reason and guard.SUBSCRIPTION_VIOLATION in run.pause_reason
        assert run.task("T1").infra_failures == 0

    def test_a_committer_violation_pauses_without_retrying(self, make):
        orch, team = make()
        seed_task(orch)
        team.overrides["commit"] = lambda req: self.violation()
        run = drive(orch)
        assert run.status == "paused" and guard.SUBSCRIPTION_VIOLATION in run.pause_reason

    def test_an_ordinary_auth_failure_still_retries(self, make):
        orch, team = make()
        seed_task(orch)
        team.overrides["coder"] = lambda req: failed("auth", "401 unauthorized")
        orch.step()
        assert orch.step().action == "retry"
        assert orch.load().task("T1").infra_failures == 1


class TestSidecarLock:
    def test_meta_edit_runs_inside_the_stores_write_lock(self, make):
        orch, _ = make()
        orch.start("goal", "auto")
        trail = []

        @contextlib.contextmanager
        def write_lock():
            trail.append("enter")
            yield
            trail.append("exit")

        orch.store.write_lock = write_lock

        def edit(meta):
            trail.append("edit")
            meta["plan_problem"] = "seen"

        run_id = orch.load().id
        orch.meta_edit(run_id, edit)
        assert trail == ["enter", "edit", "exit"]
        assert orch._meta(run_id)["plan_problem"] == "seen"

    def test_meta_edit_rereads_the_sidecar_inside_the_lock(self, make):
        orch, _ = make()
        orch.start("goal", "auto")
        run_id = orch.load().id

        @contextlib.contextmanager
        def write_lock():
            meta = orch._meta(run_id)
            meta["plan_failures"] = 41
            orch._save_meta(run_id, meta)
            yield

        orch.store.write_lock = write_lock
        orch.meta_edit(run_id, lambda m: m.__setitem__("plan_failures", m["plan_failures"] + 1))
        assert orch._meta(run_id)["plan_failures"] == 42

    def test_meta_edit_works_while_the_store_has_no_write_lock(self, make):
        orch, _ = make()
        orch.start("goal", "auto")
        run_id = orch.load().id
        orch.store.write_lock = None
        orch.meta_edit(run_id, lambda m: m.__setitem__("plan_problem", "x"))
        assert orch._meta(run_id)["plan_problem"] == "x"


WORKER = """
import sys
from pathlib import Path
from workforce import config as config_mod
from workforce.decider.base import NullDecider
from workforce.orchestrator import Orchestrator

repo, home, run_id, count = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
config = config_mod.parse(config_mod.default_toml().replace('backend = "laya"', 'backend = "off"'))
orch = Orchestrator(repo, config, {}, NullDecider(), home=home, environ={})
for _ in range(count):
    orch.meta_edit(run_id, lambda m: m.__setitem__("plan_failures", m["plan_failures"] + 1))
"""


def test_two_processes_editing_the_sidecar_lose_no_updates(make, git_repo, tmp_path):
    import sys

    orch, _ = make()
    orch.start("goal", "auto")
    run_id = orch.load().id
    args = [sys.executable, "-c", WORKER, str(git_repo), str(tmp_path / "home"), run_id, "40"]
    procs = [subprocess.Popen(args, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
    assert orch._meta(run_id)["plan_failures"] == 120


class TestEventsCarryNoSecrets:
    def test_an_agent_error_with_a_key_in_it_is_redacted_in_the_event_log(self, make):
        orch, team = make()
        seed_task(orch)
        team.overrides["coder"] = lambda req: failed("crash", "boom ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrst here")
        orch.step()
        orch.step()
        text = " ".join(e["message"] for e in events(orch, "error"))
        assert "sk-ant-abcdefghijklmnopqrst" not in text and "[redacted]" in text
        assert "boom" in text

    def test_the_subscription_violation_reason_stays_readable(self, make):
        orch, team = make()
        seed_task(orch)
        team.overrides["coder"] = lambda req: failed(
            "auth", f"{guard.SUBSCRIPTION_VIOLATION}: claude reported apiKeySource='ANTHROPIC_API_KEY'"
        )
        orch.step()
        orch.step()
        assert "apiKeySource='ANTHROPIC_API_KEY'" in events(orch, "paused")[0]["reason"]
