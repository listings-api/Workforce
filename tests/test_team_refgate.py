"""The `reference-transaction` check: no branch or detached HEAD reaches a commit that has no reviews, whatever git command,
flag or script moved it (`--no-verify`, `commit-tree` + `update-ref`, crafted merges), while ordinary git keeps working.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workforce.team import githook
from tests.test_team_hooks import approve, denied, home, pre  # noqa: F401  (fixtures + helpers)

ROOT = Path(__file__).resolve().parent.parent
ZERO = "0" * 40


def git_version():
    text = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


needs_merge_tree = pytest.mark.skipif(git_version() < (2, 38), reason="git merge-tree --write-tree needs git 2.38 or newer")


def git(repo, *args, env=None, check=True):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=check)
    return done.stdout.strip()


def shell(repo, command, env):
    return subprocess.run(command, shell=True, cwd=repo, env=env, capture_output=True, text=True)


def rev(repo, name="HEAD"):
    return git(repo, "rev-parse", name)


@pytest.fixture
def wf(git_repo, home):
    folder = githook.install(sys.executable, home)
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(ROOT), **githook.session_env(os.environ, folder)}
    env.pop("WF_HOME", None)
    return git_repo, env


def stage(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", "-A")


def commit_plain(repo, name, text, message):
    stage(repo, name, text)
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", message)
    return rev(repo)


def side_branch(repo, name, file, text):
    """A commit on branch `name` made outside `wf`, leaving the checkout on main."""
    git(repo, "checkout", "-q", "-b", name)
    commit = commit_plain(repo, file, text, name)
    git(repo, "checkout", "-q", "main")
    return commit


def test_no_verify_does_not_get_unreviewed_content_past_the_ref_check(wf, home):
    repo, env = wf
    head = rev(repo)
    stage(repo, "README.md", "unreviewed\n")
    done = shell(repo, "git commit --no-verify -qm sneaky", env)
    assert done.returncode != 0 and "WorkForce blocked this ref update" in done.stderr
    assert "refs/heads/main" in done.stderr and "/review" in done.stderr
    assert rev(repo) == head


def test_commit_tree_and_update_ref_are_refused_at_the_ref_update(wf, home):
    repo, env = wf
    head = rev(repo)
    stage(repo, "README.md", "unreviewed\n")
    done = shell(repo, "git update-ref refs/heads/work $(git commit-tree $(git write-tree) -p HEAD -m x)", env)
    assert done.returncode != 0 and "WorkForce blocked this ref update" in done.stderr
    assert git(repo, "for-each-ref", "refs/heads/work") == ""
    assert rev(repo) == head


def test_an_approved_commit_is_allowed_with_and_without_the_hook_being_skipped(wf, home):
    repo, env = wf
    stage(repo, "README.md", "reviewed\n")
    approve(repo, "claude", "codex")
    done = shell(repo, "git commit --no-verify -qm ok", env)
    assert done.returncode == 0, done.stderr
    stage(repo, "README.md", "reviewed again\n")
    approve(repo, "claude", "codex")
    done = shell(repo, "git commit -qm ok2", env)
    assert done.returncode == 0, done.stderr
    assert git(repo, "log", "--format=%s").splitlines()[:2] == ["ok2", "ok"]


def test_approvals_for_another_base_do_not_pass_the_ref_check(wf, home):
    repo, env = wf
    old = rev(repo)
    commit_plain(repo, "a.txt", "a\n", "second")
    stage(repo, "README.md", "reviewed\n")
    from workforce.team import approvals

    for reviewer in ("claude", "codex"):
        approvals.record(repo, reviewer, "APPROVE", "ok", base=old)
    head = rev(repo)
    done = shell(repo, "git commit --no-verify -qm x", env)
    assert done.returncode != 0 and "reviewed against" in done.stderr
    assert rev(repo) == head


def test_a_root_commit_needs_its_own_approvals(tmp_path, home):
    repo = tmp_path / "fresh"
    repo.mkdir()
    folder = githook.install(sys.executable, home)
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(ROOT), **githook.session_env(os.environ, folder)}
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"]):
        git(repo, *args)
    stage(repo, "a.txt", "a\n")
    done = shell(repo, "git commit --no-verify -qm first", env)
    assert done.returncode != 0 and "WorkForce blocked this ref update" in done.stderr
    from workforce.team import approvals

    tree = git(repo, "write-tree")
    for reviewer in ("claude", "codex"):
        approvals.record(repo, reviewer, "APPROVE", "ok", tree=tree)
    done = shell(repo, "git commit --no-verify -qm first", env)
    assert done.returncode == 0, done.stderr


@needs_merge_tree
def test_merging_an_existing_local_branch_cleanly_is_allowed(wf, home):
    repo, env = wf
    side_branch(repo, "feature", "f.txt", "f\n")
    commit_plain(repo, "m.txt", "m\n", "on main")
    done = shell(repo, "git merge --no-ff -m merged feature", env)
    assert done.returncode == 0, done.stderr
    assert len(git(repo, "log", "-1", "--format=%P").split()) == 2


def test_a_crafted_two_parent_commit_with_an_arbitrary_tree_is_refused(wf, home):
    repo, env = wf
    feature = side_branch(repo, "feature", "f.txt", "f\n")
    head = rev(repo)
    stage(repo, "evil.txt", "evil\n")
    approve_free = "git update-ref refs/heads/work $(git commit-tree $(git write-tree) -p HEAD -p feature -m crafted)"
    done = shell(repo, approve_free, env)
    assert done.returncode != 0 and "WorkForce blocked this ref update" in done.stderr
    assert "merge commit" in done.stderr
    assert git(repo, "for-each-ref", "refs/heads/work") == ""
    assert rev(repo) == head and rev(repo, "feature") == feature


def test_a_merge_with_a_reviewed_tree_on_its_first_parent_is_allowed(wf, home):
    repo, env = wf
    side_branch(repo, "feature", "f.txt", "f\n")
    stage(repo, "resolved.txt", "hand-resolved\n")
    approve(repo, "claude", "codex")
    done = shell(repo, "git update-ref refs/heads/work $(git commit-tree $(git write-tree) -p HEAD -p feature -m resolved)", env)
    assert done.returncode == 0, done.stderr
    assert git(repo, "for-each-ref", "refs/heads/work") != ""


def test_fast_forwarding_to_a_commit_that_exists_on_a_remote_tracking_ref_is_allowed(wf, home):
    repo, env = wf
    git(repo, "checkout", "-q", "-b", "tmp")
    tip = commit_plain(repo, "r.txt", "r\n", "remote work")
    git(repo, "update-ref", "refs/remotes/origin/main", tip)
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-D", "tmp")
    done = shell(repo, "git merge --ff-only origin/main", env)
    assert done.returncode == 0, done.stderr
    assert rev(repo) == tip


def test_a_fetch_that_only_updates_remote_tracking_refs_is_allowed(wf, home, tmp_path):
    repo, env = wf
    origin = tmp_path / "origin"
    subprocess.run(["git", "clone", "-q", str(repo), str(origin)], check=True, capture_output=True)
    git(origin, "config", "user.email", "o@e.com")
    git(origin, "config", "user.name", "O")
    git(origin, "config", "commit.gpgsign", "false")
    tip = commit_plain(origin, "o.txt", "o\n", "from origin")
    git(repo, "remote", "add", "origin", str(origin))
    done = shell(repo, "git fetch -q origin", env)
    assert done.returncode == 0, done.stderr
    assert rev(repo, "refs/remotes/origin/main") == tip
    done = shell(repo, "git pull -q --ff-only origin main", env)
    assert done.returncode == 0, done.stderr
    assert rev(repo) == tip


def test_updates_of_other_refs_return_before_running_any_git(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("no git command should run")

    monkeypatch.setattr(githook, "_git", boom)
    updates = [
        (ZERO, "a" * 40, "refs/remotes/origin/main"),
        (ZERO, "a" * 40, "refs/tags/v1"),
        ("b" * 40, ZERO, "refs/heads/gone"),
        ("ref:refs/heads/main", "ref:refs/heads/other", "HEAD"),
        (ZERO, "a" * 40, "refs/stash"),
    ]
    assert githook.ref_transaction_problem(updates, {}) is None
    started = time.perf_counter()
    for _ in range(1000):
        githook.ref_transaction_problem(updates, {})
    assert time.perf_counter() - started < 1


def test_only_the_prepared_state_is_checked(wf, home, monkeypatch, capsys):
    repo, env = wf
    stage(repo, "README.md", "unreviewed\n")
    tree = git(repo, "write-tree")
    unreviewed = git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "x")
    line = f"{rev(repo)} {unreviewed} refs/heads/main\n"
    for state in ("committed", "aborted"):
        done = subprocess.run(
            [sys.executable, "-m", "workforce.team.githook", "reference-transaction", state],
            cwd=repo, env=env, input=line, capture_output=True, text=True,
        )
        assert done.returncode == 0, done.stderr
    done = subprocess.run(
        [sys.executable, "-m", "workforce.team.githook", "reference-transaction", "prepared"],
        cwd=repo, env=env, input=line, capture_output=True, text=True,
    )
    assert done.returncode == 1 and "WorkForce blocked this ref update" in done.stderr


def test_reset_hard_to_an_earlier_commit_is_allowed(wf, home):
    repo, env = wf
    first = rev(repo)
    commit_plain(repo, "a.txt", "a\n", "second")
    done = shell(repo, "git reset -q --hard HEAD~1", env)
    assert done.returncode == 0, done.stderr
    assert rev(repo) == first


def test_moving_between_and_deleting_branches_is_allowed(wf, home):
    repo, env = wf
    tip = side_branch(repo, "feature", "f.txt", "f\n")
    for command in ("git checkout -q feature", "git checkout -q main", "git branch other feature", "git branch -D other", "git checkout -q --detach feature", "git checkout -q main"):
        done = shell(repo, command, env)
        assert done.returncode == 0, (command, done.stderr)
    assert rev(repo, "feature") == tip


def test_a_detached_head_commit_of_unreviewed_content_is_refused(wf, home):
    repo, env = wf
    assert shell(repo, "git checkout -q --detach", env).returncode == 0
    head = rev(repo)
    stage(repo, "README.md", "unreviewed\n")
    done = shell(repo, "git commit --no-verify -qm sneaky", env)
    assert done.returncode != 0 and "WorkForce blocked this ref update" in done.stderr and "HEAD" in done.stderr
    assert rev(repo) == head


def test_a_reviewed_detached_head_commit_is_allowed(wf, home):
    repo, env = wf
    assert shell(repo, "git checkout -q --detach", env).returncode == 0
    stage(repo, "README.md", "reviewed\n")
    approve(repo, "claude", "codex")
    done = shell(repo, "git commit --no-verify -qm ok", env)
    assert done.returncode == 0, done.stderr


def test_the_repos_own_reference_transaction_hook_still_runs_and_its_failure_propagates(wf, home, tmp_path):
    repo, env = wf
    own = tmp_path / "own-hooks"
    own.mkdir()
    log = tmp_path / "ref.log"
    flag = tmp_path / "fail"
    hook = own / "reference-transaction"
    hook.write_text(f'#!/bin/sh\necho "$1" >> {log}\ncat > /dev/null\n[ -e {flag} ] && [ "$1" = prepared ] && exit 1\nexit 0\n')
    hook.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(own))
    stage(repo, "README.md", "reviewed\n")
    approve(repo, "claude", "codex")
    done = shell(repo, "git commit -qm ok", env)
    assert done.returncode == 0, done.stderr
    assert "prepared" in log.read_text().split() and "committed" in log.read_text().split()
    flag.write_text("")
    head = rev(repo)
    stage(repo, "README.md", "reviewed again\n")
    approve(repo, "claude", "codex")
    done = shell(repo, "git commit -qm no", env)
    assert done.returncode != 0
    assert rev(repo) == head


def test_the_ref_transaction_reads_the_hooks_input_and_fails_closed_on_errors(wf, home):
    repo, env = wf
    stage(repo, "README.md", "reviewed\n")
    approve(repo, "claude", "codex")
    tree = git(repo, "write-tree")
    good = git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "x")
    assert githook.parse_ref_updates(f"{rev(repo)} {good} refs/heads/main\nbad line\n") == [(rev(repo), good, "refs/heads/main")]
    old_cwd = os.getcwd()
    os.chdir(repo)
    try:
        assert githook.ref_transaction_problem([(rev(repo), good, "refs/heads/main")], env, home) is None
        problem = githook.ref_transaction_problem([(rev(repo), "f" * 40, "refs/heads/main")], env, home)
    finally:
        os.chdir(old_cwd)
    assert problem and "could not check" in problem


def test_cherry_pick_and_revert_now_need_the_reviews_and_amend_is_refused(git_repo, home):
    (git_repo / "wip.py").write_text("unstaged\n")
    for command in ("git cherry-pick abc123", "git revert HEAD", "git cherry-pick -x abc123"):
        assert "/review" in denied(pre(command, git_repo, home)), command
    subprocess.run(["git", "add", "wip.py"], cwd=git_repo, check=True)
    approve(git_repo, "claude", "codex")
    assert pre("git revert HEAD", git_repo, home) is None
    assert pre("git merge --abort", git_repo, home) is None
    for command in ("git commit --amend -m x", "git commit --amend --no-edit", "git commit -a --amend", "git commit --am -m x"):
        reason = denied(pre(command, git_repo, home))
        assert "--amend" in reason and "make a new commit instead" in reason, command
