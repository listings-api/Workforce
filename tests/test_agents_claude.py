import json
import time
from pathlib import Path

import pytest

from workforce.agents.base import AgentRequest, is_infra
from workforce.agents.claude import ClaudeRunner, classify_error

FAKE = Path(__file__).parent / "fakes" / "fake_claude.py"

VERDICT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "BLOCKED"]},
        "findings": {"type": "array", "items": {"type": "object", "required": ["message"]}},
    },
}


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    path = tmp_path / "scenario.json"
    monkeypatch.setenv("WF_FAKE_SCENARIO", str(path))

    def write(steps, **extra):
        path.write_text(json.dumps({"claude": steps, **extra}))

    def calls():
        calls_file = Path(str(path) + ".calls.jsonl")
        return [json.loads(l) for l in calls_file.read_text().splitlines()] if calls_file.exists() else []

    write.calls = calls
    return write


def make_req(tmp_path, **kw):
    base = dict(
        role="coder",
        agent="claude",
        model="claude-sonnet-5-5",
        effort="high",
        prompt="do the thing",
        cwd=tmp_path,
    )
    base.update(kw)
    return AgentRequest(**base)


def runner():
    return ClaudeRunner(FAKE)


def test_fresh_run_argv_and_result(tmp_path, scenario):
    scenario([{"text": "hello"}])
    result = runner().run(make_req(tmp_path))
    assert result.ok and result.text == "hello"
    assert result.model == "claude-sonnet-5-5"
    assert result.session_id == "fake-claude-session-1"
    assert result.usage["input_tokens"] == 10
    assert "claude-sonnet-5-5" in result.usage["modelUsage"]
    argv = scenario.calls()[0]["argv"]
    assert argv == [
        "-p",
        "--model", "claude-sonnet-5-5",
        "--effort", "high",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "auto",
        "--setting-sources", "project",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
    ]


def test_resume_schema_browser_flags(tmp_path, scenario):
    scenario([{"structured": {"verdict": "APPROVE", "findings": []}}])
    result = runner().run(
        make_req(tmp_path, schema=VERDICT_SCHEMA, resume_session="sess-9", browser=True)
    )
    assert result.ok
    assert result.structured == {"verdict": "APPROVE", "findings": []}
    assert result.session_id == "sess-9"
    argv = scenario.calls()[0]["argv"]
    assert argv[argv.index("--json-schema") + 1] == json.dumps(VERDICT_SCHEMA)
    assert argv[argv.index("--resume") + 1] == "sess-9"
    assert "--chrome" in argv


def test_no_optional_flags_by_default(tmp_path, scenario):
    scenario([{}])
    runner().run(make_req(tmp_path))
    argv = scenario.calls()[0]["argv"]
    for flag in ("--json-schema", "--resume", "--chrome"):
        assert flag not in argv


def test_user_setup_omits_isolation_flags(tmp_path, scenario):
    scenario([{}])
    result = runner().run(make_req(tmp_path, user_setup=True))
    assert result.ok
    argv = scenario.calls()[0]["argv"]
    for flag in ("--setting-sources", "--strict-mcp-config", "--mcp-config"):
        assert flag not in argv
    assert "--permission-mode" in argv


def test_events_streamed_and_logged(tmp_path, scenario):
    scenario([{"text": "hi", "rate_limits": {"five_hour": 0.12, "seven_day": 0.3}}])
    events = []
    log = tmp_path / "logs" / "coder-1.jsonl"
    result = runner().run(make_req(tmp_path, log_path=log), on_event=events.append)
    assert [e["type"] for e in events] == ["system", "rate_limit_event", "assistant", "result"]
    logged = [json.loads(l) for l in log.read_text().splitlines()]
    assert logged == events
    assert result.rate_limits == {
        "five_hour": {"utilization": 0.12, "resetsAt": 1790676000},
        "seven_day": {"utilization": 0.3, "resetsAt": 1790838000},
    }


def test_no_rate_limit_event_gives_none(tmp_path, scenario):
    scenario([{}])
    assert runner().run(make_req(tmp_path)).rate_limits is None


def test_non_json_line_logged_as_raw(tmp_path, scenario):
    fake = tmp_path / "noisy.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print('warming up')\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'s','apiKeySource':'none'}))\n"
        "print(json.dumps({'type':'result','is_error':False,'result':'ok','session_id':'s'}))\n"
    )
    fake.chmod(0o755)
    log = tmp_path / "raw.jsonl"
    events = []
    result = ClaudeRunner(fake).run(make_req(tmp_path, log_path=log), on_event=events.append)
    assert result.ok and result.text == "ok"
    assert events[0] == {"type": "raw", "line": "warming up"}
    assert json.loads(log.read_text().splitlines()[0]) == {"type": "raw", "line": "warming up"}


@pytest.mark.parametrize(
    "error,kind",
    [
        ("model_unavailable", "model_unavailable"),
        ("auth", "auth"),
        ("rate_limited", "rate_limited"),
        ("crash", "crash"),
    ],
)
def test_error_kinds(tmp_path, scenario, error, kind):
    scenario([{"error": error}])
    result = runner().run(make_req(tmp_path))
    assert not result.ok
    assert result.error_kind == kind
    assert result.error
    assert result.session_id


def test_crash_reports_stderr_and_exit_code(tmp_path, scenario):
    scenario([{"error": "crash"}])
    result = runner().run(make_req(tmp_path))
    assert "simulated crash" in result.error
    assert "code 1" in result.error


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Unrecognized_model: foo", "model_unavailable"),
        ("This model does not support this model", "model_unavailable"),
        ("It may not exist or you may not have access to it", "model_unavailable"),
        ("API Error: 400 invalid model name", "model_unavailable"),
        ("API Error: 400 something else entirely", "crash"),
        ("API Error: 403 forbidden", "auth"),
        ("Please run /login", "auth"),
        ("API Error: 429", "rate_limited"),
        ("Rate limit hit", "rate_limited"),
        ("boom", "crash"),
    ],
)
def test_classify_error(text, kind):
    assert classify_error(text) == kind


def test_result_text_starting_with_api_error_is_failure(tmp_path, scenario):
    noisy = tmp_path / "api_error.py"
    noisy.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type':'result','is_error':False,'result':'API Error: 400 bad','session_id':'s'}))\n"
    )
    noisy.chmod(0o755)
    result = ClaudeRunner(noisy).run(make_req(tmp_path))
    assert not result.ok
    assert result.error_kind == "crash"


def test_api_key_source_aborts_run(tmp_path, scenario):
    scenario([{"api_key_source": "ANTHROPIC_API_KEY", "sleep": 10, "text": "should not appear"}])
    events = []
    started = time.monotonic()
    result = runner().run(make_req(tmp_path), on_event=events.append)
    assert time.monotonic() - started < 5
    assert not result.ok
    assert result.error_kind == "auth"
    assert "ANTHROPIC_API_KEY" in result.error
    assert result.text == ""
    assert [e["type"] for e in events] == ["system"]


def test_env_scrubbing(tmp_path, scenario, monkeypatch):
    scenario([{}])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    result = ClaudeRunner(FAKE, extra_env={"ANTHROPIC_API_KEY": "again", "KEEP_ME": "1"}).run(
        make_req(tmp_path)
    )
    assert result.ok
    call = scenario.calls()[0]
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        assert name not in call["env_keys"]
    assert "KEEP_ME" in call["env_keys"]
    parts = call["path"].split(":")
    assert parts[0] == str(Path("~/.local/bin").expanduser())
    assert parts[1] == "/opt/homebrew/bin"


def test_timeout(tmp_path, scenario):
    scenario([{"sleep": 30}])
    started = time.monotonic()
    result = runner().run(make_req(tmp_path, timeout_s=1))
    assert time.monotonic() - started < 10
    assert not result.ok
    assert result.error_kind == "timeout"
    assert is_infra(result.error_kind)


def test_missing_binary_is_crash(tmp_path):
    result = ClaudeRunner(tmp_path / "nope").run(make_req(tmp_path))
    assert not result.ok and result.error_kind == "crash"


def test_schema_failure_missing_key(tmp_path, scenario):
    scenario([{"structured": {"findings": []}}])
    result = runner().run(make_req(tmp_path, schema=VERDICT_SCHEMA))
    assert not result.ok
    assert result.error_kind == "schema"
    assert "verdict" in result.error


def test_schema_failure_bad_enum(tmp_path, scenario):
    scenario([{"structured": {"verdict": "MAYBE", "findings": []}}])
    result = runner().run(make_req(tmp_path, schema=VERDICT_SCHEMA))
    assert not result.ok and result.error_kind == "schema"
    assert not is_infra(result.error_kind)


def test_schema_failure_nested_item(tmp_path, scenario):
    scenario([{"structured": {"verdict": "APPROVE", "findings": [{"file": "a"}]}}])
    result = runner().run(make_req(tmp_path, schema=VERDICT_SCHEMA))
    assert result.error_kind == "schema"


def test_schema_run_without_structured_output_fails(tmp_path, scenario):
    scenario([{"text": "prose only"}])
    result = runner().run(make_req(tmp_path, schema=VERDICT_SCHEMA))
    assert not result.ok and result.error_kind == "schema"
    assert result.text == "prose only"


def test_prompt_match_assertion(tmp_path, scenario):
    scenario([{"match": "needle"}])
    result = runner().run(make_req(tmp_path, prompt="haystack"))
    assert not result.ok and result.error_kind == "crash"
    scenario([{"match": "needle"}])
    (tmp_path / "scenario.json.state").unlink()
    assert runner().run(make_req(tmp_path, prompt="a needle here")).ok


def test_write_files_land_in_cwd(tmp_path, scenario):
    work = tmp_path / "work"
    work.mkdir()
    scenario([{"write_files": {"src/a.py": "print(1)\n"}}])
    assert runner().run(make_req(work)).ok
    assert (work / "src" / "a.py").read_text() == "print(1)\n"


def test_sequential_steps_share_state_across_processes(tmp_path, scenario):
    scenario([{"text": "one"}, {"text": "two"}])
    assert runner().run(make_req(tmp_path)).text == "one"
    assert runner().run(make_req(tmp_path)).text == "two"
    result = runner().run(make_req(tmp_path))
    assert not result.ok and result.error_kind == "crash"


def test_is_infra():
    assert all(is_infra(k) for k in ("auth", "rate_limited", "timeout", "crash"))
    assert not any(is_infra(k) for k in ("schema", "model_unavailable", None))


def test_run_step_executes_after_write_files_in_cwd(tmp_path, scenario):
    work = tmp_path / "work"
    work.mkdir()
    scenario([{"write_files": {"a.txt": "hello"}, "run": "cat a.txt > copy.txt; echo out; echo err >&2"}])
    result = runner().run(make_req(work))
    assert result.ok
    assert (work / "copy.txt").read_text() == "hello"
    call = scenario.calls()[0]
    assert call["run"]["returncode"] == 0
    assert call["run"]["stdout"] == "out\n"
    assert call["run"]["stderr"] == "err\n"
    assert call["run"]["cmd"].startswith("cat a.txt")


def test_run_step_nonzero_exit_is_crash_error(tmp_path, scenario):
    scenario([{"run": "echo boom >&2; exit 3", "text": "should not appear", "structured": {"verdict": "APPROVE", "findings": []}}])
    result = runner().run(make_req(tmp_path, schema=VERDICT_SCHEMA))
    assert not result.ok
    assert result.error_kind == "crash"
    assert "exit code 3" in result.error
    call = scenario.calls()[0]
    assert call["run"]["returncode"] == 3
    assert call["run"]["stderr"] == "boom\n"


def test_structured_from_run(tmp_path, scenario):
    scenario([{"run": 'echo \'{"verdict": "APPROVE", "findings": []}\'', "structured_from_run": True}])
    result = runner().run(make_req(tmp_path, schema=VERDICT_SCHEMA))
    assert result.ok
    assert result.structured == {"verdict": "APPROVE", "findings": []}


def test_structured_from_run_non_json_fails(tmp_path, scenario):
    scenario([{"run": "echo not-json", "structured_from_run": True}])
    result = runner().run(make_req(tmp_path))
    assert not result.ok and result.error_kind == "crash"


def test_committer_style_run_reports_real_sha(tmp_path, scenario):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    schema = {"type": "object", "required": ["committed", "sha"]}
    script = (
        "git add -A && git commit -q -m 'task' && "
        "printf '{\"committed\": true, \"sha\": \"%s\"}' \"$(git rev-parse HEAD)\""
    )
    scenario([{"write_files": {"f.txt": "x"}, "run": script, "structured_from_run": True}])
    result = runner().run(make_req(repo, schema=schema, user_setup=True))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert result.ok
    assert result.structured == {"committed": True, "sha": head}
    assert len(scenario.calls()) == 1


def test_no_hook_flags_without_repo(tmp_path, scenario):
    scenario([{}])
    runner().run(make_req(tmp_path))
    assert "--settings" not in scenario.calls()[0]["argv"]


def _hook_command(settings_json):
    settings = json.loads(settings_json)
    return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_repo_adds_settings_hook_flag(tmp_path, scenario):
    from workforce.decider import hooks

    repo = tmp_path / "repo"
    work = tmp_path / "work"
    repo.mkdir()
    work.mkdir()
    scenario([{}])
    assert runner().run(make_req(work, repo=repo)).ok
    argv = scenario.calls()[0]["argv"]
    expected = hooks.claude_settings_json(repo, worktree=work, denylist_only=False)
    assert argv[-2:] == ["--settings", expected]
    assert argv[:-2] == [
        "-p",
        "--model", "claude-sonnet-5-5",
        "--effort", "high",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "auto",
        "--setting-sources", "project",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
    ]
    command = _hook_command(argv[-1])
    assert "WF_MODE" not in command and "WF_HOOK_MODE=denylist" not in command
    assert f"WF_WORKTREE={work.resolve()}" in command


def test_commit_run_gets_denylist_only_hook(tmp_path, scenario):
    from workforce.decider import hooks

    repo = tmp_path / "repo"
    work = tmp_path / "work"
    repo.mkdir()
    work.mkdir()
    scenario([{}])
    assert runner().run(make_req(work, repo=repo, user_setup=True)).ok
    argv = scenario.calls()[0]["argv"]
    assert argv[-2:] == ["--settings", hooks.claude_settings_json(repo, worktree=work, denylist_only=True)]
    assert "WF_HOOK_MODE=denylist" in _hook_command(argv[-1])
    for flag in ("--setting-sources", "--strict-mcp-config", "--mcp-config"):
        assert flag not in argv


def test_hook_flag_follows_schema_resume_chrome(tmp_path, scenario):
    repo = tmp_path / "repo"
    repo.mkdir()
    scenario([{"structured": {"verdict": "APPROVE", "findings": []}}])
    runner().run(
        make_req(tmp_path, repo=repo, schema=VERDICT_SCHEMA, resume_session="s1", browser=True)
    )
    argv = scenario.calls()[0]["argv"]
    positions = [argv.index(f) for f in ("--json-schema", "--resume", "--chrome", "--settings")]
    assert positions == sorted(positions)


def _permission_mode(argv):
    return argv[argv.index("--permission-mode") + 1]


def test_read_only_request_runs_in_plan_mode(tmp_path, scenario):
    scenario([{}, {}, {}])
    assert runner().run(make_req(tmp_path, sandbox="read-only")).ok
    assert runner().run(make_req(tmp_path)).ok
    assert runner().run(make_req(tmp_path, sandbox="workspace-write", user_setup=True)).ok
    modes = [_permission_mode(call["argv"]) for call in scenario.calls()]
    assert modes == ["plan", "auto", "auto"]


def test_read_only_build_command_argv(tmp_path):
    argv = runner().build_command(make_req(tmp_path, sandbox="read-only"))
    assert argv[argv.index("--permission-mode") :][:2] == ["--permission-mode", "plan"]


def test_read_only_still_returns_structured_output(tmp_path, scenario):
    scenario([{"structured": {"verdict": "APPROVE", "findings": []}}])
    result = runner().run(make_req(tmp_path, sandbox="read-only", schema=VERDICT_SCHEMA))
    assert result.ok and result.structured["verdict"] == "APPROVE"


def test_build_command_can_leave_the_prompt_out_for_stdin(tmp_path):
    req = make_req(tmp_path, prompt="SECRET-PROMPT")
    on_argv = runner().build_command(req)
    on_stdin = runner().build_command(req, prompt_on_stdin=True)
    assert on_argv[:2] == [str(FAKE), "-p"] and "SECRET-PROMPT" in on_argv
    assert "SECRET-PROMPT" not in on_stdin
    assert on_stdin[1] == "-p" and on_stdin[2] == "--model"
    assert [a for a in on_argv if a != "SECRET-PROMPT"] == on_stdin


def test_prompt_reaches_the_cli(tmp_path, scenario):
    scenario([{"match": "SECRET-PROMPT"}])
    assert runner().run(make_req(tmp_path, prompt="SECRET-PROMPT")).ok


def test_prompt_goes_over_stdin_when_the_base_supports_it(tmp_path, scenario):
    import inspect

    from workforce.agents.base import stream_process

    if "stdin_text" not in inspect.signature(stream_process).parameters:
        pytest.skip("agents/base.stream_process has no stdin_text yet")
    scenario([{"match": "SECRET-PROMPT"}])
    assert runner().run(make_req(tmp_path, prompt="SECRET-PROMPT")).ok
    assert "SECRET-PROMPT" not in scenario.calls()[0]["argv"]


def test_bad_api_key_source_is_a_named_subscription_violation(tmp_path, scenario):
    from workforce.agents import guard

    scenario([{"api_key_source": "ANTHROPIC_API_KEY"}])
    result = runner().run(make_req(tmp_path))
    assert result.error_kind == "auth"
    assert result.error.startswith(guard.SUBSCRIPTION_VIOLATION)
    assert guard.is_subscription_violation(result)


def test_ordinary_auth_error_is_not_a_subscription_violation():
    from workforce.agents import guard
    from workforce.agents.base import failure

    assert not guard.is_subscription_violation(failure("auth", "401 unauthorized"))
    assert not guard.is_subscription_violation(failure("crash", "subscription violation: x"))


def test_result_without_init_event_is_a_crash(tmp_path):
    fake = tmp_path / "noinit.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type':'result','is_error':False,'result':'ok','session_id':'s'}))\n"
    )
    fake.chmod(0o755)
    result = ClaudeRunner(fake).run(make_req(tmp_path))
    assert not result.ok
    assert result.error_kind == "crash"
    assert "system/init" in result.error


def test_endpoint_and_provider_overrides_are_scrubbed(tmp_path, scenario, monkeypatch):
    scenario([{}])
    names = [
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "CODEX_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ]
    for name in names:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-login")
    assert runner().run(make_req(tmp_path)).ok
    env_keys = scenario.calls()[0]["env_keys"]
    for name in names:
        assert name not in env_keys
    assert "CLAUDE_CODE_OAUTH_TOKEN" in env_keys


def test_structured_output_with_an_extra_key_is_a_schema_failure(tmp_path, scenario):
    from workforce import schemas

    finding = {"severity": "minor", "file": "a.py", "line": 1, "message": "m", "extra": "x"}
    scenario([{"structured": {"verdict": "REQUEST_CHANGES", "summary": "s", "findings": [finding]}}])
    result = runner().run(make_req(tmp_path, schema=schemas.VERDICT))
    assert not result.ok and result.error_kind == "schema"
    assert "extra" in result.error


def test_log_handle_closed_when_on_event_raises(tmp_path, scenario, monkeypatch):
    from workforce.agents import claude as claude_mod

    closed = []

    class SpyLog(claude_mod.EventLog):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(claude_mod, "EventLog", SpyLog)
    scenario([{}])

    def boom(event):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        runner().run(make_req(tmp_path, log_path=tmp_path / "raw.jsonl"), on_event=boom)
    assert closed == [True]


def test_add_dirs_default_to_none_and_skip_git_check_to_false(tmp_path):
    req = make_req(tmp_path)
    assert req.add_dirs == [] and req.skip_git_check is False
    assert make_req(tmp_path).add_dirs is not req.add_dirs
    assert "--add-dir" not in runner().build_command(req)


def test_build_command_adds_one_add_dir_per_entry_after_the_other_flags(tmp_path):
    first, second = tmp_path / "dep-a" / "_integration", tmp_path / "dep-b" / "_integration"
    req = make_req(tmp_path, add_dirs=[first, second], schema={"type": "object"}, resume_session="s1", browser=True, repo=tmp_path)
    argv = runner().build_command(req)
    positions = [i for i, arg in enumerate(argv) if arg == "--add-dir"]
    assert len(positions) == 2
    assert [argv[i + 1] for i in positions] == [str(first), str(second)]
    for flag in ("--json-schema", "--resume", "--chrome", "--settings", "--permission-mode", "--model"):
        assert argv.index(flag) < positions[0]
    assert argv[-4:] == ["--add-dir", str(first), "--add-dir", str(second)]


def test_add_dirs_argv_without_the_prompt_on_stdin(tmp_path):
    extra = tmp_path / "other"
    argv = runner().build_command(make_req(tmp_path, add_dirs=[extra]), prompt_on_stdin=True)
    assert argv[-2:] == ["--add-dir", str(extra)]
    argv = runner().build_command(make_req(tmp_path, add_dirs=[extra], user_setup=True))
    assert "--setting-sources" not in argv and argv[-2:] == ["--add-dir", str(extra)]


def test_a_run_with_add_dirs_reaches_the_cli(tmp_path, scenario):
    scenario([{"text": "ok"}])
    extra = tmp_path / "other"
    assert runner().run(make_req(tmp_path, add_dirs=[extra])).ok
    argv = scenario.calls()[0]["argv"]
    assert argv[argv.index("--add-dir") + 1] == str(extra)
