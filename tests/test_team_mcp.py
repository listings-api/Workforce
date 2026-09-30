import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.test_decider import FakeOllaya
from workforce.team import approvals, config, usage_cache
from workforce.usage.claude_limits import Window

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fakes" / "fake_codex.py"
FAKE_CLAUDE = ROOT / "tests" / "fakes" / "fake_claude.py"
PROTOCOL = "2025-03-26"


class Client:
    def __init__(self, proc):
        self.proc = proc
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put("")

    def send_raw(self, text: str):
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        self.send_raw(json.dumps({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}))

    def recv(self, timeout=60):
        line = self.lines.get(timeout=timeout)
        assert line, "server closed its stdout"
        return json.loads(line)

    def request(self, method, params=None):
        self.next_id += 1
        self.send_raw(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}))
        message = self.recv()
        assert message["id"] == self.next_id
        return message

    def call(self, name, arguments=None):
        message = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return message["result"]

    def text(self, name, arguments=None):
        result = self.call(name, arguments)
        assert not result.get("isError"), result
        return result["content"][0]["text"]

    def error_text(self, name, arguments=None):
        result = self.call(name, arguments)
        assert result.get("isError") is True, result
        return result["content"][0]["text"]


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.home = tmp_path / "home"
        self.home.mkdir()
        monkeypatch.setenv("HOME", str(self.home))  # approvals are signed with the key under $HOME/.workforce
        self.repo = tmp_path / "proj"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-b", "main"], check=True, capture_output=True)
        for key, value in (("user.name", "T"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value], check=True, capture_output=True)
        (self.repo / "app.py").write_text("print('hi')\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "initial"], check=True, capture_output=True)
        self.scenario_file = tmp_path / "scenario.json"
        (self.home / ".workforce").mkdir()
        (self.home / ".workforce" / "team.toml").write_text(f'codex = "{FAKE}"\nclaude = "{FAKE_CLAUDE}"\n')
        self.extra_env: dict = {}
        self.clients: list[subprocess.Popen] = []
        self.monkeypatch = monkeypatch

    def scenario(self, steps=(), claude=(), **extra):
        self.scenario_file.write_text(json.dumps({"codex": list(steps), "claude": list(claude), **extra}))

    def calls(self, cli="codex"):
        path = Path(str(self.scenario_file) + ".calls.jsonl")
        entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [entry for entry in entries if entry.get("cli", "codex") == cli]

    def start(self, cwd=None, project_dir="default") -> Client:
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_PROJECT_DIR")}
        env.update(
            HOME=str(self.home),
            WF_FAKE_SCENARIO=str(self.scenario_file),
            PYTHONPATH=str(ROOT),
            PYTHONDONTWRITEBYTECODE="1",
        )
        if project_dir == "default":
            env["CLAUDE_PROJECT_DIR"] = str(self.repo)
        elif project_dir is not None:
            env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        env.update(self.extra_env)
        proc = subprocess.Popen(
            [sys.executable, "-m", "workforce.team.mcp_server"],
            cwd=str(cwd or self.repo),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.clients.append(proc)
        client = Client(proc)
        reply = client.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        assert reply["result"]["protocolVersion"] == PROTOCOL
        client.notify("notifications/initialized")
        return client

    def stop(self):
        for proc in self.clients:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc.stdout.close()
            proc.stderr.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    e.scenario()
    yield e
    e.stop()


def verdict(v="APPROVE", summary="looks right", findings=()):
    return {"structured": {"verdict": v, "summary": summary, "findings": list(findings)}}


def finding(severity="minor", message="tidy"):
    return {"severity": severity, "file": "app.py", "line": 1, "message": message}


def test_handshake_ping_and_tools_list(env):
    client = env.start()
    listed = client.request("tools/list")["result"]["tools"]
    assert [t["name"] for t in listed] == [
        "codex_plan",
        "codex_ask",
        "codex_review",
        "claude_review",
        "codex_plan_review",
        "plan_status",
        "review_status",
        "usage",
        "laya",
        "codex_settings",
        "team_models",
        "picker",
        "settings",
    ]
    for tool in listed:
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and isinstance(schema["properties"], dict)
        assert set(schema["required"]) <= set(schema["properties"])
    assert client.request("tools/list")["result"]["tools"][0]["inputSchema"]["required"] == ["task"]
    assert client.request("ping")["result"] == {}


def test_initialize_reports_capabilities_and_server_info(env):
    client = env.start()
    reply = client.request("initialize", {"protocolVersion": "2024-11-05"})
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["capabilities"] == {"tools": {}}
    assert reply["result"]["serverInfo"]["name"]


def test_protocol_errors_and_notifications(env):
    client = env.start()
    assert client.request("no/such/method")["error"]["code"] == -32601
    assert client.request("tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602
    client.send_raw("{not json")
    assert client.recv()["error"]["code"] == -32700
    client.send_raw(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": "review_status", "arguments": []}}))
    assert client.recv()["result"]["isError"] is True
    client.notify("notifications/cancelled", {"requestId": 5})
    client.notify("some/unknown/notification")
    assert client.request("ping")["result"] == {}


def test_codex_plan_runs_read_only_with_configured_model(env):
    env.scenario([{"text": "1. do the thing\nRisks: none", "session_id": "thread-plan"}])
    client = env.start()
    text = client.text("codex_plan", {"task": "add a cache", "context": "uses redis"})
    assert "1. do the thing" in text and "[codex session_id: thread-plan]" in text
    call = env.calls()[0]
    argv = call["argv"]
    assert argv[0] == "exec"
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == str(env.repo)
    assert argv[argv.index("-m") + 1] == "gpt-6-sol"
    assert "model_reasoning_effort=high" in argv
    assert "--skip-git-repo-check" not in argv
    assert "add a cache" in call["prompt"] and "uses redis" in call["prompt"]
    assert call["cwd"] == str(env.repo)
    assert "OPENAI_API_KEY" not in call["env_keys"]


def test_codex_plan_overrides_model_and_effort_and_rejects_bad_ones(env):
    env.scenario([{"text": "plan"}])
    client = env.start()
    client.text("codex_plan", {"task": "t", "model": "gpt-6-astra", "effort": "max"})
    argv = env.calls()[0]["argv"]
    assert argv[argv.index("-m") + 1] == "gpt-6-astra" and "model_reasoning_effort=max" in argv
    assert "effort 'turbo'" in client.error_text("codex_plan", {"task": "t", "effort": "turbo"})
    assert "'task' is required" in client.error_text("codex_plan", {})
    assert "must be a string" in client.error_text("codex_plan", {"task": 5})
    assert len(env.calls()) == 1


def test_codex_plan_skips_git_check_outside_a_repo(env, tmp_path):
    bare = tmp_path / "plain"
    bare.mkdir()
    env.scenario([{"text": "plan"}])
    client = env.start(cwd=bare, project_dir=None)
    client.text("codex_plan", {"task": "t"})
    argv = env.calls()[0]["argv"]
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("-C") + 1] == str(bare)


def test_cwd_falls_back_to_process_cwd_without_project_dir(env):
    env.scenario([{"text": "plan"}])
    client = env.start(cwd=env.repo, project_dir=None)
    client.text("codex_plan", {"task": "t"})
    assert env.calls()[0]["cwd"] == str(env.repo)


def test_codex_ask_fresh_then_resume_keeps_the_session(env):
    env.scenario([{"text": "first answer", "session_id": "thread-x"}, {"text": "second answer"}])
    client = env.start()
    first = client.text("codex_ask", {"message": "should we use a queue?"})
    assert "first answer" in first and "thread-x" in first
    second = client.text("codex_ask", {"message": "and retries?", "session_id": "thread-x"})
    assert "second answer" in second and "[codex session_id: thread-x]" in second
    fresh, resumed = env.calls()
    assert "resume" not in fresh["argv"]
    assert resumed["argv"][:3] == ["exec", "resume", "thread-x"]
    assert 'sandbox_mode="read-only"' in resumed["argv"]
    assert "and retries?" in resumed["prompt"]


def test_codex_errors_come_back_as_error_results(env):
    env.scenario([{"error": "crash"}, {"error": "rate_limited"}, {"error": "model_unavailable"}])
    client = env.start()
    assert "crash" in client.error_text("codex_plan", {"task": "t"})
    assert "rate_limited" in client.error_text("codex_ask", {"message": "m"})
    assert "model_unavailable" in client.error_text("codex_plan", {"task": "t"})
    assert client.request("ping")["result"] == {}


def test_missing_codex_binary_is_an_error_result(env):
    (env.home / ".workforce" / "team.toml").write_text('codex = "/nonexistent/codex"\n')
    client = env.start()
    assert "cannot start codex" in client.error_text("codex_plan", {"task": "t"})


def test_codex_review_sends_the_diff_and_records_the_verdict(env):
    (env.repo / "app.py").write_text("print('changed')\n")
    (env.repo / "new_file.py").write_text("x = 1\n")
    env.scenario([verdict("APPROVE", "solid", [finding("nit")])])
    client = env.start()
    body = json.loads(client.text("codex_review", {"focus": "check the retry logic"}))
    assert body["verdict"] == "APPROVE" and body["summary"] == "solid"
    assert body["tree"] == approvals.current_tree(env.repo)
    assert body["commit_allowed"] is False and body["missing"] == ["claude: no review of the current changes"]

    call = env.calls()[0]
    argv = call["argv"]
    assert argv[argv.index("-s") + 1] == "read-only" and "--output-schema" in argv
    for needle in ("check the retry logic", "print('changed')", "new_file.py", "x = 1", body["tree"], "app.py"):
        assert needle in call["prompt"], needle

    status = approvals.status(env.repo)
    assert status["codex"]["verdict"] == "APPROVE" and status["codex"]["findings"][0]["severity"] == "nit"
    assert status["claude"] is None


def test_codex_review_uses_the_base_argument(env):
    (env.repo / "app.py").write_text("print('one')\n")
    subprocess.run(["git", "-C", str(env.repo), "commit", "-am", "second"], check=True, capture_output=True)
    (env.repo / "app.py").write_text("print('two')\n")
    env.scenario([verdict()])
    client = env.start()
    client.text("codex_review", {"base": "HEAD~1"})
    prompt = env.calls()[0]["prompt"]
    assert "print('two')" in prompt and "print('hi')" in prompt and "HEAD~1" in prompt


def test_codex_review_trims_a_huge_diff(env):
    (env.repo / "big.txt").write_text("".join(f"line {i} of a very long file\n" for i in range(6000)))
    env.scenario([verdict()])
    client = env.start()
    client.text("codex_review")
    prompt = env.calls()[0]["prompt"]
    assert "more characters of diff omitted" in prompt
    assert len(prompt) < 75_000
    assert "big.txt" in prompt


def test_codex_review_approve_with_a_blocker_is_recorded_as_request_changes(env):
    (env.repo / "app.py").write_text("boom\n")
    env.scenario([verdict("APPROVE", "fine", [finding("blocker", "crashes")])])
    client = env.start()
    body = json.loads(client.text("codex_review"))
    assert body["verdict"] == "REQUEST_CHANGES"
    assert approvals.status(env.repo)["codex"]["verdict"] == "REQUEST_CHANGES"


def test_codex_review_request_changes_is_recorded(env):
    (env.repo / "app.py").write_text("boom\n")
    env.scenario([verdict("REQUEST_CHANGES", "no tests", [finding("major", "add a test")])])
    client = env.start()
    body = json.loads(client.text("codex_review"))
    assert body["verdict"] == "REQUEST_CHANGES" and body["findings"][0]["message"] == "add a test"
    assert approvals.status(env.repo)["codex"]["verdict"] == "REQUEST_CHANGES"


def test_codex_review_failures_record_nothing(env):
    (env.repo / "app.py").write_text("boom\n")
    env.scenario([{"error": "crash"}, {"raw_output": "not json", "text": "prose"}])
    client = env.start()
    assert "codex team_review failed" in client.error_text("codex_review")
    assert "schema" in client.error_text("codex_review")
    assert approvals.status(env.repo)["codex"] is None


def test_codex_review_needs_changes_and_a_valid_base(env):
    client = env.start()
    assert "nothing to review" in client.error_text("codex_review")
    (env.repo / "app.py").write_text("x\n")
    assert "cannot resolve" in client.error_text("codex_review", {"base": "no-such-ref"})
    assert env.calls() == []


def test_codex_review_outside_a_repo_is_an_error_result(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    client = env.start(cwd=plain, project_dir=plain)
    assert "failed" in client.error_text("codex_review")
    assert "failed" in client.error_text("review_status")


def test_both_reviews_allow_the_commit_and_a_tree_change_voids_it(env):
    (env.repo / "app.py").write_text("print('v1')\n")
    env.scenario([verdict("APPROVE", "codex ok")], claude=[verdict("APPROVE", "claude ok", [finding("nit")])])
    client = env.start()
    client.text("codex_review")
    recorded = json.loads(client.text("claude_review"))
    assert recorded["verdict"] == "APPROVE" and recorded["commit_allowed"] is True and recorded["missing"] == []
    status = json.loads(client.text("review_status"))
    assert status["commit_allowed"] is True
    assert status["claude"]["summary"] == "claude ok" and status["codex"]["summary"] == "codex ok"
    assert status["tree"] == approvals.current_tree(env.repo)

    (env.repo / "app.py").write_text("print('v2')\n")
    voided = json.loads(client.text("review_status"))
    assert voided["commit_allowed"] is False and len(voided["missing"]) == 2


def test_claude_review_runs_a_fresh_read_only_claude_and_records_the_verdict_itself(env):
    (env.repo / "app.py").write_text("print('changed')\n")
    (env.repo / "new_file.py").write_text("x = 1\n")
    env.scenario(claude=[verdict("APPROVE", "claude says fine", [finding("nit")])])
    client = env.start()
    body = json.loads(client.text("claude_review", {"focus": "check the retry logic"}))
    assert body["verdict"] == "APPROVE" and body["summary"] == "claude says fine"
    assert body["tree"] == approvals.current_tree(env.repo)
    assert body["commit_allowed"] is False and body["missing"] == ["codex: no review of the current changes"]

    (call,) = env.calls("claude")
    argv = call["argv"]
    assert argv[0] == "-p" or "-p" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep" and "--safe-mode" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in argv and argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5" and argv[argv.index("--effort") + 1] == "high"
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == ["verdict", "summary", "findings"]
    assert "--plugin-dir" not in argv and "--settings" not in argv  # nothing that could call back into WorkForce
    assert call["cwd"].endswith("proj") and call["prompt"]
    for needle in ("check the retry logic", "print('changed')", "new_file.py", "x = 1", body["tree"]):
        assert needle in call["prompt"], needle

    status = approvals.status(env.repo)
    assert status["claude"]["verdict"] == "APPROVE" and status["claude"]["findings"][0]["severity"] == "nit"
    assert status["codex"] is None
    assert list(json.loads(approvals.approvals_path(env.repo).read_text())[body["tree"]]["claude"]).count("sig") == 1


def test_claude_review_uses_the_configured_reviewer_model(env):
    (env.repo / "app.py").write_text("x\n")
    config.set_team_models("claude-sonnet-5-5", "max", None, None, env.home)
    env.scenario(claude=[verdict()])
    client = env.start()
    client.text("claude_review")
    argv = env.calls("claude")[0]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5" and argv[argv.index("--effort") + 1] == "max"


def test_claude_review_consistency_failures_and_no_changes(env):
    client = env.start()
    assert "nothing to review" in client.error_text("claude_review")
    (env.repo / "app.py").write_text("boom\n")
    env.scenario(claude=[verdict("APPROVE", "fine", [finding("major", "bad")]), {"error": "crash"}, {"error": "auth"}, {"text": "prose only"}])
    body = json.loads(client.text("claude_review"))
    assert body["verdict"] == "REQUEST_CHANGES" and body["commit_allowed"] is False
    assert approvals.status(env.repo)["claude"]["verdict"] == "REQUEST_CHANGES"
    assert "claude reviewer failed" in client.error_text("claude_review")
    assert "claude reviewer failed" in client.error_text("claude_review")
    assert "schema" in client.error_text("claude_review")


def test_claude_review_failures_record_nothing(env):
    (env.repo / "app.py").write_text("boom\n")
    env.scenario(claude=[{"error": "rate_limited"}])
    client = env.start()
    assert "rate_limited" in client.error_text("claude_review")
    assert approvals.status(env.repo)["claude"] is None


def test_there_is_no_tool_that_records_a_verdict(env):
    (env.repo / "app.py").write_text("x\n")
    client = env.start()
    for name in ("record_claude_review", "record_review", "record_verdict"):
        assert client.request("tools/call", {"name": name, "arguments": {"verdict": "APPROVE", "summary": "s"}})["error"]["code"] == -32602
    for tool in client.request("tools/list")["result"]["tools"]:
        assert "verdict" not in tool["inputSchema"]["properties"]
    assert approvals.status(env.repo)["claude"] is None


def test_usage_reports_cached_claude_and_refreshes_a_stale_codex_cache(env):
    now = time.time()
    usage_cache.write_claude({"five_hour": {"used_percentage": 61.0, "resets_at": int(now) + 3000}, "seven_day": {"used_percentage": 10, "resets_at": int(now) + 90000}}, env.home)
    env.scenario(
        codex_limits={
            "primary": {"usedPercent": 12.0, "windowDurationMins": 300, "resetsAt": int(now) + 1000},
            "secondary": {"usedPercent": 3.0, "windowDurationMins": 10080, "resetsAt": int(now) + 50000},
        }
    )
    client = env.start()
    body = json.loads(client.text("usage"))
    assert body["state"]["level"] == "stop" and body["state"]["window"] == "claude five_hour"
    assert body["claude"]["five_hour"]["used_percentage"] == 61.0
    assert {w["name"] for w in body["codex"]} == {"five_hour", "seven_day"}
    assert "warning" not in body
    assert usage_cache.read(env.home)["codex"] is not None


def test_usage_uses_a_fresh_codex_cache_without_calling_codex(env):
    now = time.time()
    usage_cache.write_codex([Window("codex", "seven_day", 55.0, int(now) + 5000, now, "weekly")], env.home)
    env.scenario()
    client = env.start()
    body = json.loads(client.text("usage"))
    assert "warning" not in body
    assert body["state"]["level"] == "alert" and body["state"]["window"] == "codex seven_day"


def test_usage_keeps_the_stale_cache_and_warns_when_codex_cannot_answer(env):
    old = time.time() - 1000
    usage_cache.write_codex([Window("codex", "seven_day", 5.0, int(old) + 99999, old, "weekly")], env.home, now=old)
    env.scenario()
    client = env.start()
    body = json.loads(client.text("usage"))
    assert "codex usage unavailable" in body["warning"]
    assert body["codex"][0]["percent"] == 5.0
    assert body["claude"].startswith("no reading yet")


def test_usage_override_is_reflected(env):
    now = time.time()
    resets = int(now) + 3000
    usage_cache.write_claude({"five_hour": {"used_percentage": 75.0, "resets_at": resets}}, env.home)
    usage_cache.write_codex([], env.home)
    usage_cache.override_set("claude:five_hour", resets, env.home)
    client = env.start()
    body = json.loads(client.text("usage"))
    assert body["state"]["level"] == "alert" and body["state"]["overridden"] is True


@pytest.fixture
def ollaya():
    import threading as _t

    fake = FakeOllaya()
    thread = _t.Thread(target=fake.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield fake
    fake.shutdown()
    fake.server_close()


def test_laya_unavailable_says_so_clearly(env):
    env.extra_env["WF_LAYA_URL"] = "http://127.0.0.1:1"
    client = env.start()
    result = client.call("laya", {"question": "small change?"})
    assert not result.get("isError")
    assert "Laya not running" in result["content"][0]["text"]


def test_laya_answers_yes_no_and_pick_one(env, ollaya):
    env.extra_env["WF_LAYA_URL"] = ollaya.url
    ollaya.choose = "yes"
    client = env.start()
    yes = json.loads(client.text("laya", {"question": "Is this a rename?", "context": "renames foo to bar"}))
    assert yes == {"answer": True, "confidence": 0.95, "confident": True}
    ollaya.choose = "beta"
    pick = json.loads(client.text("laya", {"question": "Which one?", "options": ["alpha", "beta"]}))
    assert pick["answer"] == "beta" and pick["confident"] is True
    assert "renames foo to bar" in ollaya.requests[0]["body"]["state"]
    assert list(ollaya.requests[1]["body"]["questions"]["q"]["criteria"]) == ["alpha", "beta"]


def test_laya_below_threshold_or_broken_is_unsure(env, ollaya):
    env.extra_env["WF_LAYA_URL"] = ollaya.url
    client = env.start()
    ollaya.confidence = 0.4
    weak = json.loads(client.text("laya", {"question": "q?"}))
    assert weak["confident"] is False and "unsure" in weak["note"]
    ollaya.mode = "garbage"
    assert "unsure" in client.text("laya", {"question": "q?"})
    assert "at least two" in client.error_text("laya", {"question": "q?", "options": ["only"]})
    assert "'question' is required" in client.error_text("laya", {})


def test_codex_settings_get_and_set_persist_to_team_toml(env):
    client = env.start()
    got = json.loads(client.text("codex_settings"))
    assert (got["codex_model"], got["codex_effort"]) == ("gpt-6-sol", "high")
    set_ = json.loads(client.text("codex_settings", {"model": "gpt-6-astra", "effort": "xhigh"}))
    assert (set_["codex_model"], set_["codex_effort"]) == ("gpt-6-astra", "xhigh")
    saved = config.load(env.home)
    assert (saved.codex_model, saved.codex_effort) == ("gpt-6-astra", "xhigh")
    assert saved.codex == str(FAKE)

    env.scenario([{"text": "plan"}])
    client.text("codex_plan", {"task": "t"})
    argv = env.calls()[0]["argv"]
    assert argv[argv.index("-m") + 1] == "gpt-6-astra" and "model_reasoning_effort=xhigh" in argv

    assert "not valid" in client.error_text("codex_settings", {"effort": "turbo"})
    assert json.loads(client.text("codex_settings"))["codex_effort"] == "xhigh"


def test_every_call_is_logged_redacted_and_truncated(env):
    env.scenario([{"text": "plan"}])
    client = env.start()
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
    client.text("codex_plan", {"task": f"use key {secret} and " + "x" * 3000})
    client.error_text("codex_plan", {})
    client.text("review_status")
    lines = [json.loads(l) for l in (env.home / ".workforce" / "team.log").read_text().splitlines()]
    assert [(l["tool"], l["ok"]) for l in lines] == [("codex_plan", True), ("codex_plan", False), ("review_status", True)]
    raw = (env.home / ".workforce" / "team.log").read_text()
    assert secret not in raw
    assert len(lines[0]["args"]["task"]) < 700


def test_server_exits_cleanly_when_stdin_closes(env):
    client = env.start()
    client.proc.stdin.close()
    assert client.proc.wait(timeout=15) == 0



def plan_verdict(v="APPROVED", findings=(), session_id=None):
    step = {"structured": {"verdict": v, "summary": f"plan {v}", "findings": list(findings), "coverage": ["PLAN.md"], "limitations": []}}
    if session_id:
        step["session_id"] = session_id
    return step


def plan_finding(severity="high", fid="F1"):
    return {"id": fid, "severity": severity, "path": "app.py", "evidence": "app.py has no retry", "fix": "add a retry step"}


def write_plan(env, text="# Plan\n\nGoal: add retries to app.py\n"):
    plan = env.repo / ".workforce" / "team" / "plans" / "retries" / "PLAN.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(text)
    return plan


def test_codex_plan_review_reviews_the_plan_read_only_and_logs_the_round_verbatim(env):
    plan = write_plan(env)
    env.scenario([plan_verdict("REVISE", [plan_finding()], session_id="plan-thread")])
    client = env.start()
    body = json.loads(client.text("codex_plan_review", {"plan": ".workforce/team/plans/retries/PLAN.md"}))
    assert body["verdict"] == "REVISE" and body["round"] == 1 and body["session_id"] == "plan-thread"
    assert body["plan_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    call = env.calls()[0]
    argv = call["argv"]
    assert argv[argv.index("-s") + 1] == "read-only" and "--output-schema" in argv and "features.plugins=false" in argv
    assert "Goal: add retries to app.py" in call["prompt"] and "round 1" in call["prompt"]
    log = (plan.parent / "REVIEW-LOG.md").read_text()
    assert "## Round 1" in log and "app.py has no retry" in log and "recorded verdict: REVISE" in log
    status = json.loads(client.text("plan_status", {"plan": str(plan)}))
    assert status["approved"] is False and status["rounds"] == 1


def test_a_second_round_resumes_the_same_reviewer_with_the_dispositions(env):
    plan = write_plan(env)
    env.scenario([plan_verdict("REVISE", [plan_finding()], session_id="plan-thread"), plan_verdict("APPROVED", session_id="plan-thread")])
    client = env.start()
    rel = ".workforce/team/plans/retries/PLAN.md"
    first = json.loads(client.text("codex_plan_review", {"plan": rel}))
    plan.write_text(plan.read_text() + "\n3. Add a retry with backoff; proof: pytest tests/test_retry.py\n")
    second = json.loads(client.text("codex_plan_review", {"plan": rel, "session_id": first["session_id"], "feedback": "F1 accepted: step 3 added"}))
    assert second["verdict"] == "APPROVED" and second["round"] == 2
    argv = env.calls()[1]["argv"]
    assert "resume" in argv and "plan-thread" in argv
    assert "F1 accepted: step 3 added" in env.calls()[1]["prompt"]
    log = (plan.parent / "REVIEW-LOG.md").read_text()
    assert "## Round 2" in log and "F1 accepted: step 3 added" in log
    assert json.loads(client.text("plan_status", {"plan": rel}))["approved"] is True


def test_a_plan_approval_is_void_once_the_plan_changes(env):
    plan = write_plan(env)
    env.scenario([plan_verdict("APPROVED")])
    client = env.start()
    client.text("codex_plan_review", {"plan": str(plan)})
    assert json.loads(client.text("plan_status", {"plan": str(plan)}))["approved"] is True
    plan.write_text(plan.read_text() + "\nscope creep\n")
    status = json.loads(client.text("plan_status", {"plan": str(plan)}))
    assert status["approved"] is False and "changed" in status["note"]


def test_approved_with_a_medium_finding_is_recorded_as_revise(env):
    plan = write_plan(env)
    env.scenario([plan_verdict("APPROVED", [plan_finding("medium")])])
    client = env.start()
    body = json.loads(client.text("codex_plan_review", {"plan": str(plan)}))
    assert body["verdict"] == "REVISE" and "medium" in body["note"]
    assert json.loads(client.text("plan_status", {"plan": str(plan)}))["approved"] is False


@pytest.mark.parametrize("plan", ["../outside.md", "/etc/hosts", "missing.md"])
def test_plan_review_refuses_a_plan_outside_the_project_or_missing(env, tmp_path, plan):
    (tmp_path / "outside.md").write_text("x\n")
    client = env.start()
    result = client.call("codex_plan_review", {"plan": plan})
    assert result.get("isError") and env.calls() == []
