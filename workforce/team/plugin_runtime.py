"""The WorkForce Claude Code plugin: the copy packaged with `workforce`, and the runnable copy `wf` builds from it.

The packaged plugin (`workforce/team/plugin/`) knows nothing about where Python is installed: its `.mcp.json` and
`hooks/hooks.json` carry `{{python}}` / `{{python_quoted}}` placeholders. `prepare` writes a runnable copy to
`~/.workforce/plugin/` with the interpreter that is running `wf`, plus the agent files rendered from team.toml. It
only touches files whose content changes, so it is idempotent and safe when two `wf` start at once.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

from workforce.team import agents_gen, config

PLUGIN_DIRNAME = "plugin"
MANIFEST = Path(".claude-plugin") / "plugin.json"
TEAM_FILE = "TEAM.md"
GENERATED_DIR = agents_gen.AGENTS_DIRNAME
SKIP_NAMES = frozenset({"__pycache__", ".DS_Store"})
SKIP_SUFFIXES = (".pyc",)
TEMP_PREFIX = ".wf-tmp-"
STALE_TEMP_SECONDS = 300
PYTHON = "{{python}}"
PYTHON_QUOTED = "{{python_quoted}}"


class PluginMissing(RuntimeError):
    """The plugin files are not in the installed package."""


def source_dir() -> Path:
    """The plugin folder packaged inside `workforce.team`."""
    return Path(__file__).resolve().parent / PLUGIN_DIRNAME


def prepared_dir(home: Path | None = None) -> Path:
    return config.workforce_dir(home) / PLUGIN_DIRNAME


def _substitute(value, python: str):
    if isinstance(value, str):
        return value.replace(PYTHON_QUOTED, shlex.quote(python)).replace(PYTHON, python)
    if isinstance(value, list):
        return [_substitute(item, python) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, python) for key, item in value.items()}
    return value


def _source_files(source: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if not path.is_file() or SKIP_NAMES & set(relative.parts) or path.suffix in SKIP_SUFFIXES:
            continue
        if relative.parts[0] == GENERATED_DIR:
            continue
        files[relative.as_posix()] = path.read_bytes()
    return files


def desired_files(cfg: config.TeamConfig, python: str | None = None, source: Path | None = None) -> dict[str, bytes]:
    """Every file of the runnable plugin, keyed by its path inside it."""
    source = source if source is not None else source_dir()
    if not (source / MANIFEST).is_file() or not (source / TEAM_FILE).is_file():
        raise PluginMissing(
            f"the WorkForce plugin files are missing from {source} (need {MANIFEST.as_posix()} and {TEAM_FILE}). "
            "Reinstall workforce."
        )
    python = python if python is not None else sys.executable
    files = _source_files(source)
    for name, data in list(files.items()):
        if name.endswith(".json"):
            files[name] = (json.dumps(_substitute(json.loads(data), python), indent=2) + "\n").encode("utf-8")
    for name, text in agents_gen.rendered(source, cfg).items():
        files[name] = text.encode("utf-8")
    return files


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=TEMP_PREFIX)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _same(path: Path, data: bytes) -> bool:
    try:
        return path.read_bytes() == data
    except OSError:
        return False


def _is_live_temp(path: Path) -> bool:
    """A temp file another `wf` may be about to rename: left alone until it is clearly abandoned."""
    if not path.name.startswith(TEMP_PREFIX):
        return False
    try:
        return time.time() - path.stat().st_mtime < STALE_TEMP_SECONDS
    except OSError:
        return True


def _prune(target: Path, keep: set[str]) -> None:
    for path in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_dir() and not path.is_symlink():
                if not any(path.iterdir()):
                    path.rmdir()
            elif path.relative_to(target).as_posix() not in keep and not _is_live_temp(path):
                path.unlink()
        except (FileNotFoundError, OSError):
            continue


def prepare(home: Path | None = None, cfg: config.TeamConfig | None = None, python: str | None = None) -> Path:
    """Build or refresh `~/.workforce/plugin/` and return it; raises `PluginMissing` if the package lacks the plugin files."""
    cfg = cfg if cfg is not None else config.load(home)
    files = desired_files(cfg, python)
    target = prepared_dir(home)
    target.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = target / name
        if not _same(path, data):
            _write_atomic(path, data)
    _prune(target, set(files))
    return target
