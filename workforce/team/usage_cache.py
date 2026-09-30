"""Usage caches shared by the status line, the hooks and the MCP server, plus the alert/stop state."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from workforce.team.config import TeamConfig, atomic_write, workforce_dir
from workforce.usage.claude_limits import Window, now_value

CLAUDE_FILE = "usage.json"
CODEX_FILE = "codex_usage.json"
OVERRIDE_FILE = "usage_override.json"
CLAUDE_WINDOWS = ("five_hour", "seven_day")
_RANK = {"ok": 0, "alert": 1, "stop": 2}


def _path(name: str, home: Path | None) -> Path:
    return workforce_dir(home) / name


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def write_claude(windows: dict, home: Path | None = None, now: Any = None) -> None:
    """Store Claude's `five_hour` / `seven_day` readings (`used_percentage`, `resets_at`) with a read time; unknown values stay None."""
    read_at = now_value(time.time() if now is None else now)
    out = {}
    for name in CLAUDE_WINDOWS:
        info = windows.get(name)
        if not isinstance(info, dict):
            continue
        out[name] = {
            "used_percentage": _number(info.get("used_percentage")),
            "resets_at": None if _number(info.get("resets_at")) is None else int(info["resets_at"]),
            "read_at": read_at,
        }
    atomic_write(_path(CLAUDE_FILE, home), json.dumps(out, indent=2))


def write_codex(windows: list[Window], home: Path | None = None, now: Any = None) -> None:
    """Store Codex windows (from `codex_limits.read`) and the time they were fetched."""
    read_at = now_value(time.time() if now is None else now)
    payload = {
        "read_at": read_at,
        "windows": [
            {"name": w.name, "kind": w.kind, "percent": w.percent, "resets_at": w.resets_at, "read_at": w.read_at}
            for w in windows
        ],
    }
    atomic_write(_path(CODEX_FILE, home), json.dumps(payload, indent=2))


def codex_read_at(home: Path | None = None) -> float | None:
    """When the Codex cache was last refreshed (even with no windows), or None if it never was."""
    data = _load(_path(CODEX_FILE, home))
    return _number(data.get("read_at")) if isinstance(data, dict) else None


def read(home: Path | None = None) -> dict:
    """`{"claude": {...}|None, "codex": [...]|None}`; a missing or unreadable file gives None for that side."""
    claude = _load(_path(CLAUDE_FILE, home))
    codex = _load(_path(CODEX_FILE, home))
    windows = codex.get("windows") if isinstance(codex, dict) else None
    return {
        "claude": claude if isinstance(claude, dict) else None,
        "codex": windows if isinstance(windows, list) else None,
    }


def window_key(source: str, name: str) -> str:
    return f"{source}:{name}"


def _normalize_key(key: str) -> str:
    return key.strip().replace(" ", ":", 1)


def override_set(window_key: str, resets_at: int, home: Path | None = None) -> None:
    """Allow work past the stop threshold for this window until it resets at `resets_at`."""
    path = _path(OVERRIDE_FILE, home)
    data = _load(path)
    data = data if isinstance(data, dict) else {}
    now = time.time()
    data = {k: v for k, v in data.items() if not (isinstance(v, (int, float)) and 0 < v < now)}
    data[_normalize_key(window_key)] = int(resets_at or 0)
    atomic_write(path, json.dumps(data, indent=2))


def override_active(window_key: str, resets_at: int, home: Path | None = None) -> bool:
    """True when an override was set for exactly this window reset time (a new window needs a new override)."""
    data = _load(_path(OVERRIDE_FILE, home))
    return isinstance(data, dict) and data.get(_normalize_key(window_key)) == int(resets_at or 0)


def _candidates(cache: dict, now: float, sources: tuple[str, ...] | None = None) -> list[dict]:
    found = []
    for name, info in (cache["claude"] or {}).items():
        if name in CLAUDE_WINDOWS and isinstance(info, dict) and _number(info.get("used_percentage")) is not None:
            found.append(("claude", name, float(info["used_percentage"]), _number(info.get("resets_at"))))
    for info in cache["codex"] or []:
        if isinstance(info, dict) and _number(info.get("percent")) is not None and info.get("name"):
            found.append(("codex", str(info["name"]), float(info["percent"]), _number(info.get("resets_at"))))
    return [
        {"source": s, "name": n, "percent": p, "resets_at": None if r is None else int(r)}
        for s, n, p, r in found
        if (r is None or r > now) and (sources is None or s in sources)
    ]


def _describe(item: dict) -> str:
    text = f"{item['source']} {item['name']} usage is {item['percent']:.0f}%"
    if item["resets_at"]:
        text += f", resets {datetime.fromtimestamp(item['resets_at']).strftime('%a %H:%M')}"
    return text


def state(cfg: TeamConfig, home: Path | None = None, now: Any = None, sources: tuple[str, ...] | None = None) -> dict:
    """The worst window: `{"level": "ok"|"alert"|"stop", "window", "window_key", "percent", "resets_at", "overridden", "text"}`.

    A window at or above `stop_percent` with an active override counts as `alert`. Windows whose reset time has
    passed are ignored. Nothing readable gives level "ok" with no window. `sources` limits it to those sources
    (`("codex",)` for the `@codex` line, which does not use Claude).
    """
    moment = now_value(time.time() if now is None else now)
    worst = None
    for item in _candidates(read(home), moment, sources):
        level = "stop" if item["percent"] >= cfg.stop_percent else "alert" if item["percent"] >= cfg.alert_percent else "ok"
        item["key"] = window_key(item["source"], item["name"])
        item["overridden"] = level == "stop" and override_active(item["key"], item["resets_at"] or 0, home)
        item["level"] = "alert" if item["overridden"] else level
        if worst is None or (_RANK[item["level"]], item["percent"]) > (_RANK[worst["level"]], worst["percent"]):
            worst = item
    if worst is None:
        return {
            "level": "ok",
            "window": None,
            "window_key": None,
            "percent": None,
            "resets_at": None,
            "overridden": False,
            "text": "no usage readings yet",
        }
    text = _describe(worst)
    if worst["level"] == "stop":
        text += f" (stop at {cfg.stop_percent}%; /wf-continue to override until it resets)"
    elif worst["overridden"]:
        text += " (stop overridden until it resets)"
    elif worst["level"] == "alert":
        text += f" (alert at {cfg.alert_percent}%)"
    return {
        "level": worst["level"],
        "window": f"{worst['source']} {worst['name']}",
        "window_key": worst["key"],
        "percent": worst["percent"],
        "resets_at": worst["resets_at"],
        "overridden": worst["overridden"],
        "text": text,
    }
