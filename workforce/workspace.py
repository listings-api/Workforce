"""Find the workspace WorkForce runs in: a folder of git repos, or a single repo."""

import dataclasses
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from workforce import config as config_mod
from workforce.config import CONFIG_NAME, Checks, Config
from workforce.errors import ConfigError, WorkforceError

SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", "vendor", "dist", "build"})
SLUG_MAX = 40
SLUG_FALLBACK = "run"


class WorkspaceError(WorkforceError):
    """The workspace or one of its repos cannot be resolved."""


class NoWorkspace(WorkspaceError):
    """`cwd` is not inside a git repo or a workspace."""

    def __init__(self, cwd: Path):
        self.cwd = Path(cwd)
        super().__init__(f"{cwd} is not inside a git repo or a workspace")


class NotInitialized(WorkspaceError):
    """`root` holds git repos but no `workforce.toml`; `wf init` sets it up."""

    def __init__(self, root: Path, found: list["RepoRef"]):
        self.root = Path(root)
        self.found = list(found)
        names = ", ".join(ref.name for ref in self.found)
        super().__init__(
            f"This folder has {len(self.found)} git repos ({names}). Run wf init to set it up as a workspace."
        )


@dataclass(frozen=True)
class RepoRef:
    name: str
    path: Path


@dataclass(frozen=True)
class RepoInfo:
    name: str
    path: Path
    base: str | None
    checks: Checks


@dataclass(frozen=True)
class Workspace:
    root: Path
    repos: dict[str, RepoInfo]
    focus: str | None
    single: bool

    def repo(self, name: str) -> RepoInfo:
        try:
            return self.repos[name]
        except KeyError:
            raise WorkspaceError(f"no repo '{name}' in this workspace; valid repos: {sorted(self.repos)}") from None


def _is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _children(path: Path) -> list[Path]:
    try:
        entries = sorted(path.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith(".") and entry.name not in SKIP_DIRS
    ]


@dataclass(frozen=True)
class SkippedWorktree:
    name: str
    path: Path
    of: str


def git_common_dir(path: Path) -> Path | None:
    """The shared `.git` directory of the repository `path` belongs to (the same for all its linked worktrees)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.strip()
    return Path(out).resolve() if proc.returncode == 0 and out else None


def _is_main_checkout(path: Path) -> bool:
    return (path / ".git").is_dir()


def _scan(root: Path) -> list[RepoRef]:
    found: list[RepoRef] = []
    for child in _children(root):
        if _is_repo(child):
            found.append(RepoRef(child.name, child))
            continue
        for grandchild in _children(child):
            if _is_repo(grandchild):
                found.append(RepoRef(f"{child.name}/{grandchild.name}", grandchild))
    return sorted(found, key=lambda ref: ref.name)


def discover_with_skipped(root: Path) -> tuple[list[RepoRef], list[SkippedWorktree]]:
    """Repos below `root`, one per underlying git repository, plus the linked worktrees that were folded into them.

    Linked worktrees share branches with their repository, so listing both would make two workspace repos
    fight over the same integration branch. The main checkout is kept; without one, the first worktree by name.
    """
    groups: dict[Path, list[RepoRef]] = {}
    order: list[Path] = []
    for ref in _scan(Path(root)):
        key = git_common_dir(ref.path) or ref.path.resolve()
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ref)
    kept: list[RepoRef] = []
    skipped: list[SkippedWorktree] = []
    for key in order:
        refs = groups[key]
        main = next((ref for ref in refs if _is_main_checkout(ref.path)), refs[0])
        kept.append(main)
        skipped += [SkippedWorktree(ref.name, ref.path, main.name) for ref in refs if ref is not main]
    return sorted(kept, key=lambda ref: ref.name), sorted(skipped, key=lambda s: s.name)


def discover(root: Path) -> list[RepoRef]:
    """Git repos one or two levels below `root`, one per underlying repository, sorted by name."""
    return discover_with_skipped(root)[0]


def load_workspace(root: Path, config: Config) -> Workspace:
    """Build the `Workspace` for `root` from `[workspace]` / `[repos.*]`, or single-repo mode without them."""
    root = Path(root)
    if config.workspace is None:
        if not _is_repo(root):
            raise ConfigError(
                f"{root / CONFIG_NAME} has no [workspace] section and {root} is not a git repo; "
                "add a [workspace] section listing the repos"
            )
        names = [root.resolve().name]
    else:
        names = list(config.workspace.repos)
    for name in config.repo_settings:
        if name not in names:
            raise ConfigError(f"repos.{name} does not match a repo in this workspace {names}")

    repos: dict[str, RepoInfo] = {}
    seen: dict[Path, str] = {}
    for index, name in enumerate(names):
        path = root if config.workspace is None else root / name
        key = "workspace.repos" if config.workspace is not None else "workspace"
        if not path.is_dir():
            raise ConfigError(f"{key}[{index}] '{name}' does not exist at {path}")
        if not _is_repo(path):
            raise ConfigError(f"{key}[{index}] '{name}' is not a git repo ({path} has no .git)")
        common = git_common_dir(path)
        if common is not None and common in seen:
            raise ConfigError(
                f"{key}[{index}] '{name}' and '{seen[common]}' are the same git repository (one is a worktree "
                "of the other) and would share branches; list only one of them"
            )
        if common is not None:
            seen[common] = name
        settings = config.repo_settings.get(name)
        repos[name] = RepoInfo(
            name=name,
            path=path,
            base=settings.base if settings else None,
            checks=config.repo_checks(name),
        )
    return Workspace(root=root, repos=repos, focus=None, single=config.workspace is None)


def _focus(repos: dict[str, RepoInfo], cwd: Path) -> str | None:
    best: RepoInfo | None = None
    for info in repos.values():
        resolved = info.path.resolve()
        if cwd == resolved or resolved in cwd.parents:
            if best is None or len(resolved.parts) > len(best.path.resolve().parts):
                best = info
    return best.name if best else None


def find_workspace(cwd: Path, home: Path | None = None) -> Workspace:
    """Resolve the workspace for `cwd`: the nearest `workforce.toml`, else the enclosing repo.

    `home` is accepted for parity with `Paths`; discovery never reads or writes it.
    Raises `NotInitialized` for a repo-holding folder without a config and `NoWorkspace` otherwise.
    """
    cwd = Path(cwd).resolve()
    for folder in (cwd, *cwd.parents):
        if (folder / CONFIG_NAME).is_file():
            workspace = load_workspace(folder, config_mod.load(folder))
            return dataclasses.replace(workspace, focus=_focus(workspace.repos, cwd))
    for folder in (cwd, *cwd.parents):
        if _is_repo(folder):
            info = RepoInfo(name=folder.name, path=folder, base=None, checks=_empty_checks())
            return Workspace(root=folder, repos={info.name: info}, focus=info.name, single=True)
    found = discover(cwd)
    if found:
        raise NotInitialized(cwd, found)
    raise NoWorkspace(cwd)


def _empty_checks() -> Checks:
    return Checks(test="", lint="", build="")


def slugify(goal: str, today: date) -> str:
    """`[a-z0-9-]` text of the goal (at most 40 characters) plus `-MMDD`, e.g. `reviews-webhook-0929`."""
    text = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:SLUG_MAX].strip("-")
    return f"{text or SLUG_FALLBACK}-{today:%m%d}"
