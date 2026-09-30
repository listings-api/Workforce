"""Append-only event log with an in-process subscriber list."""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from workforce.paths import Paths

logger = logging.getLogger(__name__)

KINDS = frozenset(
    {
        "run_started",
        "plan_ready",
        "debate_round",
        "question",
        "answered",
        "task_status",
        "agent_event",
        "check_result",
        "review",
        "commit_waiting",
        "committed",
        "merged",
        "alert",
        "paused",
        "resumed",
        "decision",
        "note",
        "error",
        "run_done",
    }
)

Subscriber = Callable[[dict[str, Any]], None]


class EventLog:
    """Writes events to `.workforce/events.jsonl` and fans them out to subscribers."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self._write_lock = threading.Lock()
        self._subscribers: list[Subscriber] = []

    def emit(self, kind: str, **data: Any) -> dict[str, Any]:
        """Append one event and notify subscribers synchronously; returns the stored event."""
        if kind not in KINDS:
            raise ValueError(f"unknown event kind '{kind}'; expected one of {sorted(KINDS)}")
        if "ts" in data:
            raise ValueError("'ts' is reserved and set by the event log")
        event = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind, **data}
        line = (json.dumps(event, default=str) + "\n").encode("utf-8")

        with self._write_lock:
            self.paths.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.paths.events, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            subscribers = list(self._subscribers)

        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                logger.exception("event subscriber %r failed on %s", callback, kind)
        return event

    def read(self, since_offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Return complete events at or after `since_offset` and the offset to resume from."""
        try:
            with open(self.paths.events, "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                start = since_offset if since_offset <= size else 0
                handle.seek(start)
                blob = handle.read()
        except FileNotFoundError:
            return [], since_offset

        end = blob.rfind(b"\n") + 1
        events: list[dict[str, Any]] = []
        for raw in blob[:end].splitlines():
            if not raw.strip():
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                logger.warning("skipping malformed event line: %r", raw[:200])
        return events, start + end

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register `callback` for future events; the returned function unsubscribes it."""
        with self._write_lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._write_lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe
