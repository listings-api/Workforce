"""Unit tests for prompts.render and review.double_review with in-process scripted runners."""

import string
import subprocess
import threading
from pathlib import Path

import pytest

from workforce import config as config_mod
from workforce import git_ops, prompts, review, schemas
from workforce.agents.base import AgentResult
from workforce.decider.base import Decision, NullDecider
from workforce.errors import WorkforceError
from workforce.state import Task

TEMPLATES = (
    "planner",
    "plan_reviewer",
    "planner_revise",
    "coder",
    "coder_fix",
    "reviewer",
    "reviewer_reconcile",
    "committer",
    "integration_reviewer",
    "integration_fix",
)


def template_fields(name: str) -> set[str]:
    text = (Path(review.__file__).parent / "prompts" / f"{name}.md").read_text()
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_prompt_states_the_user_rules_and_renders_with_braces_in_values(name):
    fields = template_fields(name)
    assert "decisions" in fields
    values = {field: "{value with braces} %s" % field for field in fields}
    values["decisions"] = "RULE-{ONE}"
    text = prompts.render(name, **values)
    assert "User rules (always follow)" in text
    assert "RULE-{ONE}" in text
    assert "{decisions}" not in text


@pytest.mark.parametrize("name", TEMPLATES)
def test_missing_prompt_variable_raises(name):
    fields = template_fields(name)
    values = {field: "x" for field in fields}
    dropped = sorted(fields)[0]
    del values[dropped]
    with pytest.raises(KeyError, match=dropped):
        prompts.render(name, **values)


def test_unknown_template_is_an_error():
    with pytest.raises(WorkforceError, match="nope"):
        prompts.render("nope")


def render_all(name, **overrides):
    values = {field: f"<{field}>" for field in template_fields(name)}
    values.update(overrides)
    return prompts.render(name, **values)


def test_coder_prompts_carry_the_hard_rules():
    for name in ("coder", "coder_fix"):
        text = render_all(name)
        assert "current directory" in text
        assert "Do not commit" in text or "do not commit" in text.lower()
        assert "Run the tests yourself" in text
        assert "short summary" in text


def test_reviewer_prompts_are_independent_read_only_and_judge_acceptance():
    text = render_all("reviewer")
    assert "read-only" in text
    assert "Read any repository file you need" in text
    assert "acceptance" in text.lower()
    assert "BLOCKED" in text and "cannot fix without a human decision" in text
    assert "cannot see their work" in text
    assert "verdict schema" in text


def test_committer_prompt_covers_signing_history_and_pushing():
    text = render_all("committer")
    assert "Touch ID" in text
    assert "Do not amend" in text
    assert "Do not push" in text
    assert "--no-gpg-sign" not in text and "commit.gpgsign" not in text
    assert "commit_result" in text


def test_planner_prompts_name_their_schemas():
    assert "plan schema" in render_all("planner")
    assert "plan_review schema" in render_all("plan_reviewer")
    assert "plan_revision schema" in render_all("planner_revise")


class ScriptedRunner:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []
        self.lock = threading.Lock()

    def run(self, request, on_event=None):
        with self.lock:
            self.requests.append(request)
            step = self.steps.pop(0)
        return step(request) if callable(step) else step


def finding(severity="minor", message="m", file="a.txt", line=1):
    return {"severity": severity, "file": file, "line": line, "message": message}


def verdict(v, summary="s", findings=(), session="sess", model="model-x"):
    return AgentResult(
        ok=True,
        text="",
        structured={"verdict": v, "summary": summary, "findings": list(findings)},
        session_id=session,
        model=model,
    )


def failed(kind, message="boom"):
    return AgentResult(
        ok=False, text="", structured=None, session_id=None, model=None, error_kind=kind, error=message
    )


class ScriptedDecider:
    def __init__(self, answers, available=True):
        self.answers = list(answers)
        self._available = available
        self.asked = []

    def available(self):
        return self._available

    def ask_bool(self, key, state, question):
        self.asked.append((key, state, question))
        answer, confidence = self.answers.pop(0)
        return Decision(key=key, question=question, answer=answer, confidence=confidence, source="laya")

    def ask_choice(self, key, state, question, options):
        raise AssertionError("review.py never asks a choice")


@pytest.fixture
def setup(git_repo):
    (git_repo / "feature.py").write_text("VALUE = 1\n")
    base = git_ops.head_sha(git_repo)
    tree = git_ops.tree_hash(git_repo)
    config = config_mod.parse(config_mod.default_toml())
    task = Task(
        id="T1",
        title="Add feature",
        description="Add VALUE to feature.py",
        acceptance=["feature.py defines VALUE"],
        base_sha=base,
    )

    class Setup:
        repo = git_repo
        events = []
        logs = []

        def context(self, claude, codex, decider=None, gate=None):
            self.events = []
            self.logs = []

            def log_path(step):
                self.logs.append(step)
                return Path(f"/logs/{step}.jsonl")

            return review.ReviewContext(
                worktree=git_repo,
                task=task,
                base_sha=base,
                tree=tree,
                checks_summary="Checks: OK CHECK-SUMMARY",
                decisions="RULE-ALPHA",
                config=config,
                runners={"claude": claude, "codex": codex},
                decider=decider or NullDecider(),
                log_path=log_path,
                emit=lambda kind, **data: self.events.append((kind, data)),
                gate=gate or (lambda: None),
            )

    setup = Setup()
    setup.base, setup.tree, setup.config = base, tree, config
    return setup


def both(setup, claude_steps, codex_steps, **kw):
    claude, codex = ScriptedRunner(claude_steps), ScriptedRunner(codex_steps)
    outcome = review.double_review(setup.context(claude, codex, **kw))
    return outcome, claude, codex


def test_both_approve_runs_concurrently_in_fresh_sessions(setup):
    barrier = threading.Barrier(2, timeout=10)

    def wait_then(result):
        def responder(request):
            barrier.wait()
            return result

        return responder

    outcome, claude, codex = both(
        setup,
        [wait_then(verdict("APPROVE", "claude ok", session="c1"))],
        [wait_then(verdict("APPROVE", "codex ok", session="x1"))],
    )
    assert outcome.approved and not outcome.blocked
    assert outcome.infra_error is None and not outcome.tree_changed
    assert len(claude.requests) == 1 and len(codex.requests) == 1
    assert {r.reviewer for r in outcome.reviews} == {"claude", "codex"}
    for item in outcome.reviews:
        assert item.sha == setup.tree and item.base_sha == setup.base and item.verdict == "APPROVE"
    assert {r.session for r in outcome.reviews} == {"c1", "x1"}

    c, x = claude.requests[0], codex.requests[0]
    assert c.resume_session is None and x.resume_session is None
    assert c.role == "reviewer_claude" and x.role == "reviewer_codex"
    assert c.agent == "claude" and x.agent == "codex"
    assert x.sandbox == "read-only" and x.cwd == setup.repo and c.cwd == setup.repo
    assert c.schema is schemas.VERDICT and x.schema is schemas.VERDICT
    assert c.model == setup.config.role("reviewer_claude").model
    assert x.effort == setup.config.role("reviewer_codex").effort
    assert c.prompt == x.prompt
    for needle in ("RULE-ALPHA", "CHECK-SUMMARY", "feature.py defines VALUE", setup.tree, setup.base, "+VALUE = 1"):
        assert needle in c.prompt
    assert sorted(setup.logs) == ["review_claude", "review_codex"]


def test_differing_verdicts_reconcile_in_the_same_sessions(setup):
    outcome, claude, codex = both(
        setup,
        [
            verdict("APPROVE", "CLAUDE-FIRST-SUMMARY", session="c1"),
            verdict("APPROVE", "claude convinced", session="c1"),
        ],
        [
            verdict("REQUEST_CHANGES", "CODEX-FIRST-SUMMARY", [finding("minor", "CODEX-ONLY-FINDING")], session="x1"),
            verdict("APPROVE", "codex convinced", session="x1"),
        ],
    )
    assert outcome.approved
    first_c, first_x = claude.requests[0], codex.requests[0]
    assert "CODEX-ONLY-FINDING" not in first_c.prompt and "CODEX-FIRST-SUMMARY" not in first_c.prompt
    assert "CLAUDE-FIRST-SUMMARY" not in first_x.prompt
    second_c, second_x = claude.requests[1], codex.requests[1]
    assert second_c.resume_session == "c1" and second_x.resume_session == "x1"
    assert "CODEX-ONLY-FINDING" in second_c.prompt and "CODEX-FIRST-SUMMARY" in second_c.prompt
    assert "CLAUDE-FIRST-SUMMARY" in second_x.prompt
    assert second_x.sandbox == "read-only"
    assert len(outcome.reviews) == 4
    assert [r.verdict for r in outcome.final_reviews] == ["APPROVE", "APPROVE"]
    assert "reconcile_claude" in setup.logs and "reconcile_codex" in setup.logs


def test_a_major_finding_triggers_reconciliation_even_when_verdicts_match(setup):
    outcome, claude, codex = both(
        setup,
        [
            verdict("APPROVE", "ok", [finding("major", "looks risky")]),
            verdict("APPROVE", "ok after all"),
        ],
        [verdict("APPROVE", "ok"), verdict("APPROVE", "ok")],
    )
    assert outcome.approved
    assert len(claude.requests) == 2 and len(codex.requests) == 2


def test_minor_findings_with_matching_verdicts_do_not_reconcile(setup):
    outcome, claude, codex = both(
        setup,
        [verdict("REQUEST_CHANGES", "c", [finding("minor", "rename x"), finding("nit", "typo")])],
        [verdict("REQUEST_CHANGES", "x", [finding("minor", "rename x"), finding("minor", "docstring")])],
    )
    assert not outcome.approved and not outcome.blocked
    assert len(claude.requests) == 1 and len(codex.requests) == 1
    messages = [item["message"] for item in outcome.merged_findings]
    assert sorted(messages) == ["docstring", "rename x", "typo"]
    assert [item["severity"] for item in outcome.merged_findings] == ["minor", "minor", "nit"]
    text = review.format_feedback(outcome)
    assert "Reviewer claude: REQUEST_CHANGES" in text and "docstring" in text and "typo" in text


def test_blocked_from_either_reviewer_is_reported(setup):
    outcome, _, _ = both(
        setup,
        [
            verdict("BLOCKED", "needs a decision", [finding("blocker", "which API?")]),
            verdict("BLOCKED", "still blocked", [finding("blocker", "which API?")]),
        ],
        [verdict("APPROVE", "fine"), verdict("APPROVE", "fine")],
    )
    assert outcome.blocked and not outcome.approved


@pytest.mark.parametrize("kind", ["crash", "timeout", "auth", "rate_limited", "schema"])
def test_infra_failures_are_never_a_verdict(setup, kind):
    outcome, claude, codex = both(
        setup,
        [verdict("APPROVE")],
        [failed(kind, "the reviewer fell over")],
    )
    assert outcome.infra_error and "reviewer_codex" in outcome.infra_error and kind in outcome.infra_error
    assert "the reviewer fell over" in outcome.infra_error
    assert not outcome.approved and not outcome.blocked
    assert outcome.model_unavailable is None
    assert len(claude.requests) == 1 and len(codex.requests) == 1


def test_both_reviewers_failing_reports_both(setup):
    outcome, _, _ = both(setup, [failed("crash", "one")], [failed("timeout", "two")])
    assert "reviewer_claude" in outcome.infra_error and "reviewer_codex" in outcome.infra_error


def test_model_unavailable_is_reported_by_model_name(setup):
    outcome, _, _ = both(setup, [verdict("APPROVE")], [failed("model_unavailable")])
    assert outcome.model_unavailable == setup.config.role("reviewer_codex").model
    assert outcome.infra_error is None and not outcome.approved


def test_reconciliation_failure_is_infra(setup):
    outcome, _, _ = both(
        setup,
        [verdict("APPROVE"), failed("crash", "died while reconciling")],
        [verdict("REQUEST_CHANGES", "x", [finding()]), verdict("REQUEST_CHANGES", "x")],
    )
    assert outcome.infra_error and "died while reconciling" in outcome.infra_error
    assert not outcome.approved


def test_reconciliation_without_a_session_is_infra(setup):
    outcome, _, _ = both(
        setup,
        [verdict("APPROVE", session=None)],
        [verdict("REQUEST_CHANGES", "x", [finding()], session="x1"), verdict("REQUEST_CHANGES", "x")],
    )
    assert outcome.infra_error and "session" in outcome.infra_error


def test_usage_gate_blocks_launches(setup):
    outcome, claude, codex = both(setup, [], [], gate=lambda: "claude 5-hour window at 61%")
    assert outcome.pause_reason == "claude 5-hour window at 61%"
    assert claude.requests == [] and codex.requests == []
    assert not outcome.approved


def test_usage_gate_is_checked_again_before_reconciliation(setup):
    calls = []

    def gate():
        calls.append(1)
        return None if len(calls) == 1 else "paused now"

    outcome, claude, codex = both(
        setup,
        [verdict("APPROVE")],
        [verdict("REQUEST_CHANGES", "x", [finding()])],
        gate=gate,
    )
    assert outcome.pause_reason == "paused now"
    assert len(claude.requests) == 1 and len(codex.requests) == 1


def test_approval_requires_the_same_tree_as_reviewed(setup):
    def edit_and_approve(request):
        (setup.repo / "feature.py").write_text("VALUE = 2\n")
        return verdict("APPROVE", "approved something that then changed")

    outcome, _, _ = both(setup, [edit_and_approve], [verdict("APPROVE")])
    assert outcome.tree_changed and not outcome.approved and not outcome.blocked


def test_confident_inconsistency_reruns_that_reviewer_fresh_once(setup):
    decider = ScriptedDecider([(False, 0.99), (True, 0.99), (True, 0.99)])
    outcome, claude, codex = both(
        setup,
        [verdict("APPROVE", "lists a bug", [finding("minor", "wart")], session="c1"), verdict("APPROVE", "clean", session="c2")],
        [verdict("APPROVE", "fine", session="x1")],
        decider=decider,
    )
    assert outcome.approved
    assert len(claude.requests) == 2 and len(codex.requests) == 1
    assert claude.requests[1].resume_session is None
    assert claude.requests[1].prompt == claude.requests[0].prompt
    assert {d[0] for d in decider.asked} == {"verdict_consistent"}
    assert any(kind == "decision" and data["key"] == "verdict_consistent" for kind, data in setup.events)
    final = {r.reviewer: r for r in outcome.final_reviews}
    assert final["claude"].session == "c2"


def test_still_inconsistent_after_the_rerun_counts_as_request_changes(setup):
    decider = ScriptedDecider([(False, 0.99), (False, 0.99), (True, 0.99)])
    outcome, claude, _ = both(
        setup,
        [verdict("APPROVE", "a"), verdict("APPROVE", "b", [finding("minor", "wart")])],
        [verdict("APPROVE", "fine")],
        decider=decider,
    )
    assert not outcome.approved
    assert len(claude.requests) == 2
    final = {r.reviewer: r for r in outcome.final_reviews}
    assert final["claude"].verdict == "REQUEST_CHANGES"
    assert any("did not match its findings" in f.message for f in final["claude"].findings)
    assert any("did not match its findings" in item["message"] for item in outcome.merged_findings)
    assert outcome.reviews[-1].verdict == "REQUEST_CHANGES"


def test_an_unsure_decider_counts_as_inconsistent(setup):
    decider = ScriptedDecider([(True, 0.4), (True, 0.99), (True, 0.99)])
    outcome, claude, codex = both(
        setup,
        [verdict("APPROVE", "a"), verdict("APPROVE", "a again")],
        [verdict("APPROVE", "b")],
        decider=decider,
    )
    assert outcome.approved
    assert len(claude.requests) == 2 and claude.requests[1].resume_session is None
    assert len(codex.requests) == 1
    assert len(decider.asked) == 3


def test_unsure_twice_forces_request_changes(setup):
    decider = ScriptedDecider([(True, 0.4), (True, 0.4), (True, 0.99)])
    outcome, claude, _ = both(
        setup,
        [verdict("APPROVE", "a"), verdict("APPROVE", "a again")],
        [verdict("APPROVE", "b")],
        decider=decider,
    )
    assert not outcome.approved
    assert {r.reviewer: r.verdict for r in outcome.final_reviews} == {
        "claude": "REQUEST_CHANGES",
        "codex": "APPROVE",
    }


def test_no_decider_trusts_clean_verdicts_but_not_approve_with_blockers(setup):
    outcome, claude, codex = both(setup, [verdict("APPROVE")], [verdict("APPROVE")])
    assert outcome.approved and len(claude.requests) == 1
    outcome, claude, _ = both(
        setup,
        [
            verdict("APPROVE", "a", [finding("blocker", "broken")]),
            verdict("APPROVE", "still", [finding("blocker", "broken")]),
            verdict("APPROVE", "again", [finding("blocker", "broken")]),
        ],
        [verdict("APPROVE"), verdict("APPROVE")],
    )
    assert not outcome.approved
    assert len(claude.requests) == 3
    final = {r.reviewer: r for r in outcome.final_reviews}
    assert final["claude"].verdict == "REQUEST_CHANGES"


def test_rerun_failure_after_inconsistency_is_infra(setup):
    decider = ScriptedDecider([(False, 0.99), (True, 0.99)])
    outcome, _, _ = both(
        setup,
        [verdict("APPROVE"), failed("crash", "rerun died")],
        [verdict("APPROVE")],
        decider=decider,
    )
    assert outcome.infra_error and "rerun died" in outcome.infra_error and not outcome.approved


def test_large_diffs_are_truncated_with_a_note(setup, monkeypatch):
    monkeypatch.setattr(review, "MAX_DIFF_CHARS", 40)
    _, claude, _ = both(setup, [verdict("APPROVE")], [verdict("APPROVE")])
    assert "diff truncated after 40 characters" in claude.requests[0].prompt


def test_format_findings_handles_missing_lines_and_files():
    rendered = review.format_findings(
        [
            review.Finding("major", "a.py", 3, "bad"),
            review.Finding("nit", "b.py", None, "meh"),
            review.Finding("minor", "", None, "general"),
        ]
    )
    assert "- [major] a.py:3: bad" in rendered
    assert "- [nit] b.py: meh" in rendered
    assert "- [minor] general" in rendered
    assert review.format_findings([]) == "- (none)"


def run_with(setup, claude_steps, codex_steps, progress=None, **kw):
    claude, codex = ScriptedRunner(claude_steps), ScriptedRunner(codex_steps)
    ctx = setup.context(claude, codex, **kw)
    ctx.progress = progress
    return review.double_review(ctx), claude, codex


def test_a_failed_first_review_keeps_the_other_verdict_and_retries_only_the_failed_one(setup):
    outcome, claude, codex = run_with(
        setup, [verdict("APPROVE", "claude ok", session="c1")], [failed("crash", "codex died")]
    )
    assert outcome.infra_error and not outcome.approved
    assert set(outcome.progress["firsts"]) == {"claude"}
    assert outcome.progress["tree"] == setup.tree and outcome.progress["base_sha"] == setup.base
    assert outcome.progress["seconds"] == {}

    retry, claude2, codex2 = run_with(
        setup, [], [verdict("APPROVE", "codex ok", session="x1")], progress=outcome.progress
    )
    assert retry.approved and retry.infra_error is None and retry.progress is None
    assert claude2.requests == [] and len(codex2.requests) == 1
    assert {r.reviewer for r in retry.reviews} == {"claude", "codex"}
    assert {r.session for r in retry.final_reviews} == {"c1", "x1"}
    assert retry.summaries["claude"] == "claude ok"
    assert any(kind == "note" and "reusing reviewer claude" in data["text"] for kind, data in setup.events)


def test_a_failed_reconciliation_retries_only_that_reconciliation(setup):
    outcome, _, _ = run_with(
        setup,
        [verdict("APPROVE", "c first", session="c1"), verdict("APPROVE", "c second", session="c1")],
        [
            verdict("REQUEST_CHANGES", "x first", [finding()], session="x1"),
            failed("timeout", "codex timed out while reconciling"),
        ],
    )
    assert outcome.infra_error and "timed out" in outcome.infra_error
    assert set(outcome.progress["firsts"]) == {"claude", "codex"}
    assert set(outcome.progress["seconds"]) == {"claude"}

    retry, claude2, codex2 = run_with(
        setup, [], [verdict("APPROVE", "x second", session="x1")], progress=outcome.progress
    )
    assert retry.approved
    assert claude2.requests == []
    assert len(codex2.requests) == 1
    assert codex2.requests[0].resume_session == "x1"
    assert "c first" in codex2.requests[0].prompt


def test_model_unavailable_and_a_usage_pause_keep_progress_too(setup):
    outcome, _, _ = run_with(setup, [verdict("APPROVE")], [failed("model_unavailable")])
    assert outcome.model_unavailable and set(outcome.progress["firsts"]) == {"claude"}
    calls = []

    def gate():
        calls.append(1)
        return None if len(calls) == 1 else "paused now"

    outcome, _, _ = run_with(
        setup, [verdict("APPROVE")], [verdict("REQUEST_CHANGES", "x", [finding()])], gate=gate
    )
    assert outcome.pause_reason and set(outcome.progress["firsts"]) == {"claude", "codex"}


def test_saved_verdicts_are_ignored_when_the_tree_or_base_changed(setup):
    outcome, _, _ = run_with(setup, [verdict("APPROVE")], [failed("crash")])
    for key, value in (("tree", "0" * 40), ("base_sha", "1" * 40)):
        stale = dict(outcome.progress, **{key: value})
        retry, claude2, codex2 = run_with(setup, [verdict("APPROVE")], [verdict("APPROVE")], progress=stale)
        assert retry.approved
        assert len(claude2.requests) == 1 and len(codex2.requests) == 1


def test_progress_survives_a_json_round_trip(setup):
    import json

    outcome, _, _ = run_with(
        setup, [verdict("APPROVE", "ok", [finding("minor", "wart")], session="c1")], [failed("crash")]
    )
    stored = json.loads(json.dumps(outcome.progress))
    retry, claude2, _ = run_with(setup, [], [verdict("APPROVE", "ok", session="x1")], progress=stored)
    assert retry.approved and claude2.requests == []
    claude_review = next(r for r in retry.final_reviews if r.reviewer == "claude")
    assert claude_review.findings[0].message == "wart"


def test_after_run_hook_sees_every_agent_run(setup):
    seen = []
    claude, codex = ScriptedRunner([verdict("APPROVE"), verdict("APPROVE")]), ScriptedRunner(
        [verdict("REQUEST_CHANGES", "x", [finding()]), verdict("APPROVE")]
    )
    ctx = setup.context(claude, codex)
    ctx.after_run = lambda agent, result: seen.append((agent, result.ok))
    review.double_review(ctx)
    assert sorted(seen) == [("claude", True), ("claude", True), ("codex", True), ("codex", True)]


def commit_in_worktree(repo):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "rogue reviewer commit"], check=True, capture_output=True
    )


def test_a_reviewer_that_commits_voids_the_review(setup):
    def rogue(request):
        commit_in_worktree(setup.repo)
        return verdict("APPROVE", session="c1")

    before = git_ops.head_sha(setup.repo)
    outcome, claude, codex = both(setup, [rogue], [verdict("APPROVE", session="x1")])
    assert not outcome.approved and not outcome.blocked
    assert outcome.head_moved and "reviewer_claude moved HEAD" in outcome.head_moved
    assert git_ops.head_sha(setup.repo) != before
    assert outcome.final_reviews == []


def test_a_codex_reviewer_that_commits_is_caught_too(setup):
    def rogue(request):
        commit_in_worktree(setup.repo)
        return verdict("APPROVE", session="x1")

    outcome, _, _ = both(setup, [verdict("APPROVE", session="c1")], [rogue])
    assert not outcome.approved
    assert outcome.head_moved and "reviewer_codex moved HEAD" in outcome.head_moved


def test_a_reconciliation_run_that_commits_is_caught(setup):
    def rogue(request):
        commit_in_worktree(setup.repo)
        return verdict("APPROVE", session="c1")

    outcome, claude, codex = both(
        setup,
        [verdict("APPROVE", session="c1"), rogue],
        [verdict("REQUEST_CHANGES", findings=[finding("minor")], session="x1"), verdict("APPROVE", session="x1")],
    )
    assert not outcome.approved
    assert outcome.head_moved and "reconcile" in outcome.head_moved


def test_a_failed_reviewer_run_that_moved_head_is_still_caught(setup):
    def rogue(request):
        commit_in_worktree(setup.repo)
        return failed("crash")

    outcome, _, _ = both(setup, [rogue], [verdict("APPROVE", session="x1")])
    assert outcome.head_moved and not outcome.approved


def test_reviewers_that_leave_head_alone_leave_head_moved_unset(setup):
    outcome, _, _ = both(setup, [verdict("APPROVE")], [verdict("APPROVE")])
    assert outcome.approved and outcome.head_moved is None


def test_subscription_violation_is_reported_and_is_not_an_infra_error(setup):
    from workforce.agents import guard

    violation = failed("auth", f"{guard.SUBSCRIPTION_VIOLATION}: apiKeySource='ANTHROPIC_API_KEY'")
    outcome, _, _ = both(setup, [violation], [verdict("APPROVE")])
    assert not outcome.approved and outcome.infra_error is None
    assert outcome.subscription_violation.startswith("reviewer_claude: subscription violation")


def test_a_verdict_with_an_unexpected_key_is_a_schema_failure_not_a_crash(setup):
    bad = verdict("APPROVE", findings=[{**finding(), "extra": "x"}])
    outcome, _, _ = both(setup, [bad], [verdict("APPROVE")])
    assert not outcome.approved
    assert outcome.infra_error and "schema" in outcome.infra_error and "extra" in outcome.infra_error


def test_coder_prompts_tell_the_repo_worktree_dependencies_and_the_write_rule():
    for name in ("coder", "coder_fix"):
        text = render_all(name, repo="app", worktree="/wt/app/T1", dependency_worktrees="- api: /wt/api/_integration")
        assert "Repo: `app`" in text and "/wt/app/T1" in text and "/wt/api/_integration" in text
        assert "Write only inside your worktree" in text
        assert "never touch the user" in text.lower() or "read for context, never changed" in text.lower()


def test_committer_and_rebase_prompts_name_the_repo():
    assert "the repo `app`" in render_all("committer", repo_name="app")
    assert "(repo `app`)" in render_all("rebase", repo_name="app")


def test_integration_reviewer_prompt_is_read_only_independent_and_cross_repo():
    text = render_all("integration_reviewer")
    assert "read-only" in text and "cannot see their work" in text
    assert "API contracts" in text and "migrations" in text and "other repos" in text
    assert "verdict schema" in text and "BLOCKED" in text
    assert "## Task T" not in text


def test_integration_fix_prompt_asks_only_for_new_tasks_that_continue_the_numbering():
    text = render_all("integration_fix", next_id="T7")
    assert "ONLY the new fix tasks" in text and "`T7`" in text and "plan_revision schema" in text


@pytest.fixture
def integration(git_workspace, tmp_path):
    """alpha and beta each with an integration branch holding one commit, checked out in its own worktree."""
    config = config_mod.parse(config_mod.default_toml())
    repos = []
    for name in ("alpha", "beta"):
        repo = git_workspace / name
        base = git_ops.head_sha(repo)
        worktree = tmp_path / "wt" / name
        git_ops.create_worktree(repo, worktree, "wf/goal", base=base)
        (worktree / f"{name}_file.txt").write_text(f"{name} content\n")
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", f"{name} change")
        repos.append(review.IntegrationRepo(name, worktree, "wf/goal", base))

    class Setup:
        pass

    setup = Setup()
    setup.root, setup.repos, setup.config = git_workspace, repos, config
    setup.events, setup.logs = [], []

    def context(claude, codex, decider=None, gate=None, **kw):
        setup.events, setup.logs = [], []

        def log_path(step):
            setup.logs.append(step)
            return Path(f"/logs/{step}.jsonl")

        return review.IntegrationContext(
            root=git_workspace,
            goal="ship the feature across both repos",
            plan="PLAN-TEXT",
            repos=repos,
            decisions="RULE-ALPHA",
            config=config,
            runners={"claude": claude, "codex": codex},
            decider=decider or NullDecider(),
            log_path=log_path,
            emit=lambda kind, **data: setup.events.append((kind, data)),
            gate=gate or (lambda: None),
            **kw,
        )

    setup.context = context
    return setup


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def integrate(setup, claude_steps, codex_steps, **kw):
    claude, codex = ScriptedRunner(claude_steps), ScriptedRunner(codex_steps)
    return review.integration_review(setup.context(claude, codex, **kw)), claude, codex


def test_integration_review_both_approve_with_fresh_read_only_root_requests(integration):
    outcome, claude, codex = integrate(
        integration, [verdict("APPROVE", "claude fits")], [verdict("APPROVE", "codex fits")]
    )
    assert outcome.approved and not outcome.blocked and outcome.infra_error is None
    assert len(claude.requests) == 1 and len(codex.requests) == 1
    for request, agent in ((claude.requests[0], "claude"), (codex.requests[0], "codex")):
        assert request.role == f"reviewer_{agent}" and request.agent == agent
        assert request.cwd == integration.root and request.sandbox == "read-only"
        assert request.resume_session is None and request.schema is schemas.VERDICT
        assert request.skip_git_check is True
        assert request.add_dirs == [item.worktree for item in integration.repos]
        for needle in (
            "RULE-ALPHA", "ship the feature across both repos", "PLAN-TEXT", "### Repo alpha: branch wf/goal",
            "### Repo beta: branch wf/goal", "+alpha content", "+beta content", "alpha_file.txt", "Worktree (read it",
        ):
            assert needle in request.prompt
    assert sorted(integration.logs) == ["integration_claude", "integration_codex"]
    assert {r.reviewer for r in outcome.reviews} == {"claude", "codex"}
    assert {r.sha for r in outcome.final_reviews} == {review.integration_fingerprint(integration.repos)}
    assert outcome.summaries == {"claude": "claude fits", "codex": "codex fits"}
    started = [d for kind, d in integration.events if kind == "agent_event" and d["phase"] == "start"]
    assert {d["task"] for d in started} == {"integration"}


def test_integration_review_does_not_skip_the_git_check_when_the_root_is_a_repo(integration, git_repo):
    integration.root = git_repo
    ctx = integration.context(ScriptedRunner([verdict("APPROVE")]), ScriptedRunner([verdict("APPROVE")]))
    ctx.root = git_repo
    outcome = review.integration_review(ctx)
    assert outcome.approved
    assert ctx.runners["claude"].requests[0].skip_git_check is False


def test_integration_review_needs_both_to_approve_and_merges_findings(integration):
    outcome, _, _ = integrate(
        integration,
        [verdict("APPROVE", "fine")],
        [verdict("REQUEST_CHANGES", "no", [finding("major", "FIELD-MISMATCH", file="beta/x.py")])],
    )
    assert not outcome.approved and not outcome.blocked
    assert [item["message"] for item in outcome.merged_findings] == ["FIELD-MISMATCH"]
    assert "FIELD-MISMATCH" in review.format_feedback(outcome)


def test_integration_review_blocked_is_reported(integration):
    outcome, _, _ = integrate(integration, [verdict("BLOCKED", "need a call")], [verdict("APPROVE")])
    assert outcome.blocked and not outcome.approved


@pytest.mark.parametrize("kind", ["crash", "timeout", "auth", "schema"])
def test_integration_review_infra_failures_are_never_a_verdict(integration, kind):
    outcome, _, _ = integrate(integration, [failed(kind, "it broke")], [verdict("APPROVE")])
    assert not outcome.approved and kind in outcome.infra_error and "reviewer_claude" in outcome.infra_error


def test_integration_review_model_unavailable_and_subscription_violation(integration):
    outcome, _, _ = integrate(integration, [verdict("APPROVE")], [failed("model_unavailable")])
    assert outcome.model_unavailable == integration.config.role("reviewer_codex").model
    from workforce.agents import guard

    outcome, _, _ = integrate(
        integration, [failed("auth", f"{guard.SUBSCRIPTION_VIOLATION}: apiKeySource='X'")], [verdict("APPROVE")]
    )
    assert "reviewer_claude" in outcome.subscription_violation and not outcome.approved


def test_integration_review_stops_at_the_gate_before_launching_anything(integration):
    outcome, claude, codex = integrate(integration, [], [], gate=lambda: "usage: claude five_hour 61% ≥ 60%")
    assert outcome.pause_reason == "usage: claude five_hour 61% ≥ 60%"
    assert claude.requests == [] and codex.requests == []


def test_integration_review_reports_a_reviewer_that_moved_an_integration_branch(integration):
    worktree = integration.repos[0].worktree

    def commits(request):
        git(worktree, "commit", "-q", "--allow-empty", "-m", "sneaky")
        return verdict("APPROVE")

    outcome, _, _ = integrate(integration, [commits], [verdict("APPROVE")])
    assert not outcome.approved and "moved an integration branch" in outcome.head_moved
    assert "alpha" in outcome.head_moved


def test_integration_review_reruns_an_inconsistent_reviewer_once_then_forces_changes(integration):
    bad = verdict("APPROVE", "fine", [finding("blocker", "BROKEN")])
    outcome, claude, _ = integrate(integration, [bad, bad], [verdict("APPROVE")])
    assert len(claude.requests) == 2 and claude.requests[1].resume_session is None
    assert not outcome.approved
    forced = {r.reviewer: r for r in outcome.final_reviews}["claude"]
    assert forced.verdict == "REQUEST_CHANGES" and any("did not match" in f.message for f in forced.findings)

    good = verdict("APPROVE", "fine")
    outcome, claude, _ = integrate(integration, [bad, good], [verdict("APPROVE")])
    assert outcome.approved and len(claude.requests) == 2


def test_integration_review_trims_each_repos_diff_to_its_share_of_the_budget(integration, monkeypatch):
    monkeypatch.setattr(review, "MAX_INTEGRATION_DIFF_CHARS", 20_000)
    monkeypatch.setattr(review, "MIN_REPO_DIFF_CHARS", 500)
    for item in integration.repos:
        (item.worktree / "big.txt").write_text("".join(f"{item.name} line {i}\n" for i in range(3000)))
        git(item.worktree, "add", "-A")
        git(item.worktree, "commit", "-q", "-m", "big")
    ctx = integration.context(ScriptedRunner([verdict("APPROVE")]), ScriptedRunner([verdict("APPROVE")]))
    text = review.integration_repos_text(ctx)
    assert text.count("diff truncated after 10000 characters") == 2
    assert "alpha line 0" in text and "beta line 0" in text
    assert len(text) < 30_000
