"""End-to-end orchestrator scenarios driven by the fake claude/codex binaries (SPEC section 12, v1)."""

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from workforce import config as config_mod
from workforce import git_ops
from workforce.agents.base import AgentResult
from workforce.agents.claude import ClaudeRunner
from workforce.agents.codex import CodexRunner
from workforce.decider.base import Decision, NullDecider
from workforce.errors import PreflightError, WorkforceError
from workforce.events import EventLog
from workforce.orchestrator import Orchestrator, validate_plan
from workforce.paths import Paths
from workforce.state import StateStore
from workforce.usage.monitor import UsageMonitor

FAKES = Path(__file__).parent / "fakes"
FAKE_CLAUDE = FAKES / "fake_claude.py"
FAKE_CODEX = FAKES / "fake_codex.py"
SCENARIOS = Path(__file__).parent / "scenarios"
API_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
REVIEWER_MATCH = "You review one task's changes on your own"
RECONCILE_MATCH = "You reviewed this task earlier"
INTEGRATION_MATCH = "# Role: independent integration reviewer"


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class AgreeingDecider:
    """A confident decider: verdicts are consistent, plans never converge, agents can settle."""

    def __init__(self):
        self.choices: list[str] = []

    def available(self) -> bool:
        return True

    def ask_bool(self, key, state, question):
        answer = key == "verdict_consistent"
        return Decision(key=key, question=question, answer=answer, confidence=0.99, source="laya")

    def ask_choice(self, key, state, question, options):
        self.choices.append(key)
        return Decision(key=key, question=question, answer="agents", confidence=0.99, source="laya")


def config_text(test_cmd: str = "") -> str:
    text = config_mod.default_toml()
    text = text.replace('claude = "~/.local/bin/claude"', f'claude = "{FAKE_CLAUDE}"')
    text = text.replace('codex = "/opt/homebrew/bin/codex"', f'codex = "{FAKE_CODEX}"')
    text = text.replace('backend = "laya"', 'backend = "off"')
    if test_cmd:
        text = text.replace('test = ""', f'test = "{test_cmd}"')
    return text


def make_config(test_cmd: str = "") -> config_mod.Config:
    return config_mod.parse(config_text(test_cmd))


class Pinger:
    """In-process stand-in for the Claude runner the usage monitor uses for pings and summaries."""

    def __init__(self):
        self.rate_limits = None
        self.requests = []

    def run(self, request, on_event=None):
        self.requests.append(request)
        return AgentResult(
            ok=True,
            text="Usage looks fine.",
            structured=None,
            session_id="ping",
            model=request.model,
            rate_limits=self.rate_limits,
        )


def windows(five_hour: float, seven_day: float, five_reset=1_790_694_000, week_reset=1_791_440_000) -> dict:
    return {
        "five_hour": {"utilization": five_hour, "resetsAt": five_reset},
        "seven_day": {"utilization": seven_day, "resetsAt": week_reset},
    }


class Harness:
    def __init__(
        self,
        tmp_path,
        repo,
        monkeypatch,
        scenario,
        test_cmd="",
        decider=None,
        usage_gate=None,
        clock_start=None,
        monitor=False,
    ):
        for var in API_VARS:
            monkeypatch.delenv(var, raising=False)
        self.scenario_file = tmp_path / "scenario.json"
        shutil.copy(SCENARIOS / f"{scenario}.json", self.scenario_file)
        monkeypatch.setenv("WF_FAKE_SCENARIO", str(self.scenario_file))
        self.repo = repo
        self.home = tmp_path / "home"
        self.config = make_config(test_cmd)
        self.decider = decider or NullDecider()
        self.clock = FakeClock() if clock_start is None else FakeClock(clock_start)
        self.usage_gate = usage_gate
        self.paths = Paths(repo, home=self.home)
        self.store = StateStore(self.paths)
        self.pinger = Pinger()
        self.monitor = None
        if monitor:
            self.monitor = UsageMonitor(
                self.paths, self.config, self.config.bin.codex, self.pinger, EventLog(self.paths), self.clock
            )
        self.orch = self.new_orchestrator()

    def new_orchestrator(self) -> Orchestrator:
        runners = {"claude": ClaudeRunner(FAKE_CLAUDE), "codex": CodexRunner(FAKE_CODEX)}
        return Orchestrator(
            self.repo,
            self.config,
            runners,
            self.decider,
            clock=self.clock,
            home=self.home,
            usage_gate=self.usage_gate,
            usage_monitor=self.monitor,
        )

    def write_config_file(self, text: str | None = None) -> Path:
        self.paths.ensure()
        target = self.repo / "workforce.toml"
        target.write_text(text if text is not None else config_text())
        return target

    def calls(self) -> list[dict]:
        path = Path(str(self.scenario_file) + ".calls.jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def events(self, kind: str | None = None) -> list[dict]:
        events, _ = EventLog(self.paths).read()
        return [e for e in events if kind is None or e["kind"] == kind]

    def run(self):
        return self.store.load()

    def task(self, task_id="T1"):
        return self.run().task(task_id)

    def pick_models(self) -> None:
        coder = self.config.role("coder")
        for task in self.run().tasks:
            self.orch.set_models(task.id, coder.model, coder.effort)
        self.orch.resume()

    def to_models(self, goal="add the files", mode="auto"):
        self.orch.start(goal, mode)
        run = self.orch.run_until_blocked()
        assert run.status == "awaiting_models", run.status
        return run

    def go(self, goal="add the files", mode="auto"):
        self.to_models(goal, mode)
        self.pick_models()
        return self.orch.run_until_blocked()

    def step_until(self, predicate, limit=60):
        for _ in range(limit):
            if predicate(self.run()):
                return self.run()
            self.orch.step()
        raise AssertionError("condition never reached")


@pytest.fixture
def harness(tmp_path, git_repo, monkeypatch):
    def build(scenario, **kwargs):
        return Harness(tmp_path, git_repo, monkeypatch, scenario, **kwargs)

    return build


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def prompts_with(calls: list[dict], cli: str, needle: str) -> list[dict]:
    return [c for c in calls if c["cli"] == cli and needle in c["prompt"]]


def integration_branch(run) -> str:
    return f"wf/{run.slug}"


def branch_file(repo: Path, branch: str, name: str) -> str:
    return git(repo, "show", f"{branch}:{name}") + "\n"


def assert_merged(h: Harness, run, count: int) -> None:
    """Every task merged into the integration branch; the user's checkout and base branch never moved."""
    assert run.status == "done"
    assert [t.status for t in run.tasks] == ["merged"] * count
    merged = {e["task"]: e for e in h.events("merged")}
    branch = integration_branch(run)
    for task in run.tasks:
        assert git_ops.commit_tree(h.repo, merged[task.id]["sha"]) == task.head_sha
        assert not Path(task.worktree or "/nonexistent").exists()
        assert not h.paths.worktree(run.id, task.id, "repo").exists()
    assert git(h.repo, "branch", "--list", "wf/*").split() == [branch]
    assert not (h.paths.worktrees_root / run.id).exists()
    assert git_ops.commit_tree(h.repo, branch) == run.tasks[-1].head_sha
    assert git(h.repo, "rev-list", "--count", branch) == str(1 + count)
    assert git(h.repo, "rev-list", "--count", "main") == "1"
    assert git(h.repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_happy_path_two_tasks_in_dependency_order(harness):
    h = harness("happy_path")
    run = h.go()
    assert_merged(h, run, 2)
    merged = [e["task"] for e in h.events("merged")]
    assert merged == ["T1", "T2"]
    calls = h.calls()
    assert len([c for c in calls if c["cli"] == "claude"]) == 8
    assert len([c for c in calls if c["cli"] == "codex"]) == 4
    assert len(prompts_with(calls, "claude", INTEGRATION_MATCH)) == 1
    assert len(prompts_with(calls, "codex", INTEGRATION_MATCH)) == 1
    branch = integration_branch(run)
    assert branch_file(h.repo, branch, "a.txt") == "alpha\n"
    assert branch_file(h.repo, branch, "b.txt") == "beta\n"
    assert not (h.repo / "a.txt").exists() and not (h.repo / "b.txt").exists()
    assert (h.repo / ".workforce" / "plan.md").exists()
    assert h.events("run_done")


def test_a_dirty_user_checkout_runs_and_is_left_exactly_as_it_was(harness):
    h = harness("happy_path")
    (h.repo / "README.md").write_text("the user is editing this\n")
    (h.repo / "mine.txt").write_text("untracked and precious\n")
    git(h.repo, "checkout", "-q", "-b", "feature/user-work")
    before = git(h.repo, "status", "--porcelain")
    head = git(h.repo, "rev-parse", "HEAD")
    run = h.go()
    assert run.status == "done"
    assert git(h.repo, "status", "--porcelain") == before
    assert git(h.repo, "rev-parse", "HEAD") == head
    assert git(h.repo, "symbolic-ref", "--short", "HEAD") == "feature/user-work"
    assert (h.repo / "README.md").read_text() == "the user is editing this\n"
    assert (h.repo / "mine.txt").read_text() == "untracked and precious\n"
    integration = run.integration["repo"]
    assert integration.base_ref == "feature/user-work" and integration.base_sha == head
    assert git(h.repo, "rev-list", "--count", f"{head}..{integration.branch}") == "2"


def test_an_existing_integration_branch_name_is_a_question_not_a_crash(harness):
    h = harness("happy_path")
    h.to_models()
    git(h.repo, "branch", h.orch.proposed_branches()["repo"])
    h.pick_models()
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_user"
    assert "already exists" in run.questions[0].text
    assert not prompts_with(h.calls(), "claude", "# Role: coder")
    assert run.integration == {}


def test_worktrees_live_outside_the_repo_and_branches_are_named_by_run(harness):
    h = harness("happy_path")
    h.to_models()
    h.pick_models()
    h.step_until(lambda run: run.tasks[0].status == "coding")
    task = h.task("T1")
    worktree = Path(task.worktree)
    assert h.repo.resolve() not in worktree.resolve().parents
    assert h.home.resolve() in worktree.resolve().parents
    run = h.run()
    assert task.branch == f"wf/{run.slug}-T1"
    assert worktree == h.paths.worktree(run.id, "T1", "repo")
    integration = run.integration["repo"]
    assert integration.branch == f"wf/{run.slug}" and integration.base_ref == "main"
    assert integration.base_sha == git_ops.head_sha(h.repo)
    assert task.base_sha == git_ops.resolve_ref(h.repo, integration.branch) == integration.base_sha


def test_only_the_committer_uses_the_user_setup_and_no_one_disables_signing(harness):
    h = harness("happy_path")
    h.go()
    calls = h.calls()
    committers = prompts_with(calls, "claude", "# Role: committer")
    assert len(committers) == 2
    for call in committers:
        assert "--setting-sources" not in call["argv"]
        assert "--no-gpg-sign" not in call["prompt"]
    others = [c for c in calls if c["cli"] == "claude" and c not in committers]
    assert others and all("--setting-sources" in c["argv"] for c in others)
    for call in calls:
        assert "--no-gpg-sign" not in call["argv"]
    coders = prompts_with(calls, "claude", "# Role: coder")
    assert all("do not commit" in c["prompt"].lower() or "Do not commit" in c["prompt"] for c in coders)


def test_disagreement_then_fix_then_approve(harness):
    h = harness("disagreement_fix")
    run = h.go()
    assert_merged(h, run, 1)
    assert h.task("T1").review_round == 1
    calls = h.calls()

    claude_first = prompts_with(calls, "claude", REVIEWER_MATCH)
    codex_first = prompts_with(calls, "codex", REVIEWER_MATCH)
    assert len(claude_first) == 2 and len(codex_first) == 2
    for call in (claude_first[0], claude_first[1]):
        assert "CODEX-FINDING" not in call["prompt"] and "CODEX-SUMMARY" not in call["prompt"]
        assert "--resume" not in call["argv"]
    for call in (codex_first[0], codex_first[1]):
        assert "CLAUDE-FINDING" not in call["prompt"] and "CLAUDE-SUMMARY" not in call["prompt"]
        assert call["argv"][0] == "exec" and "resume" not in call["argv"][:2] and "--json" in call["argv"]
        assert call["argv"][call["argv"].index("-s") + 1] == "read-only"
        assert Path(call["argv"][call["argv"].index("-C") + 1]).resolve() == Path(call["cwd"]).resolve()

    claude_reconcile = prompts_with(calls, "claude", RECONCILE_MATCH)
    codex_reconcile = prompts_with(calls, "codex", RECONCILE_MATCH)
    assert len(claude_reconcile) == 1 and len(codex_reconcile) == 1
    assert "CODEX-FINDING-ONE" in claude_reconcile[0]["prompt"]
    assert "CODEX-SUMMARY-ONE" in claude_reconcile[0]["prompt"]
    assert "CLAUDE-SUMMARY-ONE" in codex_reconcile[0]["prompt"]
    first_claude_session = "fake-claude-session-3"
    argv = claude_reconcile[0]["argv"]
    assert argv[argv.index("--resume") + 1] == first_claude_session
    assert codex_reconcile[0]["argv"][:3] == ["exec", "resume", "fake-codex-thread-2"]
    assert "--dangerously-bypass-hook-trust" in codex_reconcile[0]["argv"]
    assert 'sandbox_mode="read-only"' in codex_reconcile[0]["argv"]

    fix = prompts_with(calls, "claude", "# Role: coder (fixing)")
    assert len(fix) == 1
    assert "CODEX-FINDING-TWO" in fix[0]["prompt"] and "CLAUDE-FINDING-TWO" in fix[0]["prompt"]
    assert "CODEX-FINDING-ONE" not in fix[0]["prompt"]
    assert fix[0]["argv"][fix[0]["argv"].index("--resume") + 1] == "fake-claude-session-2"

    reviews = h.events("review")
    assert {e["sha"] for e in reviews[:2]} != {e["sha"] for e in reviews[-2:]}
    final = h.task("T1").reviews[-2:]
    assert {r.verdict for r in final} == {"APPROVE"}
    assert all(r.sha == h.task("T1").head_sha for r in final)


def test_three_failed_rounds_ask_the_user_then_answer_continues(harness):
    h = harness("three_rounds_question")
    run = h.go()
    assert run.status == "awaiting_user"
    task = run.task("T1")
    assert task.status == "blocked" and task.review_round == 3
    question = run.questions[0]
    assert question.id == "Q1" and question.status == "open" and question.task_id == "T1"
    assert set(question.positions) == {"claude", "codex"}
    assert "CLAUDE-MINOR-3" in question.positions["claude"]
    assert "CODEX-MINOR-3" in question.positions["codex"]
    assert h.orch.step().progressed is False

    run = h.orch.answer("Q1", "USER-ADVICE: ignore the nits")
    assert run.status == "running" and run.task("T1").status == "fixing"
    assert run.task("T1").review_round == 0
    assert "USER-ADVICE" in h.paths.decisions_md.read_text()
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 1)
    last_fix = prompts_with(h.calls(), "claude", "# Role: coder (fixing)")[-1]
    assert "USER-ADVICE: ignore the nits" in last_fix["prompt"]
    later = prompts_with(h.calls(), "claude", REVIEWER_MATCH)[-1]
    assert "USER-ADVICE" in later["prompt"]


def test_needs_human_decider_can_grant_one_more_round_instead_of_asking(harness):
    decider = AgreeingDecider()
    h = harness("three_rounds_question", decider=decider)
    run = h.go()
    assert_merged(h, run, 1)
    assert h.events("question") == []
    assert decider.choices == ["needs_human"]
    assert any(e["key"] == "needs_human" for e in h.events("decision"))


def test_reviewer_crash_is_infra_never_a_round_never_approval(harness):
    h = harness("reviewer_crash")
    h.to_models()
    h.pick_models()
    h.step_until(lambda run: run.tasks[0].status == "reviewing")
    outcome = h.orch.step()
    assert outcome.action == "retry"
    task = h.task("T1")
    assert task.status == "reviewing"
    assert task.infra_failures == 1
    assert task.review_round == 0
    assert task.reviews == []
    assert h.events("review") == []
    assert h.clock.sleeps == [5]
    assert any("reviewer_claude" in e["message"] for e in h.events("error"))

    saved = h.orch._meta(h.run().id)["tasks"]["T1"]["review_progress"]
    assert set(saved["firsts"]) == {"codex"}

    run = h.orch.run_until_blocked()
    assert_merged(h, run, 1)
    task = run.task("T1")
    assert task.infra_failures == 0 and task.review_round == 0
    assert len(h.events("review")) == 2
    assert len(task.reviews) == 2 and {r.reviewer for r in task.reviews} == {"claude", "codex"}
    calls = h.calls()
    assert len(prompts_with(calls, "claude", REVIEWER_MATCH)) == 1 + 1
    assert len(prompts_with(calls, "codex", REVIEWER_MATCH)) == 1
    assert "review_progress" not in h.orch._meta(run.id)["tasks"]["T1"]


def test_repeated_reviewer_crashes_ask_the_user(harness):
    h = harness("reviewer_crash_exhausted")
    run = h.go()
    assert run.status == "awaiting_user"
    task = run.task("T1")
    assert task.status == "blocked" and task.infra_failures == 3 and task.review_round == 0
    assert task.reviews == []
    assert h.clock.sleeps == [5, 20]
    assert run.questions[0].positions == {}
    assert "crash" in run.questions[0].text
    assert len(prompts_with(h.calls(), "codex", REVIEWER_MATCH)) == 1
    run = h.orch.answer("Q1", "try again")
    assert run.task("T1").status == "reviewing" and run.task("T1").infra_failures == 0
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 1)
    assert len(prompts_with(h.calls(), "codex", REVIEWER_MATCH)) == 1


def test_blocked_verdict_asks_immediately_with_both_positions(harness):
    h = harness("blocked")
    run = h.go()
    assert run.status == "awaiting_user"
    task = run.task("T1")
    assert task.status == "blocked" and task.review_round == 0
    question = run.questions[0]
    assert set(question.positions) == {"claude", "codex"}
    assert "BLOCKED" in question.positions["claude"] and "APPROVE" in question.positions["codex"]
    calls = h.calls()
    assert len([c for c in calls if c["cli"] == "claude"]) == 4
    assert prompts_with(calls, "claude", "# Role: coder (fixing)") == []

    run = h.orch.answer("Q1", "USER-CHOICE-ALPHA keep a.txt")
    assert run.task("T1").status == "fixing"
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 1)


def test_model_unavailable_pauses_with_the_model_name_and_never_substitutes(harness):
    h = harness("model_unavailable")
    run = h.go()
    assert run.status == "paused"
    assert run.pause_reason == "model_unavailable: claude-sonnet-5-5"
    assert run.task("T1").status == "coding"
    assert run.task("T1").infra_failures == 0
    assert h.orch.step().progressed is False
    assert len(h.calls()) == 3
    models = {c["argv"][c["argv"].index("--model") + 1] for c in h.calls() if c["cli"] == "claude"}
    assert models == {"claude-opus-5-5", "claude-sonnet-5-5"}
    assert h.events("paused")[-1]["reason"] == "model_unavailable: claude-sonnet-5-5"
    run = h.orch.resume()
    assert run.status == "running" and run.pause_reason is None


@pytest.mark.parametrize("var", API_VARS)
def test_api_key_in_env_refuses_before_anything_runs(harness, monkeypatch, var):
    h = harness("happy_path")
    monkeypatch.setenv(var, "sk-not-real")
    with pytest.raises(PreflightError) as caught:
        h.orch.start("add the files")
    assert var in str(caught.value)
    assert h.calls() == []
    assert not h.paths.state.exists()


def test_resume_also_refuses_with_an_api_key(harness, monkeypatch):
    h = harness("happy_path")
    h.to_models()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-real")
    with pytest.raises(PreflightError):
        h.new_orchestrator().resume()


def test_commit_tree_mismatch_voids_reviews_and_re_reviews(harness):
    h = harness("tree_mismatch")
    run = h.go()
    assert_merged(h, run, 1)
    calls = h.calls()
    assert len(prompts_with(calls, "claude", "# Role: committer")) == 1
    assert len(prompts_with(calls, "claude", REVIEWER_MATCH)) == 2
    assert len(prompts_with(calls, "codex", REVIEWER_MATCH)) == 2
    assert any("approvals voided" in e["message"] for e in h.events("error"))
    shas = [e["sha"] for e in h.events("review")]
    assert len(set(shas)) == 2
    task = run.task("T1")
    assert task.head_sha == shas[-1] != shas[0]
    assert all(r.sha == task.head_sha for r in task.reviews)
    assert branch_file(h.repo, integration_branch(run), "stray.txt") == "stray\n"
    merged_sha = h.events("merged")[0]["sha"]
    assert git(h.repo, "rev-list", "--count", f"{task.base_sha}..{merged_sha}") == "1"


def test_failing_checks_send_output_back_to_the_coder(harness):
    h = harness("checks_failing", test_cmd="test -f ok.txt")
    run = h.go()
    assert_merged(h, run, 1)
    results = h.events("check_result")
    assert [e["ok"] for e in results] == [False, True]
    fix = prompts_with(h.calls(), "claude", "# Role: coder (fixing)")
    assert len(fix) == 1 and "Checks: FAILED" in fix[0]["prompt"] and "ok.txt" in fix[0]["prompt"]
    assert fix[0]["argv"][fix[0]["argv"].index("--resume") + 1] == "fake-claude-session-2"
    assert h.task("T1").review_round == 0
    assert "ok.txt" in git(h.repo, "ls-tree", "--name-only", integration_branch(run)).split()


def test_plan_debate_hitting_three_rounds_asks_the_user(harness):
    h = harness("plan_debate_cap")
    h.orch.start("add the files")
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_user"
    question = run.questions[0]
    assert question.task_id is None
    assert set(question.positions) == {"claude", "codex"}
    assert "REVIEWER-OBJECTION-4" in question.positions["claude"]
    assert "PLANNER-POSITION-3" in question.positions["codex"]
    assert "final review" in question.text
    calls = h.calls()
    assert len([c for c in calls if c["cli"] == "claude"]) == 4
    assert len([c for c in calls if c["cli"] == "codex"]) == 4
    codex = [c for c in calls if c["cli"] == "codex"]
    assert codex[0]["argv"][0] == "exec" and "resume" not in codex[0]["argv"][:2]
    assert all(c["argv"][:2] == ["exec", "resume"] for c in codex[1:])
    assert all(c["argv"][2] == "fake-codex-thread-1" for c in codex[1:])
    assert codex[0]["argv"][codex[0]["argv"].index("-s") + 1] == "read-only"
    assert 'sandbox_mode="read-only"' in codex[1]["argv"]
    assert len(h.events("debate_round")) == 7
    final_review = prompts_with(calls, "claude", "# Role: plan reviewer")[-1]
    assert "## Debate round\n4\n" in final_review["prompt"]
    plan_md = h.paths.plan_md.read_text()
    assert "PLANNER-POSITION-3" in plan_md and "### Round 3" in plan_md

    run = h.orch.answer("Q1", "ship the plan as it stands")
    assert run.status == "awaiting_models"
    assert "ship the plan as it stands" in h.paths.decisions_md.read_text()


def test_plan_debate_can_converge_and_replaces_the_tasks(harness):
    h = harness("plan_debate_converges")
    run = h.to_models()
    assert [t.id for t in run.tasks] == ["T1", "T2"]
    assert run.task("T2").depends_on == ["T1"]
    assert "### T2: Add b.txt" in h.paths.plan_md.read_text()
    assert "PLANNER-POSITION-1" in h.paths.plan_md.read_text()
    reviewer_prompts = prompts_with(h.calls(), "claude", "# Role: plan reviewer")
    assert "Add b.txt" in reviewer_prompts[1]["prompt"] and "Add b.txt" not in reviewer_prompts[0]["prompt"]
    revise = prompts_with(h.calls(), "codex", "REVIEWER-OBJECTION-1")
    assert len(revise) == 1 and "REVIEWER-CHANGE-1" in revise[0]["prompt"]


def test_awaiting_models_stops_until_models_are_set(harness):
    h = harness("happy_path")
    h.to_models()
    assert h.orch.step().progressed is False
    with pytest.raises(WorkforceError, match="no model chosen"):
        h.orch.resume()
    with pytest.raises(WorkforceError, match="effort"):
        h.orch.set_models("T1", "claude-sonnet-5-5", "ultra")
    with pytest.raises(WorkforceError, match="empty"):
        h.orch.set_models("T1", " ", "high")
    task = h.orch.set_models("T1", "claude-haiku-4-5-20251001", "low")
    assert (task.model, task.effort, task.agent) == ("claude-haiku-4-5-20251001", "low", "claude")
    h.orch.set_models("T2", "claude-sonnet-5-5", "high")
    h.orch.resume()
    h.orch.run_until_blocked()
    coder_calls = prompts_with(h.calls(), "claude", "# Role: coder")
    models = [c["argv"][c["argv"].index("--model") + 1] for c in coder_calls]
    efforts = [c["argv"][c["argv"].index("--effort") + 1] for c in coder_calls]
    assert models == ["claude-haiku-4-5-20251001", "claude-sonnet-5-5"]
    assert efforts == ["low", "high"]


def test_set_models_refused_outside_the_model_pick(harness):
    h = harness("happy_path")
    h.orch.start("add the files")
    with pytest.raises(WorkforceError, match="awaiting_models"):
        h.orch.set_models("T1", "claude-sonnet-5-5", "high")


def test_restart_after_every_step_repeats_nothing(harness):
    h = harness("happy_path")
    orch = h.orch
    orch.start("add the files")
    steps = 0
    while True:
        outcome = orch.step()
        steps += 1
        assert steps < 200
        if h.run().status == "awaiting_models":
            h.orch = orch
            h.pick_models()
            continue
        if not outcome.progressed:
            break
        orch = h.new_orchestrator()
        orch.resume()
    run = h.run()
    assert_merged(h, run, 2)
    calls = h.calls()
    assert len([c for c in calls if c["cli"] == "claude"]) == 8
    assert len([c for c in calls if c["cli"] == "codex"]) == 4
    assert len({c["id"] for c in calls}) == len(calls)
    assert [e["task"] for e in h.events("merged")] == ["T1", "T2"]
    assert len(h.events("review")) == 6
    assert [e["task"] for e in h.events("review")].count("integration") == 2
    assert len(h.events("committed")) == 2


def test_restart_between_commit_and_merge_does_not_commit_twice(harness):
    h = harness("happy_path")
    h.to_models()
    h.pick_models()
    h.step_until(lambda run: run.tasks[0].status == "committing")
    outcome = h.orch.step()
    assert outcome.action == "committed"
    assert h.task("T1").status == "committing"
    fresh = h.new_orchestrator()
    fresh.resume()
    assert fresh.step().action == "merged"
    assert len(prompts_with(h.calls(), "claude", "# Role: committer")) == 1


def test_usage_gate_pauses_before_any_agent_launches(harness):
    state = {"reason": "claude 5-hour window at 61%"}
    h = harness("happy_path", usage_gate=lambda: state["reason"])
    h.orch.start("add the files")
    run = h.orch.run_until_blocked()
    assert run.status == "paused"
    assert run.pause_reason == "claude 5-hour window at 61%"
    assert h.calls() == []
    state["reason"] = None
    run = h.orch.resume()
    assert run.status == "planning"
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_models"
    assert len(h.calls()) == 2


def test_usage_gate_called_before_every_coder_launch(harness):
    seen = []
    h = harness("happy_path", usage_gate=lambda: seen.append(1))
    h.go()
    assert len(seen) >= 2 + 2 * 3


def test_step_mode_asks_before_each_task(harness):
    h = harness("happy_path")
    h.to_models(mode="step")
    h.pick_models()
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_user"
    question = run.questions[0]
    assert question.task_id == "T1" and "Start T1" in question.text
    assert run.task("T1").status == "pending"
    assert h.orch.step().progressed is False
    h.orch.answer("Q1", "yes")
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_user" and run.questions[1].task_id == "T2"
    assert run.task("T1").status == "merged"
    assert "yes" not in h.paths.decisions_md.read_text()
    h.orch.answer("Q2", "yes, and keep it small")
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 2)
    assert "keep it small" in h.paths.decisions_md.read_text()


def test_step_mode_declining_pauses(harness):
    h = harness("happy_path")
    h.to_models(mode="step")
    h.pick_models()
    h.orch.run_until_blocked()
    run = h.orch.answer("Q1", "no")
    assert run.status == "paused" and "T1" in run.pause_reason
    assert run.task("T1").status == "pending"


def test_switching_to_auto_stops_the_confirmations(harness):
    h = harness("happy_path")
    h.to_models(mode="step")
    h.pick_models()
    h.orch.set_mode("auto")
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 2)


def test_manual_pause_and_resume_return_to_the_same_point(harness):
    h = harness("happy_path")
    h.orch.start("add the files")
    h.orch.step()
    run = h.orch.pause("lunch")
    assert run.status == "paused" and run.pause_reason == "lunch"
    assert h.orch.step().progressed is False
    run = h.orch.resume()
    assert run.status == "debating" and run.pause_reason is None


def test_start_refuses_while_a_run_is_active(harness):
    h = harness("happy_path")
    h.orch.start("first")
    with pytest.raises(WorkforceError, match="still"):
        h.orch.start("second")


def test_run_until_blocked_holds_the_lock(harness):
    h = harness("happy_path")
    h.orch.start("add the files")
    with h.store.lock():
        from workforce.errors import StateLockedError

        with pytest.raises(StateLockedError):
            h.orch.run_until_blocked()


def test_agent_streams_go_to_step_logs_and_state_changes_emit_events(harness):
    h = harness("happy_path")
    run = h.go()
    run_dir = h.paths.run_dir(run.id)
    assert (run_dir / "T1" / "code-1.jsonl").exists()
    assert (run_dir / "T1" / "review_claude-1.jsonl").exists()
    assert (run_dir / "T1" / "review_codex-1.jsonl").exists()
    assert (run_dir / "T1" / "commit-1.jsonl").exists()
    assert (run_dir / "plan" / "plan-1.jsonl").exists()
    transitions = [(e["task"], e["status"]) for e in h.events("task_status") if e["task"] == "T1"]
    assert transitions == [
        ("T1", "coding"),
        ("T1", "testing"),
        ("T1", "reviewing"),
        ("T1", "awaiting_commit"),
        ("T1", "committing"),
        ("T1", "merged"),
    ]
    assert h.events("commit_waiting") and "Touch ID" in h.events("commit_waiting")[0]["message"]


def test_invalid_plans_are_rejected():
    def item(tid, deps=(), acceptance=("done",), title="t"):
        return {"id": tid, "title": title, "description": "d", "acceptance": list(acceptance), "depends_on": list(deps)}

    ok = {"tasks": [item("T1"), item("T2", ["T1"])], "risks": [], "open_questions": []}
    assert validate_plan(ok) is None
    assert "no tasks" in validate_plan({"tasks": []})
    assert "unique" in validate_plan({"tasks": [item("T1"), item("T1")]})
    assert "unknown" in validate_plan({"tasks": [item("T1", ["T9"])]})
    assert "itself" in validate_plan({"tasks": [item("T1", ["T1"])]})
    assert "cycle" in validate_plan({"tasks": [item("T1", ["T2"]), item("T2", ["T1"])]})
    assert "acceptance" in validate_plan({"tasks": [item("T1", acceptance=())]})
    assert "title" in validate_plan({"tasks": [item("T1", title=" ")]})


def test_a_failed_commit_is_retried_once_then_asks_the_user(harness):
    h = harness("commit_failed")
    run = h.go()
    assert run.status == "awaiting_user"
    task = run.task("T1")
    assert task.status == "blocked"
    question = run.questions[0]
    assert "failed twice" in question.text and "Touch ID" in question.text
    assert len(prompts_with(h.calls(), "claude", "# Role: committer")) == 2
    assert len(h.events("commit_waiting")) == 2
    assert git_ops.head_sha(h.repo) == task.base_sha

    run = h.orch.answer("Q1", "I tapped it this time")
    assert run.task("T1").status == "awaiting_commit"
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 1)
    assert len(prompts_with(h.calls(), "claude", "# Role: committer")) == 3


def test_planner_crashes_are_retried_then_ask_the_user(harness):
    h = harness("planner_crash")
    h.orch.start("add the files")
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_user"
    assert h.clock.sleeps == [5, 20]
    question = run.questions[0]
    assert question.task_id is None and "planner failed (crash)" in question.text
    assert run.tasks == []
    run = h.orch.answer("Q1", "retry please")
    assert run.status == "planning"
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_models" and [t.id for t in run.tasks] == ["T1"]


def test_checks_that_never_pass_ask_the_user_after_three_fix_attempts(harness):
    h = harness("checks_never_pass", test_cmd="test -f ok.txt")
    run = h.go()
    assert run.status == "awaiting_user"
    task = run.task("T1")
    assert task.status == "blocked"
    assert len(prompts_with(h.calls(), "claude", "# Role: coder (fixing)")) == 3
    question = run.questions[0]
    assert "3 fix attempts" in question.text and "Checks: FAILED" in question.positions["checks"]
    assert [e["ok"] for e in h.events("check_result")] == [False] * 4

    run = h.orch.answer("Q1", "USER-HINT-CREATE-OK by writing ok.txt")
    assert run.task("T1").status == "fixing"
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 1)
    last_fix = prompts_with(h.calls(), "claude", "# Role: coder (fixing)")[-1]
    assert "USER-HINT-CREATE-OK" in last_fix["prompt"] and "Checks: FAILED" in last_fix["prompt"]


def test_a_coder_that_commits_is_stopped(harness):
    h = harness("coder_commits")
    run = h.go()
    assert run.status == "awaiting_user"
    assert run.task("T1").status == "blocked"
    assert "committed although only the committer may" in run.questions[0].text
    assert h.events("check_result") == []


def test_a_merge_that_cannot_fast_forward_asks_the_user(harness):
    h = harness("merge_blocked")
    run = h.go()
    assert run.status == "awaiting_user"
    task = run.task("T1")
    assert task.status == "blocked"
    assert "fast-forward" in run.questions[0].text
    assert git(h.repo, "log", "-1", "--format=%s", integration_branch(run)) == "moved"
    assert git(h.repo, "log", "-1", "--format=%s", "main") == "initial"
    assert git_ops.commit_tree(h.repo, task.branch) == task.head_sha
    assert Path(task.worktree).exists()
    assert h.events("merged") == []


def test_a_stale_branch_from_a_crashed_start_is_replaced(harness):
    h = harness("happy_path")
    h.to_models()
    h.pick_models()
    run_id = h.run().id
    git(h.repo, "branch", f"wf/{h.run().slug}-T1")
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 2)


def test_the_final_plan_review_can_still_agree_after_the_last_revision(harness):
    h = harness("plan_debate_final_agrees")
    run = h.to_models()
    assert run.questions == []
    calls = h.calls()
    assert len([c for c in calls if c["cli"] == "claude"]) == 4
    assert len([c for c in calls if c["cli"] == "codex"]) == 4
    revisions = [c for c in calls if c["cli"] == "codex"][1:]
    assert len(revisions) == 3


def test_open_plan_questions_go_to_the_user_when_the_decider_is_unsure(harness):
    h = harness("plan_open_question")
    h.orch.start("add the files")
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_user"
    question = run.questions[0]
    assert question.task_id is None
    assert "Which database should we use?" in question.text
    assert h.orch.step().progressed is False
    run = h.orch.answer("Q1", "Postgres")
    assert run.status == "awaiting_models"
    assert "Postgres" in h.paths.decisions_md.read_text()
    assert len(prompts_with(h.calls(), "claude", "# Role: plan reviewer")) == 1


class SplitDecider:
    """Says the agents can settle any open question that mentions AGENTS-Q, and the user must settle the rest."""

    def __init__(self):
        self.states = []

    def available(self):
        return True

    def ask_bool(self, key, state, question):
        return Decision(key=key, question=question, answer=False, confidence=0.99, source="laya")

    def ask_choice(self, key, state, question, options):
        self.states.append((key, state))
        answer = "agents" if "AGENTS-Q" in state else "user"
        return Decision(key=key, question=question, answer=answer, confidence=0.99, source="laya")


def test_open_plan_questions_the_agents_can_settle_go_to_the_plan_reviewer(harness):
    decider = SplitDecider()
    h = harness("plan_open_question_split", decider=decider)
    h.orch.start("add the files")
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_user"
    assert [q.text for q in run.questions] == ["The planner left an open question: USER-Q which cloud account?"]
    assert all(key == "needs_human" and "Goal: add the files" in state for key, state in decider.states)
    assert len(decider.states) == 2
    assert len(prompts_with(h.calls(), "claude", "# Role: plan reviewer")) == 1

    run = h.orch.answer("Q1", "the staging account")
    assert run.status == "debating"
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_models"
    reviews = prompts_with(h.calls(), "claude", "# Role: plan reviewer")
    assert len(reviews) == 2
    settle = reviews[1]["prompt"].split("## Open questions the agents should settle")[1].split("## Plan under review")[0]
    assert "AGENTS-Q which naming style?" in settle and "USER-Q" not in settle
    first_settle = reviews[0]["prompt"].split("## Open questions the agents should settle")[1].split("## Plan under review")[0]
    assert "AGENTS-Q" not in first_settle and "(none)" in first_settle
    assert "the staging account" in reviews[1]["prompt"]


def test_open_questions_are_asked_only_once(harness):
    decider = SplitDecider()
    h = harness("plan_open_question_split", decider=decider)
    h.orch.start("add the files")
    h.orch.run_until_blocked()
    h.orch.answer("Q1", "staging")
    h.orch.run_until_blocked()
    assert len(decider.states) == 2
    assert len(h.run().questions) == 1


def test_resume_reloads_workforce_toml_but_keeps_user_picked_task_models(harness):
    h = harness("happy_path")
    h.write_config_file()
    h.to_models()
    h.pick_models()
    h.step_until(lambda run: run.tasks[0].status == "merged")
    h.orch.pause("changing models")
    config_mod.save_role(h.repo, "coder", "claude-haiku-4-5-20251001", "low")
    config_mod.save_role(h.repo, "reviewer_claude", "claude-sonnet-5-5", "medium")
    config_mod.save_role(h.repo, "reviewer_codex", "gpt-6-astra", "xhigh")
    assert h.orch.config.role("reviewer_claude").model == "claude-opus-5-5"
    h.orch.resume()
    assert h.orch.config.role("reviewer_claude").model == "claude-sonnet-5-5"
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 2)

    calls = h.calls()
    coders = prompts_with(calls, "claude", "# Role: coder")
    assert [c["argv"][c["argv"].index("--model") + 1] for c in coders] == ["claude-sonnet-5-5"] * 2
    claude_reviews = prompts_with(calls, "claude", REVIEWER_MATCH)
    first, second = claude_reviews
    assert first["argv"][first["argv"].index("--model") + 1] == "claude-opus-5-5"
    assert second["argv"][second["argv"].index("--model") + 1] == "claude-sonnet-5-5"
    assert second["argv"][second["argv"].index("--effort") + 1] == "medium"
    codex_reviews = prompts_with(calls, "codex", REVIEWER_MATCH)
    assert codex_reviews[0]["argv"][codex_reviews[0]["argv"].index("-m") + 1] == "gpt-6-sol"
    assert codex_reviews[1]["argv"][codex_reviews[1]["argv"].index("-m") + 1] == "gpt-6-astra"


def test_a_model_change_made_while_paused_unavailable_takes_effect_and_the_good_verdict_is_kept(harness):
    h = harness("reviewer_model_unavailable")
    h.write_config_file()
    run = h.go()
    assert run.status == "paused" and run.pause_reason == "model_unavailable: gpt-6-sol"
    config_mod.save_role(h.repo, "reviewer_codex", "gpt-6-astra", "high")
    h.orch.resume()
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 1)
    calls = h.calls()
    assert len(prompts_with(calls, "claude", REVIEWER_MATCH)) == 1
    codex = prompts_with(calls, "codex", REVIEWER_MATCH)
    assert [c["argv"][c["argv"].index("-m") + 1] for c in codex] == ["gpt-6-sol", "gpt-6-astra"]


def test_resume_with_an_invalid_workforce_toml_fails_loudly_and_changes_nothing(harness):
    h = harness("happy_path")
    h.write_config_file()
    h.to_models()
    h.orch.pause("edit")
    (h.repo / "workforce.toml").write_text("[bin]\n")
    with pytest.raises(config_mod.ConfigError):
        h.orch.resume()
    assert h.run().status == "paused"


def test_a_fifty_percent_reading_alerts_once_and_never_stops_the_run(harness):
    h = harness("monitor_alert", clock_start=1_790_670_000.0, monitor=True)
    run = h.go()
    assert_merged(h, run, 1)
    alerts = h.events("alert")
    assert len(alerts) == 1
    assert alerts[0]["source"] == "claude" and alerts[0]["window"] == "five_hour"
    assert alerts[0]["percent"] == 50
    assert h.events("paused") == []
    readings = h.monitor.readings()
    assert readings["sources"]["codex"]["freshness"] == "fresh"
    assert readings["sources"]["claude"]["freshness"] == "fresh"
    assert h.paths.usage_json.exists()


def test_a_five_hour_window_at_sixty_percent_pauses_then_auto_resumes_after_the_reset(harness):
    h = harness("monitor_five_hour_pause", clock_start=1_790_670_000.0, monitor=True)
    run = h.go()
    assert run.status == "paused"
    assert run.pause_reason == "usage: claude five_hour 61% ≥ 60%"
    assert run.task("T1").status == "reviewing"
    assert h.events("paused")[-1]["reason"] == run.pause_reason
    assert len(h.events("alert")) == 1
    review_calls_before = len(prompts_with(h.calls(), "claude", REVIEWER_MATCH))
    assert review_calls_before == 0

    assert h.orch.poll_tick() == "none"
    assert h.run().status == "paused"
    h.clock.now = 1_790_676_059.0
    assert h.orch.poll_tick() == "none"

    h.clock.now = 1_790_676_060.0
    h.pinger.rate_limits = windows(0.10, 0.10)
    assert h.orch.poll_tick() == "resume"
    run = h.run()
    assert run.status == "running" and run.pause_reason is None
    assert h.events("resumed")[-1]["status"] == "running"

    run = h.orch.run_until_blocked()
    assert_merged(h, run, 1)


def test_auto_resume_waits_while_the_fresh_claude_reading_is_still_high(harness):
    h = harness("monitor_five_hour_pause", clock_start=1_790_676_060.0 - 6_000, monitor=True)
    h.go()
    h.clock.now = 1_790_676_060.0
    h.pinger.rate_limits = windows(0.75, 0.10, five_reset=1_790_694_000)
    assert h.orch.poll_tick() == "none"
    assert h.run().status == "paused"
    h.clock.now = 1_790_694_060.0
    h.pinger.rate_limits = windows(0.05, 0.10, five_reset=1_790_712_000)
    assert h.orch.poll_tick() == "resume"


def test_a_weekly_window_at_sixty_percent_pauses_and_poll_tick_never_resumes_it(harness):
    h = harness("monitor_weekly_pause", clock_start=1_790_670_000.0, monitor=True)
    run = h.go()
    assert run.status == "paused"
    assert run.pause_reason == "usage: claude seven_day 61% ≥ 60%"

    resumed_before = len(h.events("resumed"))
    h.pinger.rate_limits = windows(0.05, 0.05)
    for now in (1_790_676_060.0, 1_790_838_000.0 + 3_600, 1_791_000_000.0):
        h.clock.now = now
        assert h.orch.poll_tick() == "none"
        assert h.run().status == "paused"
    assert all("Reply OK" not in r.prompt for r in h.pinger.requests)
    assert len(h.events("resumed")) == resumed_before


def test_resume_of_a_usage_pause_needs_fresh_readings_below_the_line(harness):
    h = harness("monitor_weekly_pause", clock_start=1_790_670_000.0, monitor=True)
    h.go()
    h.clock.now = 1_791_000_000.0
    h.pinger.rate_limits = windows(0.05, 0.70, week_reset=1_791_600_000)
    with pytest.raises(WorkforceError, match="still at or above the pause line"):
        h.orch.resume()
    assert h.run().status == "paused"

    h.pinger.rate_limits = windows(0.05, 0.10, week_reset=1_791_600_000)
    run = h.orch.resume()
    assert run.status == "running"
    run = h.orch.run_until_blocked()
    assert_merged(h, run, 1)


def test_manual_resume_of_a_non_usage_pause_does_not_consult_the_monitor(harness):
    h = harness("monitor_alert", clock_start=1_790_670_000.0, monitor=True)
    h.orch.start("add the files")
    h.orch.pause("lunch")
    requests_before = len(h.pinger.requests)
    run = h.orch.resume()
    assert run.status == "planning"
    assert len(h.pinger.requests) == requests_before


def test_poll_tick_without_a_monitor_does_nothing(harness):
    h = harness("happy_path")
    h.orch.start("add the files")
    assert h.orch.poll_tick() == "none"
    assert h.run().status == "planning"


def test_poll_tick_pauses_a_running_run_when_the_gate_trips(harness):
    h = harness("monitor_five_hour_pause", clock_start=1_790_670_000.0, monitor=True)
    h.orch.start("add the files")
    h.pinger.rate_limits = windows(0.62, 0.10)
    assert h.orch.poll_tick() == "pause"
    run = h.run()
    assert run.status == "paused" and run.pause_reason == "usage: claude five_hour 62% ≥ 60%"
    assert h.events("paused")[-1]["reason"] == run.pause_reason


PUBLIC_API = (
    "step_task",
    "load",
    "emit",
    "set_task",
    "task_meta",
    "meta_edit",
    "gate_reason",
    "gate_blocked",
    "launch",
    "handle_failure",
    "ask",
    "log_path",
    "decisions",
    "commit_exists",
    "already_merged",
    "finish_task",
    "finish_run",
    "current_task",
)


def wait_for_calls(h, count, timeout=30.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(h.calls()) >= count:
            return
        time.sleep(0.02)
    raise AssertionError(f"fewer than {count} agent calls started")


def test_the_scheduler_api_is_public_and_documented(harness):
    import inspect

    h = harness("happy_path")
    for name in PUBLIC_API:
        method = getattr(h.orch, name)
        assert callable(method) and inspect.getdoc(method), name
    for private, public in (
        ("_load", "load"),
        ("_ask", "ask"),
        ("_emit", "emit"),
        ("_set_task", "set_task"),
        ("_meta_edit", "meta_edit"),
        ("_gate_reason", "gate_reason"),
    ):
        assert callable(getattr(h.orch, private)) and callable(getattr(h.orch, public))


def test_step_task_drives_one_task_at_a_time_like_step(harness):
    h = harness("happy_path")
    h.to_models()
    h.pick_models()
    seen = []
    for task_id in ("T1", "T2"):
        for _ in range(40):
            outcome = h.orch.step_task(task_id)
            seen.append(outcome.action)
            if h.task(task_id).status == "merged":
                break
        assert h.task(task_id).status == "merged"
    assert h.orch.step_task("T1").action == "idle" and h.orch.step_task("T1").progressed is False
    run = h.orch.step()
    assert run.action == "done"
    assert_merged(h, h.run(), 2)
    assert seen.count("merged") == 2


def test_step_task_on_a_blocked_task_is_idle(harness):
    h = harness("three_rounds_question")
    run = h.go()
    assert run.task("T1").status == "blocked"
    outcome = h.orch.step_task("T1")
    assert (outcome.action, outcome.progressed) == ("idle", False)


def test_ask_is_atomic_across_threads(harness):
    h = harness("happy_path")
    h.orch.start("add the files")
    errors = []

    def worker(tag):
        try:
            for i in range(20):
                h.orch.ask("infra_run", f"{tag}-{i}", {}, resume_run="planning")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert errors == []
    run = h.run()
    ids = [q.id for q in run.questions]
    assert len(ids) == 40 and len(set(ids)) == 40
    assert {q.text for q in run.questions} == {f"{t}-{i}" for t in ("a", "b") for i in range(20)}
    sidecar = h.orch._meta(run.id)["questions"]
    assert set(sidecar) == set(ids)
    assert all(info["kind"] == "infra_run" and info["resume_run"] == "planning" for info in sidecar.values())
    assert len([e for e in h.events("question")]) == 40


def test_meta_edit_read_modify_write_is_atomic_across_threads(harness):
    h = harness("happy_path")
    run = h.orch.start("add the files")

    def bump(m):
        m["counter"] = m.get("counter", 0) + 1

    def worker():
        for _ in range(20):
            h.orch.meta_edit(run.id, bump)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert h.orch._meta(run.id)["counter"] == 40


def test_concurrent_failures_on_two_tasks_keep_separate_counts(harness):
    h = harness("happy_path")
    h.to_models()
    h.pick_models()
    for task_id in ("T1", "T2"):
        h.orch.set_task(task_id, status="reviewing")
    run = h.run()

    def fail(task_id):
        task = h.run().task(task_id)
        h.orch._register_failure(h.run(), task, f"{task_id} fell over")

    threads = [threading.Thread(target=fail, args=(t,)) for t in ("T1", "T2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    run = h.run()
    assert run.task("T1").infra_failures == 1 and run.task("T2").infra_failures == 1


def test_a_pause_while_the_planner_is_in_flight_sticks_and_resume_continues(harness):
    h = harness("plan_slow")
    h.orch.start("add the files")
    worker = threading.Thread(target=h.orch.step)
    worker.start()
    wait_for_calls(h, 1)
    h.orch.pause("hold on")
    worker.join(60)
    run = h.run()
    assert run.status == "paused" and run.pause_reason == "hold on"
    assert [t.id for t in run.tasks] == ["T1"]
    assert h.orch._meta(run.id)["paused_from"] == "debating"
    assert h.orch.step().progressed is False

    run = h.orch.resume()
    assert run.status == "debating" and run.pause_reason is None
    run = h.orch.run_until_blocked()
    assert run.status == "awaiting_models"


def test_a_pause_while_the_plan_review_is_in_flight_sticks_and_resume_continues(harness):
    h = harness("plan_review_slow")
    h.orch.start("add the files")
    assert h.orch.step().action == "plan"
    worker = threading.Thread(target=h.orch.step)
    worker.start()
    wait_for_calls(h, 2)
    h.orch.pause("hold on")
    worker.join(60)
    run = h.run()
    assert run.status == "paused" and run.pause_reason == "hold on"
    assert h.orch._meta(run.id)["paused_from"] == "awaiting_models"

    run = h.orch.resume()
    assert run.status == "awaiting_models"
    h.pick_models()
    assert h.run().status == "running"


def test_a_question_created_while_paused_keeps_the_pause_and_resume_lands_on_awaiting_user(harness):
    h = harness("happy_path")
    h.orch.start("add the files")
    h.orch.pause("hold on")
    h.orch.ask("infra_run", "please decide", {}, resume_run="planning")
    run = h.run()
    assert run.status == "paused" and run.questions[0].status == "open"
    assert h.orch._meta(run.id)["paused_from"] == "awaiting_user"
    run = h.orch.resume()
    assert run.status == "awaiting_user"


def test_an_answer_given_while_paused_keeps_the_pause_and_resume_continues(harness):
    h = harness("happy_path")
    h.orch.start("add the files")
    h.orch.ask("infra_run", "please decide", {}, resume_run="planning")
    h.orch.pause("hold on")
    run = h.orch.answer("Q1", "go on")
    assert run.status == "paused" and run.questions[0].status == "answered"
    assert h.orch._meta(run.id)["paused_from"] == "planning"
    run = h.orch.resume()
    assert run.status == "planning"


def test_merged_tasks_leave_no_branch_worktree_or_run_dir_behind(harness):
    h = harness("happy_path")
    run = h.go()
    assert run.status == "done"
    assert git(h.repo, "branch", "--list", "wf/*").split() == [integration_branch(run)]
    assert sorted(git(h.repo, "branch", "--format=%(refname:short)").split()) == sorted(["main", integration_branch(run)])
    assert git(h.repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not h.paths.worktrees_root.joinpath(run.id).exists()
    assert [e for e in h.events("note") if "kept" in e["text"]] == []


def test_the_branch_is_deleted_as_soon_as_its_task_merges_not_only_at_the_end(harness):
    h = harness("happy_path")
    h.to_models()
    h.pick_models()
    run = h.step_until(lambda r: r.tasks[0].status == "merged")
    assert git(h.repo, "branch", "--list", f"wf/{run.slug}-T1") == ""
    assert h.paths.worktrees_root.joinpath(run.id).exists()
    assert run.tasks[1].status == "pending"


def test_when_the_safe_delete_refuses_the_branch_is_kept_and_a_note_says_why(harness, monkeypatch):
    from workforce.errors import GitError

    def refusing(repo, name, force=False):
        assert force is False
        raise GitError("git branch -d failed: the branch is not fully merged")

    monkeypatch.setattr(git_ops, "delete_branch", refusing)
    h = harness("happy_path")
    run = h.go()
    assert run.status == "done" and [t.status for t in run.tasks] == ["merged", "merged"]
    left = git(h.repo, "branch", "--list", "wf/*").split()
    branch = integration_branch(run)
    assert sorted(left) == [branch, f"{branch}-T1", f"{branch}-T2"]
    notes = [e for e in h.events("note") if "kept branch" in e["text"]]
    assert len(notes) == 2 and "not fully merged" in notes[0]["text"] and notes[0]["task"] == "T1"
    assert not h.paths.worktrees_root.joinpath(run.id).exists()


def test_cleanup_never_forces_a_branch_delete(harness, monkeypatch):
    deletes = []
    removals = []
    real_delete = git_ops.delete_branch
    real_remove = git_ops.remove_worktree

    def delete_spy(repo, name, force=False):
        deletes.append((Path(repo).name, name, force))
        return real_delete(repo, name, force=force)

    def remove_spy(repo, path, branch=None, delete_branch=False, force_branch=False, **kwargs):
        removals.append((branch, delete_branch, force_branch))
        return real_remove(repo, path, branch=branch, delete_branch=delete_branch, force_branch=force_branch, **kwargs)

    monkeypatch.setattr(git_ops, "delete_branch", delete_spy)
    monkeypatch.setattr(git_ops, "remove_worktree", remove_spy)
    h = harness("happy_path")
    run = h.go()
    branch = integration_branch(run)
    assert [(where, name, force) for where, name, force in deletes] == [
        ("_integration", f"{branch}-T1", False),
        ("_integration", f"{branch}-T2", False),
    ]
    assert all(not delete and not force for _, delete, force in removals)


def test_a_restart_after_the_merge_but_before_it_was_recorded_still_deletes_the_branch(harness):
    h = harness("happy_path")
    h.to_models()
    h.pick_models()
    run = h.step_until(lambda r: r.tasks[0].status == "committing")
    assert h.orch.step().action == "committed"
    task = h.task("T1")
    integration = Path(h.run().integration["repo"].worktree)
    git(integration, "merge", "--ff-only", task.branch)
    git_ops.remove_worktree(h.repo, task.worktree, managed_root=h.home / ".workforce" / "worktrees")
    assert git(h.repo, "branch", "--list", task.branch)
    fresh = h.new_orchestrator()
    fresh.resume()
    assert fresh.step().action == "merged"
    assert git(h.repo, "branch", "--format=%(refname:short)", "--list", "wf/*").split() == [integration_branch(run)]
    assert h.task("T1").status == "merged"


def test_a_run_dir_that_is_not_empty_is_left_alone(harness):
    h = harness("happy_path")
    h.to_models()
    h.pick_models()
    run_id = h.run().id
    run_dir = h.paths.worktrees_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "keep-me.txt").write_text("not ours\n")
    run = h.orch.run_until_blocked()
    assert run.status == "done"
    assert (run_dir / "keep-me.txt").read_text() == "not ours\n"


def test_a_run_that_is_not_done_keeps_its_run_dir(harness):
    h = harness("three_rounds_question")
    run = h.go()
    assert run.status == "awaiting_user"
    assert h.paths.worktrees_root.joinpath(run.id, "repo", "T1").exists()
    assert h.paths.worktrees_root.joinpath(run.id, "repo", "_integration").exists()
    assert git(h.repo, "branch", "--list", f"wf/{run.slug}-T1")


def rebased_task(h, rebase=True, move_main=True):
    """Get T1 to `reviewing`, then play the parts of the committer, of a second merged task and of the rebase.

    T1's a.txt is committed by the test (recorded as a committer commit, as `_launch` would), the integration
    branch gains an unrelated commit that touches other.txt, and, when `rebase` is true, the task branch is
    rebased onto it. With `move_main=False` the integration branch is left alone. Returns (task, old_base,
    main_sha), where main_sha is the integration branch's HEAD. `base_sha` on the task is deliberately left at
    the old base.
    """
    h.to_models()
    h.pick_models()
    h.step_until(lambda run: run.tasks[0].status == "reviewing")
    task = h.task("T1")
    worktree = Path(task.worktree)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "T1: a")
    h.orch._record_committer_sha("T1", git(worktree, "rev-parse", "HEAD"))
    run = h.run()
    integration = Path(run.integration["repo"].worktree)
    if move_main:
        (integration / "other.txt").write_text("from main\n")
        git(integration, "add", "-A")
        git(integration, "commit", "-q", "-m", "T0: other")
    main_sha = git_ops.head_sha(integration)
    if rebase:
        git(worktree, "rebase", integration_branch(run))
        h.orch._record_committer_sha("T1", git(worktree, "rev-parse", "HEAD"))
    return task, task.base_sha, main_sha


def void_for_recheck(h):
    def fn(run):
        item = run.task("T1")
        item.reviews = []
        item.head_sha = None
        item.status = "testing"

    h.store.update(fn)


def diff_section(prompt: str) -> str:
    return prompt.split("## Diff against the base commit")[1]


def test_a_drift_after_a_rebase_moves_base_sha_and_the_next_review_excludes_mains_files(harness):
    h = harness("rebased_drift")
    task, old_base, main_sha = rebased_task(h)
    assert old_base != main_sha
    assert h.orch._rebased_base(h.task("T1")) == main_sha
    void_for_recheck(h)

    h.step_until(lambda run: run.tasks[0].status == "awaiting_commit")
    assert h.task("T1").base_sha == old_base
    first = prompts_with(h.calls(), "claude", REVIEWER_MATCH)[0]["prompt"]
    assert "diff --git a/other.txt" in diff_section(first)

    (Path(task.worktree) / "extra.txt").write_text("late change\n")
    outcome = h.orch.step()
    assert outcome.action == "voided"
    voided = h.task("T1")
    assert voided.base_sha == main_sha and voided.reviews == [] and voided.status == "testing"
    notes = [e["text"] for e in h.events("note") if "now contains the integration branch" in e["text"]]
    assert len(notes) == 1 and main_sha[:7] in notes[0]

    h.step_until(lambda run: run.tasks[0].status == "awaiting_commit")
    reviews = prompts_with(h.calls(), "claude", REVIEWER_MATCH) + prompts_with(h.calls(), "codex", REVIEWER_MATCH)
    second = [c["prompt"] for c in reviews if "extra.txt" in diff_section(c["prompt"])]
    assert len(second) == 2
    for prompt in second:
        section = diff_section(prompt)
        assert "diff --git a/other.txt" not in section
        assert "diff --git a/a.txt" in section and f"Base commit: {main_sha}" in prompt
    approved = {r.reviewer: r for r in h.task("T1").reviews}
    assert {r.base_sha for r in approved.values()} == {main_sha}

    assert h.step_until(lambda run: run.tasks[0].status == "committing")
    assert h.orch.step().action == "committed"
    assert h.orch.step().action == "merged"
    assert len(prompts_with(h.calls(), "claude", "# Role: committer")) == 1
    run = h.run()
    assert run.tasks[0].status == "merged"
    branch = integration_branch(run)
    assert git(h.repo, "log", "--format=%s", branch).splitlines() == ["T1: extra", "T1: a", "T0: other", "initial"]
    assert git_ops.commit_tree(h.repo, branch) == run.tasks[0].head_sha


def test_commit_exists_accepts_the_rebased_committer_commit_when_mains_commits_are_in_the_range(harness):
    h = harness("rebased_drift")
    task, old_base, main_sha = rebased_task(h)
    worktree = Path(task.worktree)
    tree = git_ops.commit_tree(worktree)
    h.orch.set_task("T1", status="committing", head_sha=tree)
    task = h.task("T1")
    assert task.base_sha == old_base
    assert git(worktree, "rev-list", "--count", f"{old_base}..HEAD") == "2"
    assert git(worktree, "rev-list", "--count", f"{main_sha}..HEAD") == "1"
    assert h.orch.commit_exists(task) is True


def test_commit_exists_still_rejects_unrecorded_commits_after_a_rebase(harness):
    h = harness("rebased_drift")
    task, _, _ = rebased_task(h)
    worktree = Path(task.worktree)
    (worktree / "foreign.txt").write_text("not the committer\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "someone else")
    h.orch.set_task("T1", status="committing", head_sha=git_ops.commit_tree(worktree))
    assert h.orch.commit_exists(h.task("T1")) is False

    h.orch.meta_edit(h.run().id, lambda m: h.orch.task_meta(m, "T1").update(committer_shas=[]))
    assert h.orch.commit_exists(h.task("T1")) is False


def test_commit_exists_after_a_rebase_needs_the_rebased_commit_itself_to_be_recorded(harness):
    h = harness("rebased_drift")
    task, _, _ = rebased_task(h)
    worktree = Path(task.worktree)
    head = git(worktree, "rev-parse", "HEAD")
    h.orch.meta_edit(
        h.run().id,
        lambda m: h.orch.task_meta(m, "T1").update(
            committer_shas=[s for s in h.orch.task_meta(m, "T1")["committer_shas"] if s != head]
        ),
    )
    h.orch.set_task("T1", status="committing", head_sha=git_ops.commit_tree(worktree))
    assert h.orch.commit_exists(h.task("T1")) is False


def test_a_branch_that_does_not_contain_main_keeps_its_base_when_approvals_are_voided(harness):
    h = harness("rebased_drift")
    task, old_base, main_sha = rebased_task(h, rebase=False)
    assert h.orch._rebased_base(h.task("T1")) is None
    h.orch.set_task("T1", status="awaiting_commit", head_sha=git_ops.tree_hash(Path(task.worktree)))
    outcome = h.orch.step()
    assert outcome.action == "voided"
    assert h.task("T1").base_sha == old_base != main_sha
    assert [e for e in h.events("note") if "now contains main" in e["text"]] == []


def test_a_committed_but_not_rebased_branch_is_still_recognised_and_main_moving_does_not_change_that(harness):
    h = harness("rebased_drift")
    task, old_base, _ = rebased_task(h, rebase=False)
    worktree = Path(task.worktree)
    h.orch.set_task("T1", status="committing", head_sha=git_ops.commit_tree(worktree))
    assert h.orch._rebased_base(h.task("T1")) is None
    assert h.orch.commit_exists(h.task("T1")) is True


def test_when_main_does_not_move_the_base_is_never_replaced(harness):
    h = harness("rebased_drift")
    h.to_models()
    h.pick_models()
    h.step_until(lambda run: run.tasks[0].status == "awaiting_commit")
    task = h.task("T1")
    assert h.orch._rebased_base(task) is None
    (Path(task.worktree) / "extra.txt").write_text("late change\n")
    assert h.orch.step().action == "voided"
    assert h.task("T1").base_sha == task.base_sha == git_ops.head_sha(h.repo)
    assert [e for e in h.events("note") if "now contains main" in e["text"]] == []


def test_a_restart_after_main_was_fast_forwarded_to_the_branch_tip_does_not_ask_for_a_second_commit(harness):
    h = harness("rebased_drift")
    task, _, _ = rebased_task(h, rebase=False, move_main=False)
    worktree = Path(task.worktree)
    h.orch.set_task("T1", status="committing", head_sha=git_ops.commit_tree(worktree))
    git(h.repo, "merge", "--ff-only", task.branch)
    assert git_ops.head_sha(h.repo) == git_ops.head_sha(worktree)
    fresh = h.new_orchestrator()
    fresh.resume()
    assert fresh.commit_exists(h.task("T1")) is True
    assert fresh.step_task("T1").action == "merged"
    assert prompts_with(h.calls(), "claude", "# Role: committer") == []
