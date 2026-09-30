"""Claude Code status line: `WF │ Claude 5h 23% · wk 44% │ Codex wk 3% │ codex gpt-6-sol·high`.

Claude Code pipes its session JSON to stdin. The Claude usage part is written to the usage cache for the hooks and the
`usage` tool. The Codex part is read from the cache the Codex server writes; a stale cache is refreshed by a detached
background process, so this command never waits on Codex.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

CODEX_MAX_AGE_S = 120
REFRESH_FLAG = "--refresh-codex"
ATTEMPT_FILE = "codex_usage.attempt"
RESET = "\x1b[0m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
SEP = f" {DIM}│{RESET} "
UNKNOWN = "?"


def _percent(window: Any) -> float | None:
    if not isinstance(window, Mapping):
        return None
    value = window.get("used_percentage", window.get("percent"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _colour(percent: float | None, alert: int, stop: int) -> str:
    if percent is None:
        return ""
    if percent >= stop:
        return RED
    if percent >= alert:
        return YELLOW
    return ""


def _cell(label: str, percent: float | None, cfg: Any, suffix: str = "") -> str:
    text = f"{label} {round(percent)}%{suffix}" if percent is not None else f"{label} {UNKNOWN}"
    colour = _colour(percent, cfg.alert_percent, cfg.stop_percent)
    return f"{colour}{text}{RESET}" if colour else text


def claude_windows(stdin_json: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """`{"five_hour": {...}, "seven_day": {...}}` from Claude Code's `rate_limits`, with unreadable fields as None."""
    limits = stdin_json.get("rate_limits") if isinstance(stdin_json, Mapping) else None
    limits = limits if isinstance(limits, Mapping) else {}
    windows: dict[str, dict[str, Any]] = {}
    for key in ("five_hour", "seven_day"):
        raw = limits.get(key)
        raw = raw if isinstance(raw, Mapping) else {}
        resets = raw.get("resets_at")
        windows[key] = {
            "used_percentage": _percent(raw),
            "resets_at": int(resets) if isinstance(resets, (int, float)) and not isinstance(resets, bool) else None,
        }
    return windows


def codex_cells(cache: Mapping[str, Any] | None, cfg: Any, now: float) -> str:
    """`Codex 5h 3% · wk 3%` from the Codex cache; last-known values get a `~` when older than 120s."""
    entries = cache.get("codex") if isinstance(cache, Mapping) else None
    if not isinstance(entries, list) or not entries:
        return f"Codex {UNKNOWN}"
    cells = []
    for kind, label in (("5h", "5h"), ("weekly", "wk")):
        entry = next((e for e in entries if isinstance(e, Mapping) and e.get("kind") == kind), None)
        if entry is None:
            continue
        resets = entry.get("resets_at")
        if isinstance(resets, (int, float)) and not isinstance(resets, bool) and resets <= now:
            cells.append(_cell(label, None, cfg))
            continue
        read_at = entry.get("read_at")
        stale = not isinstance(read_at, (int, float)) or now - read_at > CODEX_MAX_AGE_S
        cells.append(_cell(label, _percent(entry), cfg, "~" if stale else ""))
    return "Codex " + " · ".join(cells) if cells else f"Codex {UNKNOWN}"


def render(stdin_json: Mapping[str, Any] | None, cfg: Any, cache: Mapping[str, Any] | None, now: float) -> str:
    """The one status line. Fields that are missing show `?`; nothing is ever estimated."""
    windows = claude_windows(stdin_json or {})
    claude = "Claude " + " · ".join(
        (
            _cell("5h", windows["five_hour"]["used_percentage"], cfg),
            _cell("wk", windows["seven_day"]["used_percentage"], cfg),
        )
    )
    tag = f"codex {cfg.codex_model}·{cfg.codex_effort}"
    parts = [f"{BOLD}WF{RESET}", claude, codex_cells(cache, cfg, now)]
    chat = claude_tag(stdin_json or {})
    if chat:
        parts.append(chat)
    return SEP.join(parts + [tag])


def claude_tag(stdin_json: Mapping[str, Any]) -> str | None:
    """`claude Opus 5.5·high` from the model and effort Claude Code reports, or None when it reports no model."""
    model = stdin_json.get("model") if isinstance(stdin_json.get("model"), Mapping) else {}
    name = model.get("display_name") if isinstance(model.get("display_name"), str) else None
    if not name:
        return None
    effort = stdin_json.get("effort") if isinstance(stdin_json.get("effort"), Mapping) else {}
    level = effort.get("level") if isinstance(effort.get("level"), str) else None
    return f"claude {name}·{level}" if level else f"claude {name}"


def _wf_dir(home: Path | None) -> Path:
    from workforce.team import config

    return config.workforce_dir(home)


def codex_refresh_due(home: Path | None, now: float) -> bool:
    """True when the Codex cache is missing or older than 120s and no refresh was started in the last 120s."""
    from workforce.team import usage_cache

    read_at = usage_cache.codex_read_at(home)
    if read_at is not None and now - read_at <= CODEX_MAX_AGE_S:
        return False
    try:
        return now - (_wf_dir(home) / ATTEMPT_FILE).stat().st_mtime > CODEX_MAX_AGE_S
    except OSError:
        return True


def _spawn_refresh() -> None:
    subprocess.Popen(
        [sys.executable, "-m", "workforce.team.statusline", REFRESH_FLAG],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def refresh_codex(home: Path | None = None) -> int:
    """Read Codex's usage windows (`codex app-server`, no model quota) into the cache. Runs in a detached process."""
    from workforce.team import config, usage_cache
    from workforce.usage import codex_limits

    cfg = config.load(home)
    usage_cache.write_codex(codex_limits.read(Path(cfg.codex).expanduser()), home)
    return 0


def _log_error(home: Path | None, exc: BaseException) -> None:
    try:
        wf_dir = _wf_dir(home)
        wf_dir.mkdir(parents=True, exist_ok=True)
        with open(wf_dir / "team.log", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), "source": "statusline", "error": f"{type(exc).__name__}: {exc}"}) + "\n")
    except OSError:
        pass


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    home: Path | None = None,
    now: float | None = None,
    spawn: Callable[[], None] = _spawn_refresh,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == [REFRESH_FLAG]:
        try:
            return refresh_codex(home)
        except BaseException as exc:
            _log_error(home, exc)
            return 1
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    now = time.time() if now is None else now
    try:
        from workforce.team import config, usage_cache

        try:
            payload = json.loads(stdin.read() or "{}")
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        cfg = config.load(home)
        windows = claude_windows(payload)
        if any(w["used_percentage"] is not None for w in windows.values()):
            usage_cache.write_claude(windows, home)
        cache = usage_cache.read(home)
        try:
            from workforce.team import claude_info

            claude_info.record_seen(payload, home)
        except OSError as exc:
            _log_error(home, exc)
        if codex_refresh_due(home, now):
            try:
                (_wf_dir(home)).mkdir(parents=True, exist_ok=True)
                (_wf_dir(home) / ATTEMPT_FILE).touch()
                spawn()
            except OSError as exc:
                _log_error(home, exc)
        print(render(payload, cfg, cache, now), file=stdout)
    except BaseException as exc:
        _log_error(home, exc)
        print(f"WF {DIM}│{RESET} status unavailable", file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
