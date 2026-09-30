"""Menus for picking models and efforts: questions shaped for Claude Code's AskUserQuestion (at most 4 options each)."""

from __future__ import annotations

from typing import Any, Sequence

from workforce.config import EFFORTS
from workforce.team import claude_info
from workforce.team.codex_models import CodexModel, find

MAX_OPTIONS = 4
KINDS = ("codex_model", "codex_effort", "claude_model", "claude_effort")
CLAUDE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-haiku-4-5-20251001")
ROLES = {
    "Reviewer": "The Claude that reviews every change before a commit (claude_review)",
    "Fast coder": "The sub-agent Claude hands small, mechanical tasks to",
    "Both": "Set the reviewer and the fast coder together",
}
EFFORT_TEXT = {
    "low": "Fastest, least thinking",
    "medium": "Balanced",
    "high": "Thorough",
    "xhigh": "Extra thorough",
    "max": "Deepest thinking",
    "ultra": "Most thinking, slowest",
}
MAIN_CHAT_NOTE = "This chat's own Claude model and effort are set with /model (Claude Code's picker; left/right arrows change the effort)."


def _window(values: Sequence[str], current: str | None) -> list[str]:
    """Up to 4 values in their natural order, keeping `current` in view."""
    values = list(values)
    if len(values) <= MAX_OPTIONS:
        return values
    index = values.index(current) if current in values else 0
    start = max(0, min(index - 1, len(values) - MAX_OPTIONS))
    return values[start:start + MAX_OPTIONS]


def _question(question: str, header: str, options: list[dict], rest: Sequence[str]) -> dict:
    if rest:
        question += f" More: choose Other and type {', '.join(rest)}."
    return {"question": question, "header": header, "options": options, "multiSelect": False}


def _mark(current: bool, text: str) -> str:
    return f"Current. {text}".strip() if current else text or "No description"


def codex_model_question(models: list[CodexModel], current: str) -> dict:
    ids = [m.id for m in models]
    ordered = ([current] if current in ids else []) + [i for i in ids if i != current]
    shown, rest = ordered[:MAX_OPTIONS], ordered[MAX_OPTIONS:]
    if current not in ids:
        shown, rest = [current] + ordered[: MAX_OPTIONS - 1], ordered[MAX_OPTIONS - 1:]
    options = []
    for model_id in shown:
        model = find(models, model_id)
        text = model.description if model else "your saved model (not in Codex's current list)"
        options.append({"label": model_id, "description": _mark(model_id == current, text)})
    return _question("Which Codex model should the team use?", "Codex model", options, rest)


def effort_question(efforts: Sequence[str], current: str, who: str) -> dict:
    shown = _window(efforts, current)
    rest = [e for e in efforts if e not in shown]
    options = [{"label": e, "description": _mark(e == current, EFFORT_TEXT.get(e, ""))} for e in shown]
    return _question(f"Which effort for {who}?", "Effort", options, rest)


def claude_role_question() -> dict:
    options = [{"label": label, "description": text} for label, text in ROLES.items()]
    return _question("Which team Claude are you setting? " + MAIN_CHAT_NOTE, "Claude role", options, ())


def _now(value: str, cfg: Any, reviewer_key: str, coder_key: str) -> str:
    users = [who for who, key in (("reviewer", reviewer_key), ("fast coder", coder_key)) if getattr(cfg, key) == value]
    return f"Now: {' and '.join(users)}. " if users else ""


def claude_model_question(cfg: Any) -> dict:
    extra = [m for m in (cfg.reviewer_model, cfg.fast_coder_model) if m not in CLAUDE_MODELS]
    ordered = list(dict.fromkeys(extra + list(CLAUDE_MODELS)))
    shown = ordered[:MAX_OPTIONS]
    rest = [m for m in ordered if m not in shown]
    options = [
        {"label": m, "description": (_now(m, cfg, "reviewer_model", "fast_coder_model") + claude_info.display_name(m)).strip()}
        for m in shown
    ]
    return _question("Which Claude model?", "Claude model", options, rest)


def claude_effort_question(cfg: Any) -> dict:
    efforts = EFFORTS["claude"]
    shown = _window(efforts, cfg.reviewer_effort)
    rest = [e for e in efforts if e not in shown]
    options = [{"label": e, "description": _now(e, cfg, "reviewer_effort", "fast_coder_effort") + EFFORT_TEXT.get(e, "")} for e in shown]
    return _question("Which effort for that Claude?", "Effort", options, rest)


def build(kind: str, cfg: Any, models: list[CodexModel], problem: str | None = None) -> dict:
    """The questions for `kind`, and what to call with the answers."""
    if kind not in KINDS:
        raise ValueError(f"unknown picker '{kind}'; expected one of {list(KINDS)}")
    codex_efforts = find(models, cfg.codex_model).efforts if find(models, cfg.codex_model) else EFFORTS["codex"]
    if kind == "codex_model":
        questions = [codex_model_question(models, cfg.codex_model), effort_question(codex_efforts, cfg.codex_effort, "Codex")]
        then = "Call codex_settings with model = the first answer and effort = the second answer (the exact labels, or the text typed under Other)."
    elif kind == "codex_effort":
        questions = [effort_question(codex_efforts, cfg.codex_effort, f"Codex ({cfg.codex_model})")]
        then = "Call codex_settings with effort = the answer."
    elif kind == "claude_model":
        questions = [claude_role_question(), claude_model_question(cfg), claude_effort_question(cfg)]
        then = (
            "Call team_models: for Reviewer set reviewer_model and reviewer_effort; for Fast coder set fast_coder_model and fast_coder_effort; "
            "for Both set all four. Use the exact labels, or the text typed under Other."
        )
    else:
        questions = [claude_role_question(), claude_effort_question(cfg)]
        then = "Call team_models: for Reviewer set reviewer_effort; for Fast coder set fast_coder_effort; for Both set both."
    result = {
        "questions": questions,
        "then": then + " Then say in one line what is now set.",
        "current": {
            "codex_model": cfg.codex_model,
            "codex_effort": cfg.codex_effort,
            "reviewer_model": cfg.reviewer_model,
            "reviewer_effort": cfg.reviewer_effort,
            "fast_coder_model": cfg.fast_coder_model,
            "fast_coder_effort": cfg.fast_coder_effort,
        },
        "main_chat": MAIN_CHAT_NOTE,
    }
    if problem:
        result["note"] = problem
    return result
