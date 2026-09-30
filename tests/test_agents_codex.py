import json
import subprocess
import time
from pathlib import Path

import pytest

from workforce.agents.base import AgentRequest, is_infra
from workforce.agents.codex import CodexRunner, classify_error

FAKE = Path(__file__).parent / "fakes" / "fake_codex.py"

PLAN_SCHEMA = {
    "type": "object",
    "required": ["tasks", "risks"],
    "properties": {
        "tasks": {"type": "array", "items": {"type": "object", "required": ["id", "title"]}},
        "risks": {"type": "array"},
    },
}


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    path = tmp_path / "scenario.json"
    monkeypatch.setenv("WF_FAKE_SCENARIO", str(path))

    def write(steps, **extra):
        path.write_text(json.dumps({"codex": steps, **extra}))

    def calls():
        calls_file = Path(str(path) + ".calls.jsonl")
        return [json.loads(l) for l in calls_file.read_text().splitlines()] if calls_file.exists() else []

    write.calls = calls
    return write


def make_req(tmp_path, **kw):
    base = dict(
        role="planner",
        agent="codex",
        model="gpt-6-astra",
        effort="high",
        prompt="plan it",
        cwd=tmp_path,
    )
    base.update(kw)
    return AgentRequest(**base)


def runner():
    return CodexRunner(FAKE)


def test_fresh_run_argv_and_result(tmp_path, scenario):
    scenario([{"text": "planned"}])
    result = runner().run(make_req(tmp_path, sandbox="read-only"))
    assert result.ok and result.text == "planned"
    assert result.session_id == "fake-codex-thread-1"
    assert result.model == "gpt-6-astra"
    assert result.usage["output_tokens"] == 5
    assert result.rate_limits is None
    argv = scenario.calls()[0]["argv"]
    output_file = argv[argv.index("-o") + 1]
    assert argv == [
        "exec", "--json",
        "-m", "gpt-6-astra",
        "-c", "model_reasoning_effort=high",
        "-s", "read-only",
        "-C", str(tmp_path),
        "--disable", "browser_use", "--disable", "computer_use",
        "-o", output_file,
        "plan it",
    ]


def test_browser_flags(tmp_path, scenario):
    scenario([{}, {}, {}])
    runner().run(make_req(tmp_path, browser=True))
    runner().run(make_req(tmp_path, browser=True, computer_use=True))
    runner().run(make_req(tmp_path, browser=False, computer_use=True))
    browser_only, both, neither = [c["argv"] for c in scenario.calls()]
    assert "browser_use" not in browser_only and "computer_use" in browser_only
    assert "--disable" not in both
    assert "browser_use" in neither and "computer_use" in neither


def test_schema_run_reads_output_file(tmp_path, scenario):
    plan = {"tasks": [{"id": "T1", "title": "x"}], "risks": []}
    scenario([{"structured": plan, "text": "here is the plan"}])
    result = runner().run(make_req(tmp_path, schema=PLAN_SCHEMA))
    assert result.ok
    assert result.structured == plan
    assert result.text == "here is the plan"
    argv = scenario.calls()[0]["argv"]
    assert "--output-schema" in argv


def test_schema_file_content_matches_schema(tmp_path, scenario, monkeypatch):
    scenario([{"structured": {"tasks": [], "risks": []}}])
    captured = {}
    original = CodexRunner.build_command

    def spy(self, req, output_file, schema_file):
        cmd = original(self, req, output_file, schema_file)
        captured["schema"] = json.loads(Path(schema_file).read_text())
        return cmd

    monkeypatch.setattr(CodexRunner, "build_command", spy)
    assert runner().run(make_req(tmp_path, schema=PLAN_SCHEMA)).ok
    assert captured["schema"] == PLAN_SCHEMA


def test_resume_argv(tmp_path, scenario):
    scenario([{"text": "again"}])
    result = runner().run(
        make_req(tmp_path, resume_session="thread-42", sandbox="read-only", prompt="revise")
    )
    assert result.ok and result.session_id == "thread-42"
    argv = scenario.calls()[0]["argv"]
    output_file = argv[argv.index("-o") + 1]
    assert argv == [
        "exec", "resume", "thread-42",
        "--json",
        "-m", "gpt-6-astra",
        "-c", "model_reasoning_effort=high",
        "-c", 'sandbox_mode="read-only"',
        "--disable", "browser_use", "--disable", "computer_use",
        "-o", output_file,
        "revise",
    ]
    assert scenario.calls()[0]["cwd"] == str(tmp_path.resolve())


def test_resume_with_schema(tmp_path, scenario):
    plan = {"tasks": [], "risks": ["r"]}
    scenario([{"structured": plan}])
    result = runner().run(make_req(tmp_path, resume_session="t1", schema=PLAN_SCHEMA))
    assert result.ok and result.structured == plan
    assert "--output-schema" in scenario.calls()[0]["argv"]


def test_events_streamed_and_logged(tmp_path, scenario):
    scenario([{"text": "hi"}])
    events = []
    log = tmp_path / "runs" / "plan-1.jsonl"
    runner().run(make_req(tmp_path, log_path=log), on_event=events.append)
    assert [e["type"] for e in events] == [
        "thread.started", "turn.started", "item.completed", "turn.completed",
    ]
    assert [json.loads(l) for l in log.read_text().splitlines()] == events


def test_last_agent_message_wins(tmp_path):
    fake = tmp_path / "multi.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "def e(x): print(json.dumps(x), flush=True)\n"
        "e({'type':'thread.started','thread_id':'th'})\n"
        "e({'type':'item.completed','item':{'type':'agent_message','text':'first'}})\n"
        "e({'type':'item.completed','item':{'type':'command_execution','text':'ls'}})\n"
        "e({'type':'item.completed','item':{'type':'agent_message','text':'second'}})\n"
        "e({'type':'turn.completed','usage':{}})\n"
    )
    fake.chmod(0o755)
    result = CodexRunner(fake).run(make_req(tmp_path))
    assert result.ok and result.text == "second"


def test_nonfatal_error_event_before_completion_is_ok(tmp_path):
    fake = tmp_path / "warn.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "def e(x): print(json.dumps(x), flush=True)\n"
        "e({'type':'thread.started','thread_id':'th'})\n"
        "e({'type':'error','message':'Reconnecting... 1/5'})\n"
        "e({'type':'item.completed','item':{'type':'agent_message','text':'done'}})\n"
        "e({'type':'turn.completed','usage':{}})\n"
    )
    fake.chmod(0o755)
    assert CodexRunner(fake).run(make_req(tmp_path)).ok


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
    assert result.session_id == "fake-codex-thread-1"


def test_turn_failed_message_is_reported(tmp_path, scenario):
    scenario([{"error": "model_unavailable"}])
    result = runner().run(make_req(tmp_path))
    assert "not supported when using Codex with a ChatGPT account" in result.error


@pytest.mark.parametrize(
    "message,kind",
    [
        ("The 'x' model is not supported when using Codex with a ChatGPT account.", "model_unavailable"),
        ("Model metadata for `gpt-x` not found", "model_unavailable"),
        ("Not logged in, run codex login", "auth"),
        ("401 Unauthorized", "auth"),
        ("You've hit your usage limit", "rate_limited"),
        ("rate limit exceeded", "rate_limited"),
        ("something exploded", "crash"),
    ],
)
def test_classify_error(message, kind):
    assert classify_error(message) == kind


def test_exit_without_events_is_crash(tmp_path):
    fake = tmp_path / "dead.py"
    fake.write_text("#!/usr/bin/env python3\nimport sys\nprint('kaboom', file=sys.stderr)\nsys.exit(2)\n")
    fake.chmod(0o755)
    result = CodexRunner(fake).run(make_req(tmp_path))
    assert not result.ok and result.error_kind == "crash"
    assert "kaboom" in result.error


def test_error_event_without_turn_completion_is_classified(tmp_path):
    fake = tmp_path / "err.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'type':'error','message':'You have hit your usage limit'}), flush=True)\n"
        "sys.exit(1)\n"
    )
    fake.chmod(0o755)
    result = CodexRunner(fake).run(make_req(tmp_path))
    assert result.error_kind == "rate_limited"


def test_env_scrubbing(tmp_path, scenario, monkeypatch):
    scenario([{}])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    result = CodexRunner(FAKE, extra_env={"OPENAI_API_KEY": "again", "KEEP_ME": "1"}).run(
        make_req(tmp_path)
    )
    assert result.ok
    call = scenario.calls()[0]
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        assert name not in call["env_keys"]
    assert "KEEP_ME" in call["env_keys"]
    parts = call["path"].split(":")
    assert parts[:2] == [str(Path("~/.local/bin").expanduser()), "/opt/homebrew/bin"]


def test_stdin_is_devnull(tmp_path):
    fake = tmp_path / "stdin.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "data = sys.stdin.read()\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':repr(data)}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}), flush=True)\n"
    )
    fake.chmod(0o755)
    result = CodexRunner(fake).run(make_req(tmp_path, timeout_s=5))
    assert result.ok and result.text == "''"


def test_timeout(tmp_path, scenario):
    scenario([{"sleep": 30}])
    started = time.monotonic()
    result = runner().run(make_req(tmp_path, timeout_s=1))
    assert time.monotonic() - started < 10
    assert result.error_kind == "timeout" and is_infra(result.error_kind)


def test_missing_binary_is_crash(tmp_path):
    result = CodexRunner(tmp_path / "nope").run(make_req(tmp_path))
    assert not result.ok and result.error_kind == "crash"


def test_schema_failure_missing_key(tmp_path, scenario):
    scenario([{"structured": {"tasks": []}}])
    result = runner().run(make_req(tmp_path, schema=PLAN_SCHEMA))
    assert not result.ok and result.error_kind == "schema"
    assert "risks" in result.error


def test_schema_failure_invalid_json(tmp_path, scenario):
    scenario([{"structured": {"tasks": [], "risks": []}, "text": "prose", "raw_output": "not json {"}])
    result = runner().run(make_req(tmp_path, schema=PLAN_SCHEMA))
    assert not result.ok and result.error_kind == "schema"
    assert result.text == "prose"


def test_schema_failure_nested_item(tmp_path, scenario):
    scenario([{"structured": {"tasks": [{"id": "T1"}], "risks": []}}])
    result = runner().run(make_req(tmp_path, schema=PLAN_SCHEMA))
    assert result.error_kind == "schema"


def test_write_files_land_in_cd(tmp_path, scenario):
    work = tmp_path / "work"
    work.mkdir()
    scenario([{"write_files": {"a.txt": "x"}}])
    assert runner().run(make_req(work)).ok
    assert (work / "a.txt").read_text() == "x"


def test_match_assertion(tmp_path, scenario):
    scenario([{"match": "needle"}])
    result = runner().run(make_req(tmp_path, prompt="haystack"))
    assert not result.ok and result.error_kind == "crash"


def test_app_server_answers_rate_limits(tmp_path, scenario):
    limits = {
        "primary": {"usedPercent": 4, "windowDurationMins": 10080, "resetsAt": 1791265456},
        "secondary": None,
    }
    scenario([], codex_limits=limits)
    proc = subprocess.Popen(
        [str(FAKE), "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "workforce", "version": "0.1"}}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"},
    ]
    out, _ = proc.communicate("".join(json.dumps(r) + "\n" for r in requests), timeout=10)
    replies = [json.loads(l) for l in out.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["rateLimits"] == limits


def test_run_step_executes_in_cd_dir_after_write_files(tmp_path, scenario):
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


def test_run_step_nonzero_exit_is_crash(tmp_path, scenario):
    scenario([{"run": "echo boom >&2; exit 4", "text": "should not appear"}])
    result = runner().run(make_req(tmp_path))
    assert not result.ok and result.error_kind == "crash"
    assert "exit code 4" in result.error
    assert scenario.calls()[0]["run"]["returncode"] == 4


def test_structured_from_run(tmp_path, scenario):
    plan = {"tasks": [{"id": "T1", "title": "x"}], "risks": []}
    scenario([{"run": f"echo '{json.dumps(plan)}'", "structured_from_run": True}])
    result = runner().run(make_req(tmp_path, schema=PLAN_SCHEMA))
    assert result.ok and result.structured == plan


def test_structured_from_run_non_json_fails(tmp_path, scenario):
    scenario([{"run": "echo not-json", "structured_from_run": True}])
    result = runner().run(make_req(tmp_path, schema=PLAN_SCHEMA))
    assert not result.ok and result.error_kind == "crash"


def test_no_hook_flags_without_repo(tmp_path, scenario):
    scenario([{}])
    runner().run(make_req(tmp_path))
    argv = scenario.calls()[0]["argv"]
    assert "--dangerously-bypass-hook-trust" not in argv
    assert not any("hooks." in a for a in argv)


def test_repo_inserts_hook_args_after_exec(tmp_path, scenario):
    from workforce.decider import hooks

    repo = tmp_path / "repo"
    work = tmp_path / "work"
    repo.mkdir()
    work.mkdir()
    scenario([{}])
    assert runner().run(make_req(work, repo=repo, sandbox="read-only")).ok
    argv = scenario.calls()[0]["argv"]
    hook_args = hooks.codex_hook_args(repo, worktree=work)
    output_file = argv[argv.index("-o") + 1]
    assert argv == [
        "exec",
        *hook_args,
        "--json",
        "-m", "gpt-6-astra",
        "-c", "model_reasoning_effort=high",
        "-s", "read-only",
        "-C", str(work),
        "--disable", "browser_use", "--disable", "computer_use",
        "-o", output_file,
        "plan it",
    ]
    assert hook_args[0] == "--dangerously-bypass-hook-trust"


def test_repo_inserts_hook_args_after_resume_id(tmp_path, scenario):
    from workforce.decider import hooks

    repo = tmp_path / "repo"
    work = tmp_path / "work"
    repo.mkdir()
    work.mkdir()
    scenario([{}])
    assert runner().run(make_req(work, repo=repo, resume_session="thread-7", sandbox="read-only")).ok
    argv = scenario.calls()[0]["argv"]
    hook_args = hooks.codex_hook_args(repo, worktree=work)
    assert argv[:3] == ["exec", "resume", "thread-7"]
    assert argv[3 : 3 + len(hook_args)] == hook_args
    assert argv[3 + len(hook_args)] == "--json"


def test_endpoint_overrides_and_codex_api_key_are_scrubbed(tmp_path, scenario, monkeypatch):
    scenario([{}])
    names = ["CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "ANTHROPIC_BASE_URL"]
    for name in names:
        monkeypatch.setenv(name, "x")
    assert runner().run(make_req(tmp_path)).ok
    env_keys = scenario.calls()[0]["env_keys"]
    for name in names:
        assert name not in env_keys


def test_structured_output_with_an_extra_key_is_a_schema_failure(tmp_path, scenario):
    from workforce import schemas

    plan = {"tasks": [], "risks": [], "open_questions": [], "surprise": 1}
    scenario([{"structured": plan}])
    result = runner().run(make_req(tmp_path, sandbox="read-only", schema=schemas.PLAN))
    assert not result.ok and result.error_kind == "schema"
    assert "surprise" in result.error


def build(tmp_path, **kw):
    return runner().build_command(make_req(tmp_path, **kw), tmp_path / "out.txt", None)


def test_skip_git_check_adds_the_flag_before_the_prompt(tmp_path):
    assert "--skip-git-repo-check" not in build(tmp_path)
    argv = build(tmp_path, skip_git_check=True)
    assert argv.count("--skip-git-repo-check") == 1
    assert argv.index("--skip-git-repo-check") < argv.index("-o")
    assert argv[-1] == "plan it"


def test_skip_git_check_also_applies_to_resume(tmp_path):
    argv = build(tmp_path, skip_git_check=True, resume_session="thread-1")
    assert argv[:4] == [str(FAKE), "exec", "resume", "thread-1"]
    assert argv.count("--skip-git-repo-check") == 1


def test_add_dirs_are_ignored_because_add_dir_would_make_them_writable(tmp_path):
    extra = tmp_path / "other"
    assert build(tmp_path, add_dirs=[extra]) == build(tmp_path)
    assert not any("--add-dir" in arg or str(extra) in arg for arg in build(tmp_path, add_dirs=[extra]))


def test_a_run_with_skip_git_check_reaches_the_cli(tmp_path, scenario):
    scenario([{"text": "planned"}])
    assert runner().run(make_req(tmp_path, skip_git_check=True, sandbox="read-only")).ok
    argv = scenario.calls()[0]["argv"]
    assert "--skip-git-repo-check" in argv and argv[argv.index("-s") + 1] == "read-only"
