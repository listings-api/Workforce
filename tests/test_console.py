"""The console: logo, render functions, input routing, completion, the controller and a prompt_toolkit smoke test."""

import re
import subprocess
import time
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from tests.test_scheduler import FakeTeam, config_with_checks, make_orch, seed
from workforce import __version__
from workforce import config as config_mod
from workforce.console import app, render
from workforce.console.app import (
    Action,
    CompletionContext,
    ConsoleController,
    PickState,
    QuitState,
    completions,
    parse_input,
    run_console,
)
from workforce.console.logo import LETTERS, render_logo, supports_truecolor
from workforce.console.render import PreflightSummary, strip_ansi
from workforce.render_text import EventFormatter
from workforce.state import Question, Run, Task

WAIT_S = 30


def wait_for(predicate, timeout=WAIT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition never became true")


def make_run(**overrides) -> Run:
    run = Run(id="20260929-120000", goal="add CSV export", created_at="now", status="running", mode="auto")
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


def make_task(task_id="T1", status="pending", **kw) -> Task:
    return Task(id=task_id, title=f"Task {task_id}", description="d", acceptance=["a"], status=status, **kw)


class TestLogo:
    def test_renders_five_lines_with_truecolor_codes(self):
        lines = render_logo(True)
        assert len(lines) == 5
        assert all("\x1b[38;2;" in line and line.endswith("\x1b[0m") for line in lines)
        assert "\x1b[38;2;90;168;255m" in lines[0]
        assert "\x1b[38;2;47;111;224m" in lines[-1]

    def test_falls_back_to_256_colours(self):
        lines = render_logo(False)
        assert len(lines) == 5
        assert all("\x1b[38;5;" in line and "\x1b[38;2;" not in line for line in lines)

    def test_spells_wf_in_block_letters_18_columns_wide(self):
        plain = [strip_ansi(line) for line in render_logo(True)]
        assert plain == list(LETTERS)
        assert all(len(row) == 18 for row in plain)
        assert plain[0] == "██╗    ██╗███████╗"

    def test_colour_gets_darker_top_to_bottom(self):
        reds = [int(re.search(r"38;2;(\d+);", line).group(1)) for line in render_logo(True)]
        assert reds == sorted(reds, reverse=True)

    def test_truecolor_detection_uses_colorterm(self):
        assert supports_truecolor({"COLORTERM": "truecolor"})
        assert supports_truecolor({"COLORTERM": "24bit"})
        assert not supports_truecolor({"COLORTERM": ""})
        assert not supports_truecolor({})


class TestRender:
    def test_header_shows_version_models_subscription_and_repo_with_tilde(self):
        lines = render.header_lines(
            __version__, config_with_checks(), PreflightSummary(), "/Users/me/work/my-app", "/Users/me", True
        )
        plain = [strip_ansi(line) for line in lines]
        assert len(plain) == 5
        assert plain[0].rstrip().endswith(f"WorkForce Agent v{__version__}")
        assert "Claude opus-5-5 · high │ Codex gpt-6-sol · high" in plain[1]
        assert plain[2].rstrip().endswith("Claude subscription · ChatGPT subscription · no API keys ✓")
        assert plain[3].rstrip().endswith("~/work/my-app")
        assert "\x1b[38;2;" in lines[0]

    def test_header_reports_a_failed_preflight(self):
        summary = PreflightSummary(claude_ok=False, keys_ok=False, problems=["claude is not logged in"])
        lines = render.header_lines(__version__, config_with_checks(), summary, "/r", "/h", False, color=False)
        text = "\n".join(lines)
        assert "Claude subscription ✗" in text and "API key set ✗" in text
        assert "✗ claude is not logged in" in text
        assert "\x1b" not in text

    def test_home_relative(self):
        assert render.home_relative("/Users/me/x", "/Users/me") == "~/x"
        assert render.home_relative("/Users/me", "/Users/me") == "~"
        assert render.home_relative("/Users/meow/x", "/Users/me") == "/Users/meow/x"

    def test_usage_text_fresh_last_known_and_unknown(self):
        readings = {
            "windows": [
                {"source": "claude", "kind": "5h", "name": "five_hour", "percent": 51.2, "freshness": "fresh"},
                {"source": "claude", "kind": "weekly", "name": "seven_day", "percent": 33.0, "freshness": "last_known"},
                {"source": "codex", "kind": "weekly", "name": "seven_day", "percent": 4.0, "freshness": "fresh"},
            ]
        }
        assert render.usage_text(readings) == "Claude 5h 51% · wk ~33% │ Codex wk 4%"
        assert render.usage_text(None) == "Claude ? │ Codex ?"
        assert render.usage_text({"windows": []}) == "Claude ? │ Codex ?"
        only_codex = {"windows": [readings["windows"][2]]}
        assert render.usage_text(only_codex) == "Claude ? │ Codex wk 4%"

    def test_expired_windows_are_marked_as_last_known(self):
        readings = {
            "windows": [
                {"source": "claude", "kind": "5h", "name": "five_hour", "percent": 70, "freshness": "fresh", "expired": True}
            ]
        }
        assert render.usage_text(readings).startswith("Claude 5h ~70%")

    def test_usage_alerts_only_for_fresh_windows_at_or_above_the_line(self):
        readings = {
            "windows": [
                {"source": "claude", "kind": "5h", "name": "five_hour", "percent": 51, "freshness": "fresh"},
                {"source": "claude", "kind": "weekly", "name": "seven_day", "percent": 80, "freshness": "last_known"},
                {"source": "codex", "kind": "weekly", "name": "seven_day", "percent": 20, "freshness": "fresh"},
            ]
        }
        lines = [strip_ansi(x) for x in render.usage_alerts(readings, 50)]
        assert lines == ["  ⚠ Claude 5-hour window at 51%. Still working."]

    def test_footer_two_lines_model_repo_usage_mode_and_hints(self):
        run = make_run(tasks=[make_task("T1", "coding", model="claude-sonnet-5-5", effort="high"), make_task("T3", "reviewing")])
        readings = {"windows": [{"source": "claude", "kind": "5h", "name": "five_hour", "percent": 51, "freshness": "fresh"}]}
        first, second = render.footer_lines(config_with_checks(), run, readings, "/Users/me/my-app", "/Users/me", "auto", 90)
        assert first.startswith("  sonnet-5-5 · high │ ~/my-app")
        assert first.rstrip().endswith("Claude 5h 51% │ Codex ?")
        assert len(first) <= 90
        assert second == "  ▸▸ auto · running T1, T3 · shift+tab: auto ⇄ step · /help"

    def test_footer_shortens_a_long_repo_path_so_the_usage_still_fits(self):
        long_repo = "/Users/me/" + "deeply/nested/" * 8 + "my-app"
        first, _ = render.footer_lines(config_with_checks(), None, None, long_repo, "/Users/me", "auto", 80)
        assert len(first) <= 79
        assert "…" in first and first.rstrip().endswith("Claude ? │ Codex ?")
        assert "my-app" in first

    def test_footer_defaults_to_the_plan_reviewer_model_when_nothing_runs(self):
        first, second = render.footer_lines(config_with_checks(), None, None, "/r", "/h", "step", 80)
        assert first.startswith("  opus-5-5 · high │ /r")
        assert second == "  ▸▸ step · idle · shift+tab: auto ⇄ step · /help"

    @pytest.mark.parametrize(
        ("run", "expected"),
        [
            (None, "idle"),
            (make_run(status="planning"), "planning"),
            (make_run(status="debating"), "plan debate"),
            (make_run(status="awaiting_models"), "pick models"),
            (make_run(status="running"), "running"),
            (make_run(status="paused", pause_reason="usage: claude five_hour 61% ≥ 60%"), "⏸ paused: usage: claude five_hour 61% ≥ 60%"),
            (make_run(status="done"), "done"),
        ],
    )
    def test_state_text(self, run, expected):
        assert render.state_text(run) == expected

    def test_state_text_lists_open_questions(self):
        run = make_run(
            status="awaiting_user",
            questions=[Question(id="Q3", task_id=None, text="t"), Question(id="Q4", task_id=None, text="t", status="answered")],
        )
        assert render.state_text(run) == "waiting for you (Q3)"

    def test_event_lines_indent_and_colour(self):
        formatter = EventFormatter()
        alert = render.event_lines(formatter, {"kind": "alert", "message": "Claude 5-hour window at 51%"})
        assert alert == ["\x1b[33m  ⚠ Claude 5-hour window at 51%. Still working.\x1b[0m"]
        plain = render.event_lines(formatter, {"kind": "merged", "task": "T1", "sha": "3f9c2a1abc", "branch": "wf/x/T1"}, color=False)
        assert plain == ["  [T1]    merged  3f9c2a1 into wf/x/T1"]

    def test_question_events_show_positions_and_the_answer_command(self):
        event = {"kind": "question", "question": "Q3", "task": "T1", "text": "Which way?", "positions": {"claude": "A", "codex": "B"}}
        lines = [strip_ansi(x) for x in render.event_lines(EventFormatter(), event)]
        assert lines[0].startswith("  ❓ Q3 (T1): Which way?")
        assert "    claude: A" in lines and "    codex: B" in lines
        assert lines[-1] == "    ↳ type /answer Q3 <your answer>"

    def test_flagged_decisions_read_as_a_warning(self):
        event = {"kind": "decision", "key": "agent_progress", "task": "T3", "answer": "stuck", "flag": True}
        (line,) = [strip_ansi(x) for x in render.event_lines(EventFormatter(), event)]
        assert "T3 looks stuck" in line and "nothing was stopped" in line

    def test_status_lines(self):
        run = make_run(
            tasks=[make_task("T1", "merged", model="claude-sonnet-5-5", effort="high"), make_task("T2", "pending", depends_on=["T1"])],
            questions=[Question(id="Q1", task_id="T2", text="what now?")],
        )
        text = "\n".join(render.status_lines(run, None, "auto"))
        assert "run 20260929-120000 · running · mode auto" in text
        assert "[T1]    merged  sonnet-5-5 · high" in text
        assert "model not chosen" in text and "after T1" in text
        assert "❓ Q1: what now?" in text
        assert render.status_lines(None, None, "auto")[0].strip().startswith("No run yet")

    def test_plan_lines_show_the_laya_hint_only_when_there_is_one(self):
        run = make_run(tasks=[make_task("T1"), make_task("T2", depends_on=["T1"])])
        lines = render.plan_lines(run, {"T1": "small", "T2": None}, config_with_checks().role("coder"))
        assert any("T1: Task T1 · looks small (hint from Laya)" in x for x in lines)
        assert any(x.strip().startswith("T2: Task T2 (after T1)") and "Laya" not in x for x in lines)
        assert any("sonnet-5-5 · high" in x for x in lines)

    def test_rule_matches_the_width(self):
        assert len(render.rule(60)) == 59


class TestParseInput:
    @pytest.mark.parametrize(
        ("text", "active", "expected"),
        [
            ("", False, Action("empty")),
            ("   ", True, Action("empty")),
            ("add CSV export", False, Action("goal", "", "add CSV export")),
            ("add CSV export", True, Action("note", "", "add CSV export")),
            ("/status", False, Action("command", "status", "")),
            ("/ANSWER Q3 use the second one", True, Action("command", "answer", "Q3 use the second one")),
            ("/run build it now", False, Action("command", "run", "build it now")),
            ("/nope", False, Action("unknown", "nope", "")),
            ("@claude why is T1 slow?", True, Action("side", "claude", "why is T1 slow?")),
            ("@Codex", False, Action("side", "codex", "")),
            ("@gpt hi", False, Action("unknown", "gpt", "hi")),
        ],
    )
    def test_routes(self, text, active, expected):
        assert parse_input(text, active) == expected

    def test_every_documented_command_is_known(self):
        documented = {
            "run", "status", "answer", "pause", "resume", "models", "effort", "log", "repos", "publish", "computer", "help", "quit"
        }
        assert set(app.COMMAND_NAMES) == documented
        for name in documented:
            assert parse_input(f"/{name}", False).kind == "command"


class TestCompletions:
    ctx = CompletionContext(
        task_ids=("T1", "T2", "T10"),
        question_ids=("Q3",),
        efforts={"planner": ("low", "high"), "T1": ("low", "medium", "high")},
    )

    def test_commands(self):
        assert completions("/", self.ctx)[0] == [f"/{n}" for n in app.COMMAND_NAMES]
        assert completions("/st", self.ctx) == (["/status"], "/st")
        assert completions("/re", self.ctx)[0] == ["/resume", "/repos"]
        assert completions("/x", self.ctx) == ([], "/x")

    def test_side_agents(self):
        assert completions("@c", self.ctx)[0] == ["@claude", "@codex"]
        assert completions("@cl", self.ctx)[0] == ["@claude"]

    def test_plain_text_has_no_completions(self):
        assert completions("add a thing", self.ctx) == ([], "")

    def test_question_and_task_ids(self):
        assert completions("/answer ", self.ctx) == (["Q3"], "")
        assert completions("/answer q", self.ctx) == (["Q3"], "q")
        assert completions("/log T1", self.ctx) == (["T1", "T10"], "T1")
        assert completions("/computer ", self.ctx)[0] == ["on", "off"]
        assert completions("/computer on T", self.ctx)[0] == ["T1", "T2", "T10"]

    def test_effort_completes_roles_tasks_then_levels(self):
        candidates, _ = completions("/effort ", self.ctx)
        assert "coder" in candidates and "T2" in candidates
        assert completions("/effort planner ", self.ctx)[0] == ["low", "high"]
        assert completions("/effort T1 m", self.ctx)[0] == ["medium"]

    def test_no_completion_after_the_last_argument(self):
        assert completions("/answer Q3 ", self.ctx)[0] == []
        assert completions("/run something ", self.ctx)[0] == []

    def test_the_completer_yields_replacements(self):
        from prompt_toolkit.document import Document

        completer = app.WorkforceCompleter(lambda: self.ctx)
        found = list(completer.get_completions(Document("/answer "), None))
        assert [c.text for c in found] == ["Q3"]
        assert found[0].start_position == 0
        found = list(completer.get_completions(Document("/st"), None))
        assert found[0].text == "/status" and found[0].start_position == -3


PLAN = [
    {"id": "T1", "title": "Add t1", "description": "Create t1.txt", "acceptance": ["exists"], "depends_on": []},
    {"id": "T2", "title": "Add t2", "description": "Create t2.txt", "acceptance": ["exists"], "depends_on": []},
]


@pytest.fixture
def rig(tmp_path, git_repo, monkeypatch):
    class Rig:
        pass

    r = Rig()
    r.tmp_path, r.repo = tmp_path, git_repo
    r.team = FakeTeam(git_repo, plan_tasks=PLAN)
    r.orch = make_orch(tmp_path, git_repo, r.team, monkeypatch)
    r.lines = []
    r.controller = ConsoleController(r.orch, PreflightSummary(), printer=r.lines.append, truecolor=False, color=False)
    r.text = lambda: "\n".join(strip_ansi(x) for x in r.lines)
    r.run = lambda: r.orch.store.load()
    r.settle = lambda: wait_for(lambda: not r.controller.driver.alive())
    return r


class TestController:
    def test_goal_goes_through_plan_model_pick_and_the_team_to_done(self, rig):
        c = rig.controller
        c.handle_line("add the files")
        rig.settle()
        c.tick()
        assert rig.run().status == "awaiting_models"
        assert isinstance(c.modal, PickState)
        assert "Plan agreed. Pick the coder for each task." in rig.text()
        assert "T1: Add t1" in rig.text() and "T2: Add t2" in rig.text()
        assert c.prompt_label() == "coder T1 [sonnet-5-5 · high] ❯ "

        c.handle_line("claude-opus-5-5 low")
        assert c.prompt_label() == "coder T2 [sonnet-5-5 · high] ❯ "
        c.handle_line("not-an-effort nonsense")
        assert "effort 'nonsense' is not valid" in rig.text()
        c.handle_line("")
        assert isinstance(c.modal, PickState)
        assert c.prompt_label().startswith(f"branch for {rig.repo.name} [wf/add-the-files-")
        assert "your checkout is never touched" in rig.text()
        c.handle_line("")
        assert c.modal is None
        wait_for(lambda: rig.run().status == "done")
        rig.settle()
        c.tick()
        run = rig.run()
        assert run.task("T1").model == "claude-opus-5-5" and run.task("T1").effort == "low"
        assert run.task("T2").model == "claude-sonnet-5-5" and run.task("T2").effort == "high"
        assert "run done" in rig.text()
        assert len(rig.team.of("coder")) == 2
        assert {c["request"].model for c in rig.team.of("coder")} == {"claude-opus-5-5", "claude-sonnet-5-5"}

    def test_model_pick_shows_the_task_size_hint_from_the_orchestrator(self, rig, monkeypatch):
        seed(rig.orch, [("T1", []), ("T2", [])])
        rig.orch.store.update(lambda r: setattr(r, "status", "awaiting_models"))
        monkeypatch.setattr(rig.orch, "task_size_hints", lambda: {"T1": "big", "T2": None})
        rig.controller.tick()
        assert "T1: Add t1 · looks big (hint from Laya)" in rig.text()
        assert "looks" not in rig.text().split("T2: Add t2")[1].splitlines()[0]

    def test_a_failing_hint_lookup_does_not_break_the_pick(self, rig, monkeypatch):
        seed(rig.orch, [("T1", [])])
        rig.orch.store.update(lambda r: setattr(r, "status", "awaiting_models"))

        def boom():
            raise RuntimeError("laya is down")

        monkeypatch.setattr(rig.orch, "task_size_hints", boom)
        rig.controller.tick()
        assert isinstance(rig.controller.modal, PickState)

    def test_plain_text_during_a_run_is_a_note_in_decisions_md(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.controller.handle_line("please use tabs, not spaces")
        assert "- User note (console): please use tabs, not spaces" in rig.orch.paths.decisions_md.read_text()
        rig.controller.tick()
        assert "added to decisions.md" in rig.text()

    def test_a_new_goal_while_a_run_is_active_is_refused_with_a_hint(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.controller.handle_line("/run another goal")
        assert "is running; plain text is added as a note" in rig.text()

    def test_answer_records_the_answer_and_restarts_the_team(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.orch.ask("infra", "The coder crashed. How to proceed?", {}, task_id="T1", resume_to="pending")
        assert rig.run().status == "awaiting_user"
        rig.controller.handle_line("/answer q1 retry it")
        assert "✓ answered Q1" in rig.text()
        assert rig.run().question("Q1").answer == "retry it"
        wait_for(lambda: rig.run().status == "done")
        rig.settle()

    def test_answering_while_paused_does_not_unpause_until_resume(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.orch.ask("infra", "The coder crashed. How to proceed?", {}, task_id="T1", resume_to="pending")
        rig.controller.handle_line("/pause")
        assert rig.run().status == "paused"
        rig.controller.handle_line("/answer Q1 retry it")
        assert rig.run().status == "paused"
        assert rig.run().question("Q1").status == "answered"
        assert not rig.controller.driver.alive()
        assert "The run is still paused. Type /resume" in rig.text()
        rig.controller.handle_line("/resume")
        wait_for(lambda: rig.run().status == "done")
        rig.settle()

    def test_answer_needs_an_id_and_text(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.controller.handle_line("/answer Q1")
        assert "usage: /answer <question id> <your answer>" in rig.text()
        rig.controller.handle_line("/answer Q9 hello")
        assert "✗" in rig.text()

    def test_pause_and_resume(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.controller.handle_line("/pause")
        assert rig.run().status == "paused"
        assert "Pausing" in rig.text()
        rig.controller.handle_line("/pause")
        assert "already paused" in rig.text()
        assert rig.run().status == "paused"
        rig.controller.handle_line("/resume")
        rig.settle()
        assert rig.run().status == "done"

    def test_resume_with_no_run_says_so(self, rig):
        rig.controller.handle_line("/resume")
        assert "there is no run to resume" in rig.text()

    def test_status_models_help_log_and_unknown(self, rig):
        seed(rig.orch, [("T1", [])])
        c = rig.controller
        c.handle_line("/status")
        assert "run " in rig.text() and "[T1]" in rig.text()
        c.handle_line("/models")
        assert "planner" in rig.text() and "gpt-6-sol · high" in rig.text() and "T1" in rig.text()
        c.handle_line("/help")
        assert "shift+tab" in rig.text() and "@claude <question>" in rig.text()
        rig.orch.events.emit("note", task="T1", text="hello log")
        c.handle_line("/log T1")
        assert "hello log" in rig.text()
        c.handle_line("/log T9")
        assert "No events for T9." in rig.text()
        c.handle_line("/frobnicate")
        assert "Unknown command: /frobnicate" in rig.text()

    def test_effort_for_a_role_rewrites_only_that_roles_effort_line(self, rig):
        rig.orch.paths.config.write_text(config_mod.default_toml())
        before = rig.orch.paths.config.read_text()
        rig.orch.config = config_with_checks()
        rig.controller.handle_line("/effort planner xhigh")
        assert rig.orch.config.role("planner").effort == "xhigh"
        after = rig.orch.paths.config.read_text()
        assert after != before and after.count("xhigh") == before.count("xhigh") + 1
        rig.controller.handle_line("/effort planner nonsense")
        assert "effort 'nonsense' is not valid for codex" in rig.text()

    def test_effort_for_a_task_only_before_it_starts(self, rig):
        seed(rig.orch, [("T1", []), ("T2", [])])
        rig.orch.store.update(lambda r: setattr(r.task("T2"), "status", "coding"))
        rig.controller.handle_line("/effort T1 low")
        assert rig.run().task("T1").effort == "low"
        rig.controller.handle_line("/effort T2 low")
        assert "has already started" in rig.text()
        assert rig.run().task("T2").effort == "high"

    def test_effort_for_a_task_while_picking_models(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.orch.store.update(lambda r: setattr(r, "status", "awaiting_models"))
        rig.controller.handle_line("/effort T1 medium")
        assert rig.run().task("T1").effort == "medium"

    def test_computer_toggle(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.controller.handle_line("/computer on T1")
        assert rig.run().task("T1").computer_use is True
        rig.controller.handle_line("/computer off t1")
        assert rig.run().task("T1").computer_use is False
        rig.controller.handle_line("/computer maybe T1")
        assert "usage: /computer on|off <task>" in rig.text()

    def test_toggle_mode_without_and_with_a_run(self, rig):
        c = rig.controller
        assert c.current_mode() == "auto"
        assert c.toggle_mode() == "step" and c.current_mode() == "step"
        assert c.toggle_mode() == "auto"
        seed(rig.orch, [("T1", [])], mode="auto")
        assert c.toggle_mode() == "step"
        assert rig.run().mode == "step"
        assert c.current_mode() == "step"
        assert c.toggle_mode() == "auto" and rig.run().mode == "auto"

    def test_step_mode_asks_before_each_task_and_questions_are_shown_inline(self, rig):
        seed(rig.orch, [("T1", [])], mode="auto")
        rig.controller.toggle_mode()
        rig.controller.handle_line("/resume")
        rig.settle()
        rig.controller.tick()
        assert rig.run().status == "awaiting_user"
        assert "❓ Q1 (T1): Start T1: Add t1?" in rig.text()
        assert "type /answer Q1 <your answer>" in rig.text()
        rig.controller.handle_line("/answer Q1 yes")
        wait_for(lambda: rig.run().status == "done")
        rig.settle()

    def test_tick_prints_each_event_once(self, rig):
        rig.orch.events.emit("note", text="first")
        rig.controller.tick()
        rig.controller.tick()
        assert rig.text().count("first") == 1

    def test_events_from_before_the_console_started_are_not_replayed(self, rig):
        rig.orch.events.emit("note", text="ancient")
        late = ConsoleController(rig.orch, PreflightSummary(), printer=rig.lines.append, color=False)
        late.tick()
        assert "ancient" not in rig.text()

    def test_startup_state_mentions_waiting_questions_and_paused_runs(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.orch.ask("infra", "boom", {}, task_id="T1", resume_to="coding")
        rig.controller.show_header()
        text = rig.text()
        assert "WorkForce Agent v" in text
        assert "❓ 1 question waiting (Q1), type /answer Q1 …" in text

    def test_startup_with_a_running_run_and_no_team_says_to_resume(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.controller.show_header()
        assert "No team is attached" in rig.text() and "/resume" in rig.text()

    def test_completion_context_reflects_state(self, rig):
        seed(rig.orch, [("T1", []), ("T2", [])])
        rig.orch.ask("infra", "boom", {}, task_id="T1", resume_to="coding")
        ctx = rig.controller.completion_context()
        assert ctx.task_ids == ("T1", "T2") and ctx.question_ids == ("Q1",)
        assert "high" in ctx.efforts["T1"] and "ultra" in ctx.efforts["planner"]

    def test_toolbar_lines_are_a_rule_then_the_footer(self, rig):
        lines = rig.controller.toolbar_lines(80)
        assert lines[0].strip("─ ") == "" and len(lines) == 3
        assert "shift+tab: auto ⇄ step" in lines[2]


class TestSideQuestions:
    def test_claude_side_question_is_a_fresh_read_only_session_with_the_plan_reviewer_model(self, rig):
        rig.controller.handle_line("@claude where is the CSV code?")
        for thread in rig.controller.side_threads:
            thread.join(WAIT_S)
        (call,) = rig.team.of("side")
        req = call["request"]
        assert req.agent == "claude" and req.model == "claude-opus-5-5" and req.effort == "high"
        assert req.resume_session is None and req.sandbox == "read-only" and req.browser is False
        assert req.cwd == rig.repo and req.user_setup is False
        assert "where is the CSV code?" in req.prompt and "read-only" in req.prompt.lower()
        assert "side answer from claude" in rig.text()

    def test_codex_side_question_uses_the_planner_model_and_read_only_sandbox(self, rig):
        rig.controller.handle_line("@codex summarise the repo")
        for thread in rig.controller.side_threads:
            thread.join(WAIT_S)
        (call,) = rig.team.of("side")
        req = call["request"]
        assert req.agent == "codex" and req.model == "gpt-6-sol" and req.sandbox == "read-only"
        assert "side answer from codex" in rig.text()

    def test_side_question_does_not_disturb_the_run_state(self, rig):
        seed(rig.orch, [("T1", [])])
        before = rig.run()
        rig.controller.handle_line("@claude quick one")
        for thread in rig.controller.side_threads:
            thread.join(WAIT_S)
        assert rig.run() == before

    def test_empty_side_question_shows_usage(self, rig):
        rig.controller.handle_line("@codex")
        assert "Usage: @codex <question>" in rig.text()
        assert rig.team.of("side") == []

    def test_side_question_respects_the_usage_gate(self, rig):
        rig.orch.usage_gate = lambda: "usage: claude five_hour 61% ≥ 60%"
        rig.controller.handle_line("@claude hi")
        assert "not asking @claude while usage is paused" in rig.text()
        assert rig.team.of("side") == []

    def test_a_failed_side_question_is_reported(self, rig):
        class Broken:
            def run(self, req, on_event=None):
                from workforce.agents.base import AgentResult

                return AgentResult(ok=False, text="", structured=None, session_id=None, model=None, error_kind="crash", error="boom")

        rig.orch.runners["claude"] = Broken()
        rig.controller.handle_line("@claude hi")
        for thread in rig.controller.side_threads:
            thread.join(WAIT_S)
        assert "[@claude] failed (crash): boom" in rig.text()


class TestQuit:
    def test_quit_when_idle_exits_at_once(self, rig):
        rig.controller.handle_line("/quit")
        assert rig.controller.done

    def test_quit_with_a_running_team_asks_then_pauses_after_the_step(self, rig):
        rig.team.delays = {"T1": 0.6}
        seed(rig.orch, [("T1", [])])
        rig.controller.handle_line("/resume")
        wait_for(lambda: rig.team.of("coder"))
        rig.controller.handle_line("/quit")
        assert isinstance(rig.controller.modal, QuitState) and not rig.controller.done
        assert "there is no background daemon in v3" in rig.text()
        assert rig.controller.prompt_label().startswith("pause / leave / cancel")
        rig.controller.handle_line("x")
        assert "Type p to pause, l to leave, or c to cancel." in rig.text()
        rig.controller.handle_line("l")
        assert rig.controller.done
        assert not rig.controller.driver.alive()
        run = rig.run()
        assert run.status == "paused" and "no background daemon in v3" in run.pause_reason
        assert len(rig.team.of("coder")) == 1

    def test_cancel_keeps_the_console_open(self, rig):
        rig.team.delays = {"T1": 0.4}
        seed(rig.orch, [("T1", [])])
        rig.controller.handle_line("/resume")
        rig.controller.handle_line("/quit")
        rig.controller.handle_line("c")
        assert rig.controller.modal is None and not rig.controller.done
        rig.settle()

    def test_pause_choice_pauses_with_the_user_reason(self, rig):
        rig.team.delays = {"T1": 0.4}
        seed(rig.orch, [("T1", [])])
        rig.controller.handle_line("/resume")
        rig.controller.handle_line("/quit")
        rig.controller.handle_line("pause")
        assert rig.controller.done
        assert rig.run().pause_reason == app.QUIT_PAUSE_REASON

    def test_eof_twice_quits_regardless(self, rig):
        rig.team.delays = {"T1": 0.4}
        seed(rig.orch, [("T1", [])])
        rig.controller.handle_line("/resume")
        rig.controller.on_eof()
        assert isinstance(rig.controller.modal, QuitState)
        rig.controller.on_eof()
        assert rig.controller.done
        rig.settle()


class TestDriver:
    def test_only_one_team_thread_at_a_time(self, rig):
        rig.team.delays = {"T1": 0.4}
        seed(rig.orch, [("T1", [])])
        assert rig.controller.driver.start() is True
        assert rig.controller.driver.start() is False
        rig.settle()
        assert rig.controller.driver.start() is True
        rig.settle()

    def test_errors_from_the_team_are_reported(self, rig):
        errors = []
        driver = app.Driver(lambda: (_ for _ in ()).throw(RuntimeError("kaput")), errors.append)
        driver.start()
        driver.join(WAIT_S)
        assert errors == ["✗ the team stopped on an unexpected error: RuntimeError: kaput"]


class TestSideQuestionHead:
    def _commit_during(self, rig, commit: bool):
        import subprocess

        from workforce.agents.base import AgentResult

        repo = rig.repo

        class Committer:
            def run(self, req, on_event=None):
                if commit:
                    (repo / "sneaky.txt").write_text("x")
                    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
                    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "sneaky"], check=True)
                return AgentResult(ok=True, text="answer", structured=None, session_id=None, model=None)

        rig.orch.runners["claude"] = Committer()
        rig.controller.handle_line("@claude hi")
        for thread in rig.controller.side_threads:
            thread.join(WAIT_S)

    def test_a_commit_during_a_side_question_is_flagged(self, rig):
        self._commit_during(rig, True)
        text = rig.text()
        assert "moved HEAD" in text and "read-only side question" in text
        events, _ = rig.orch.events.read(0)
        errors = [e for e in events if e["kind"] == "error" and e.get("source") == "side_question"]
        assert len(errors) == 1 and "moved HEAD" in errors[0]["message"]
        assert any("\x1b[31m" in line and "moved HEAD" in line for line in rig.lines) or "moved HEAD" in text

    def test_a_quiet_side_question_raises_no_warning(self, rig):
        self._commit_during(rig, False)
        assert "moved HEAD" not in rig.text()
        events, _ = rig.orch.events.read(0)
        assert not [e for e in events if e["kind"] == "error"]


class TestBackgroundTimers:
    def _start(self, rig, monkeypatch):
        monkeypatch.setattr(app, "SLOW_LOOP_S", 0.01)
        monkeypatch.setattr(app, "PROGRESS_INTERVAL_S", 0.03)
        timers = app.Background(rig.controller, tick_s=60)
        timers.start()
        return timers

    def _errors(self, rig):
        events, _ = rig.orch.events.read(0)
        return [e for e in events if e["kind"] == "error" and e.get("source") == "progress_tick"]

    def test_a_failing_progress_tick_is_logged_and_the_thread_keeps_running(self, rig, monkeypatch):
        calls = []

        def broken():
            calls.append(1)
            raise RuntimeError("laya exploded")

        monkeypatch.setattr(rig.orch, "progress_tick", broken)
        timers = self._start(rig, monkeypatch)
        try:
            wait_for(lambda: len(calls) >= 3)
            slow = [t for t in timers._threads if t.name == "wf-slow"][0]
            assert slow.is_alive()
        finally:
            timers.stop()
        errors = self._errors(rig)
        assert len(errors) >= 3
        assert errors[0]["message"] == "RuntimeError: laya exploded"

    def test_a_failing_usage_poll_is_logged_and_progress_checks_still_run(self, rig, monkeypatch):
        polls, progress = [], []

        def broken_poll():
            polls.append(1)
            raise OSError("disk went away")

        monkeypatch.setattr(rig.orch, "poll_tick", broken_poll)
        monkeypatch.setattr(rig.orch, "progress_tick", lambda: progress.append(1))
        object.__setattr__(rig.orch.config.limits, "poll_minutes", 0.0005)
        timers = self._start(rig, monkeypatch)
        try:
            wait_for(lambda: len(polls) >= 2 and len(progress) >= 2)
        finally:
            timers.stop()
        events, _ = rig.orch.events.read(0)
        errors = [e for e in events if e["kind"] == "error" and e.get("source") == "usage_poll"]
        assert len(errors) >= 2 and errors[0]["message"] == "OSError: disk went away"

    def test_a_failure_while_logging_the_error_does_not_kill_the_thread(self, rig, monkeypatch):
        calls = []
        monkeypatch.setattr(rig.orch, "progress_tick", lambda: (calls.append(1), 1 / 0))
        monkeypatch.setattr(rig.orch.events, "emit", lambda *a, **k: (_ for _ in ()).throw(OSError("log full")))
        timers = self._start(rig, monkeypatch)
        try:
            wait_for(lambda: len(calls) >= 3)
        finally:
            timers.stop()


class TestConsoleHardening:
    def test_finished_side_threads_are_pruned(self, rig):
        for _ in range(3):
            rig.controller.handle_line("@claude one more")
            for thread in rig.controller.side_threads:
                thread.join(WAIT_S)
        rig.controller.handle_line("@claude last")
        assert len(rig.controller.side_threads) <= 2
        for thread in rig.controller.side_threads:
            thread.join(WAIT_S)

    def test_side_answers_lose_terminal_escapes(self, rig):
        from workforce.agents.base import AgentResult

        class Sneaky:
            def run(self, req, on_event=None):
                text = "fine \x1b[2J\x1b]0;pwned\x07answer"
                return AgentResult(ok=True, text=text, structured=None, session_id=None, model=None)

        rig.orch.runners["claude"] = Sneaky()
        rig.controller.handle_line("@claude hi")
        for thread in rig.controller.side_threads:
            thread.join(WAIT_S)
        joined = "\n".join(rig.lines)
        assert "\x1b" not in joined and "\x07" not in joined
        assert "fine answer" in joined

    def test_the_model_pick_gives_new_tasks_the_configured_computer_use_default(self, rig):
        import dataclasses

        rig.orch.config = dataclasses.replace(
            rig.orch.config, browser=dataclasses.replace(rig.orch.config.browser, computer_use=True)
        )
        c = rig.controller
        c.handle_line("add the files")
        rig.settle()
        c.tick()
        assert rig.run().status == "awaiting_models"
        assert [t.computer_use for t in rig.run().tasks] == [False, False]
        c.handle_line("")
        c.handle_line("")
        c.handle_line("")
        assert [t.computer_use for t in rig.run().tasks] == [True, True]
        rig.settle()


class TestSmoke:
    def _run(self, rig, keys, eof=False, **kwargs):
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            if eof:
                pipe.close()
            code = run_console(
                rig.repo,
                home=rig.tmp_path / "home",
                input=pipe,
                output=DummyOutput(),
                orchestrator=rig.orch,
                preflight=lambda: PreflightSummary(),
                printer=rig.lines.append,
                **kwargs,
            )
        return code

    def test_status_then_quit(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.orch.store.update(lambda r: setattr(r, "status", "paused"))
        assert self._run(rig, "/status\r/quit\r") == 0
        text = rig.text()
        assert f"WorkForce Agent v{__version__}" in text
        assert "run 20" in text and "[T1]" in text
        history = (rig.tmp_path / "home" / ".workforce_history").read_text()
        assert "/status" in history and "/quit" in history

    def test_shift_tab_toggles_the_mode(self, rig):
        seed(rig.orch, [("T1", [])])
        rig.orch.store.update(lambda r: setattr(r, "status", "paused"))
        assert self._run(rig, "\x1b[Z/quit\r", background=False) == 0
        assert rig.orch.store.load().mode == "step"
        assert self._run(rig, "\x1b[Z\x1b[Z/quit\r", background=False) == 0
        assert rig.orch.store.load().mode == "step"

    def test_a_goal_typed_at_the_prompt_starts_the_team(self, rig):
        assert self._run(rig, "add the files\r/quit\rl\r", background=False) == 0
        run = rig.orch.store.load()
        assert run.goal == "add the files"
        assert run.status in ("paused", "awaiting_models")
        assert len(rig.team.of("plan")) == 1

    def test_end_of_input_quits_cleanly(self, rig):
        assert self._run(rig, "/help\r", eof=True, background=False) == 0
        assert "shift+tab" in rig.text()

    def test_the_session_layout(self, rig):
        controller = ConsoleController(rig.orch, PreflightSummary(), printer=rig.lines.append, color=False)
        with create_pipe_input() as pipe:
            session = app.build_session(controller, input=pipe, output=DummyOutput())
            message = "".join(text for _, text in session.message())
            assert message.startswith(" ─") and message.endswith("\n❯ ")
            toolbar = "".join(text for _, text in session.bottom_toolbar())
            assert toolbar.count("\n") == 2 and "auto" in toolbar
            assert "@codex / @claude" in "".join(text for _, text in session.placeholder)


class TestWorkspaceConsole:
    """The console on a folder of repos, driven through a stub orchestrator that speaks the workspace API."""

    @pytest.fixture
    def wrig(self, tmp_path, git_workspace, monkeypatch):
        from tests.ws_shim import StubOrch
        from workforce.agents.base import AgentResult
        from workforce.workspace import find_workspace

        class Recorder:
            def __init__(self):
                self.requests = []
                self.result = AgentResult(ok=True, text="fine answer", structured=None, session_id="s", model="m")
                self.on_run = None

            def run(self, req, on_event=None):
                self.requests.append(req)
                if self.on_run:
                    self.on_run(req)
                return self.result

        class Rig:
            pass

        r = Rig()
        r.tmp_path, r.root = tmp_path, git_workspace
        (git_workspace / "workforce.toml").write_text(config_mod.workspace_toml(["alpha", "beta"]))
        r.runner = Recorder()
        r.config = config_mod.load(git_workspace)
        r.build = lambda ws, proposed=None: StubOrch(
            ws, r.config, tmp_path, runners={"claude": r.runner, "codex": r.runner}, proposed=proposed
        )
        r.ws = find_workspace(git_workspace)
        r.orch = r.build(r.ws, {"alpha": "wf/add-things-0929", "beta": "wf/add-things-0929"})
        r.lines = []
        r.controller = ConsoleController(r.orch, PreflightSummary(), printer=r.lines.append, truecolor=False, color=False)
        r.text = lambda: "\n".join(strip_ansi(x) for x in r.lines)
        monkeypatch.setattr(r.controller.driver, "start", lambda: True)
        return r

    def test_the_header_names_the_workspace_and_its_repo_count(self, wrig):
        wrig.controller.show_header()
        assert "Workspace ~/ws · 2 repos" in wrig.text()

    def test_the_single_repo_header_shows_the_repo_path(self, tmp_path, git_repo, monkeypatch):
        from tests.ws_shim import StubOrch
        from workforce.workspace import find_workspace

        (git_repo / "workforce.toml").write_text(config_mod.default_toml())
        orch = StubOrch(find_workspace(git_repo), config_mod.load(git_repo), tmp_path)
        lines = []
        controller = ConsoleController(orch, PreflightSummary(), printer=lines.append, truecolor=False, color=False)
        controller.show_header()
        text = "\n".join(strip_ansi(x) for x in lines)
        assert f"~/{git_repo.relative_to(tmp_path)}" in text
        assert "Workspace" not in text

    def test_the_footer_adds_the_focus_repo(self, wrig, tmp_path):
        from workforce.workspace import find_workspace

        sub = wrig.root / "alpha" / "src"
        sub.mkdir()
        focused = wrig.build(find_workspace(sub))
        controller = ConsoleController(focused, PreflightSummary(), printer=wrig.lines.append, color=False)
        footer = controller.toolbar_lines(100)
        assert "~/ws · alpha" in footer[1]
        assert "~/ws" in wrig.controller.toolbar_lines(100)[1] and " · alpha" not in wrig.controller.toolbar_lines(100)[1]

    def test_the_footer_has_no_focus_in_single_repo_mode(self, rig):
        assert " · " not in rig.controller.toolbar_lines(100)[1].split("│")[1]

    def test_repos_command_lists_the_repos(self, wrig):
        (wrig.root / "beta" / "README.md").write_text("changed\n")
        wrig.controller.handle_line("/repos")
        lines = [l for l in wrig.text().splitlines() if l.strip()]
        alpha = next(l for l in lines if l.strip().startswith("alpha"))
        beta = next(l for l in lines if l.strip().startswith("beta"))
        assert "base main (current) · on main · clean · none detected" in alpha
        assert "dirty" in beta

    def test_events_are_tagged_with_repo_and_task(self, wrig):
        wrig.orch.events.emit("agent_event", phase="start", role="coder", task="T1", repo="alpha", model="claude-sonnet-5-5", step="code")
        wrig.controller.tick()
        assert "  [alpha/T1]" in wrig.text() and "coding" in wrig.text()

    def test_status_shows_repo_tags_and_integration_branches(self, wrig):
        from tests.ws_shim import make_branch, seed_run

        base, _ = make_branch(wrig.root / "alpha", "wf/add-things-0929", {"a.txt": "a\n"})
        seed_run(
            wrig.orch,
            status="paused",
            tasks=[("alpha", "T1"), ("beta", "T2")],
            branches={"alpha": ("wf/add-things-0929", "main", base)},
        )
        wrig.controller.handle_line("/status")
        text = wrig.text()
        assert "[alpha/T1]" in text and "[beta/T2]" in text
        assert "Integration branches:" in text
        assert f"alpha  wf/add-things-0929  1 commit on top of main ({base[:8]})" in text

    def test_log_filters_by_qualified_task(self, wrig):
        wrig.orch.events.emit("review", task="T1", repo="alpha", model="claude-opus-5-5", verdict="APPROVE", sha="aaaaaaa", findings=0)
        wrig.orch.events.emit("review", task="T1", repo="beta", model="claude-opus-5-5", verdict="APPROVE", sha="bbbbbbb", findings=0)
        wrig.controller.handle_line("/log alpha/t1")
        text = wrig.text()
        assert "[alpha/T1]" in text and "[beta/T1]" not in text
        wrig.lines.clear()
        wrig.controller.handle_line("/log gamma/T1")
        assert "No events for gamma/T1." in wrig.text()

    def test_publish_and_repos_complete(self, wrig):
        ctx = wrig.controller.completion_context()
        assert ctx.repo_names == ("alpha", "beta")
        assert completions("/publish a", ctx) == (["alpha"], "a")
        assert completions("/publish ", ctx)[0] == ["alpha", "beta"]

    def test_side_questions_run_at_the_workspace_root_read_only_with_the_repos_readable(self, wrig):
        wrig.controller.handle_line("@claude how do the repos fit together?")
        for t in wrig.controller.side_threads:
            t.join(10)
        (request,) = wrig.runner.requests
        assert request.cwd == wrig.root and request.sandbox == "read-only" and request.repo == wrig.root
        assert request.add_dirs == [wrig.root / "alpha", wrig.root / "beta"]
        assert request.skip_git_check is False
        assert "fine answer" in wrig.text()

    def test_a_codex_side_question_uses_the_root_and_skips_the_git_check(self, wrig):
        wrig.controller.handle_line("@codex what is in here?")
        for t in wrig.controller.side_threads:
            t.join(10)
        (request,) = wrig.runner.requests
        assert request.cwd == wrig.root and request.sandbox == "read-only"
        assert request.skip_git_check is True and request.add_dirs == []

    def test_a_side_question_that_moves_any_repos_head_is_reported_with_the_repo(self, wrig):
        def commit_in_beta(req):
            subprocess.run(["git", "-C", str(wrig.root / "beta"), "commit", "-q", "--allow-empty", "-m", "oops"], check=True)

        wrig.runner.on_run = commit_in_beta
        wrig.controller.handle_line("@claude anything")
        for t in wrig.controller.side_threads:
            t.join(10)
        assert "moved HEAD in beta" in wrig.text()
        events, _ = wrig.orch.events.read(0)
        assert any(e["kind"] == "error" and "moved HEAD in beta" in e["message"] for e in events)

    def test_the_model_pick_shows_repo_labels_then_asks_for_each_branch(self, wrig):
        from tests.ws_shim import seed_run

        seed_run(wrig.orch, status="awaiting_models", tasks=[("alpha", "T1"), ("beta", "T2")])
        c = wrig.controller
        c.tick()
        assert "alpha/T1: Add t1" in wrig.text() and "beta/T2: Add t2" in wrig.text()
        assert c.prompt_label() == "coder alpha/T1 [sonnet-5-5 · high] ❯ "
        c.handle_line("")
        assert c.prompt_label() == "coder beta/T2 [sonnet-5-5 · high] ❯ "
        c.handle_line("claude-opus-5-5 low")
        assert c.prompt_label() == "branch for alpha [wf/add-things-0929] ❯ "
        assert wrig.orch.resumed == 0
        c.handle_line("")
        assert c.prompt_label() == "branch for beta [wf/add-things-0929] ❯ "
        c.handle_line("two words")
        assert "'two words' is not a valid branch name" in wrig.text()
        assert c.prompt_label() == "branch for beta [wf/add-things-0929] ❯ "
        c.handle_line("wf/beta-own")
        assert c.modal is None
        assert wrig.orch.set_branch_calls == [("beta", "wf/beta-own")]
        assert wrig.orch.resumed == 1
        assert [call[0] for call in wrig.orch.model_calls] == ["T1", "T2"]
        assert wrig.orch.model_calls[1] == ("T2", "claude-opus-5-5", "low")

    def published(self, wrig, status="done", names=("alpha",)):
        from tests.ws_shim import make_branch, seed_run

        branches = {}
        for name in names:
            base, _ = make_branch(wrig.root / name, "wf/add-things-0929", {"x.txt": "x\n"})
            branches[name] = ("wf/add-things-0929", "main", base)
        seed_run(wrig.orch, status=status, tasks=[(n, f"T{i}") for i, n in enumerate(names, 1)], branches=branches)

    def test_publish_asks_first_and_cancels_on_anything_but_yes(self, wrig):
        from workforce.console.app import PublishState

        self.published(wrig)
        c = wrig.controller
        c.handle_line("/publish")
        assert isinstance(c.modal, PublishState)
        assert c.prompt_label() == "publish? [y/N] ❯ "
        assert "Ready to publish" in wrig.text() and "alpha  wf/add-things-0929  1 commit on top of main" in wrig.text()
        c.handle_line("no")
        assert c.modal is None and "Publish cancelled." in wrig.text()
        assert wrig.runner.requests == []

    def test_publish_yes_runs_the_committer_and_prints_the_pr(self, wrig):
        from workforce.agents.base import AgentResult

        self.published(wrig)
        wrig.runner.result = AgentResult(
            ok=True,
            text="",
            structured={"pushed": True, "pr_url": "https://example.test/pull/9", "error": None},
            session_id="s",
            model="m",
        )
        c = wrig.controller
        c.handle_line("/publish")
        c.handle_line("y")
        for t in c.side_threads:
            t.join(30)
        (request,) = wrig.runner.requests
        assert request.user_setup is True and request.role == "committer"
        assert request.cwd.name == "_publish" and request.repo == wrig.root / "alpha"
        text = wrig.text()
        assert "✓ alpha  wf/add-things-0929  https://example.test/pull/9" in text
        assert not request.cwd.exists()

    def test_publish_with_an_error_shows_it(self, wrig):
        from workforce.agents.base import AgentResult

        self.published(wrig)
        wrig.runner.result = AgentResult(
            ok=True, text="", structured={"pushed": False, "pr_url": None, "error": "gh: not logged in"}, session_id="s", model="m"
        )
        c = wrig.controller
        c.handle_line("/publish alpha")
        c.handle_line("yes")
        for t in c.side_threads:
            t.join(30)
        assert "✗ alpha  wf/add-things-0929  gh: not logged in" in wrig.text()

    def test_publish_is_refused_while_a_run_is_active_or_the_team_is_working(self, wrig, monkeypatch):
        self.published(wrig, status="running")
        wrig.controller.handle_line("/publish")
        assert "run 20260929-120000 is running" in wrig.text()
        assert wrig.controller.modal is None
        wrig.orch.store.update(lambda r: setattr(r, "status", "done"))
        wrig.lines.clear()
        monkeypatch.setattr(wrig.controller.driver, "alive", lambda: True)
        wrig.controller.handle_line("/publish")
        assert "the team is still working" in wrig.text()

    def test_publish_of_an_unknown_repo_or_with_nothing_to_publish_is_an_error(self, wrig):
        self.published(wrig)
        wrig.controller.handle_line("/publish nope")
        assert "no repo 'nope'" in wrig.text()
        wrig.lines.clear()
        wrig.controller.handle_line("/publish beta")
        assert "no integration branch for beta has commits to publish" in wrig.text()

    def test_help_lists_the_new_commands(self, wrig):
        wrig.controller.handle_line("/help")
        assert "/repos" in wrig.text() and "/publish [repo]" in wrig.text()
