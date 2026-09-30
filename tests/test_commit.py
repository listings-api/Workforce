"""Unit tests for commit.commit_task with an in-process committer that runs real git in a temp repo."""

import subprocess
from pathlib import Path

import pytest

from workforce import commit, git_ops, schemas
from workforce.agents.base import AgentResult
from workforce.config import Role
from workforce.state import Task


def sh(repo: Path, script: str) -> str:
    return subprocess.run(
        script, shell=True, cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def committed_result(repo: Path) -> AgentResult:
    return AgentResult(
        ok=True,
        text="done",
        structured={"committed": True, "sha": git_ops.head_sha(repo), "message": "m", "error": None},
        session_id="s",
        model="claude-sonnet-5-5",
    )


def not_committed(error: str) -> AgentResult:
    return AgentResult(
        ok=True,
        text="",
        structured={"committed": False, "sha": None, "message": None, "error": error},
        session_id="s",
        model="claude-sonnet-5-5",
    )


def failure(kind: str, message: str = "boom") -> AgentResult:
    return AgentResult(
        ok=False, text="", structured=None, session_id=None, model=None, error_kind=kind, error=message
    )


class CommitterRunner:
    def __init__(self, action):
        self.action = action
        self.requests = []

    def run(self, request, on_event=None):
        self.requests.append(request)
        return self.action(request)


@pytest.fixture
def scene(git_repo):
    (git_repo / "feature.py").write_text("VALUE = 1\n")
    tree = git_ops.tree_hash(git_repo)
    events = []

    class Scene:
        repo = git_repo
        approved_tree = tree
        emitted = events

        def context(self, runner, branch="main", worktree=None):
            return commit.CommitContext(
                worktree=worktree or git_repo,
                branch=branch,
                task=Task(id="T1", title="Add feature", description="Add VALUE"),
                approved_tree=tree,
                role=Role(agent="claude", model="claude-sonnet-5-5", effort="high"),
                runner=runner,
                decisions="RULE-BETA",
                log_path=Path("/logs/commit-1.jsonl"),
                emit=lambda kind, **data: events.append((kind, data)),
            )

    return Scene()


def real_commit(request):
    sh(request.cwd, "git add -A && git commit -q -m 'T1: add feature'")
    return committed_result(request.cwd)


def test_successful_commit_is_verified_against_the_approved_tree(scene):
    runner = CommitterRunner(real_commit)
    before = git_ops.head_sha(scene.repo)
    outcome = commit.commit_task(scene.context(runner))
    assert outcome.ok and not outcome.tree_mismatch and outcome.error is None
    assert outcome.sha == git_ops.head_sha(scene.repo) != before
    assert git_ops.commit_tree(scene.repo) == scene.approved_tree

    request = runner.requests[0]
    assert request.role == "committer" and request.agent == "claude"
    assert request.user_setup is True
    assert request.cwd == scene.repo
    assert request.schema is schemas.COMMIT_RESULT
    assert request.resume_session is None
    assert request.log_path == Path("/logs/commit-1.jsonl")
    for needle in ("RULE-BETA", "T1", "Add feature", scene.approved_tree, "`main`"):
        assert needle in request.prompt
    kinds = [kind for kind, _ in scene.emitted]
    assert kinds[0] == "commit_waiting" and "committing" in scene.emitted[0][1]["message"]
    assert scene.emitted[0][1]["message"] == "👆 committing… (confirm if your git asks)"
    assert "committed" in kinds


def test_waiting_message_is_emitted_before_the_committer_runs(scene):
    order = []

    def action(request):
        order.append("ran")
        return real_commit(request)

    context = scene.context(CommitterRunner(action))
    original = context.emit
    context.emit = lambda kind, **data: (order.append(kind), original(kind, **data))
    commit.commit_task(context)
    assert order.index("commit_waiting") < order.index("ran")


def test_a_different_committed_tree_is_a_mismatch(scene):
    def action(request):
        (request.cwd / "stray.txt").write_text("stray\n")
        return real_commit(request)

    outcome = commit.commit_task(scene.context(CommitterRunner(action)))
    assert not outcome.ok and outcome.tree_mismatch
    assert outcome.sha == git_ops.head_sha(scene.repo)
    assert "differs" in outcome.error
    assert "committed" not in [kind for kind, _ in scene.emitted]


def test_committed_false_is_a_failure_and_head_stays_put(scene):
    before = git_ops.head_sha(scene.repo)
    outcome = commit.commit_task(scene.context(CommitterRunner(lambda r: not_committed("signing cancelled"))))
    assert not outcome.ok and not outcome.tree_mismatch
    assert outcome.error == "signing cancelled"
    assert git_ops.head_sha(scene.repo) == before


def test_claiming_success_without_moving_head_is_a_failure(scene):
    def liar(request):
        return AgentResult(
            ok=True,
            text="",
            structured={"committed": True, "sha": "abc", "message": "m", "error": None},
            session_id="s",
            model="m",
        )

    outcome = commit.commit_task(scene.context(CommitterRunner(liar)))
    assert not outcome.ok and not outcome.tree_mismatch
    assert "HEAD did not move" in outcome.error


def test_wrong_branch_is_a_failure(scene):
    outcome = commit.commit_task(scene.context(CommitterRunner(real_commit), branch="wf/run/T1"))
    assert not outcome.ok
    assert "'main'" in outcome.error and "wf/run/T1" in outcome.error


def test_model_unavailable_is_reported_and_nothing_is_verified(scene):
    outcome = commit.commit_task(scene.context(CommitterRunner(lambda r: failure("model_unavailable"))))
    assert not outcome.ok
    assert outcome.model_unavailable == "claude-sonnet-5-5"
    assert outcome.error_kind == "model_unavailable"


@pytest.mark.parametrize("kind", ["crash", "timeout", "auth", "rate_limited", "schema"])
def test_runner_failure_without_a_commit_fails_with_its_kind(scene, kind):
    outcome = commit.commit_task(scene.context(CommitterRunner(lambda r: failure(kind, "no tap"))))
    assert not outcome.ok and not outcome.tree_mismatch
    assert outcome.error_kind == kind and outcome.error == "no tap"
    assert outcome.model_unavailable is None


def test_runner_failure_after_a_correct_commit_still_counts_as_committed(scene):
    def action(request):
        sh(request.cwd, "git add -A && git commit -q -m 'T1: add feature'")
        return failure("timeout", "hung after committing")

    outcome = commit.commit_task(scene.context(CommitterRunner(action)))
    assert outcome.ok
    assert git_ops.commit_tree(scene.repo) == scene.approved_tree


def test_an_unreadable_worktree_is_a_failure_not_a_crash(scene, tmp_path):
    outcome = commit.commit_task(
        scene.context(CommitterRunner(real_commit), worktree=tmp_path / "missing")
    )
    assert not outcome.ok
    assert "HEAD" in outcome.error


def test_after_run_hook_sees_the_committer_run(scene):
    seen = []
    context = scene.context(CommitterRunner(real_commit))
    context.after_run = lambda agent, result: seen.append((agent, result.ok))
    assert commit.commit_task(context).ok
    assert seen == [("claude", True)]


def test_on_commit_is_called_with_the_new_sha_after_every_check(scene):
    recorded = []
    context = scene.context(CommitterRunner(real_commit))
    context.on_commit = recorded.append
    outcome = commit.commit_task(context)
    assert outcome.ok and recorded == [outcome.sha]


def test_on_commit_is_also_called_for_a_committer_commit_with_the_wrong_tree(scene):
    recorded = []

    def action(request):
        (request.cwd / "stray.txt").write_text("stray\n")
        return real_commit(request)

    context = scene.context(CommitterRunner(action))
    context.on_commit = recorded.append
    outcome = commit.commit_task(context)
    assert outcome.tree_mismatch
    assert recorded == [outcome.sha]


def test_on_commit_is_not_called_when_nothing_was_committed(scene):
    recorded = []
    context = scene.context(CommitterRunner(lambda r: not_committed("cancelled")))
    context.on_commit = recorded.append
    assert not commit.commit_task(context).ok
    assert recorded == []


def test_two_commits_are_rejected(scene):
    def action(request):
        sh(request.cwd, "git add -A && git commit -q -m one && git commit -q --allow-empty -m two")
        return committed_result(request.cwd)

    recorded = []
    context = scene.context(CommitterRunner(action))
    context.on_commit = recorded.append
    outcome = commit.commit_task(context)
    assert not outcome.ok and not outcome.tree_mismatch
    assert "2 commits" in outcome.error
    assert recorded == []


def test_a_merge_commit_is_rejected(scene):
    def action(request):
        sh(request.cwd, "git checkout -q -b side && git commit -q --allow-empty -m side && git checkout -q main")
        sh(request.cwd, "git add -A && git commit -q -m one && git merge -q --no-ff side -m merge")
        return committed_result(request.cwd)

    outcome = commit.commit_task(scene.context(CommitterRunner(action)))
    assert not outcome.ok
    assert "commits" in outcome.error or "parent" in outcome.error


def signed_repo(scene):
    sh(scene.repo, "git config commit.gpgsign true")


def test_unsigned_commit_is_rejected_when_signing_is_on(scene):
    signed_repo(scene)

    def action(request):
        sh(request.cwd, "git add -A && git -c commit.gpgsign=false commit -q -m one")
        return committed_result(request.cwd)

    recorded = []
    context = scene.context(CommitterRunner(action))
    context.on_commit = recorded.append
    outcome = commit.commit_task(context)
    assert not outcome.ok and not outcome.tree_mismatch
    assert "signature" in outcome.error and "'N'" in outcome.error
    assert recorded == [outcome.sha]
    assert commit.signature_problem(scene.repo) is not None


def test_signature_is_not_checked_when_signing_is_off(scene):
    assert commit.commit_task(scene.context(CommitterRunner(real_commit))).ok
    assert commit.signature_problem(scene.repo) is None


def test_subscription_violation_is_reported_and_not_verified(scene):
    from workforce.agents import guard

    violation = failure("auth", f"{guard.SUBSCRIPTION_VIOLATION}: apiKeySource='ANTHROPIC_API_KEY'")
    outcome = commit.commit_task(scene.context(CommitterRunner(lambda r: violation)))
    assert not outcome.ok and outcome.subscription_violation
    assert outcome.error_kind == "auth"


def test_the_committer_prompt_names_the_repo_and_branch(scene):
    runner = CommitterRunner(real_commit)
    ctx = scene.context(runner)
    ctx.repo_name = "backend/api"
    ctx.repo = scene.repo
    outcome = commit.commit_task(ctx)
    assert outcome.ok
    prompt = runner.requests[0].prompt
    assert "the repo `backend/api`" in prompt and "`main`" in prompt and "RULE-BETA" in prompt
    assert runner.requests[0].user_setup is True and runner.requests[0].repo == scene.repo


def test_the_committer_prompt_has_a_placeholder_when_no_repo_name_is_given(scene):
    runner = CommitterRunner(real_commit)
    commit.commit_task(scene.context(runner))
    assert "the repo `(this repo)`" in runner.requests[0].prompt
