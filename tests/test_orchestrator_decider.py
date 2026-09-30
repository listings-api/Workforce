"""Laya wiring in the orchestrator: per-key thresholds, the decider factory, progress and size hints."""

import json
import threading
from pathlib import Path

import pytest

from workforce import config as config_mod
from workforce import review
from workforce.decider import questions
from workforce.decider.base import NullDecider
from workforce.decider.factory import make_decider
from workforce.decider.laya import LayaDecider
from workforce.events import EventLog
from workforce.orchestrator import _log_line_text
from workforce.paths import Paths
from tests.test_commit import CommitterRunner, real_commit, scene  # noqa: F401
from tests.test_decider import FakeOllaya
from tests.test_orchestrator_e2e import (  # noqa: F401
    REVIEWER_MATCH,
    assert_merged,
    config_text,
    harness,
    prompts_with,
)
from tests.test_review import ScriptedRunner, both, setup, verdict  # noqa: F401


@pytest.fixture
def ollaya():
    fake = FakeOllaya()
    thread = threading.Thread(target=fake.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield fake
    fake.shutdown()
    fake.server_close()


@pytest.fixture
def laya(ollaya, tmp_path):
    def build(thresholds, confidence=0.85):
        events = EventLog(Paths(tmp_path / "decider-events"))
        return LayaDecider(ollaya.url, confidence, events, thresholds=thresholds)

    return build


def asked(server: FakeOllaya, text: str) -> list[dict]:
    return [r["body"] for r in server.requests if r["body"]["questions"]["q"]["instructions"] == text]


def laya_config(extra: str = "") -> config_mod.Config:
    return config_mod.parse(config_mod.default_toml() + extra)


def test_make_decider_builds_null_for_off_and_laya_for_laya(tmp_path):
    events = EventLog(Paths(tmp_path))
    off = config_mod.parse(config_mod.default_toml().replace('backend = "laya"', 'backend = "off"'))
    assert isinstance(make_decider(off, events), NullDecider)
    decider = make_decider(config_mod.parse(config_mod.default_toml()), events)
    assert isinstance(decider, LayaDecider)
    decider.close()


def test_make_decider_carries_the_config_thresholds_and_models(tmp_path):
    events = EventLog(Paths(tmp_path))
    text = config_mod.default_toml()
    assert "[decider.thresholds]" in text
    decider = make_decider(config_mod.parse(text), events)
    assert decider.threshold_for("task_size") == 0.75
    assert decider.threshold_for("agent_progress") == 0.45
    assert decider.threshold_for("debate_converged") == 0.63
    for unreliable in ("needs_human", "verdict_consistent", "risk_gate"):
        assert decider.threshold_for(unreliable) is None
    assert decider.model_for("task_size") == "laya:en"
    decider.close()

    custom = text + '\n[decider.models]\ntask_size = "laya:typed-decisions"\n'
    decider = make_decider(config_mod.parse(custom), events)
    assert decider.model_for("task_size") == "laya:typed-decisions"
    assert decider.model_for("agent_progress") == "laya:en"
    decider.close()


def test_a_key_without_a_threshold_is_never_asked_and_the_user_gets_the_question(harness, laya, ollaya):
    h = harness("three_rounds_question", decider=laya({"task_size": 0.5}))
    ollaya.choose = "agents"
    run = h.go()
    assert run.status == "awaiting_user"
    assert run.questions[0].task_id == "T1"
    assert ollaya.requests == []


def test_needs_human_with_its_own_threshold_can_grant_the_extra_round(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "agents", 0.95
    h = harness("three_rounds_question", decider=laya({"needs_human": 0.6}))
    run = h.go()
    assert_merged(h, run, 1)
    assert h.events("question") == []
    sent = asked(ollaya, questions.QUESTION_TEXT["needs_human"])
    assert len(sent) == 1 and "REQUEST_CHANGES" in sent[0]["state"]
    assert len(ollaya.requests) == 1


def test_the_per_key_threshold_beats_the_global_confidence(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "agents", 0.95
    h = harness("three_rounds_question", decider=laya({"needs_human": 0.97}, confidence=0.85))
    run = h.go()
    assert run.status == "awaiting_user" and run.questions[0].task_id == "T1"
    assert len(asked(ollaya, questions.QUESTION_TEXT["needs_human"])) == 1


@pytest.mark.parametrize(
    "thresholds, confidence, converged",
    [
        ({"debate_converged": 0.63}, 0.70, True),
        ({"debate_converged": 0.75}, 0.70, False),
        ({"task_size": 0.50}, 0.99, False),
    ],
)
def test_debate_converged_uses_its_own_threshold_and_is_skipped_when_unreliable(
    harness, laya, ollaya, thresholds, confidence, converged
):
    ollaya.choose, ollaya.confidence = "yes", confidence
    h = harness("plan_debate_cap", decider=laya(thresholds))
    h.orch.start("add the files")
    run = h.orch.run_until_blocked()
    calls = h.calls()
    if converged:
        assert run.status == "awaiting_models"
        assert len([c for c in calls if c["cli"] == "claude"]) == 1
        assert len([c for c in calls if c["cli"] == "codex"]) == 2
        assert len(asked(ollaya, questions.QUESTION_TEXT["debate_converged"])) == 1
    else:
        assert run.status == "awaiting_user" and run.questions[0].task_id is None
        assert len([c for c in calls if c["cli"] == "claude"]) == 4
        if "debate_converged" in thresholds:
            assert len(asked(ollaya, questions.QUESTION_TEXT["debate_converged"])) == 3
        else:
            assert ollaya.requests == []


def test_verdict_consistency_is_not_asked_and_reviewers_are_not_rerun_when_the_key_is_unreliable(
    setup, laya, ollaya
):
    outcome, claude, codex = both(setup, [verdict("APPROVE")], [verdict("APPROVE")], decider=laya({}))
    assert outcome.approved
    assert len(claude.requests) == 1 and len(codex.requests) == 1
    assert ollaya.requests == []


def test_a_confident_consistent_verdict_is_approved_with_its_own_threshold(setup, laya, ollaya):
    ollaya.choose, ollaya.confidence = "yes", 0.95
    decider = laya({"verdict_consistent": 0.9})
    outcome, claude, codex = both(setup, [verdict("APPROVE")], [verdict("APPROVE")], decider=decider)
    assert outcome.approved
    assert len(claude.requests) == 1 and len(codex.requests) == 1
    assert len(asked(ollaya, review.CONSISTENCY_QUESTION)) == 2


def test_below_the_per_key_threshold_counts_as_unsure_and_reruns_then_rejects(setup, laya, ollaya):
    ollaya.choose, ollaya.confidence = "yes", 0.95
    decider = laya({"verdict_consistent": 0.99}, confidence=0.5)
    outcome, claude, codex = both(
        setup,
        [verdict("APPROVE"), verdict("APPROVE")],
        [verdict("APPROVE"), verdict("APPROVE")],
        decider=decider,
    )
    assert not outcome.approved
    assert len(claude.requests) == 2 and len(codex.requests) == 2
    assert {r.verdict for r in outcome.final_reviews} == {"REQUEST_CHANGES"}


def test_every_agent_request_carries_the_repo(setup):
    claude, codex = ScriptedRunner([verdict("APPROVE")]), ScriptedRunner([verdict("APPROVE")])
    ctx = setup.context(claude, codex)
    ctx.repo = setup.repo
    assert review.double_review(ctx).approved
    assert claude.requests[0].repo == setup.repo and codex.requests[0].repo == setup.repo


def test_the_commit_request_carries_the_repo(scene):  # noqa: F811
    from workforce import commit

    runner = CommitterRunner(real_commit)
    context = scene.context(runner)
    context.repo = scene.repo
    assert commit.commit_task(context).ok
    assert runner.requests[0].repo == scene.repo


def test_orchestrator_runs_get_the_risk_gate_hooks_because_they_carry_the_repo(harness):
    h = harness("happy_path")
    h.go()
    calls = h.calls()
    assert calls
    for call in calls:
        if call["cli"] == "claude":
            assert "--settings" in call["argv"]
        else:
            assert "--dangerously-bypass-hook-trust" in call["argv"]


def at_coding(h):
    h.to_models()
    h.pick_models()
    return h.step_until(lambda run: run.tasks[0].status == "coding")


def write_log(h, run, step, n, lines):
    path = h.paths.step_log(run.id, "T1", step, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def assistant_line(text):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


def test_progress_tick_flags_a_confident_stuck_agent_from_the_last_forty_lines(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "stuck", 0.95
    h = harness("happy_path", decider=laya({"agent_progress": 0.45}))
    run = at_coding(h)
    log = write_log(h, run, "code", 1, [assistant_line(f"working on step {i}") for i in range(60)])

    flagged = h.orch.progress_tick()
    assert len(flagged) == 1
    item = flagged[0]
    assert item["task"] == "T1" and item["answer"] == "stuck" and item["flag"] is True
    assert item["key"] == "agent_progress" and item["log"] == str(log)
    events = [e for e in h.events("decision") if e.get("flag")]
    assert len(events) == 1 and events[0]["task"] == "T1" and events[0]["answer"] == "stuck"

    state = asked(ollaya, questions.QUESTION_TEXT["agent_progress"])[0]["state"]
    assert "working on step 59" in state and "working on step 20" in state
    assert "working on step 19\n" not in state and "working on step 0\n" not in state
    assert "assistant: working on step 59" in state and '{"type"' not in state
    assert "Task: " in state
    assert h.run().task("T1").status == "coding"


def test_progress_tick_flags_off_task_but_not_progressing_or_low_confidence(harness, laya, ollaya):
    h = harness("happy_path", decider=laya({"agent_progress": 0.45}))
    run = at_coding(h)
    write_log(h, run, "code", 1, [assistant_line("hello")])

    ollaya.choose, ollaya.confidence = "off_task", 0.95
    assert [f["answer"] for f in h.orch.progress_tick()] == ["off_task"]
    ollaya.choose, ollaya.confidence = "progressing", 0.95
    assert h.orch.progress_tick() == []
    ollaya.choose, ollaya.confidence = "stuck", 0.30
    assert h.orch.progress_tick() == []
    assert len([e for e in h.events("decision") if e.get("flag")]) == 1


def test_progress_tick_skips_an_unreliable_key(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "stuck", 0.95
    h = harness("happy_path", decider=laya({"task_size": 0.5}))
    run = at_coding(h)
    write_log(h, run, "code", 1, [assistant_line("hello")])
    assert h.orch.progress_tick() == []
    assert ollaya.requests == []


def test_progress_tick_skips_a_task_with_no_log_yet(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "stuck", 0.95
    h = harness("happy_path", decider=laya({"agent_progress": 0.45}))
    at_coding(h)
    assert h.orch.progress_tick() == []
    assert ollaya.requests == []


def test_progress_tick_does_nothing_while_the_run_is_not_running(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "stuck", 0.95
    h = harness("happy_path", decider=laya({"agent_progress": 0.45}))
    run = at_coding(h)
    write_log(h, run, "code", 1, [assistant_line("hello")])
    h.orch.pause("lunch")
    assert h.orch.progress_tick() == []
    assert ollaya.requests == []


def test_progress_tick_without_a_decider_does_nothing(harness):
    h = harness("happy_path")
    run = at_coding(h)
    write_log(h, run, "code", 1, [assistant_line("hello")])
    assert h.orch.progress_tick() == []


def test_progress_tick_reads_the_latest_fix_log_for_a_fixing_task_and_skips_other_statuses(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "stuck", 0.95
    h = harness("happy_path", decider=laya({"agent_progress": 0.45}))
    run = at_coding(h)
    write_log(h, run, "code", 1, [assistant_line("CODE-LOG-LINE")])
    write_log(h, run, "fix", 1, [assistant_line("OLD-FIX-LINE")])
    write_log(h, run, "fix", 2, [assistant_line("NEW-FIX-LINE")])
    write_log(h, run, "fix", 10, [assistant_line("NEWEST-FIX-LINE")])

    def to(status):
        def fn(r):
            r.task("T1").status = status

        h.store.update(fn)

    to("fixing")
    assert len(h.orch.progress_tick()) == 1
    state = ollaya.requests[-1]["body"]["state"]
    assert "NEWEST-FIX-LINE" in state and "OLD-FIX-LINE" not in state and "CODE-LOG-LINE" not in state

    sent = len(ollaya.requests)
    for status in ("testing", "reviewing", "pending"):
        to(status)
        assert h.orch.progress_tick() == []
    assert len(ollaya.requests) == sent


def test_log_line_text_makes_agent_streams_readable():
    assert _log_line_text(assistant_line("hi there")) == "assistant: hi there"
    tool = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}
    )
    assert _log_line_text(tool) == 'assistant: tool Bash: {"command": "ls"}'
    codex = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
    assert _log_line_text(codex) == "agent_message: done"
    assert _log_line_text(json.dumps({"type": "result", "result": "OK"})) == "result: OK"
    assert _log_line_text(json.dumps({"type": "turn.started"})) == "turn.started"
    assert _log_line_text("plain text line\n") == "plain text line"
    assert _log_line_text("[1, 2]") == "[1, 2]"


def test_task_size_hints_come_from_confident_answers_and_are_cached(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "small", 0.95
    h = harness("happy_path", decider=laya({"task_size": 0.5}))
    h.to_models()
    assert h.orch.task_size_hints() == {"T1": "small", "T2": "small"}
    sent = asked(ollaya, questions.QUESTION_TEXT["task_size"])
    assert len(sent) == 2 and "Add a.txt" in sent[0]["state"] and "Add b.txt" in sent[1]["state"]
    assert h.orch.task_size_hints() == {"T1": "small", "T2": "small"}
    assert len(ollaya.requests) == 2


def test_task_size_hint_is_none_when_unsure(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "big", 0.30
    h = harness("happy_path", decider=laya({"task_size": 0.5}))
    h.to_models()
    assert h.orch.task_size_hints() == {"T1": None, "T2": None}
    assert len(ollaya.requests) == 2


def test_task_size_hint_is_none_for_an_unreliable_key_without_asking(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "big", 0.95
    h = harness("happy_path", decider=laya({"agent_progress": 0.45}))
    h.to_models()
    assert h.orch.task_size_hints() == {"T1": None, "T2": None}
    assert ollaya.requests == []


def test_task_size_hint_is_none_without_a_decider(harness):
    h = harness("happy_path")
    h.to_models()
    assert h.orch.task_size_hints() == {"T1": None, "T2": None}


def test_task_size_hint_never_changes_the_models(harness, laya, ollaya):
    ollaya.choose, ollaya.confidence = "big", 0.95
    h = harness("happy_path", decider=laya({"task_size": 0.5}))
    h.to_models()
    assert h.orch.task_size_hints() == {"T1": "big", "T2": "big"}
    assert all(t.model is None and t.effort is None for t in h.run().tasks)
    assert h.run().status == "awaiting_models"
