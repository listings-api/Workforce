"""Usage monitor: keeps the latest Claude/Codex quota readings, gates launches and plans resumes."""

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from workforce.agents.base import AgentRequest, AgentResult
from workforce.config import Config
from workforce.usage import claude_limits, codex_limits, summary
from workforce.usage.claude_limits import KIND_5H, Window
from workforce.usage.codex_limits import UsageReadError

FRESH = "fresh"
LAST_KNOWN = "last_known"
UNKNOWN = "unknown"

ACTION_NONE = "none"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"

USAGE_PAUSE_PREFIX = "usage:"
RESUME_DELAY_S = 60
PING_PROMPT = "Reply OK"
PING_TIMEOUT_S = 180
SOURCES = ("claude", "codex")


class SystemClock:
    """Wall clock with the same `.time()` interface tests replace with a fake."""

    def time(self) -> float:
        return time.time()


@dataclass(frozen=True)
class ResumePlan:
    """Whether a usage pause can end by itself, and when to look again."""

    auto: bool
    at: float | None
    blocking_windows: tuple[Window, ...] = field(default_factory=tuple)


class UsageMonitor:
    """Thread-safe: `ingest_claude` runs on the orchestrator thread, `tick` on the poll thread."""

    def __init__(
        self,
        paths: Any,
        config: Config,
        codex_bin: str | Path,
        claude_runner: Any,
        events: Any,
        clock: Any,
        codex_timeout_s: float = codex_limits.DEFAULT_TIMEOUT_S,
    ):
        self.paths = paths
        self.config = config
        self.codex_bin = codex_bin
        self.claude_runner = claude_runner
        self.events = events
        self.clock = clock
        self.codex_timeout_s = codex_timeout_s
        self._lock = threading.RLock()
        self._windows: dict[tuple[str, str], Window] = {}
        self._source_read_at: dict[str, float | None] = {name: None for name in SOURCES}
        self._source_error: dict[str, str | None] = {name: None for name in SOURCES}
        self._alerted: set[tuple[str, str, int | None]] = set()
        self._summary_state = summary.SummaryState()
        self._paused = False
        self._pause_reason: str | None = None
        self._pause_state: dict[tuple[str, str], Window] | None = None
        self._load()

    @property
    def pause_percent(self) -> int:
        return self.config.limits.pause_percent

    @property
    def alert_percent(self) -> int:
        return self.config.limits.alert_percent

    @property
    def poll_seconds(self) -> int:
        return self.config.limits.poll_minutes * 60

    @property
    def pause_reason(self) -> str | None:
        return self._pause_reason

    def _now(self) -> float:
        return claude_limits.now_value(self.clock)

    def ingest_claude(self, result: AgentResult) -> bool:
        """Record the `unifiedWindows` of a finished Claude run; True if it carried any reading."""
        if result.rate_limits is None:
            return False
        now = self._now()
        windows = claude_limits.from_rate_limits(result.rate_limits, now)
        if not windows:
            return False
        with self._lock:
            for window in windows:
                self._windows[window.key] = window
            self._source_read_at["claude"] = now
            self._source_error["claude"] = None
            self._after_update(windows)
        return True

    def refresh_codex(self) -> bool:
        """Read Codex quota via app-server. On failure the old values stay, marked by their age."""
        try:
            fresh = codex_limits.read(self.codex_bin, timeout_s=self.codex_timeout_s)
        except UsageReadError as exc:
            self._record_error("codex", str(exc))
            return False
        now = self._now()
        windows = [replace(w, read_at=now) for w in fresh]
        with self._lock:
            for key in [k for k in self._windows if k[0] == "codex"]:
                del self._windows[key]
            for window in windows:
                self._windows[window.key] = window
            self._source_read_at["codex"] = now
            self._source_error["codex"] = None
            self._after_update(windows)
        return True

    def refresh_claude_if_stale(self) -> bool:
        """Ping Haiku with a 1-turn prompt if the last Claude reading is older than the poll interval."""
        with self._lock:
            read_at = self._source_read_at["claude"]
            stale = read_at is None or self._now() - read_at >= self.poll_seconds
        return self._ping_claude() if stale else False

    def _ping_claude(self) -> bool:
        role = self.config.role("usage_summary")
        request = AgentRequest(
            role="usage_summary",
            agent=role.agent,
            model=role.model,
            effort=role.effort,
            prompt=PING_PROMPT,
            cwd=Path(self.paths.repo),
            timeout_s=PING_TIMEOUT_S,
            sandbox="read-only",
            repo=Path(self.paths.repo),
        )
        try:
            result = self.claude_runner.run(request)
        except Exception as exc:
            self._record_error("claude", f"usage ping raised {type(exc).__name__}: {exc}")
            return False
        if self.ingest_claude(result):
            return True
        detail = result.error if not result.ok else "run reported no rate_limit_event"
        self._record_error("claude", f"usage ping gave no reading: {detail}")
        return False

    def _record_error(self, source: str, message: str) -> None:
        with self._lock:
            changed = self._source_error[source] != message
            self._source_error[source] = message
            self._persist()
        if changed:
            self.events.emit("error", source=f"usage_{source}", message=message)

    def _after_update(self, windows: list[Window]) -> None:
        self._emit_alerts(windows)
        if self._pause_state is not None:
            for window in windows:
                if window.percent >= self.pause_percent:
                    self._pause_state[window.key] = window
        self._persist()

    def _emit_alerts(self, windows: list[Window]) -> None:
        for window in windows:
            if window.percent < self.alert_percent:
                continue
            key = (window.source, window.name, window.resets_at)
            if key in self._alerted:
                continue
            self._alerted.add(key)
            self.events.emit(
                "alert",
                source=window.source,
                window=window.name,
                percent=window.percent,
                resets_at=window.resets_at,
                message=f"{window.source} {window.name} window at {int(window.percent)}%",
            )

    def _freshness(self, read_at: float | None, now: float) -> str:
        if read_at is None:
            return UNKNOWN
        return FRESH if now - read_at < self.poll_seconds else LAST_KNOWN

    def readings(self) -> dict:
        """Current readings with per-window freshness; also what `usage.json` holds."""
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict:
        now = self._now()
        windows = []
        for window in sorted(self._windows.values(), key=lambda w: w.key):
            item = window.to_dict()
            item["freshness"] = self._freshness(window.read_at, now)
            item["expired"] = window.resets_at is not None and window.resets_at <= now
            windows.append(item)
        sources = {
            name: {
                "freshness": self._freshness(self._source_read_at[name], now),
                "read_at": self._source_read_at[name],
                "error": self._source_error[name],
            }
            for name in SOURCES
        }
        return {
            "generated_at": now,
            "poll_seconds": self.poll_seconds,
            "thresholds": {"alert_percent": self.alert_percent, "pause_percent": self.pause_percent},
            "sources": sources,
            "windows": windows,
        }

    def _persist(self) -> None:
        data = self._snapshot()
        data["alerted"] = sorted([list(k) for k in self._alerted], key=str)
        data["summary"] = {"at": self._summary_state.last_at, "digest": self._summary_state.last_digest}
        data["pause_state"] = (
            None
            if self._pause_state is None
            else {
                "reason": self._pause_reason,
                "windows": [w.to_dict() for w in sorted(self._pause_state.values(), key=lambda w: w.key)],
            }
        )
        path = Path(self.paths.usage_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def _load(self) -> None:
        try:
            data = json.loads(Path(self.paths.usage_json).read_text(encoding="utf-8"))
            windows = [Window.from_dict(w) for w in data.get("windows", [])]
            sources = data.get("sources", {})
            read_at = {name: sources.get(name, {}).get("read_at") for name in SOURCES}
            alerted = {(a[0], a[1], a[2]) for a in data.get("alerted", [])}
            saved = data.get("summary", {})
            state = summary.SummaryState(last_at=saved.get("at"), last_digest=saved.get("digest"))
            saved_pause = data.get("pause_state")
            pause_state = None
            pause_reason = None
            if isinstance(saved_pause, dict):
                remembered = [Window.from_dict(w) for w in saved_pause["windows"]]
                pause_state = {w.key: w for w in remembered}
                pause_reason = saved_pause.get("reason")
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
            return
        self._windows = {w.key: w for w in windows}
        self._source_read_at = {name: read_at[name] for name in SOURCES}
        self._alerted = alerted
        self._summary_state = state
        if pause_state:
            self._pause_state = pause_state
            self._pause_reason = pause_reason
            self._paused = True

    def _live_blocking(self, now: float) -> list[Window]:
        return [
            w
            for w in self._windows.values()
            if w.percent >= self.pause_percent and (w.resets_at is None or w.resets_at > now)
        ]

    def gate(self) -> str | None:
        """None to proceed; otherwise a pause reason. Windows past their reset time no longer count.

        Makes no network call. When it returns a reason it remembers the blocking windows
        (`pause_state` in usage.json), which `resume_plan()` uses until `clear_pause()`.
        """
        with self._lock:
            blocking = self._live_blocking(self._now())
            if not blocking:
                return None
            worst = max(blocking, key=lambda w: (w.percent, w.source, w.name))
            reason = (
                f"{USAGE_PAUSE_PREFIX} {worst.source} {worst.name} "
                f"{int(worst.percent)}% ≥ {self.pause_percent}%"
            )
            self._paused = True
            self._pause_reason = reason
            self._pause_state = {w.key: w for w in blocking}
            self._persist()
            return reason

    def _plan_windows(self) -> tuple[Window, ...]:
        """The windows that caused the pause: the remembered ones, else those now at the pause line."""
        source = (
            self._pause_state.values()
            if self._pause_state is not None
            else (w for w in self._windows.values() if w.percent >= self.pause_percent)
        )
        return tuple(sorted(source, key=lambda w: w.key))

    def resume_plan(self) -> ResumePlan:
        """Auto-resume only if every window that caused the pause is a 5-hour window.

        Uses the windows remembered when the pause started, so a later low reading cannot erase the
        plan. Without a remembered pause it falls back to the windows currently at the pause line.
        """
        with self._lock:
            blocking = self._plan_windows()
        if not blocking:
            return ResumePlan(auto=False, at=None, blocking_windows=blocking)
        if any(w.kind != KIND_5H or w.resets_at is None for w in blocking):
            return ResumePlan(auto=False, at=None, blocking_windows=blocking)
        return ResumePlan(
            auto=True,
            at=max(w.resets_at for w in blocking) + RESUME_DELAY_S,
            blocking_windows=blocking,
        )

    def can_resume(self) -> bool:
        """Take fresh readings of both sources; True only if every window is below the pause line.

        A source that was blocking must have been read successfully in this call.
        """
        with self._lock:
            blocking_sources = {w.source for w in self._plan_windows()}
            blocking_sources |= {w.source for w in self._windows.values() if w.percent >= self.pause_percent}
        refreshed = {"codex": self.refresh_codex(), "claude": self._ping_claude()}
        if any(not refreshed[source] for source in blocking_sources):
            return False
        with self._lock:
            return not any(w.percent >= self.pause_percent for w in self._windows.values())

    def clear_pause(self) -> None:
        """Forget the usage pause and its remembered windows (after any resume)."""
        with self._lock:
            self._paused = False
            self._pause_reason = None
            self._pause_state = None
            self._persist()

    def update_summary(self) -> bool:
        """Write `usage.md` if due; failures are reported as events and never raise."""
        readings = self.readings()
        wrote = summary.write_summary(
            readings,
            self.claude_runner,
            self.config.role("usage_summary"),
            self.paths,
            self.clock,
            self._summary_state,
            events=self.events,
            on_result=self.ingest_claude,
        )
        with self._lock:
            self._persist()
        return wrote

    def tick(self, paused: bool | None = None, pause_reason: str | None = None) -> str:
        """Poll step. Returns "none", "pause" or "resume".

        `paused`/`pause_reason` are the run's own status when the caller knows it; otherwise the
        monitor's memory of its last `gate()` pause is used. Only usage pauses ever auto-resume.
        """
        with self._lock:
            was_paused = self._paused if paused is None else paused
            usage_pause = was_paused and (self._paused or (pause_reason or "").startswith(USAGE_PAUSE_PREFIX))
        self.refresh_codex()
        if not was_paused:
            self.refresh_claude_if_stale()
        plan = self.resume_plan()
        self.update_summary()

        if not was_paused:
            return ACTION_PAUSE if self.gate() else ACTION_NONE
        if not usage_pause:
            return ACTION_NONE
        if not (plan.auto and plan.at is not None and self._now() >= plan.at):
            return ACTION_NONE
        if self.can_resume():
            self.clear_pause()
            return ACTION_RESUME
        return ACTION_NONE
