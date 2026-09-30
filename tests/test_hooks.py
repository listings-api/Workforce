import io
import json
import os
import shlex
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest

from tests.test_decider import FakeOllaya
from workforce import config as wf_config
from workforce.decider import hooks
from workforce.decider.base import Decision
from workforce.decider.laya import LayaDecider
from workforce.events import EventLog
from workforce.paths import Paths

HOME = Path("/Users/tester")
WORKTREE = "/work/repo-wt"


class FakeDecider:
    def __init__(self, answer=False, confidence=0.99, source="laya"):
        self.answer = answer
        self.confidence = confidence
        self.source = source
        self.calls: list[tuple[str, str, str]] = []

    def available(self):
        return True

    def ask_bool(self, key, state, question):
        self.calls.append((key, state, question))
        return Decision(key, question, self.answer, self.confidence, self.source)

    def ask_choice(self, key, state, question, options):
        raise AssertionError("the hook only asks booleans")


def bash(command: str, cwd: str = WORKTREE) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd, "session_id": "s1"}


def run_main(agent: str, payload, repo: Path, decider=None, threshold=0.85, extra_env=None, raw: str | None = None, home=None):
    stdin = io.StringIO(raw if raw is not None else json.dumps(payload))
    stderr = io.StringIO()
    env = {"WF_REPO": str(repo), "WF_WORKTREE": WORKTREE, **(extra_env or {})}
    code = hooks.main([agent], stdin=stdin, stderr=stderr, env=env, decider=decider, threshold=threshold, home=home)
    return code, stderr.getvalue()


def events_of(repo: Path, kind="decision") -> list[dict]:
    return [event for event in EventLog(Paths(repo)).read()[0] if event["kind"] == kind]


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
def test_denylist_blocks_even_when_laya_says_safe(tmp_path, command, needle):
    decider = FakeDecider(answer=False, confidence=0.99)
    code, stderr = run_main("claude", bash(command), tmp_path, decider=decider)
    assert code == 2, command
    assert "risk gate blocked" in stderr
    assert needle in stderr
    assert decider.calls == [], "the deny-list must run before Laya is consulted"
    logged = events_of(tmp_path)[0]
    assert logged["stage"] == "hook:denylist" and logged["allowed"] is False


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
    decider = FakeDecider(answer=False, confidence=0.99)
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": WORKTREE}
    code, stderr = run_main("codex" if tool == "apply_patch" else "claude", payload, tmp_path, decider=decider)
    assert code == 2
    assert decider.calls == []


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
def test_denylist_lets_ordinary_commands_through_to_laya(tmp_path, command):
    decider = FakeDecider(answer=False, confidence=0.99)
    code, _ = run_main("claude", bash(command), tmp_path, decider=decider)
    assert code == 0, command
    assert len(decider.calls) == 1


def test_denylist_allows_reading_claude_config_that_is_not_a_credential(tmp_path):
    decider = FakeDecider(answer=False, confidence=0.99)
    payload = {"tool_name": "Read", "tool_input": {"file_path": "~/.claude/CLAUDE.md"}, "cwd": WORKTREE}
    assert run_main("claude", payload, tmp_path, decider=decider)[0] == 0


def _repo_with_branch(git_repo: Path, branch: str) -> Path:
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "-B", branch], check=True)
    return git_repo


def test_reset_hard_denied_on_main_branch(git_repo):
    reason = hooks.denylist_reason("Bash", {"command": "git reset --hard HEAD~1"}, str(git_repo), str(git_repo), HOME)
    assert reason and "main" in reason


def test_reset_hard_denied_on_master_branch(git_repo):
    _repo_with_branch(git_repo, "master")
    assert hooks.denylist_reason("Bash", {"command": "git reset --hard"}, str(git_repo), str(git_repo), HOME)


def test_reset_hard_allowed_on_task_branch_but_not_toward_main(git_repo):
    _repo_with_branch(git_repo, "wf/R1/T1")
    assert hooks.denylist_reason("Bash", {"command": "git reset --hard HEAD~1"}, str(git_repo), str(git_repo), HOME) is None
    assert hooks.denylist_reason("Bash", {"command": "git reset --hard origin/main"}, str(git_repo), str(git_repo), HOME)


def test_reset_hard_denied_when_branch_unknown(tmp_path):
    reason = hooks.denylist_reason("Bash", {"command": "git reset --hard"}, str(tmp_path), str(tmp_path), HOME)
    assert reason and "unknown" in reason


def test_reset_hard_with_dash_c_uses_that_directory(git_repo, tmp_path):
    reason = hooks.denylist_reason("Bash", {"command": f"git -C {git_repo} reset --hard"}, str(tmp_path), str(tmp_path), HOME)
    assert reason and "main" in reason


def test_rm_relative_targets_follow_cd_inside_worktree(tmp_path):
    (tmp_path / "sub").mkdir()
    ok = hooks.denylist_reason("Bash", {"command": "cd sub && rm -rf out"}, str(tmp_path), str(tmp_path), HOME)
    bad = hooks.denylist_reason("Bash", {"command": "cd .. && rm -rf out"}, str(tmp_path), str(tmp_path), HOME)
    assert ok is None
    assert bad


def test_rm_symlink_escaping_worktree_is_denied(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "link").symlink_to(outside)
    reason = hooks.denylist_reason("Bash", {"command": "rm -rf link"}, str(worktree), str(worktree), HOME)
    assert reason and "outside the worktree" in reason


def test_laya_confident_risky_denies(tmp_path):
    decider = FakeDecider(answer=True, confidence=0.93)
    code, stderr = run_main("claude", bash("curl -X POST https://api.stripe.com/v1/charges"), tmp_path, decider=decider)
    assert code == 2
    assert "Laya judged this risky" in stderr
    assert decider.calls[0][0] == "risk_gate"


def test_laya_confident_safe_allows_even_non_read_only(tmp_path):
    decider = FakeDecider(answer=False, confidence=0.95)
    code, stderr = run_main("claude", bash("pytest -q"), tmp_path, decider=decider)
    assert (code, stderr) == (0, "")


@pytest.mark.parametrize("source,confidence", [("laya", 0.4), ("fallback", None), ("off", None)])
def test_unsure_and_not_read_only_is_allowed_but_flagged(tmp_path, source, confidence):
    decider = FakeDecider(answer=True, confidence=confidence, source=source)
    code, stderr = run_main("claude", bash("pytest -q"), tmp_path, decider=decider)
    assert (code, stderr) == (0, "")
    logged = events_of(tmp_path)[0]
    assert logged["allowed"] is True
    assert logged["flagged"] is True
    assert logged["action"] == "flag_unsure_not_read_only"
    assert logged["stage"] == "hook:unsure_flagged"


@pytest.mark.parametrize(
    "payload",
    [
        bash("ls -la"),
        bash("git status"),
        bash("git diff HEAD~1 | head -50"),
        bash("cat README.md 2>/dev/null"),
        bash("grep -rn TODO src | wc -l"),
        bash("find . -name '*.py'"),
        bash("git branch -a"),
        {"tool_name": "Read", "tool_input": {"file_path": "src/a.py"}, "cwd": WORKTREE},
        {"tool_name": "Grep", "tool_input": {"pattern": "x"}, "cwd": WORKTREE},
        {"tool_name": "Glob", "tool_input": {"pattern": "*.py"}, "cwd": WORKTREE},
    ],
)
def test_unsure_but_read_only_allows(tmp_path, payload):
    decider = FakeDecider(answer=True, confidence=0.3)
    code, stderr = run_main("claude", payload, tmp_path, decider=decider)
    assert (code, stderr) == (0, "")
    logged = events_of(tmp_path)[0]
    assert logged["stage"] == "hook:unsure_read_only"
    assert "flagged" not in logged


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
    assert hooks.is_read_only("Bash", {"command": command}) is False


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "Task", "mcp__x__y"])
def test_edit_tools_are_not_read_only(tool):
    assert hooks.is_read_only(tool, {"file_path": "a.py"}) is False


def assert_quiet_pass(event: dict, allowed: bool = True) -> None:
    assert event["stage"] == "hook:denylist_pass"
    assert event["allowed"] is allowed
    assert "flagged" not in event and event.get("action") is None
    assert not any(key.startswith("laya_") for key in event)


@pytest.mark.parametrize("decider_factory", ["fallback", "null"])
def test_untrusted_decider_only_applies_the_denylist_and_logs_one_quiet_event(tmp_path, decider_factory):
    from workforce.decider.base import NullDecider
    from workforce.decider.fallback import FallbackDecider

    make_decider = FallbackDecider if decider_factory == "fallback" else NullDecider
    calls = [
        {"tool_name": "Write", "tool_input": {"file_path": "a.py", "content": "x"}, "cwd": WORKTREE},
        {"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "a", "new_string": "b"}, "cwd": WORKTREE},
        bash("pytest -q"),
        {"tool_name": "Read", "tool_input": {"file_path": "a.py"}, "cwd": WORKTREE},
    ]
    for call in calls:
        assert run_main("claude", call, tmp_path, decider=make_decider()) == (0, "")
    logged = events_of(tmp_path)
    assert len(logged) == len(calls)
    for event in logged:
        assert_quiet_pass(event)


def test_untrusted_decider_still_blocks_denylist_matches(tmp_path):
    from workforce.decider.fallback import FallbackDecider

    code, stderr = run_main("claude", bash("git push --force"), tmp_path, decider=FallbackDecider())
    assert code == 2 and "hard deny-list" in stderr
    logged = events_of(tmp_path)
    assert len(logged) == 1 and logged[0]["stage"] == "hook:denylist"


def test_claude_contract_allow_is_silent_exit_zero(tmp_path, capsys):
    code, stderr = run_main("claude", bash("ls"), tmp_path, decider=FakeDecider(answer=False))
    assert code == 0 and stderr == ""
    assert capsys.readouterr().out == ""


def test_claude_contract_deny_is_exit_two_with_reason_on_stderr(tmp_path, capsys):
    code, stderr = run_main("claude", bash("git push --force"), tmp_path, decider=FakeDecider())
    assert code == 2
    assert stderr.startswith("WorkForce risk gate blocked this tool call:")
    assert "Do not retry" in stderr
    assert capsys.readouterr().out == ""


def test_codex_contract_bash_and_apply_patch(tmp_path):
    codex_bash = {
        "session_id": "t1", "turn_id": "u1", "transcript_path": None, "cwd": WORKTREE, "hook_event_name": "PreToolUse",
        "model": "gpt-6-astra", "permission_mode": "default", "tool_name": "Bash", "tool_use_id": "c1",
        "tool_input": {"command": "cat ~/.codex/auth.json"},
    }
    code, stderr = run_main("codex", codex_bash, tmp_path, decider=FakeDecider(answer=False))
    assert code == 2 and "auth.json" in stderr

    patch = {**codex_bash, "tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Add File: src/new.py\n+x\n*** End Patch"}}
    assert run_main("codex", patch, tmp_path, decider=FakeDecider(answer=False, confidence=0.99))[0] == 0
    assert run_main("codex", patch, tmp_path, decider=FakeDecider(answer=False, confidence=0.1))[0] == 0
    assert run_main("codex", patch, tmp_path, decider=FakeDecider(answer=True, confidence=0.99))[0] == 2

    safe = {**codex_bash, "tool_input": {"command": "ls"}}
    assert run_main("codex", safe, tmp_path, decider=FakeDecider(answer=True, confidence=0.1)) == (0, "")


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '"text"'])
def test_unreadable_payload_fails_closed(tmp_path, raw):
    code, stderr = run_main("claude", None, tmp_path, decider=FakeDecider(), raw=raw)
    assert code == 2 and "unreadable hook payload" in stderr


def test_payload_without_tool_name_fails_closed(tmp_path):
    code, _ = run_main("claude", {"tool_input": {}}, tmp_path, decider=FakeDecider())
    assert code == 2


def test_missing_repo_env_fails_closed():
    stderr = io.StringIO()
    code = hooks.main(["claude"], stdin=io.StringIO("{}"), stderr=stderr, env={}, decider=FakeDecider(), threshold=0.85)
    assert code == 2 and "WF_REPO" in stderr.getvalue()


def test_bad_agent_argument_is_rejected(tmp_path):
    for argv in (["gemini"], [], ["claude", "extra"]):
        stderr = io.StringIO()
        assert hooks.main(argv, stdin=io.StringIO("{}"), stderr=stderr, env={"WF_REPO": str(tmp_path)}, decider=FakeDecider(), threshold=0.85) == 2
        assert "usage" in stderr.getvalue()


def test_injected_decider_needs_threshold(tmp_path):
    with pytest.raises(ValueError):
        hooks.main(["claude"], stdin=io.StringIO(json.dumps(bash("ls"))), stderr=io.StringIO(), env={"WF_REPO": str(tmp_path)}, decider=FakeDecider())


def test_every_decision_is_appended_to_events_jsonl(tmp_path):
    run_main("claude", bash("ls"), tmp_path, decider=FakeDecider(answer=False))
    run_main("claude", bash("rm -rf /"), tmp_path, decider=FakeDecider())
    run_main("codex", bash("pytest"), tmp_path, decider=FakeDecider(answer=True))
    lines = (tmp_path / ".workforce" / "events.jsonl").read_text().splitlines()
    logged = [json.loads(line) for line in lines]
    assert [event["allowed"] for event in logged] == [True, False, False]
    assert [event["agent"] for event in logged] == ["claude", "claude", "codex"]
    assert all(event["kind"] == "decision" and event["key"] == "risk_gate" for event in logged)
    assert logged[1]["summary"] == "rm -rf /"
    assert logged[0]["session_id"] == "s1"


def test_logged_summary_is_clipped(tmp_path):
    run_main("claude", bash("echo " + "x" * 5000), tmp_path, decider=FakeDecider(answer=False))
    assert len(events_of(tmp_path)[0]["summary"]) <= 300


@pytest.fixture
def toml_repo(tmp_path):
    def build(backend: str, url: str = "http://127.0.0.1:11435") -> Path:
        repo = tmp_path / "target"
        repo.mkdir(exist_ok=True)
        text = wf_config.default_toml()
        text = text.replace('backend = "laya"', f'backend = "{backend}"').replace("http://127.0.0.1:11435", url)
        text = text.replace("[decider.thresholds]\n", "[decider.thresholds]\nrisk_gate = 0.85\n")
        (repo / "workforce.toml").write_text(text)
        return repo

    return build


def hook_subprocess(agent: str, payload: dict, repo: Path, cwd: Path, worktree: Path | None = None) -> subprocess.CompletedProcess:
    package_root = Path(hooks.workforce.__file__).resolve().parent.parent
    env = {**os.environ, "WF_REPO": str(repo), "PYTHONPATH": str(package_root)}
    env.pop("WF_WORKTREE", None)
    if worktree is not None:
        env["WF_WORKTREE"] = str(worktree)
    return subprocess.run(
        [sys.executable, "-m", "workforce.decider.hooks", agent],
        input=json.dumps(payload), capture_output=True, text=True, cwd=cwd, env=env, timeout=60,
    )


def test_subprocess_contract_with_decider_off(toml_repo, tmp_path):
    repo = toml_repo("off")
    work = tmp_path / "wt"
    work.mkdir()
    allowed = hook_subprocess("claude", bash("ls", str(work)), repo, work)
    assert (allowed.returncode, allowed.stdout, allowed.stderr) == (0, "", "")
    passed = hook_subprocess("claude", bash("pytest -q", str(work)), repo, work)
    assert (passed.returncode, passed.stdout, passed.stderr) == (0, "", "")
    denied = hook_subprocess("codex", bash("git push --force", str(work)), repo, work)
    assert denied.returncode == 2 and "force" in denied.stderr
    logged = events_of(repo)
    assert len(logged) == 3
    assert [event["stage"] for event in logged] == ["hook:denylist_pass", "hook:denylist_pass", "hook:denylist"]
    assert not any("flagged" in event for event in logged)


def test_subprocess_with_live_fake_server_uses_repo_config(toml_repo, tmp_path):
    server = FakeOllaya()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        repo = toml_repo("laya", server.url)
        work = tmp_path / "wt"
        work.mkdir()
        server.choose = "no"
        server.confidence = 0.97
        safe = hook_subprocess("claude", bash("pytest -q", str(work)), repo, work)
        assert safe.returncode == 0 and safe.stderr == ""
        server.choose = "yes"
        risky = hook_subprocess("claude", bash("pytest -q", str(work)), repo, work)
        assert risky.returncode == 2 and "risky" in risky.stderr
        server.mode = "unsure"
        unsure = hook_subprocess("claude", bash("pytest -q", str(work)), repo, work)
        assert (unsure.returncode, unsure.stderr) == (0, "")
        assert server.requests[0]["body"]["questions"]["q"]["criteria"]["yes"].startswith("Risky")
    finally:
        server.shutdown()
        server.server_close()
    stages = [event["stage"] for event in events_of(repo) if str(event.get("stage", "")).startswith("hook:")]
    assert stages == ["hook:laya_safe", "hook:laya_risky", "hook:unsure_flagged"]


def test_subprocess_with_unreachable_laya_falls_back(toml_repo, tmp_path):
    repo = toml_repo("laya", "http://127.0.0.1:1")
    work = tmp_path / "wt"
    work.mkdir()
    assert hook_subprocess("claude", bash("ls", str(work)), repo, work).returncode == 0
    assert hook_subprocess("claude", bash("pytest", str(work)), repo, work).returncode == 0
    assert [event["stage"] for event in events_of(repo)] == ["hook:denylist_pass", "hook:denylist_pass"]
    assert not any("flagged" in event for event in events_of(repo))


def test_subprocess_with_broken_config_is_advisory_only(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    (repo / "workforce.toml").write_text("not = [valid")
    work = tmp_path / "wt"
    work.mkdir()
    assert hook_subprocess("claude", bash("pytest", str(work)), repo, work).returncode == 0
    assert hook_subprocess("claude", bash("git push --force", str(work)), repo, work).returncode == 2
    assert events_of(repo, "error")
    assert [event["stage"] for event in events_of(repo)] == ["hook:denylist_pass", "hook:denylist"]


def test_claude_settings_json_registers_a_working_pretooluse_hook(toml_repo, tmp_path):
    repo = toml_repo("off")
    work = tmp_path / "wt"
    work.mkdir()
    settings = json.loads(hooks.claude_settings_json(repo, worktree=work))
    groups = settings["hooks"]["PreToolUse"]
    assert len(groups) == 1 and groups[0]["matcher"] == "*"
    handler = groups[0]["hooks"][0]
    assert handler["type"] == "command" and handler["timeout"] == hooks.HOOK_TIMEOUT_S
    command = handler["command"]
    assert f"WF_REPO={shlex.quote(str(repo.resolve()))}" in command
    assert "-m workforce.decider.hooks claude" in command

    payload = bash("git push --force", str(work))
    blocked = subprocess.run(["sh", "-c", command], input=json.dumps(payload), capture_output=True, text=True, cwd=work, timeout=60)
    assert blocked.returncode == 2 and "force" in blocked.stderr
    allowed = subprocess.run(["sh", "-c", command], input=json.dumps(bash("ls", str(work))), capture_output=True, text=True, cwd=work, timeout=60)
    assert allowed.returncode == 0


def test_claude_settings_json_worktree_is_optional_and_quoted(tmp_path):
    repo = tmp_path / "my repo"
    repo.mkdir()
    command = json.loads(hooks.claude_settings_json(repo))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "WF_WORKTREE" not in command
    assert shlex.quote(str(repo.resolve())) in command
    with_worktree = json.loads(hooks.claude_settings_json(repo, worktree=tmp_path / "w t"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "WF_WORKTREE=" in with_worktree


def test_codex_hook_args_are_valid_toml_overrides_for_a_working_hook(toml_repo, tmp_path):
    repo = toml_repo("off")
    work = tmp_path / "wt"
    work.mkdir()
    args = hooks.codex_hook_args(repo, worktree=work)
    assert args[0] == "--dangerously-bypass-hook-trust"
    assert args[1] == "-c"
    key, _, value = args[2].partition("=")
    assert key == "hooks.PreToolUse"
    parsed = tomllib.loads(f"value = {value}")["value"]
    handler = parsed[0]["hooks"][0]
    assert handler["type"] == "command" and handler["timeout"] == hooks.HOOK_TIMEOUT_S
    assert "-m workforce.decider.hooks codex" in handler["command"]

    blocked = subprocess.run(
        ["sh", "-c", handler["command"]], input=json.dumps(bash("cat ~/.ssh/id_rsa", str(work))),
        capture_output=True, text=True, cwd=work, timeout=60,
    )
    assert blocked.returncode == 2 and "ssh" in blocked.stderr


def test_denylist_only_mode_allows_ordinary_calls_without_asking_laya(tmp_path):
    decider = FakeDecider(answer=True, confidence=0.99)
    code, stderr = run_main(
        "claude", bash("git commit -m 'add feature'"), tmp_path, decider=decider, extra_env={hooks.MODE_ENV: hooks.MODE_DENYLIST_ONLY}
    )
    assert (code, stderr) == (0, "")
    assert decider.calls == []
    assert events_of(tmp_path)[0]["stage"] == "hook:denylist_only"


@pytest.mark.parametrize("command", ["git commit --no-gpg-sign -m x", "git push --force", "cat ~/.ssh/id_rsa", "rm -rf /"])
def test_denylist_only_mode_still_enforces_the_denylist(tmp_path, command):
    code, stderr = run_main("claude", bash(command), tmp_path, decider=FakeDecider(), extra_env={hooks.MODE_ENV: hooks.MODE_DENYLIST_ONLY})
    assert code == 2 and "hard deny-list" in stderr


def test_denylist_only_settings_and_args_carry_the_mode(toml_repo, tmp_path):
    repo = toml_repo("laya", "http://127.0.0.1:1")
    work = tmp_path / "wt"
    work.mkdir()
    command = json.loads(hooks.claude_settings_json(repo, worktree=work, denylist_only=True))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert f"{hooks.MODE_ENV}={hooks.MODE_DENYLIST_ONLY}" in command
    commit = subprocess.run(["sh", "-c", command], input=json.dumps(bash("git commit -m x", str(work))), capture_output=True, text=True, cwd=work, timeout=60)
    assert commit.returncode == 0
    forced = subprocess.run(["sh", "-c", command], input=json.dumps(bash("git commit --no-gpg-sign", str(work))), capture_output=True, text=True, cwd=work, timeout=60)
    assert forced.returncode == 2
    default_command = json.loads(hooks.claude_settings_json(repo))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert f"{hooks.MODE_ENV}={hooks.MODE_AGENT}" in default_command
    assert f"{hooks.MODE_ENV}={hooks.MODE_DENYLIST_ONLY}" in hooks.codex_hook_args(repo, denylist_only=True)[2]


@pytest.fixture
def ollaya():
    server = FakeOllaya()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _laya(server, tmp_path, **kwargs) -> LayaDecider:
    return LayaDecider(server.url, 0.85, EventLog(Paths(tmp_path)), **kwargs)


def test_risky_answer_below_the_risk_gate_threshold_is_only_flagged(ollaya, tmp_path):
    ollaya.choose = "yes"
    ollaya.confidence = 0.7
    decider = _laya(ollaya, tmp_path, thresholds={"risk_gate": 0.9})
    code, stderr = run_main("claude", bash("pytest -q"), tmp_path, decider=decider, threshold=0.5)
    assert (code, stderr) == (0, "")
    hook_event = [e for e in events_of(tmp_path) if str(e.get("stage", "")).startswith("hook:")][0]
    assert hook_event["stage"] == "hook:unsure_flagged" and hook_event["flagged"] is True


def test_risky_answer_at_the_risk_gate_threshold_blocks_even_below_the_global_default(ollaya, tmp_path):
    ollaya.choose = "yes"
    ollaya.confidence = 0.7
    decider = _laya(ollaya, tmp_path, thresholds={"risk_gate": 0.6})
    code, stderr = run_main("claude", bash("pytest -q"), tmp_path, decider=decider, threshold=0.99)
    assert code == 2 and "Laya judged this risky" in stderr


def test_unreliable_risk_gate_never_blocks_and_never_asks_laya(ollaya, tmp_path):
    ollaya.choose = "yes"
    ollaya.confidence = 0.999
    decider = _laya(ollaya, tmp_path, thresholds={"agent_progress": 0.8})
    code, stderr = run_main("claude", bash("pytest -q"), tmp_path, decider=decider, threshold=0.5)
    assert (code, stderr) == (0, "")
    assert ollaya.requests == [] and ollaya.model_gets == 0
    logged = events_of(tmp_path)
    assert len(logged) == 1
    assert_quiet_pass(logged[0])


def test_default_config_without_a_risk_gate_threshold_never_flags(toml_repo, tmp_path, ollaya):
    repo = toml_repo("laya", ollaya.url)
    text = (repo / "workforce.toml").read_text().replace("risk_gate = 0.85\n", "")
    (repo / "workforce.toml").write_text(text)
    work = tmp_path / "wt"
    work.mkdir()
    for command in ["pytest -q", "git add -A", "ls"]:
        completed = hook_subprocess("claude", bash(command, str(work)), repo, work)
        assert (completed.returncode, completed.stdout, completed.stderr) == (0, "", "")
    assert ollaya.requests == [] and ollaya.model_gets == 0
    logged = events_of(repo)
    assert len(logged) == 3
    for event in logged:
        assert_quiet_pass(event)


def test_trusted_risk_gate_with_an_unavailable_server_passes_quietly(ollaya, tmp_path):
    ollaya.mode = "error"
    decider = _laya(ollaya, tmp_path, thresholds={"risk_gate": 0.5})
    assert run_main("claude", bash("pytest -q"), tmp_path, decider=decider, threshold=0.5) == (0, "")
    assert ollaya.requests == []
    assert_quiet_pass(events_of(tmp_path)[-1])


def test_denylist_still_blocks_when_risk_gate_is_unreliable_or_says_safe(ollaya, tmp_path):
    ollaya.choose = "no"
    decider = _laya(ollaya, tmp_path, thresholds={})
    assert run_main("claude", bash("git push --force"), tmp_path, decider=decider, threshold=0.5)[0] == 2
    assert ollaya.requests == []


def test_risk_gate_uses_its_own_model(ollaya, tmp_path):
    ollaya.models = ["laya:en", "laya:typed-decisions"]
    ollaya.choose = "no"
    ollaya.confidence = 0.95
    decider = _laya(ollaya, tmp_path, models={"risk_gate": "laya:typed-decisions"})
    assert run_main("claude", bash("pytest -q"), tmp_path, decider=decider, threshold=0.5)[0] == 0
    assert ollaya.requests[0]["body"]["model"] == "laya:typed-decisions"


def _managed_worktree(repo: Path, home: Path, run_id="R1", task_id="T2") -> Path:
    path = Paths(repo, home=home).worktree(run_id, task_id)
    path.mkdir(parents=True)
    return path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir(parents=True)
    return repo


def test_managed_worktree_tags_every_hook_decision_with_run_and_task(tmp_path):
    repo, home = _repo(tmp_path), tmp_path / "home"
    work = _managed_worktree(repo, home)
    env = {"WF_WORKTREE": str(work)}
    run_main("claude", bash("ls", str(work)), repo, decider=FakeDecider(answer=False), extra_env=env, home=home)
    run_main("claude", bash("git push --force", str(work)), repo, decider=FakeDecider(), extra_env=env, home=home)
    run_main("claude", bash("pytest -q", str(work)), repo, decider=FakeDecider(answer=True, confidence=0.2), extra_env=env, home=home)
    run_main("codex", bash("pytest -q", str(work)), repo, decider=FakeDecider(answer=True, confidence=0.99), extra_env=env, home=home)
    logged = events_of(repo)
    assert [event["stage"] for event in logged] == ["hook:laya_safe", "hook:denylist", "hook:unsure_flagged", "hook:laya_risky"]
    assert all(event["run_id"] == "R1" and event["task_id"] == "T2" for event in logged)


def test_managed_worktree_tags_events_from_the_laya_decider_and_config_errors_too(tmp_path, ollaya):
    repo, home = _repo(tmp_path), tmp_path / "home"
    work = _managed_worktree(repo, home, "R7", "T3")
    ollaya.choose = "no"
    ollaya.confidence = 0.95
    text = wf_config.default_toml().replace("http://127.0.0.1:11435", ollaya.url)
    (repo / "workforce.toml").write_text(text.replace("[decider.thresholds]\n", "[decider.thresholds]\nrisk_gate = 0.85\n"))
    code, _ = run_main("claude", bash("pytest -q", str(work)), repo, decider=None, extra_env={"WF_WORKTREE": str(work)}, home=home)
    assert code == 0
    decisions = events_of(repo)
    assert {event["source"] if "source" in event else event["stage"] for event in decisions} >= {"laya", "hook:laya_safe"}
    assert all(event["run_id"] == "R7" and event["task_id"] == "T3" for event in decisions)

    broken = _repo(tmp_path / "second")
    (broken / "workforce.toml").write_text("not = [valid")
    other = _managed_worktree(broken, home, "R8", "T1")
    run_main("claude", bash("pytest -q", str(other)), broken, decider=None, extra_env={"WF_WORKTREE": str(other)}, home=home)
    errors = events_of(broken, "error")
    assert errors and errors[0]["run_id"] == "R8" and errors[0]["task_id"] == "T1"


def test_unmanaged_worktrees_leave_run_and_task_out(tmp_path):
    repo, home = _repo(tmp_path), tmp_path / "home"
    root = Paths(repo, home=home).worktrees_root
    (root / "R1" / "T2").mkdir(parents=True)
    elsewhere = tmp_path / "other-repo-wt"
    elsewhere.mkdir()
    other_slug = home / ".workforce" / "worktrees" / "someone-else-deadbeef" / "R1" / "T2"
    other_slug.mkdir(parents=True)
    for worktree in (repo, root / "R1", elsewhere, other_slug, tmp_path / "does-not-exist", root.parent):
        run_main("claude", bash("ls", str(worktree)), repo, decider=FakeDecider(answer=False), extra_env={"WF_WORKTREE": str(worktree)}, home=home)
    logged = events_of(repo)
    assert len(logged) == 6
    assert all("run_id" not in event and "task_id" not in event for event in logged)


def test_no_worktree_env_leaves_run_and_task_out(tmp_path):
    repo = _repo(tmp_path)
    stdin, stderr = io.StringIO(json.dumps(bash("ls"))), io.StringIO()
    env = {"WF_REPO": str(repo)}
    assert hooks.main(["claude"], stdin=stdin, stderr=stderr, env=env, decider=FakeDecider(answer=False), threshold=0.85, home=tmp_path / "home") == 0
    assert "task_id" not in events_of(repo)[0]


def test_managed_ids_resolves_symlinks_and_rejects_other_shapes(tmp_path):
    repo, home = _repo(tmp_path), tmp_path / "home"
    paths = Paths(repo, home=home)
    work = _managed_worktree(repo, home, "R2", "T9")
    link = tmp_path / "link-to-worktree"
    link.symlink_to(work)
    assert hooks.managed_ids(str(work), paths) == {"run_id": "R2", "task_id": "T9"}
    assert hooks.managed_ids(str(link), paths) == {"run_id": "R2", "task_id": "T9"}
    assert hooks.managed_ids(None, paths) == {}
    assert hooks.managed_ids("", paths) == {}
    assert hooks.managed_ids(str(paths.worktrees_root), paths) == {}
    assert hooks.managed_ids(str(paths.worktrees_root.parent), paths) == {}


def test_subprocess_hook_in_a_managed_worktree_tags_events(toml_repo, tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    repo = toml_repo("off")
    work = Paths(repo, home=fake_home).worktree("R1", "T1")
    work.mkdir(parents=True)
    completed = hook_subprocess("claude", bash("ls", str(work)), repo, work, worktree=work)
    assert completed.returncode == 0, completed.stderr
    logged = events_of(repo)[0]
    assert (logged["run_id"], logged["task_id"]) == ("R1", "T1")


AGENT_ENV = {hooks.MODE_ENV: hooks.MODE_AGENT}
COMMITTER_ENV = {hooks.MODE_ENV: hooks.MODE_DENYLIST_ONLY}
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


@pytest.mark.parametrize("command", HISTORY_COMMANDS)
@pytest.mark.parametrize("env", [None, AGENT_ENV], ids=["mode-unset", "mode-agent"])
def test_agent_mode_denies_git_history_changes(tmp_path, command, env):
    decider = FakeDecider(answer=False, confidence=0.99)
    code, stderr = run_main("claude", bash(command), tmp_path, decider=decider, extra_env=env)
    assert code == 2, command
    assert "hard deny-list" in stderr
    assert decider.calls == []
    assert events_of(tmp_path)[0]["stage"] == "hook:denylist"


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_agent_mode_denies_for_both_agents(tmp_path, agent):
    assert run_main(agent, bash("git commit -m x"), tmp_path, decider=FakeDecider(), extra_env=AGENT_ENV)[0] == 2


@pytest.mark.parametrize(
    "command",
    [
        "git status", "git diff HEAD~1", "git log --oneline -5", "git show HEAD", "git add -A", "git add src/a.py",
        "git checkout -- src/a.py", "git branch -a", "git tag", "git tag -l", "git rev-parse HEAD", "git config --get user.name",
        "git diff --stat | head", "pytest -q", "git blame src/a.py",
    ],
)
def test_agent_mode_still_allows_everyday_git_and_commands(tmp_path, command):
    decider = FakeDecider(answer=False, confidence=0.99)
    assert run_main("claude", bash(command), tmp_path, decider=decider, extra_env=AGENT_ENV) == (0, "")
    assert len(decider.calls) == 1


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'add feature'",
        "git commit -S -m x",
        "git push -u origin wf/R1/T1",
        "git push origin wf/R1/T1",
        "git merge --ff-only wf/R1/T1",
        "git rebase main",
        "git commit --amend --no-edit",
        "git stash",
    ],
)
def test_committer_mode_keeps_todays_denylist_and_allows_commit_and_push(tmp_path, command):
    decider = FakeDecider(answer=True, confidence=0.99)
    assert run_main("claude", bash(command), tmp_path, decider=decider, extra_env=COMMITTER_ENV) == (0, "")
    assert decider.calls == []


@pytest.mark.parametrize("value", ["committer", "denylist"])
def test_committer_mode_accepts_both_env_spellings(tmp_path, value):
    assert run_main("claude", bash("git commit -m x"), tmp_path, decider=FakeDecider(), extra_env={hooks.MODE_ENV: value})[0] == 0


def test_unknown_mode_value_is_treated_as_agent(tmp_path):
    assert run_main("claude", bash("git commit -m x"), tmp_path, decider=FakeDecider(), extra_env={hooks.MODE_ENV: "typo"})[0] == 2


def test_denylist_reason_defaults_to_committer_behaviour_and_takes_a_mode():
    assert hooks.denylist_reason("Bash", {"command": "git commit -m x"}, WORKTREE, WORKTREE, HOME) is None
    assert hooks.denylist_reason("Bash", {"command": "git commit -m x"}, WORKTREE, WORKTREE, HOME, mode="committer") is None
    assert hooks.denylist_reason("Bash", {"command": "git commit -m x"}, WORKTREE, WORKTREE, HOME, mode="agent")


def test_evaluate_mode_argument_beats_denylist_only():
    payload = bash("git commit -m x")
    assert hooks.evaluate("claude", payload, FakeDecider(), 0.85, WORKTREE, HOME, denylist_only=True, mode="agent").allow is False
    assert hooks.evaluate("claude", payload, FakeDecider(), 0.85, WORKTREE, HOME, denylist_only=True).allow is True
    assert hooks.evaluate("claude", payload, FakeDecider(), 0.85, WORKTREE, HOME).allow is False
    assert hooks.evaluate("claude", payload, FakeDecider(), 0.85, WORKTREE, HOME, mode="committer").stage == "denylist_only"


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
    reason = hooks.denylist_reason("Bash", {"command": command}, WORKTREE, WORKTREE, HOME, mode=mode)
    assert reason and needle.lower() in reason.lower(), (command, reason)


@pytest.mark.parametrize("command,needle", AUDIT_PROBES[:12] + AUDIT_PROBES[26:30])
def test_audit_probes_block_through_the_hook_entry_point(tmp_path, command, needle):
    decider = FakeDecider(answer=False, confidence=0.99)
    code, stderr = run_main("claude", bash(command), tmp_path, decider=decider, extra_env=COMMITTER_ENV)
    assert code == 2 and "hard deny-list" in stderr
    assert decider.calls == []


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
    assert hooks.denylist_reason("Bash", payload["tool_input"], WORKTREE, WORKTREE, HOME, mode="committer")


def test_array_form_history_commands_are_denied_in_agent_mode(tmp_path):
    payload = {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", "git commit -m x"]}, "cwd": WORKTREE}
    code, stderr = run_main("codex", payload, tmp_path, decider=FakeDecider(), extra_env=AGENT_ENV)
    assert code == 2 and "commit run" in stderr
    assert events_of(tmp_path)[0]["summary"] == "bash -lc 'git commit -m x'"


def test_array_form_ordinary_commands_are_allowed(tmp_path):
    payload = {"tool_name": "Bash", "tool_input": {"command": ["bash", "-lc", "pytest -q"]}, "cwd": WORKTREE}
    assert run_main("codex", payload, tmp_path, decider=FakeDecider(answer=False, confidence=0.99), extra_env=AGENT_ENV) == (0, "")


def test_string_tool_input_is_checked_as_a_command():
    assert hooks.denylist_reason("Bash", "git push --force", WORKTREE, WORKTREE, HOME)


def test_apply_patch_given_as_argv_still_checks_its_paths():
    patch = "*** Begin Patch\n*** Update File: ../../.ssh/config\n@@\n-a\n+b\n*** End Patch"
    assert hooks.denylist_reason("shell", {"command": ["apply_patch", patch]}, WORKTREE, WORKTREE, HOME)


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
    assert hooks.denylist_reason("Bash", {"command": command}, WORKTREE, WORKTREE, HOME, mode="committer") is None, command


def test_project_claude_globs_are_only_allowed_inside_the_worktree():
    assert hooks.denylist_reason("Glob", {"pattern": ".claude/commands/*.md"}, WORKTREE, WORKTREE, HOME) is None
    assert hooks.denylist_reason("Glob", {"pattern": ".claude/*"}, WORKTREE, WORKTREE, HOME) is None
    assert hooks.denylist_reason("Glob", {"pattern": "~/.claude/*"}, WORKTREE, WORKTREE, HOME)
    assert hooks.denylist_reason("Glob", {"pattern": "../../.claude/*"}, WORKTREE, WORKTREE, HOME)
    assert hooks.denylist_reason("Grep", {"pattern": "x", "path": "~/.ss?"}, WORKTREE, WORKTREE, HOME)
    assert hooks.denylist_reason("Glob", {"pattern": "**/.ss*"}, WORKTREE, WORKTREE, HOME)
    assert hooks.denylist_reason("Read", {"file_path": "/Users/tester/.gnupg/pubring.kbx"}, WORKTREE, WORKTREE, HOME)
    assert hooks.denylist_reason("Read", {"file_path": "~/.s\"\"sh/id_rsa"}, WORKTREE, WORKTREE, HOME)


def test_glob_exemption_does_not_survive_a_cd():
    assert hooks.denylist_reason("Bash", {"command": "cd ~ && ls .claude/*"}, WORKTREE, WORKTREE, HOME)
    assert hooks.denylist_reason("Bash", {"command": "ls .claude/*"}, WORKTREE, WORKTREE, HOME) is None


def test_nul_in_a_command_makes_the_helper_raise_not_pass():
    with pytest.raises(ValueError):
        hooks.denylist_reason("Bash", {"command": "rm -rf /tmp/x\x00y"}, WORKTREE, WORKTREE, HOME)


def nul_payload() -> dict:
    return bash("rm -rf /tmp/x\x00y")


def test_nul_payload_is_denied_with_a_risk_gate_error(tmp_path):
    code, stderr = run_main("claude", nul_payload(), tmp_path, decider=FakeDecider())
    assert code == 2
    assert "risk gate error: ValueError" in stderr
    logged = events_of(tmp_path)
    assert len(logged) == 1
    assert logged[0]["allowed"] is False and logged[0]["stage"] == "hook:error"
    assert logged[0]["reason"] == "risk gate error: ValueError"
    errors = events_of(tmp_path, "error")
    assert errors and errors[0]["source"] == "hook" and "ValueError" in errors[0]["message"]


def test_nul_payload_through_a_real_subprocess_exits_two(toml_repo, tmp_path):
    repo = toml_repo("off")
    work = tmp_path / "wt"
    work.mkdir()
    completed = hook_subprocess("claude", nul_payload() | {"cwd": str(work)}, repo, work, worktree=work)
    assert completed.returncode == 2
    assert "risk gate error: ValueError" in completed.stderr
    assert completed.stdout == ""


class RaisingDecider:
    def __init__(self, exc: BaseException):
        self.exc = exc

    def available(self):
        return True

    def ask_bool(self, key, state, question):
        raise self.exc

    def ask_choice(self, key, state, question, options):
        raise self.exc


@pytest.mark.parametrize("exc", [RuntimeError("boom"), OSError("disk"), KeyError("k"), KeyboardInterrupt(), SystemExit(0), MemoryError()])
def test_a_raising_decider_is_a_deny_not_a_bypass(tmp_path, exc):
    decider = RaisingDecider(exc)
    decider.threshold_for = lambda key: 0.5
    code, stderr = run_main("claude", bash("pytest -q"), tmp_path, decider=decider)
    assert code == 2
    assert f"risk gate error: {type(exc).__name__}" in stderr
    assert "Do not retry" in stderr
    assert events_of(tmp_path)[0]["reason"] == f"risk gate error: {type(exc).__name__}"


def test_a_failing_decider_close_is_a_deny(tmp_path):
    class BadClose(FakeDecider):
        def close(self):
            raise RuntimeError("close failed")

    code, stderr = run_main("claude", bash("ls"), tmp_path, decider=BadClose())
    assert code == 2 and "risk gate error: RuntimeError" in stderr


def test_an_unexpected_config_error_is_a_deny(tmp_path, monkeypatch):
    def broken_load(repo):
        raise PermissionError("cannot read workforce.toml")

    monkeypatch.setattr(hooks.wf_config, "load", broken_load)
    code, stderr = run_main("claude", bash("pytest -q"), tmp_path, decider=None)
    assert code == 2 and "risk gate error: PermissionError" in stderr


def test_a_failing_event_log_still_denies(tmp_path, monkeypatch):
    def broken_emit(self, kind, **data):
        raise OSError("read-only file system")

    monkeypatch.setattr(EventLog, "emit", broken_emit)
    code, stderr = run_main("claude", nul_payload(), tmp_path, decider=FakeDecider())
    assert code == 2 and "risk gate error: ValueError" in stderr


def test_a_normal_verdict_is_unchanged_by_the_error_wrapper(tmp_path):
    assert run_main("claude", bash("ls"), tmp_path, decider=FakeDecider(answer=False, confidence=0.99)) == (0, "")


def test_deeply_nested_payload_json_fails_closed(tmp_path):
    raw = "[" * 100000
    code, stderr = run_main("claude", None, tmp_path, decider=FakeDecider(), raw=raw)
    assert code == 2 and "unreadable hook payload" in stderr


def test_hook_command_turns_a_crashing_hook_into_a_deny(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    for mode in ("agent", "committer"):
        command = json.loads(hooks.claude_settings_json(repo, python="false", mode=mode))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert command.endswith("|| exit 2")
        completed = subprocess.run(["sh", "-c", command], input="{}", capture_output=True, text=True, timeout=60)
        assert completed.returncode == 2


def test_hook_command_lets_a_clean_allow_through(toml_repo, tmp_path):
    repo = toml_repo("off")
    work = tmp_path / "wt"
    work.mkdir()
    command = json.loads(hooks.claude_settings_json(repo, worktree=work))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    completed = subprocess.run(["sh", "-c", command], input=json.dumps(bash("ls", str(work))), capture_output=True, text=True, cwd=work, timeout=60)
    assert (completed.returncode, completed.stdout, completed.stderr) == (0, "", "")


def test_subprocess_agent_mode_denies_commit_and_committer_mode_allows_it(toml_repo, tmp_path):
    repo = toml_repo("off")
    work = tmp_path / "wt"
    work.mkdir()
    agent_command = json.loads(hooks.claude_settings_json(repo, worktree=work))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    committer_command = json.loads(hooks.claude_settings_json(repo, worktree=work, mode="committer"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    payload = json.dumps(bash("git commit -m x", str(work)))

    def run(command):
        return subprocess.run(["sh", "-c", command], input=payload, capture_output=True, text=True, cwd=work, timeout=60)

    assert run(agent_command).returncode == 2
    assert run(committer_command).returncode == 0
    forced = subprocess.run(
        ["sh", "-c", committer_command], input=json.dumps(bash("git push --force", str(work))), capture_output=True, text=True, cwd=work, timeout=60
    )
    assert forced.returncode == 2


def test_hook_command_env_carries_the_mode(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()

    def command(**kwargs):
        return json.loads(hooks.claude_settings_json(repo, **kwargs))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    assert f"{hooks.MODE_ENV}=agent" in command()
    assert f"{hooks.MODE_ENV}=agent" in command(mode="agent")
    assert f"{hooks.MODE_ENV}=agent" in command(denylist_only=False)
    assert f"{hooks.MODE_ENV}=denylist" in command(mode="committer")
    assert f"{hooks.MODE_ENV}=denylist" in command(denylist_only=True)
    assert f"{hooks.MODE_ENV}=agent" in command(denylist_only=True, mode="agent")
    assert hooks.claude_settings_json(repo, mode="committer") == hooks.claude_settings_json(repo, denylist_only=True)
    assert hooks.claude_settings_json(repo, mode="agent") == hooks.claude_settings_json(repo)
    with pytest.raises(ValueError):
        hooks.claude_settings_json(repo, mode="yolo")
    with pytest.raises(ValueError):
        hooks.codex_hook_args(repo, mode="yolo")


def test_codex_args_carry_the_mode(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    assert f"{hooks.MODE_ENV}=agent" in hooks.codex_hook_args(repo)[2]
    assert f"{hooks.MODE_ENV}=agent" in hooks.codex_hook_args(repo, mode="agent")[2]
    assert f"{hooks.MODE_ENV}=denylist" in hooks.codex_hook_args(repo, mode="committer")[2]
    assert hooks.codex_hook_args(repo, mode="committer") == hooks.codex_hook_args(repo, denylist_only=True)


def _codex_command(args: list[str]) -> str:
    key, _, value = args[2].partition("=")
    assert key == "hooks.PreToolUse"
    return tomllib.loads(f"value = {value}")["value"][0]["hooks"][0]["command"]


@pytest.mark.parametrize(
    "name",
    ["plain", "with space", "café-é", "日本語", "emoji-😀", "astral-𝔘𝔫𝔦", "quote\"s", "back\\slash", "tab\there", "dollar$and`tick", "semi;colon", "é"],
)
def test_codex_override_round_trips_awkward_and_non_ascii_paths(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir()
    work = tmp_path / f"wt-{name}"
    work.mkdir()
    args = hooks.codex_hook_args(repo, worktree=work)
    command = _codex_command(args)
    assert command == hooks._hook_command("codex", repo, work, None, "agent")
    words = shlex.split(command)
    assert f"WF_REPO={repo.resolve()}" in words
    assert f"WF_WORKTREE={work.resolve()}" in words
    assert "\\ud83d" not in args[2].lower()


def test_codex_override_keeps_astral_characters_literal(tmp_path):
    repo = tmp_path / "😀"
    repo.mkdir()
    value = hooks.codex_hook_args(repo)[2]
    assert "😀" in value and "\\u" not in value


def test_codex_override_executes_for_a_non_ascii_repo(tmp_path):
    repo = tmp_path / "répo-😀"
    repo.mkdir()
    work = tmp_path / "wt"
    work.mkdir()
    handler = _codex_command(hooks.codex_hook_args(repo, worktree=work))
    completed = subprocess.run(["sh", "-c", handler], input=json.dumps(bash("git push --force", str(work))), capture_output=True, text=True, cwd=work, timeout=60)
    assert completed.returncode == 2 and "force" in completed.stderr
    assert events_of(repo)


def test_toml_string_escapes_controls_quotes_and_backslashes():
    for value in ["", "a\"b", "a\\b", "line\nbreak", "\x00\x01\x1f\x7f", "tab\t", "\b\f\r", "😀é日本", "plain ascii"]:
        assert tomllib.loads(f"v = {hooks._toml_string(value)}")["v"] == value


def test_toml_string_refuses_lone_surrogates():
    from workforce.errors import ConfigError

    with pytest.raises(ConfigError):
        hooks._toml_string("bad\udcff")


def test_codex_hook_args_refuse_a_path_that_cannot_be_registered(tmp_path):
    from workforce.errors import ConfigError

    with pytest.raises(ConfigError):
        hooks.codex_hook_args(tmp_path, worktree=Path(os.fsdecode(b"/tmp/bad-\xff-path")))


def test_codex_hook_args_refuse_an_override_that_does_not_parse(tmp_path, monkeypatch):
    from workforce.errors import ConfigError

    monkeypatch.setattr(hooks, "_toml_string", lambda value: '"' + value.replace('"', "") + "\\q")
    with pytest.raises(ConfigError):
        hooks.codex_hook_args(tmp_path)


def test_codex_hook_args_refuse_an_override_that_changes_the_command(tmp_path, monkeypatch):
    from workforce.errors import ConfigError

    monkeypatch.setattr(hooks, "_toml_string", lambda value: '"changed"')
    with pytest.raises(ConfigError):
        hooks.codex_hook_args(tmp_path)


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
