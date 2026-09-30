import hashlib
import subprocess
from pathlib import Path

from workforce.errors import GitError, WorkforceError

WORKFORCE_DIR = ".workforce"
EXCLUDE_ENTRY = ".workforce/"
_EXCLUDE_SPELLINGS = {".workforce", ".workforce/", "/.workforce", "/.workforce/"}
INTEGRATION_DIR = "_integration"
CONFIG_EXCLUDE_ENTRY = "/workforce.toml"
_CONFIG_EXCLUDE_SPELLINGS = {"workforce.toml", "/workforce.toml"}


class PathEscapeError(WorkforceError):
    """A run, task or step name would resolve to a path outside the directory it belongs in."""


def _inside(root: Path, *parts: str) -> Path:
    """`root/parts...`, checked (symlinks resolved) to be strictly below `root`."""
    for part in parts:
        if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
            raise PathEscapeError(f"'{part}' is not a valid path component under {root}")
    path = root.joinpath(*parts)
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise PathEscapeError(f"{path} would be outside {root}")
    return path


def _repo_segments(repo_name: str) -> list[str]:
    """A repo name is a relative path (`app`, `tools/gamma`); each segment is checked like any other component."""
    return repo_name.split("/")


class Paths:
    """Every filesystem location WorkForce uses: state in the workspace root, worktrees under `home`.

    `workspace_root` is the folder holding `workforce.toml` and `.workforce/`; `repo` is a
    backward-compatible alias of it. `root` stays the `.workforce/` state directory.
    """

    def __init__(self, root: Path, home: Path | None = None):
        self.workspace_root = Path(root)
        self.repo = self.workspace_root
        self.home = Path(home) if home is not None else Path.home()
        self.config = self.workspace_root / "workforce.toml"
        self.root = self.workspace_root / WORKFORCE_DIR
        self.state = self.root / "state.json"
        self.lock = self.root / "state.lock"
        self.events = self.root / "events.jsonl"
        self.plan_md = self.root / "plan.md"
        self.decisions_md = self.root / "decisions.md"
        self.usage_json = self.root / "usage.json"
        self.usage_md = self.root / "usage.md"
        self.runs = self.root / "runs"
        resolved = self.workspace_root.resolve()
        digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
        self.ws_slug = f"{resolved.name}-{digest}"
        self.repo_slug = self.ws_slug
        self.worktrees_root = self.home / WORKFORCE_DIR / "worktrees" / self.ws_slug

    def run_dir(self, run_id: str) -> Path:
        return _inside(self.runs, run_id)

    def step_log(self, run_id: str, task_id: str, step: str, n: int) -> Path:
        return _inside(self.runs, run_id, task_id) / f"{step}-{n}.jsonl"

    def worktree(self, run_id: str, task_id: str, repo_name: str | None = None) -> Path:
        """One task's worktree: `<run>/<task>`, or `<run>/<repo_name>/<task>` in a workspace."""
        if task_id == INTEGRATION_DIR:
            raise PathEscapeError(f"'{task_id}' is reserved for the integration worktree")
        segments = [] if repo_name is None else _repo_segments(repo_name)
        return _inside(self.worktrees_root, run_id, *segments, task_id)

    def integration_worktree(self, run_id: str, repo_name: str) -> Path:
        """The worktree of one repo's integration branch: `<run>/<repo_name>/_integration`."""
        return _inside(self.worktrees_root, run_id, *_repo_segments(repo_name), INTEGRATION_DIR)

    def worktree_dir_for(self, run_id: str) -> Path:
        """Create (if needed) and return the directory holding one run's worktrees."""
        directory = _inside(self.worktrees_root, run_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def ensure(self) -> None:
        """Create the .workforce/ layout; git-ignore it via .git/info/exclude when the root is a git repo."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs.mkdir(exist_ok=True)
        self._exclude_from_git()

    def _exclude_from_git(self) -> None:
        exclude = self._git_exclude_path()
        if exclude is None:
            return
        existing = exclude.read_text() if exclude.exists() else ""
        lines = {line.strip() for line in existing.splitlines()}
        missing = []
        if not lines & _EXCLUDE_SPELLINGS:
            missing.append(EXCLUDE_ENTRY)
        if not lines & _CONFIG_EXCLUDE_SPELLINGS:
            missing.append(CONFIG_EXCLUDE_ENTRY)
        if not missing:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a") as handle:
            handle.write(prefix + "".join(f"{entry}\n" for entry in missing))

    def _git_exclude_path(self) -> Path | None:
        """The repo's info/exclude, or None when the workspace root is not itself a git repo top level."""
        toplevel = self._git("--show-toplevel")
        if toplevel is None or Path(toplevel).resolve() != self.workspace_root.resolve():
            return None
        exclude = self._git("--git-path", "info/exclude")
        if exclude is None:
            return None
        path = Path(exclude)
        return path if path.is_absolute() else self.workspace_root / path

    def _git(self, *args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.workspace_root), "rev-parse", *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError("git is not installed or not on PATH") from exc
        return proc.stdout.strip() if proc.returncode == 0 else None
