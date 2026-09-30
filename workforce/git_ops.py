"""Git plumbing for WorkForce: worktrees, snapshots, diffs and fast-forward merges.

Nothing here commits, pushes, stashes, resets or rebases. Commits and rebases
belong to the Claude committer (SPEC rule 3).
"""

import os
import subprocess
import tempfile
from pathlib import Path

from workforce.errors import GitError

MANAGED_ROOT = Path.home() / ".workforce" / "worktrees"


def _run(
    repo: Path | str,
    *args: str,
    env: dict[str, str] | None = None,
    ok_codes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo), *args]
    full_env = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=full_env)
    except FileNotFoundError as exc:
        raise GitError(f"git executable not found: {exc}") from exc
    if proc.returncode not in ok_codes:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


def _out(repo: Path | str, *args: str) -> str:
    return _run(repo, *args).stdout.strip()


def current_branch(repo: Path | str) -> str:
    """Return the checked-out branch name; raise GitError if HEAD is detached."""
    proc = _run(repo, "symbolic-ref", "--quiet", "--short", "HEAD", ok_codes=(0, 1))
    if proc.returncode != 0:
        raise GitError(f"HEAD is detached in {repo}")
    return proc.stdout.strip()


def head_sha(repo: Path | str) -> str:
    """Return the full SHA of HEAD."""
    return _out(repo, "rev-parse", "HEAD")


def _status_paths(repo: Path | str) -> list[str]:
    raw = _run(repo, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    entries = raw.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            if i < len(entries):
                paths.append(entries[i])
            i += 1
    return paths


def is_clean(repo: Path | str, ignore: tuple[str, ...] = (".workforce/",)) -> bool:
    """True if the working tree has no changes outside the `ignore` path prefixes."""
    for path in _status_paths(repo):
        if not any(path == p.rstrip("/") or path.startswith(p) for p in ignore):
            return False
    return True


def branch_exists(repo: Path | str, branch: str) -> bool:
    """True if a local branch named `branch` exists."""
    proc = _run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", ok_codes=(0, 1))
    return proc.returncode == 0


def create_worktree(repo: Path | str, path: Path | str, branch: str, base: str = "HEAD") -> str:
    """Create a worktree at `path` on new branch `branch` from `base`; return the base SHA."""
    base_sha = _out(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
    _run(repo, "worktree", "add", "-b", branch, str(path), base_sha)
    return base_sha


def resolve_ref(repo: Path | str, ref: str) -> str:
    """Return the commit SHA `ref` points at; raise GitError if it does not resolve to a commit."""
    proc = _run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", ok_codes=(0, 1))
    if proc.returncode != 0:
        raise GitError(f"cannot resolve {ref!r} to a commit in {repo}")
    return proc.stdout.strip()


def create_branch(repo: Path | str, name: str, start_sha: str) -> None:
    """Create branch `name` at `start_sha` without checking anything out; raise GitError if it already exists."""
    if branch_exists(repo, name):
        raise GitError(f"branch {name!r} already exists in {repo}")
    _run(repo, "branch", name, start_sha)


def add_worktree_for_branch(repo: Path | str, path: Path | str, branch: str) -> None:
    """Check out the existing `branch` in a new worktree at `path`."""
    if not branch_exists(repo, branch):
        raise GitError(f"branch {branch!r} does not exist in {repo}")
    _run(repo, "worktree", "add", str(path), branch)


def merge_ff_only_in(worktree: Path | str, branch: str) -> None:
    """Fast-forward the branch checked out in a WorkForce `worktree` to `branch`; never creates a commit."""
    _run(worktree, "merge", "--ff-only", branch)


def commits_between(repo: Path | str, base: str, branch: str) -> int:
    """Number of commits reachable from `branch` but not from `base`."""
    return int(_out(repo, "rev-list", "--count", f"{base}..{branch}"))


def diff_between(repo: Path | str, base: str, branch: str) -> str:
    """Full diff of `branch` between `base` and `branch` (`base..branch`)."""
    return _run(repo, "diff", "--no-color", "--no-ext-diff", f"{base}..{branch}").stdout


def diff_stat_between(repo: Path | str, base: str, branch: str) -> str:
    """`--stat` summary between `base` and `branch`."""
    return _run(repo, "diff", "--no-color", "--no-ext-diff", "--stat", f"{base}..{branch}").stdout


def delete_branch(repo: Path | str, name: str, force: bool = False) -> None:
    """Delete a local branch: `-d` (merged only) by default, `-D` with `force`."""
    _run(repo, "branch", "-D" if force else "-d", name)


def worktree_path_free(repo: Path | str, path: Path | str) -> bool:
    """True if `git worktree add` could use `path`: nothing there (or an empty directory) and no worktree registered for it."""
    target = Path(path)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        return False
    resolved = target.resolve()
    return all(Path(entry["path"]).resolve() != resolved for entry in list_worktrees(repo))


def _is_managed_worktree(path: Path | str, managed_root: Path | str) -> bool:
    managed = Path(managed_root).resolve()
    return managed in Path(path).resolve().parents


def remove_worktree(
    repo: Path | str,
    path: Path | str,
    branch: str | None = None,
    delete_branch: bool = False,
    force_branch: bool = False,
    managed_root: Path = MANAGED_ROOT,
) -> None:
    """Remove a worktree; optionally delete its branch.

    `--force` is only used for worktrees under `managed_root`. `delete_branch`
    uses `git branch -d` (merged branches only); `force_branch=True` uses `-D`.
    """
    args = ["worktree", "remove"]
    if _is_managed_worktree(path, managed_root):
        args.append("--force")
    args.append(str(path))
    _run(repo, *args)
    if delete_branch or force_branch:
        if not branch:
            raise GitError("deleting a branch requires a branch name")
        _run(repo, "branch", "-D" if force_branch else "-d", branch)


def _temp_index_run(worktree: Path | str, fn):
    with tempfile.TemporaryDirectory(prefix="wf-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _run(worktree, "read-tree", "HEAD", env=env)
        _run(worktree, "add", "-A", env=env)
        return fn(env)


def tree_hash(worktree: Path | str) -> str:
    """Tree hash of the whole working state (tracked + untracked, non-ignored).

    Computed against a temporary index so the real index is never touched.
    """
    return _temp_index_run(worktree, lambda env: _run(worktree, "write-tree", env=env).stdout.strip())


def diff_against(worktree: Path | str, base_sha: str) -> str:
    """Full diff of the working state (including untracked files) versus `base_sha`."""
    return _temp_index_run(
        worktree,
        lambda env: _run(
            worktree, "diff", "--cached", "--no-color", "--no-ext-diff", base_sha, env=env
        ).stdout,
    )


def diff_stat(worktree: Path | str, base_sha: str) -> str:
    """`--stat` summary of the working state versus `base_sha`."""
    return _temp_index_run(
        worktree,
        lambda env: _run(
            worktree, "diff", "--cached", "--no-color", "--no-ext-diff", "--stat", base_sha, env=env
        ).stdout,
    )


def changed_files(worktree: Path | str, base_sha: str) -> list[str]:
    """Paths changed in the working state versus `base_sha`."""
    out = _temp_index_run(
        worktree,
        lambda env: _run(
            worktree, "diff", "--cached", "--name-only", "-z", "--no-renames", base_sha, env=env
        ).stdout,
    )
    return [p for p in out.split("\0") if p]


def commit_tree(worktree: Path | str, ref: str = "HEAD") -> str:
    """Tree hash of the commit at `ref`."""
    return _out(worktree, "rev-parse", f"{ref}^{{tree}}")


def can_fast_forward(repo: Path | str, branch: str) -> bool:
    """True if HEAD of `repo` is an ancestor of `branch` (so `--ff-only` would succeed)."""
    proc = _run(repo, "merge-base", "--is-ancestor", "HEAD", branch, ok_codes=(0, 1))
    return proc.returncode == 0


def merge_ff_only(repo: Path | str, branch: str) -> None:
    """Fast-forward the current branch of `repo` to `branch`; never creates a commit."""
    _run(repo, "merge", "--ff-only", branch)


def list_worktrees(repo: Path | str) -> list[dict]:
    """Parse `git worktree list --porcelain` into dicts with path/head/branch/bare/detached."""
    raw = _run(repo, "worktree", "list", "--porcelain").stdout
    result: list[dict] = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        entry: dict = {"path": None, "head": None, "branch": None, "bare": False, "detached": False}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                entry["path"] = value
            elif key == "HEAD":
                entry["head"] = value
            elif key == "branch":
                entry["branch"] = value.removeprefix("refs/heads/")
            elif key == "bare":
                entry["bare"] = True
            elif key == "detached":
                entry["detached"] = True
        result.append(entry)
    return result


def untracked_paths(worktree: Path | str) -> set[str]:
    """Untracked, non-ignored paths in the worktree; whole new directories are reported once with a trailing slash."""
    out = _run(worktree, "ls-files", "--others", "--exclude-standard", "--directory", "-z").stdout
    return {p for p in out.split("\0") if p}


def exclude_locally(worktree: Path | str, paths: set[str] | list[str]) -> list[str]:
    """Append paths to the repository's shared `info/exclude` (never to a committed .gitignore); return what was added."""
    exclude = Path(_out(worktree, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = Path(worktree) / exclude
    existing = set(exclude.read_text().splitlines()) if exclude.exists() else set()
    added = [f"/{p.lstrip('/')}" for p in sorted(paths) if f"/{p.lstrip('/')}" not in existing]
    if added:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = exclude.read_text() if exclude.exists() else ""
        prefix = "" if not text or text.endswith("\n") else "\n"
        with exclude.open("a") as handle:
            handle.write(prefix + "".join(f"{line}\n" for line in added))
    return added


def run_excluding_new_untracked(worktree: Path | str, fn):
    """Call fn(); any untracked path that appears while it runs is excluded locally. Return (fn's result, excluded paths).

    Used around committer runs, which load the user's own Claude setup: tools in that setup can drop files
    (for example `.claude-flow/`) that are not part of the approved work and must not reach a snapshot.
    """
    before = untracked_paths(worktree)
    result = fn()
    added = exclude_locally(worktree, untracked_paths(worktree) - before)
    return result, added
