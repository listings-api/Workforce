"""The WorkForce logo: the block letters `WF` in a top-to-bottom blue gradient."""

from __future__ import annotations

import os

TOP_RGB = (0x5A, 0xA8, 0xFF)
BOTTOM_RGB = (0x2F, 0x6F, 0xE0)
TRUECOLOR_VALUES = ("truecolor", "24bit")
RESET = "\x1b[0m"
CUBE_STEPS = (0, 95, 135, 175, 215, 255)

LETTERS = (
    "██╗    ██╗███████╗",
    "██║    ██║██╔════╝",
    "██║ █╗ ██║█████╗  ",
    "██║███╗██║██╔══╝  ",
    "╚███╔███╔╝██║     ",
)


def supports_truecolor(environ: dict[str, str] | None = None) -> bool:
    """True when `COLORTERM` says the terminal understands 24-bit colour."""
    env = os.environ if environ is None else environ
    return env.get("COLORTERM", "") in TRUECOLOR_VALUES


def _gradient(step: int, steps: int) -> tuple[int, int, int]:
    ratio = step / (steps - 1)
    return tuple(round(a + (b - a) * ratio) for a, b in zip(TOP_RGB, BOTTOM_RGB))


def _nearest_256(rgb: tuple[int, int, int]) -> int:
    indices = [min(range(len(CUBE_STEPS)), key=lambda i: abs(CUBE_STEPS[i] - channel)) for channel in rgb]
    return 16 + 36 * indices[0] + 6 * indices[1] + indices[2]


def _colour_code(rgb: tuple[int, int, int], truecolor: bool) -> str:
    if truecolor:
        return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    return f"\x1b[38;5;{_nearest_256(rgb)}m"


def render_logo(truecolor: bool) -> list[str]:
    """Five lines of `WF`, each one colour of a top-to-bottom `#5AA8FF` → `#2F6FE0` gradient."""
    return [_colour_code(_gradient(index, len(LETTERS)), truecolor) + row + RESET for index, row in enumerate(LETTERS)]


def logo_width() -> int:
    return len(LETTERS[0])
