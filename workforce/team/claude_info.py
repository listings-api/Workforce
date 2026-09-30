"""Which Claude model and effort the chat runs with, for the banner: `--model`/`--effort`, Claude Code's settings, or what the status line last saw."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

SEEN_FILE = "claude_model.json"
ALIASES = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "fable": "Fable", "opusplan": "Opus Plan", "default": "default model"}


def display_name(model: str) -> str:
    """`claude-opus-5-5` → `Opus 5.5`, `claude-haiku-4-5-20251001` → `Haiku 4.5`, `sonnet` → `Sonnet`; anything else unchanged."""
    bare = model.strip()
    base = re.sub(r"\[.*\]$", "", bare)
    if base.lower() in ALIASES:
        return ALIASES[base.lower()]
    match = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?", base)
    if not match:
        return bare
    family, major, minor = match.groups()
    return f"{family.capitalize()} {major}{'.' + minor if minor else ''}"


def _arg(args: Sequence[str], name: str) -> str | None:
    for index, token in enumerate(args):
        if token == name and index + 1 < len(args):
            return args[index + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return None


def _settings(environ: Mapping[str, str], home: Path) -> dict[str, Any]:
    folder = Path(environ["CLAUDE_CONFIG_DIR"]).expanduser() if environ.get("CLAUDE_CONFIG_DIR") else home / ".claude"
    try:
        data = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def last_seen(wf_home: Path | None = None) -> dict[str, Any]:
    from workforce.team import config

    try:
        data = json.loads((config.workforce_dir(wf_home) / SEEN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_seen(payload: Mapping[str, Any], wf_home: Path | None = None) -> None:
    """Save the model and effort Claude Code reported to the status line, when they changed."""
    from workforce.team import config

    model = payload.get("model") if isinstance(payload.get("model"), Mapping) else {}
    effort = payload.get("effort") if isinstance(payload.get("effort"), Mapping) else {}
    seen = {
        "id": model.get("id") if isinstance(model.get("id"), str) else None,
        "display_name": model.get("display_name") if isinstance(model.get("display_name"), str) else None,
        "effort": effort.get("level") if isinstance(effort.get("level"), str) else None,
    }
    if not seen["id"] and not seen["display_name"]:
        return
    if last_seen(wf_home) == seen:
        return
    config.atomic_write(config.workforce_dir(wf_home) / SEEN_FILE, json.dumps(seen))


def banner_label(args: Sequence[str], environ: Mapping[str, str], home: Path | None = None, wf_home: Path | None = None) -> str | None:
    """`Opus 5.5 · high` for the banner, or None when nothing says which model the chat will use."""
    home = Path(home) if home is not None else Path(environ.get("HOME") or Path.home())
    settings = _settings(environ, home)
    seen = last_seen(wf_home)
    model = _arg(args, "--model") or environ.get("ANTHROPIC_MODEL") or (settings.get("model") if isinstance(settings.get("model"), str) else None)
    name = display_name(model) if model else seen.get("display_name") or (display_name(seen["id"]) if seen.get("id") else None)
    if not name:
        return None
    model_id = model or seen.get("id")
    per_model = settings.get("modelSettings") if isinstance(settings.get("modelSettings"), Mapping) else {}
    entry = per_model.get(model_id) if isinstance(model_id, str) and isinstance(per_model.get(model_id), Mapping) else {}
    effort = (
        _arg(args, "--effort")
        or (entry.get("effortLevel") if isinstance(entry.get("effortLevel"), str) else None)
        or (settings.get("effortLevel") if isinstance(settings.get("effortLevel"), str) else None)
        or (seen.get("effort") if not model or model == seen.get("id") else None)
    )
    return f"{name} · {effort}" if effort else name
