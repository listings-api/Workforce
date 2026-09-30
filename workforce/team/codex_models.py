"""The Codex models this ChatGPT account can use, read live from `codex app-server` (`model/list`) and cached for an hour."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CACHE_FILE = "codex_models.json"
MAX_AGE_S = 3600
TIMEOUT_S = 20


@dataclass(frozen=True)
class CodexModel:
    id: str
    name: str
    description: str
    efforts: tuple[str, ...]
    default_effort: str | None
    is_default: bool


def parse(result: Any) -> list[CodexModel]:
    """The visible models in a `model/list` result, in the order Codex lists them."""
    items = result.get("data") if isinstance(result, dict) else None
    models = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("hidden") or not isinstance(item.get("id"), str):
            continue
        efforts = []
        for entry in item.get("supportedReasoningEfforts") or []:
            value = entry.get("reasoningEffort") if isinstance(entry, dict) else entry
            if isinstance(value, str):
                efforts.append(value)
        models.append(
            CodexModel(
                id=item["id"],
                name=str(item.get("displayName") or item["id"]),
                description=str(item.get("description") or ""),
                efforts=tuple(efforts),
                default_effort=item.get("defaultReasoningEffort") if isinstance(item.get("defaultReasoningEffort"), str) else None,
                is_default=bool(item.get("isDefault")),
            )
        )
    return models


def fetch(binary: str | Path) -> list[CodexModel]:
    from workforce.usage import codex_limits

    models: list[CodexModel] = []
    cursor = None
    for _ in range(10):
        result = codex_limits.request(binary, "model/list", {"cursor": cursor} if cursor else {}, TIMEOUT_S)
        models += parse(result)
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return models


def _cache_path(home: Path | None) -> Path:
    from workforce.team import config

    return config.workforce_dir(home) / CACHE_FILE


def _read_cache(home: Path | None) -> tuple[list[CodexModel], float | None]:
    try:
        data = json.loads(_cache_path(home).read_text(encoding="utf-8"))
        models = [CodexModel(**{**m, "efforts": tuple(m["efforts"])}) for m in data["models"]]
        return models, float(data["read_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return [], None


def load(binary: str | Path, home: Path | None = None, now: float | None = None) -> tuple[list[CodexModel], str | None]:
    """(models, problem): the cached list while under an hour old, else a fresh read; the last list if a read fails."""
    from workforce.team import config

    now = time.time() if now is None else now
    cached, read_at = _read_cache(home)
    if cached and read_at is not None and now - read_at <= MAX_AGE_S:
        return cached, None
    try:
        models = fetch(Path(binary).expanduser())
    except Exception as exc:
        return cached, f"could not read the Codex model list ({type(exc).__name__}: {exc})"
    if models:
        config.atomic_write(_cache_path(home), json.dumps({"read_at": now, "models": [asdict(m) for m in models]}, indent=2))
    return models, None


def find(models: list[CodexModel], model_id: str) -> CodexModel | None:
    return next((m for m in models if m.id == model_id), None)
