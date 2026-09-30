"""The hardened commit gate (reviews run by the server, signed approvals, the tree that is committed,
fail-closed parsing, the repo a command targets), MCP shutdown/cancel, the banner rules and the `@codex` usage stop.

Fakes only: the fake codex/claude, tmp homes and tmp repos. The real claude/codex are never run.
"""

import io
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_team_extras import hk  # noqa: F401  (fixture)
from tests.test_team_hooks import NOW, approve, denied, home, pre  # noqa: F401  (fixtures + helpers)
from tests.test_team_launch import Tty, run as launch_run, setup as launch_setup  # noqa: F401  (fixture)
from tests.test_team_mcp import FAKE, FAKE_CLAUDE, ROOT, env, finding, verdict  # noqa: F401  (fixtures + helpers)
from workforce.errors import GitError
from workforce.team import approvals, config, gitgate, hooks, usage_cache
from workforce.usage.claude_limits import Window


@pytest.fixture(autouse=True)
def _no_real_home(tmp_path, monkeypatch):
    """Nothing here may touch the real home: approvals sign with a key under $HOME/.workforce."""
    guard = tmp_path / "home-guard"
    guard.mkdir()
    monkeypatch.setenv("HOME", str(guard))
    monkeypatch.delenv("WF_HOME", raising=False)


def git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return done.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    for key, value in (("user.name", "T"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
        git(path, "config", key, value)
    (path / "README.md").write_text("hello\n")
    git(path, "add", "-A")
    git(path, "commit", "-m", "initial")
    return path


def post(tool, tool_input, cwd, home_dir):
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}
    return hooks.evaluate("pretooluse", payload, env={}, home=home_dir, now=NOW)


# ------------------------------------------------------------------ B1: the tree that will be committed


def test_partial_staging_with_a_stale_index_is_denied(git_repo, home):
    """The index holds an older version of `f`; the working file is what was reviewed. A plain commit records the index."""
    (git_repo / ".gitignore").write_text("*.log\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "ignore logs")
    (git_repo / "f").write_text("EVIL\n")
    (git_repo / "s.log").write_text("ignored but force-added\n")
    git(git_repo, "add", "f")
    git(git_repo, "add", "-f", "s.log")
    (git_repo / "f").write_text("good\n")
    approve(git_repo, "claude", "codex")  # both approved the working state (f=good, no s.log)
    assert approvals.index_tree(git_repo) != approvals.current_tree(git_repo)
    reason = denied(pre("git commit -m x", git_repo, home))
    assert "staged (index) tree" in reason and "Missing" in reason
    # `-a` re-stages f, but the force-added ignored file would still ride along unreviewed
    assert "index holds content" in denied(pre("git commit -am x", git_repo, home))
    assert "index holds content" in denied(pre("git add -A && git commit -m x", git_repo, home))


def test_stage_everything_in_the_same_command_compares_the_working_state(git_repo, home):
    (git_repo / "f").write_text("EVIL\n")
    git(git_repo, "add", "f")
    (git_repo / "f").write_text("good\n")
    (git_repo / "new.txt").write_text("new\n")
    approve(git_repo, "claude", "codex")
    reason = denied(pre("git commit -m x", git_repo, home))
    assert "working state is approved" in reason and "git add -A" in reason
    for command in (
        "git add -A && git commit -m x",
        "git add --all && git commit -m x",
        "git add . && git commit -m x",
        "git add -A; git commit -m x",
        "git commit -am x",
        "git commit --all -m x",
        "git add -u && git commit -m x",
    ):
        assert pre(command, git_repo, home) is None, command
    # narrower staging in the same command is not what was reviewed
    denied(pre("git add f && git commit -m x", git_repo, home))
    # after staging everything in a separate call, a plain commit records exactly the reviewed tree
    git(git_repo, "add", "-A")
    assert pre("git commit -m x", git_repo, home) is None


def test_add_dot_from_a_subfolder_is_not_a_full_stage(git_repo, home):
    (git_repo / "sub").mkdir()
    (git_repo / "sub" / "a.txt").write_text("a\n")
    (git_repo / "top.txt").write_text("top\n")
    approve(git_repo, "claude", "codex")
    assert pre("git add . && git commit -m x", git_repo, home) is None
    denied(pre(f"cd {git_repo / 'sub'} && git add . && git commit -m x", git_repo, home))


def test_commit_with_explicit_paths_is_refused(git_repo, home):
    approve(git_repo, "claude", "codex")
    for command in ("git commit -m x README.md", "git commit -m x -- README.md", "git commit -o README.md -m x", "git commit -i README.md -m x"):
        assert "explicit paths" in denied(pre(command, git_repo, home)), command
    assert pre("git commit -m 'a b c' --author='A <a@b>' -S", git_repo, home) is None
    assert pre("git commit -am 'msg' --no-edit", git_repo, home) is None


def test_index_tree_and_stage_all_tree_never_touch_the_real_index_or_refs(git_repo):
    (git_repo / "a.txt").write_text("a\n")
    (git_repo / "README.md").write_text("changed\n")
    before = (git(git_repo, "status", "--porcelain"), git(git_repo, "rev-parse", "HEAD"), git(git_repo, "for-each-ref"))
    approvals.index_tree(git_repo)
    full = approvals.stage_all_tree(git_repo)
    assert full == approvals.current_tree(git_repo)
    tracked_only = approvals.stage_all_tree(git_repo, update=True)
    assert tracked_only != full
    assert (git(git_repo, "status", "--porcelain"), git(git_repo, "rev-parse", "HEAD"), git(git_repo, "for-each-ref")) == before


# ------------------------------------------------------------------ B2: approvals cannot be forged


def test_a_forged_approvals_file_is_denied(git_repo, home):
    (git_repo / "a.txt").write_text("a\n")
    git(git_repo, "add", "-A")
    tree = approvals.index_tree(git_repo)
    now = "2026-01-01T00:00:00+00:00"
    entry = {"verdict": "APPROVE", "summary": "forged", "findings": [], "at": now}
    path = approvals.approvals_path(git_repo)
    for forged in (
        {tree: {"claude": entry, "codex": entry}},  # no signature at all
        {tree: {"claude": {**entry, "sig": "0" * 64}, "codex": {**entry, "sig": "0" * 64}}},  # made-up signature
    ):
        path.write_text(json.dumps(forged))
        reason = denied(pre("git commit -m x", git_repo, home))
        assert "bad signature" in reason and "claude_review" in reason
    assert approvals.status(git_repo)["commit_allowed"] is False


def test_a_signature_is_bound_to_its_tree_reviewer_and_content(git_repo, home):
    (git_repo / "a.txt").write_text("a\n")
    git(git_repo, "add", "-A")
    approve(git_repo, "claude", "codex")
    path = approvals.approvals_path(git_repo)
    data = json.loads(path.read_text())
    (tree,) = data
    assert pre("git commit -m x", git_repo, home) is None
    # tamper with the content of one signed entry
    tampered = json.loads(json.dumps(data))
    tampered[tree]["codex"]["verdict"] = "APPROVE"
    tampered[tree]["codex"]["summary"] = "edited after signing"
    path.write_text(json.dumps(tampered))
    denied(pre("git commit -m x", git_repo, home))
    # move signed entries of one tree onto another tree
    (git_repo / "b.txt").write_text("b\n")
    git(git_repo, "add", "-A")
    other = approvals.index_tree(git_repo)
    path.write_text(json.dumps({other: data[tree]}))
    denied(pre("git commit -m x", git_repo, home))
    # swap reviewers: a claude signature cannot stand in for codex
    path.write_text(json.dumps({other: {"claude": data[tree]["claude"], "codex": data[tree]["claude"]}}))
    denied(pre("git commit -m x", git_repo, home))
    # a rejected REQUEST_CHANGES cannot be flipped to APPROVE
    approve(git_repo, "claude", "codex", verdict="REQUEST_CHANGES")
    flipped = json.loads(path.read_text())
    for who in flipped[other]:
        flipped[other][who]["verdict"] = "APPROVE"
    path.write_text(json.dumps(flipped))
    denied(pre("git commit -m x", git_repo, home))


def test_approvals_signed_with_another_key_do_not_count(git_repo, home, tmp_path):
    (git_repo / "a.txt").write_text("a\n")
    git(git_repo, "add", "-A")
    approvals.record(git_repo, "claude", "APPROVE", "fine", home=tmp_path / "other-home")
    approvals.record(git_repo, "codex", "APPROVE", "fine", home=tmp_path / "other-home")
    denied(pre("git commit -m x", git_repo, home))


def test_the_key_is_created_once_with_mode_0600(tmp_path):
    fake_home = tmp_path / "h"
    key = approvals.load_key(fake_home)
    path = approvals.key_path(fake_home)
    assert path == fake_home / ".workforce" / "team.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert approvals.load_key(fake_home) == key and len(key) == approvals.KEY_BYTES


def test_verifying_never_creates_the_key(git_repo, home):
    denied(pre("git commit -m x", git_repo, home))
    assert approvals.status(git_repo, home)["commit_allowed"] is False
    assert not approvals.key_path(home).exists()  # only recording a review (the server) creates it


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_claude_cannot_write_the_approvals_file_with_a_file_tool(git_repo, home, tool):
    path = approvals.approvals_path(git_repo)
    for target in (path, path.parent / "wf-approvals.json.lock", ".git/wf-approvals.json"):
        tool_input = {"file_path": str(target), "content": "{}", "new_string": "{}", "old_string": "", "notebook_path": str(target)}
        assert "written only by the WorkForce server" in denied(post(tool, tool_input, git_repo, home)), (tool, target)
    edits = {"file_path": str(path), "edits": [{"file_path": str(path), "old_string": "a", "new_string": "b"}]}
    denied(post("MultiEdit", edits, git_repo, home))
    # other files, and other tools' fields, are untouched
    assert post(tool, {"file_path": str(git_repo / "README.md"), "content": "x"}, git_repo, home) is None


@pytest.mark.parametrize(
    "command",
    [
        "echo '{}' > .git/wf-approvals.json",
        "echo '{}' >> .git/wf-approvals.json",
        "cat forged.json > .git/wf-approvals.json",
        "cp forged.json .git/wf-approvals.json",
        "mv forged.json .git/wf-approvals.json",
        "tee .git/wf-approvals.json < forged.json",
        "sed -i 's/REQUEST_CHANGES/APPROVE/' .git/wf-approvals.json",
        "python3 -c \"open('.git/wf-approvals.json','w').write('{}')\"",
        "python3 -c \"open('.git/wf-'+'approvals.json','w').write('{}')\"",
        "python3 -c \"open('.git/wf-app' 'rovals.json','w')\"",
        "rm .git/wf-approvals.json",
        "ln -sf /tmp/x .git/wf-approvals.json",
        "cd .git && echo '{}' > 'wf-approvals.json'",
        "bash -c \"echo '{}' > .git/wf-approvals.json\"",
    ],
)
def test_claude_cannot_write_the_approvals_file_with_bash(git_repo, home, command):
    assert "wf-approvals.json" in denied(pre(command, git_repo, home))


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"from workforce.team import approvals; approvals.record('.', 'claude', 'APPROVE', 'x')\"",
        "python3 -c \"import workforce.team.approvals as a; a.record('.', 'codex', 'APPROVE', 'x')\"",
        "python3 -c \"import importlib; importlib.import_module('workforce.team.'+'approvals')\"",
        "python3 -c \"approvals.load_key()\"",
    ],
)
def test_claude_cannot_call_the_approvals_module_from_bash(git_repo, home, command):
    assert "approvals module" in denied(pre(command, git_repo, home))


@pytest.mark.parametrize(
    "command",
    ["cat .git/wf-approvals.json", "jq . .git/wf-approvals.json", "grep -c APPROVE .git/wf-approvals.json", "ls -l .git/wf-approvals.json"],
)
def test_reading_the_approvals_file_is_fine(git_repo, home, command):
    assert pre(command, git_repo, home) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.workforce/team.key",
        "cat $HOME/.workforce/team.key",
        "less ~/.workforce/team.key",
        "python3 -c \"print(open('/x/.workforce/team.key').read())\"",
        "python3 -c \"print(open('/x/.workforce/team'+'.key').read())\"",
        "cp ~/.workforce/team.key /tmp/k",
        "echo aa > ~/.workforce/team.key",
        "base64 < ~/.workforce/'team.key'",
        "head -c 10 ~/.workforce/te''am.key",
    ],
)
def test_the_key_can_be_neither_read_nor_written_with_bash(git_repo, home, command):
    assert "team.key" in denied(pre(command, git_repo, home))


def test_the_key_cannot_be_read_with_the_file_tools(git_repo, home):
    key = approvals.key_path(home)
    approvals.load_key(home)
    for tool, tool_input in (
        ("Read", {"file_path": str(key)}),
        ("Grep", {"pattern": ".", "path": str(key)}),
        ("Grep", {"pattern": "[0-9a-f]{64}", "path": str(key.parent)}),
        ("Glob", {"pattern": "*", "path": str(key.parent)}),
        ("Read", {"file_path": "~/.workforce/team.key"}),
        ("Write", {"file_path": str(key), "content": "0" * 64}),
        ("Edit", {"file_path": str(key), "old_string": "a", "new_string": "b"}),
    ):
        assert "approvals key" in denied(post(tool, tool_input, git_repo, home)) or "team.key" in denied(post(tool, tool_input, git_repo, home)), (tool, tool_input)
    assert post("Read", {"file_path": str(home / ".workforce" / "team.toml")}, git_repo, home) is None
    assert post("Grep", {"pattern": "x", "path": str(git_repo)}, git_repo, home) is None


def test_the_protection_fails_open_only_for_non_commit_errors(git_repo, home, monkeypatch):
    monkeypatch.setattr(gitgate, "protects_approvals", lambda command: 1 / 0)
    assert pre("ls", git_repo, home) is None
    approve(git_repo, "claude", "codex")
    assert pre("git commit -m x", git_repo, home) is None  # the protection failed, the commit gate still ran
    assert "ZeroDivisionError" in (home / ".workforce" / "team.log").read_text()


# ------------------------------------------------------------------ M2/M3/M4: history-writing commands fail closed


SHELL_COMMITS = [
    "sh -c 'git commit -m x'",
    "bash -lc \"git commit -m x\"",
    "if true; then git commit -m x; fi",
    "for i in 1; do git commit -m x; done",
    "while git commit -m x; do break; done",
    "{ git commit -m x; }",
    "! git commit -m x",
    "true && ! git commit -m x",
    "find . -maxdepth 0 -exec git commit -m x \\;",
    "eval 'git commit -m x'",
    "echo 'git commit -m x' | sh",
    "echo 'git commit -m x' | bash",
    "bash <<< 'git commit -m x'",
    "bash -c \"$(echo git commit -m x)\"",
    "g=git; $g commit -m x",
    "git${IFS}commit -m x",
    "git $(echo commit) -m x",
    "$(which git) commit -m x",
    "python3 -c \"import subprocess; subprocess.run(['git','commit','-m','x'])\"",
    "sh -c 'if true; then git commit -m x; fi'",
    "x=$(git commit -m x)",
    "time git commit -m x",
    "nohup git commit -m x",
    "command git commit -m x",
    "\\git commit -m x",
    "git -c core.editor=true commit -m x",
]


@pytest.mark.parametrize("command", SHELL_COMMITS)
def test_shell_syntax_around_a_commit_is_gated(git_repo, home, command):
    reason = denied(pre(command, git_repo, home))
    assert "/review" in reason or "plain `git commit`" in reason, (command, reason)
    approve(git_repo, "claude", "codex")
    after = pre(command, git_repo, home)
    if after is not None:
        assert "plain `git commit`" in denied(after), command


@pytest.mark.parametrize(
    "command",
    ["git am patch.mbox", "git merge --continue", "git revert --continue", "git rebase --continue"],
)
def test_other_commit_like_commands_need_the_reviews(git_repo, home, command):
    if "rebase" in command:
        assert "outside the review gate" in denied(pre(command, git_repo, home))
        return
    assert "/review" in denied(pre(command, git_repo, home))
    approve(git_repo, "claude", "codex")
    assert pre(command, git_repo, home) is None


@pytest.mark.parametrize(
    "command",
    ["git merge origin/master", "git merge --no-ff feature", "git cherry-pick abc123", "git revert HEAD", "git pull origin master", "git merge --abort"],
)
def test_merging_existing_commits_into_a_clean_index_needs_no_review(git_repo, home, command):
    (git_repo / "wip.py").write_text("unstaged\n")
    assert pre(command, git_repo, home) is None


@pytest.mark.parametrize("command", ["git merge feature", "git cherry-pick abc123", "git revert HEAD", "git add -A && git merge feature"])
def test_merge_with_staged_changes_needs_the_reviews(git_repo, home, command):
    (git_repo / "wip.py").write_text("staged\n")
    subprocess.run(["git", "add", "wip.py"], cwd=git_repo, check=True)
    assert "/review" in denied(pre(command, git_repo, home))
    approve(git_repo, "claude", "codex")
    assert pre(command, git_repo, home) is None


@pytest.mark.parametrize("command", ["git merge --continue", "git cherry-pick --continue", "git am patch.mbox"])
def test_conflict_resolutions_and_patches_always_need_the_reviews(git_repo, home, command):
    assert "/review" in denied(pre(command, git_repo, home))


@pytest.mark.parametrize(
    "command, word",
    [
        ("git rebase main", "rebase"),
        ("git rebase -i HEAD~3", "rebase"),
        ("git filter-branch --tree-filter x", "filter-branch"),
        ("git update-ref refs/heads/main abc123", "update-ref"),
        ("git update-ref refs/heads/main $(git commit-tree HEAD^{tree} -m x)", "commit-tree"),
        ("git commit-tree HEAD^{tree} -m x", "commit-tree"),
        ("git replace abc123 def456", "replace"),
        ("git notes add -m x", "notes"),
        ("git stash", "stash"),
        ("git stash push -m wip", "stash"),
        ("git stash save wip", "stash"),
        ("sh -c 'git rebase main'", "rebase"),
        ("echo 'git rebase main' | sh", "rebase"),
        ("eval 'git update-ref refs/heads/x abc'", "update-ref"),
        ("g=git; $g stash", "stash"),
        ("git${IFS}rebase main", "rebase"),
    ],
)
def test_history_rewrites_are_refused_even_with_approvals(git_repo, home, command, word):
    approve(git_repo, "claude", "codex")
    reason = denied(pre(command, git_repo, home))
    assert reason.startswith("WorkForce blocked this command") and word in reason and "outside the review gate" in reason, reason


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff HEAD~1",
        "git log --grep=commit",
        "git log --oneline --grep 'merge commit' -5",
        "git show HEAD",
        "git show :README.md",
        "git stash list",
        "git stash show -p",
        "git stash pop",
        "git stash apply",
        "git merge --abort",
        "git rebase --abort",
        "git notes list",
        "git notes show HEAD",
        "git replace --list",
        "git commit-graph verify",
        "git cherry --verbose",
        "git blame README.md",
        "git branch --list",
        "git push origin feature",
        "git pull --ff-only",
        "git checkout -b feature",
        "echo 'git commit -m x'",
        "echo 'I am fine' && git status",
        "pytest tests/commit.py",
        "git add -A && pytest tests/commit.py",
        "git status && python fix.py --replace",
        "cat << 'EOF' > notes.md\ngit commit -m x\nEOF",
        "gh pr list",
        "gh repo view",
        "gh api repos/o/r/pulls",
    ],
)
def test_read_only_and_ordinary_uses_are_not_gated(git_repo, home, command):
    assert pre(command, git_repo, home) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/git/commits -f message=x -f tree=abc",
        "gh api -X POST repos/o/r/git/refs -f ref=refs/heads/x -f sha=abc",
        "gh api -X PUT repos/o/r/contents/README.md -f message=x -f content=eA==",
        "gh api repos/o/r/contents/README.md --method PUT --input body.json",
        "gh api repos/o/r/merges -f base=main -f head=x",
        "gh repo sync",
        "gh repo sync owner/repo --force",
        "sh -c 'gh repo sync'",
    ],
)
def test_gh_commands_that_create_commits_are_denied(git_repo, home, command):
    approve(git_repo, "claude", "codex")
    reason = denied(pre(command, git_repo, home))
    assert reason.startswith("WorkForce blocked this command") and "review gate" in reason


def test_an_alias_for_commit_is_gated(git_repo, home):
    git(git_repo, "config", "alias.ci", "commit")
    git(git_repo, "config", "alias.save", "commit -a")
    git(git_repo, "config", "alias.sh", "!git commit -m x")
    git(git_repo, "config", "alias.lg", "log --oneline")
    for command in ("git ci -m x", "git save -m x", "git sh", "git -c core.pager=cat ci -m x"):
        assert "/review" in denied(pre(command, git_repo, home)), command
    assert pre("git lg", git_repo, home) is None
    approve(git_repo, "claude", "codex")
    assert pre("git ci -m x", git_repo, home) is None


def test_the_dashed_git_commands_are_gated(git_repo, home):
    assert "/review" in denied(pre("git-commit -m x", git_repo, home))
    assert "outside the review gate" in denied(pre("git-rebase main", git_repo, home))


def test_no_verify_and_signing_stay_denied_lists_unchanged(git_repo, home):
    approve(git_repo, "claude", "codex")
    assert "signing" in denied(pre("git commit --no-gpg-sign -m x", git_repo, home))
    assert "GIT_CONFIG" in denied(pre("GIT_CONFIG_COUNT=1 git commit -m x", git_repo, home))
    assert "force" in denied(pre("git push --force", git_repo, home))


def test_unreadable_history_commands_fail_closed(home):
    out = io.StringIO()
    for raw in ('{"tool_input": {"command": "git merge feature"', '{"tool_input": {"command": "echo hi; git rebase main"'):
        out = io.StringIO()
        hooks.main(["pretooluse"], stdin=io.StringIO(raw), stdout=out, home=home)
        assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny", raw
    out = io.StringIO()
    hooks.main(["pretooluse"], stdin=io.StringIO('{"tool_input": {"command": "git status"'), stdout=out, home=home)
    assert out.getvalue() == ""


# ------------------------------------------------------------------ M1/M7: the repo the command targets


def test_dash_c_is_cumulative(tmp_path, home):
    outer = tmp_path / "work"
    approved = make_repo(outer / "a" / "b")
    unapproved = make_repo(outer / "a" / "c")
    for repo in (approved, unapproved):
        (repo / "f.txt").write_text("changed\n")
        git(repo, "add", "-A")
    approve(approved, "claude", "codex")
    assert pre("git -C a -C b commit -m x", outer, home) is None
    assert pre("git -C a/b commit -m x", outer, home) is None
    assert "could not verify" in denied(pre("git commit -m x", outer, home))  # not a repo: fails closed, clearly
    assert "not a git repository" in denied(pre("git commit -m x", outer, home))
    denied(pre("git -C a -C c commit -m x", outer, home))
    denied(pre("git -C a/c commit -m x", outer, home))
    # the abs-then-relative form: -C /abs -C rel
    assert pre(f"git -C {outer / 'a'} -C b commit -m x", tmp_path, home) is None
    denied(pre(f"git -C {outer} -C a -C c commit -m x", tmp_path, home))


def test_git_dir_work_tree_and_env_vars_pick_the_repo(git_repo, tmp_path, home):
    other = make_repo(tmp_path / "other")
    (other / "f.txt").write_text("dirty\n")
    git(other, "add", "-A")
    approve(git_repo, "claude", "codex")  # the cwd repo is approved, the target repo is not
    commands = [
        f"git --git-dir={other}/.git --work-tree={other} commit -am x",
        f"git --git-dir {other}/.git --work-tree {other} commit -m x",
        f"GIT_DIR={other}/.git GIT_WORK_TREE={other} git commit -am x",
        f"export GIT_DIR={other}/.git GIT_WORK_TREE={other}; git commit -am x",
        f"GIT_DIR={other}/.git; GIT_WORK_TREE={other}; export GIT_DIR GIT_WORK_TREE; git commit -am x",
        f"env GIT_DIR={other}/.git GIT_WORK_TREE={other} git commit -m x",
        f"git -C {other} commit -m x",
        f"cd {other} && git commit -m x",
        f"git --git-dir={other}/.git commit -m x",
        f"git --work-tree={other} --git-dir={other}/.git commit --all -m x",
    ]
    for command in commands:
        assert "/review" in denied(pre(command, git_repo, home)), command
    approve(other, "claude", "codex")
    for command in commands:
        assert pre(command, git_repo, home) is None, command
    # and the reverse: the cwd repo is not approved even though the target one is
    (git_repo / "g.txt").write_text("dirty\n")
    git(git_repo, "add", "-A")
    assert pre(f"git -C {other} commit -m x", git_repo, home) is None
    denied(pre("git commit -m x", git_repo, home))


def test_ambient_git_dir_in_the_environment_is_honoured(git_repo, tmp_path, home):
    other = make_repo(tmp_path / "other")
    (other / "f.txt").write_text("dirty\n")
    git(other, "add", "-A")
    approve(git_repo, "claude", "codex")
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(git_repo)}
    env = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}
    assert "/review" in denied(hooks.evaluate("pretooluse", payload, env=env, home=home, now=NOW))


def test_an_unresolvable_repo_directory_is_refused(git_repo, home):
    approve(git_repo, "claude", "codex")
    for command in ('git -C "$REPO" commit -m x', "cd $SOMEWHERE && git commit -m x", "GIT_DIR=$X git commit -m x"):
        assert "cannot be resolved" in denied(pre(command, git_repo, home)), command


def test_approvals_are_per_repo_and_shared_by_its_worktrees(tmp_path, home):
    one, two = make_repo(tmp_path / "one"), make_repo(tmp_path / "two")
    for repo in (one, two):
        (repo / "f.txt").write_text("same content\n")
        git(repo, "add", "-A")
    approve(one, "claude", "codex")
    assert pre("git commit -m x", one, home) is None
    denied(pre("git commit -m x", two, home))  # identical tree, but approved in another repo
    assert approvals.approvals_path(one) != approvals.approvals_path(two)
    tree = tmp_path / "wt"
    git(one, "worktree", "add", "-b", "feature", str(tree))
    (tree / "f.txt").write_text("same content\n")
    (tree / "g.txt").write_text("g\n")
    git(tree, "add", "-A")
    approve(tree, "claude", "codex")
    assert pre("git commit -m x", tree, home) is None
    assert approvals.status(one, home)["commit_allowed"] is True  # same common dir, different tree
    assert approvals.approvals_path(tree) == approvals.approvals_path(one)


def test_the_hook_uses_the_repo_of_the_commit_not_the_hook_cwd(tmp_path, home):
    folder = tmp_path / "several"
    repo = make_repo(folder / "svc")
    (repo / "f.txt").write_text("x\n")
    git(repo, "add", "-A")
    assert "/review" in denied(pre("cd svc && git commit -m x", folder, home))
    approve(repo, "claude", "codex")
    assert pre("cd svc && git commit -m x", folder, home) is None
    assert pre("git -C svc commit -m x", folder, home) is None


# ------------------------------------------------------------------ MCP: repo argument (M7)


def test_review_tools_take_a_repo_argument_for_a_subfolder_repo(env, tmp_path):
    folder = tmp_path / "several"
    svc = make_repo(folder / "svc")
    (svc / "f.txt").write_text("new\n")
    env.scenario([verdict("APPROVE", "codex fine")], claude=[verdict("APPROVE", "claude fine")])
    client = env.start(cwd=folder, project_dir=folder)
    assert "failed" in client.error_text("codex_review")  # the folder is not itself a repo
    assert "failed" in client.error_text("review_status")
    assert "not a folder" in client.error_text("review_status", {"repo": "nope"})
    body = json.loads(client.text("claude_review", {"repo": "svc"}))
    assert body["verdict"] == "APPROVE" and body["tree"] == approvals.current_tree(svc)
    json.loads(client.text("codex_review", {"repo": str(svc)}))  # absolute works too
    status = json.loads(client.text("review_status", {"repo": "svc"}))
    assert status["commit_allowed"] is True
    assert env.calls()[0]["cwd"] == str(svc.resolve()) or env.calls()[0]["cwd"].endswith("svc")
    assert env.calls("claude")[0]["cwd"].endswith("svc")
    assert approvals.approvals_path(svc).exists()


def test_codex_plan_runs_in_the_given_repo(env, tmp_path):
    folder = tmp_path / "several"
    make_repo(folder / "svc")
    env.scenario([{"text": "1. plan"}])
    client = env.start(cwd=folder, project_dir=folder)
    assert "1. plan" in client.text("codex_plan", {"task": "t", "repo": "svc"})
    argv = env.calls()[0]["argv"]
    assert argv[argv.index("-C") + 1].endswith("/svc")


def test_the_commit_gate_and_the_review_tools_agree_on_the_repo(env, tmp_path):
    folder = tmp_path / "several"
    svc = make_repo(folder / "svc")
    (svc / "f.txt").write_text("new\n")
    git(svc, "add", "-A")
    env.scenario([verdict("APPROVE", "codex fine")], claude=[verdict("APPROVE", "claude fine")])
    client = env.start(cwd=folder, project_dir=folder)
    client.text("claude_review", {"repo": "svc"})
    client.text("codex_review", {"repo": "svc"})
    # the server signed with the key under env.home (= HOME), the hook verifies with the same key
    assert pre("cd svc && git commit -m x", folder, env.home) is None
    assert pre("git -C svc commit -m x", folder, env.home) is None


# ------------------------------------------------------------------ M5: the MCP server stops its children


def pids_of(marker: str) -> list[int]:
    done = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return [int(p) for p in done.stdout.split() if int(p) != os.getpid()]


def wait_for(predicate, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return predicate()


def start_sleeping_call(env, name="codex_ask", arguments=None, request_id=41):
    env.scenario([{"sleep": 120, "text": "late"}], claude=[{"sleep": 120, **verdict()}])
    (env.repo / "app.py").write_text("changed\n")
    client = env.start()
    client.send_raw(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments or {"message": "hi"}}}))
    marker = str(FAKE if name.startswith("codex") else FAKE_CLAUDE)
    pids = wait_for(lambda: [p for p in pids_of(marker) if str(env.repo) in subprocess.run(["ps", "-o", "command=", "-p", str(p)], capture_output=True, text=True).stdout or name == "claude_review"])
    assert pids, "the fake agent never started"
    return client, marker, pids


def test_children_are_killed_when_stdin_reaches_eof(env):
    client, marker, pids = start_sleeping_call(env)
    started = time.monotonic()
    client.proc.stdin.close()
    assert client.proc.wait(timeout=15) == 0
    assert time.monotonic() - started < 10  # it did not wait for the 120s Codex run
    assert wait_for(lambda: not any(p in pids_of(marker) for p in pids), 10)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_children_are_killed_on_sigterm_and_sigint(env, signum):
    client, marker, pids = start_sleeping_call(env)
    os.kill(client.proc.pid, signum)
    assert client.proc.wait(timeout=15) == 128 + signum
    assert wait_for(lambda: not any(p in pids_of(marker) for p in pids), 10), signum


def test_a_running_claude_review_is_killed_on_eof_too(env):
    client, marker, pids = start_sleeping_call(env, "claude_review", {}, request_id=5)
    client.proc.stdin.close()
    assert client.proc.wait(timeout=15) == 0
    assert wait_for(lambda: not any(p in pids_of(marker) for p in pids), 10)


def test_notifications_cancelled_kills_that_calls_children_and_sends_no_response(env):
    client, marker, pids = start_sleeping_call(env, "codex_plan", {"task": "t"}, request_id=77)
    assert client.request("ping")["result"] == {}  # not blocked by the running call
    client.notify("notifications/cancelled", {"requestId": 77, "reason": "user cancelled"})
    assert wait_for(lambda: not any(p in pids_of(marker) for p in pids), 15), "the cancelled call's Codex is still running"
    time.sleep(0.5)
    assert client.request("ping")["result"] == {}  # the next message is the ping: no response was sent for id 77
    assert client.lines.empty()
    client.notify("notifications/cancelled", {"requestId": 12345})  # unknown ids are ignored
    client.notify("notifications/cancelled", {"requestId": {"weird": 1}})
    assert client.request("ping")["result"] == {}


def test_cancelling_one_call_leaves_the_other_running(env):
    env.scenario([{"sleep": 120, "text": "a"}, {"sleep": 3, "text": "second answer"}])
    client = env.start()
    for request_id, message in ((1, "first"), (2, "second")):
        client.send_raw(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": "codex_ask", "arguments": {"message": message}}}))
        time.sleep(1.0)
    assert wait_for(lambda: len(pids_of(str(FAKE))) >= 2)
    client.notify("notifications/cancelled", {"requestId": 1})
    reply = client.recv(timeout=30)
    assert reply["id"] == 2 and "second answer" in reply["result"]["content"][0]["text"]
    assert wait_for(lambda: not pids_of(str(env.repo)), 10)


def test_timeouts_are_20_minutes_for_reviews_and_10_for_plans_and_asks():
    from workforce.team import mcp_server

    assert mcp_server.REVIEW_TIMEOUT_S == 20 * 60 and mcp_server.ASK_TIMEOUT_S == 10 * 60


def test_tool_timeouts_reach_the_runners(env, monkeypatch):
    from workforce.team import claude_review, mcp_server, relay

    seen = []

    def fake_run_codex(cfg, project, role, prompt, model, effort, sandbox="read-only", timeout_s=0, **extra):
        seen.append((role, timeout_s))
        raise relay.CodexFailed("stop here")

    monkeypatch.setattr(relay, "run_codex", fake_run_codex)
    monkeypatch.setattr(claude_review, "run", lambda cfg, repo, prompt, timeout_s: seen.append(("claude_review", timeout_s)) or (_ for _ in ()).throw(mcp_server.WorkforceError("stop")))
    (env.repo / "app.py").write_text("x\n")
    server = mcp_server.Server(home=env.home)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(env.repo))
    for name, args in (("codex_plan", {"task": "t"}), ("codex_ask", {"message": "m"}), ("codex_review", {}), ("claude_review", {})):
        server.call(name, args)
    assert dict(seen) == {"team_plan": 600, "team_ask": 600, "team_review": 1200, "claude_review": 1200}


def test_a_tools_call_notification_gets_no_response(env):
    client = env.start()
    client.send_raw(json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "review_status", "arguments": {}}}))
    assert client.request("ping")["result"] == {}
    assert client.lines.empty()


# ------------------------------------------------------------------ M6: the banner


def test_no_banner_when_stdout_is_not_a_terminal(launch_setup):
    out = io.StringIO()
    assert launch_run(launch_setup, ["fix it"], stdout=out) == 0
    assert out.getvalue() == ""
    assert len(launch_setup[3]) == 1  # claude was still started


@pytest.mark.parametrize("flag", ["-p", "--print"])
def test_no_banner_in_print_mode_even_on_a_terminal(launch_setup, flag):
    out = Tty()
    assert launch_run(launch_setup, [flag, "hello"], stdout=out) == 0
    assert out.getvalue() == ""
    (_, argv), = launch_setup[3]
    assert argv[-2:] == [flag, "hello"]


def test_the_banner_is_still_printed_on_a_terminal(launch_setup):
    out = Tty()
    launch_run(launch_setup, ["fix the -p flag"], stdout=out)  # `-p` inside a prompt string is not print mode
    assert "WorkForce" in out.getvalue() and "\x1b[" in out.getvalue()


def test_no_color_prints_the_banner_without_ansi(launch_setup):
    out = Tty()
    launch_run(launch_setup, [], environ={"NO_COLOR": "1"}, stdout=out)
    assert "WorkForce" in out.getvalue() and "\x1b" not in out.getvalue()


def test_wf_print_mode_piped_has_clean_stdout(tmp_path):
    """`wf -p …` through a pipe: the only stdout is what claude printed."""
    home = tmp_path / "home"
    (home / ".workforce").mkdir(parents=True)
    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\necho CLAUDE-OUTPUT\n")
    stub.chmod(0o755)
    (home / ".workforce" / "team.toml").write_text(f'claude = "{stub}"\ncodex = "/x/codex"\n')
    done = subprocess.run(
        [sys.executable, "-m", "workforce.team.launch", "-p", "hi"],
        capture_output=True, text=True, timeout=60, cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "CLAUDE-OUTPUT\n"


def test_a_bad_team_toml_is_a_clear_exit_3_not_a_traceback(launch_setup, capsys):
    home = launch_setup[1]
    (home / ".workforce" / "team.toml").write_text("alert_percent = 90\nstop_percent = 10\n")
    assert launch_run(launch_setup, []) == 3
    err = capsys.readouterr().err
    assert "alert_percent" in err and "Traceback" not in err
    assert launch_setup[3] == []


def test_steering_variables_are_warned_about(launch_setup, capsys):
    assert launch_run(launch_setup, [], environ={"ANTHROPIC_BASE_URL": "http://x", "CLAUDE_CODE_USE_BEDROCK": "1"}) == 0
    err = capsys.readouterr().err
    assert "ANTHROPIC_BASE_URL" in err and "CLAUDE_CODE_USE_BEDROCK" in err
    assert len(launch_setup[3]) == 1


# ------------------------------------------------------------------ item 10: @codex respects the Codex usage windows


def put_codex_window(home_dir, percent, resets=NOW + 5000, name="weekly"):
    usage_cache.write_codex([Window("codex", name, percent, int(resets), NOW, name)], home_dir, now=NOW)


def put_claude_window(home_dir, five=None, week=None):
    usage_cache.write_claude(
        {"five_hour": {"used_percentage": five, "resets_at": int(NOW) + 3600}, "seven_day": {"used_percentage": week, "resets_at": int(NOW) + 90000}},
        home_dir, now=NOW,
    )


def test_at_codex_is_refused_at_the_codex_stop_with_the_reset_time(hk):
    put_codex_window(hk.home, 60.0)
    hk.scenario([{"text": "should not be asked"}])
    out = hk.submit("@codex are you there?")
    reason = out["reason"]
    assert out["decision"] == "block" and set(out) == {"decision", "reason"}
    assert "not sent" in reason and "codex weekly" in reason and "60%" in reason and "resets" in reason
    assert time.strftime("%a %H:%M", time.localtime(NOW + 5000)) in reason
    assert hk.calls() == []
    assert not (hk.project / ".workforce" / "team" / "codex-thread.md").exists()  # nothing was sent, nothing is logged as sent


@pytest.mark.parametrize("percent", [0.0, 49.0, 59.9])
def test_at_codex_passes_below_the_stop(hk, percent):
    put_codex_window(hk.home, percent)
    hk.scenario([{"text": "hello back"}])
    assert hk.submit("@codex hi")["reason"] == "Codex (gpt-6-sol):\nhello back"


def test_claude_windows_do_not_stop_at_codex(hk):
    put_claude_window(hk.home, five=99, week=95)
    hk.scenario([{"text": "still answers"}])
    assert hk.submit("@codex hi")["reason"] == "Codex (gpt-6-sol):\nstill answers"
    assert hk.submit("a normal prompt")["decision"] == "block"  # but the normal prompt is stopped by Claude's window


def test_wf_continue_overrides_the_codex_stop_for_that_window_only(hk):
    put_codex_window(hk.home, 80.0)
    hk.scenario([{"text": "ok now"}, {"text": "second window"}])
    assert "not sent" in hk.submit("@codex hi")["reason"]
    ack = hk.submit("/wf-continue")["hookSpecificOutput"]["additionalContext"]
    assert "override recorded" in ack and "codex weekly" in ack
    assert hk.submit("@codex hi")["reason"] == "Codex (gpt-6-sol):\nok now"
    put_codex_window(hk.home, 85.0, resets=NOW + 200000)  # the window reset and filled again: a new override is needed
    assert "not sent" in hk.submit("@codex hi")["reason"]


def test_a_stopped_codex_window_ignores_stale_windows_and_fails_open(hk, monkeypatch):
    put_codex_window(hk.home, 99.0, resets=NOW - 10)  # the window has already reset
    hk.scenario([{"text": "fine"}])
    assert hk.submit("@codex hi")["reason"].startswith("Codex (gpt-6-sol):")
    monkeypatch.setattr(usage_cache, "state", lambda *a, **k: 1 / 0)
    hk.scenario([{"text": "fine"}, {"text": "again"}])
    assert hk.submit("@codex hi")["reason"].startswith("Codex (gpt-6-sol):")  # a usage-check bug never blocks the relay


def test_usage_state_can_be_limited_to_one_source(home):
    put_claude_window(home, five=90)
    put_codex_window(home, 10.0)
    cfg = config.load(home)
    assert usage_cache.state(cfg, home, NOW)["window"] == "claude five_hour"
    assert usage_cache.state(cfg, home, NOW, sources=("codex",))["level"] == "ok"
    assert usage_cache.state(cfg, home, NOW, sources=("claude",))["level"] == "stop"


def test_overrides_for_windows_that_have_reset_are_pruned(home, monkeypatch):
    usage_cache.override_set("claude:five_hour", 1_000_000, home)
    usage_cache.override_set("codex:weekly", int(time.time()) + 3600, home)
    data = json.loads((home / ".workforce" / usage_cache.OVERRIDE_FILE).read_text())
    assert list(data) == ["codex:weekly"]


# ------------------------------------------------------------------ log files


def test_team_log_files_are_private(env, tmp_path):
    home = tmp_path / "hook-home"
    home.mkdir()
    client = env.start()
    client.text("review_status")
    log = env.home / ".workforce" / "team.log"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    hooks.evaluate("pretooluse", {"tool_name": "Read", "tool_input": {"file_path": str(home / ".workforce" / "team.key")}, "cwd": str(home)}, env={}, home=home, now=NOW)
    hooks._log(home, event="x")
    assert stat.S_IMODE((home / ".workforce" / "team.log").stat().st_mode) == 0o600


def test_git_errors_in_the_gate_are_reported_clearly(tmp_path, home):
    plain = tmp_path / "plain"
    plain.mkdir()
    reason = denied(pre("git commit -m x", plain, home))
    assert "could not verify" in reason and "not a git repository" in reason
    with pytest.raises(GitError):
        approvals.index_tree(plain)


def test_statusline_shows_unknown_for_a_codex_window_that_has_reset(home):
    from workforce.team import statusline

    cfg = config.load(home)
    cache = {"codex": [
        {"name": "weekly", "kind": "weekly", "percent": 61.0, "resets_at": int(NOW) - 5, "read_at": NOW - 10},
        {"name": "5h", "kind": "5h", "percent": 12.0, "resets_at": int(NOW) + 500, "read_at": NOW - 10},
    ]}
    text = statusline.codex_cells(cache, cfg, NOW)
    assert "wk ?" in text and "61" not in text and "5h 12%" in text


def test_wf_home_moves_workforce_state_without_moving_home(tmp_path, monkeypatch):
    other = tmp_path / "wf-home"
    monkeypatch.setenv("WF_HOME", str(other))
    assert config.workforce_dir() == other / ".workforce"
    assert approvals.key_path() == other / ".workforce" / "team.key"
    assert config.workforce_dir(tmp_path / "explicit") == tmp_path / "explicit" / ".workforce"  # an explicit home wins
    monkeypatch.delenv("WF_HOME")
    assert config.workforce_dir() == Path.home() / ".workforce"


def test_the_server_keeps_its_state_under_wf_home(env, tmp_path):
    other = tmp_path / "wf-home"
    (other / ".workforce").mkdir(parents=True)
    (other / ".workforce" / "team.toml").write_text(f'codex = "{FAKE}"\nclaude = "{FAKE_CLAUDE}"\nreviewer_model = "claude-haiku-4-5-20251001"\nreviewer_effort = "low"\n')
    (env.repo / "app.py").write_text("x\n")
    env.extra_env["WF_HOME"] = str(other)
    env.scenario(claude=[verdict()])
    client = env.start()
    client.text("claude_review")
    argv = env.calls("claude")[0]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5-20251001" and argv[argv.index("--effort") + 1] == "low"
    assert (other / ".workforce" / "team.key").is_file() and (other / ".workforce" / "team.log").is_file()
    assert not (env.home / ".workforce" / "team.key").exists()  # HOME's own .workforce was not used for the key
    assert approvals.status(env.repo, other)["claude"]["verdict"] == "APPROVE"


@pytest.fixture
def side_commit(git_repo):
    git(git_repo, "checkout", "-q", "-b", "side")
    (git_repo / "side.txt").write_text("unreviewed\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "side")
    sha = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "checkout", "-q", "main")
    git(git_repo, "checkout", "-q", "-b", "work")
    return sha


MOVES = [
    "git reset --hard {sha}",
    "git reset {sha}",
    "git reset --soft {sha} --",
    "git branch -f main {sha}",
    "git branch --force other {sha}",
    "git checkout -B main {sha}",
    "git switch -C main {sha}",
    "git switch --force-create=main {sha}",
    "cd . && git reset --keep {sha}",
]


@pytest.mark.parametrize("command", MOVES)
def test_moving_a_branch_to_an_unreviewed_commit_needs_the_reviews(git_repo, home, side_commit, command):
    command = command.format(sha=side_commit[:10])
    reason = denied(pre(command, git_repo, home))
    assert "/review" in reason and "git checkout --detach" in reason, (command, reason)
    tree = git(git_repo, "rev-parse", f"{side_commit}^{{tree}}")
    for reviewer in ("claude", "codex"):
        approvals.record(git_repo, reviewer, "APPROVE", "fine", tree=tree)
    assert pre(command, git_repo, home) is None, command


def test_one_approval_of_the_target_commit_is_not_enough(git_repo, home, side_commit):
    tree = git(git_repo, "rev-parse", f"{side_commit}^{{tree}}")
    approvals.record(git_repo, "claude", "APPROVE", "fine", tree=tree)
    assert "Codex" in denied(pre(f"git reset --hard {side_commit}", git_repo, home))


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard",
        "git reset --hard HEAD",
        "git reset --hard HEAD~1",
        "git reset --soft HEAD~1",
        "git branch -f other HEAD~1",
        "git branch -f other",
        "git checkout -B fresh",
        "git reset README.md",
        "git reset -- README.md",
        "git reset {sha} -- README.md",
        "git reset -p",
        "git branch -D side",
        "git checkout -b new {sha}",
        "git checkout --detach {sha}",
    ],
)
def test_moves_within_the_branch_and_path_resets_need_no_review(git_repo, home, side_commit, command):
    (git_repo / "README.md").write_text("two\n")
    git(git_repo, "commit", "-q", "-am", "two")
    assert pre(command.format(sha=side_commit), git_repo, home) is None, command


def test_moving_to_a_commit_on_a_remote_branch_needs_no_review(git_repo, home, side_commit):
    git(git_repo, "update-ref", "refs/remotes/origin/dev", side_commit)
    assert pre("git reset --hard origin/dev", git_repo, home) is None
    assert pre(f"git branch -f main {side_commit}", git_repo, home) is None


def test_an_unknown_revision_is_blocked(git_repo, home):
    git(git_repo, "checkout", "-q", "-b", "work")
    assert "not a commit" in denied(pre("git branch -f main nosuchref", git_repo, home))
    assert "computed revision" in denied(pre("git reset --hard $TARGET", git_repo, home))


def test_reset_hard_on_or_to_main_stays_blocked_even_when_reviewed(git_repo, home):
    assert "on main" in denied(pre("git reset --hard HEAD", git_repo, home))
    git(git_repo, "checkout", "-q", "-b", "work")
    assert "to main" in denied(pre("git reset --hard main", git_repo, home))


def test_the_claude_reviewer_gets_only_read_tools_in_safe_mode(tmp_path):
    from workforce.agents.base import AgentRequest
    from workforce.agents.claude import ClaudeRunner
    from workforce.team import claude_review

    req = AgentRequest(role="r", agent="claude", model="m", effort="high", prompt="p", cwd=tmp_path, sandbox="read-only", tools=claude_review.READ_TOOLS)
    cmd = ClaudeRunner(Path("claude")).build_command(req, prompt_on_stdin=True)
    assert cmd[cmd.index("--tools") + 1] == "Read,Glob,Grep"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Glob,Grep"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--permission-prompts") + 1] == "none"
    assert "--safe-mode" in cmd and "--strict-mcp-config" in cmd


def test_read_only_codex_runs_switch_off_plugins_and_every_configured_mcp_server(tmp_path, monkeypatch):
    from workforce.agents.base import AgentRequest
    from workforce.agents.codex import CodexRunner

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('[mcp_servers.node_repl]\ncommand = "x"\n\n[mcp_servers.computer-use]\ncommand = "y"\n\n[mcp_servers."odd.name"]\ncommand = "z"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    req = AgentRequest(role="r", agent="codex", model="m", effort="high", prompt="p", cwd=tmp_path, sandbox="read-only", no_mcp=True)
    cmd = CodexRunner(Path("codex")).build_command(req, tmp_path / "out", None)
    pairs = [cmd[i + 1] for i, token in enumerate(cmd) if token == "-c"]
    assert "features.plugins=false" in pairs
    assert "mcp_servers.node_repl.enabled=false" in pairs
    assert "mcp_servers.computer-use.enabled=false" in pairs
    assert not any("odd.name" in pair for pair in pairs)
    plain = CodexRunner(Path("codex")).build_command(AgentRequest(role="r", agent="codex", model="m", effort="high", prompt="p", cwd=tmp_path), tmp_path / "out", None)
    assert "features.plugins=false" not in plain


def test_team_codex_runs_drop_mcp_only_when_read_only(tmp_path, monkeypatch):
    from workforce.team import relay

    seen = []

    class Runner:
        def __init__(self, binary):
            pass

        def run(self, request):
            seen.append((request.sandbox, request.no_mcp))
            from workforce.agents.base import AgentResult
            return AgentResult(ok=True, text="ok", structured=None, session_id="s", model="m")

    monkeypatch.setattr(relay, "CodexRunner", Runner)
    cfg = config.load(tmp_path / "home")
    for sandbox in ("read-only", "workspace-write"):
        relay.run_codex(cfg, tmp_path, "team_ask", "p", "m", "high", sandbox=sandbox)
    assert seen == [("read-only", True), ("workspace-write", False)]


@pytest.mark.parametrize(
    "tool, path",
    [
        ("Write", ".workforce/team/plan-reviews.json"),
        ("Edit", ".workforce/team/plan-reviews.json"),
        ("Write", ".workforce/team/plans/retries/REVIEW-LOG.md"),
        ("Edit", ".workforce/team/plans/retries/REVIEW-LOG.md"),
    ],
)
def test_plan_review_records_are_written_only_by_the_server(git_repo, home, tool, path):
    payload = {"tool_name": tool, "tool_input": {"file_path": str(git_repo / path), "content": "x"}, "cwd": str(git_repo)}
    assert "WorkForce server" in denied(hooks.evaluate("pretooluse", payload, env={}, home=home, now=NOW))


def test_claude_may_write_the_plan_itself(git_repo, home):
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(git_repo / ".workforce/team/plans/retries/PLAN.md"), "content": "x"}, "cwd": str(git_repo)}
    assert hooks.evaluate("pretooluse", payload, env={}, home=home, now=NOW) is None


@pytest.mark.parametrize("command", ["echo x > .workforce/team/plan-reviews.json", "cp /tmp/x .workforce/team/plan-reviews.json"])
def test_bash_cannot_write_plan_review_records(git_repo, home, command):
    assert "codex_plan_review" in denied(pre(command, git_repo, home))
