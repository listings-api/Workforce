"""Find the `claude` and `codex` CLIs on this machine and read their versions."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

NPM_TIMEOUT_S = 3
VERSION_TIMEOUT_S = 5
WELL_KNOWN_DIRS = (
    "~/.local/bin",
    "~/.claude/local",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.npm-global/bin",
    "~/.bun/bin",
    "~/.volta/bin",
    "~/.cargo/bin",
)


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _expand(directory: str, home: Path | None) -> Path:
    if directory.startswith("~/") and home is not None:
        return Path(home) / directory[2:]
    return Path(directory).expanduser()


def _npm_global_bin(environ: Mapping[str, str]) -> Path | None:
    npm = shutil.which("npm", path=environ.get("PATH"))
    if not npm:
        return None
    try:
        proc = subprocess.run(
            [npm, "prefix", "-g"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=NPM_TIMEOUT_S,
            env=dict(environ),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    prefix = proc.stdout.strip()
    if proc.returncode != 0 or not prefix:
        return None
    return Path(prefix) / "bin"


def find_cli(name: str, environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path | None:
    """The first executable `name` on PATH, then in the usual install folders (and npm's global bin); None if not found."""
    environ = os.environ if environ is None else environ
    found = shutil.which(name, path=environ.get("PATH"))
    if found:
        return Path(found)
    for directory in WELL_KNOWN_DIRS:
        candidate = _expand(directory, home) / name
        if _executable(candidate):
            return candidate
    npm_bin = _npm_global_bin(environ)
    if npm_bin is not None and _executable(npm_bin / name):
        return npm_bin / name
    return None


def version(path: str | Path) -> str | None:
    """The first line of `<path> --version`, or None if it cannot be run."""
    try:
        proc = subprocess.run(
            [str(Path(path).expanduser()), "--version"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or proc.stderr).strip().splitlines()
    return lines[0].strip() if lines else None
