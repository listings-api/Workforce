import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from workforce import state as state_mod
from workforce.errors import StateLockedError
from workforce.paths import Paths
from workforce.state import Finding, Question, Review, Run, StateError, StateStore, Task


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "repo", home=tmp_path / "home")


@pytest.fixture
def store(paths: Paths) -> StateStore:
    return StateStore(paths)


def full_run() -> Run:
    run = Run.create("r1", "add csv export", mode="step")
    run.status = "running"
    run.pause_reason = "Claude 5-hour window at 61%"
    review = Review(
        reviewer="codex",
        model="gpt-6-astra",
        session="thread-1",
        sha="a" * 40,
        base_sha="b" * 40,
        verdict="REQUEST_CHANGES",
        findings=[
            Finding("major", "a.py", 12, "bug"),
            Finding("nit", "b.py", None, "style"),
        ],
        at="2026-09-29T10:00:00+00:00",
    )
    run.tasks = [
        Task(
            id="T1",
            title="one",
            description="do one",
            acceptance=["works", "tested"],
            depends_on=[],
            model="claude-sonnet-5-5",
            effort="high",
            agent="claude",
            status="reviewing",
            branch="wf/r1/T1",
            worktree="/x/.workforce/worktrees/T1",
            base_sha="b" * 40,
            head_sha="c" * 40,
            coder_session="sess-1",
            review_round=2,
            reviews=[review],
            infra_failures=1,
            computer_use=True,
        ),
        Task(id="T2", title="two", description="do two", depends_on=["T1"]),
    ]
    run.questions = [
        Question(id="Q1", task_id="T1", text="which?", positions={"claude": "a", "codex": "b"}),
        Question(id="Q2", task_id=None, text="done?", answer="yes", status="answered"),
    ]
    return run


def test_load_without_file_is_none(store: StateStore):
    assert store.load() is None


def test_round_trip_is_lossless(store: StateStore):
    run = full_run()
    store.save(run)
    loaded = store.load()
    assert loaded == run
    assert asdict(loaded) == asdict(run)
    assert isinstance(loaded.tasks[0].reviews[0].findings[0], Finding)
    assert loaded.tasks[0].reviews[0].findings[1].line is None
    store.save(loaded)
    assert json.loads(store.paths.state.read_text()) == asdict(run)


def test_defaults_for_new_task_and_run():
    task = Task(id="T9", title="t", description="d")
    assert task.status == "pending"
    assert task.review_round == 0 and task.infra_failures == 0 and task.computer_use is False
    run = Run.create("r", "g")
    assert run.status == "planning" and run.mode == "auto" and run.tasks == [] and run.pause_reason is None
    assert run.created_at


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d.__setitem__("status", "flying"), "Run.status 'flying'"),
        (lambda d: d.__setitem__("mode", "turbo"), "Run.mode 'turbo'"),
        (lambda d: d["tasks"][0].__setitem__("status", "nope"), "Task.status 'nope'"),
        (lambda d: d["questions"][0].__setitem__("status", "maybe"), "Question.status 'maybe'"),
        (lambda d: d["tasks"][0]["reviews"][0].__setitem__("verdict", "LGTM"), "Review.verdict 'LGTM'"),
        (lambda d: d["tasks"][0]["reviews"][0].__setitem__("reviewer", "gemini"), "Review.reviewer 'gemini'"),
        (
            lambda d: d["tasks"][0]["reviews"][0]["findings"][0].__setitem__("severity", "huge"),
            "Finding.severity 'huge'",
        ),
        (lambda d: d.pop("goal"), "missing keys: ['goal']"),
        (lambda d: d.__setitem__("surprise", 1), "unknown keys: ['surprise']"),
        (lambda d: d["tasks"][1].pop("title"), "Run.tasks[1] is missing keys"),
    ],
)
def test_invalid_state_rejected_on_load(store: StateStore, mutate, message):
    data = asdict(full_run())
    mutate(data)
    store.paths.root.mkdir(parents=True)
    store.paths.state.write_text(json.dumps(data))
    with pytest.raises(StateError, match=message.replace("[", r"\[").replace("]", r"\]")):
        store.load()


def test_corrupt_json_and_non_object(store: StateStore):
    store.paths.root.mkdir(parents=True)
    store.paths.state.write_text("{nope")
    with pytest.raises(StateError, match="not valid JSON"):
        store.load()
    store.paths.state.write_text("[]")
    with pytest.raises(StateError, match="must be an object"):
        store.load()


def test_save_rejects_invalid_status_and_keeps_old_file(store: StateStore):
    run = full_run()
    store.save(run)
    before = store.paths.state.read_bytes()
    run.tasks[0].status = "bogus"
    with pytest.raises(StateError):
        store.save(run)
    assert store.paths.state.read_bytes() == before


def test_atomic_write_survives_crash_before_replace(store: StateStore, monkeypatch):
    run = full_run()
    store.save(run)
    before = store.paths.state.read_bytes()

    def boom(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(state_mod.os, "replace", boom)
    run.goal = "changed"
    with pytest.raises(OSError, match="simulated crash"):
        store.save(run)
    monkeypatch.undo()

    assert store.paths.state.read_bytes() == before
    assert store.load().goal == "add csv export"
    assert [p.name for p in store.paths.root.iterdir() if p.name.startswith(".state.")] == []


def test_atomic_write_fsyncs_before_replace(store: StateStore, monkeypatch):
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(state_mod.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(state_mod.os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    store.save(full_run())
    assert calls.index("fsync") < calls.index("replace")


def test_lock_contention_between_stores(paths: Paths):
    first, second = StateStore(paths), StateStore(paths)
    with first.lock():
        with pytest.raises(StateLockedError, match="only one run"):
            with second.lock():
                pytest.fail("second lock must not be acquired")
    with second.lock():
        pass


def test_lock_released_after_exception(paths: Paths):
    first, second = StateStore(paths), StateStore(paths)
    with pytest.raises(RuntimeError):
        with first.lock():
            raise RuntimeError("x")
    with second.lock():
        pass


def test_lock_held_across_processes(paths: Paths):
    with StateStore(paths).lock():
        code = (
            "import sys; from pathlib import Path; from workforce.paths import Paths; "
            "from workforce.state import StateStore; from workforce.errors import StateLockedError\n"
            "try:\n"
            "    with StateStore(Paths(Path(sys.argv[1]))).lock(): print('acquired')\n"
            "except StateLockedError: print('locked')\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code, str(paths.repo)], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "locked"


def test_update_mutates_and_persists(store: StateStore):
    store.save(Run.create("r1", "goal"))
    result = store.update(lambda run: setattr(run, "status", "running"))
    assert result.status == "running"
    assert store.load().status == "running"


def test_update_without_run_errors(store: StateStore):
    with pytest.raises(StateError, match="no run in progress"):
        store.update(lambda run: None)


def test_update_does_not_save_when_fn_raises(store: StateStore):
    store.save(Run.create("r1", "goal"))

    def fn(run: Run):
        run.status = "running"
        raise RuntimeError("halfway")

    with pytest.raises(RuntimeError):
        store.update(fn)
    assert store.load().status == "planning"


def test_update_is_serialised_across_threads(store: StateStore):
    store.save(Run.create("r1", "goal"))

    def add_task():
        for _ in range(10):
            store.update(lambda run: run.tasks.append(Task(id=store_next(run), title="t", description="d")))

    def store_next(run: Run) -> str:
        return f"T{len(run.tasks) + 1}"

    threads = [threading.Thread(target=add_task) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [t.id for t in store.load().tasks]
    assert len(ids) == 40 and len(set(ids)) == 40


def test_update_reentrant_from_same_thread(store: StateStore):
    store.save(Run.create("r1", "goal"))
    store.update(lambda run: store.update(lambda inner: setattr(inner, "mode", "step")))
    assert store.load() is not None


def test_next_ids(store: StateStore):
    assert store.next_task_id() == "T1"
    assert store.next_question_id() == "Q1"
    run = full_run()
    run.tasks.append(Task(id="T10", title="x", description="y"))
    store.save(run)
    assert store.next_task_id() == "T11"
    assert store.next_question_id() == "Q3"


def test_task_and_question_lookup():
    run = full_run()
    assert run.task("T2").title == "two"
    assert run.question("Q1").text == "which?"
    with pytest.raises(StateError):
        run.task("T99")
    with pytest.raises(StateError):
        run.question("Q99")


def test_paths_layout_and_ensure_excludes_from_git(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    paths = Paths(tmp_path, home=tmp_path / "home")
    paths.ensure()
    paths.ensure()

    assert paths.root == tmp_path / ".workforce"
    assert paths.state == paths.root / "state.json"
    assert paths.lock == paths.root / "state.lock"
    assert paths.events == paths.root / "events.jsonl"
    assert paths.plan_md == paths.root / "plan.md"
    assert paths.decisions_md == paths.root / "decisions.md"
    assert paths.usage_json == paths.root / "usage.json"
    assert paths.usage_md == paths.root / "usage.md"
    assert paths.run_dir("r1") == paths.root / "runs" / "r1"
    assert paths.step_log("r1", "T1", "code", 2) == paths.root / "runs" / "r1" / "T1" / "code-2.jsonl"
    assert paths.runs.is_dir()
    assert not (paths.root / "worktrees").exists()
    assert not (tmp_path / "home").exists()

    exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert exclude.splitlines().count(".workforce/") == 1
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"], capture_output=True, text=True, check=True
    )
    assert ".workforce" not in status.stdout


def test_ensure_appends_on_new_line_and_respects_existing_entry(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.write_text("*.log")
    Paths(tmp_path, home=tmp_path / "home").ensure()
    assert exclude.read_text().splitlines() == ["*.log", ".workforce/", "/workforce.toml"]

    exclude.write_text("/.workforce\n")
    Paths(tmp_path, home=tmp_path / "home").ensure()
    assert exclude.read_text() == "/.workforce\n/workforce.toml\n"


def test_ensure_outside_git_repo_skips_the_exclude_step(tmp_path: Path):
    paths = Paths(tmp_path, home=tmp_path / "home")
    paths.ensure()
    assert paths.root.is_dir() and paths.runs.is_dir()
    assert not (tmp_path / ".git").exists()


def test_repo_slug_and_worktree_paths(tmp_path: Path):
    import hashlib

    repo = tmp_path / "my-app"
    repo.mkdir()
    home = tmp_path / "home"
    paths = Paths(repo, home=home)

    digest = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:8]
    assert paths.repo_slug == f"my-app-{digest}"
    assert paths.home == home
    assert paths.worktrees_root == home / ".workforce" / "worktrees" / f"my-app-{digest}"
    assert paths.worktree("r1", "T2") == paths.worktrees_root / "r1" / "T2"
    assert not home.exists()

    other = tmp_path / "elsewhere" / "my-app"
    other.mkdir(parents=True)
    assert Paths(other, home=home).repo_slug != paths.repo_slug
    assert Paths(other, home=home).repo_slug.startswith("my-app-")


def test_slug_uses_resolved_path(tmp_path: Path):
    repo = tmp_path / "real"
    repo.mkdir()
    link = tmp_path / "link"
    link.symlink_to(repo)
    home = tmp_path / "home"
    assert Paths(link, home=home).repo_slug == Paths(repo, home=home).repo_slug


def test_worktree_dir_for_is_lazy_and_outside_repo(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    home = tmp_path / "home"
    paths = Paths(tmp_path / "repo", home=home)
    paths.ensure()
    assert not home.exists()

    directory = paths.worktree_dir_for("r1")
    assert directory == paths.worktrees_root / "r1"
    assert directory.is_dir()
    assert paths.worktree("r1", "T1").parent == directory
    assert not paths.worktree("r1", "T1").exists()
    assert paths.repo not in directory.parents
    assert paths.worktree_dir_for("r1") == directory


def test_default_home_is_real_home_but_never_touched_by_construction(tmp_path: Path):
    paths = Paths(tmp_path)
    assert paths.home == Path.home()
