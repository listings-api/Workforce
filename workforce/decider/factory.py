"""Build the decider the orchestrator and CLI use from `[decider]` config, in one place."""

from workforce.config import Config
from workforce.decider.base import Decider
from workforce.decider.laya import build_decider
from workforce.events import EventLog


def make_decider(config: Config, events: EventLog) -> Decider:
    """A LayaDecider (with the config's per-key thresholds and models) for backend "laya", else a NullDecider."""
    return build_decider(config.decider, events)
