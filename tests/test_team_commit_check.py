"""Commit gate fixes: a command may not change files before it commits, approvals are bound to the reviewed base,
and inside `wf` a git pre-commit check compares the exact committed tree with the approvals before the user's own hooks run.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from workforce.team import approvals, githook, gitgate, mcp_server
from tests.test_team_hooks import approve, denied, home, pre  # noqa: F401  (fixtures + helpers)
from tests.test_team_launch import run as launch_run, setup as launch_setup  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent


def git(repo, *args, env=None, check=True):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=check)
    return done.stdout.strip()


# ------------------------------------------------------------------ 1: no file changes before a commit in one command


def test_the_reported_edit_and_commit_command_is_refused_even_with_both_approvals(git_repo, home):
    (git_repo / "README.md").write_text("reviewed change\n")
    approve(git_repo, "claude", "codex")
    reason = denied(pre("printf 'unreviewed replacement\\n' > README.md && git add -A && git commit -m changed", git_repo, home))
    assert "change files before it commits" in reason and "> README.md" in reason


@pytest.mark.parametrize(
    "command",
    [
        "black . && git commit -am fmt",
        "sed -i s/a/b/ README.md && git commit -am x",
        "npm run build; git add -A && git commit -m x",
        "git merge feature; git commit -am resolve",
        "git rm README.md && git commit -m rm",
        "git checkout -- README.md && git commit -m x",
        "git add -A > out.txt && git commit -m x",
        "touch new.py && git add -A && git commit -m x",
        "bash gen.sh && git commit -am x",
        "find . -name '*.pyc' -delete && git commit -am x",
    ],
)
def test_anything_that_can_change_files_before_the_commit_is_refused(git_repo, home, command):
    (git_repo / "README.md").write_text("reviewed\n")
    approve(git_repo, "claude", "codex")
    assert "change files before it commits" in denied(pre(command, git_repo, home)), command


@pytest.mark.parametrize(
    "command",
    [
        "git add -A && git commit -m ok",
        "git status && git diff && git add -A && git commit -m ok",
        "cd . && git add . && git commit -m ok",
        "grep -r reviewed . && git commit -am ok",
        "git commit -am ok && make build && echo done > done.txt",
        "git commit -am ok 2>/dev/null",
        "git commit -am ok > /tmp/commit.log 2>&1",
        "git commit -am 'a > b'",
    ],
)
def test_plain_commits_and_changes_after_the_commit_are_still_allowed(git_repo, home, command):
    (git_repo / "README.md").write_text("reviewed\n")
    approve(git_repo, "claude", "codex")
    assert pre(command, git_repo, home) is None, command


@pytest.mark.parametrize(
    "command, needle",
    [
        ("git commit --no-verify -am x", "--no-verify"),
        ("git commit -anm x", "--no-verify"),
        ("git merge --continue --no-verify", "--no-verify"),
        ("env -i git commit -am x", "clears or unsets"),
        ("env -u HOME git commit -am x", "clears or unsets"),
        ("git -c core.hooksPath=/tmp/none commit -am x", "hooks configuration"),
        ("git config core.hooksPath /tmp/none", "hooks configuration"),
        ("GIT_CONFIG_COUNT=0 git commit -am x", "GIT_CONFIG"),
    ],
)
def test_ways_around_the_commit_time_check_are_refused(git_repo, home, command, needle):
    (git_repo / "README.md").write_text("reviewed\n")
    approve(git_repo, "claude", "codex")
    assert needle in denied(pre(command, git_repo, home)), command


def test_a_commit_hidden_from_the_parser_is_refused_even_when_approved(git_repo, home):
    approve(git_repo, "claude", "codex")
    assert "plain `git commit`" in denied(pre("g=git; $g commit -m x", git_repo, home))


# ------------------------------------------------------------------ 2: approvals are bound to the reviewed base


def test_an_approval_records_and_signs_the_base_commit(git_repo, home):
    (git_repo / "README.md").write_text("changed\n")
    entry = approvals.record(git_repo, "codex", "APPROVE", "ok")
    assert entry["base"] == git(git_repo, "rev-parse", "HEAD")
    data = json.loads(approvals.approvals_path(git_repo).read_text())
    data[entry["tree"]]["codex"]["base"] = "0" * 40
    approvals.approvals_path(git_repo).write_text(json.dumps(data))
    assert "bad signature" in " ".join(approvals.status(git_repo)["missing"])


def test_approvals_against_another_base_do_not_allow_a_commit_on_head(git_repo, home):
    (git_repo / "a.txt").write_text("a\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "second")
    (git_repo / "README.md").write_text("changed\n")
    older = git(git_repo, "rev-parse", "HEAD~1")
    for reviewer in ("claude", "codex"):
        approvals.record(git_repo, reviewer, "APPROVE", "ok", base=older)
    status = approvals.status(git_repo)
    assert status["commit_allowed"] is False and all("reviewed against" in m for m in status["missing"])
    assert "reviewed against" in denied(pre("git add -A && git commit -m x", git_repo, home))
    approve(git_repo, "claude", "codex")
    assert pre("git add -A && git commit -m x", git_repo, home) is None


def test_the_reported_custom_base_case_no_longer_authorises_the_head_commit(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".workforce").mkdir(parents=True)
    (home / ".workforce" / "team.toml").write_text(f'codex = "{ROOT}/tests/fakes/fake_codex.py"\nclaude = "{ROOT}/tests/fakes/fake_claude.py"\n')
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "work"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"]):
        git(repo, *args)
    (repo / "README.md").write_text("hi\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "A")
    (repo / "security_check.py").write_text("def verify(token):\n    return token == 'x'\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "B")
    (repo / "security_check.py").unlink()
    (repo / "docs.md").write_text("new docs\n")
    scenario = tmp_path / "scenario.json"
    verdict = {"structured": {"verdict": "APPROVE", "summary": "only a doc", "findings": []}}
    scenario.write_text(json.dumps({"codex": [verdict], "claude": [verdict]}))
    for key, value in {"HOME": str(home), "WF_FAKE_SCENARIO": str(scenario), "CLAUDE_PROJECT_DIR": str(repo), "PYTHONPATH": str(ROOT)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("WF_HOME", raising=False)
    server = mcp_server.Server(home=home)
    for tool in ("codex_review", "claude_review"):
        body = json.loads(server.call(tool, {"base": "HEAD~1"})["content"][0]["text"])
        assert body["verdict"] == "APPROVE" and body["commit_allowed"] is False and "base_note" in body
    reason = denied(pre("git add -A && git commit -m docs", repo, home))
    assert "reviewed against" in reason


# ------------------------------------------------------------------ the commit-time check inside wf


@pytest.fixture
def wf_git(git_repo, home, tmp_path):
    own = tmp_path / "own-hooks"
    own.mkdir()
    log = tmp_path / "own.log"
    (own / "pre-commit").write_text(f"#!/bin/sh\necho ran >> {log}\n")
    (own / "pre-commit").chmod(0o755)
    git(git_repo, "config", "core.hooksPath", str(own))
    folder = githook.install(ROOT / ".venv" / "bin" / "python", home)
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(ROOT), **githook.session_env(os.environ, folder)}
    env.pop("WF_HOME", None)
    return git_repo, env, log


def shell(repo, command, env):
    return subprocess.run(command, shell=True, cwd=repo, env=env, capture_output=True, text=True)


def test_an_approved_commit_passes_and_the_repos_own_hook_still_runs(wf_git, home):
    repo, env, log = wf_git
    (repo / "README.md").write_text("reviewed\n")
    approve(repo, "claude", "codex")
    done = shell(repo, "git add -A && git commit -qm ok", env)
    assert done.returncode == 0, done.stderr
    assert log.read_text().strip() == "ran"


def test_the_commit_time_check_catches_what_the_command_check_never_saw(wf_git, home):
    repo, env, log = wf_git
    (repo / "README.md").write_text("reviewed\n")
    approve(repo, "claude", "codex")
    done = shell(repo, "printf 'unreviewed\\n' > README.md && git add -A && git commit -qm sneaky", env)
    assert done.returncode != 0 and "WorkForce blocked this commit" in done.stderr
    assert git(repo, "log", "--oneline").count("\n") == 0
    assert not log.exists()


def test_the_commit_time_check_refuses_approvals_for_another_base(wf_git, home):
    repo, env, _ = wf_git
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "second")
    (repo / "README.md").write_text("reviewed\n")
    for reviewer in ("claude", "codex"):
        approvals.record(repo, reviewer, "APPROVE", "ok", base=git(repo, "rev-parse", "HEAD~1"))
    done = shell(repo, "git add -A && git commit -qm x", env)
    assert done.returncode != 0 and "reviewed against" in done.stderr


def test_other_hooks_are_handed_to_the_repos_own_hooks_with_the_original_environment(wf_git, tmp_path):
    repo, env, _ = wf_git
    own = Path(git(repo, "config", "core.hooksPath"))
    seen = tmp_path / "post.env"
    (own / "post-checkout").write_text(f"#!/bin/sh\nenv | grep -c GIT_CONFIG_KEY > {seen}\n")
    (own / "post-checkout").chmod(0o755)
    shell(repo, "git checkout -q -b other", env)
    assert seen.read_text().strip() == "0"


def test_session_env_adds_one_entry_and_original_env_removes_exactly_it(tmp_path):
    base = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "Me"}
    extra = githook.session_env(base, tmp_path)
    assert extra == {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": str(tmp_path), githook.INDEX_ENV: "1"}
    assert githook.original_env({**base, **extra}) == base
    assert githook.original_env({**githook.session_env({}, tmp_path), "X": "1"}) == {"X": "1"}


def test_wf_sets_up_the_hooks_and_points_git_at_them(launch_setup):
    root, home, _, calls = launch_setup
    environ = {}
    launch_run(launch_setup, [], environ=environ)
    folder = home / ".workforce" / githook.HOOKS_DIRNAME
    assert environ["GIT_CONFIG_KEY_0"] == "core.hooksPath" and environ["GIT_CONFIG_VALUE_0"] == str(folder)
    assert (folder / "pre-commit").is_file() and os.access(folder / "pre-commit", os.X_OK)
    assert set(p.name for p in folder.iterdir() if not p.name.startswith(".")) == set(githook.HOOK_NAMES)
