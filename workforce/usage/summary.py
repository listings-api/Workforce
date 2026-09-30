"""Haiku writes a short plain-English note about the usage readings. It never decides anything."""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from workforce.agents.base import AgentRequest, AgentResult
from workforce.config import Role
from workforce.usage.claude_limits import now_value

MIN_INTERVAL_S = 600
SUMMARY_TIMEOUT_S = 180
PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "usage_summary.md"


@dataclass
class SummaryState:
    """When the summary was last written and for which readings."""

    last_at: float | None = None
    last_digest: str | None = None


def digest(readings: dict) -> str:
    """Stable fingerprint of the numbers in `readings`, ignoring timestamps and freshness."""
    core = [
        [w["source"], w["name"], w["percent"], w.get("resets_at")]
        for w in sorted(readings.get("windows", []), key=lambda w: (w["source"], w["name"]))
    ]
    unknown = sorted(s for s, info in readings.get("sources", {}).items() if info.get("freshness") == "unknown")
    return hashlib.sha1(json.dumps([core, unknown]).encode("utf-8")).hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _compact(readings: dict) -> dict:
    windows = []
    for w in readings.get("windows", []):
        item = {
            "source": w["source"],
            "window": w["name"],
            "percent_used": w["percent"],
            "freshness": w["freshness"],
            "read_at": _iso(w["read_at"]),
        }
        if w.get("resets_at") is not None:
            item["resets_at"] = _iso(w["resets_at"])
        windows.append(item)
    sources = {
        name: {"freshness": info["freshness"], **({"error": info["error"]} if info.get("error") else {})}
        for name, info in readings.get("sources", {}).items()
    }
    return {"windows": windows, "sources": sources}


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def write_summary(
    readings: dict,
    runner: Any,
    role: Role,
    paths: Any,
    clock: Any,
    state: SummaryState,
    events: Any = None,
    on_result: Callable[[AgentResult], None] | None = None,
) -> bool:
    """Write `usage.md` if the readings changed and the last write is at least 10 minutes old.

    Returns True when a new file was written. Any failure keeps the old file, emits an `error`
    event and returns False. `usage.json` (written by the monitor) stays the authority.
    """
    now = now_value(clock)
    current = digest(readings)
    if current == state.last_digest:
        return False
    if state.last_at is not None and now - state.last_at < MIN_INTERVAL_S:
        return False

    thresholds = readings.get("thresholds", {})
    prompt = PROMPT_FILE.read_text(encoding="utf-8").format(
        now=_iso(now),
        readings=json.dumps(_compact(readings), indent=2, sort_keys=True),
        alert_percent=thresholds["alert_percent"],
        pause_percent=thresholds["pause_percent"],
    )
    request = AgentRequest(
        role="usage_summary",
        agent=role.agent,
        model=role.model,
        effort=role.effort,
        prompt=prompt,
        cwd=Path(paths.repo),
        timeout_s=SUMMARY_TIMEOUT_S,
        sandbox="read-only",
        repo=Path(paths.repo),
    )
    state.last_at = now
    try:
        result = runner.run(request)
    except Exception as exc:
        return _fail(events, f"usage summary run raised {type(exc).__name__}: {exc}")
    if on_result is not None:
        on_result(result)
    if not result.ok:
        return _fail(events, f"usage summary run failed ({result.error_kind}): {result.error}")
    text = (result.text or "").strip()
    if not text:
        return _fail(events, "usage summary run returned no text")
    try:
        _atomic_write_text(Path(paths.usage_md), f"Updated {_iso(now)}\n\n{text}\n")
    except OSError as exc:
        return _fail(events, f"cannot write {paths.usage_md}: {exc}")
    state.last_digest = current
    return True


def _fail(events: Any, message: str) -> bool:
    if events is not None:
        events.emit("error", source="usage_summary", message=message)
    return False
