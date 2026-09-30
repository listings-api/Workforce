"""Read Codex quota from `codex app-server` (JSON-RPC over stdio); costs no model quota."""

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from workforce.agents.base import kill_group, register, scrubbed_env, shutdown_requested, unregister
from workforce.errors import WorkforceError
from workforce.usage.claude_limits import KIND_5H, KIND_OTHER, KIND_WEEKLY, Window, now_value

DEFAULT_TIMEOUT_S = 20
FIVE_HOUR_MINS = 300
WEEKLY_MINS = 10080

_EOF = object()


class UsageReadError(WorkforceError):
    """A usage reading could not be obtained (timeout, crash, malformed reply)."""


def parse_rate_limits(rate_limits: dict | None, now: Any) -> list[Window]:
    """Map `primary`/`secondary` to windows by `windowDurationMins`; null windows are skipped."""
    read_at = now_value(now)
    windows: dict[str, Window] = {}
    if not isinstance(rate_limits, dict):
        return []
    for slot in ("primary", "secondary"):
        info = rate_limits.get(slot)
        if not isinstance(info, dict):
            continue
        used = info.get("usedPercent")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        minutes = info.get("windowDurationMins")
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            name, kind = slot, KIND_OTHER
        elif minutes == FIVE_HOUR_MINS:
            name, kind = "five_hour", KIND_5H
        elif minutes == WEEKLY_MINS:
            name, kind = "seven_day", KIND_WEEKLY
        else:
            name, kind = f"{minutes}m", KIND_OTHER
        resets = info.get("resetsAt")
        window = Window(
            source="codex",
            name=name,
            percent=float(used),
            resets_at=int(resets) if isinstance(resets, (int, float)) and not isinstance(resets, bool) else None,
            read_at=read_at,
            kind=kind,
        )
        existing = windows.get(name)
        if existing is None or window.percent > existing.percent:
            windows[name] = window
    return list(windows.values())


def _kill(proc: subprocess.Popen) -> None:
    kill_group(proc)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _close_pipes(proc: subprocess.Popen) -> None:
    for pipe in (proc.stdin, proc.stdout):
        if pipe is None:
            continue
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


def read(binary: str | Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> list[Window]:
    """Ask `codex app-server` for `account/rateLimits/read`; always kills the process.

    Raises UsageReadError on any failure. Never returns guessed numbers.
    """
    result = request(binary, "account/rateLimits/read", None, timeout_s)
    limits = result.get("rateLimits")
    if not isinstance(limits, dict):
        raise UsageReadError("codex app-server reply has no rateLimits object")
    return parse_rate_limits(limits, time.time())


def request(binary: str | Path, method: str, params: dict | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """One JSON-RPC call to a fresh `codex app-server` (after the initialize handshake); always kills the process."""
    if shutdown_requested():
        raise UsageReadError("workforce is shutting down; not reading Codex usage")
    deadline = time.monotonic() + timeout_s
    try:
        proc = subprocess.Popen(
            [str(Path(binary).expanduser()), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=scrubbed_env(),
            start_new_session=True,
        )
    except OSError as exc:
        raise UsageReadError(f"cannot start codex app-server at {binary}: {exc}") from exc
    register(proc)

    lines: "queue.Queue[Any]" = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(_EOF)

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()

    def send(message: dict) -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise UsageReadError(f"codex app-server closed its input: {exc}") from exc

    def await_response(request_id: int) -> dict:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageReadError(f"codex app-server did not answer within {timeout_s}s")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                raise UsageReadError(f"codex app-server did not answer within {timeout_s}s") from None
            if line is _EOF:
                raise UsageReadError("codex app-server exited before answering")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                raise UsageReadError(f"codex app-server error: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise UsageReadError(f"codex app-server returned no result for request {request_id}")
            return result

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "workforce", "version": "0.1"}},
            }
        )
        await_response(1)
        send({"jsonrpc": "2.0", "method": "initialized"})
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": 2, "method": method}
        if params is not None:
            message["params"] = params
        send(message)
        return await_response(2)
    finally:
        try:
            _kill(proc)
            pump_thread.join(timeout=5)
            _close_pipes(proc)
        finally:
            unregister(proc)
