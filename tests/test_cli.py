"""CLI tests: `main([...])` against a throwaway repo, the fake claude/codex binaries and an injected home."""

import json
import subprocess
import sys
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

from workforce import cli, render_text
from workforce import config as config_mod
from workforce.events import EventLog
from workforce.paths import Paths
from workforce.state import StateStore

FAKES = Path(__file__).parent / "fakes"
FAKE_CLAUDE = FAKES / "fake_claude.py"
FAKE_CODEX = FAKES / "fake_codex.py"
SCENARIOS = Path(__file__).parent / "scenarios"
API_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
REPO_ROOT = Path(__file__).resolve().parent.parent
LOW_CODEX_LIMITS = {
    "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1790676000},
    "secondary": {"usedPercent": 4, "windowDurationMins": 10080, "resetsAt": 1791265456},
    "planType": "business",
}


def fake_toml() -> str:
    text = config_mod.default_toml()
    text = text.replace('claude = "~/.local/bin/claude"', f'claude = "{FAKE_CLAUDE}"')
    text = text.replace('codex = "/opt/homebrew/bin/codex"', f'codex = "{FAKE_CODEX}"')
    text = text.replace("parallel = 2", "parallel = 1")
    return text.replace('backend = "laya"', 'backend = "off"')


class Env:
    """One repo, one home, one scripted `input`, and a `wf(...)` that runs main() in that repo."""

    def __init__(self, tmp_path, repo, monkeypatch, capsys):
        for var in API_VARS:
            monkeypatch.delenv(var, raising=False)
        self.tmp_path = tmp_path
        self.repo = repo
        self.home = tmp_path / "home"
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.scenario_file = tmp_path / "scenario.json"
        self.clock = None
        self.inputs: list = []
        self.branch_inputs: list[str] = []
        self.prompts: list[str] = []
        monkeypatch.chdir(repo)
        monkeypatch.setattr("builtins.input", self._input)
        (repo / "workforce.toml").write_text(fake_toml())

    def set_parallel(self, count: int) -> None:
        path = self.repo / "workforce.toml"
        path.write_text(path.read_text().replace("parallel = 1", f"parallel = {count}"))

    def scenario(self, name: str) -> None:
        payload = json.loads((SCENARIOS / f"{name}.json").read_text())
        payload["codex_limits"] = LOW_CODEX_LIMITS
        self.scenario_file.write_text(json.dumps(payload))
        self.monkeypatch.setenv("WF_FAKE_SCENARIO", str(self.scenario_file))

    def raw_scenario(self, payload: dict) -> None:
        self.scenario_file.write_text(json.dumps(payload))
        self.monkeypatch.setenv("WF_FAKE_SCENARIO", str(self.scenario_file))

    def _input(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if prompt.startswith("branch for "):
            return self.branch_inputs.pop(0) if self.branch_inputs else ""
        if not self.inputs:
            raise EOFError("test ran out of scripted input")
        item = self.inputs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def wf(self, *argv: str) -> tuple[int, str]:
        self.capsys.readouterr()
        code = cli.main(list(argv), home=self.home, clock=self.clock)
        return code, self.capsys.readouterr().out

    @property
    def paths(self) -> Paths:
        return Paths(self.repo, home=self.home)

    def run(self):
        return StateStore(self.paths).load()

    def calls(self) -> list[dict]:
        path = Path(str(self.scenario_file) + ".calls.jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def env(tmp_path, git_repo, monkeypatch, capsys):
    return Env(tmp_path, git_repo, monkeypatch, capsys)


def integration_files(env) -> set[str]:
    """The files on the run's integration branch; a run never merges into the user's checkout."""
    branch = f"wf/{env.run().slug}"
    proc = subprocess.run(
        ["git", "-C", str(env.repo), "ls-tree", "-r", "--name-only", branch], capture_output=True, text=True, check=True
    )
    return set(proc.stdout.split())


def stop_at_model_pick(env) -> None:
    env.inputs = [KeyboardInterrupt()]
    code, out = env.wf("run", "add the files")
    assert code == 130
    assert "Interrupted" in out and "workforce run --resume" in out
    assert env.run().status == "awaiting_models"


@pytest.fixture
def initialised(env):
    env.scenario("happy_path")
    code, _ = env.wf("init")
    assert code == 0
    return env


def test_init_prints_the_checklist_and_creates_the_layout(env):
    env.scenario("happy_path")
    code, out = env.wf("init")
    assert code == 0, out
    assert "✓ claude 2.1.284 (claude.ai login, no API key)" in out
    assert "✓ codex 0.158.0 (ChatGPT login, no API key)" in out
    assert "✓ workforce.toml already exists, kept as it is" in out
    assert (env.repo / ".workforce" / "runs").is_dir()
    assert not (env.home / ".workforce").exists()


def test_init_writes_the_default_config_when_missing(env, monkeypatch):
    env.scenario("happy_path")
    (env.repo / "workforce.toml").unlink()
    monkeypatch.setattr(config_mod, "_DEFAULT_TOML", fake_toml())
    code, out = env.wf("init")
    assert code == 0, out
    assert "✓ wrote workforce.toml" in out
    assert config_mod.load(env.repo).role("coder").model == "claude-sonnet-5-5"


def test_init_leaves_the_tree_clean_for_the_preflight_that_run_does(initialised):
    status = subprocess.run(
        ["git", "-C", str(initialised.repo), "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == ""


def test_init_outside_a_repo_exits_3_and_writes_nothing(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    code = cli.main(["init"], home=tmp_path / "home")
    out = capsys.readouterr().out
    assert code == 3
    assert "not inside a git repo or a workspace" in out
    assert list(plain.iterdir()) == []


def test_init_refuses_when_an_api_key_is_set(env, monkeypatch):
    env.scenario("happy_path")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    code, out = env.wf("init")
    assert code == 3
    assert "✗" in out and "OPENAI_API_KEY" in out
    assert "preflight failed" in out


def test_init_reports_a_claude_login_that_is_not_claude_ai(env):
    env.raw_scenario({"claude": [], "claude_auth_status": {"authMethod": "api_key"}})
    code, out = env.wf("init")
    assert code == 3
    assert "✗ claude:" in out and "api_key" in out


def test_init_check_models_probes_every_role_once_per_model(env):
    env.raw_scenario({"claude": [{"text": "OK"}] * 3, "codex": [{"text": "OK"}]})
    code, out = env.wf("init", "--check-models")
    assert code == 0, out
    assert "✓ planner: codex gpt-6-sol · high" in out
    assert "✓ coder: claude claude-sonnet-5-5 · high" in out
    assert "✓ usage_summary: claude claude-haiku-4-5-20251001 · low" in out
    assert len(env.calls()) == 4


def test_init_check_models_marks_an_unavailable_model_and_exits_3(env):
    env.raw_scenario({"claude": [{"text": "OK"}, {"error": "model_unavailable"}, {"text": "OK"}], "codex": [{"text": "OK"}]})
    code, out = env.wf("init", "--check-models")
    assert code == 3
    assert "✗ coder: claude claude-sonnet-5-5 · high (not available on this subscription)" in out
    assert "✓ plan_reviewer: claude claude-opus-5-5 · high" in out


def test_parse_model_choice():
    assert cli.parse_model_choice("", "m", "high", "claude") == ("m", "high")
    assert cli.parse_model_choice("claude-opus-5-5", "m", "high", "claude") == ("claude-opus-5-5", "high")
    assert cli.parse_model_choice("x max", "m", "high", "claude") == ("x", "max")
    with pytest.raises(ValueError):
        cli.parse_model_choice("x ultra", "m", "high", "claude")
    with pytest.raises(ValueError):
        cli.parse_model_choice("a b c", "m", "high", "claude")
    assert cli.parse_model_choice("x ultra", "m", "high", "codex") == ("x", "ultra")


def test_full_run_reaches_done_with_the_users_model_choices(initialised):
    env = initialised
    env.inputs = ["", "claude-opus-5-5 foo", "claude-opus-5-5 max"]
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    assert env.run().status == "done"
    assert [t.status for t in env.run().tasks] == ["merged", "merged"]
    assert env.prompts[0] == "T1  Add a.txt   coder [claude-sonnet-5-5 · high]: "
    assert "effort 'foo' is not valid for claude" in out
    assert "[T1]" in out and "merged" in out
    assert "review  opus-5-5 → APPROVE" in out
    assert "✓ run done: 2 tasks merged" in out
    coder_calls = [c for c in env.calls() if c["cli"] == "claude" and "# Role: coder" in c["prompt"]]
    picked = [(c["argv"][c["argv"].index("--model") + 1], c["argv"][c["argv"].index("--effort") + 1]) for c in coder_calls]
    assert picked == [("claude-sonnet-5-5", "high"), ("claude-opus-5-5", "max")]
    assert {"a.txt", "b.txt"} <= integration_files(env)
    assert not (env.repo / "a.txt").exists() and not (env.repo / "b.txt").exists()


def test_run_without_a_goal_is_a_usage_error(initialised):
    code, out = initialised.wf("run")
    assert code == 2
    assert "give a goal" in out


def test_run_refuses_a_second_run_while_one_is_active(initialised):
    env = initialised
    stop_at_model_pick(env)
    code, out = env.wf("run", "another goal")
    assert code == 1
    assert "awaiting_models" in out


def test_run_answers_a_question_inline_and_finishes(env):
    env.scenario("blocked")
    assert env.wf("init")[0] == 0
    env.inputs = ["", "USER-CHOICE-ALPHA keep a.txt"]
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    assert "❓ Q1 (T1)" in out
    assert "claude:" in out and "codex:" in out and "BLOCKED" in out
    assert env.run().status == "done"
    assert "USER-CHOICE-ALPHA keep a.txt" in env.paths.decisions_md.read_text()


def test_question_left_open_then_answer_then_resume_completes(env):
    env.scenario("blocked")
    assert env.wf("init")[0] == 0
    env.inputs = ["", KeyboardInterrupt()]
    code, out = env.wf("run", "add the files")
    assert code == 0
    assert "Q1 is still open" in out and 'workforce answer Q1 "<text>"' in out
    assert env.run().status == "awaiting_user"

    code, out = env.wf("status")
    assert code == 0
    assert "awaiting_user" in out and "❓ Q1" in out

    code, out = env.wf("questions")
    assert code == 0 and "Q1" in out and "BLOCKED" in out

    code, out = env.wf("answer", "Q1", "USER-CHOICE-ALPHA keep a.txt")
    assert code == 0, out
    assert "answered Q1" in out and "workforce run --resume" in out
    decisions = env.paths.decisions_md.read_text()
    assert "Q1" in decisions and "USER-CHOICE-ALPHA keep a.txt" in decisions
    assert env.run().question("Q1").status == "answered"
    assert env.wf("questions")[1].strip() == "No open questions."

    code, out = env.wf("run", "--resume")
    assert code == 0, out
    assert env.run().status == "done"
    assert "✓ run done" in out


def test_answer_for_an_unknown_question_fails_cleanly(initialised):
    env = initialised
    stop_at_model_pick(env)
    code, out = env.wf("answer", "Q9", "whatever")
    assert code == 1
    assert "Q9" in out
    assert "Traceback" not in out


def test_answer_with_end_of_input_leaves_the_question_open(env):
    env.scenario("blocked")
    assert env.wf("init")[0] == 0
    env.inputs = ["", EOFError()]
    code, out = env.wf("run", "add the files")
    assert code == 0
    assert env.run().question("Q1").status == "open"


def test_status_and_log_show_the_expected_lines(initialised):
    env = initialised
    assert "No run yet" in env.wf("status")[1]
    env.inputs = ["", ""]
    assert env.wf("run", "add the files")[0] == 0

    code, out = env.wf("status")
    assert code == 0
    assert "· done ·" in out and "Goal: add the files" in out
    assert "Add a.txt" in out and "merged" in out and "sonnet-5-5 · high" in out

    code, out = env.wf("log")
    assert code == 0
    assert "[run]" in out and "started" in out
    assert "[plan]" in out and "plan.md: 2 tasks" in out
    assert "review  opus-5-5 → APPROVE" in out
    assert "commit  👆 committing… (confirm if your git asks)" in out

    code, out = env.wf("log", "T2")
    assert code == 0
    assert "[T2]" in out and "[T1]" not in out and "[plan]" not in out
    assert "No events for T9." in env.wf("log", "T9")[1]


def test_no_subcommand_opens_the_console(initialised, monkeypatch):
    env = initialised
    seen = {}

    def fake_run_console(repo, **kwargs):
        seen["repo"] = Path(repo)
        seen["kwargs"] = kwargs
        return 7

    monkeypatch.setattr("workforce.console.app.run_console", fake_run_console)
    env.clock = FakeClock(1.0)
    code, out = env.wf()
    assert code == 7
    assert seen["repo"].resolve() == env.repo.resolve()
    assert seen["kwargs"]["home"] == env.home
    assert seen["kwargs"]["clock"] is env.clock
    assert "No run yet" not in out


def test_no_subcommand_outside_a_repo_exits_3(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    called = []
    monkeypatch.setattr("workforce.console.app.run_console", lambda *a, **k: called.append(1) or 0)
    assert cli.main([], home=tmp_path / "home") == 3
    assert called == []


def test_usage_without_readings_and_with_readings(initialised):
    env = initialised
    code, out = env.wf("usage")
    assert code == 0 and out.strip() == "no usage readings yet"
    env.paths.usage_json.write_text(
        json.dumps(
            {
                "thresholds": {"alert_percent": 50, "pause_percent": 60},
                "sources": {"claude": {"freshness": "fresh", "read_at": 1800000000, "error": None},
                            "codex": {"freshness": "unknown", "read_at": None, "error": None}},
                "windows": [
                    {"source": "claude", "name": "five_hour", "percent": 51.0, "resets_at": 1800003600,
                     "read_at": 1800000000, "freshness": "fresh", "expired": False}
                ],
            }
        )
    )
    env.paths.usage_md.write_text("Claude is at 51% of the five hour window.\n")
    code, out = env.wf("usage")
    assert code == 0
    assert "five_hour" in out and "51%" in out and "fresh" in out
    assert "codex" in out and "unknown" in out
    assert "alert at 50% · pause at 60%" in out
    assert "Claude is at 51% of the five hour window." in out


def test_models_lists_roles_and_set_changes_only_that_role(initialised):
    env = initialised
    before = (env.repo / "workforce.toml").read_text()
    code, out = env.wf("models")
    assert code == 0
    assert "planner" in out and "gpt-6-sol" in out and "claude-haiku-4-5-20251001" in out

    code, out = env.wf("models", "set", "coder", "claude-opus-5-5", "max")
    assert code == 0, out
    assert "coder: claude claude-opus-5-5 · max" in out
    after = (env.repo / "workforce.toml").read_text()
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert changed == [
        ('model = "claude-sonnet-5-5"', 'model = "claude-opus-5-5"'),
        ('effort = "high"', 'effort = "max"'),
    ]
    assert len(before.splitlines()) == len(after.splitlines())
    config = config_mod.load(env.repo)
    assert config.role("coder").model == "claude-opus-5-5" and config.role("coder").effort == "max"
    assert config.role("committer").model == "claude-sonnet-5-5"
    assert config.role("reviewer_claude").model == "claude-opus-5-5" and config.role("reviewer_claude").effort == "high"

    code, out = env.wf("models", "set", "coder", "claude-sonnet-5-5")
    assert code == 0
    assert config_mod.load(env.repo).role("coder").effort == "max"


def test_models_set_rejects_bad_input(initialised):
    env = initialised
    before = (env.repo / "workforce.toml").read_text()
    assert env.wf("models", "set", "nobody", "m")[0] == 2
    code, out = env.wf("models", "set", "coder", "m", "ultra")
    assert code == 3 and "ultra" in out
    assert (env.repo / "workforce.toml").read_text() == before


def test_pause_and_resume_around_the_model_pick(initialised):
    env = initialised
    stop_at_model_pick(env)

    code, out = env.wf("pause")
    assert code == 0 and "Paused" in out
    run = env.run()
    assert run.status == "paused" and run.pause_reason == "paused by the user"
    assert "Paused: paused by the user" in env.wf("status")[1]

    env.inputs = ["", ""]
    code, out = env.wf("resume")
    assert code == 0, out
    assert env.run().status == "done"


def test_resume_when_the_run_is_done_says_so(initialised):
    env = initialised
    env.inputs = ["", ""]
    env.wf("run", "add the files")
    code, out = env.wf("resume")
    assert code == 0 and "already done" in out
    assert env.wf("run", "--resume")[0] == 0


def test_a_model_unavailable_pause_prints_the_reason_and_exits_0(env):
    env.scenario("model_unavailable")
    assert env.wf("init")[0] == 0
    env.inputs = [""]
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    assert "Paused: model_unavailable: claude-sonnet-5-5" in out
    assert env.run().status == "paused"


class StubMonitor:
    def __init__(self, can_resume):
        self._can = can_resume
        self.cleared = False

    def can_resume(self):
        return self._can

    def clear_pause(self):
        self.cleared = True

    def readings(self):
        window = {"source": "claude", "name": "five_hour", "percent": 61.0, "freshness": "fresh",
                  "read_at": 1_790_670_000, "resets_at": 1_790_676_000}
        return {"sources": {"claude": {"freshness": "fresh", "read_at": 1.0, "error": None}}, "windows": [window]}

    def resume_plan(self):
        return SimpleNamespace(auto=False, at=None, blocking_windows=())

    def gate(self):
        return None

    def refresh_codex(self):
        return True

    def ingest_claude(self, result):
        return False

    def tick(self, paused=None, pause_reason=None):
        return "none"


def test_resume_of_a_usage_pause_needs_the_monitor_to_agree(initialised, monkeypatch):
    env = initialised
    stop_at_model_pick(env)
    from workforce.orchestrator import Orchestrator

    orch = Orchestrator(env.repo, config_mod.load(env.repo), {}, cli.build_decider(config_mod.load(env.repo).decider, EventLog(env.paths)), home=env.home)
    orch.pause("usage: claude five_hour 61% ≥ 60%")

    blocked = StubMonitor(False)
    monkeypatch.setattr(cli, "build_monitor", lambda ctx, config, runners, events: blocked)
    code, out = env.wf("resume")
    assert code == 0
    assert "Traceback" not in out
    assert "five_hour" in out and "61%" in out
    assert "still above 60%; try again after " in out
    assert env.run().status == "paused" and not blocked.cleared
    code, out = env.wf("run", "--resume")
    assert code == 0 and "still above 60%; try again after " in out

    allowed = StubMonitor(True)
    monkeypatch.setattr(cli, "build_monitor", lambda ctx, config, runners, events: allowed)
    env.inputs = ["", ""]
    code, out = env.wf("resume")
    assert code == 0, out
    assert allowed.cleared and env.run().status == "done"


def test_missing_config_exits_3_with_a_message_and_no_traceback(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    code = cli.main(["models"], home=tmp_path / "home")
    out = capsys.readouterr().out
    assert code == 3
    assert "workforce init" in out and "Traceback" not in out


def test_broken_config_exits_3_naming_the_key(env):
    (env.repo / "workforce.toml").write_text(fake_toml().replace("poll_minutes = 10\n", ""))
    code, out = env.wf("models")
    assert code == 3
    assert "poll_minutes" in out


def test_state_lock_held_by_another_process_exits_4(initialised):
    env = initialised
    with StateStore(env.paths).lock():
        code, out = env.wf("run", "add the files")
    assert code == 4
    assert "only one run can be active" in out
    assert env.run() is None


def test_argparse_errors_exit_2(env):
    assert env.wf("--bogus")[0] == 2
    assert env.wf("answer", "Q1")[0] == 2
    assert env.wf("run", "goal", "--mode", "fast")[0] == 2
    assert env.wf("--help")[0] == 0


def test_the_event_formatter_covers_every_event_kind():
    from workforce.events import KINDS

    formatter = render_text.EventFormatter()
    samples = {
        "run_started": {"run": "r1", "goal": "g", "mode": "auto"},
        "plan_ready": {"tasks": 3},
        "debate_round": {"round": 1, "speaker": "plan_reviewer", "agree": False, "objections": ["a"]},
        "question": {"question": "Q1", "task": "T1", "text": "why"},
        "answered": {"question": "Q1", "answer": "because"},
        "task_status": {"task": "T1", "status": "coding"},
        "agent_event": {"phase": "start", "role": "coder", "task": "T3", "model": "claude-sonnet-5-5", "step": "code"},
        "check_result": {"task": "T1", "ok": False, "no_checks": False},
        "review": {"task": "T1", "model": "claude-opus-5-5", "verdict": "APPROVE", "sha": "3f9c2a1abcdef", "findings": 0},
        "commit_waiting": {"task": "T1", "message": "👆 committing… (confirm if your git asks)"},
        "committed": {"task": "T1", "sha": "abcdef123456"},
        "merged": {"task": "T1", "sha": "abcdef123456", "branch": "wf/r/T1"},
        "alert": {"message": "claude five_hour window at 51%"},
        "paused": {"reason": "usage: x"},
        "resumed": {"status": "running"},
        "decision": {"key": "needs_human", "answer": "user", "confidence": 0.9, "source": "laya"},
        "note": {"text": "hello"},
        "error": {"task": "T1", "message": "boom"},
        "run_done": {"tasks": 2, "summary": ["T1: a (abc)", "T2: b (def)"]},
    }
    assert set(samples) == set(KINDS)
    for kind, data in samples.items():
        lines = formatter.format({"kind": kind, "ts": "2026-09-29T10:00:00.000+00:00", **data})
        assert all(isinstance(line, str) and line for line in lines), kind
        if kind != "task_status":
            assert lines, kind
    assert formatter.format({"kind": "task_status", "task": "T1", "status": "coding"}) == []
    assert formatter.format({"kind": "task_status", "task": "T1", "status": "testing"}) == ["[T1]    →       testing"]
    review = formatter.format({"kind": "review", **samples["review"]})[0]
    assert review == "[T1]    review  opus-5-5 → APPROVE @ 3f9c2a1 (0 findings)"


def test_the_formatter_shows_elapsed_time_on_agent_end():
    formatter = render_text.EventFormatter()
    start = {"kind": "agent_event", "phase": "start", "role": "coder", "task": "T3", "model": "claude-sonnet-5-5",
             "step": "code", "ts": "2026-09-29T10:00:00.000+00:00"}
    end = {**start, "phase": "end", "ok": True, "ts": "2026-09-29T10:03:02.000+00:00"}
    formatter.format(start)
    assert "3m02s" in formatter.format(end)[0]
    bad = {**end, "ok": False, "error_kind": "crash"}
    assert "✗ failed (crash)" in formatter.format(bad)[0]


def test_installed_entry_point_prints_help():
    binary = Path(sys.executable).parent / "workforce"
    assert binary.exists()
    proc = subprocess.run([str(binary), "--help"], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0
    assert "usage: workforce" in proc.stdout and "init" in proc.stdout and "resume" in proc.stdout


def test_wf_entry_point_delegates_the_old_subcommands():
    """`wf --help` (no subcommand) goes to Claude Code by design; the old pipeline is `wf run|status|…`."""
    binary = Path(sys.executable).parent / "wf"
    assert binary.exists()
    proc = subprocess.run([str(binary), "run", "--help"], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0
    assert "usage: workforce run" in proc.stdout


def orch_stub(monitor=True, poll_minutes=10):
    class Limits:
        pass

    Limits.poll_minutes = poll_minutes
    emitted = []

    class Orch:
        usage_monitor = object() if monitor else None
        calls = []

        class config:
            limits = Limits

        class events:
            @staticmethod
            def emit(kind, **data):
                emitted.append((kind, data))

        def poll_tick(self):
            self.calls.append("poll")
            return "none"

        def progress_tick(self):
            self.calls.append("progress")
            return []

    Orch.emitted = emitted
    return Orch()


def test_the_ticker_runs_both_timers_and_stops(monkeypatch):
    orch = orch_stub()
    ticker = cli.Ticker(orch, progress_interval_s=0.05)
    ticker.poll_interval_s = 0.05
    with ticker:
        deadline = threading.Event()
        while {"poll", "progress"} - set(orch.calls) and not deadline.wait(0.05):
            pass
    assert {"poll", "progress"} <= set(orch.calls)
    count = len(orch.calls)
    threading.Event().wait(0.2)
    assert len(orch.calls) == count


def test_the_ticker_skips_usage_polling_without_a_monitor_and_reports_tick_failures():
    orch = orch_stub(monitor=False)

    def broken():
        raise RuntimeError("tick blew up")

    orch.progress_tick = broken
    ticker = cli.Ticker(orch, progress_interval_s=0.05)
    with ticker:
        deadline = threading.Event()
        while not orch.emitted and not deadline.wait(0.05):
            pass
    assert len(ticker._threads) == 0
    assert orch.calls == []
    assert orch.emitted[0] == ("error", {"source": "progress_tick", "message": "tick blew up"})


def test_the_ticker_default_progress_interval_is_three_minutes():
    assert cli.PROGRESS_INTERVAL_S == 180
    assert cli.Ticker(orch_stub()).progress_interval_s == 180
    assert cli.Ticker(orch_stub(poll_minutes=7)).poll_interval_s == 420


class FakeClock:
    def __init__(self, now):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Pinger:
    """In-process Claude runner for the usage monitor: quota readings depend on the fake clock."""

    def __init__(self, clock, low_after):
        self.clock = clock
        self.low_after = low_after
        self.requests = []

    def run(self, request, on_event=None):
        from workforce.agents.base import AgentResult

        self.requests.append(request)
        used = 0.10 if self.clock.time() >= self.low_after else 0.75
        limits = {
            "five_hour": {"utilization": used, "resetsAt": 1_790_694_000},
            "seven_day": {"utilization": 0.10, "resetsAt": 1_791_440_000},
        }
        return AgentResult(
            ok=True, text="Usage looks fine.", structured=None, session_id="ping", model=request.model,
            usage={}, rate_limits=limits, error_kind=None, error=None,
        )


@pytest.fixture
def usage_env(env, monkeypatch):
    env.clock = FakeClock(1_790_670_000.0)
    monitors = []

    def build_monitor(ctx, config, runners, events):
        from workforce.usage.monitor import UsageMonitor

        monitor = UsageMonitor(
            ctx.paths, config, str(FAKE_CODEX), Pinger(ctx.clock, low_after=1_790_676_060.0), events, ctx.clock
        )
        monitors.append(monitor)
        return monitor

    monkeypatch.setattr(cli, "build_monitor", build_monitor)
    env.monitors = monitors
    return env


def init_with(env, scenario_name):
    env.scenario(scenario_name)
    assert env.wf("init")[0] == 0


def test_a_five_hour_usage_pause_waits_in_the_foreground_and_resumes_by_itself(usage_env):
    env = usage_env
    init_with(env, "monitor_five_hour_pause")
    env.inputs = [""]
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    assert "⏸ Paused: usage: claude five_hour 61% ≥ 60%. Resumes automatically at " in out
    assert "if all limits are under 60%. Ctrl-C to stop waiting (the run stays paused)." in out
    assert "(in 1h41m)" in out
    assert "▶ Resumed" in out
    waiting = out.index("⏸ Paused")
    assert waiting < out.index("▶ Resumed", waiting) < out.index("run done")
    assert env.run().status == "done"
    assert env.clock.sleeps and max(env.clock.sleeps) <= 600
    assert sum(env.clock.sleeps) >= 1_790_676_060.0 - 1_790_670_000.0
    assert env.monitors[0].pause_reason is None
    assert env.monitors[0]._pause_state is None
    assert "Reply OK" in [r.prompt for r in env.monitors[0].claude_runner.requests]


def test_the_wait_survives_a_first_poll_where_usage_is_still_high(usage_env):
    env = usage_env
    init_with(env, "monitor_five_hour_pause")
    env.inputs = [""]
    pinger_holder = {}
    original = cli.build_monitor

    def build(ctx, config, runners, events):
        monitor = original(ctx, config, runners, events)
        monitor.claude_runner.low_after = 1_790_676_060.0 + 1_800
        pinger_holder["monitor"] = monitor
        return monitor

    env.monkeypatch.setattr(cli, "build_monitor", build)
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    assert env.run().status == "done"
    assert env.clock.now >= 1_790_676_060.0 + 1_800
    assert out.count("⏸ Paused:") == 1


def test_ctrl_c_while_waiting_leaves_the_run_paused_and_exits_0(usage_env):
    env = usage_env
    init_with(env, "monitor_five_hour_pause")
    env.inputs = [""]

    def interrupted(seconds):
        raise KeyboardInterrupt

    env.clock.sleep = interrupted
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    assert "Stopped waiting. The run stays paused" in out and "workforce resume" in out
    run = env.run()
    assert run.status == "paused" and run.pause_reason.startswith("usage:")


def test_run_resume_from_a_new_process_waits_and_finishes(usage_env):
    env = usage_env
    init_with(env, "monitor_five_hour_pause")
    env.inputs = [""]
    original_sleep = env.clock.sleep

    def interrupted(seconds):
        raise KeyboardInterrupt

    env.clock.sleep = interrupted
    assert env.wf("run", "add the files")[0] == 0
    assert env.run().status == "paused"

    env.clock.sleep = original_sleep
    code, out = env.wf("run", "--resume")
    assert code == 0, out
    assert "Resumes automatically at" in out and "▶ Resumed" in out
    assert env.run().status == "done"

    env2 = env
    env2.clock.now = 1_790_670_000.0
    assert env2.wf("resume")[1].strip().endswith("is already done.")


def test_resume_command_also_waits_for_a_five_hour_pause(usage_env):
    env = usage_env
    init_with(env, "monitor_five_hour_pause")
    env.inputs = [""]
    original_sleep = env.clock.sleep

    def interrupted(seconds):
        raise KeyboardInterrupt

    env.clock.sleep = interrupted
    env.wf("run", "add the files")
    env.clock.sleep = original_sleep
    code, out = env.wf("resume")
    assert code == 0, out
    assert "Resumes automatically at" in out and env.run().status == "done"


def test_a_weekly_usage_pause_prints_the_reason_and_exits_without_waiting(usage_env):
    env = usage_env
    init_with(env, "monitor_weekly_pause")
    env.inputs = [""]
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    assert "⏸ Paused: usage: claude seven_day 61% ≥ 60%" in out
    assert "Resumes automatically" not in out
    assert "workforce resume" in out
    assert env.clock.sleeps == []
    assert env.run().status == "paused"


def test_a_model_unavailable_pause_hints_at_models_set(env):
    env.scenario("model_unavailable")
    assert env.wf("init")[0] == 0
    env.inputs = [""]
    code, out = env.wf("run", "add the files")
    assert code == 0
    assert "workforce models set <role> <model> [effort]" in out


def test_the_model_pick_shows_laya_size_hints_only_when_there_are_some(initialised, monkeypatch):
    env = initialised
    monkeypatch.setattr(
        "workforce.orchestrator.Orchestrator.task_size_hints", lambda self: {"T1": "small", "T2": None}
    )
    env.inputs = ["", ""]
    assert env.wf("run", "add the files")[0] == 0
    assert env.prompts[0] == "T1  Add a.txt   (Laya: small)   coder [claude-sonnet-5-5 · high]: "
    assert env.prompts[1] == "T2  Add b.txt   coder [claude-sonnet-5-5 · high]: "


def test_runner_lock_is_held_for_the_whole_drive_loop(initialised, monkeypatch, capsys):
    env = initialised
    stop_at_model_pick(env)
    entered = threading.Event()
    release = threading.Event()

    def blocking_input(prompt=""):
        entered.set()
        release.wait(60)
        return ""

    monkeypatch.setattr("builtins.input", blocking_input)
    result = {}

    def first():
        result["code"] = cli.main(["run", "--resume"], home=env.home)

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(60)
        capsys.readouterr()
        assert cli.main(["run", "--resume"], home=env.home) == 4
        assert "only one run can be active" in capsys.readouterr().out
        assert cli.main(["resume"], home=env.home) == 4
        assert cli.main(["run", "another goal"], home=env.home) == 4
    finally:
        release.set()
        thread.join(120)
    assert result["code"] == 0
    assert env.run().status == "done"


def test_pause_does_not_need_the_runner_lock(initialised):
    env = initialised
    stop_at_model_pick(env)
    with StateStore(env.paths).lock():
        code, out = env.wf("pause")
    assert code == 0, out
    assert env.run().status == "paused"


def test_answer_from_a_second_terminal_works_while_a_run_holds_the_lock(env, monkeypatch, capsys):
    env.scenario("blocked")
    assert env.wf("init")[0] == 0
    entered = threading.Event()
    release = threading.Event()

    def blocking_input(prompt=""):
        if prompt.startswith("Q1"):
            entered.set()
            release.wait(60)
            return "should not be used"
        return ""

    monkeypatch.setattr("builtins.input", blocking_input)
    result = {}
    thread = threading.Thread(target=lambda: result.update(code=cli.main(["run", "add the files"], home=env.home)))
    thread.start()
    try:
        assert entered.wait(120)
        code, out = env.wf("answer", "Q1", "USER-CHOICE-ALPHA keep a.txt")
        assert code == 0, out
    finally:
        release.set()
        thread.join(120)
    assert result["code"] == 0
    assert env.run().question("Q1").status == "answered"


def test_blocked_risk_gate_calls_are_printed_with_the_task_in_flight(initialised, capsys):
    env = initialised
    stop_at_model_pick(env)
    store = StateStore(env.paths)
    store.update(lambda run: setattr(run.tasks[0], "status", "coding"))
    events = EventLog(env.paths)
    console = cli.render_text.make_console()
    tail = cli.EventTail(events, console)
    events.emit(
        "decision", key="risk_gate", stage="hook:denylist", agent="claude", tool="Bash",
        summary="git push --force origin main", allowed=False, reason="hard deny-list: git push with force",
    )
    events.emit("decision", key="risk_gate", stage="hook:laya_safe", agent="claude", tool="Read",
                summary="Read a.txt", allowed=True, reason="safe")
    capsys.readouterr()
    tail.drain()
    out = capsys.readouterr().out
    assert "⛔ [T1] blocked: git push --force origin main (hard deny-list: git push with force)" in out
    assert "Read a.txt" not in out


def test_a_blocked_call_names_no_task_when_several_or_none_are_in_flight(initialised, capsys):
    env = initialised
    stop_at_model_pick(env)
    events = EventLog(env.paths)
    tail = cli.EventTail(events, cli.render_text.make_console())
    events.emit("decision", key="risk_gate", agent="codex", tool="Bash", summary="rm -rf /", allowed=False, reason="deny")
    capsys.readouterr()
    tail.drain()
    assert "⛔ blocked: rm -rf / (deny)" in capsys.readouterr().out


def test_the_formatter_prints_flagged_agent_progress():
    formatter = render_text.EventFormatter()
    stuck = {"kind": "decision", "key": "agent_progress", "task": "T2", "answer": "stuck", "confidence": 0.71, "flag": True}
    assert formatter.format(stuck) == ["⚠ [T2] Laya thinks the coder may be stuck (conf 0.71). Check with: workforce log T2"]
    off = {**stuck, "answer": "off_task", "confidence": 0.9}
    assert "may be off task (conf 0.90)" in formatter.format(off)[0]


def test_wait_helpers_format_clock_and_duration():
    assert render_text.format_wait(6060) == "1h41m"
    assert render_text.format_wait(2460) == "41m"
    assert render_text.format_wait(-5) == "0m"
    assert len(render_text.format_clock(1_790_676_060)) == 5


def test_a_real_run_leaves_usage_readings_for_the_usage_command(initialised):
    env = initialised
    env.inputs = ["", ""]
    assert env.wf("run", "add the files")[0] == 0
    code, out = env.wf("usage")
    assert code == 0
    assert "codex" in out and "five_hour" in out


def fake_team_env(env, monkeypatch, plan_tasks, delays=None):
    from tests.test_scheduler import FakeTeam

    env.set_parallel(2)
    env.scenario("happy_path")
    assert env.wf("init")[0] == 0
    team = FakeTeam(env.repo, plan_tasks)
    team.delays = delays or {}
    monkeypatch.setattr(cli, "build_runners", lambda config: {"claude": team, "codex": team})
    return team


def independent_plan():
    return [
        {"id": tid, "title": f"Add {tid.lower()}", "description": f"Create {tid.lower()}.txt",
         "acceptance": ["the file exists"], "depends_on": []}
        for tid in ("T1", "T2")
    ]


def test_parallel_two_drives_two_independent_tasks_through_the_scheduler(env, monkeypatch):
    team = fake_team_env(env, monkeypatch, independent_plan(), delays={"T1": 0.4, "T2": 0.4})
    env.inputs = ["", ""]
    code, out = env.wf("run", "build things")
    assert code == 0, out
    run = env.run()
    assert run.status == "done" and [t.status for t in run.tasks] == ["merged", "merged"]
    coders = {c["task"]: c for c in team.of("coder")}
    assert coders["T1"]["start"] < coders["T2"]["end"] and coders["T2"]["start"] < coders["T1"]["end"]
    assert coders["T1"]["thread"] != coders["T2"]["thread"]
    assert {"t1.txt", "t2.txt"} <= integration_files(env)
    assert not (env.repo / "t1.txt").exists() and not (env.repo / "t2.txt").exists()
    counts = [
        subprocess.run(
            ["git", "-C", str(env.repo), "rev-list", "--count", ref], capture_output=True, text=True, check=True
        ).stdout.strip()
        for ref in ("HEAD", f"wf/{run.slug}")
    ]
    assert counts == ["1", "3"]
    assert "[T1]" in out and "[T2]" in out and "✓ run done: 2 tasks merged" in out


def test_parallel_one_keeps_the_sequential_loop(env, monkeypatch):
    calls = []
    real = cli.Scheduler
    monkeypatch.setattr(cli, "Scheduler", lambda *a, **k: calls.append(a) or real(*a, **k))
    env.scenario("happy_path")
    assert env.wf("init")[0] == 0
    env.inputs = ["", ""]
    assert env.wf("run", "add the files")[0] == 0
    assert calls == [] and env.run().status == "done"


def test_parallel_two_uses_the_scheduler_object(env, monkeypatch):
    calls = []
    real = cli.Scheduler
    monkeypatch.setattr(cli, "Scheduler", lambda orch, parallel: calls.append(parallel) or real(orch, parallel))
    env.set_parallel(2)
    env.scenario("happy_path")
    assert env.wf("init")[0] == 0
    env.inputs = ["", ""]
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    assert calls and set(calls) == {2}
    assert env.run().status == "done"
    assert "review  opus-5-5 → APPROVE" in out


def test_parallel_two_still_prompts_for_questions_inline(env):
    env.set_parallel(2)
    env.scenario("blocked")
    assert env.wf("init")[0] == 0
    env.inputs = ["", "USER-CHOICE-ALPHA keep a.txt"]
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    assert "❓ Q1 (T1)" in out and "BLOCKED" in out
    assert env.run().status == "done"
    assert "USER-CHOICE-ALPHA keep a.txt" in env.paths.decisions_md.read_text()


def test_parallel_two_still_waits_for_a_five_hour_pause_and_resumes(usage_env):
    env = usage_env
    env.set_parallel(2)
    init_with(env, "monitor_five_hour_pause")
    env.inputs = [""]
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    waiting = out.index("⏸ Paused: usage: claude five_hour 61% ≥ 60%. Resumes automatically at ")
    assert waiting < out.index("▶ Resumed", waiting) < out.index("run done")
    assert env.run().status == "done"
    assert env.monitors[0].pause_reason is None
    assert env.clock.sleeps


def test_parallel_two_a_weekly_pause_exits_without_waiting(usage_env):
    env = usage_env
    env.set_parallel(2)
    init_with(env, "monitor_weekly_pause")
    env.inputs = [""]
    code, out = env.wf("run", "add the files")
    assert code == 0, out
    assert "Resumes automatically" not in out and "workforce resume" in out
    assert env.clock.sleeps == []


def test_parallel_two_holds_the_runner_lock_while_tasks_run(env, monkeypatch, capsys):
    team = fake_team_env(env, monkeypatch, independent_plan())
    entered = threading.Event()
    release = threading.Event()

    def hold(task):
        entered.set()
        release.wait(60)

    team.on_coder_start = hold
    env.inputs = ["", ""]
    monkeypatch.setattr("builtins.input", env._input)
    result = {}
    thread = threading.Thread(target=lambda: result.update(code=cli.main(["run", "build things"], home=env.home)))
    thread.start()
    try:
        assert entered.wait(120)
        capsys.readouterr()
        assert cli.main(["run", "--resume"], home=env.home) == 4
        assert cli.main(["resume"], home=env.home) == 4
        assert "only one run can be active" in capsys.readouterr().out
    finally:
        release.set()
        thread.join(180)
    assert result["code"] == 0
    assert env.run().status == "done"


def test_the_blocked_line_prefers_the_events_task_id(initialised, capsys):
    env = initialised
    stop_at_model_pick(env)
    store = StateStore(env.paths)

    def two_in_flight(run):
        run.tasks[0].status = "coding"
        run.tasks[1].status = "coding"

    store.update(two_in_flight)
    events = EventLog(env.paths)
    tail = cli.EventTail(events, cli.render_text.make_console())
    events.emit("decision", key="risk_gate", agent="claude", tool="Bash", summary="rm -rf /", allowed=False,
                reason="deny", task_id="T2")
    events.emit("decision", key="risk_gate", agent="claude", tool="Bash", summary="git push -f", allowed=False,
                reason="deny")
    capsys.readouterr()
    tail.drain()
    out = capsys.readouterr().out
    assert "⛔ [T2] blocked: rm -rf / (deny)" in out
    assert "⛔ blocked: git push -f (deny)" in out


def test_a_bare_goal_is_the_same_as_run(initialised):
    env = initialised
    env.inputs = [KeyboardInterrupt()]
    code, out = env.wf("add the files")
    assert code == 130
    assert "Interrupted" in out
    run = env.run()
    assert run.goal == "add the files" and run.status == "awaiting_models"


def test_a_bare_goal_takes_run_options(initialised):
    env = initialised
    env.inputs = [KeyboardInterrupt()]
    code, _ = env.wf("add the files", "--mode", "step")
    assert code == 130
    assert env.run().mode == "step"


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["status"], ["status"]),
        (["run", "goal"], ["run", "goal"]),
        (["--help"], ["--help"]),
        ([], []),
        (["build a thing"], ["run", "build a thing"]),
        (["build", "a", "thing"], ["run", "build", "a", "thing"]),
        (["models", "task", "T1", "m"], ["models", "task", "T1", "m"]),
    ],
)
def test_with_default_command(argv, expected):
    assert cli.with_default_command(cli.build_parser(), argv) == expected


def test_argv_defaults_to_the_process_arguments(initialised, monkeypatch):
    env = initialised
    monkeypatch.setattr(sys, "argv", ["wf", "status"])
    monkeypatch.chdir(env.repo)
    assert cli.main(None, home=env.home) == 0


def picked_run(env):
    stop_at_model_pick(env)
    return env.run()


def test_models_task_changes_a_task_that_has_not_started(initialised):
    env = initialised
    picked_run(env)
    code, out = env.wf("models", "task", "T1", "claude-opus-5-5", "low")
    assert code == 0, out
    assert "T1: claude claude-opus-5-5 · low" in out
    task = env.run().task("T1")
    assert (task.model, task.effort, task.agent) == ("claude-opus-5-5", "low", "claude")
    assert env.run().task("T2").model is None
    code, _ = env.wf("models", "task", "t1", "claude-haiku-4-5-20251001")
    assert code == 0
    task = env.run().task("T1")
    assert (task.model, task.effort) == ("claude-haiku-4-5-20251001", "low")
    assert "will use claude claude-haiku-4-5-20251001 · low" in env.wf("log")[1]


def test_models_task_refuses_a_merged_task(initialised):
    env = initialised
    picked_run(env)

    def merge(run):
        run.task("T1").status = "merged"

    StateStore(env.paths).update(merge)
    code, out = env.wf("models", "task", "T1", "claude-opus-5-5")
    assert code == 1 and "already merged" in out
    assert env.run().task("T1").model is None


def test_models_task_refuses_a_running_task_until_the_run_is_paused(initialised):
    env = initialised
    picked_run(env)

    def start(run):
        run.status = "running"
        run.task("T1").status = "coding"

    StateStore(env.paths).update(start)
    code, out = env.wf("models", "task", "T1", "claude-opus-5-5")
    assert code == 1 and "pause the run first" in out
    code, _ = env.wf("models", "task", "T2", "claude-opus-5-5")
    assert code == 0
    StateStore(env.paths).update(lambda run: setattr(run, "status", "paused"))
    code, _ = env.wf("models", "task", "T1", "claude-opus-5-5", "medium")
    assert code == 0
    assert env.run().task("T1").effort == "medium"


def test_models_task_rejects_bad_input(initialised):
    env = initialised
    picked_run(env)
    assert env.wf("models", "task", "T9", "m")[0] == 1
    code, out = env.wf("models", "task", "T1", "m", "ultra")
    assert code == 1 and "ultra" in out
    assert env.wf("models", "task", "T1")[0] == 2
    assert env.run().task("T1").model is None


def test_models_task_without_a_run_fails_cleanly(initialised):
    code, out = initialised.wf("models", "task", "T1", "m")
    assert code == 1 and "no run in progress" in out


def build_orch(env, computer_use: bool):
    path = env.repo / "workforce.toml"
    path.write_text(path.read_text().replace("computer_use = false", f"computer_use = {str(computer_use).lower()}"))
    ctx = cli.Context(repo=env.repo, home=env.home, console=render_text.make_console(), clock=env.clock or __import__("time"))
    return cli.build_orchestrator(ctx, config_mod.load(env.repo))


def test_set_coder_gives_new_tasks_the_configured_computer_use_default(initialised):
    env = initialised
    picked_run(env)
    orch = build_orch(env, True)
    try:
        cli.set_coder(orch, "T1", "claude-opus-5-5", "low")
        assert env.run().task("T1").computer_use is True
        assert env.run().task("T2").computer_use is False
        StateStore(env.paths).update(lambda run: setattr(run.task("T1"), "computer_use", False))
        cli.set_coder(orch, "T1", "claude-opus-5-5", "medium")
        assert env.run().task("T1").computer_use is False
    finally:
        cli.close_orchestrators()


def test_set_coder_leaves_computer_use_off_when_the_config_says_off(initialised):
    env = initialised
    picked_run(env)
    orch = build_orch(env, False)
    try:
        cli.set_coder(orch, "T1", "claude-opus-5-5", "low")
        assert env.run().task("T1").computer_use is False
    finally:
        cli.close_orchestrators()


def test_the_pick_prompt_sets_the_computer_use_default(initialised):
    env = initialised
    path = env.repo / "workforce.toml"
    path.write_text(path.read_text().replace("computer_use = false", "computer_use = true"))
    env.inputs = ["", ""]
    code, _ = env.wf("run", "add the files")
    assert env.run().task("T1").computer_use is True
    assert env.run().task("T2").computer_use is True


def test_main_closes_the_orchestrators_it_built(initialised):
    env = initialised
    env.wf("pause")
    assert cli._orchestrators == []


def test_main_restores_signal_handlers(initialised):
    import signal

    before = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    initialised.wf("status")
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
