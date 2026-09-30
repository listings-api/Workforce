"""Terminal control sequences from model or repo text never reach the screen."""

import io

import pytest
from rich.console import Console

from workforce import render_text
from workforce.console import render
from workforce.render_text import EventFormatter, sanitize
from workforce.state import Question, Run, Task

ESC = "\x1b"


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("plain text", "plain text"),
        (f"a{ESC}[31mred{ESC}[0m b", "ared b"),
        (f"a{ESC}[2J{ESC}[H b", "a b"),
        (f"x{ESC}]0;evil title\x07y", "xy"),
        (f"x{ESC}]8;;http://evil{ESC}\\link{ESC}]8;;{ESC}\\y", "xlinky"),
        (f"a{ESC}Pq#0;2;0;0;0{ESC}\\b", "ab"),
        (f"a{ESC}_app data{ESC}\\b", "ab"),
        (f"a{ESC}(Bb", "ab"),
        (f"a{ESC}cb", "ab"),
        ("bell\x07 backspace\x08 formfeed\x0c cr\r nul\x00", "bell backspace formfeed cr nul"),
        ("keeps\ttabs\nand newlines", "keeps\ttabs\nand newlines"),
        ("\x9b31mc1 csi", "c1 csi"),
        ("del\x7fchar", "delchar"),
        (ESC, ""),
        (f"{ESC}[", ""),
        (f"trailing{ESC}[31", "trailing"),
        ("unicode ✓ ❓ é 日本 😀 stays", "unicode ✓ ❓ é 日本 😀 stays"),
    ],
)
def test_sanitize(raw, clean):
    assert sanitize(raw) == clean


def test_sanitize_accepts_non_strings():
    assert sanitize(None) == "None"
    assert sanitize(ValueError(f"bad{ESC}[31m")) == "bad"


def test_clip_strips_control_sequences():
    assert render_text.clip(f"a{ESC}[31mb\nc", 50) == "ab c"


def printed(fn) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=200, highlight=False)
    fn(console)
    return buffer.getvalue()


def test_print_lines_and_checks_are_sanitized():
    assert ESC not in printed(lambda c: render_text.print_lines(c, [f"hello{ESC}[31m x", f"{ESC}]0;t\x07ok"]))
    assert ESC not in printed(lambda c: render_text.print_check(c, False, f"bad{ESC}[2J"))


def test_the_event_formatter_sanitizes_every_line_and_unknown_kinds():
    formatter = EventFormatter()
    events = [
        {"kind": "note", "text": f"n{ESC}[31mote"},
        {"kind": "error", "message": f"boom{ESC}]0;t\x07"},
        {"kind": "question", "question": "Q1", "text": f"why{ESC}[H"},
        {"kind": "made_up", "text": f"x{ESC}[31m"},
    ]
    for event in events:
        for line in formatter.format(event):
            assert ESC not in line and "\x07" not in line


def test_questions_status_and_tables_are_sanitized():
    run = Run.create("r1", f"goal{ESC}[31m")
    run.pause_reason = f"why{ESC}]0;t\x07"
    run.tasks = [Task(id="T1", title=f"title{ESC}[2J", description="d")]
    run.questions = [Question(id="Q1", task_id="T1", text=f"q{ESC}[H", positions={f"a{ESC}[1m": f"pos{ESC}[31m\nline2"})]
    assert ESC not in printed(lambda c: render_text.print_status(c, run))
    assert ESC not in printed(lambda c: render_text.print_questions(c, run, True))
    assert ESC not in printed(lambda c: c.print(render_text.plan_table(run)))
    run.questions[0].answer = f"ans{ESC}[31m"
    run.questions[0].status = "answered"
    assert ESC not in printed(lambda c: render_text.print_questions(c, run, True))


def test_usage_output_is_sanitized():
    readings = {"sources": {f"claude{ESC}[31m": {"freshness": "unknown", "error": f"err{ESC}[2J"}}, "windows": []}
    assert ESC not in printed(lambda c: render_text.print_usage(c, readings, f"summary{ESC}[31m"))


def test_console_render_lines_carry_only_our_own_styling():
    formatter = EventFormatter()
    lines = render.event_lines(formatter, {"kind": "note", "text": f"hi{ESC}[31m there"}, color=False)
    assert all(ESC not in line for line in lines)
    question = {"kind": "question", "question": "Q1", "text": "t", "positions": {"claude": f"pos{ESC}[31m"}}
    assert all(ESC not in line for line in render.event_lines(formatter, question, color=False))
    run = Run.create("r1", f"goal{ESC}[31m")
    run.pause_reason = f"why{ESC}[2J"
    run.status = "paused"
    run.tasks = [Task(id="T1", title=f"t{ESC}[H", description="d")]
    assert all(ESC not in line for line in render.status_lines(run, None, "auto"))
    assert all(ESC not in line for line in render.plan_lines(run, {}, type("R", (), {"model": "m", "effort": "high"})()))
    assert ESC not in render.state_text(run)
