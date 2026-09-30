import subprocess
from pathlib import Path

import pytest

from workforce import git_ops
from workforce.errors import GitError


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)


@pytest.fixture
def managed_root(tmp_path: Path) -> Path:
    root = tmp_path / "managed" / "worktrees"
    root.mkdir(parents=True)
    return root


def make_worktree(repo: Path, root: Path, task: str = "T1") -> tuple[Path, str, str]:
    path = root / "slug" / "run1" / task
    branch = f"wf/run1/{task}"
    base = git_ops.create_worktree(repo, path, branch)
    return path, branch, base


def test_branch_and_head(git_repo):
    assert git_ops.current_branch(git_repo) == "main"
    assert git_ops.head_sha(git_repo) == git(git_repo, "rev-parse", "HEAD").strip()
    assert len(git_ops.head_sha(git_repo)) == 40


def test_current_branch_detached_raises(git_repo):
    git(git_repo, "update-ref", "--no-deref", "HEAD", git_ops.head_sha(git_repo))
    with pytest.raises(GitError):
        git_ops.current_branch(git_repo)


def test_git_error_carries_stderr(git_repo):
    with pytest.raises(GitError) as exc:
        git_ops.merge_ff_only(git_repo, "no-such-branch")
    assert "no-such-branch" in str(exc.value)


def test_is_clean_and_ignore(git_repo):
    assert git_ops.is_clean(git_repo)
    (git_repo / ".workforce").mkdir()
    (git_repo / ".workforce" / "state.json").write_text("{}")
    assert git_ops.is_clean(git_repo)
    assert not git_ops.is_clean(git_repo, ignore=())
    (git_repo / "new.txt").write_text("x")
    assert not git_ops.is_clean(git_repo)
    commit_all(git_repo, "add new")
    assert git_ops.is_clean(git_repo)
    (git_repo / "README.md").write_text("changed\n")
    assert not git_ops.is_clean(git_repo)


def test_is_clean_rename_outside_ignored_dir(git_repo):
    git(git_repo, "mv", "README.md", "DOCS.md")
    assert not git_ops.is_clean(git_repo)


def test_worktree_lifecycle(git_repo, managed_root):
    path, branch, base = make_worktree(git_repo, managed_root)
    assert base == git_ops.head_sha(git_repo)
    assert path.is_dir()
    assert git_ops.branch_exists(git_repo, branch)
    assert git_ops.current_branch(path) == branch

    listed = git_ops.list_worktrees(git_repo)
    assert len(listed) == 2
    by_branch = {w["branch"]: w for w in listed}
    assert Path(by_branch[branch]["path"]).resolve() == path.resolve()
    assert by_branch["main"]["head"] == base

    (path / "dirty.txt").write_text("uncommitted")
    git_ops.remove_worktree(git_repo, path, branch=branch, delete_branch=True, managed_root=managed_root)
    assert not path.exists()
    assert not git_ops.branch_exists(git_repo, branch)
    assert len(git_ops.list_worktrees(git_repo)) == 1


def test_remove_worktree_keeps_branch_by_default(git_repo, managed_root):
    path, branch, _ = make_worktree(git_repo, managed_root)
    git_ops.remove_worktree(git_repo, path, managed_root=managed_root)
    assert git_ops.branch_exists(git_repo, branch)


def test_remove_worktree_no_force_outside_managed_root(git_repo, managed_root, tmp_path):
    path = tmp_path / "elsewhere"
    git_ops.create_worktree(git_repo, path, "wf/other")
    (path / "dirty.txt").write_text("uncommitted")
    git(path, "add", "dirty.txt")
    with pytest.raises(GitError):
        git_ops.remove_worktree(git_repo, path, managed_root=managed_root)
    assert path.exists()


def test_remove_worktree_force_only_under_managed_root(git_repo, managed_root):
    path, _, _ = make_worktree(git_repo, managed_root)
    (path / "dirty.txt").write_text("uncommitted")
    git(path, "add", "dirty.txt")
    git_ops.remove_worktree(git_repo, path, managed_root=managed_root)
    assert not path.exists()


def test_managed_root_default_is_home_workforce():
    assert git_ops.MANAGED_ROOT == Path.home() / ".workforce" / "worktrees"


def test_delete_branch_refuses_unmerged(git_repo, managed_root):
    path, branch, _ = make_worktree(git_repo, managed_root)
    (path / "f.txt").write_text("f\n")
    commit_all(path, "unmerged work")
    with pytest.raises(GitError):
        git_ops.remove_worktree(
            git_repo, path, branch=branch, delete_branch=True, managed_root=managed_root
        )
    assert git_ops.branch_exists(git_repo, branch)


def test_delete_branch_allowed_when_merged(git_repo, managed_root):
    path, branch, _ = make_worktree(git_repo, managed_root)
    (path / "f.txt").write_text("f\n")
    commit_all(path, "work")
    git_ops.merge_ff_only(git_repo, branch)
    git_ops.remove_worktree(
        git_repo, path, branch=branch, delete_branch=True, managed_root=managed_root
    )
    assert not git_ops.branch_exists(git_repo, branch)


def test_force_branch_deletes_unmerged(git_repo, managed_root):
    path, branch, _ = make_worktree(git_repo, managed_root)
    (path / "f.txt").write_text("f\n")
    commit_all(path, "unmerged work")
    git_ops.remove_worktree(
        git_repo, path, branch=branch, force_branch=True, managed_root=managed_root
    )
    assert not git_ops.branch_exists(git_repo, branch)


def test_delete_branch_requires_name(git_repo, managed_root):
    path, _, _ = make_worktree(git_repo, managed_root)
    with pytest.raises(GitError):
        git_ops.remove_worktree(git_repo, path, delete_branch=True, managed_root=managed_root)


def test_create_worktree_existing_branch_fails(git_repo, managed_root):
    make_worktree(git_repo, managed_root)
    with pytest.raises(GitError):
        git_ops.create_worktree(git_repo, managed_root / "again", "wf/run1/T1")


def test_tree_hash_stable_and_matches_head_when_clean(git_repo):
    first = git_ops.tree_hash(git_repo)
    assert first == git_ops.tree_hash(git_repo)
    assert first == git_ops.commit_tree(git_repo)


def test_tree_hash_changes_with_edits_and_untracked(git_repo):
    clean = git_ops.tree_hash(git_repo)
    (git_repo / "README.md").write_text("edited\n")
    edited = git_ops.tree_hash(git_repo)
    assert edited != clean
    (git_repo / "untracked.txt").write_text("new\n")
    with_untracked = git_ops.tree_hash(git_repo)
    assert with_untracked not in (clean, edited)
    (git_repo / "untracked.txt").unlink()
    assert git_ops.tree_hash(git_repo) == edited


def test_tree_hash_respects_gitignore(git_repo):
    (git_repo / ".gitignore").write_text("ignored.txt\n")
    commit_all(git_repo, "ignore")
    before = git_ops.tree_hash(git_repo)
    (git_repo / "ignored.txt").write_text("secret")
    assert git_ops.tree_hash(git_repo) == before


def test_tree_hash_leaves_real_index_untouched(git_repo):
    (git_repo / "README.md").write_text("edited\n")
    (git_repo / "untracked.txt").write_text("new\n")
    git(git_repo, "add", "README.md")
    status_before = git(git_repo, "status", "--porcelain")
    staged_before = git(git_repo, "diff", "--cached")
    index_before = (git_repo / ".git" / "index").read_bytes()

    git_ops.tree_hash(git_repo)
    git_ops.diff_against(git_repo, git_ops.head_sha(git_repo))
    git_ops.changed_files(git_repo, git_ops.head_sha(git_repo))

    assert git(git_repo, "status", "--porcelain") == status_before
    assert git(git_repo, "diff", "--cached") == staged_before
    assert (git_repo / ".git" / "index").read_bytes() == index_before
    assert "untracked.txt" in status_before


def test_tree_hash_clean_repo_index_stays_empty_of_changes(git_repo):
    (git_repo / "untracked.txt").write_text("new\n")
    git_ops.tree_hash(git_repo)
    assert git(git_repo, "diff", "--cached") == ""
    assert git(git_repo, "status", "--porcelain").strip() == "?? untracked.txt"


def test_tree_hash_in_worktree_matches_committed_tree(git_repo, managed_root):
    path, branch, base = make_worktree(git_repo, managed_root)
    (path / "feature.py").write_text("print('hi')\n")
    (path / "README.md").write_text("changed\n")
    snap = git_ops.tree_hash(path)
    assert git_ops.is_clean(git_repo)
    assert git(path, "diff", "--cached") == ""

    commit_all(path, "work")
    assert git_ops.commit_tree(path) == snap
    assert git_ops.commit_tree(git_repo, branch) == snap


def test_diff_includes_untracked_and_modified(git_repo):
    base = git_ops.head_sha(git_repo)
    (git_repo / "README.md").write_text("hello\nmore\n")
    (git_repo / "brand_new.py").write_text("x = 1\n")
    diff = git_ops.diff_against(git_repo, base)
    assert "brand_new.py" in diff
    assert "+x = 1" in diff
    assert "+more" in diff
    stat = git_ops.diff_stat(git_repo, base)
    assert "brand_new.py" in stat and "README.md" in stat
    assert sorted(git_ops.changed_files(git_repo, base)) == ["README.md", "brand_new.py"]


def test_diff_empty_when_nothing_changed(git_repo):
    base = git_ops.head_sha(git_repo)
    assert git_ops.diff_against(git_repo, base) == ""
    assert git_ops.changed_files(git_repo, base) == []


def test_diff_covers_deletions_and_committed_changes(git_repo, managed_root):
    path, _, base = make_worktree(git_repo, managed_root)
    (path / "a.txt").write_text("a\n")
    commit_all(path, "add a")
    (path / "README.md").unlink()
    diff = git_ops.diff_against(path, base)
    assert "a.txt" in diff and "README.md" in diff
    assert sorted(git_ops.changed_files(path, base)) == ["README.md", "a.txt"]


def test_fast_forward_detection_and_merge(git_repo, managed_root):
    path, branch, base = make_worktree(git_repo, managed_root)
    assert git_ops.can_fast_forward(git_repo, branch)

    (path / "f.txt").write_text("f\n")
    commit_all(path, "work")
    assert git_ops.can_fast_forward(git_repo, branch)

    git_ops.merge_ff_only(git_repo, branch)
    assert git_ops.head_sha(git_repo) == git_ops.head_sha(path)
    assert (git_repo / "f.txt").exists()


def test_cannot_fast_forward_when_base_moved(git_repo, managed_root):
    path, branch, _ = make_worktree(git_repo, managed_root)
    (path / "f.txt").write_text("f\n")
    commit_all(path, "work")

    (git_repo / "other.txt").write_text("o\n")
    commit_all(git_repo, "main moved")

    assert not git_ops.can_fast_forward(git_repo, branch)
    head_before = git_ops.head_sha(git_repo)
    with pytest.raises(GitError):
        git_ops.merge_ff_only(git_repo, branch)
    assert git_ops.head_sha(git_repo) == head_before


def test_branch_exists(git_repo):
    assert git_ops.branch_exists(git_repo, "main")
    assert not git_ops.branch_exists(git_repo, "nope")


def snapshot(repo: Path) -> dict:
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "symbolic-ref", "HEAD"),
        "status": git(repo, "status", "--porcelain=v2", "--untracked-files=all"),
        "index": git(repo, "ls-files", "-s"),
        "readme": (repo / "README.md").read_text(),
        "staged": (repo / "staged.txt").read_text(),
        "untracked": (repo / "scratch.txt").read_text(),
    }


@pytest.fixture
def dirty_repo(git_repo):
    (git_repo / "README.md").write_text("hello\nlocal edit\n")
    (git_repo / "staged.txt").write_text("staged\n")
    git(git_repo, "add", "staged.txt")
    (git_repo / "scratch.txt").write_text("untracked\n")
    return git_repo


def test_resolve_ref(dirty_repo):
    before = snapshot(dirty_repo)
    sha = git_ops.resolve_ref(dirty_repo, "main")
    assert sha == before["head"].strip() and len(sha) == 40
    assert git_ops.resolve_ref(dirty_repo, "HEAD") == sha
    assert git_ops.resolve_ref(dirty_repo, sha) == sha
    with pytest.raises(GitError, match="nope"):
        git_ops.resolve_ref(dirty_repo, "nope")
    assert snapshot(dirty_repo) == before


def test_create_branch_does_not_check_out(dirty_repo):
    before = snapshot(dirty_repo)
    sha = git_ops.resolve_ref(dirty_repo, "main")
    git_ops.create_branch(dirty_repo, "wf/goal-0929", sha)
    assert git_ops.branch_exists(dirty_repo, "wf/goal-0929")
    assert git_ops.resolve_ref(dirty_repo, "wf/goal-0929") == sha
    assert snapshot(dirty_repo) == before


def test_create_branch_refuses_an_existing_name(dirty_repo):
    before = snapshot(dirty_repo)
    sha = git_ops.resolve_ref(dirty_repo, "main")
    with pytest.raises(GitError, match="already exists"):
        git_ops.create_branch(dirty_repo, "main", sha)
    git_ops.create_branch(dirty_repo, "wf/x", sha)
    with pytest.raises(GitError, match="already exists"):
        git_ops.create_branch(dirty_repo, "wf/x", sha)
    assert snapshot(dirty_repo) == before


def test_add_worktree_for_branch(dirty_repo, managed_root):
    before = snapshot(dirty_repo)
    git_ops.create_branch(dirty_repo, "wf/goal", git_ops.resolve_ref(dirty_repo, "main"))
    path = managed_root / "slug" / "run1" / "app" / "_integration"
    git_ops.add_worktree_for_branch(dirty_repo, path, "wf/goal")
    assert (path / "README.md").read_text() == "hello\n"
    assert git(path, "symbolic-ref", "--short", "HEAD").strip() == "wf/goal"
    assert path.resolve() in [Path(w["path"]).resolve() for w in git_ops.list_worktrees(dirty_repo)]
    assert snapshot(dirty_repo) == before


def test_add_worktree_for_branch_needs_an_existing_branch(dirty_repo, managed_root):
    before = snapshot(dirty_repo)
    with pytest.raises(GitError, match="does not exist"):
        git_ops.add_worktree_for_branch(dirty_repo, managed_root / "wt", "wf/missing")
    assert not (managed_root / "wt").exists()
    assert snapshot(dirty_repo) == before


def test_add_worktree_for_branch_refuses_a_branch_checked_out_in_the_user_checkout(dirty_repo, managed_root):
    before = snapshot(dirty_repo)
    with pytest.raises(GitError):
        git_ops.add_worktree_for_branch(dirty_repo, managed_root / "wt", "main")
    assert snapshot(dirty_repo) == before


def test_create_worktree_leaves_a_dirty_checkout_alone(dirty_repo, managed_root):
    before = snapshot(dirty_repo)
    path, branch, base = make_worktree(dirty_repo, managed_root)
    assert base == before["head"].strip()
    assert (path / "README.md").read_text() == "hello\n"
    assert not (path / "staged.txt").exists() and not (path / "scratch.txt").exists()
    assert snapshot(dirty_repo) == before


def build_integration(repo: Path, root: Path, branch: str = "wf/goal-0929"):
    base_sha = git_ops.resolve_ref(repo, "main")
    git_ops.create_branch(repo, branch, base_sha)
    integration = root / "slug" / "run1" / "app" / "_integration"
    git_ops.add_worktree_for_branch(repo, integration, branch)
    return base_sha, integration


def add_task_commit(repo: Path, root: Path, integration_branch: str, task: str, filename: str):
    path = root / "slug" / "run1" / "app" / task
    task_branch = f"{integration_branch}-{task}"
    git_ops.create_worktree(repo, path, task_branch, base=integration_branch)
    (path / filename).write_text(f"{task}\n")
    commit_all(path, f"add {filename}")
    return path, task_branch


def test_merge_ff_only_in_advances_the_integration_worktree_only(dirty_repo, managed_root):
    base_sha, integration = build_integration(dirty_repo, managed_root)
    _, task_branch = add_task_commit(dirty_repo, managed_root, "wf/goal-0929", "T1", "t1.txt")
    before = snapshot(dirty_repo)
    git_ops.merge_ff_only_in(integration, task_branch)
    assert (integration / "t1.txt").read_text() == "T1\n"
    assert git_ops.head_sha(integration) == git_ops.resolve_ref(dirty_repo, task_branch)
    assert git_ops.resolve_ref(dirty_repo, "main") == base_sha
    assert snapshot(dirty_repo) == before
    assert not (dirty_repo / "t1.txt").exists()


def test_merge_ff_only_in_refuses_when_it_cannot_fast_forward(dirty_repo, managed_root):
    base_sha, integration = build_integration(dirty_repo, managed_root)
    _, first = add_task_commit(dirty_repo, managed_root, "wf/goal-0929", "T1", "t1.txt")
    _, second = add_task_commit(dirty_repo, managed_root, "wf/goal-0929", "T2", "t2.txt")
    git_ops.merge_ff_only_in(integration, first)
    head = git_ops.head_sha(integration)
    before = snapshot(dirty_repo)
    with pytest.raises(GitError):
        git_ops.merge_ff_only_in(integration, second)
    assert git_ops.head_sha(integration) == head
    assert snapshot(dirty_repo) == before


def test_commits_and_diffs_between(dirty_repo, managed_root):
    base_sha, integration = build_integration(dirty_repo, managed_root)
    before = snapshot(dirty_repo)
    assert git_ops.commits_between(dirty_repo, base_sha, "wf/goal-0929") == 0
    assert git_ops.diff_between(dirty_repo, base_sha, "wf/goal-0929") == ""
    _, task_branch = add_task_commit(dirty_repo, managed_root, "wf/goal-0929", "T1", "t1.txt")
    git_ops.merge_ff_only_in(integration, task_branch)
    assert git_ops.commits_between(dirty_repo, base_sha, "wf/goal-0929") == 1
    diff = git_ops.diff_between(dirty_repo, base_sha, "wf/goal-0929")
    assert "+++ b/t1.txt" in diff and "+T1" in diff
    assert "local edit" not in diff and "staged.txt" not in diff
    stat = git_ops.diff_stat_between(dirty_repo, base_sha, "wf/goal-0929")
    assert "t1.txt" in stat and "1 file changed" in stat
    assert git_ops.commits_between(dirty_repo, "main", "main") == 0
    with pytest.raises(GitError):
        git_ops.commits_between(dirty_repo, base_sha, "nope")
    with pytest.raises(GitError):
        git_ops.diff_between(dirty_repo, base_sha, "nope")
    assert snapshot(dirty_repo) == before


def test_delete_branch_safe_and_forced(dirty_repo, managed_root):
    base_sha, integration = build_integration(dirty_repo, managed_root)
    _, task_branch = add_task_commit(dirty_repo, managed_root, "wf/goal-0929", "T1", "t1.txt")
    git_ops.remove_worktree(dirty_repo, managed_root / "slug" / "run1" / "app" / "T1", managed_root=managed_root)
    before = snapshot(dirty_repo)
    with pytest.raises(GitError):
        git_ops.delete_branch(dirty_repo, task_branch)
    assert git_ops.branch_exists(dirty_repo, task_branch)
    git_ops.delete_branch(dirty_repo, task_branch, force=True)
    assert not git_ops.branch_exists(dirty_repo, task_branch)
    git_ops.remove_worktree(dirty_repo, integration, managed_root=managed_root)
    git_ops.delete_branch(dirty_repo, "wf/goal-0929")
    assert not git_ops.branch_exists(dirty_repo, "wf/goal-0929")
    assert snapshot(dirty_repo) == before


def test_delete_branch_safe_mode_is_relative_to_the_branch_checked_out_where_it_runs(dirty_repo, managed_root):
    base_sha, integration = build_integration(dirty_repo, managed_root)
    path, task_branch = add_task_commit(dirty_repo, managed_root, "wf/goal-0929", "T1", "t1.txt")
    git_ops.merge_ff_only_in(integration, task_branch)
    git_ops.remove_worktree(dirty_repo, path, managed_root=managed_root)
    with pytest.raises(GitError):
        git_ops.delete_branch(dirty_repo, task_branch)
    git_ops.delete_branch(integration, task_branch)
    assert not git_ops.branch_exists(dirty_repo, task_branch)


def test_delete_branch_refuses_the_checked_out_branch(dirty_repo):
    before = snapshot(dirty_repo)
    with pytest.raises(GitError):
        git_ops.delete_branch(dirty_repo, "main", force=True)
    assert snapshot(dirty_repo) == before


def test_worktree_path_free(dirty_repo, managed_root, tmp_path):
    before = snapshot(dirty_repo)
    path = managed_root / "slug" / "run1" / "app" / "_integration"
    assert git_ops.worktree_path_free(dirty_repo, path)
    build_integration(dirty_repo, managed_root)
    assert not git_ops.worktree_path_free(dirty_repo, path)
    assert not git_ops.worktree_path_free(dirty_repo, dirty_repo)

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    assert git_ops.worktree_path_free(dirty_repo, occupied)
    (occupied / "file").write_text("x")
    assert not git_ops.worktree_path_free(dirty_repo, occupied)
    a_file = tmp_path / "a-file"
    a_file.write_text("x")
    assert not git_ops.worktree_path_free(dirty_repo, a_file)
    assert snapshot(dirty_repo) == before


def test_worktree_path_free_after_removal(dirty_repo, managed_root):
    _, integration = build_integration(dirty_repo, managed_root)
    git_ops.remove_worktree(dirty_repo, integration, managed_root=managed_root)
    assert git_ops.worktree_path_free(dirty_repo, integration)


def test_a_full_integration_flow_never_moves_the_users_checkout(dirty_repo, managed_root):
    before = snapshot(dirty_repo)
    base_sha, integration = build_integration(dirty_repo, managed_root)
    for task, name in (("T1", "t1.txt"), ("T2", "t2.txt")):
        path, task_branch = add_task_commit(dirty_repo, managed_root, "wf/goal-0929", task, name)
        git_ops.merge_ff_only_in(integration, task_branch)
        git_ops.remove_worktree(dirty_repo, path, managed_root=managed_root)
        git_ops.delete_branch(integration, task_branch)
        assert not git_ops.branch_exists(dirty_repo, task_branch)
    git_ops.remove_worktree(dirty_repo, integration, managed_root=managed_root)
    assert git_ops.commits_between(dirty_repo, base_sha, "wf/goal-0929") == 2
    assert git_ops.resolve_ref(dirty_repo, "main") == base_sha
    assert snapshot(dirty_repo) == before
