"""`~/.workforce/team.toml`: binaries, the Codex default model/effort and the usage thresholds."""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from workforce.config import EFFORTS
from workforce.errors import ConfigError

TEAM_TOML = "team.toml"
WORKFORCE_DIRNAME = ".workforce"
WF_HOME_ENV = "WF_HOME"

DEFAULTS = {
    "claude": "~/.local/bin/claude",
    "codex": "/opt/homebrew/bin/codex",
    "codex_model": "gpt-6-sol",
    "codex_effort": "high",
    "alert_percent": 50,
    "stop_percent": 60,
    "codex_mode": "read-only",
    "reviewer_model": "claude-opus-5-5",
    "reviewer_effort": "high",
    "fast_coder_model": "claude-sonnet-5-5",
    "fast_coder_effort": "high",
}
CODEX_MODES = ("read-only", "write")
_CLAUDE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]*")


@dataclass(frozen=True)
class TeamConfig:
    claude: str
    codex: str
    codex_model: str
    codex_effort: str
    alert_percent: int
    stop_percent: int
    codex_mode: str = DEFAULTS["codex_mode"]
    reviewer_model: str = DEFAULTS["reviewer_model"]
    reviewer_effort: str = DEFAULTS["reviewer_effort"]
    fast_coder_model: str = DEFAULTS["fast_coder_model"]
    fast_coder_effort: str = DEFAULTS["fast_coder_effort"]


def workforce_dir(home: Path | None = None) -> Path:
    """`<home>/.workforce` (`$WF_HOME`, else the real home, when `home` is None); not created here."""
    if home is None:
        home = os.environ.get(WF_HOME_ENV) or Path.home()
    return Path(home) / WORKFORCE_DIRNAME


def config_path(home: Path | None = None) -> Path:
    return workforce_dir(home) / TEAM_TOML


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` by renaming a finished temp file over it, so readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _render(values: dict) -> str:
    lines = ["# WorkForce team CLI settings (`wf`). Edit here or with /codex-model."]
    for key, value in values.items():
        lines.append(f'{key} = "{value}"' if isinstance(value, str) else f"{key} = {value}")
    return "\n".join(lines) + "\n"


def _validate_effort(effort: str) -> str:
    if effort not in EFFORTS["codex"]:
        raise ConfigError(f"codex effort '{effort}' is not valid; expected one of {list(EFFORTS['codex'])}")
    return effort


def _validate_model(model: str) -> str:
    if not isinstance(model, str) or not model.strip() or any(c.isspace() or c in '"\\' for c in model):
        raise ConfigError(f"codex model {model!r} is not a valid model name")
    return model


def _validate_mode(mode: str) -> str:
    if mode not in CODEX_MODES:
        raise ConfigError(f"codex mode '{mode}' is not valid; expected one of {list(CODEX_MODES)}")
    return mode


def _validate_claude_model(model: str) -> str:
    if not isinstance(model, str) or not _CLAUDE_MODEL.fullmatch(model):
        raise ConfigError(f"claude model {model!r} is not a valid model name")
    return model


def _validate_claude_effort(effort: str) -> str:
    if effort not in EFFORTS["claude"]:
        raise ConfigError(f"claude effort '{effort}' is not valid; expected one of {list(EFFORTS['claude'])}")
    return effort


def _from_table(table: dict, path: Path) -> TeamConfig:
    values = {**DEFAULTS, **table}
    for key in ("claude", "codex", "codex_model", "codex_effort", "codex_mode", "reviewer_model", "reviewer_effort", "fast_coder_model", "fast_coder_effort"):
        if not isinstance(values[key], str) or not values[key].strip():
            raise ConfigError(f"{path}: {key} must be a non-empty string")
    for key in ("alert_percent", "stop_percent"):
        if isinstance(values[key], bool) or not isinstance(values[key], int):
            raise ConfigError(f"{path}: {key} must be an integer")
    _validate_effort(values["codex_effort"])
    try:
        _validate_mode(values["codex_mode"])
        for role in ("reviewer", "fast_coder"):
            _validate_claude_model(values[f"{role}_model"])
            _validate_claude_effort(values[f"{role}_effort"])
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not 0 < values["alert_percent"] < values["stop_percent"] <= 100:
        raise ConfigError(f"{path}: need 0 < alert_percent < stop_percent <= 100")
    return TeamConfig(**{key: values[key] for key in DEFAULTS})


def load(home: Path | None = None) -> TeamConfig:
    """Read `team.toml`, creating it with the defaults on first use; keys missing from the file take the defaults."""
    path = config_path(home)
    if not path.exists():
        atomic_write(path, _render(DEFAULTS))
        return TeamConfig(**DEFAULTS)
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return _from_table(table, path)


def _set_key(text: str, key: str, value: str) -> str:
    line = f'{key} = "{value}"'
    pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    return text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"


def set_codex(model: str | None, effort: str | None, home: Path | None = None) -> TeamConfig:
    """Persist the Codex default model and/or effort (None leaves that one alone); the effort is validated for Codex."""
    current = load(home)
    if model is not None:
        _validate_model(model)
    if effort is not None:
        _validate_effort(effort)
    if model is None and effort is None:
        return current
    path = config_path(home)
    text = path.read_text(encoding="utf-8")
    if model is not None:
        text = _set_key(text, "codex_model", model)
    if effort is not None:
        text = _set_key(text, "codex_effort", effort)
    atomic_write(path, text)
    return load(home)


def _set_many(updates: dict[str, str], home: Path | None) -> TeamConfig:
    current = load(home)
    if not updates:
        return current
    path = config_path(home)
    text = path.read_text(encoding="utf-8")
    for key, value in updates.items():
        text = _set_key(text, key, value)
    atomic_write(path, text)
    return load(home)


def set_codex_mode(mode: str, home: Path | None = None) -> TeamConfig:
    """Persist `codex_mode` (`read-only` or `write`)."""
    return _set_many({"codex_mode": _validate_mode(mode)}, home)


def set_team_models(
    reviewer_model: str | None = None,
    reviewer_effort: str | None = None,
    fast_coder_model: str | None = None,
    fast_coder_effort: str | None = None,
    home: Path | None = None,
) -> TeamConfig:
    """Persist the sub-agent models/efforts (None leaves one alone). Everything is validated before anything is written."""
    updates: dict[str, str] = {}
    for key, value, check in (
        ("reviewer_model", reviewer_model, _validate_claude_model),
        ("reviewer_effort", reviewer_effort, _validate_claude_effort),
        ("fast_coder_model", fast_coder_model, _validate_claude_model),
        ("fast_coder_effort", fast_coder_effort, _validate_claude_effort),
    ):
        if value is not None:
            updates[key] = check(value)
    return _set_many(updates, home)


def as_dict(cfg: TeamConfig) -> dict:
    return asdict(cfg)
