"""The v3 scheduler and merge queue, driven by an in-process fake team and real git worktrees."""

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from workforce import config as config_mod
from workforce import workspace as ws_mod
from workforce import git_ops
from workforce.agents.base import AgentResult
from workforce.decider.base import NullDecider
from workforce.errors import WorkforceError
from workforce.events import EventLog
from workforce.orchestrator import Orchestrator
from workforce.scheduler import MergeQueue, Scheduler
from workforce.state import Task
from tests.test_workspace_e2e import Ws, WsTeam, task_spec

TASK_PATTERN = re.compile(r"## Task (T\d+):")
INTEGRATION_BRANCH = re.compile(r"integration branch `([^`]+)`")
INTEGRATION = "wf/build-things-0115"
WAIT_LIMIT_S = 30


class FakeClock:
    def time(self) -> float:
        return 1_800_000_000.0

    def sleep(self, seconds: float) -> None:
        return None


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class FakeTeam:
    """Stands in for both CLIs. Every call is recorded with monotonic start/end times, thread-safely."""

    def __init__(self, repo: Path, plan_tasks: list[dict] | None = None):
        self.repo = repo
        self.plan_tasks = plan_tasks or []
        self.delays: dict[str, float] = {}
        self.hold_for_file: dict[str, str] = {}
        self.on_coder_start = None
        self.rebase_mode = "ok"
        self.changes_after_rebase: dict[str, str] = {}
        self.rebase_litter: str | None = None
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def run(self, req, on_event=None):
        kind, task = self._classify(req)
        entry = {
            "kind": kind,
            "task": task,
            "role": req.role,
            "cwd": Path(req.cwd),
            "request": req,
            "start": time.monotonic(),
            "end": None,
            "thread": threading.current_thread().name,
        }
        with self._lock:
            self.calls.append(entry)
        result = getattr(self, f"_{kind}")(req, task, entry)
        entry["end"] = time.monotonic()
        return result

    def of(self, kind: str, task: str | None = None) -> list[dict]:
        with self._lock:
            return [c for c in self.calls if c["kind"] == kind and (task is None or c["task"] == task)]

    @staticmethod
    def _classify(req) -> tuple[str, str | None]:
        prompt = req.prompt
        found = TASK_PATTERN.search(prompt)
        task = found.group(1) if found else None
        if "# Role: side question" in prompt:
            return "side", None
        if "# Role: plan reviewer" in prompt:
            return "plan_review", None
        if "# Role: planner" in prompt:
            return "plan", None
        if "# Role: committer (rebase)" in prompt:
            return "rebase", task
        if "# Role: committer" in prompt:
            return "commit", task
        if req.role.startswith("reviewer_"):
            return "reviewer", task
        if req.role == "coder":
            return "coder", task
        return "other", task

    def _ok(self, req, text="", structured=None) -> AgentResult:
        return AgentResult(
            ok=True, text=text, structured=structured, session_id=f"s-{req.role}", model=req.model
        )

    def _plan(self, req, task, entry):
        return self._ok(req, structured={"tasks": self.plan_tasks, "risks": [], "open_questions": []})

    def _plan_review(self, req, task, entry):
        return self._ok(req, structured={"agree": True, "objections": [], "suggested_changes": []})

    def _side(self, req, task, entry):
        return self._ok(req, text=f"side answer from {req.agent}")

    def _other(self, req, task, entry):
        return self._ok(req, text="ok")

    def _on_integration(self, name: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "cat-file", "-e", f"{INTEGRATION}:{name}"], capture_output=True, check=False
        )
        return proc.returncode == 0

    def _coder(self, req, task, entry):
        entry["saw"] = sorted(p.name for p in Path(req.cwd).iterdir() if p.is_file())
        if self.on_coder_start:
            self.on_coder_start(task)
        wanted = self.hold_for_file.get(task)
        deadline = time.monotonic() + WAIT_LIMIT_S
        while wanted and not self._on_integration(wanted):
            assert time.monotonic() < deadline, f"{task} waited too long for {wanted} on the integration branch"
            time.sleep(0.02)
        time.sleep(self.delays.get(task, 0.05))
        fixing = "# Role: coder (fixing)" in req.prompt
        name = f"{task.lower()}b.txt" if fixing else f"{task.lower()}.txt"
        (Path(req.cwd) / name).write_text(f"{task}\n")
        return self._ok(req, text=f"Created {name}")

    def _reviewer(self, req, task, entry):
        entry["saw"] = sorted(p.name for p in Path(req.cwd).iterdir() if p.is_file())
        time.sleep(0.02)
        wanted = self.changes_after_rebase.get(task)
        if wanted and "t1.txt" in entry["saw"] and wanted not in entry["saw"]:
            finding = {"severity": "major", "file": "t2.txt", "line": None, "message": f"add {wanted}"}
            return self._ok(req, structured={"verdict": "REQUEST_CHANGES", "summary": "needs more", "findings": [finding]})
        return self._ok(req, structured={"verdict": "APPROVE", "summary": f"{task} is fine", "findings": []})

    def _commit(self, req, task, entry):
        time.sleep(0.05)
        git(req.cwd, "add", "-A")
        git(req.cwd, "commit", "-q", "-m", f"{task}: change")
        sha = git(req.cwd, "rev-parse", "HEAD")
        return self._ok(req, structured={"committed": True, "sha": sha, "message": "m", "error": None})

    def _rebase(self, req, task, entry):
        time.sleep(0.05)
        if self.rebase_mode == "ok":
            git(req.cwd, "rebase", INTEGRATION_BRANCH.search(req.prompt).group(1))
        if self.rebase_litter:
            litter = Path(req.cwd) / self.rebase_litter
            litter.parent.mkdir(parents=True, exist_ok=True)
            litter.write_text("{}")
        sha = git(req.cwd, "rev-parse", "HEAD")
        return self._ok(req, structured={"committed": True, "sha": sha, "message": "rebased", "error": None})


def config_with_checks(test_cmd: str = "") -> config_mod.Config:
    text = config_mod.default_toml().replace('backend = "laya"', 'backend = "off"')
    if test_cmd:
        text = text.replace('test = ""', f'test = "{test_cmd}"')
    return config_mod.parse(text)


def make_orch(tmp_path, repo, team, monkeypatch, *, test_cmd="", usage_gate=None) -> Orchestrator:
    orch = Orchestrator(
        repo,
        config_with_checks(test_cmd),
        {"claude": team, "codex": team},
        NullDecider(),
        clock=FakeClock(),
        home=tmp_path / "home",
        usage_gate=usage_gate,
        environ={},
    )
    monkeypatch.setattr(orch, "_preflight", lambda: None)
    return orch


def seed(orch: Orchestrator, specs: list[tuple[str, list[str]]], mode: str = "auto") -> None:
    orch.start("build things", mode)

    def fn(run):
        run.tasks = [
            Task(
                id=task_id,
                title=f"Add {task_id.lower()}",
                description=f"Create {task_id.lower()}.txt",
                acceptance=["the file exists"],
                depends_on=deps,
                model="claude-sonnet-5-5",
                effort="high",
                agent="claude",
            )
            for task_id, deps in specs
        ]
        run.status = "running"

    orch.store.update(fn)


def events_of(orch: Orchestrator, kind: str) -> list[dict]:
    events, _ = EventLog(orch.paths).read()
    return [e for e in events if e["kind"] == kind]


def overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


@pytest.fixture
def env(tmp_path, git_repo, monkeypatch):
    class Env:
        pass

    e = Env()
    e.tmp_path, e.repo, e.monkeypatch = tmp_path, git_repo, monkeypatch
    e.team = FakeTeam(git_repo)

    def build(specs, *, mode="auto", parallel=2, **kwargs):
        e.orch = make_orch(tmp_path, git_repo, e.team, monkeypatch, **kwargs)
        seed(e.orch, specs, mode)
        e.scheduler = Scheduler(e.orch, parallel)
        return e

    e.build = build
    return e


def main_files(repo: Path) -> list[str]:
    return sorted(git(repo, "ls-tree", "--name-only", INTEGRATION).splitlines())


def test_parallel_must_be_at_least_one(env):
    env.build([("T1", [])])
    with pytest.raises(WorkforceError, match="parallel"):
        Scheduler(env.orch, 0)


def test_two_independent_tasks_run_concurrently(env):
    env.team.delays = {"T1": 0.5, "T2": 0.5}
    env.build([("T1", []), ("T2", [])])
    run = env.scheduler.run_until_blocked()
    assert run.status == "done"
    assert {t.status for t in run.tasks} == {"merged"}
    (c1,) = env.team.of("coder", "T1")
    (c2,) = env.team.of("coder", "T2")
    assert overlaps(c1, c2)
    assert c1["thread"] != c2["thread"]
    assert c1["cwd"] != c2["cwd"]
    assert {"t1.txt", "t2.txt"} <= set(main_files(env.repo))


def test_commits_never_overlap_so_touch_id_is_one_at_a_time(env):
    env.team.delays = {"T1": 0.2, "T2": 0.2}
    env.build([("T1", []), ("T2", [])])
    env.scheduler.run_until_blocked()
    commits = env.team.of("commit") + env.team.of("rebase")
    assert len(commits) >= 2
    for first in commits:
        for second in commits:
            if first is not second:
                assert not overlaps(first, second)


def test_parallel_one_runs_tasks_one_at_a_time(env):
    env.team.delays = {"T1": 0.3, "T2": 0.3}
    env.build([("T1", []), ("T2", [])], parallel=1)
    run = env.scheduler.run_until_blocked()
    assert run.status == "done"
    (c1,) = env.team.of("coder", "T1")
    (c2,) = env.team.of("coder", "T2")
    assert not overlaps(c1, c2)


def test_dependencies_start_only_after_their_parents_merged(env):
    env.team.delays = {"T1": 0.3, "T2": 0.1}
    env.build([("T1", []), ("T2", []), ("T3", ["T1"]), ("T4", ["T2", "T3"])])
    run = env.scheduler.run_until_blocked()
    assert run.status == "done"
    (t1_commit,) = env.team.of("commit", "T1")
    (t3_code,) = env.team.of("coder", "T3")
    assert t3_code["start"] > t1_commit["end"]
    assert "t1.txt" in t3_code["saw"]
    (t4_code,) = env.team.of("coder", "T4")
    assert {"t1.txt", "t2.txt", "t3.txt"} <= set(t4_code["saw"])
    merged_order = [e["task"] for e in events_of(env.orch, "merged")]
    assert merged_order.index("T1") < merged_order.index("T3") < merged_order.index("T4")
    assert merged_order.index("T2") < merged_order.index("T4")


def test_a_second_task_is_not_started_while_its_parent_is_still_running(env):
    env.team.delays = {"T1": 0.4}
    env.build([("T1", []), ("T2", ["T1"])])
    env.scheduler.run_until_blocked()
    (c1,) = env.team.of("coder", "T1")
    (c2,) = env.team.of("coder", "T2")
    assert not overlaps(c1, c2)


def test_merge_queue_rebase_voids_approvals_and_reviews_again(env):
    env.team.hold_for_file = {"T2": "t1.txt"}
    env.build([("T1", []), ("T2", [])])
    run = env.scheduler.run_until_blocked()
    assert run.status == "done"

    reviews_t2 = env.team.of("reviewer", "T2")
    assert len(reviews_t2) == 4
    first_pass, second_pass = reviews_t2[:2], reviews_t2[2:]
    assert all("t1.txt" not in call["saw"] for call in first_pass)
    assert all("t1.txt" in call["saw"] for call in second_pass)
    assert max(c["end"] for c in first_pass) < env.team.of("rebase", "T2")[0]["start"]
    assert env.team.of("rebase", "T2")[0]["end"] < min(c["start"] for c in second_pass)
    assert len(env.team.of("commit", "T2")) == 1
    assert len(env.team.of("rebase", "T2")) == 1
    assert env.team.of("rebase", "T1") == []
    assert len(env.team.of("reviewer", "T1")) == 2

    rebase_request = env.team.of("rebase", "T2")[0]["request"]
    assert rebase_request.user_setup is True
    assert rebase_request.role == "committer"
    assert rebase_request.schema is not None

    t2 = run.task("T2")
    main_sha_after_t1 = git(env.repo, "log", "--format=%H", "--grep=T1: change", INTEGRATION)
    assert len(t2.reviews) == 2
    assert {r.base_sha for r in t2.reviews} == {main_sha_after_t1}
    assert {r.sha for r in t2.reviews} == {git(env.repo, "rev-parse", f"{INTEGRATION}^{{tree}}")}
    assert t2.base_sha == main_sha_after_t1

    log = git(env.repo, "log", "--format=%s", INTEGRATION).splitlines()
    assert log == ["T2: change", "T1: change", "initial"]
    assert git(env.repo, "log", "--format=%s", "main").splitlines() == ["initial"]
    notes = [e["text"] for e in events_of(env.orch, "note") if e.get("task") == "T2"]
    assert any("rebased" in text and "void" in text for text in notes)
    assert len([e for e in events_of(env.orch, "review") if e["task"] == "T2"]) == 4
    assert not any(env.team.of("commit", "T2")[0]["end"] > c["start"] for c in env.team.of("rebase", "T1"))


def test_changes_requested_after_a_rebase_attach_approvals_to_the_final_tree(env):
    env.team.hold_for_file = {"T2": "t1.txt"}
    env.team.changes_after_rebase = {"T2": "t2b.txt"}
    env.build([("T1", []), ("T2", [])])
    run = env.scheduler.run_until_blocked()
    assert run.status == "done"

    assert len(env.team.of("rebase", "T2")) == 1
    assert len(env.team.of("coder", "T2")) == 2
    assert len(env.team.of("commit", "T2")) == 2
    assert git(env.repo, "log", "--format=%s", INTEGRATION).splitlines() == [
        "T2: change",
        "T2: change",
        "T1: change",
        "initial",
    ]

    t2 = run.task("T2")
    main_tree = git(env.repo, "rev-parse", f"{INTEGRATION}^{{tree}}")
    t1_sha = git(env.repo, "log", "--format=%H", "--grep=T1: change", INTEGRATION)
    latest = {r.reviewer: r for r in t2.reviews}
    assert set(latest) == {"claude", "codex"}
    assert {r.verdict for r in latest.values()} == {"APPROVE"}
    assert {r.sha for r in latest.values()} == {main_tree} == {t2.head_sha}
    assert {r.base_sha for r in latest.values()} == {t1_sha} == {t2.base_sha}
    assert not git_ops.branch_exists(env.repo, t2.branch)
    assert "t2b.txt" in main_files(env.repo)
    assert any(r.verdict == "REQUEST_CHANGES" for r in t2.reviews)


def test_approval_problem_covers_missing_stale_and_branch_mismatch(env):
    from workforce.state import Review

    env.build([("T1", [])])
    tree = git(env.repo, "rev-parse", "HEAD^{tree}")

    def review(reviewer, verdict="APPROVE", sha=tree, base="b"):
        return Review(reviewer=reviewer, model="m", session=None, sha=sha, base_sha=base, verdict=verdict)

    def task(reviews, head=tree):
        return Task(id="T1", title="t", description="d", worktree=str(env.repo), head_sha=head, base_sha="b", reviews=reviews)

    both = [review("claude"), review("codex")]
    assert env.scheduler._approval_problem(task(both)) is None
    assert "both reviewers" in env.scheduler._approval_problem(task([review("claude")]))
    assert "codex approval" in env.scheduler._approval_problem(task([review("claude"), review("codex", "REQUEST_CHANGES")]))
    assert "codex approval" in env.scheduler._approval_problem(task([review("claude"), review("codex", sha="0" * 40)]))
    assert "codex approval" in env.scheduler._approval_problem(task([review("claude"), review("codex", base="old")]))
    assert "approval is not for the current tree" in env.scheduler._approval_problem(task(both, head="0" * 40))
    other = "1" * 40
    matching_other = [review("claude", sha=other), review("codex", sha=other)]
    assert "branch HEAD tree" in env.scheduler._approval_problem(task(matching_other, head=other))


def _t1_sha(env) -> str:
    return git(env.repo, "log", "--format=%H", "--grep=T1: change", INTEGRATION)


def test_a_question_after_a_successful_rebase_resumes_into_recheck_not_a_second_commit(env, monkeypatch):
    monkeypatch.setattr("workforce.git_ops.run_excluding_new_untracked", lambda worktree, fn: (fn(), []))
    env.team.hold_for_file = {"T2": "t1.txt"}
    env.team.rebase_litter = ".claude-flow/state.json"
    env.build([("T1", []), ("T2", [])])
    run = env.scheduler.run_until_blocked()

    assert run.status == "awaiting_user"
    assert run.task("T1").status == "merged" and run.task("T2").status == "blocked"
    (question,) = [q for q in run.questions if q.status == "open"]
    assert question.task_id == "T2" and "uncommitted changes after the rebase" in question.text
    assert len(env.team.of("commit", "T2")) == 1
    assert len(env.team.of("rebase", "T2")) == 1
    assert len(env.team.of("reviewer", "T2")) == 2

    shutil.rmtree(Path(run.task("T2").worktree) / ".claude-flow")
    env.orch.answer(question.id, "cleaned it up")
    assert env.orch.store.load().status == "running"
    run = env.scheduler.run_until_blocked()

    assert run.status == "done"
    assert len(env.team.of("commit", "T2")) == 1
    assert len(env.team.of("rebase", "T2")) == 1
    reviews = env.team.of("reviewer", "T2")
    assert len(reviews) == 4
    rebase_end = env.team.of("rebase", "T2")[0]["end"]
    assert all(call["start"] > rebase_end for call in reviews[2:])
    assert all("t1.txt" in call["saw"] for call in reviews[2:])
    notes = [e["text"] for e in events_of(env.orch, "note") if e.get("task") == "T2"]
    assert any("already rebased" in text for text in notes)

    main_tree = git(env.repo, "rev-parse", f"{INTEGRATION}^{{tree}}")
    latest = {r.reviewer: r for r in run.task("T2").reviews}
    assert {r.verdict for r in latest.values()} == {"APPROVE"}
    assert {r.sha for r in latest.values()} == {main_tree}
    assert {r.base_sha for r in latest.values()} == {_t1_sha(env)}
    assert git(env.repo, "log", "--format=%s", INTEGRATION).splitlines() == ["T2: change", "T1: change", "initial"]


def test_litter_from_the_committer_rebase_is_excluded_so_no_question_is_asked(env):
    env.team.hold_for_file = {"T2": "t1.txt"}
    env.team.rebase_litter = ".claude-flow/state.json"
    env.build([("T1", []), ("T2", [])])
    run = env.scheduler.run_until_blocked()

    assert run.status == "done"
    assert run.questions == []
    assert len(env.team.of("commit", "T2")) == 1
    assert len(env.team.of("reviewer", "T2")) == 4
    assert not any(".claude-flow" in call["request"].prompt for call in env.team.of("reviewer"))
    notes = [e["text"] for e in events_of(env.orch, "note") if e.get("task") == "T2"]
    assert any("excluded files created by your Claude setup" in text and ".claude-flow" in text for text in notes)
    assert ".claude-flow" not in git(env.repo, "ls-tree", "-r", "--name-only", INTEGRATION)


def test_rebased_since_approval_is_false_in_the_normal_states(env):
    env.build([("T1", [])])
    run = env.scheduler.run_until_blocked()
    assert run.status == "done"
    finished = run.task("T1")
    assert env.scheduler._rebased_since_approval(finished) is False
    assert env.scheduler._rebased_since_approval(Task(id="T9", title="t", description="d")) is False


def test_rebase_that_does_not_rebase_asks_the_user(env):
    env.team.hold_for_file = {"T2": "t1.txt"}
    env.team.rebase_mode = "noop"
    env.build([("T1", []), ("T2", [])])
    run = env.scheduler.run_until_blocked()
    assert run.status == "awaiting_user"
    assert run.task("T1").status == "merged"
    assert run.task("T2").status == "blocked"
    (question,) = [q for q in run.questions if q.status == "open"]
    assert question.task_id == "T2"
    assert "could not be rebased" in question.text
    assert len(env.team.of("reviewer", "T2")) == 2


def test_failing_checks_on_main_after_a_merge_stop_the_queue_and_ask(env):
    marker = env.tmp_path / "break-main"
    marker.write_text("x")
    script = env.tmp_path / "check.sh"
    script.write_text(
        f'if [ "$(basename "$PWD")" = "_integration" ] && [ -f {marker} ] && [ -f t1.txt ]; then echo main is broken; exit 1; fi\nexit 0\n'
    )
    env.team.hold_for_file = {"T2": "t1.txt"}
    env.team.delays = {"T1": 0.05, "T2": 0.8}
    env.build([("T1", []), ("T2", [])], test_cmd=f"sh {script}")
    run = env.scheduler.run_until_blocked()

    assert run.status == "awaiting_user"
    assert run.task("T1").status == "merged"
    assert run.task("T2").status != "merged"
    assert len(events_of(env.orch, "merged")) == 1
    main_checks = [e for e in events_of(env.orch, "check_result") if e["task"] == "integration"]
    assert len(main_checks) == 1 and main_checks[0]["ok"] is False
    (question,) = [q for q in run.questions if q.status == "open"]
    assert "checks on the integration branch" in question.text
    assert "main is broken" in question.positions["checks"]
    sidecar = json.loads((env.orch.paths.run_dir(run.id) / "orchestrator.json").read_text())
    assert sidecar["questions"][question.id]["kind"] == "main_checks_failed"
    assert len(env.team.of("reviewer", "T2")) == 0

    marker.unlink()
    env.orch.answer(question.id, "fixed main")
    assert env.orch.store.load().status == "running"
    run = env.scheduler.run_until_blocked()
    assert run.status == "done"
    assert {t.status for t in run.tasks} == {"merged"}


def test_usage_gate_pause_mid_parallel_lets_running_steps_finish_then_stops(env):
    flag = threading.Event()
    both_started = {"T1": threading.Event(), "T2": threading.Event()}

    def on_start(task):
        both_started[task].set()
        if task == "T2":
            assert both_started["T1"].wait(WAIT_LIMIT_S)
            flag.set()

    env.team.on_coder_start = on_start
    env.team.delays = {"T1": 0.3, "T2": 0.3}
    reason = "usage: claude five_hour 61% ≥ 60%"
    env.build([("T1", []), ("T2", [])], usage_gate=lambda: reason if flag.is_set() else None)
    run = env.scheduler.run_until_blocked()

    assert run.status == "paused"
    assert run.pause_reason == reason
    assert len(env.team.of("coder", "T1")) == 1 and len(env.team.of("coder", "T2")) == 1
    assert (env.team.of("coder", "T1")[0]["end"] is not None) and (env.team.of("coder", "T2")[0]["end"] is not None)
    assert env.team.of("reviewer") == []
    assert {run.task("T1").status, run.task("T2").status} <= {"testing", "reviewing"}
    assert any(e["reason"] == reason for e in events_of(env.orch, "paused"))

    flag.clear()
    env.orch.resume()
    run = env.scheduler.run_until_blocked()
    assert run.status == "done"
    assert len(env.team.of("coder")) == 2
    assert len(env.team.of("reviewer")) >= 4


def test_gate_is_checked_before_a_task_is_launched(env):
    env.build([("T1", [])], usage_gate=lambda: "usage: codex seven_day 70% ≥ 60%")
    run = env.scheduler.run_until_blocked()
    assert run.status == "paused"
    assert env.team.calls == []
    assert run.task("T1").status == "pending"


def test_step_mode_asks_before_each_task_one_question_at_a_time(env):
    env.build([("T1", []), ("T2", [])], mode="step")
    seen_questions = []
    for _ in range(6):
        run = env.scheduler.run_until_blocked()
        if run.status == "done":
            break
        assert run.status == "awaiting_user"
        for question in [q for q in run.questions if q.status == "open"]:
            seen_questions.append(question.id)
            env.orch.answer(question.id, "yes")
    assert env.orch.store.load().status == "done"
    assert seen_questions == ["Q1", "Q2"]


def test_a_crashing_worker_pauses_the_run_and_reports_it(env):
    def boom(task):
        raise RuntimeError("coder exploded")

    env.team.on_coder_start = boom
    env.build([("T1", [])])
    run = env.scheduler.run_until_blocked()
    assert run.status == "paused"
    assert "internal error in T1" in run.pause_reason
    assert any("coder exploded" in e["message"] for e in events_of(env.orch, "error"))


def test_a_second_scheduler_cannot_take_the_runner_lock(env):
    env.build([("T1", [])])
    with env.orch.store.lock():
        with pytest.raises(Exception, match="another workforce process"):
            env.scheduler.run_until_blocked()


def test_plan_and_debate_run_sequentially_before_the_parallel_phase(env):
    tasks = [
        {
            "id": "T1",
            "title": "Add t1",
            "description": "Create t1.txt",
            "acceptance": ["exists"],
            "depends_on": [],
        }
    ]
    env.team.plan_tasks = tasks
    orch = make_orch(env.tmp_path, env.repo, env.team, env.monkeypatch)
    orch.start("goal", "auto")
    run = Scheduler(orch, 2).run_until_blocked()
    assert run.status == "awaiting_models"
    assert [t.id for t in run.tasks] == ["T1"]
    assert len(env.team.of("plan")) == 1
    assert env.team.of("coder") == []


class TestMergeQueue:
    def test_grants_in_join_order_one_at_a_time(self):
        queue = MergeQueue(poll_s=0.01)
        for task in ("A", "B", "C"):
            queue.join(task)
        granted: list[str] = []
        inside = []
        overlap_seen = []

        def worker(task: str):
            assert queue.acquire(task, lambda: True)
            inside.append(task)
            if len(inside) > 1:
                overlap_seen.append(task)
            granted.append(task)
            time.sleep(0.05)
            inside.remove(task)
            queue.release(task)

        threads = [threading.Thread(target=worker, args=(t,)) for t in ("C", "A", "B")]
        for thread in threads:
            thread.start()
            time.sleep(0.02)
        for thread in threads:
            thread.join(5)
        assert granted == ["A", "B", "C"]
        assert overlap_seen == []
        assert queue.order() == []

    def test_a_waiter_gives_up_its_place_when_told_to_stop(self):
        queue = MergeQueue(poll_s=0.01)
        queue.join("A")
        queue.join("B")
        stop = threading.Event()
        result = []

        def waiter():
            result.append(queue.acquire("B", lambda: not stop.is_set()))

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        stop.set()
        thread.join(5)
        assert result == [False]
        assert queue.order() == ["A"]

    def test_join_is_idempotent(self):
        queue = MergeQueue(poll_s=0.01)
        queue.join("A")
        queue.join("A")
        assert queue.order() == ["A"]


def test_a_litter_excluded_file_never_appears_in_a_snapshot_or_diff(git_repo, tmp_path):
    from workforce import git_ops

    worktree = tmp_path / "wt"
    base = git_ops.create_worktree(git_repo, worktree, "wf/r/T1")
    (worktree / "work.txt").write_text("real work\n")

    def committer_run():
        (worktree / ".claude-flow").mkdir()
        (worktree / ".claude-flow" / "state.json").write_text("{}")
        (worktree / "stray.log").write_text("noise\n")

    _, added = git_ops.run_excluding_new_untracked(worktree, committer_run)
    assert set(added) == {"/.claude-flow/", "/stray.log"}

    assert "work.txt" in git_ops.diff_against(worktree, base)
    diff = git_ops.diff_against(worktree, base)
    assert ".claude-flow" not in diff and "stray.log" not in diff
    assert git_ops.changed_files(worktree, base) == ["work.txt"]
    tree_files = git(worktree, "ls-tree", "-r", "--name-only", git_ops.tree_hash(worktree)).splitlines()
    assert sorted(tree_files) == ["README.md", "work.txt"]
    assert ".claude-flow" not in git_ops.diff_stat(worktree, base)


class TimedWsTeam(WsTeam):
    """A `WsTeam` that records when every call ran and can slow coders and committers down per task."""

    def __init__(self, plan_tasks):
        super().__init__(plan_tasks)
        self.coder_delays: dict[str, float] = {}
        self.commit_delays: dict[str, float] = {}
        self.timings: list[dict] = []

    def run(self, req, on_event=None):
        kind = self._kind(req)
        found = TASK_PATTERN.search(req.prompt)
        task = found.group(1) if found else None
        entry = {"kind": kind, "task": task, "start": time.monotonic(), "thread": threading.current_thread().name}
        if kind == "coder":
            time.sleep(self.coder_delays.get(task, 0.05))
        if kind == "commit":
            time.sleep(self.commit_delays.get(task, 0.05))
        result = super().run(req, on_event)
        entry["end"] = time.monotonic()
        with self._lock:
            self.timings.append(entry)
        return result

    def timing(self, kind: str, task: str) -> dict:
        (entry,) = [t for t in self.timings if t["kind"] == kind and t["task"] == task]
        return entry


@pytest.fixture
def workspace_env(tmp_path, git_workspace, monkeypatch):
    def build(plan_tasks, *, parallel=2):
        w = Ws(git_workspace, tmp_path / "home", monkeypatch, plan_tasks=plan_tasks)
        w.team = TimedWsTeam(plan_tasks)
        w.orch = w.new_orchestrator()
        w.to_models()
        w.pick_models()
        w.scheduler = Scheduler(w.orch, parallel)
        return w

    return build


def test_tasks_in_two_repos_run_in_parallel_through_separate_merge_queues(workspace_env):
    w = workspace_env([task_spec("T1", "alpha"), task_spec("T2", "beta")])
    w.team.coder_delays = {"T1": 0.5, "T2": 0.5}
    run = w.scheduler.run_until_blocked()
    assert run.status == "done"
    assert [t.status for t in run.tasks] == ["merged", "merged"]
    assert overlaps(w.team.timing("coder", "T1"), w.team.timing("coder", "T2"))
    assert w.team.timing("coder", "T1")["thread"] != w.team.timing("coder", "T2")["thread"]
    assert set(w.scheduler.queues) == {"alpha", "beta"}
    assert w.scheduler.queue_for("alpha") is not w.scheduler.queue_for("beta")
    assert w.scheduler.queue_for("alpha") is w.scheduler.queue_for("alpha")
    assert len(w.team.of("integration_review")) == 2


def test_one_repos_merge_queue_does_not_block_another_repos(workspace_env):
    w = workspace_env([task_spec("T1", "alpha"), task_spec("T2", "beta")])
    alpha, beta = w.scheduler.queue_for("alpha"), w.scheduler.queue_for("beta")
    alpha.join("A1")
    assert alpha.acquire("A1", lambda: True)
    beta.join("B1")
    assert beta.acquire("B1", lambda: True)
    alpha.join("A2")
    assert alpha.order() == ["A1", "A2"] and beta.order() == ["B1"]
    alpha.release("A1")
    beta.release("B1")


def test_commits_and_rebases_never_overlap_across_all_repos_and_everything_still_merges(workspace_env):
    w = workspace_env([task_spec("T1", "alpha"), task_spec("T2", "alpha"), task_spec("T3", "beta")], parallel=3)
    w.team.commit_delays = {"T1": 0.4, "T2": 0.4, "T3": 0.4}
    run = w.scheduler.run_until_blocked()
    assert run.status == "done"
    assert [t.status for t in run.tasks] == ["merged"] * 3
    committers = [t for t in w.team.timings if t["kind"] in ("commit", "rebase")]
    assert len([t for t in committers if t["kind"] == "commit"]) == 3
    for first in committers:
        for second in committers:
            if first is not second:
                assert not overlaps(first, second), (first["task"], second["task"])


def test_two_repos_with_tasks_approved_at_the_same_moment_commit_one_after_the_other(workspace_env):
    w = workspace_env([task_spec("T1", "alpha"), task_spec("T2", "beta")])
    w.team.coder_delays = {"T1": 0.2, "T2": 0.2}
    w.team.commit_delays = {"T1": 0.5, "T2": 0.5}
    run = w.scheduler.run_until_blocked()
    assert run.status == "done"
    first, second = sorted((w.team.timing("commit", t) for t in ("T1", "T2")), key=lambda t: t["start"])
    assert first["end"] <= second["start"]
    assert sorted(e["repo"] for e in events_of(w.orch, "merged")) == ["alpha", "beta"]
    assert len(w.team.of("commit")) == 2


def test_a_cross_repo_dependency_waits_for_the_merge_in_the_other_repo(workspace_env):
    w = workspace_env([task_spec("T1", "alpha"), task_spec("T2", "beta", ["T1"])])
    w.team.coder_delays = {"T1": 0.3}
    run = w.scheduler.run_until_blocked()
    assert run.status == "done"
    assert w.team.timing("coder", "T2")["start"] >= w.team.timing("commit", "T1")["end"]
    merged = [e["task"] for e in events_of(w.orch, "merged")]
    assert merged == ["T1", "T2"]
    alpha_integration = Path(run.integration["alpha"].worktree)
    assert w.team.of("coder")[1].add_dirs == [alpha_integration]


def test_post_merge_checks_run_in_each_repos_integration_worktree_with_its_own_commands(workspace_env, tmp_path):
    w = workspace_env([task_spec("T1", "alpha"), task_spec("T2", "beta")])
    marker = tmp_path / "beta-broken"
    marker.write_text("x")
    script = tmp_path / "beta-check.sh"
    script.write_text(f'if [ -f {marker} ] && [ "$(basename "$PWD")" = "_integration" ]; then echo beta is broken; exit 1; fi\nexit 0\n')
    w.orch.config = config_mod.parse(
        config_mod.workspace_toml(["alpha", "beta", "tools/gamma"]).replace('backend = "laya"', 'backend = "off"')
        + f'\n[repos.beta.checks]\ntest = "sh {script}"\n'
    )
    w.orch.workspace = ws_mod.load_workspace(w.root, w.orch.config)
    run = w.scheduler.run_until_blocked()
    assert run.status == "awaiting_user"
    (question,) = [q for q in run.questions if q.status == "open"]
    assert "beta" in question.text and "integration branch" in question.text
    assert "beta is broken" in question.positions["checks"]
    checks_events = [(e["repo"], e["ok"]) for e in events_of(w.orch, "check_result") if e["task"] == "integration"]
    assert ("beta", False) in checks_events and ("beta", True) not in checks_events
    assert all(ok for repo, ok in checks_events if repo == "alpha")
