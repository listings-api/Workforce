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
