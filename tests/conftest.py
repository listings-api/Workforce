import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_git_config(tmp_path_factory: pytest.TempPathFactory):
    """No test may read the user's global or system git config, hooks or signing setup."""
    empty = tmp_path_factory.mktemp("gitconfig") / "empty.gitconfig"
    empty.write_text("")
    patch = pytest.MonkeyPatch()
    patch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    yield empty
    patch.undo()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo on branch `main` with one initial commit.

    Signing is disabled locally in this temp repo only (test-only; never in a real repo).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo
