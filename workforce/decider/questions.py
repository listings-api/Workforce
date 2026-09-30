"""Question texts, option lists and compact-state builders for each Laya decision key."""

from typing import Any, Mapping, Sequence

FIELD_CHARS = 300
LOG_LINE_CHARS = 160

BOOL_OPTIONS = ("yes", "no")

QUESTION_TEXT = {
    "risk_gate": (
        "Is this tool call risky: does it delete data, force-push, touch secrets or credentials, "
        "touch production or billing, or touch files outside the worktree?"
    ),
    "agent_progress": "Is the agent progressing, stuck, or off task?",
    "debate_converged": "Do these two positions now agree?",
    "verdict_consistent": (
        "Does the review text match its verdict? An APPROVE that lists a blocking bug is not consistent."
    ),
    "needs_human": "Can the agents settle this themselves, or does it need the user?",
    "task_size": "Is this task small or big?",
}

CHOICE_OPTIONS = {
    "agent_progress": ["progressing", "stuck", "off_task"],
    "needs_human": ["agents", "user"],
    "task_size": ["small", "big"],
}

OPTION_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("risk_gate", "yes"): "Risky: it does one of the listed dangerous things",
    ("risk_gate", "no"): "Safe: routine, read-only or inside the worktree",
    ("debate_converged", "yes"): "The two positions agree",
    ("debate_converged", "no"): "The two positions still disagree",
    ("verdict_consistent", "yes"): "The review text supports its verdict",
    ("verdict_consistent", "no"): "The review text contradicts its verdict",
    ("needs_human", "agents"): "The agents can settle it themselves",
    ("needs_human", "user"): "The user must decide",
    ("agent_progress", "progressing"): "Making steady progress toward the task",
    ("agent_progress", "stuck"): "Repeating the same actions or errors without progress",
    ("agent_progress", "off_task"): "Working on something unrelated to the task",
    ("task_size", "small"): "A small change in one or two files",
    ("task_size", "big"): "A large change across many files or a new subsystem",
}


def option_descriptions(key: str, options: Sequence[str]) -> dict[str, str]:
    """Label → description for `options`; a label without a known description describes itself."""
    return {option: OPTION_DESCRIPTIONS.get((key, option), option) for option in options}


def clip(text: object, limit: int = FIELD_CHARS) -> str:
    """One-line, whitespace-collapsed text cut to `limit` characters."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 0)] + "…"


def risk_gate(tool_name: str, tool_input: Mapping[str, Any] | str | None, cwd: str | None = None) -> tuple[str, str, None]:
    lines = [f"Tool: {clip(tool_name, 80)}"]
    if isinstance(tool_input, Mapping):
        for name, value in tool_input.items():
            lines.append(f"{clip(name, 40)}: {clip(value)}")
    elif tool_input is not None:
        lines.append(f"input: {clip(tool_input)}")
    if cwd:
        lines.append(f"Worktree: {clip(cwd, 200)}")
    return "\n".join(lines), QUESTION_TEXT["risk_gate"], None


def agent_progress(log_lines: Sequence[str], task_title: str = "") -> tuple[str, str, list[str]]:
    recent = [clip(line, LOG_LINE_CHARS) for line in list(log_lines)[-40:] if str(line).strip()]
    header = f"Task: {clip(task_title)}\n" if task_title else ""
    state = header + "Last log lines:\n" + "\n".join(recent)
    return state, QUESTION_TEXT["agent_progress"], list(CHOICE_OPTIONS["agent_progress"])


def debate_converged(position_a: str, position_b: str, label_a: str = "A", label_b: str = "B") -> tuple[str, str, None]:
    state = f"Position {label_a}: {clip(position_a, 600)}\nPosition {label_b}: {clip(position_b, 600)}"
    return state, QUESTION_TEXT["debate_converged"], None


def verdict_consistent(verdict: str, findings: Sequence[Mapping[str, Any]], summary: str = "") -> tuple[str, str, None]:
    lines = [f"Verdict: {clip(verdict, 40)}"]
    if summary:
        lines.append(f"Summary: {clip(summary, 500)}")
    if findings:
        lines.append("Findings:")
        for finding in list(findings)[:12]:
            where = clip(finding.get("file", ""), 80)
            line = finding.get("line")
            place = f"{where}:{line}" if where and line else where
            lines.append(f"- [{clip(finding.get('severity', ''), 20)}] {place} {clip(finding.get('message', ''), 200)}".rstrip())
    else:
        lines.append("Findings: none")
    return "\n".join(lines), QUESTION_TEXT["verdict_consistent"], None


def needs_human(question: str, positions: Mapping[str, str] | None = None) -> tuple[str, str, list[str]]:
    lines = [f"Open question: {clip(question, 500)}"]
    for agent, position in (positions or {}).items():
        lines.append(f"{clip(agent, 40)} says: {clip(position, 400)}")
    return "\n".join(lines), QUESTION_TEXT["needs_human"], list(CHOICE_OPTIONS["needs_human"])


def task_size(title: str, description: str, acceptance: Sequence[str] = ()) -> tuple[str, str, list[str]]:
    lines = [f"Task: {clip(title)}", f"Description: {clip(description, 700)}"]
    if acceptance:
        lines.append("Acceptance: " + "; ".join(clip(item, 120) for item in list(acceptance)[:8]))
    return "\n".join(lines), QUESTION_TEXT["task_size"], list(CHOICE_OPTIONS["task_size"])


BUILDERS = {
    "risk_gate": risk_gate,
    "agent_progress": agent_progress,
    "debate_converged": debate_converged,
    "verdict_consistent": verdict_consistent,
    "needs_human": needs_human,
    "task_size": task_size,
}
