"""The WF banner `wf` prints once before it hands the terminal to Claude Code."""

from __future__ import annotations

import os
import re

from workforce.console.logo import BOTTOM_RGB, LETTERS, RESET, TOP_RGB, _colour_code, supports_truecolor

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
GAP = "   "
DIM = "\x1b[2m"
BOLD = "\x1b[1m"


def _gradient(step: int, steps: int) -> tuple[int, int, int]:
    ratio = step / (steps - 1)
    return tuple(round(a + (b - a) * ratio) for a, b in zip(TOP_RGB, BOTTOM_RGB))


def text_lines(codex_model: str, codex_effort: str, cwd: str, claude: str | None = None) -> list[str | None]:
    """The plain text shown beside the letters, one entry per letter row (None = nothing on that row)."""
    return [
        None,
        "WorkForce · Claude + Codex team",
        f"Claude {claude or '(as configured in Claude Code)'}  │  Codex {codex_model} · {codex_effort}",
        cwd,
        None,
    ]


def render(
    truecolor: bool,
    codex_model: str = "gpt-6-sol",
    codex_effort: str = "high",
    cwd: str | None = None,
    claude: str | None = None,
) -> list[str]:
    """Five rows: the block-letter WF in a top-to-bottom `#5AA8FF` → `#2F6FE0` gradient, with text beside it."""
    cwd = cwd if cwd is not None else os.getcwd()
    beside = text_lines(codex_model, codex_effort, cwd, claude)
    rows = []
    for index, (letters, text) in enumerate(zip(LETTERS, beside)):
        row = _colour_code(_gradient(index, len(LETTERS)), truecolor) + letters + RESET
        if text is not None:
            style = BOLD if index == 1 else DIM if index == 3 else ""
            row += GAP + style + text + (RESET if style else "")
        rows.append(row)
    return rows


def print_banner(codex_model: str, codex_effort: str, environ: dict[str, str] | None = None, stream=None, claude: str | None = None) -> None:
    import sys

    stream = stream or sys.stdout
    plain = bool((environ if environ is not None else os.environ).get("NO_COLOR"))
    for row in render(supports_truecolor(environ), codex_model, codex_effort, claude=claude):
        print(_ANSI.sub("", row) if plain else row, file=stream)
    print(file=stream)
    stream.flush()
