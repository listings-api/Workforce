"""Turn Claude `rate_limit_event` windows into `Window` readings."""

from dataclasses import asdict, dataclass
from typing import Any

KIND_5H = "5h"
KIND_WEEKLY = "weekly"
KIND_OTHER = "other"

_CLAUDE_WINDOWS = {"five_hour": KIND_5H, "seven_day": KIND_WEEKLY}


@dataclass(frozen=True)
class Window:
    """One usage window. `percent` is 0-100; `resets_at` is unix seconds or None if unreported."""

    source: str
    name: str
    percent: float
    resets_at: int | None
    read_at: float
    kind: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.name)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Window":
        return cls(
            source=str(data["source"]),
            name=str(data["name"]),
            percent=float(data["percent"]),
            resets_at=None if data.get("resets_at") is None else int(data["resets_at"]),
            read_at=float(data["read_at"]),
            kind=str(data["kind"]),
        )


def now_value(now: Any) -> float:
    """Accept either a unix timestamp or a clock object with a `.time()` method."""
    return float(now.time()) if hasattr(now, "time") else float(now)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def from_rate_limits(unified_windows: dict | None, now: Any) -> list[Window]:
    """Parse `rate_limit_info.unifiedWindows` (utilization 0-1) into windows.

    Entries without a numeric utilization are skipped rather than guessed at.
    """
    read_at = now_value(now)
    windows: list[Window] = []
    if not isinstance(unified_windows, dict):
        return windows
    for name, info in unified_windows.items():
        if not isinstance(info, dict) or not _is_number(info.get("utilization")):
            continue
        resets = info.get("resetsAt")
        windows.append(
            Window(
                source="claude",
                name=str(name),
                percent=round(float(info["utilization"]) * 100, 6),
                resets_at=int(resets) if _is_number(resets) else None,
                read_at=read_at,
                kind=_CLAUDE_WINDOWS.get(str(name), KIND_OTHER),
            )
        )
    return windows
