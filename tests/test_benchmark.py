import importlib.util
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from workforce.team import relay
from workforce.team import config as team_config

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "benchmark.py"
TASKS_DIR = ROOT / "scripts" / "bench_tasks"

spec = importlib.util.spec_from_file_location("wf_benchmark", SCRIPT)
bench = importlib.util.module_from_spec(spec)
sys.modules["wf_benchmark"] = bench
spec.loader.exec_module(bench)

FAKE_AGENT = textwrap.dedent(
    '''\
    #!{python}
    import json, os, shutil, subprocess, sys, time
    from pathlib import Path

    name = Path(sys.argv[0]).name
    args = sys.argv[1:]
    cwd = Path.cwd()
    with open(os.environ["FAKE_ARGV_LOG"], "a") as log:
        log.write(json.dumps({{"name": name, "args": args, "cwd": str(cwd), "wf_home": os.environ.get("WF_HOME")}}) + "\\n")
    behaviour = os.environ.get("FAKE_BEHAVIOUR", "solve")
    if behaviour == "sleep":
        time.sleep(60)
    if behaviour == "garbage":
        print("not json at all")
        sys.exit(1)
    if behaviour == "error":
        print(json.dumps({{"type": "result", "is_error": True, "result": "model is not available", "usage": {{}}}}))
        sys.exit(0)
    task = cwd.parent.name.rsplit("-", 2)[0]
    reference = Path(os.environ["FAKE_TASKS_DIR"]) / task / "reference"
    if behaviour == "solve":
        shutil.copytree(reference, cwd, dirs_exist_ok=True)
    if behaviour == "solve" and os.environ.get("FAKE_COMMIT", "1") == "1":
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "solve"], cwd=cwd, check=True)
    if name == "wf":
        home = Path(os.environ["WF_HOME"]) / ".workforce"
        home.mkdir(parents=True, exist_ok=True)
        entries = [
            {{"tool": "codex_plan", "seconds": 12.5, "ok": True}},
            {{"tool": "codex_review", "seconds": 30.0, "ok": True}},
            {{"tool": "claude_review", "seconds": 20.0, "ok": True}},
            {{"tool": "codex_review", "seconds": 28.0, "ok": True}},
            {{"tool": "usage", "seconds": 1.0, "ok": True}},
        ]
        (home / "team.log").write_text("\\n".join(json.dumps(e) for e in entries) + "\\n")
        team = cwd / ".workforce" / "team"
        team.mkdir(parents=True)
        with open(cwd / ".git" / "info" / "exclude", "a") as handle:
            handle.write(".workforce/\\n")
        marker = "===== CODEX OUTPUT (verbatim) =====\\n"
        def artefact(filename, session, reply):
            (team / filename).write_text(
                "# x 1 · now\\nmodel: m · effort: e · sandbox: read-only · codex session: " + session + "\\n\\n"
                "===== FULL PROMPT GIVEN TO CODEX (exact) =====\\nprompt\\n\\n" + marker + reply + "\\n"
            )
        artefact("plan-1.md", "SESS-PLAN", "a plan")
        artefact("review-1.md", "SESS-REV1", json.dumps({{"verdict": "REQUEST_CHANGES", "summary": "s", "findings": [{{"severity": "major", "file": "a.py", "line": 1, "message": "m"}}, {{"severity": "minor", "file": "a.py", "line": 2, "message": "n"}}]}}))
        artefact("review-2.md", "SESS-REV2", json.dumps({{"verdict": "APPROVE", "summary": "ok", "findings": []}}))
        artefact("claude-review-1.md", "unknown", json.dumps({{"verdict": "APPROVE", "summary": "ok", "findings": []}}))
        sessions = Path(os.environ["FAKE_CODEX_HOME"]) / "sessions" / "2026" / "09" / "30"
        sessions.mkdir(parents=True, exist_ok=True)
        for sid, total in (("SESS-PLAN", 1000), ("SESS-REV1", 2000), ("SESS-REV2", 3000)):
            early = {{"type": "event_msg", "payload": {{"type": "token_count", "info": {{"total_token_usage": {{"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0, "total_tokens": 2}}}}}}}}
            late = {{"type": "event_msg", "payload": {{"type": "token_count", "info": {{"total_token_usage": {{"input_tokens": total - 100, "cached_input_tokens": 10, "output_tokens": 100, "reasoning_output_tokens": 5, "total_tokens": total}}}}}}}}
            (sessions / ("rollout-2026-09-30T00-00-00-" + sid + ".jsonl")).write_text(json.dumps(early) + "\\n" + json.dumps(late) + "\\n")
    print(json.dumps({{
        "type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "s-1",
        "num_turns": 4, "duration_ms": 1234, "total_cost_usd": 0.0123,
        "usage": {{"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40}},
        "modelUsage": {{"claude-haiku-4-5-20251001": {{"inputTokens": 10, "outputTokens": 20, "costUSD": 0.0123}}}},
    }}))
    '''
)

FAKE_CODEX = textwrap.dedent(
    '''\
    #!{python}
    import json, os, sys

    if sys.argv[1:2] != ["app-server"]:
        sys.exit(2)
    counter = os.path.join(os.environ["FAKE_CODEX_HOME"], "reads")
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        if msg.get("method") == "initialize":
            print(json.dumps({{"id": msg["id"], "result": {{}}}}), flush=True)
        elif msg.get("method") == "account/rateLimits/read":
            try:
                reads = int(open(counter).read())
            except OSError:
                reads = 0
            open(counter, "w").write(str(reads + 1))
            used = 10.0 + 2.5 * reads
            limits = {{"primary": {{"usedPercent": used, "windowDurationMins": 10080, "resetsAt": 1791265456}},
                       "secondary": {{"usedPercent": 1.0, "windowDurationMins": 300, "resetsAt": 1790000000}}}}
            print(json.dumps({{"id": msg["id"], "result": {{"rateLimits": limits}}}}), flush=True)
    '''
)


def make_executable(path: Path, body: str) -> Path:
    path.write_text(body.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    agent = FAKE_AGENT
    paths = {
        "claude": make_executable(bin_dir / "claude", agent),
        "wf": make_executable(bin_dir / "wf", agent),
        "codex": make_executable(bin_dir / "codex", FAKE_CODEX),
        "codex_home": codex_home,
        "log": tmp_path / "argv.jsonl",
    }
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "FAKE_ARGV_LOG": str(paths["log"]),
        "FAKE_TASKS_DIR": str(TASKS_DIR),
        "FAKE_CODEX_HOME": str(codex_home),
        "GIT_CONFIG_GLOBAL": os.environ["GIT_CONFIG_GLOBAL"],
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    monkeypatch.setenv("FAKE_CODEX_HOME", str(codex_home))
    paths["env"] = env
    return paths


def task_named(name):
    return bench.discover_tasks(TASKS_DIR, [name])[0]


def run_args(fakes, **overrides):
    args = dict(
        claude_bin=str(fakes["claude"]),
        codex_bin=str(fakes["codex"]),
        wf_bin=str(fakes["wf"]),
        claude_model="claude-haiku-4-5-20251001",
        claude_effort="low",
        codex_model="gpt-6-luna",
        codex_effort="low",
        timeout_s=60,
        environ=fakes["env"],
        codex_home=fakes["codex_home"],
    )
    args.update(overrides)
    return args


class TestTasks:
    def test_at_least_three_complete_tasks(self):
        tasks = bench.discover_tasks(TASKS_DIR)
        assert len(tasks) >= 3
        for task in tasks:
            assert task.text
            assert (task.seed / "README.md").is_file()
            assert not (task.seed / "hidden_test.py").exists()
            assert any(task.seed.glob("test_*.py"))

    def test_unknown_task_is_an_error(self):
        with pytest.raises(ValueError, match="unknown task"):
            bench.discover_tasks(TASKS_DIR, ["nope"])

    @pytest.mark.parametrize("task", [t.name for t in bench.discover_tasks(TASKS_DIR)])
    def test_hidden_tests_fail_on_the_seed_and_pass_on_the_reference(self, tmp_path, task):
        task = task_named(task)
        repo = bench.make_repo(task, tmp_path / "repo")
        assert bench.run_hidden_test(task, repo)["passed"] is False
        (repo / "reference").mkdir()
        for source in (task.path / "reference").iterdir():
            (repo / source.name).write_bytes(source.read_bytes())
        result = bench.run_hidden_test(task, repo)
        assert result["passed"] is True, result["output_tail"]
        assert result["tests_run"] >= 8
        assert result["failed"] == 0
        assert bench.run_hidden_test(task, repo)["passed"] is True

    def test_the_seed_itself_is_healthy(self, tmp_path):
        import subprocess

        for task in bench.discover_tasks(TASKS_DIR):
            repo = bench.make_repo(task, tmp_path / task.name)
            done = subprocess.run(
                [sys.executable, "-m", "unittest"],
                cwd=repo,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            assert done.returncode == 0, done.stderr

    def test_hidden_test_is_not_in_the_repo(self, tmp_path):
        task = task_named("pagination")
        repo = bench.make_repo(task, tmp_path / "repo")
        assert not list(repo.rglob("hidden_test.py"))
        assert bench.git(repo, "config", "commit.gpgsign") == "false"
        assert bench.git(repo, "rev-list", "--count", "HEAD") == "1"

    @pytest.mark.parametrize(
        "task, filename, old, new",
        [
            ("pagination", "catalog.py", "(page - 1) * per_page", "page * per_page"),
            ("pagination", "catalog.py", "-(-total // per_page)", "total // per_page + 1"),
            (
                "dst_schedule",
                "schedule.py",
                "datetime.combine(start + timedelta(days=n), at, tzinfo=zone)",
                "add_days(datetime.combine(start, at, tzinfo=zone), n)",
            ),
            ("static_files", "static_server.py", "os.path.commonpath([real_root, target]) != real_root", "not target.startswith(real_root)"),
            (
                "static_files",
                "static_server.py",
                'target = os.path.realpath(os.path.join(real_root, relative.lstrip("/")))',
                'target = os.path.normpath(os.path.join(real_root, relative.lstrip("/")))',
            ),
            (
                "bill_split",
                "money.py",
                "shares = [total_cents * weight // weight_sum for weight in weights]",
                "return [share(total_cents, weight, weight_sum) for weight in weights]",
            ),
        ],
    )
    def test_plausible_mistakes_are_caught_by_the_hidden_tests(self, tmp_path, task, filename, old, new):
        task = task_named(task)
        repo = bench.make_repo(task, tmp_path / "repo")
        source = (task.path / "reference" / filename).read_text()
        assert old in source
        (repo / filename).write_text(source.replace(old, new))
        result = bench.run_hidden_test(task, repo)
        assert result["passed"] is False, "the hidden tests missed this mistake"


class TestClaudeOutput:
    RESULT = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "all done",
        "session_id": "abc",
        "num_turns": 7,
        "duration_ms": 4321,
        "total_cost_usd": 0.05,
        "usage": {"input_tokens": 5, "output_tokens": 6, "cache_creation_input_tokens": 7, "cache_read_input_tokens": 8},
        "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 5, "outputTokens": 6, "costUSD": 0.05}},
    }

    def test_object_form(self):
        parsed = bench.parse_claude_output(json.dumps(self.RESULT))
        assert parsed["parsed"] is True
        assert parsed["tokens"] == {
            "input_tokens": 5,
            "output_tokens": 6,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 8,
        }
        assert parsed["cost_usd_estimate"] == 0.05
        assert parsed["num_turns"] == 7
        assert parsed["models"]["claude-haiku-4-5-20251001"]["output_tokens"] == 6
        assert parsed["is_error"] is False

    def test_list_form_uses_the_result_event(self):
        events = [{"type": "system", "subtype": "init"}, {"type": "assistant"}, self.RESULT]
        parsed = bench.parse_claude_output(json.dumps(events))
        assert parsed["parsed"] is True and parsed["tokens"]["output_tokens"] == 6

    def test_jsonl_form(self):
        text = json.dumps({"type": "system"}) + "\n" + json.dumps(self.RESULT) + "\n"
        assert bench.parse_claude_output(text)["cost_usd_estimate"] == 0.05

    def test_missing_fields_are_none_not_zero(self):
        parsed = bench.parse_claude_output(json.dumps({"type": "result", "is_error": False, "result": "x"}))
        assert parsed["tokens"]["input_tokens"] is None
        assert parsed["cost_usd_estimate"] is None

    def test_error_result(self):
        parsed = bench.parse_claude_output(json.dumps({"type": "result", "is_error": True, "result": "API Error: 400"}))
        assert parsed["is_error"] is True and "400" in parsed["result_excerpt"]

    @pytest.mark.parametrize("text", ["", "   ", "not json", "[1, 2]", "null"])
    def test_garbage_is_unparsed(self, text):
        assert bench.parse_claude_output(text) == {"parsed": False}


class TestCommandLine:
    def test_solo_and_team_share_every_argument_but_the_binary(self):
        solo = bench.build_argv("solo", "/x/claude", "do it", "claude-haiku-4-5-20251001", "low")
        team = bench.build_argv("team", "/x/wf", "do it", "claude-haiku-4-5-20251001", "low")
        assert solo[0] == "/x/claude" and team[0] == "/x/wf"
        assert solo[1:] == team[1:]
        assert solo[1:3] == ["-p", "do it"]
        assert solo[solo.index("--output-format") + 1] == "json"
        assert solo[solo.index("--permission-mode") + 1] == "acceptEdits"
        allowed = solo[solo.index("--allowedTools") + 1].split(",")
        for needed in ("Edit", "Write", "Bash(python3:*)", "Bash(git add:*)", "Bash(git commit:*)"):
            assert needed in allowed
        assert solo[solo.index("--model") + 1] == "claude-haiku-4-5-20251001"
        assert solo[solo.index("--effort") + 1] == "low"

    def test_unknown_mode(self):
        with pytest.raises(ValueError):
            bench.build_argv("both", "x", "p", "m", "low")

    def test_prompt_is_the_task_plus_the_commit_request(self):
        task = task_named("pagination")
        prompt = task.prompt()
        assert prompt.startswith(task.text)
        assert "commit" in prompt.lower()

    def test_team_toml_sets_models_and_absolute_paths(self, tmp_path):
        bench.write_team_toml(tmp_path, "/abs/claude", "/abs/codex", "gpt-6-luna", "low", "claude-haiku-4-5-20251001", "low")
        cfg = team_config.load(tmp_path)
        assert cfg.claude == "/abs/claude" and cfg.codex == "/abs/codex"
        assert (cfg.codex_model, cfg.codex_effort) == ("gpt-6-luna", "low")
        assert (cfg.reviewer_model, cfg.reviewer_effort) == ("claude-haiku-4-5-20251001", "low")
        assert (cfg.fast_coder_model, cfg.fast_coder_effort) == ("claude-haiku-4-5-20251001", "low")

    def test_api_key_detection(self):
        assert bench.api_key_vars({"PATH": "/bin"}) == []
        assert bench.api_key_vars({"ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": ""}) == ["ANTHROPIC_API_KEY"]


class TestTeamArtefacts:
    def test_team_log_calls(self, tmp_path):
        log = tmp_path / "team.log"
        lines = [
            {"tool": "codex_plan", "seconds": 10.0, "ok": True},
            {"tool": "codex_review", "seconds": 5.5, "ok": False},
            {"tool": "claude_review", "seconds": 7.0, "ok": True},
            {"tool": "usage", "seconds": 1.0, "ok": True},
        ]
        log.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n[1]\n")
        summary = bench.summarize_calls(bench.parse_team_log(log))
        assert summary["codex_call_count"] == 2
        assert summary["codex_call_seconds"] == 15.5
        assert [c["tool"] for c in summary["codex_calls"]] == ["codex_plan", "codex_review"]
        assert summary["claude_review_call_count"] == 1
        assert summary["claude_review_call_seconds"] == 7.0

    def test_missing_team_log(self, tmp_path):
        assert bench.parse_team_log(tmp_path / "nope.log") == []
        assert bench.summarize_calls([])["codex_call_count"] == 0

    def test_reads_artefacts_written_by_the_real_relay(self, tmp_path):
        verdict = {"verdict": "REQUEST_CHANGES", "summary": "s", "findings": [{"severity": "major", "file": "a", "line": 1, "message": "m"}]}
        relay.save_artifact(
            tmp_path, "review", sent={"focus": None}, prompt="p", reply=json.dumps(verdict),
            model="m", effort="low", sandbox="read-only", session="SESS-1",
        )
        relay.save_artifact(
            tmp_path, "review", sent={"focus": None}, prompt="p", reply=json.dumps({"verdict": "APPROVE", "summary": "ok", "findings": []}),
            model="m", effort="low", sandbox="read-only", session="SESS-2",
        )
        relay.save_artifact(
            tmp_path, "claude-review", sent={}, prompt="p", reply=json.dumps({"verdict": "APPROVE", "summary": "ok", "findings": []}),
            model="m", effort="low", sandbox="read-only", session=None,
        )
        relay.save_artifact(
            tmp_path, "plan", sent={"task": "t"}, prompt="p", reply="a plan", model="m", effort="low", sandbox="read-only", session="SESS-0",
        )
        found = bench.parse_artifacts(tmp_path)
        assert [(r["reviewer"], r["verdict"], r["findings"]) for r in found["reviews"]] == [
            ("claude", "APPROVE", 0),
            ("codex", "REQUEST_CHANGES", 1),
            ("codex", "APPROVE", 0),
        ]
        assert set(found["codex_sessions"]) == {"SESS-0", "SESS-1", "SESS-2"}
        summary = bench.review_summary(found["reviews"])
        assert summary["review_count"] == 3
        assert summary["changes_requested"] == 1
        assert summary["findings_total"] == 1
        assert summary["final_verdicts"] == {"claude": "APPROVE", "codex": "APPROVE"}

    def test_reply_that_is_not_json_still_yields_a_verdict(self):
        text = 'Some prose then {"verdict": "BLOCKED", "summary": "x"'
        assert bench.parse_review_reply(text) == {"verdict": "BLOCKED", "findings": None}
        assert bench.parse_review_reply("no verdict here") == {"verdict": None, "findings": None}

    def test_no_artefacts(self, tmp_path):
        assert bench.parse_artifacts(tmp_path) == {"reviews": [], "codex_sessions": []}
        assert bench.review_summary([])["findings_total"] is None

    def test_codex_tokens_use_the_last_cumulative_count_per_session(self, tmp_path):
        day = tmp_path / "sessions" / "2026" / "09" / "30"
        day.mkdir(parents=True)

        def rollout(session, totals):
            lines = [
                json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": t - 5, "output_tokens": 5, "total_tokens": t}}}})
                for t in totals
            ]
            (day / f"rollout-2026-09-30T00-00-00-{session}.jsonl").write_text("\n".join(["{}", *lines]) + "\n")

        rollout("S1", [100, 250])
        rollout("S2", [40])
        usage = bench.codex_session_usage(["S1", "S2", "S1", "S3"], tmp_path)
        assert usage["total_tokens"] == 290
        assert usage["input_tokens"] == 280
        assert usage["sessions_with_usage"] == 2

    def test_codex_tokens_unavailable_is_none(self, tmp_path):
        assert bench.codex_session_usage(["S1"], tmp_path) is None
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "rollout-x-S1.jsonl").write_text("{}\n")
        assert bench.codex_session_usage(["S1"], tmp_path) is None


class TestCodexUsage:
    def test_reads_weekly_and_five_hour(self, fakes):
        read = bench.read_codex_usage(fakes["codex"])
        assert read == {"weekly": 10.0, "five_hour": 1.0, "error": None}

    def test_a_missing_binary_is_a_note_not_a_number(self, tmp_path):
        read = bench.read_codex_usage(tmp_path / "no-such-codex")
        assert read["weekly"] is None and read["five_hour"] is None
        assert "cannot start" in read["error"]


class TestRuns:
    def test_solo_run(self, fakes, tmp_path):
        record = bench.run_one(task_named("pagination"), "solo", 1, tmp_path / "work", **run_args(fakes))
        assert record["ok"] is True and record["error"] is None
        assert record["commit_made"] is True and record["commits"] == 2
        assert record["hidden"]["passed"] is True
        assert record["claude"]["tokens"]["output_tokens"] == 20
        assert record["claude"]["cost_usd_estimate"] == 0.0123
        assert record["seconds"] >= 0
        assert "codex" not in record and "reviews" not in record
        call = json.loads(fakes["log"].read_text().splitlines()[-1])
        assert call["name"] == "claude" and call["wf_home"] is None
        assert "--plugin-dir" not in call["args"]

    def test_team_run(self, fakes, tmp_path):
        readings = iter([{"weekly": 10.0, "five_hour": 1.0, "error": None}, {"weekly": 12.5, "five_hour": 2.0, "error": None}])
        record = bench.run_one(
            task_named("dst_schedule"), "team", 1, tmp_path / "work", **run_args(fakes, usage_reader=lambda _: next(readings))
        )
        assert record["ok"] is True
        assert record["hidden"]["passed"] is True and record["commit_made"] is True
        codex = record["codex"]
        assert codex["codex_call_count"] == 3
        assert codex["codex_call_seconds"] == 70.5
        assert codex["claude_review_call_count"] == 1
        assert set(codex["sessions"]) == {"SESS-PLAN", "SESS-REV1", "SESS-REV2"}
        assert codex["tokens"]["total_tokens"] == 6000
        assert (codex["weekly_before"], codex["weekly_after"], codex["weekly_delta"]) == (10.0, 12.5, 2.5)
        summary = record["review_summary"]
        assert summary["review_count"] == 3 and summary["changes_requested"] == 1 and summary["findings_total"] == 2
        assert summary["final_verdicts"] == {"claude": "APPROVE", "codex": "APPROVE"}
        call = json.loads(fakes["log"].read_text().splitlines()[-1])
        assert call["name"] == "wf"
        assert call["wf_home"].endswith("wf-home")
        assert not Path(call["wf_home"]).is_relative_to(Path(fakes["env"]["HOME"]))
        toml = (Path(call["wf_home"]) / ".workforce" / "team.toml").read_text()
        assert 'codex_model = "gpt-6-luna"' in toml and f'claude = "{fakes["claude"]}"' in toml

    def test_a_commit_that_was_never_made_is_reported(self, fakes, tmp_path):
        env = {**fakes["env"], "FAKE_COMMIT": "0"}
        record = bench.run_one(task_named("bill_split"), "solo", 1, tmp_path / "work", **run_args(fakes, environ=env))
        assert record["commit_made"] is False
        assert record["uncommitted_changes"] is True
        assert record["hidden"]["passed"] is True

    def test_a_wrong_solution_fails_the_hidden_tests(self, fakes, tmp_path):
        env = {**fakes["env"], "FAKE_BEHAVIOUR": "nothing"}
        record = bench.run_one(task_named("bill_split"), "solo", 1, tmp_path / "work", **run_args(fakes, environ=env))
        assert record["ok"] is True
        assert record["hidden"]["passed"] is False
        assert record["commit_made"] is False

    def test_error_result_is_a_failure(self, fakes, tmp_path):
        env = {**fakes["env"], "FAKE_BEHAVIOUR": "error"}
        record = bench.run_one(task_named("pagination"), "solo", 1, tmp_path / "work", **run_args(fakes, environ=env))
        assert record["ok"] is False
        assert "model is not available" in record["error"]

    def test_unparseable_output_is_a_failure(self, fakes, tmp_path):
        env = {**fakes["env"], "FAKE_BEHAVIOUR": "garbage"}
        record = bench.run_one(task_named("pagination"), "solo", 1, tmp_path / "work", **run_args(fakes, environ=env))
        assert record["ok"] is False and "exit code 1" in record["error"]

    def test_timeout_is_a_failure(self, fakes, tmp_path):
        env = {**fakes["env"], "FAKE_BEHAVIOUR": "sleep"}
        record = bench.run_one(task_named("pagination"), "solo", 1, tmp_path / "work", **run_args(fakes, environ=env, timeout_s=1))
        assert record["ok"] is False and record["timed_out"] is True
        assert record["error"] == "timed out"
        assert record["seconds"] < 30

    def test_a_missing_binary_becomes_a_failed_record(self, fakes, tmp_path):
        record = bench.run_one(
            task_named("pagination"), "solo", 1, tmp_path / "work", **run_args(fakes, claude_bin=str(tmp_path / "missing"))
        )
        assert record["ok"] is False and record["error"]


def sample_runs():
    def claude(out, cost):
        return {
            "parsed": True,
            "is_error": False,
            "cost_usd_estimate": cost,
            "tokens": {"input_tokens": 100, "output_tokens": out, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 500},
        }

    return [
        {
            "task": "pagination", "mode": "solo", "repeat": 1, "ok": True, "error": None, "seconds": 60.0,
            "commit_made": True, "hidden": {"passed": False, "tests_run": 10, "failed": 2}, "claude": claude(1000, 0.02),
        },
        {
            "task": "pagination", "mode": "team", "repeat": 1, "ok": True, "error": None, "seconds": 240.0,
            "commit_made": True, "hidden": {"passed": True, "tests_run": 10, "failed": 0}, "claude": claude(1500, 0.04),
            "codex": {
                "codex_call_count": 3, "codex_call_seconds": 90.0, "claude_review_call_count": 1, "tokens": {"total_tokens": 7000},
                "weekly_before": 10.0, "weekly_after": 11.0, "weekly_delta": 1.0,
            },
            "review_summary": {
                "review_count": 3, "changes_requested": 1, "findings_total": 2, "final_verdicts": {"claude": "APPROVE", "codex": "APPROVE"},
            },
        },
        {
            "task": "dst_schedule", "mode": "solo", "repeat": 1, "ok": False, "error": "timed out", "timed_out": True, "seconds": 1800.0,
            "commit_made": False, "hidden": {"passed": False, "tests_run": 13, "failed": 13}, "claude": {"parsed": False},
        },
    ]


class TestReport:
    def test_summarize_totals(self):
        totals = bench.summarize(sample_runs())
        solo, team = totals["solo"], totals["team"]
        assert solo["runs"] == 2 and solo["failed"] == 1 and solo["commits"] == 1 and solo["hidden_passed"] == 0
        assert solo["seconds_total"] == 1860.0 and solo["seconds_mean"] == 930.0
        assert solo["claude_tokens"]["output_tokens"] == 1000
        assert solo["cost_usd_estimate"] == 0.02
        assert team["hidden_passed"] == 1 and team["codex_calls"] == 3 and team["codex_tokens_total"] == 7000
        assert team["codex_weekly_delta"] == 1.0 and team["review_findings"] == 2 and team["changes_requested"] == 1

    def test_paired_only_for_tasks_run_in_both_modes(self):
        pairs = bench.paired(sample_runs())
        assert pairs == [{"task": "pagination", "solo_seconds": 60.0, "team_seconds": 240.0, "ratio": 4.0}]

    def test_report_has_the_sections_and_the_numbers(self):
        data = {
            "meta": {
                "started_at": "2026-09-30T10:00:00+00:00", "claude_model": "claude-haiku-4-5-20251001", "claude_effort": "low",
                "codex_model": "gpt-6-luna", "codex_effort": "low", "tasks": ["pagination", "dst_schedule"],
                "modes": ["solo", "team"], "repeat": 1, "timeout_s": 1800,
            },
            "runs": sample_runs(),
        }
        report = bench.render_report(data)
        for heading in ("## Method", "## Setup", "## Per-run results", "### Usage per run", "## Totals", "### Time, team against solo", "## Caveats"):
            assert heading in report
        assert "gpt-6-luna" in report and "4.00x" in report
        assert "FAILED: timed out" in report
        assert "API-equivalent" in report
        assert "not statistically significant" in report
        assert "10.0% → 11.0%" in report
        assert "4m00s" in report and "30m00s" in report
        assert "n/a" in report

    def test_report_without_runs(self):
        report = bench.render_report({"meta": {}, "runs": []})
        assert "No runs recorded." in report

    def test_report_never_mentions_private_details(self):
        report = bench.render_report({"meta": {}, "runs": sample_runs()}).lower()
        for word in ("macos", "touch id", "apple", "signing", "gpg"):
            assert word not in report


class TestMain:
    def test_list(self, capsys):
        assert bench.main(["list"]) == 0
        assert "pagination" in capsys.readouterr().out.split()

    def test_refuses_when_an_api_key_is_set(self, tmp_path, capsys):
        code = bench.main(
            ["run", "--claude-model", "m", "--mode", "solo", "--out", str(tmp_path / "r.json")],
            environ={"ANTHROPIC_API_KEY": "sk-not-real", "PATH": "/bin"},
        )
        assert code == bench.EXIT_PREFLIGHT
        assert "API keys" in capsys.readouterr().err
        assert not (tmp_path / "r.json").exists()

    def test_team_needs_codex_settings(self, tmp_path, capsys):
        code = bench.main(["run", "--claude-model", "m", "--mode", "team", "--out", str(tmp_path / "r.json")], environ={"PATH": "/bin"})
        assert code == bench.EXIT_USAGE
        assert "--codex-model" in capsys.readouterr().err

    def test_unknown_task(self, tmp_path, capsys):
        code = bench.main(
            ["run", "--claude-model", "m", "--mode", "solo", "--tasks", "nope", "--out", str(tmp_path / "r.json")],
            environ={"PATH": "/bin"},
        )
        assert code == bench.EXIT_USAGE

    def test_run_then_report_end_to_end(self, fakes, tmp_path, capsys):
        out = tmp_path / "results.json"
        code = bench.main(
            [
                "run", "--mode", "both", "--tasks", "pagination,bill_split", "--claude-model", "claude-haiku-4-5-20251001",
                "--claude-effort", "low", "--codex-model", "gpt-6-luna", "--codex-effort", "low", "--repeat", "2",
                "--out", str(out), "--timeout", "60", "--claude-bin", str(fakes["claude"]), "--codex-bin", str(fakes["codex"]),
                "--wf-bin", str(fakes["wf"]), "--workdir", str(tmp_path / "work"),
            ],
            environ=fakes["env"],
        )
        assert code == 0
        data = json.loads(out.read_text())
        assert data["meta"]["tasks"] == ["pagination", "bill_split"]
        assert data["meta"]["codex_model"] == "gpt-6-luna"
        assert len(data["runs"]) == 8
        assert {(r["task"], r["mode"], r["repeat"]) for r in data["runs"]} == {
            (t, m, n) for t in ("pagination", "bill_split") for m in ("solo", "team") for n in (1, 2)
        }
        assert all(r["ok"] and r["hidden"]["passed"] and r["commit_made"] for r in data["runs"])
        team = [r for r in data["runs"] if r["mode"] == "team"]
        assert all(r["codex"]["weekly_delta"] == 2.5 for r in team[:1])
        orders = [(r["repeat"], r["mode"]) for r in data["runs"] if r["task"] == "pagination"]
        assert orders == [(1, "solo"), (1, "team"), (2, "team"), (2, "solo")]
        capsys.readouterr()
        assert bench.main(["report", str(out)]) == 0
        report = capsys.readouterr().out
        assert "| pagination | team | 2 |" in report
        assert "## Totals" in report

    def test_report_on_a_missing_file(self, tmp_path, capsys):
        assert bench.main(["report", str(tmp_path / "nope.json")]) == bench.EXIT_USAGE
