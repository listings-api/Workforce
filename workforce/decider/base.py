from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

KEYS = frozenset({
    "risk_gate",
    "agent_progress",
    "debate_converged",
    "verdict_consistent",
    "needs_human",
    "task_size",
})


@dataclass
class Decision:
    key: str
    question: str
    answer: str | bool | float | None
    confidence: float | None
    source: str
    raw: dict | None = field(default=None)


def is_confident(decision: Decision, threshold: float) -> bool:
    """True when the decision came from Laya with confidence at or above threshold."""
    return (
        decision.source == "laya"
        and decision.answer is not None
        and decision.confidence is not None
        and decision.confidence >= threshold
    )


@runtime_checkable
class Decider(Protocol):
    def available(self) -> bool: ...

    def ask_bool(self, key: str, state: str, question: str) -> Decision: ...

    def ask_choice(self, key: str, state: str, question: str, options: list[str]) -> Decision: ...


class NullDecider:
    """Decider used when the decider backend is off: every answer is unsure."""

    def available(self) -> bool:
        return False

    def ask_bool(self, key: str, state: str, question: str) -> Decision:
        return Decision(key=key, question=question, answer=None, confidence=None, source="off")

    def ask_choice(self, key: str, state: str, question: str, options: list[str]) -> Decision:
        return Decision(key=key, question=question, answer=None, confidence=None, source="off")
