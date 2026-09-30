from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

ERROR_KINDS = ("model_unavailable", "auth", "rate_limited", "timeout", "crash", "schema")
INFRA_KINDS = ("auth", "rate_limited", "timeout", "crash")
SCRUBBED_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
EXTRA_SCRUBBED_ENV_VARS = (
    "CODEX_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
EXTRA_PATH_DIRS = ("~/.local/bin", "/opt/homebrew/bin")
KILL_GRACE_S = 3.0
KILL_POLL_S = 0.05


@dataclass
class AgentRequest:
    role: str
    agent: str
    model: str
    effort: str
    prompt: str
    cwd: Path
    schema: dict | None = None
    resume_session: str | None = None
    sandbox: str = "workspace-write"
    browser: bool = False
    computer_use: bool = False
    user_setup: bool = False
    log_path: Path | None = None
    timeout_s: int = 3600
    repo: Path | None = None
    add_dirs: list[Path] = field(default_factory=list)
    skip_git_check: bool = False
    tools: tuple[str, ...] | None = None
    no_mcp: bool = False


@dataclass
class AgentResult:
    ok: bool
    text: str
    structured: dict | None
    session_id: str | None
    model: str | None
    usage: dict = field(default_factory=dict)
    rate_limits: dict | None = None
    error_kind: str | None = None
    error: str | None = None


class AgentRunner(Protocol):
    def run(
        self, req: AgentRequest, on_event: Callable[[dict], None] | None = None
    ) -> AgentResult: ...


def is_infra(kind: str | None) -> bool:
    """True for failures of the machinery (auth, quota, timeout, crash), not of the work."""
    return kind in INFRA_KINDS


def failure(kind: str, message: str, **kw) -> AgentResult:
    """Build a failed AgentResult with sane empty fields."""
    return AgentResult(
        ok=False,
        text=kw.pop("text", ""),
        structured=None,
        session_id=kw.pop("session_id", None),
        model=kw.pop("model", None),
        usage=kw.pop("usage", {}),
        rate_limits=kw.pop("rate_limits", None),
        error_kind=kind,
        error=message,
    )


NO_ARTIFACT_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}
NO_ARTIFACT_PYTEST_OPTS = "-p no:cacheprovider"


def without_build_artifacts(env: dict) -> dict:
    """Stop Python and pytest from writing caches into the worktree, where they would be snapshotted and committed."""
    env.update(NO_ARTIFACT_ENV)
    opts = env.get("PYTEST_ADDOPTS", "")
    if NO_ARTIFACT_PYTEST_OPTS not in opts:
        env["PYTEST_ADDOPTS"] = f"{opts} {NO_ARTIFACT_PYTEST_OPTS}".strip()
    return env


def scrubbed_env(extra_env: dict | None = None, environ: dict | None = None) -> dict:
    """Environment for a child agent: no API keys or endpoint overrides, Homebrew and ~/.local/bin on PATH."""
    env = dict(os.environ if environ is None else environ)
    if extra_env:
        env.update(extra_env)
    for name in SCRUBBED_ENV_VARS + EXTRA_SCRUBBED_ENV_VARS:
        env.pop(name, None)
    extra = [os.path.expanduser(p) for p in EXTRA_PATH_DIRS]
    current = [p for p in env.get("PATH", "").split(os.pathsep) if p and p not in extra]
    env["PATH"] = os.pathsep.join(extra + current)
    return without_build_artifacts(env)


class EventLog:
    """Append-only JSONL writer for a raw agent event stream; usable as a context manager."""

    def __init__(self, path: Path | None):
        self._fh = None
        if path is not None:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def write(self, event: dict) -> None:
        if self._fh is not None:
            self._fh.write(json.dumps(event) + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@dataclass
class ProcessOutcome:
    returncode: int | None
    timed_out: bool
    aborted: bool
    stderr: str


_live: set[subprocess.Popen] = set()
_live_lock = threading.Lock()
_shutdown = threading.Event()


def request_shutdown() -> None:
    """From now on `stream_process` refuses to start new children (until `clear_shutdown`)."""
    _shutdown.set()


def clear_shutdown() -> None:
    _shutdown.clear()


def shutdown_requested() -> bool:
    return _shutdown.is_set()


def register(proc: subprocess.Popen) -> None:
    """Track a child that was started with `start_new_session`, so `kill_all` can stop its process group."""
    with _live_lock:
        _live.add(proc)


def unregister(proc: subprocess.Popen) -> None:
    with _live_lock:
        _live.discard(proc)


def live_processes() -> list[subprocess.Popen]:
    with _live_lock:
        return list(_live)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def kill_all(grace_s: float = KILL_GRACE_S) -> None:
    """SIGTERM every registered process group, then SIGKILL whatever is still alive after `grace_s`."""
    procs = [p for p in live_processes() if p.poll() is None]
    for proc in procs:
        _signal_group(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while procs and time.monotonic() < deadline:
        procs = [p for p in procs if p.poll() is None or _group_alive(p.pid)]
        if procs:
            time.sleep(KILL_POLL_S)
    for proc in procs:
        _signal_group(proc.pid, signal.SIGKILL)
    for proc in procs:
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            pass


atexit.register(kill_all)


def kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's process group (falls back to the child alone)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


_kill_group = kill_group


def stream_process(
    cmd: list[str],
    cwd: Path,
    env: dict,
    timeout_s: int,
    on_line: Callable[[str], bool | None],
    stdin_text: str | None = None,
) -> ProcessOutcome:
    """Run a command, feed stdout lines to on_line, kill on timeout or when on_line returns True.

    stdin is /dev/null, or `stdin_text` followed by end-of-file when it is given. Raises OSError if the
    binary cannot be started.
    """
    if shutdown_requested():
        raise OSError("workforce is shutting down; not starting new agent processes")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    register(proc)
    state = {"timed_out": False, "aborted": False}
    stderr_chunks: list[str] = []

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    def on_timeout() -> None:
        state["timed_out"] = True
        _kill_group(proc)

    def feed_stdin() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin_text)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

    drainer = threading.Thread(target=drain_stderr, daemon=True)
    drainer.start()
    feeder = None
    if stdin_text is not None:
        feeder = threading.Thread(target=feed_stdin, daemon=True)
        feeder.start()
    timer = threading.Timer(timeout_s, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if on_line(line.rstrip("\n")):
                state["aborted"] = True
                _kill_group(proc)
                break
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            _kill_group(proc)
            proc.wait()
        drainer.join(timeout=5)
        if feeder is not None:
            feeder.join(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()
        unregister(proc)
    return ProcessOutcome(
        returncode=proc.returncode,
        timed_out=state["timed_out"],
        aborted=state["aborted"],
        stderr="".join(stderr_chunks),
    )
