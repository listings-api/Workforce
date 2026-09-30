import subprocess
from pathlib import Path

from workforce import git_ops


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def test_new_untracked_files_from_a_committer_run_are_excluded_locally(git_repo, tmp_path):
    worktree = tmp_path / "wt"
    git_ops.create_worktree(git_repo, worktree, "wf/r/T1")
    (worktree / "keep.txt").write_text("pre-existing untracked work\n")

    def committer_run():
        (worktree / ".claude-flow").mkdir()
        (worktree / ".claude-flow" / "state.json").write_text("{}")
        return "result"

    before_tree = git_ops.tree_hash(worktree)
    result, added = git_ops.run_excluding_new_untracked(worktree, committer_run)
    assert result == "result"
    assert added == ["/.claude-flow/"]
    assert git_ops.tree_hash(worktree) == before_tree
    assert "keep.txt" in git_ops.untracked_paths(worktree)
    exclude = Path(_git(worktree, "rev-parse", "--git-path", "info/exclude").strip())
    exclude = exclude if exclude.is_absolute() else worktree / exclude
    assert "/.claude-flow/" in exclude.read_text().splitlines()
    assert not (git_repo / ".gitignore").exists()


def test_exclude_locally_is_idempotent(git_repo):
    assert git_ops.exclude_locally(git_repo, {"a/"}) == ["/a/"]
    assert git_ops.exclude_locally(git_repo, {"a/"}) == []
