import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from workforce.team import approvals, config, hooks, usage_cache

NOW = 1_800_000_000.0


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))  # approvals are signed with the key under $HOME/.workforce
    return path


def pre(command, cwd, home, tool="Bash"):
    payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}
    return hooks.evaluate("pretooluse", payload, env={}, home=home, now=NOW)


def denied(output):
    assert output and output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    return output["hookSpecificOutput"]["permissionDecisionReason"]


def approve(repo, *reviewers, verdict="APPROVE"):
    for reviewer in reviewers:
        approvals.record(repo, reviewer, verdict, "fine")


COMMITS = [
    "git commit -m x",
    "git commit",
    "git -C {repo} commit -m x",
    "git add -A && git commit -m x",
    "git add -A; git commit -m x",
    "true || git commit -m x",
    "env GIT_AUTHOR_NAME=a git commit -m x",
    "FOO=1 git commit -m x",
    "sudo -u me git commit -m x",
    "bash -c 'git commit -m x'",
    "echo hi | xargs git commit -m",
    "(cd . && git commit -m x)",
    "git -c user.name=me commit -m x",
    "/usr/bin/git commit -m x",
    "cd {repo} && git commit -m x",
]


@pytest.mark.parametrize("command", COMMITS)
def test_commit_variants_denied_without_reviews(git_repo, home, command):
    reason = denied(pre(command.format(repo=git_repo), git_repo, home))
    assert "/review" in reason
    assert "Claude reviewer" in reason and "Codex" in reason


@pytest.mark.parametrize("command", COMMITS)
def test_commit_variants_allowed_with_both_approvals(git_repo, home, command):
    approve(git_repo, "claude", "codex")
    assert pre(command.format(repo=git_repo), git_repo, home) is None


def test_missing_reviewer_is_named(git_repo, home):
    approve(git_repo, "claude")
    reason = denied(pre("git commit -m x", git_repo, home))
    assert "Codex" in reason and "Claude reviewer (claude_review)" not in reason
    (git_repo / "new.txt").write_text("changed\n")  # any edit voids the old approval
    approve(git_repo, "codex")
    reason = denied(pre("git add -A && git commit -m x", git_repo, home))
    assert "Claude reviewer (claude_review)" in reason and "Codex (" not in reason


def test_request_changes_blocks_and_is_named(git_repo, home):
    approve(git_repo, "claude")
    approve(git_repo, "codex", verdict="REQUEST_CHANGES")
    reason = denied(pre("git commit -m x", git_repo, home))
    assert "REQUEST_CHANGES" in reason


def test_editing_after_approval_voids_it(git_repo, home):
    (git_repo / "README.md").write_text("reviewed\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    approve(git_repo, "claude", "codex")
    assert pre("git commit -m x", git_repo, home) is None
    (git_repo / "README.md").write_text("edited after the review\n")
    # a plain commit records the (unchanged) index, which is what was reviewed; staging the edit voids the approval
    assert pre("git commit -m x", git_repo, home) is None
    denied(pre("git commit -am x", git_repo, home))
    denied(pre("git add -A && git commit -m x", git_repo, home))


def test_git_dash_c_checks_that_repo_not_the_cwd(git_repo, home, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    approve(git_repo, "claude", "codex")
    assert pre(f"git -C {git_repo} commit -m x", other, home) is None
    # an approved cwd does not cover a commit made in some other directory
    assert "could not verify" in denied(pre(f"git -C {other} commit -m x", git_repo, home))


def test_commit_outside_a_repo_fails_closed(tmp_path, home):
    plain = tmp_path / "plain"
    plain.mkdir()
    reason = denied(pre("git commit -m x", plain, home))
    assert "could not verify" in reason


def test_internal_error_on_commit_fails_closed(git_repo, home, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("kaput")

    monkeypatch.setattr(approvals, "status", boom)
    assert "could not verify" in denied(pre("git commit -m x", git_repo, home))
    assert "kaput" in (home / ".workforce" / "team.log").read_text()


def test_parse_error_on_commit_fails_closed(git_repo, home, monkeypatch):
    from workforce.team import gitgate

    monkeypatch.setattr(gitgate, "raw_segments", lambda command: 1 / 0)
    assert "could not check" in denied(pre("git commit -m x", git_repo, home))
    assert pre("git status", git_repo, home) is None


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-gpg-sign -m x",
        "git -c commit.gpgsign=false commit -m x",
        "git config commit.gpgsign false",
        "git push --force origin main",
        "git push -f",
        "git push origin +main",
        "git push --force-with-lease",
        "GIT_CONFIG_COUNT=1 git commit -m x",
    ],
)
def test_always_deny_list_even_with_approvals(git_repo, home, command):
    approve(git_repo, "claude", "codex")
    reason = denied(pre(command, git_repo, home))
    assert reason.startswith("WorkForce blocked this command")


@pytest.mark.parametrize(
    "command",
    ["git status", "git diff HEAD", "git log --oneline", "ls -la", "python -m pytest", "git push origin feature", "git commit-graph verify", "echo 'git commit'"],
)
def test_non_commit_bash_allowed(git_repo, home, command):
    assert pre(command, git_repo, home) is None


def test_non_bash_tools_are_not_commit_checked(git_repo, home):
    payload = {"tool_name": "Write", "tool_input": {"file_path": "x", "content": "git commit"}, "cwd": str(git_repo)}
    assert hooks.evaluate("pretooluse", payload, env={}, home=home, now=NOW) is None


def test_main_prints_deny_json_and_exits_zero(git_repo, home):
    out = io.StringIO()
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(git_repo)})
    code = hooks.main(["pretooluse"], stdin=io.StringIO(payload), stdout=out, env={}, home=home, now=NOW)
    assert code == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_silent_when_allowed(git_repo, home):
    out = io.StringIO()
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(git_repo)})
    assert hooks.main(["pretooluse"], stdin=io.StringIO(payload), stdout=out, env={}, home=home, now=NOW) == 0
    assert out.getvalue() == ""


def test_main_bad_usage_and_garbage_payload(home):
    err = io.StringIO()
    assert hooks.main(["nope"], stdin=io.StringIO(""), stderr=err, home=home) == 2
    out = io.StringIO()
    assert hooks.main(["pretooluse"], stdin=io.StringIO("not json"), stdout=out, home=home) == 0
    assert out.getvalue() == ""
    assert hooks.main(["userpromptsubmit"], stdin=io.StringIO("[]"), stdout=out, home=home) == 0


def test_main_garbage_payload_that_looks_like_a_commit_is_denied(home):
    out = io.StringIO()
    hooks.main(["pretooluse"], stdin=io.StringIO('{"tool_input": {"command": "git commit -m x"'), stdout=out, home=home)
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_module_runs_as_a_subprocess_with_a_fake_home(git_repo, home):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(git_repo)})
    done = subprocess.run(
        [sys.executable, "-m", "workforce.team.hooks", "pretooluse"],
        input=payload, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(home)}, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---- usage


def set_usage(home, five=None, week=None, resets=NOW + 3600):
    usage_cache.write_claude(
        {
            "five_hour": {"used_percentage": five, "resets_at": int(resets)},
            "seven_day": {"used_percentage": week, "resets_at": int(resets) + 86400},
        },
        home,
        now=NOW,
    )


def prompt(home, text="do a thing"):
    return hooks.evaluate("userpromptsubmit", {"prompt": text}, env={}, home=home, now=NOW)


def test_no_usage_data_allows(home):
    assert prompt(home) is None
    assert pre("ls", Path("."), home) is None


def test_49_percent_is_silent(home):
    set_usage(home, five=49)
    assert prompt(home) is None
    assert pre("ls", Path("."), home) is None


def test_50_percent_alerts_without_blocking(home):
    set_usage(home, five=50)
    out = prompt(home)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "usage alert" in out["hookSpecificOutput"]["additionalContext"]
    assert "claude five_hour" in out["hookSpecificOutput"]["additionalContext"]
    assert "decision" not in out
    assert pre("ls", Path("."), home) is None


def test_59_percent_alerts_60_blocks_prompt_and_tools(home):
    set_usage(home, week=59)
    assert "usage alert" in prompt(home)["hookSpecificOutput"]["additionalContext"]
    set_usage(home, five=60, resets=NOW + 7200)
    out = prompt(home)
    assert out["decision"] == "block"
    assert "claude five_hour" in out["reason"] and "60%" in out["reason"] and "/wf-continue" in out["reason"]
    reason = denied(pre("ls", Path("."), home))
    assert "claude five_hour" in reason and "resets" in reason
    reason = denied(hooks.evaluate("pretooluse", {"tool_name": "Read", "tool_input": {"file_path": "x"}}, env={}, home=home, now=NOW))
    assert "usage stop" in reason


def test_codex_window_at_60_blocks(home):
    from workforce.usage.claude_limits import Window

    usage_cache.write_codex([Window("codex", "weekly", 61.0, int(NOW) + 5000, NOW, "weekly")], home, now=NOW)
    assert prompt(home)["decision"] == "block"
    assert "codex weekly" in prompt(home)["reason"]


def test_wf_continue_sets_override_for_this_window_only(home):
    set_usage(home, five=72, resets=NOW + 7200)
    assert prompt(home)["decision"] == "block"
    out = prompt(home, "/wf-continue")
    assert "override recorded" in out["hookSpecificOutput"]["additionalContext"]
    assert "decision" not in out
    # unblocked, still alerting
    assert "usage alert" in prompt(home)["hookSpecificOutput"]["additionalContext"]
    assert pre("ls", Path("."), home) is None
    assert usage_cache.override_active("claude:five_hour", int(NOW + 7200), home)
    # the window resets and fills again: a new reset time needs a new override
    set_usage(home, five=75, resets=NOW + 30000)
    assert prompt(home)["decision"] == "block"
    denied(pre("ls", Path("."), home))


def test_wf_continue_overrides_every_window_over_the_limit(home):
    set_usage(home, five=65, week=80)
    out = prompt(home, "/wf-continue")["hookSpecificOutput"]["additionalContext"]
    assert "five_hour" in out and "seven_day" in out
    assert pre("ls", Path("."), home) is None


def test_wf_continue_namespaced_and_with_nothing_to_override(home):
    out = prompt(home, "/workforce:wf-continue")
    assert "nothing to override" in out["hookSpecificOutput"]["additionalContext"]


def test_usage_errors_fail_open(home, monkeypatch):
    monkeypatch.setattr(usage_cache, "state", lambda *a, **k: 1 / 0)
    assert prompt(home) is None
    assert pre("ls", Path("."), home) is None
    assert "ZeroDivisionError" in (home / ".workforce" / "team.log").read_text()


def test_stop_does_not_hide_the_commit_gate_reason(git_repo, home):
    set_usage(home, five=90)
    assert "/review" in denied(pre("git commit -m x", git_repo, home))


# ------------------------------------------------------------------ the hard deny-list applies to every tool call


def payload_for(tool, tool_input, cwd):
    return {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}


@pytest.mark.parametrize(
    "command, needle",
    [
        ("cat ~/.codex/auth.json", "auth.json"),
        ("cat ~/.claude/.credentials.json", "credentials"),
        ("security find-generic-password -s 'Claude Code-credentials' -w", "credentials"),
        ("rm -rf /opt/wf-outside", "outside"),
        ("echo x > /opt/wf-outside/notes.txt", "outside"),
        ("cp README.md /opt/wf-outside/", "outside"),
        ("cd /tmp && rm -rf wf-scratch", "outside"),
    ],
)
def test_credential_reads_and_writes_outside_the_project_are_refused(git_repo, home, command, needle):
    reason = denied(pre(command, git_repo, home))
    assert needle in reason and "project folder" in reason


@pytest.mark.parametrize(
    "tool, tool_input, needle",
    [
        ("Read", {"file_path": "~/.ssh/id_rsa"}, "ssh"),
        ("Read", {"file_path": "/Users/someone/.codex/auth.json"}, "auth.json"),
        ("Grep", {"pattern": "BEGIN", "path": "~/.ssh"}, "ssh"),
        ("Write", {"file_path": "/opt/wf-outside/notes.txt", "content": "x"}, "outside"),
        ("Edit", {"file_path": "/opt/wf-outside/notes.txt", "old_string": "a", "new_string": "b"}, "outside"),
    ],
)
def test_file_tools_follow_the_same_rules(git_repo, home, tool, tool_input, needle):
    assert needle in denied(hooks.evaluate("pretooluse", payload_for(tool, tool_input, git_repo), env={}, home=home, now=NOW))


@pytest.mark.parametrize(
    "tool, tool_input",
    [
        ("Read", {"file_path": "~/.claude/settings.json"}),
        ("Write", {"file_path": "src/new.py", "content": "x"}),
        ("Write", {"file_path": "/tmp/scratch.txt", "content": "x"}),
        ("Bash", {"command": "rm -rf build node_modules"}),
        ("Bash", {"command": "rm /tmp/scratch.txt"}),
        ("Bash", {"command": "cat ~/.zshrc"}),
        ("Bash", {"command": "git status && git diff"}),
    ],
)
def test_ordinary_reads_and_writes_inside_the_project_or_a_temp_folder_are_allowed(git_repo, home, tool, tool_input):
    assert hooks.evaluate("pretooluse", payload_for(tool, tool_input, git_repo), env={}, home=home, now=NOW) is None


def test_the_project_folder_is_where_claude_code_started_even_after_a_cd(home):
    env = {"CLAUDE_PROJECT_DIR": "/opt/wf-project"}
    inside = {"tool_name": "Write", "tool_input": {"file_path": "../other.txt", "content": "x"}, "cwd": "/opt/wf-project/sub"}
    assert hooks.evaluate("pretooluse", inside, env=env, home=home, now=NOW) is None
    outside = {"tool_name": "Write", "tool_input": {"file_path": "/opt/elsewhere/x.txt", "content": "x"}, "cwd": "/opt/wf-project/sub"}
    assert "outside" in denied(hooks.evaluate("pretooluse", outside, env=env, home=home, now=NOW))


# ------------------------------------------------------------------ /wf-allow-outside


def user_types(text, cwd, home, session="s1"):
    payload = {"prompt": text, "cwd": str(cwd), "session_id": session}
    return hooks.evaluate("userpromptsubmit", payload, env={"CLAUDE_PROJECT_DIR": str(cwd)}, home=home, now=NOW)


def tool_call(tool_name, tool_input, cwd, home, session="s1", mode=None):
    payload = {"tool_name": tool_name, "tool_input": tool_input, "cwd": str(cwd), "session_id": session}
    if mode:
        payload["permission_mode"] = mode
    return hooks.evaluate("pretooluse", payload, env={"CLAUDE_PROJECT_DIR": str(cwd)}, home=home, now=NOW)


OUTSIDE_WRITE = ("Write", {"file_path": "/opt/wf-outside/notes.txt", "content": "x"})


def test_the_user_can_allow_writes_outside_the_project_for_this_session(git_repo, home):
    assert "outside" in denied(tool_call(*OUTSIDE_WRITE, git_repo, home))
    context = user_types("/wf-allow-outside", git_repo, home)["hookSpecificOutput"]["additionalContext"]
    assert "allowed writing outside the project folder" in context and "Credential files stay off limits" in context
    assert tool_call(*OUTSIDE_WRITE, git_repo, home) is None
    assert tool_call("Bash", {"command": "echo x > /opt/wf-outside/notes.txt"}, git_repo, home) is None
    assert tool_call("Bash", {"command": "rm -rf /opt/wf-outside"}, git_repo, home) is None
    assert tool_call("Bash", {"command": "cp README.md /opt/wf-outside/"}, git_repo, home) is None


def test_the_permission_is_bound_to_the_session_that_typed_it(git_repo, home):
    user_types("/wf-allow-outside", git_repo, home, session="s1")
    assert tool_call(*OUTSIDE_WRITE, git_repo, home, session="s2") is not None
    assert tool_call(*OUTSIDE_WRITE, git_repo, home, session=None) is not None
    assert tool_call(*OUTSIDE_WRITE, git_repo, home, session="s1") is None


def test_credential_files_and_the_whole_disk_stay_off_limits(git_repo, home):
    user_types("/wf-allow-outside", git_repo, home)
    assert "auth.json" in denied(tool_call("Bash", {"command": "cat ~/.codex/auth.json"}, git_repo, home))
    assert "credentials" in denied(tool_call("Read", {"file_path": "~/.claude/.credentials.json"}, git_repo, home))
    assert "force" in denied(tool_call("Bash", {"command": "git push --force origin main"}, git_repo, home))
    for command in ("rm -rf /", "rm -rf ~", "rm -rf /*"):
        reason = denied(tool_call("Bash", {"command": command}, git_repo, home))
        assert "whole disk or your home folder" in reason and "Even with /wf-allow-outside" in reason, command


def test_typing_it_again_turns_it_off(git_repo, home):
    user_types("/wf-allow-outside", git_repo, home)
    assert "off again" in user_types("/workforce:wf-allow-outside", git_repo, home)["hookSpecificOutput"]["additionalContext"]
    assert "outside" in denied(tool_call(*OUTSIDE_WRITE, git_repo, home))


def test_without_a_session_id_nothing_is_recorded(git_repo, home):
    context = user_types("/wf-allow-outside", git_repo, home, session=None)["hookSpecificOutput"]["additionalContext"]
    assert "cannot tell which session" in context
    assert not (git_repo / ".workforce" / "team" / "allow-outside.json").exists()


def test_claude_cannot_write_the_marker_itself(git_repo, home):
    marker = git_repo / ".workforce" / "team" / "allow-outside.json"
    for tool_name, tool_input in (("Write", {"file_path": str(marker), "content": "{}"}), ("Edit", {"file_path": str(marker), "old_string": "a", "new_string": "b"})):
        assert "/wf-allow-outside" in denied(tool_call(tool_name, tool_input, git_repo, home))
    for command in ("echo '{\"session_id\": \"s1\"}' > .workforce/team/allow-outside.json", "touch .workforce/team/allow-outside.json"):
        assert "/wf-allow-outside" in denied(tool_call("Bash", {"command": command}, git_repo, home))
    script = git_repo / "grant.py"
    assert "allow-outside" in denied(tool_call("Write", {"file_path": str(script), "content": "open('.workforce/team/allow-outside.json', 'w').write('{}')\n"}, git_repo, home))


def asked(output):
    assert output and output["hookSpecificOutput"]["permissionDecision"] == "ask"
    return output["hookSpecificOutput"]["permissionDecisionReason"]


def test_in_the_normal_mode_a_write_outside_the_project_asks_the_user(git_repo, home):
    reason = asked(tool_call(*OUTSIDE_WRITE, git_repo, home, mode="default"))
    assert "/opt/wf-outside/notes.txt" in reason and "only if you asked for it" in reason
    for command in ("rm -rf /opt/wf-outside", "cp README.md /opt/wf-outside/", "echo x > /opt/wf-outside/notes.txt"):
        assert asked(tool_call("Bash", {"command": command}, git_repo, home, mode="default")), command


@pytest.mark.parametrize("mode", [None, "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"])
def test_other_modes_keep_refusing_because_the_prompt_may_never_reach_the_user(git_repo, home, mode):
    assert "outside" in denied(tool_call(*OUTSIDE_WRITE, git_repo, home, mode=mode))


def test_asking_never_replaces_the_hard_refusals(git_repo, home):
    assert "auth.json" in denied(tool_call("Bash", {"command": "cat ~/.codex/auth.json"}, git_repo, home, mode="default"))
    assert "credentials" in denied(tool_call("Read", {"file_path": "~/.claude/.credentials.json"}, git_repo, home, mode="default"))
    assert "force" in denied(tool_call("Bash", {"command": "git push --force origin main"}, git_repo, home, mode="default"))
    for command in ("rm -rf /", "rm -rf ~"):
        assert denied(tool_call("Bash", {"command": command}, git_repo, home, mode="default")), command


def granted_context(text, git_repo, home, session="s1"):
    return user_types(text, git_repo, home, session=session)["hookSpecificOutput"]["additionalContext"]


def test_the_user_can_allow_one_folder(git_repo, home):
    assert "/opt/wf-granted" in granted_context("/wf-allow-outside /opt/wf-granted", git_repo, home)
    for tool_input in (
        ("Write", {"file_path": "/opt/wf-granted/report.md", "content": "x"}),
        ("Bash", {"command": "echo x > /opt/wf-granted/sub/notes.txt"}),
        ("Bash", {"command": "rm -rf /opt/wf-granted/old"}),
        ("Bash", {"command": "rm -rf /opt/wf-granted"}),
        ("Bash", {"command": "find /opt/wf-granted -name '*.tmp' -delete"}),
    ):
        assert tool_call(*tool_input, git_repo, home) is None, tool_input
    assert "outside" in denied(tool_call("Write", {"file_path": "/opt/wf-granted-not/x", "content": "x"}, git_repo, home))
    assert "outside" in denied(tool_call("Bash", {"command": "rm -rf /opt"}, git_repo, home))
    assert "outside" in denied(tool_call(*OUTSIDE_WRITE, git_repo, home))


def test_a_file_grant_covers_only_that_file(git_repo, home):
    granted_context("/wf-allow-outside /opt/wf-file/notes.txt", git_repo, home)
    assert tool_call("Write", {"file_path": "/opt/wf-file/notes.txt", "content": "x"}, git_repo, home) is None
    assert "outside" in denied(tool_call("Write", {"file_path": "/opt/wf-file/other.txt", "content": "x"}, git_repo, home))


def test_key_and_env_files_inside_a_granted_folder_stay_off_limits(git_repo, home):
    granted_context("/wf-allow-outside /opt/wf-granted", git_repo, home)
    for path in ("/opt/wf-granted/.env", "/opt/wf-granted/.env.local", "/opt/wf-granted/id_ed25519", "/opt/wf-granted/.aws/config", "/opt/wf-granted/x/.ssh/k"):
        assert denied(tool_call("Write", {"file_path": path, "content": "x"}, git_repo, home)), path


def test_links_and_dot_dots_are_followed_before_granting(git_repo, home, tmp_path):
    link = tmp_path / "shortcut"
    link.symlink_to("/opt/wf-real-target")
    context = granted_context(f"/wf-allow-outside {link} /opt/wf-a/../wf-b", git_repo, home)
    assert "/opt/wf-real-target" in context and "/opt/wf-b" in context and ".." not in context
    assert tool_call("Write", {"file_path": "/opt/wf-real-target/x", "content": "x"}, git_repo, home) is None
    assert tool_call("Write", {"file_path": "/opt/wf-b/x", "content": "x"}, git_repo, home) is None
    assert denied(tool_call("Write", {"file_path": "/opt/wf-a/x", "content": "x"}, git_repo, home))


@pytest.mark.parametrize(
    "path, needle",
    [
        ("/", "whole disk or your home folder"),
        ("~", "whole disk or your home folder"),
        ("~/..", "whole disk or your home folder"),
        ("~/.ssh", "logins, keys"),
        ("~/.workforce/plugin", "logins, keys"),
        ("~/.claude", "logins, keys"),
        ("/opt/wf-*", "wildcards"),
    ],
)
def test_some_folders_can_never_be_granted(git_repo, home, path, needle):
    context = granted_context(f"/wf-allow-outside {path}", git_repo, home)
    assert "did not allow" in context and needle in context
    assert not (git_repo / ".workforce" / "team" / "allow-outside.json").exists()


def test_off_or_the_bare_command_clears_folder_grants(git_repo, home):
    granted_context("/wf-allow-outside /opt/wf-granted", git_repo, home)
    assert "off again" in granted_context("/wf-allow-outside off", git_repo, home)
    assert denied(tool_call("Write", {"file_path": "/opt/wf-granted/x", "content": "x"}, git_repo, home))
    granted_context("/wf-allow-outside /opt/wf-granted", git_repo, home)
    assert "off again" in granted_context("/wf-allow-outside", git_repo, home)
    assert denied(tool_call("Write", {"file_path": "/opt/wf-granted/x", "content": "x"}, git_repo, home))


def test_folder_grants_are_bound_to_the_session_that_typed_them(git_repo, home):
    granted_context("/wf-allow-outside /opt/wf-granted", git_repo, home, session="s1")
    assert denied(tool_call("Write", {"file_path": "/opt/wf-granted/x", "content": "x"}, git_repo, home, session="s2"))
    assert tool_call("Write", {"file_path": "/opt/wf-granted/x", "content": "x"}, git_repo, home, session="s1") is None


def test_a_marker_from_the_earlier_version_still_means_everything(git_repo, home):
    folder = git_repo / ".workforce" / "team"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "allow-outside.json").write_text(json.dumps({"session_id": "s1", "at": 1}))
    assert tool_call(*OUTSIDE_WRITE, git_repo, home) is None


def test_the_plugin_has_the_command():
    from workforce.team import plugin_runtime

    text = (plugin_runtime.source_dir() / "commands" / "wf-allow-outside.md").read_text()
    assert "turned writing outside the project folder on or off" in text
