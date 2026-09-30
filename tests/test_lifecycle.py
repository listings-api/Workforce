"""Process lifecycle: the child-process registry, interrupts, the shutdown guard and the state write lock."""

import multiprocessing
import os
import signal
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from tests.test_scheduler import FakeTeam, make_orch, seed
from workforce import checks
from workforce.agents import base
from workforce.agents.base import AgentResult, stream_process
from workforce.cli import pause_interrupted, shutdown_guard
from workforce.paths import Paths
from workforce.scheduler import Scheduler
from workforce.state import Run, StateStore

WAIT_S = 20

SLEEPER = textwrap.dedent(
    """
    import os, signal, subprocess, sys, time
    pid_dir = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2] == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    with open(os.path.join(pid_dir, f"pids-{os.getpid()}"), "w") as fh:
        fh.write(f"{os.getpid()} {child.pid}")
    print("ready", flush=True)
    time.sleep(300)
    """
)


@pytest.fixture(autouse=True)
def clean_registry():
    previous = signal.getsignal(signal.SIGINT)
    if previous is signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    yield
    signal.signal(signal.SIGINT, previous)
    base.kill_all(grace_s=0.5)
    base.clear_shutdown()
    for proc in base.live_processes():
        base.unregister(proc)


@pytest.fixture
def sleeper(tmp_path):
    path = tmp_path / "sleeper.py"
    path.write_text(SLEEPER)
    return path


def wait_until(predicate, timeout=WAIT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def read_pids(directory: Path) -> list[tuple[int, int]]:
    found = []
    for path in directory.glob("pids-*"):
        text = path.read_text().split()
        if len(text) == 2:
            found.append((int(text[0]), int(text[1])))
    return found


def group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


def pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def launch_in_thread(cmd, cwd, timeout_s=120):
    box = {}

    def target():
        box["outcome"] = stream_process(cmd, cwd, dict(os.environ), timeout_s, lambda line: None)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


class TestRegistry:
    def test_register_and_unregister(self):
        proc = type("P", (), {"pid": 1, "poll": lambda self: None})()
        base.register(proc)
        assert proc in base.live_processes()
        base.unregister(proc)
        base.unregister(proc)
        assert proc not in base.live_processes()

    def test_stream_process_registers_while_running_and_unregisters_after(self, tmp_path, sleeper):
        thread, box = launch_in_thread([sys.executable, str(sleeper), str(tmp_path)], tmp_path)
        assert wait_until(lambda: read_pids(tmp_path))
        assert len(base.live_processes()) == 1
        base.kill_all(grace_s=2)
        thread.join(WAIT_S)
        assert not thread.is_alive()
        assert base.live_processes() == []
        assert box["outcome"].returncode != 0

    def test_kill_all_kills_the_whole_process_group(self, tmp_path, sleeper):
        thread, _ = launch_in_thread([sys.executable, str(sleeper), str(tmp_path)], tmp_path)
        assert wait_until(lambda: read_pids(tmp_path))
        (leader, grandchild), = read_pids(tmp_path)
        assert not group_gone(leader)
        base.kill_all(grace_s=2)
        thread.join(WAIT_S)
        assert wait_until(lambda: group_gone(leader))
        assert wait_until(lambda: pid_gone(grandchild))

    def test_a_child_that_ignores_sigterm_is_killed_after_the_grace_period(self, tmp_path, sleeper):
        thread, _ = launch_in_thread([sys.executable, str(sleeper), str(tmp_path), "ignore-term"], tmp_path)
        assert wait_until(lambda: read_pids(tmp_path))
        (leader, grandchild), = read_pids(tmp_path)
        started = time.monotonic()
        base.kill_all(grace_s=0.6)
        assert time.monotonic() - started >= 0.5
        thread.join(WAIT_S)
        assert wait_until(lambda: group_gone(leader))
        assert wait_until(lambda: pid_gone(grandchild))

    def test_kill_all_with_nothing_registered_is_a_no_op(self):
        base.kill_all(grace_s=0.1)

    def test_no_new_child_starts_after_a_shutdown_is_requested(self, tmp_path):
        base.request_shutdown()
        with pytest.raises(OSError, match="shutting down"):
            stream_process([sys.executable, "-c", "print(1)"], tmp_path, dict(os.environ), 5, lambda line: None)
        base.clear_shutdown()
        outcome = stream_process([sys.executable, "-c", "print(1)"], tmp_path, dict(os.environ), 5, lambda line: None)
        assert outcome.returncode == 0

    def test_an_event_log_closes_at_the_end_of_a_with_block(self, tmp_path):
        with base.EventLog(tmp_path / "raw.jsonl") as log:
            log.write({"a": 1})
        assert log._fh is None
        assert (tmp_path / "raw.jsonl").read_text().strip() == '{"a": 1}'


class TestChecksInterrupt:
    def test_an_interrupted_check_kills_its_process_group(self, tmp_path):
        pid_file = tmp_path / "check.pid"
        cmd = f"echo $$ > {pid_file}; sleep 300; echo done"
        threading.Thread(
            target=lambda: (wait_until(pid_file.exists) and time.sleep(0.2), os.kill(os.getpid(), signal.SIGINT)),
            daemon=True,
        ).start()
        with pytest.raises(KeyboardInterrupt):
            checks._run_one(tmp_path, "test", cmd, timeout_s=120)
        pgid = int(pid_file.read_text())
        assert group_gone(pgid)
        assert base.live_processes() == []

    def test_a_check_that_times_out_is_unregistered(self, tmp_path):
        result = checks._run_one(tmp_path, "test", "sleep 300", timeout_s=1)
        assert result.timed_out
        assert base.live_processes() == []

    def test_a_running_check_is_visible_to_kill_all(self, tmp_path):
        pid_file = tmp_path / "check.pid"
        box = {}
        thread = threading.Thread(
            target=lambda: box.update(result=checks._run_one(tmp_path, "test", f"echo $$ > {pid_file}; sleep 300", 120)),
            daemon=True,
        )
        thread.start()
        assert wait_until(lambda: pid_file.exists() and pid_file.read_text().strip() and base.live_processes())
        base.kill_all(grace_s=2)
        thread.join(WAIT_S)
        assert not thread.is_alive()
        assert not box["result"].passed
        assert group_gone(int(pid_file.read_text()))


class SleepingCoderTeam(FakeTeam):
    """A FakeTeam whose coder step runs a real child (with a grandchild) through `stream_process`."""

    def __init__(self, repo, script, pid_dir, plan_tasks=None):
        super().__init__(repo, plan_tasks)
        self.script = script
        self.pid_dir = pid_dir

    def _coder(self, req, task, entry):
        try:
            stream_process(
                [sys.executable, str(self.script), str(self.pid_dir)], Path(req.cwd), dict(os.environ), 300, lambda line: None
            )
        except OSError as exc:
            return AgentResult(ok=False, text="", structured=None, session_id=None, model=req.model, error_kind="crash", error=str(exc))
        return self._ok(req, text="interrupted")


class TestInterruptedRun:
    def test_interrupting_a_parallel_run_leaves_no_child_and_pauses_the_run(self, tmp_path, git_repo, sleeper, monkeypatch):
        pid_dir = tmp_path / "pids"
        pid_dir.mkdir()
        team = SleepingCoderTeam(git_repo, sleeper, pid_dir)
        orch = make_orch(tmp_path, git_repo, team, monkeypatch)
        seed(orch, [("T1", []), ("T2", [])])
        scheduler = Scheduler(orch, 2)

        def interrupter():
            if wait_until(lambda: len(read_pids(pid_dir)) == 2):
                time.sleep(0.2)
                os.kill(os.getpid(), signal.SIGINT)

        threading.Thread(target=interrupter, daemon=True).start()
        with pytest.raises(KeyboardInterrupt):
            with shutdown_guard(lambda: pause_interrupted(orch)):
                scheduler.run_until_blocked()

        pids = read_pids(pid_dir)
        assert len(pids) == 2
        for leader, grandchild in pids:
            assert group_gone(leader)
            assert pid_gone(grandchild)
        assert base.live_processes() == []
        run = orch.store.load()
        assert run.status == "paused"
        assert run.pause_reason == "interrupted"
        assert not base.shutdown_requested()

    def test_run_parallel_pauses_cancels_and_reraises_on_any_base_exception(self, tmp_path, git_repo, monkeypatch):
        team = FakeTeam(git_repo)
        orch = make_orch(tmp_path, git_repo, team, monkeypatch)
        seed(orch, [("T1", []), ("T2", [])])
        scheduler = Scheduler(orch, 1)
        calls = {"n": 0}

        def boom(*args, **kwargs):
            calls["n"] += 1
            raise KeyboardInterrupt

        monkeypatch.setattr("workforce.scheduler.wait", boom)
        with pytest.raises(KeyboardInterrupt):
            scheduler.run_until_blocked()
        assert calls["n"] == 1
        assert scheduler._halt.is_set()
        run = orch.store.load()
        assert run.status == "paused" and run.pause_reason == "interrupted"
        assert threading.active_count() >= 1
        assert not [t for t in threading.enumerate() if t.name.startswith("wf-task")]

    def test_an_unexpected_error_in_the_scheduler_loop_pauses_with_its_message(self, tmp_path, git_repo, monkeypatch):
        orch = make_orch(tmp_path, git_repo, FakeTeam(git_repo), monkeypatch)
        seed(orch, [("T1", [])])
        scheduler = Scheduler(orch, 1)
        monkeypatch.setattr(scheduler, "_reap", lambda active: (_ for _ in ()).throw(RuntimeError("scheduler bug")))
        with pytest.raises(RuntimeError, match="scheduler bug"):
            scheduler.run_until_blocked()
        run = orch.store.load()
        assert run.status == "paused" and "scheduler bug" in run.pause_reason


class TestShutdownGuard:
    def test_sigint_becomes_keyboard_interrupt_and_handlers_are_restored(self):
        before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
        paused = []
        with pytest.raises(KeyboardInterrupt):
            with shutdown_guard(lambda: paused.append(1)):
                assert signal.getsignal(signal.SIGINT) is not before[0]
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(2)
        assert paused
        assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
        assert not base.shutdown_requested()

    def test_sigterm_becomes_system_exit_143(self):
        with pytest.raises(SystemExit) as info:
            with shutdown_guard(lambda: None):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(2)
        assert info.value.code == 128 + signal.SIGTERM

    def test_the_guard_is_a_no_op_off_the_main_thread(self):
        seen = {}

        def target():
            with shutdown_guard(lambda: None):
                seen["ran"] = True

        thread = threading.Thread(target=target)
        thread.start()
        thread.join()
        assert seen == {"ran": True}

    def test_pause_interrupted_only_touches_a_run_that_is_doing_work(self, tmp_path, git_repo, monkeypatch):
        orch = make_orch(tmp_path, git_repo, FakeTeam(git_repo), monkeypatch)
        pause_interrupted(orch)
        seed(orch, [("T1", [])])
        orch.store.update(lambda r: setattr(r, "status", "awaiting_user"))
        pause_interrupted(orch)
        assert orch.store.load().status == "awaiting_user"
        orch.store.update(lambda r: setattr(r, "status", "running"))
        pause_interrupted(orch)
        assert orch.store.load().status == "paused"
        assert orch.store.load().pause_reason == "interrupted"


def _increment_many(root: str, count: int) -> None:
    store = StateStore(Paths(Path(root)))
    for _ in range(count):
        store.update(lambda run: setattr(run, "goal", str(int(run.goal) + 1)))


def _increment_once(root: str, done) -> None:
    _increment_many(root, 1)
    done.set()


@pytest.fixture
def counter_store(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = StateStore(Paths(repo, home=tmp_path / "home"))
    store.paths.root.mkdir(parents=True, exist_ok=True)
    store.save(Run.create("r1", "0"))
    return store


class TestStateWriteLock:
    def test_two_processes_incrementing_lose_no_update(self, counter_store):
        root = str(counter_store.paths.repo)
        ctx = multiprocessing.get_context("spawn")
        workers = [ctx.Process(target=_increment_many, args=(root, 50)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(120)
            assert worker.exitcode == 0
        assert counter_store.load().goal == "100"

    def test_threads_and_a_process_together_lose_no_update(self, counter_store):
        root = str(counter_store.paths.repo)
        ctx = multiprocessing.get_context("spawn")
        worker = ctx.Process(target=_increment_many, args=(root, 30))
        worker.start()
        threads = [threading.Thread(target=_increment_many, args=(root, 10)) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        worker.join(120)
        assert worker.exitcode == 0
        assert counter_store.load().goal == "60"

    def test_holding_write_lock_blocks_another_process_until_released(self, counter_store):
        root = str(counter_store.paths.repo)
        ctx = multiprocessing.get_context("spawn")
        done = ctx.Event()
        with counter_store.write_lock():
            worker = ctx.Process(target=_increment_once, args=(root, done))
            worker.start()
            assert not done.wait(1.5)
            assert counter_store.load().goal == "0"
        assert done.wait(60)
        worker.join(60)
        assert counter_store.load().goal == "1"

    def test_write_lock_is_reentrant_and_update_can_run_inside_it(self, counter_store):
        with counter_store.write_lock():
            with counter_store.write_lock():
                counter_store.update(lambda run: setattr(run, "goal", "5"))
        assert counter_store.load().goal == "5"

    def test_two_store_objects_on_one_repo_share_the_lock_in_a_thread(self, counter_store):
        other = StateStore(counter_store.paths)
        with counter_store.write_lock():
            other.update(lambda run: setattr(run, "goal", "7"))
        assert other.load().goal == "7"

    def test_the_lock_file_is_next_to_state_json(self, counter_store):
        with counter_store.write_lock():
            pass
        assert (counter_store.paths.root / "state.write.lock").exists()


class TestStdinText:
    def test_the_text_reaches_the_child_and_then_stdin_closes(self, tmp_path):
        lines = []
        script = "import sys; data = sys.stdin.read(); print(len(data)); print(data[-5:])"
        text = "x" * 500_000 + "END\n"
        outcome = stream_process([sys.executable, "-c", script], tmp_path, dict(os.environ), 30, lines.append, stdin_text=text)
        assert outcome.returncode == 0
        assert lines[0] == str(len(text)) and lines[1] == "xEND"
        assert base.live_processes() == []

    def test_without_stdin_text_the_child_sees_end_of_file_at_once(self, tmp_path):
        lines = []
        outcome = stream_process(
            [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], tmp_path, dict(os.environ), 30, lines.append
        )
        assert outcome.returncode == 0 and lines == ["''"]

    def test_a_child_that_never_reads_stdin_does_not_hang_the_run(self, tmp_path):
        outcome = stream_process(
            [sys.executable, "-c", "print('bye')"], tmp_path, dict(os.environ), 30, lambda line: None, stdin_text="y" * 2_000_000
        )
        assert outcome.returncode == 0


class TestScrubbedEnvironment:
    def test_keys_and_endpoint_overrides_are_dropped(self):
        environ = {
            "ANTHROPIC_API_KEY": "a",
            "ANTHROPIC_AUTH_TOKEN": "b",
            "OPENAI_API_KEY": "c",
            "CODEX_API_KEY": "d",
            "ANTHROPIC_BASE_URL": "http://x",
            "OPENAI_BASE_URL": "http://y",
            "OPENAI_ORG_ID": "org",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "KEEP_ME": "yes",
            "HOME": "/h",
        }
        env = base.scrubbed_env(environ=environ)
        assert env["KEEP_ME"] == "yes" and env["HOME"] == "/h"
        for name in base.SCRUBBED_ENV_VARS + base.EXTRA_SCRUBBED_ENV_VARS:
            assert name not in env
        assert "CODEX_API_KEY" in base.EXTRA_SCRUBBED_ENV_VARS

    def test_extra_env_cannot_smuggle_a_key_in(self):
        env = base.scrubbed_env(extra_env={"CODEX_API_KEY": "z", "OPENAI_BASE_URL": "http://y"}, environ={})
        assert "CODEX_API_KEY" not in env and "OPENAI_BASE_URL" not in env

    def test_schema_validation_lives_in_schemas_only(self):
        assert not hasattr(base, "validate_schema")


class TestPathsStayInside:
    @pytest.mark.parametrize("bad", ["..", ".", "", "a/b", "../x", "T1/../../x", "a\\b", "x\x00y"])
    def test_bad_components_are_refused(self, tmp_path, bad):
        from workforce.paths import PathEscapeError

        paths = Paths(tmp_path / "repo", home=tmp_path / "home")
        with pytest.raises(PathEscapeError):
            paths.worktree("run1", bad)
        with pytest.raises(PathEscapeError):
            paths.worktree(bad, "T1")
        with pytest.raises(PathEscapeError):
            paths.step_log("run1", bad, "code", 1)
        with pytest.raises(PathEscapeError):
            paths.run_dir(bad)

    def test_a_symlink_out_of_the_root_is_refused(self, tmp_path):
        from workforce.paths import PathEscapeError

        paths = Paths(tmp_path / "repo", home=tmp_path / "home")
        paths.worktrees_root.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (paths.worktrees_root / "run1").symlink_to(outside)
        with pytest.raises(PathEscapeError):
            paths.worktree("run1", "T1")
        with pytest.raises(PathEscapeError):
            paths.worktree_dir_for("run1")

    def test_normal_ids_resolve_under_the_root(self, tmp_path):
        paths = Paths(tmp_path / "repo", home=tmp_path / "home")
        wt = paths.worktree("20260929-101010", "T12")
        assert wt == paths.worktrees_root / "20260929-101010" / "T12"
        assert paths.step_log("r", "T1", "code", 2) == paths.runs / "r" / "T1" / "code-2.jsonl"
        assert paths.worktree_dir_for("r").is_dir()
