"""No-model decider: returns the SPEC section 9 "when unsure" behaviour for each key."""

from dataclasses import dataclass

from workforce.decider.base import KEYS, Decision, is_confident


@dataclass(frozen=True)
class Unsure:
    answer: str | bool | None
    action: str


UNSURE: dict[str, Unsure] = {
    "risk_gate": Unsure(True, "block_and_create_question"),
    "agent_progress": Unsure("unsure", "flag_to_user"),
    "debate_converged": Unsure(False, "keep_going"),
    "verdict_consistent": Unsure(False, "treat_as_not_approved_rerun_reviewer_once"),
    "needs_human": Unsure("user", "ask_user"),
    "task_size": Unsure(None, "show_no_hint"),
}

assert set(UNSURE) == KEYS


class FallbackDecider:
    """Answers every key with its "when unsure" behaviour and `source="fallback"`. Makes no model calls."""

    def available(self) -> bool:
        return False

    def ask_bool(self, key: str, state: str, question: str) -> Decision:
        return self.decide(key, question)

    def ask_choice(self, key: str, state: str, question: str, options: list[str]) -> Decision:
        return self.decide(key, question)

    def decide(self, key: str, question: str = "") -> Decision:
        """The fallback decision for `key`; raises KeyError for a key outside SPEC section 9."""
        if key not in UNSURE:
            raise KeyError(f"unknown decision key '{key}'; expected one of {sorted(UNSURE)}")
        unsure = UNSURE[key]
        return Decision(
            key=key,
            question=question,
            answer=unsure.answer,
            confidence=None,
            source="fallback",
            raw={"action": unsure.action},
        )


def settle(decision: Decision, threshold: float) -> Decision:
    """The decision to act on: `decision` if it is confident, else the fallback for its key."""
    if is_confident(decision, threshold):
        return decision
    return FallbackDecider().decide(decision.key, decision.question)
