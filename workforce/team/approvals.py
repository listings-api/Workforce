"""Review approvals keyed by tree hash, shared by all worktrees of a repo and signed so they cannot be forged.

Only the MCP server records approvals (after it ran a reviewer itself). Each entry carries an HMAC-SHA256 signature made
with the key in `<home>/.workforce/team.key` (0600, created on first use); an entry without a valid signature counts as
no approval. Trees:

- `current_tree`: the whole working state (tracked and untracked), computed through a temporary index. Reviews are bound to it.
- `index_tree`: what `git commit` would record right now (`git write-tree`; writes objects only, never refs or the index).
- `stage_all_tree`: what `git add -A` (or `git commit -a`, with `update=True`) would leave in the index, simulated on a copy.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from workforce import git_ops
from workforce.errors import GitError
from workforce.team import config
from workforce.team.config import atomic_write

REVIEWERS = ("claude", "codex")
VERDICTS = ("APPROVE", "REQUEST_CHANGES", "BLOCKED")
KEEP_TREES = 50
FILENAME = "wf-approvals.json"
KEY_NAME = "team.key"
KEY_BYTES = 32


def _git(cwd: Path | str, *args: str, env: Mapping[str, str] | None = None, stdin: str | None = None) -> str:
    full = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=full, input=stdin)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise GitError(f"cannot run git in {cwd}: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def _temp_index(cwd: Path, git_env: Mapping[str, str] | None, seed: str, fn):
    """Run `fn(env)` with `GIT_INDEX_FILE` pointing at a scratch index seeded from `seed` ("HEAD" or a copy of the real one)."""
    with tempfile.TemporaryDirectory(prefix="wf-index-") as tmp:
        path = Path(tmp) / "index"
        env = {**(git_env or {}), "GIT_INDEX_FILE": str(path)}
        if seed == "HEAD":
            _git(cwd, "read-tree", "HEAD", env=env)
        else:
            real = Path(cwd) / _git(cwd, "rev-parse", "--git-path", "index", env=git_env)
            if real.is_file():
                shutil.copyfile(real, path)
        return fn(env)


def current_tree(cwd: Path, git_env: Mapping[str, str] | None = None) -> str:
    """Tree hash of the whole working state (tracked and untracked), computed without touching the real index."""
    if not git_env:
        return git_ops.tree_hash(cwd)

    def build(env):
        _git(cwd, "add", "-A", env=env)
        return _git(cwd, "write-tree", env=env)

    return _temp_index(Path(cwd), git_env, "HEAD", build)


def index_tree(cwd: Path, git_env: Mapping[str, str] | None = None) -> str:
    """The tree `git commit` (without `-a` or paths) would record right now: `git write-tree` of the real index."""
    return _git(cwd, "write-tree", env=git_env)


def stage_all_tree(cwd: Path, git_env: Mapping[str, str] | None = None, update: bool = False) -> str:
    """The index tree after `git add -A` (or, with `update`, after `git add -u` / `git commit -a`), simulated on a copy of the index."""

    def build(env):
        _git(cwd, "add", "-u" if update else "-A", env=env)
        return _git(cwd, "write-tree", env=env)

    return _temp_index(Path(cwd), git_env, "index", build)


def approvals_path(cwd: Path, git_env: Mapping[str, str] | None = None) -> Path:
    """`<git common dir>/wf-approvals.json`: the same file from the main checkout and every linked worktree."""
    out = _git(cwd, "rev-parse", "--git-common-dir", env=git_env)
    return (Path(cwd) / out).resolve() / FILENAME


def key_path(home: Path | None = None) -> Path:
    return config.workforce_dir(home) / KEY_NAME


def load_key(home: Path | None = None, create: bool = True) -> bytes | None:
    """The HMAC key, created (0600) on first use. Concurrent first uses converge on one key.

    With `create=False` (verifying only) a missing key gives None and nothing is written.
    """
    path = key_path(home)
    try:
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pass
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = secrets.token_hex(KEY_BYTES)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        text = path.read_text(encoding="ascii").strip()
        return bytes.fromhex(text)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(fresh + "\n")
    return bytes.fromhex(fresh)


def _canonical(tree: str, reviewer: str, entry: Mapping) -> bytes:
    body = {
        "tree": tree,
        "reviewer": reviewer,
        "verdict": entry.get("verdict"),
        "summary": entry.get("summary"),
        "findings": entry.get("findings"),
        "at": entry.get("at"),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sign(key: bytes, tree: str, reviewer: str, entry: Mapping) -> str:
    return hmac.new(key, _canonical(tree, reviewer, entry), hashlib.sha256).hexdigest()


def verified(key: bytes | None, tree: str, reviewer: str, entry: object) -> bool:
    """True when `entry` carries a signature made with `key` for exactly this tree, reviewer and content."""
    if not isinstance(entry, dict):
        return False
    signature = entry.get("sig")
    return key is not None and isinstance(signature, str) and hmac.compare_digest(signature, sign(key, tree, reviewer, entry))


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def record(
    cwd: Path,
    reviewer: str,
    verdict: str,
    summary: str,
    findings: list | None = None,
    tree: str | None = None,
    home: Path | None = None,
    git_env: Mapping[str, str] | None = None,
) -> dict:
    """Store `reviewer`'s signed verdict for `tree` (default: the current tree); returns the stored entry plus tree and reviewer.

    Only the MCP server calls this, after it ran the reviewer itself.
    """
    if reviewer not in REVIEWERS:
        raise ValueError(f"reviewer must be one of {list(REVIEWERS)}, got {reviewer!r}")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {list(VERDICTS)}, got {verdict!r}")
    if findings is not None and not isinstance(findings, list):
        raise ValueError("findings must be a list")
    tree = tree or current_tree(cwd, git_env)
    entry = {
        "verdict": verdict,
        "summary": summary,
        "findings": list(findings or []),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    entry["sig"] = sign(load_key(home), tree, reviewer, entry)
    path = approvals_path(cwd, git_env)
    with _locked(path):
        data = _read(path)
        per_tree = data.pop(tree, {})
        per_tree[reviewer] = entry
        data[tree] = per_tree
        while len(data) > KEEP_TREES:
            data.pop(next(iter(data)))
        atomic_write(path, json.dumps(data, indent=2))
    return {"tree": tree, "reviewer": reviewer, **entry}


def status(
    cwd: Path,
    home: Path | None = None,
    tree: str | None = None,
    git_env: Mapping[str, str] | None = None,
) -> dict:
    """Both verdicts for `tree` (default: the current working-state tree), whether a commit is allowed, and what is missing.

    An entry whose signature does not verify is ignored and reported in `missing`.
    """
    tree = tree or current_tree(cwd, git_env)
    per_tree = _read(approvals_path(cwd, git_env)).get(tree, {})
    key = load_key(home, create=False)
    result: dict = {"tree": tree}
    missing: list[str] = []
    for reviewer in REVIEWERS:
        entry = per_tree.get(reviewer)
        if entry is not None and not verified(key, tree, reviewer, entry):
            result[reviewer] = None
            missing.append(f"{reviewer}: an approval entry with a bad signature was ignored (only the WorkForce server can record reviews: claude_review / codex_review)")
        elif entry is None:
            result[reviewer] = None
            missing.append(f"{reviewer}: no review of the current changes")
        else:
            result[reviewer] = entry
            if entry.get("verdict") != "APPROVE":
                missing.append(f"{reviewer}: {entry.get('verdict')} (needs APPROVE)")
    result["commit_allowed"] = not missing
    result["missing"] = missing
    return result
