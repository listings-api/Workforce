"""Workspace mode end to end: several repos, integration branches, cross-repo review, and the user's checkouts left alone."""

import json
import re
import subprocess
import threading
from pathlib import Path

import pytest

from workforce import config as config_mod
from workforce import git_ops, workspace as ws_mod
from workforce.agents.base import AgentResult
from workforce.decider.base import NullDecider
from workforce.errors import WorkforceError
from workforce.events import EventLog
from workforce.orchestrator import OVERVIEW_MAX_CHARS, Orchestrator, repo_overview
from workforce.state import Task

TASK_PATTERN = re.compile(r"## Task (T\d+):")
INTEGRATION_MATCH = "# Role: independent integration reviewer"
REPO_NAMES = ["alpha", "beta", "tools/gamma"]
CODER = ("claude-sonnet-5-5", "high")


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def task_spec(task_id, repo, depends_on=(), title=None):
    return {
        "id": task_id,
        "title": title or f"Change {task_id.lower()} in {repo}",
        "description": f"Create {task_id.lower()}.txt in {repo}",
        "acceptance": [f"{task_id.lower()}.txt exists"],
        "depends_on": list(depends_on),
        "repo": repo,
    }


def cross_repo_plan():
    return [task_spec("T1", "alpha"), task_spec("T2", "beta", ["T1"])]


def request_changes(message="the client reads a field the API does not send"):
    finding = {"severity": "major", "file": "beta/t2.txt", "line": None, "message": message}
    return {"verdict": "REQUEST_CHANGES", "summary": "the pieces do not fit", "findings": [finding]}


def approve(summary="fits"):
    return {"verdict": "APPROVE", "summary": summary, "findings": []}


class WsTeam:
    """Both CLIs in one object; every request is kept with the kind it was classified as."""

    def __init__(self, plan_tasks):
        self.plan_replies = [plan_tasks]
        self.fix_replies = []
        self.integration = {"reviewer_claude": [], "reviewer_codex": []}
        self.requests = []
        self.coder_saw = {}
        self._lock = threading.Lock()

    def run(self, req, on_event=None):
        kind = self._kind(req)
        with self._lock:
            self.requests.append((kind, req))
        return getattr(self, f"_{kind}")(req)

    def of(self, kind):
        with self._lock:
            return [req for k, req in self.requests if k == kind]

    @staticmethod
    def _kind(req):
        prompt = req.prompt
        if "# Role: planner (fixing after integration review)" in prompt:
            return "integration_fix"
        if "# Role: planner (revising" in prompt:
            return "plan_revise"
        if "# Role: plan reviewer" in prompt:
            return "plan_review"
        if "# Role: planner" in prompt:
            return "plan"
        if "# Role: committer (rebase)" in prompt:
            return "rebase"
        if "# Role: committer" in prompt:
            return "commit"
        if INTEGRATION_MATCH in prompt:
            return "integration_review"
        if req.role.startswith("reviewer_"):
            return "reviewer"
        return "coder"

    @staticmethod
    def ok(req, text="", structured=None):
        return AgentResult(ok=True, text=text, structured=structured, session_id=f"s-{req.role}", model=req.model)

    def _plan(self, req):
        tasks = self.plan_replies.pop(0) if len(self.plan_replies) > 1 else self.plan_replies[0]
        return self.ok(req, structured={"tasks": tasks, "risks": [], "open_questions": []})

    def _plan_review(self, req):
        return self.ok(req, structured={"agree": True, "objections": [], "suggested_changes": []})

    def _integration_fix(self, req):
        tasks = self.fix_replies.pop(0)
        debate = {"position": "fix what the reviewers found", "concessions": [], "remaining_disagreements": []}
        return self.ok(req, structured={"plan": {"tasks": tasks, "risks": [], "open_questions": []}, "debate": debate})

    def _coder(self, req):
        task = TASK_PATTERN.search(req.prompt).group(1)
        self.coder_saw[task] = {p: sorted(x.name for x in Path(p).iterdir() if x.is_file()) for p in req.add_dirs}
        (Path(req.cwd) / f"{task.lower()}.txt").write_text(f"{task}\n")
        return self.ok(req, text=f"Created {task.lower()}.txt")

    def _reviewer(self, req):
        return self.ok(req, structured=approve())

    def _integration_review(self, req):
        queue = self.integration[req.role]
        return self.ok(req, structured=queue.pop(0) if queue else approve())

    def _commit(self, req):
        task = TASK_PATTERN.search(req.prompt).group(1)
        git(req.cwd, "add", "-A")
        git(req.cwd, "commit", "-q", "-m", f"{task}: change")
        sha = git(req.cwd, "rev-parse", "HEAD")
        return self.ok(req, structured={"committed": True, "sha": sha, "message": "m", "error": None})

    def _rebase(self, req):
        git(req.cwd, "rebase", re.search(r"integration branch `([^`]+)`", req.prompt).group(1))
        sha = git(req.cwd, "rev-parse", "HEAD")
        return self.ok(req, structured={"committed": True, "sha": sha, "message": "m", "error": None})


class FakeClock:
    def time(self):
        return 1_800_000_000.0

    def sleep(self, seconds):
        return None


class Ws:
    """A workspace of alpha, beta and tools/gamma plus an orchestrator wired to a `WsTeam`."""

    def __init__(self, root: Path, home: Path, monkeypatch, plan_tasks=None, extra_toml="", repos=None):
        self.root, self.home = Path(root).resolve(), home
        self.repos = list(repos or REPO_NAMES)
        text = config_mod.workspace_toml(self.repos).replace('backend = "laya"', 'backend = "off"') + extra_toml
        self.config = config_mod.parse(text)
        self.workspace = ws_mod.load_workspace(self.root, self.config)
        self.team = WsTeam(plan_tasks if plan_tasks is not None else cross_repo_plan())
        self.monkeypatch = monkeypatch
        self.orch = self.new_orchestrator()

    def new_orchestrator(self):
        orch = Orchestrator(
            self.workspace,
            self.config,
            {"claude": self.team, "codex": self.team},
            NullDecider(),
            clock=FakeClock(),
            home=self.home,
            environ={},
        )
        self.monkeypatch.setattr(orch, "_preflight", lambda: None)
        return orch

    def repo(self, name) -> Path:
        return self.root / name

    def run(self):
        return self.orch.store.load()

    def events(self, kind=None):
        events, _ = EventLog(self.orch.paths).read()
        return [e for e in events if kind is None or e["kind"] == kind]

    def to_models(self, goal="wire the new API into the client"):
        self.orch.start(goal, "auto")
        run = self.orch.run_until_blocked()
        assert run.status == "awaiting_models", (run.status, run.pause_reason, [q.text for q in run.questions])
        return run

    def pick_models(self):
        for task in self.run().tasks:
            if not task.model:
                self.orch.set_models(task.id, *CODER)
        self.orch.resume()

    def go(self, goal="wire the new API into the client"):
        self.to_models(goal)
        self.pick_models()
        return self.orch.run_until_blocked()


@pytest.fixture
def ws(tmp_path, git_workspace, monkeypatch):
    def build(**kwargs):
        return Ws(git_workspace, tmp_path / "home", monkeypatch, **kwargs)

    return build


def checkout_state(root: Path, names) -> dict:
    return {
        name: {
            "head": git(root / name, "rev-parse", "HEAD"),
            "branch": git(root / name, "symbolic-ref", "--short", "HEAD"),
            "status": git(root / name, "status", "--porcelain"),
            "branches": git(root / name, "branch", "--format=%(refname:short)"),
        }
        for name in names
    }


def branch_names(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)", "--list", "wf/*").split()


def test_two_repo_run_with_a_cross_repo_dependency_lands_on_integration_branches(ws, git_workspace):
    w = ws(extra_toml='\n[repos.beta.checks]\ntest = "test -f t2.txt"\n')
    (git_workspace / "beta" / "README.md").write_text("edited by the user\n")
    (git_workspace / "alpha" / "scratch.txt").write_text("the user's untracked file\n")
    before = checkout_state(git_workspace, REPO_NAMES)
    assert "M README.md" in before["beta"]["status"] and "?? scratch.txt" in before["alpha"]["status"]

    run = w.go()

    assert run.status == "done", (run.status, run.pause_reason, [q.text for q in run.questions])
    assert [t.status for t in run.tasks] == ["merged", "merged"]
    assert [t.repo for t in run.tasks] == ["alpha", "beta"]
    assert set(run.integration) == {"alpha", "beta"}
    assert run.slug.startswith("wire-the-new-api-into-the-client-")
    branch = f"wf/{run.slug}"
    assert branch_names(git_workspace / "alpha") == [branch]
    assert branch_names(git_workspace / "beta") == [branch]
    assert branch_names(git_workspace / "tools" / "gamma") == []
    assert git(git_workspace / "alpha", "show", f"{branch}:t1.txt") == "T1"
    assert git(git_workspace / "beta", "show", f"{branch}:t2.txt") == "T2"
    assert git(git_workspace / "alpha", "log", "--format=%s", branch).splitlines() == ["T1: change", "initial"]
    assert git(git_workspace / "beta", "log", "--format=%s", branch).splitlines() == ["T2: change", "initial"]

    after = checkout_state(git_workspace, REPO_NAMES)
    for name in REPO_NAMES:
        assert after[name]["head"] == before[name]["head"]
        assert after[name]["branch"] == before[name]["branch"] == "main"
        assert after[name]["status"] == before[name]["status"]
    assert (git_workspace / "beta" / "README.md").read_text() == "edited by the user\n"
    assert (git_workspace / "alpha" / "scratch.txt").read_text() == "the user's untracked file\n"
    assert not (git_workspace / "alpha" / "t1.txt").exists()
    assert not (git_workspace / "beta" / "t2.txt").exists()

    assert not (w.orch.paths.worktrees_root / run.id).exists()
    for name in ("alpha", "beta"):
        assert git(git_workspace / name, "worktree", "list", "--porcelain").count("worktree ") == 1

    summary = w.events("run_done")[-1]["summary"]
    assert summary[-1] == "Run wf publish to push and open PRs."
    assert any(line.startswith(f"alpha: branch {branch}: 1 commit on top of main (") for line in summary)
    assert any(line.startswith(f"beta: branch {branch}: 1 commit on top of main (") for line in summary)
    by_repo = {item["repo"]: item for item in w.orch.branch_summary()}
    assert by_repo["alpha"]["commits"] == 1 and by_repo["beta"]["commits"] == 1
    assert by_repo["alpha"]["base_ref"] == "main" and by_repo["alpha"]["created"] is True
    assert by_repo["alpha"]["base_sha"] == before["alpha"]["head"]
    assert w.orch.integration("beta").branch == branch
    assert w.orch.integration("tools/gamma") is None


def test_the_dependent_coder_reads_the_other_repos_integration_worktree_and_writes_only_in_its_own(ws):
    w = ws()
    run = w.go()
    assert run.status == "done"
    t1, t2 = w.team.of("coder")
    alpha_integration = Path(run.integration["alpha"].worktree)
    assert t1.add_dirs == []
    assert t2.add_dirs == [alpha_integration]
    assert "t1.txt" in w.team.coder_saw["T2"][alpha_integration]
    assert str(alpha_integration) in t2.prompt
    assert "Repo: `beta`" in t2.prompt and str(Path(t2.cwd)) in t2.prompt
    assert t2.repo == w.root and t1.repo == w.root
    assert Path(t2.cwd).name == "T2" and Path(t2.cwd).parent.name == "beta"
    assert "Write only inside your worktree" in t2.prompt
    assert "(none: this task does not depend on work in other repos)" in t1.prompt


def test_a_dependency_in_the_same_repo_adds_no_extra_directory(ws):
    w = ws(plan_tasks=[task_spec("T1", "alpha"), task_spec("T2", "alpha", ["T1"])])
    assert w.go().status == "done"
    assert all(req.add_dirs == [] for req in w.team.of("coder"))


def test_codex_coders_get_no_extra_directories(ws):
    w = ws()
    w.to_models()
    w.orch.set_models("T1", *CODER)
    w.orch.set_models("T2", *CODER)
    w.orch.resume()
    w.orch.store.update(lambda r: setattr(r.task("T2"), "agent", "codex"))
    assert w.orch.run_until_blocked().status == "done"
    assert w.team.of("coder")[1].add_dirs == []


def test_planner_and_plan_reviewer_run_at_the_workspace_root_with_the_repo_overview(ws, git_workspace):
    w = ws()
    w.to_models()
    (plan,) = w.team.of("plan")
    (review,) = w.team.of("plan_review")
    for req in (plan, review):
        assert Path(req.cwd) == w.root
        assert req.sandbox == "read-only"
        assert req.skip_git_check is True
        assert req.repo == w.root
        for name in REPO_NAMES:
            assert f"### {name}" in req.prompt
    enum = plan.schema["properties"]["tasks"]["items"]["properties"]["repo"]["enum"]
    assert enum == REPO_NAMES
    assert "repo" in plan.schema["properties"]["tasks"]["items"]["required"]
    assert "one of these names: alpha, beta, tools/gamma" in plan.prompt
    assert "{decisions}" not in plan.prompt


def test_plan_md_groups_tasks_by_repo(ws):
    w = ws(plan_tasks=[task_spec("T1", "beta"), task_spec("T2", "alpha"), task_spec("T3", "beta", ["T1"])])
    w.to_models()
    text = w.orch.paths.plan_md.read_text()
    assert text.index("## Repo: alpha") < text.index("## Repo: beta")
    alpha_part, beta_part = text.split("## Repo: alpha")[1].split("## Repo: beta")
    assert "### T2:" in alpha_part and "### T1:" not in alpha_part
    assert "### T1:" in beta_part and "### T3:" in beta_part
    assert "tools/gamma" not in text


def test_an_unknown_repo_goes_back_to_the_planner_and_is_not_an_infra_failure(ws):
    bad = [task_spec("T1", "nowhere")]
    w = ws(plan_tasks=bad)
    w.team.plan_replies = [bad, cross_repo_plan()]
    w.orch.start("goal", "auto")
    outcome = w.orch.step()
    assert outcome.action == "plan_rejected" and "nowhere" in outcome.detail
    run = w.run()
    assert run.status == "planning" and run.tasks == []
    meta = w.orch._meta(run.id)
    assert meta["plan_failures"] == 0 and meta["repo_rejects"] == 1
    assert not w.events("error")
    assert w.orch.step().action == "plan"
    retry = w.team.of("plan")[1]
    assert "Your previous reply was rejected" in retry.prompt and "nowhere" in retry.prompt
    assert w.orch._meta(run.id)["repo_rejects"] == 0
    assert [t.repo for t in w.run().tasks] == ["alpha", "beta"]


def test_repeatedly_naming_unknown_repos_becomes_a_question(ws):
    bad = [task_spec("T1", "nowhere")]
    w = ws(plan_tasks=bad)
    w.orch.start("goal", "auto")
    limit = w.config.limits.debate_rounds
    for _ in range(limit):
        assert w.orch.step().action == "plan_rejected"
    outcome = w.orch.step()
    assert outcome.action == "question"
    run = w.run()
    assert run.status == "awaiting_user" and "wrong repos" in run.questions[0].text
    w.team.plan_replies = [cross_repo_plan()]
    w.orch.answer("Q1", "use alpha and beta")
    assert w.run().status == "planning" and w.orch._meta(run.id)["repo_rejects"] == 0
    assert w.orch.run_until_blocked().status == "awaiting_models"


def test_a_revised_plan_with_an_unknown_repo_is_rejected_too(ws):
    w = ws()
    w.orch.start("goal", "auto")
    w.orch.step()
    w.orch.store.update(lambda r: setattr(r, "status", "debating"))
    w.orch._meta_edit(
        w.run().id,
        lambda m: m["debate"].update(phase="revise", objections=["split it"], suggested=["split it"]),
    )
    bad_revision = {
        "plan": {"tasks": [task_spec("T1", "nowhere")], "risks": [], "open_questions": []},
        "debate": {"position": "p", "concessions": [], "remaining_disagreements": []},
    }
    w.team._plan_revise = lambda req: w.team.ok(req, structured=bad_revision)
    outcome = w.orch.step()
    assert outcome.action == "plan_rejected" and "nowhere" in outcome.detail
    assert w.orch._meta(w.run().id)["debate"]["round"] == 0


def test_integration_reviewers_are_fresh_read_only_at_the_root_and_see_every_repo(ws, git_workspace):
    w = ws()
    run = w.go()
    assert run.status == "done"
    reviews = w.team.of("integration_review")
    assert sorted(req.role for req in reviews) == ["reviewer_claude", "reviewer_codex"]
    for req in reviews:
        assert Path(req.cwd) == w.root
        assert req.sandbox == "read-only"
        assert req.skip_git_check is True
        assert req.resume_session is None
        assert req.repo == w.root
        assert sorted(req.add_dirs) == sorted(Path(run.integration[n].worktree) for n in ("alpha", "beta"))
        assert req.schema["properties"]["verdict"]["enum"] == ["APPROVE", "REQUEST_CHANGES", "BLOCKED"]
        assert "Repo alpha:" in req.prompt and "Repo beta:" in req.prompt
        assert "### Repo alpha: branch " in req.prompt and "### Repo beta: branch " in req.prompt
        assert "diff --git a/t1.txt b/t1.txt" in req.prompt and "diff --git a/t2.txt b/t2.txt" in req.prompt
        assert "Files changed:" in req.prompt and "wire the new API into the client" in req.prompt
        assert "{decisions}" not in req.prompt
    assert w.events("review")[-1]["task"] == "integration"
    assert w.orch.phase() == ""


def test_an_integration_request_for_changes_plans_fix_tasks_then_reviews_again(ws, git_workspace):
    w = ws()
    w.team.integration["reviewer_claude"] = [request_changes(), approve()]
    w.team.integration["reviewer_codex"] = [request_changes("also wrong on the wire"), approve()]
    fix = task_spec("T3", "beta", ["T2"], title="Fix the client field name")
    w.team.fix_replies = [[fix]]

    run = w.go()

    assert run.status == "awaiting_models"
    assert w.orch.phase() == "integrating"
    assert [t.id for t in run.tasks] == ["T1", "T2", "T3"]
    assert run.task("T3").repo == "beta" and run.task("T3").status == "pending" and run.task("T3").model is None
    (fix_request,) = w.team.of("integration_fix")
    assert Path(fix_request.cwd) == w.root
    assert fix_request.resume_session == "s-planner"
    assert fix_request.sandbox == "read-only"
    assert "the first new task is `T3`" in fix_request.prompt
    assert "the client reads a field the API does not send" in fix_request.prompt
    assert "also wrong on the wire" in fix_request.prompt
    assert fix_request.schema["properties"]["plan"]["properties"]["tasks"]["items"]["properties"]["repo"]["enum"] == REPO_NAMES
    assert "### T3: Fix the client field name" in w.orch.paths.plan_md.read_text()
    assert w.orch.integration("beta") is not None and Path(w.orch.integration("beta").worktree).exists()

    w.pick_models()
    run = w.orch.run_until_blocked()

    assert run.status == "done"
    assert [t.status for t in run.tasks] == ["merged"] * 3
    branch = f"wf/{run.slug}"
    assert git(git_workspace / "beta", "log", "--format=%s", branch).splitlines() == ["T3: change", "T2: change", "initial"]
    assert len(w.team.of("integration_review")) == 4
    verdicts = [(e["reviewer"], e["verdict"]) for e in w.events("review") if e["task"] == "integration"]
    assert verdicts.count(("claude", "REQUEST_CHANGES")) == 1 and verdicts.count(("claude", "APPROVE")) == 1
    assert w.orch._meta(run.id)["integration"]["round"] == 1
    assert not (w.orch.paths.worktrees_root / run.id).exists()
    summary = w.events("run_done")[-1]["summary"]
    assert any(line.startswith(f"beta: branch {branch}: 2 commits on top of main") for line in summary)


def test_one_reviewer_asking_for_changes_is_enough_to_plan_fixes(ws):
    w = ws()
    w.team.integration["reviewer_codex"] = [request_changes()]
    w.team.fix_replies = [[task_spec("T3", "alpha", ["T1"])]]
    run = w.go()
    assert run.status == "awaiting_models" and run.task("T3").repo == "alpha"


def test_a_fix_plan_that_reuses_an_id_or_names_an_unknown_repo_goes_back_to_the_planner(ws):
    w = ws()
    w.team.integration["reviewer_claude"] = [request_changes()]
    w.team.fix_replies = [
        [task_spec("T2", "beta")],
        [task_spec("T3", "nowhere")],
        [task_spec("T3", "beta", ["T2"])],
    ]
    w.to_models()
    w.pick_models()
    run = w.orch.run_until_blocked()
    assert run.status == "awaiting_models"
    assert run.task("T3").repo == "beta"
    rejected = [e for e in w.events("note") if "rejected" in e["text"]]
    assert len(rejected) == 2 and "not unique" in rejected[0]["text"] and "nowhere" in rejected[1]["text"]
    retry = w.team.of("integration_fix")[1]
    assert "Your previous reply was rejected" in retry.prompt and "not unique" in retry.prompt
    assert not w.events("error")


def test_a_fix_task_with_a_lower_id_is_rejected_and_repeats_become_a_question(ws):
    w = ws()
    w.team.integration["reviewer_claude"] = [request_changes()]
    w.team.fix_replies = [[task_spec("T1", "beta")]] * (w.config.limits.debate_rounds + 1)
    w.to_models()
    w.pick_models()
    run = w.orch.run_until_blocked()
    assert run.status == "awaiting_user"
    assert "wrong repos" in run.questions[0].text and "not unique" in run.questions[0].text
    assert [t.id for t in run.tasks] == ["T1", "T2"]


def test_several_fix_tasks_can_depend_on_each_other(ws):
    w = ws()
    w.team.integration["reviewer_claude"] = [request_changes()]
    w.team.fix_replies = [[task_spec("T3", "beta", ["T2"]), {**task_spec("T4", "beta", ["T3"])}]]
    w.to_models()
    w.pick_models()
    run = w.orch.run_until_blocked()
    assert run.status == "awaiting_models" and [t.id for t in run.tasks] == ["T1", "T2", "T3", "T4"]


def test_fix_tasks_can_reach_a_repo_that_had_no_integration_branch_yet(ws, git_workspace):
    w = ws()
    w.team.integration["reviewer_claude"] = [request_changes()]
    w.team.fix_replies = [[task_spec("T3", "tools/gamma", ["T2"])]]
    run = w.go()
    assert run.status == "awaiting_models"
    assert w.orch.integration("tools/gamma") is None
    assert w.orch.proposed_branches()["tools/gamma"] == f"wf/{run.slug}"
    w.orch.set_branch("tools/gamma", "wf/gamma-fix")
    w.pick_models()
    run = w.orch.run_until_blocked()
    assert run.status == "done"
    assert branch_names(git_workspace / "tools" / "gamma") == ["wf/gamma-fix"]
    assert git(git_workspace / "tools" / "gamma", "show", "wf/gamma-fix:t3.txt") == "T3"


def test_integration_rounds_are_capped_then_the_user_is_asked(ws):
    w = ws()
    limit = w.config.limits.review_rounds
    w.team.integration["reviewer_claude"] = [request_changes() for _ in range(limit + 2)]
    w.team.integration["reviewer_codex"] = [request_changes() for _ in range(limit + 2)]
    ids = [f"T{n}" for n in range(3, 3 + limit)]
    w.team.fix_replies = [[task_spec(tid, "beta", [f"T{int(tid[1:]) - 1}"])] for tid in ids]
    run = w.go()
    for _ in range(limit - 1):
        assert run.status == "awaiting_models"
        w.pick_models()
        run = w.orch.run_until_blocked()
    assert run.status == "awaiting_user"
    (question,) = [q for q in run.questions if q.status == "open"]
    assert "requests changes after 3 rounds" in question.text
    assert set(question.positions) == {"claude", "codex"}
    assert w.orch._meta(run.id)["questions"][question.id]["kind"] == "integration_rounds"
    assert len(w.team.of("integration_fix")) == limit - 1

    w.team.integration["reviewer_claude"] = [approve()]
    w.team.integration["reviewer_codex"] = [approve()]
    w.orch.answer(question.id, "good enough, ship it")
    assert w.orch._meta(run.id)["integration"]["round"] == 0
    assert w.run().status == "running"
    assert w.orch.run_until_blocked().status == "done"


def test_a_blocked_integration_review_asks_at_once(ws):
    w = ws()
    w.team.integration["reviewer_claude"] = [
        {"verdict": "BLOCKED", "summary": "needs a product call", "findings": []}
    ]
    run = w.go()
    assert run.status == "awaiting_user"
    assert w.orch._meta(run.id)["questions"]["Q1"]["kind"] == "integration_blocked"
    assert w.team.of("integration_fix") == []


def test_one_repo_with_one_task_skips_the_integration_review_but_two_tasks_run_it(ws, git_repo, tmp_path, monkeypatch):
    def single(tasks):
        config = config_mod.parse(config_mod.default_toml().replace('backend = "laya"', 'backend = "off"'))
        team = WsTeam(tasks)
        orch = Orchestrator(
            git_repo, config, {"claude": team, "codex": team}, NullDecider(), clock=FakeClock(),
            home=tmp_path / "home", environ={},
        )
        monkeypatch.setattr(orch, "_preflight", lambda: None)
        orch.start("just do it", "auto")
        run = orch.run_until_blocked()
        for task in run.tasks:
            orch.set_models(task.id, *CODER)
        orch.resume()
        return orch, team, orch.run_until_blocked()

    name = git_repo.name
    one, team_one, run_one = single([{**task_spec("T1", name), "repo": name}])
    assert run_one.status == "done" and team_one.of("integration_review") == []
    assert [e for e in EventLog(one.paths).read()[0] if e["kind"] == "review" and e["task"] == "integration"] == []
    assert one.workspace.single is True and run_one.task("T1").repo == name

    git(git_repo, "branch", "-D", f"wf/{run_one.slug}")
    (git_repo / ".workforce" / "state.json").unlink()
    two, team_two, run_two = single([task_spec("T1", name), task_spec("T2", name, ["T1"])])
    assert run_two.status == "done" and len(team_two.of("integration_review")) == 2


def test_single_repo_plans_use_the_static_schema_and_assign_the_only_repo(ws, git_repo, tmp_path, monkeypatch):
    config = config_mod.parse(config_mod.default_toml().replace('backend = "laya"', 'backend = "off"'))
    plain = {k: v for k, v in task_spec("T1", "x").items() if k != "repo"}
    team = WsTeam([plain])
    orch = Orchestrator(
        git_repo, config, {"claude": team, "codex": team}, NullDecider(), clock=FakeClock(),
        home=tmp_path / "home", environ={},
    )
    monkeypatch.setattr(orch, "_preflight", lambda: None)
    orch.start("plain plan", "auto")
    orch.run_until_blocked()
    (plan,) = team.of("plan")
    assert "repo" not in plan.schema["properties"]["tasks"]["items"]["properties"]
    assert plan.skip_git_check is False
    assert Path(plan.cwd) == git_repo.resolve()
    assert orch.load().task("T1").repo == git_repo.name
    assert "Every task belongs to exactly one repo" not in plan.prompt


def test_a_branch_name_clash_is_a_question_and_renaming_recovers(ws, git_workspace):
    w = ws()
    run = w.to_models()
    clash = w.orch.proposed_branches()["beta"]
    git(git_workspace / "beta", "branch", clash)
    for task in w.run().tasks:
        w.orch.set_models(task.id, *CODER)
    w.orch.resume()
    run = w.orch.run_until_blocked()

    assert run.status == "awaiting_user"
    (question,) = [q for q in run.questions if q.status == "open"]
    assert "beta" in question.text and clash in question.text and "already exists" in question.text
    assert w.orch._meta(run.id)["questions"][question.id]["kind"] == "preflight"
    assert run.integration == {}
    assert branch_names(git_workspace / "alpha") == []
    assert w.team.of("coder") == []

    w.orch.answer(question.id, "I will rename it")
    assert w.run().status == "awaiting_models"
    assert w.orch.set_branch("beta", "wf/beta-only") == "wf/beta-only"
    assert w.orch.proposed_branches() == {"alpha": clash, "beta": "wf/beta-only"}
    w.orch.resume()
    run = w.orch.run_until_blocked()

    assert run.status == "done"
    assert run.integration["beta"].branch == "wf/beta-only"
    assert run.integration["alpha"].branch == clash
    assert git(git_workspace / "beta", "show", "wf/beta-only:t2.txt") == "T2"
    assert git(git_workspace / "beta", "log", "--format=%s", clash).splitlines() == ["initial"]
    assert run.task("T2").branch == f"wf/{run.slug}-T2"


def test_a_worktree_path_in_use_is_a_question_too(ws):
    w = ws()
    run = w.to_models()
    blocker = w.orch.paths.integration_worktree(run.id, "alpha")
    blocker.mkdir(parents=True)
    (blocker / "in-the-way.txt").write_text("x")
    w.pick_models()
    run = w.orch.run_until_blocked()
    assert run.status == "awaiting_user"
    assert "worktree path" in run.questions[0].text and "alpha" in run.questions[0].text


def test_set_branch_rules(ws, git_workspace):
    w = ws()
    with pytest.raises(WorkforceError, match="awaiting_models"):
        w.orch.start("goal", "auto")
        w.orch.set_branch("alpha", "wf/x")
    w.orch.run_until_blocked()
    assert w.run().status == "awaiting_models"
    with pytest.raises(WorkforceError, match="no tasks"):
        w.orch.set_branch("tools/gamma", "wf/x")
    with pytest.raises(WorkforceError, match="empty"):
        w.orch.set_branch("alpha", "   ")
    for bad in ("has space", "a..b", "-lead", "end/", "x.lock"):
        with pytest.raises(WorkforceError, match="not a valid"):
            w.orch.set_branch("alpha", bad)
    with pytest.raises(WorkforceError, match="already exists"):
        w.orch.set_branch("alpha", "main")
    assert w.orch.set_branch("alpha", " feature/api-work ") == "feature/api-work"
    assert w.orch.set_branch("alpha", "plain-name") == "plain-name"
    assert w.orch.proposed_branches()["alpha"] == "plain-name"
    w.pick_models()
    assert w.orch.run_until_blocked().status == "done"
    assert branch_names(git_workspace / "alpha") == []
    assert git(git_workspace / "alpha", "branch", "--format=%(refname:short)").split() == ["main", "plain-name"]
    with pytest.raises(WorkforceError, match="awaiting_models"):
        w.orch.set_branch("alpha", "again")


def test_proposed_branches_before_models_are_the_prefix_plus_slug(ws):
    w = ws()
    run = w.to_models()
    slug = run.slug
    assert w.orch.proposed_branches() == {"alpha": f"wf/{slug}", "beta": f"wf/{slug}"}
    assert w.orch.integration("alpha") is None and w.orch.branch_summary() == []


def test_events_carry_the_repo_of_their_task(ws):
    w = ws()
    w.go()
    tagged = {(e["kind"], e["task"], e["repo"]) for e in w.events() if e.get("task") in ("T1", "T2")}
    assert ("merged", "T1", "alpha") in tagged and ("merged", "T2", "beta") in tagged
    assert ("task_status", "T2", "beta") in tagged
    assert all(e["repo"] == ("alpha" if e["task"] == "T1" else "beta") for e in w.events() if e.get("task") in ("T1", "T2"))
    assert not any("repo" in e for e in w.events("review") if e["task"] == "integration")


def test_per_repo_checks_run_in_the_task_worktree_with_the_repos_own_commands(ws):
    w = ws(extra_toml='\n[repos.beta.checks]\ntest = "test -f t2.txt"\n\n[repos.alpha.checks]\ntest = "test -f t1.txt"\n')
    assert w.go().status == "done"
    results = [e for e in w.events("check_result") if e["task"] in ("T1", "T2")]
    assert [(e["repo"], e["ok"]) for e in results] == [("alpha", True), ("beta", True)]
    assert all("test" in e["summary"] for e in results)


def test_a_failing_check_in_one_repo_uses_that_repos_command(ws):
    w = ws(extra_toml='\n[repos.beta.checks]\ntest = "test -f never-made.txt"\n')
    w.to_models()
    w.pick_models()
    run = w.orch.run_until_blocked()
    assert run.task("T1").status == "merged"
    assert run.task("T2").status in ("fixing", "testing", "blocked", "coding")
    failed = [e for e in w.events("check_result") if e["task"] == "T2" and not e["ok"]]
    assert failed and failed[0]["repo"] == "beta"


def test_the_repo_overview_is_capped_and_trimmed_evenly(git_workspace):
    for name in REPO_NAMES:
        repo = git_workspace / name
        for index in range(60):
            (repo / f"module_{index:02d}_with_a_long_name.py").write_text("x = 1\n")
        (repo / "README.md").write_text("\n".join(f"line {i} of the readme for {name}" for i in range(80)))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "more")
    (git_workspace / "beta" / "package.json").write_text("{}")
    git(git_workspace / "beta", "add", "-A")
    git(git_workspace / "beta", "commit", "-q", "-m", "pkg")
    config = config_mod.parse(config_mod.workspace_toml(REPO_NAMES))
    workspace = ws_mod.load_workspace(git_workspace, config)
    text = repo_overview(workspace)
    assert len(text) <= OVERVIEW_MAX_CHARS
    for name in REPO_NAMES:
        assert f"### {name}" in text
    sections = text.split("### ")[1:]
    lengths = [len(section) for section in sections]
    assert max(lengths) - min(lengths) < 400
    assert "JavaScript/TypeScript" in text and "Base: main" in text

    focused = ws_mod.Workspace(git_workspace, workspace.repos, "alpha", False)
    assert "The user started wf inside `alpha`." in repo_overview(focused)


def test_the_repo_overview_of_a_small_repo_is_complete(git_workspace):
    config = config_mod.parse(config_mod.workspace_toml(["alpha"]))
    text = repo_overview(ws_mod.load_workspace(git_workspace, config))
    assert "### alpha" in text and "README.md" in text and "alpha" in text
    assert "(+" not in text


def test_an_old_single_repo_state_file_loads_and_completes(git_repo, tmp_path, monkeypatch):
    config = config_mod.parse(config_mod.default_toml().replace('backend = "laya"', 'backend = "off"'))
    team = WsTeam([{k: v for k, v in task_spec("T1", "x").items() if k != "repo"}])
    orch = Orchestrator(
        git_repo, config, {"claude": team, "codex": team}, NullDecider(), clock=FakeClock(),
        home=tmp_path / "home", environ={},
    )
    monkeypatch.setattr(orch, "_preflight", lambda: None)
    orch.start("finish the old run", "auto")
    run = orch.run_until_blocked()
    orch.set_models("T1", *CODER)
    orch.resume()
    state_path = orch.paths.state
    data = json.loads(state_path.read_text())
    data.pop("slug")
    data.pop("integration")
    for task in data["tasks"]:
        task.pop("repo")
    state_path.write_text(json.dumps(data))
    meta_path = orch.paths.run_dir(run.id) / "orchestrator.json"
    meta = json.loads(meta_path.read_text())
    for key in ("repo_rejects", "branches", "phase", "integration"):
        meta.pop(key)
    meta_path.write_text(json.dumps(meta))

    fresh = Orchestrator(
        git_repo, config, {"claude": team, "codex": team}, NullDecider(), clock=FakeClock(),
        home=tmp_path / "home", environ={},
    )
    monkeypatch.setattr(fresh, "_preflight", lambda: None)
    loaded = fresh.store.load()
    assert loaded.slug is None and loaded.integration == {} and loaded.tasks[0].repo is None
    assert fresh.proposed_branches() == {git_repo.name: f"wf/{fresh._slug(loaded)}"}

    done = fresh.run_until_blocked()

    assert done.status == "done"
    assert done.task("T1").repo == git_repo.name and done.slug
    branch = f"wf/{done.slug}"
    assert git(git_repo, "show", f"{branch}:t1.txt") == "T1"
    assert git(git_repo, "rev-list", "--count", "main") == "1"
    assert git(git_repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert not (git_repo / "t1.txt").exists()


def test_the_run_pauses_instead_of_crashing_when_git_fails_while_preparing(ws, monkeypatch):
    w = ws()
    w.to_models()
    from workforce.errors import GitError

    def boom(*args, **kwargs):
        raise GitError("simulated failure")

    monkeypatch.setattr(git_ops, "create_branch", boom)
    w.pick_models()
    run = w.orch.run_until_blocked()
    assert run.status == "paused" and "simulated failure" in run.pause_reason


def test_preparing_twice_is_idempotent_and_a_half_made_repo_is_finished(ws, git_workspace):
    w = ws()
    run = w.to_models()
    w.pick_models()
    branch = w.orch.proposed_branches()["alpha"]
    base = git(git_workspace / "alpha", "rev-parse", "main")
    git(git_workspace / "alpha", "branch", branch, base)
    from workforce.state import IntegrationInfo

    recorded = IntegrationInfo(
        repo="alpha", base_ref="main", base_sha=base, branch=branch,
        worktree=str(w.orch.paths.integration_worktree(run.id, "alpha")), created=False,
    )
    w.orch.store.update(lambda r: r.integration.__setitem__("alpha", recorded))
    outcome = w.orch.prepare(w.run())
    assert outcome.action == "prepared"
    assert Path(recorded.worktree).exists()
    assert git(Path(recorded.worktree), "symbolic-ref", "--short", "HEAD") == branch
    assert w.run().integration["alpha"].created is True
    assert w.orch.prepare(w.run()) is None
    assert w.orch.run_until_blocked().status == "done"


def test_a_recorded_branch_that_moved_is_a_question(ws, git_workspace):
    w = ws()
    run = w.to_models()
    w.pick_models()
    branch = w.orch.proposed_branches()["alpha"]
    git(git_workspace / "alpha", "branch", branch)
    other = git(git_workspace / "alpha", "commit-tree", "-m", "elsewhere", "main^{tree}", "-p", "main")
    git(git_workspace / "alpha", "update-ref", f"refs/heads/{branch}", other)
    from workforce.state import IntegrationInfo

    recorded = IntegrationInfo(
        repo="alpha", base_ref="main", base_sha=git(git_workspace / "alpha", "rev-parse", "main"), branch=branch,
        worktree=str(w.orch.paths.integration_worktree(run.id, "alpha")), created=False,
    )
    w.orch.store.update(lambda r: r.integration.__setitem__("alpha", recorded))
    run = w.orch.run_until_blocked()
    assert run.status == "awaiting_user"
    assert "does not match" in run.questions[0].text and "alpha" in run.questions[0].text


def test_a_repo_left_out_of_the_plan_is_never_touched(ws, git_workspace):
    w = ws()
    before = checkout_state(git_workspace, ["tools/gamma"])
    assert w.go().status == "done"
    assert checkout_state(git_workspace, ["tools/gamma"]) == before
    assert git(git_workspace / "tools" / "gamma", "worktree", "list", "--porcelain").count("worktree ") == 1


def test_the_hook_root_is_the_workspace_root_for_every_role(ws):
    w = ws()
    w.go()
    for kind, req in w.team.requests:
        assert req.repo == w.root, kind


def test_every_agent_runs_where_it_should(ws, git_workspace):
    w = ws()
    run = w.go()
    for kind in ("plan", "plan_review", "integration_review"):
        assert all(Path(r.cwd) == w.root for r in w.team.of(kind))
    for kind in ("coder", "commit", "reviewer"):
        assert all(Path(r.cwd) != w.root and Path(r.cwd).parent.parent.name == run.id for r in w.team.of(kind))
    (commit_one, commit_two) = w.team.of("commit")
    assert "Repo alpha" not in commit_one.prompt
    assert "the repo `alpha`" in commit_one.prompt and "the repo `beta`" in commit_two.prompt
    assert f"wf/{run.slug}-T1" in commit_one.prompt
    assert all(req.user_setup for req in w.team.of("commit"))


def test_a_user_commit_in_a_checkout_during_the_integration_review_is_not_a_question(ws, git_workspace):
    w = ws()
    original = w.team._integration_review

    once = threading.Lock()
    done = []

    def user_commits_meanwhile(req):
        with once:
            if not done:
                done.append(True)
                git(git_workspace / "alpha", "commit", "-q", "--allow-empty", "-m", "user commit")
        return original(req)

    w.team._integration_review = user_commits_meanwhile
    run = w.go()
    assert run.status == "done" and run.questions == []
    assert git(git_workspace / "alpha", "log", "-1", "--format=%s", "main") == "user commit"
    branch = f"wf/{run.slug}"
    assert git(git_workspace / "alpha", "log", "--format=%s", branch).splitlines() == ["T1: change", "initial"]
