import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from workforce.team import denylist

HOME = Path("/Users/tester")
WORKTREE = "/work/repo-wt"


def bash(command: str, cwd: str = WORKTREE) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd, "session_id": "s1"}


def run_main(agent: str, payload, repo: Path, home=None, mode=None):
    """What the pipeline hook used to do with `payload`: (2, the reason) when the deny-list blocks it, else (0, "")."""
    tool_name = payload.get("tool_name") if isinstance(payload, dict) else None
    if not isinstance(tool_name, str) or not tool_name:
        return 2, "risk gate blocked: hook payload has no tool_name"
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    reason = denylist.denylist_reason(tool_name, payload.get("tool_input"), cwd, WORKTREE, home, mode=mode or denylist.MODE_AGENT)
    return (2, f"risk gate blocked: hard deny-list: {reason}") if reason else (0, "")


DENIED_COMMANDS = [
    ("rm -rf /", "outside the worktree"),
    ("rm -rf ~/Documents", "outside the worktree"),
    ("rm -rf ../sibling", "outside the worktree"),
    ("rm -fr /Users/tester/project", "outside the worktree"),
    ("rm -r -f /tmp/x", "outside the worktree"),
    ("rm -rf --no-preserve-root /", "outside the worktree"),
    ("rm -rf $HOME/x", "outside the worktree"),
    ("rm -rf $UNKNOWN_VAR/x", "outside the worktree"),
    ("cd / && rm -rf tmp", "outside the worktree"),
    ("cd /tmp; rm -rf build", "outside the worktree"),
    ("sudo rm -rf /var/log", "outside the worktree"),
    ("bash -c 'rm -rf /etc'", "outside the worktree"),
    ("true && rm -rf /*", "outside the worktree"),
    ("git push --force origin main", "force"),
    ("git push -f", "force"),
    ("git push origin +main", "force"),
    ("git push --force-with-lease origin wf/x", "force"),
    ("git -C /some/repo push -fu origin x", "force"),
    ("ls && git push --force", "force"),
    ("git reset --hard origin/main", "main"),
    ("git reset --hard main", "main"),
    ("git commit --no-gpg-sign -m x", "signing"),
    ("git -c commit.gpgsign=false commit -m x", "signing"),
    ("git config commit.gpgsign false", "signing"),
    ("git commit -S --no-gpg-sign", "signing"),
    ("cat ~/.ssh/id_rsa", "ssh"),
    ("ls $HOME/.ssh", "ssh"),
    ("cp /Users/tester/.ssh/id_ed25519 /tmp/k", "ssh"),
    ("cat ~/.codex/auth.json", "auth.json"),
    ("jq . $HOME/.codex/auth.json", "auth.json"),
    ("cat ~/.claude/.credentials.json", "credentials"),
    ("cat ~/.claude.json", "claude.json"),
    ("security find-generic-password -s 'Claude Code-credentials' -w", "credentials"),
    ("security dump-keychain", "keychain"),
]


@pytest.mark.parametrize("command,needle", DENIED_COMMANDS)
def test_denylist_blocks_dangerous_commands(tmp_path, command, needle):
    code, stderr = run_main("claude", bash(command), tmp_path)
    assert code == 2, command
    assert "risk gate blocked" in stderr
    assert needle in stderr


@pytest.mark.parametrize(
    "tool,tool_input",
    [
        ("Read", {"file_path": "/Users/tester/.ssh/id_rsa"}),
        ("Read", {"file_path": "~/.codex/auth.json"}),
        ("Read", {"file_path": "/Users/tester/.claude/.credentials.json"}),
        ("Read", {"file_path": "/Users/tester/.claude.json"}),
        ("Grep", {"pattern": "BEGIN", "path": "~/.ssh"}),
        ("Glob", {"pattern": "**/.ssh/*"}),
        ("Edit", {"file_path": "/Users/tester/.ssh/config", "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": "../../.ssh/authorized_keys", "content": "x"}),
        ("MultiEdit", {"file_path": "src/a.py", "edits": [{"file_path": "~/.ssh/config"}]}),
        ("apply_patch", {"command": "*** Begin Patch\n*** Update File: ../../.ssh/config\n@@\n-a\n+b\n*** End Patch"}),
    ],
)
def test_denylist_blocks_credential_paths_in_file_tools(tmp_path, tool, tool_input):
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": WORKTREE}
    code, stderr = run_main("codex" if tool == "apply_patch" else "claude", payload, tmp_path)
    assert code == 2


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "rm -rf ./dist node_modules",
        "rm -rf *.pyc",
        "rm -rf /work/repo-wt/tmp",
        "rm -rf src/../build",
        "rm somefile",
        "git add -A",
        "git status",
        "ls ~/Documents",
        "grep -rn ssh_config src",
        "cat docs/ssh-notes.md",
    ],
)
def test_denylist_lets_ordinary_commands_through(tmp_path, command):
    code, _ = run_main("claude", bash(command), tmp_path)
    assert code == 0, command


def test_denylist_allows_reading_claude_config_that_is_not_a_credential(tmp_path):
    payload = {"tool_name": "Read", "tool_input": {"file_path": "~/.claude/CLAUDE.md"}, "cwd": WORKTREE}
    assert run_main("claude", payload, tmp_path)[0] == 0


def _repo_with_branch(git_repo: Path, branch: str) -> Path:
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "-B", branch], check=True)
    return git_repo


def test_reset_hard_denied_on_main_branch(git_repo):
    reason = denylist.denylist_reason("Bash", {"command": "git reset --hard HEAD~1"}, str(git_repo), str(git_repo), HOME)
    assert reason and "main" in reason


def test_reset_hard_denied_on_master_branch(git_repo):
    _repo_with_branch(git_repo, "master")
    assert denylist.denylist_reason("Bash", {"command": "git reset --hard"}, str(git_repo), str(git_repo), HOME)


def test_reset_hard_allowed_on_task_branch_but_not_toward_main(git_repo):
    _repo_with_branch(git_repo, "wf/R1/T1")
    assert denylist.denylist_reason("Bash", {"command": "git reset --hard HEAD~1"}, str(git_repo), str(git_repo), HOME) is None
    assert denylist.denylist_reason("Bash", {"command": "git reset --hard origin/main"}, str(git_repo), str(git_repo), HOME)


def test_reset_hard_denied_when_branch_unknown(tmp_path):
    reason = denylist.denylist_reason("Bash", {"command": "git reset --hard"}, str(tmp_path), str(tmp_path), HOME)
    assert reason and "unknown" in reason


def test_reset_hard_with_dash_c_uses_that_directory(git_repo, tmp_path):
    reason = denylist.denylist_reason("Bash", {"command": f"git -C {git_repo} reset --hard"}, str(tmp_path), str(tmp_path), HOME)
    assert reason and "main" in reason


def test_rm_relative_targets_follow_cd_inside_worktree(tmp_path):
    (tmp_path / "sub").mkdir()
    ok = denylist.denylist_reason("Bash", {"command": "cd sub && rm -rf out"}, str(tmp_path), str(tmp_path), HOME)
    bad = denylist.denylist_reason("Bash", {"command": "cd .. && rm -rf out"}, str(tmp_path), str(tmp_path), HOME)
    assert ok is None
    assert bad


def test_rm_symlink_escaping_worktree_is_denied(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "link").symlink_to(outside)
    reason = denylist.denylist_reason("Bash", {"command": "rm -rf link"}, str(worktree), str(worktree), HOME)
    assert reason and "outside the worktree" in reason


@pytest.mark.parametrize(
    "command",
    [
        "echo hi > file",
        "cat a >> b",
        "sed -i s/a/b/ f",
        "sed --in-place s/a/b/ f",
        "find . -delete",
        "find . -exec rm {} ;",
        "git branch -D old",
        "git branch newbranch",
        "git tag v1",
        "git commit -m x",
        "git checkout main",
        "cat $(rm -rf x)",
        "cat `whoami`",
        "ls && rm file",
        "python script.py",
        "npm install",
        "curl https://example.com",
        "",
    ],
)
def test_is_read_only_rejects_mutations(command):
    assert denylist.is_read_only("Bash", {"command": command}) is False


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "Task", "mcp__x__y"])
def test_edit_tools_are_not_read_only(tool):
    assert denylist.is_read_only(tool, {"file_path": "a.py"}) is False


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir(parents=True)
    return repo


HISTORY_COMMANDS = [
    "git commit -m x",
    "git commit --amend --no-edit",
    "git commit -am x",
    "git push",
    "git push origin wf/R1/T1",
    "git push -u origin wf/R1/T1",
    "git merge wf/R1/T2",
    "git rebase main",
    "git reset HEAD file.py",
    "git reset --soft HEAD~1",
    "git stash",
    "git stash pop",
    "git cherry-pick abc123",
    "git am patch.mbox",
    "git tag v1",
    "git tag -a v1 -m x",
    "git update-ref refs/heads/main HEAD",
    "git -C /work/repo-wt commit -m x",
    "git --no-pager commit -m x",
    "git status && git commit -m x",
    "bash -lc 'git commit -m x'",
    "sudo git commit -m x",
    "env FOO=1 git push",
    "xargs git commit -m",
    "find . -name x -exec git commit -m x {} ;",
    "git add . && git commit -m x",
    "git revert HEAD",
    "git pull",
]


def test_denylist_reason_defaults_to_committer_behaviour_and_takes_a_mode():
    assert denylist.denylist_reason("Bash", {"command": "git commit -m x"}, WORKTREE, WORKTREE, HOME) is None
    assert denylist.denylist_reason("Bash", {"command": "git commit -m x"}, WORKTREE, WORKTREE, HOME, mode="committer") is None
    assert denylist.denylist_reason("Bash", {"command": "git commit -m x"}, WORKTREE, WORKTREE, HOME, mode="agent")


AUDIT_PROBES = [
    ("bash -lc 'git push --force origin main'", "force"),
    ("zsh -ic 'git push --force origin main'", "force"),
    ("bash -xc 'git push --force origin main'", "force"),
    ("sh -ec 'git push -f'", "force"),
    ("bash --norc -c 'git push --force'", "force"),
    ("bash -o pipefail -c 'git push --force'", "force"),
    ("env bash -lc 'git push --force'", "force"),
    ("bash -lc 'rm -rf /Users/tester/x'", "outside the worktree"),
    ("bash -lc \"bash -c 'rm -rf /etc'\"", "outside the worktree"),
    ("xargs rm -rf /Users/tester/x", "xargs"),
    ("ls | xargs rm -rf", "xargs"),
    ("find . -name x | xargs -0 -n1 rm -rf", "xargs"),
    ("echo /etc | xargs -I{} sh -c 'rm -rf {}'", "xargs"),
    ("xargs git push --force", "force"),
    ("find /Users/tester -delete", "outside the worktree"),
    ("find / -name x -delete", "outside the worktree"),
    ("find ~ -exec rm -rf {} +", "outside the worktree"),
    ("find .. -delete", "outside the worktree"),
    ("find /etc \\( -name a -o -name b \\) -delete", "outside the worktree"),
    ("find . -exec rm -rf /etc ;", "outside the worktree"),
    ("find . -exec git push --force ;", "force"),
    ("eval 'rm -rf /'", "eval"),
    ("eval \"$(echo cm0gLXJm | base64 -d)\"", "eval"),
    ("sudo eval x", "eval"),
    ("bash -c 'eval x'", "eval"),
    ("source ~/x.sh", "source"),
    (". ./x.sh", "'.'"),
    ("cat ~/.s\"\"sh/id_rsa", "ssh"),
    ("cat ~/.s''sh/id_rsa", "ssh"),
    ("cat ~/.s\\sh/id_rsa", "ssh"),
    ("cat ~/.ss[h]/id_rsa", "glob"),
    ("ls ~/.ss?/", "glob"),
    ("ls ~/.s*", "glob"),
    ("cat ~/.[s]sh/id_rsa", "glob"),
    ("cat ~/.SSH/id_rsa", "ssh"),
    ("cat ~/.cod?x/auth.json", "glob"),
    ("cat ~/.co*/auth.json", "glob"),
    ("cat ~/.claude/.cred*", "glob"),
    ("cat ~/.claude/*", "glob"),
    ("cat ~/.claude.js?n", "glob"),
    ("cat ~/.gnup?/private-keys-v1.d/*", "glob"),
    ("cat ~/.gnupg/pubring.kbx", "gnupg"),
    ("ls ~/{.s,}sh", "ssh"),
    ("cat ~/{.ssh,x}/id_rsa", "ssh"),
    ("cd ~ && cat .ss?/id_rsa", "glob"),
    ("cd ~ && cat .claude/.cred*", "glob"),
    ("cat ~/.codex/auth.js\"\"on", "auth.json"),
    ("git -c gpg.program=/usr/bin/true commit -m x", "gpg.program"),
    ("git -c gpg.format=ssh commit -m x", "gpg.format"),
    ("git -c GPG.program=/usr/bin/true commit -m x", "gpg.program"),
    ("git -c commit.gpgsign=false commit -m x", "signing"),
    ("git -c commit.gpgsign=0 commit -m x", "signing"),
    ("git -c user.signingkey=x commit -m x", "signingkey"),
    ("git -c alias.ci='!git push --force' ci", "alias"),
    ("git --config-env=gpg.program=FOO commit -m x", "gpg.program"),
    ("git config gpg.program /usr/bin/true", "gpg.program"),
    ("git config --global commit.gpgsign true", "gpgsign"),
    ("git config --global user.signingkey x", "signingkey"),
    ("env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=commit.gpgsign GIT_CONFIG_VALUE_0=false git commit -m x", "GIT_CONFIG"),
    ("GIT_CONFIG_PARAMETERS=\"'gpg.program=true'\" git commit -m x", "GIT_CONFIG"),
    ("export GIT_CONFIG_GLOBAL=/tmp/x; git commit -m x", "GIT_CONFIG"),
    ("ls\nrm -rf /etc", "outside the worktree"),
    ("ls\ngit push --force", "force"),
    ("echo `rm -rf /etc`", "outside the worktree"),
    ("echo \"$(rm -rf /etc)\"", "outside the worktree"),
    ("echo \"`git push --force`\"", "force"),
    ("bash <<EOF\nrm -rf /etc\nEOF", "outside the worktree"),
    ("sudo -u root rm -rf /var", "outside the worktree"),
    ("timeout 5 rm -rf /var", "outside the worktree"),
    ("setsid rm -rf /var", "outside the worktree"),
    ("cat <<EOF\nhi\nEOF\nrm -rf /etc", "outside the worktree"),
]


@pytest.mark.parametrize("mode", ["committer", "agent"])
@pytest.mark.parametrize("command,needle", AUDIT_PROBES)
def test_audit_bypass_probes_are_denied_in_both_modes(command, needle, mode):
    reason = denylist.denylist_reason("Bash", {"command": command}, WORKTREE, WORKTREE, HOME, mode=mode)
    assert reason and needle.lower() in reason.lower(), (command, reason)


@pytest.mark.parametrize(
    "command",
    [
        ["git", "push", "--force", "origin", "main"],
        ["bash", "-lc", "git push --force origin main"],
        ["zsh", "-ic", "rm -rf /Users/tester/x"],
        ["cat", "/Users/tester/.ssh/id_rsa"],
        ["cat", "~/.ss?/id_rsa"],
        ["git", "commit", "--no-gpg-sign", "-m", "x"],
        ["git", "-c", "gpg.program=true", "commit", "-m", "x"],
        ["eval", "rm -rf /"],
        ["rm", "-rf", "/Users/tester/x"],
        ["xargs", "rm", "-rf"],
    ],
)
@pytest.mark.parametrize("key", ["command", "cmd"])
def test_array_form_commands_are_joined_and_checked(command, key):
    payload = {"tool_name": "Bash", "tool_input": {key: command}, "cwd": WORKTREE}
    assert denylist.denylist_reason("Bash", payload["tool_input"], WORKTREE, WORKTREE, HOME, mode="committer")


def test_string_tool_input_is_checked_as_a_command():
    assert denylist.denylist_reason("Bash", "git push --force", WORKTREE, WORKTREE, HOME)


def test_apply_patch_given_as_argv_still_checks_its_paths():
    patch = "*** Begin Patch\n*** Update File: ../../.ssh/config\n@@\n-a\n+b\n*** End Patch"
    assert denylist.denylist_reason("shell", {"command": ["apply_patch", patch]}, WORKTREE, WORKTREE, HOME)


@pytest.mark.parametrize(
    "command",
    [
        "find . -name '*.pyc' -delete",
        "find build -type f -exec rm {} +",
        "find . \\( -name a -o -name b \\) -exec rm {} \\;",
        "find /work/repo-wt/src -delete",
        "rm -rf build && echo done",
        "echo 'a;b' && ls",
        "echo 'run eval later; then rm -rf /'",
        "git commit -m 'fix: handle eval; source and . cases'",
        "git commit -m \"$(cat <<'EOF'\nfix things\n\neval is discussed here\nsource maps too\n. and more\nEOF\n)\"",
        "python -c 'print(1)'",
        "bash script.sh -c foo",
        "bash -n script.sh",
        "sh ./build.sh",
        "ls .claude/commands/*.md",
        "cat .claude/*.md",
        "ls .* ",
        "echo $GIT_DIR_NOT_CONFIG",
        "grep -rn ssh_config src",
        "cat docs/gnupg-notes.md",
        "ls ~/Documents/*.pdf",
        "xargs echo",
        "ls | xargs cat",
        "ls | xargs -n1 grep foo",
        "rm -rf ./out ./dist",
        "git config --get commit.gpgsign",
        "git config --list",
        "git config user.name",
    ],
)
def test_hardening_does_not_over_block_ordinary_commands(command):
    assert denylist.denylist_reason("Bash", {"command": command}, WORKTREE, WORKTREE, HOME, mode="committer") is None, command


def test_project_claude_globs_are_only_allowed_inside_the_worktree():
    assert denylist.denylist_reason("Glob", {"pattern": ".claude/commands/*.md"}, WORKTREE, WORKTREE, HOME) is None
    assert denylist.denylist_reason("Glob", {"pattern": ".claude/*"}, WORKTREE, WORKTREE, HOME) is None
    assert denylist.denylist_reason("Glob", {"pattern": "~/.claude/*"}, WORKTREE, WORKTREE, HOME)
    assert denylist.denylist_reason("Glob", {"pattern": "../../.claude/*"}, WORKTREE, WORKTREE, HOME)
    assert denylist.denylist_reason("Grep", {"pattern": "x", "path": "~/.ss?"}, WORKTREE, WORKTREE, HOME)
    assert denylist.denylist_reason("Glob", {"pattern": "**/.ss*"}, WORKTREE, WORKTREE, HOME)
    assert denylist.denylist_reason("Read", {"file_path": "/Users/tester/.gnupg/pubring.kbx"}, WORKTREE, WORKTREE, HOME)
    assert denylist.denylist_reason("Read", {"file_path": "~/.s\"\"sh/id_rsa"}, WORKTREE, WORKTREE, HOME)


def test_glob_exemption_does_not_survive_a_cd():
    assert denylist.denylist_reason("Bash", {"command": "cd ~ && ls .claude/*"}, WORKTREE, WORKTREE, HOME)
    assert denylist.denylist_reason("Bash", {"command": "ls .claude/*"}, WORKTREE, WORKTREE, HOME) is None


def test_nul_in_a_command_makes_the_helper_raise_not_pass():
    with pytest.raises(ValueError):
        denylist.denylist_reason("Bash", {"command": "rm -rf /tmp/x\x00y"}, WORKTREE, WORKTREE, HOME)


def nul_payload() -> dict:
    return bash("rm -rf /tmp/x\x00y")


def _codex_command(args: list[str]) -> str:
    key, _, value = args[2].partition("=")
    assert key == "denylist.PreToolUse"
    return tomllib.loads(f"value = {value}")["value"][0]["hooks"][0]["command"]


def test_git_repo_fixture_ignores_the_users_global_git_config(git_repo):
    global_config = os.environ["GIT_CONFIG_GLOBAL"]
    assert Path(global_config).read_text() == ""
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    listed = subprocess.run(["git", "-C", str(git_repo), "config", "--list", "--show-origin"], capture_output=True, text=True, check=True).stdout
    origins = {line.split("\t")[0] for line in listed.splitlines()}
    assert origins and all(origin == "file:.git/config" for origin in origins)


def test_global_git_hooks_and_signing_do_not_run_in_tests(tmp_path):
    marker = tmp_path / "hook-ran"
    hooks_dir = tmp_path / "global-hooks"
    hooks_dir.mkdir()
    (hooks_dir / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (hooks_dir / "pre-commit").chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".gitconfig").write_text(f"[core]\n\thooksPath = {hooks_dir}\n[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /nonexistent-gpg\n")
    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "HOME": str(fake_home)}
    for args in (["init", "-b", "main"], ["config", "user.name", "T"], ["config", "user.email", "t@example.com"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, env=env)
    done = subprocess.run(["git", "-C", str(repo), "commit", "-m", "x"], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    "command",
    ["echo x > /opt/elsewhere/notes.txt", "rm -rf /opt/elsewhere", "cp README.md /opt/elsewhere/", "mkdir -p ~/new-folder", "find /opt/elsewhere -name '*.log' -delete"],
)
def test_allow_outside_lets_writes_and_deletes_outside_the_worktree_through(command):
    assert denylist.denylist_reason("Bash", {"command": command}, WORKTREE, WORKTREE, HOME) is not None
    assert denylist.denylist_reason("Bash", {"command": command}, WORKTREE, WORKTREE, HOME, allow_outside=True) is None


@pytest.mark.parametrize(
    "command, needle",
    [("rm -rf /", "whole disk"), ("rm -rf ~", "home folder"), ("rm -rf /Users/tester", "home folder"), ("cat ~/.ssh/id_rsa", "ssh"), ("git push --force", "force"), ("git commit --no-gpg-sign -m x", "signing")],
)
def test_allow_outside_keeps_the_floor_the_credential_rules_and_the_git_rules(command, needle):
    assert needle in denylist.denylist_reason("Bash", {"command": command}, WORKTREE, WORKTREE, HOME, allow_outside=True)


def test_allow_outside_applies_to_file_tools_too():
    outside = {"file_path": "/opt/elsewhere/notes.txt", "content": "x"}
    assert "outside" in denylist.denylist_reason("Write", outside, WORKTREE, WORKTREE, HOME)
    assert denylist.denylist_reason("Write", outside, WORKTREE, WORKTREE, HOME, allow_outside=True) is None
    assert "ssh" in denylist.denylist_reason("Read", {"file_path": "~/.ssh/id_rsa"}, WORKTREE, WORKTREE, HOME, allow_outside=True)
