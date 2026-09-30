"""`wf demo`: a tiny throwaway project with one real bug, so a first run of `wf` has something to do."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

DEMO_DIRNAME = "workforce-demo"
MARKER = ".workforce-demo"
PROMPT = (
    "This is a WorkForce demo. Fix the failing test: ask Codex for a plan, make the fix, run the test, "
    "get both reviews, then commit."
)
GIT_TIMEOUT_S = 30

MODULE = '''"""A tiny shopping helper."""


def total_price(prices, discount_percent=0):
    """The total of `prices` after taking `discount_percent` percent off."""
    subtotal = sum(prices)
    return round(subtotal + subtotal * discount_percent / 100, 2)
'''

TESTS = '''import unittest

from shopping import total_price


class TotalPriceTest(unittest.TestCase):
    def test_no_discount(self):
        self.assertEqual(total_price([10, 20, 30]), 60)

    def test_empty_basket(self):
        self.assertEqual(total_price([]), 0)

    def test_discount_is_taken_off(self):
        self.assertEqual(total_price([10, 20, 30], 10), 54)


if __name__ == "__main__":
    unittest.main()
'''

README = """# WorkForce demo

This folder was created by `wf demo`. It is a throwaway project with one real bug.

- `shopping.py` has `total_price(prices, discount_percent)`.
- `test_shopping.py` checks it. One test fails, because the discount is added to the total instead of taken off.

Run the test yourself (nothing to install):

    python3 -m unittest

In the demo session, Claude Code asks Codex for a plan, fixes the bug, runs the test, gets a review from each
model, and then commits. WorkForce asks for your approval before that commit.

Delete this folder whenever you like; `wf demo` makes a fresh one.
"""

GITIGNORE = "__pycache__/\n*.pyc\n"
FILES = {
    "shopping.py": MODULE,
    "test_shopping.py": TESTS,
    "README.md": README,
    ".gitignore": GITIGNORE,
    MARKER: "Created by `wf demo`; safe to delete.\n",
}


class DemoError(Exception):
    """The demo folder cannot be used."""


def _git(target: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", *args],
        cwd=target,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise DemoError(f"git {' '.join(args)} failed in {target}: {(proc.stderr or proc.stdout).strip()}")


def _clear(target: Path) -> None:
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def prepare(target: Path, force: bool = False) -> None:
    """Make `target` ready for the demo files: create it, or empty a previous demo; refuse other non-empty folders."""
    if target.exists() and not target.is_dir():
        raise DemoError(f"{target} exists and is not a folder; pick another with --dir")
    if not target.exists():
        target.mkdir(parents=True)
        return
    if not any(target.iterdir()):
        return
    if (target / MARKER).is_file():
        _clear(target)
        return
    if not force:
        raise DemoError(
            f"{target} is not empty and was not made by `wf demo`, so it was left alone. "
            "Pick another folder with --dir, or pass --force to add the demo files to it."
        )
    if (target / ".git").exists():
        raise DemoError(f"{target} is already a git repository; pick another folder with --dir")


def create(target: Path, force: bool = False) -> Path:
    """Write the demo project into `target` and make its one commit (a throwaway repo, never signed)."""
    target = Path(target).expanduser()
    prepare(target, force)
    for name, text in FILES.items():
        (target / name).write_text(text, encoding="utf-8")
    _git(target, "init", "-b", "main")
    _git(target, "config", "user.name", "WorkForce demo")
    _git(target, "config", "user.email", "demo@workforce.invalid")
    _git(target, "config", "commit.gpgsign", "false")
    _git(target, "add", "-A")
    _git(target, "-c", "commit.gpgsign=false", "commit", "-m", "Start the WorkForce demo project")
    return target


def _default_launcher(home: Path | None, environ: Mapping[str, str] | None) -> Callable[[list[str]], int]:
    def run(argv: list[str]) -> int:
        from workforce.team import launch

        return launch.main(argv, home=home, environ=environ)

    return run


def main(
    args: Sequence[str] | None = None,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    launcher: Callable[[list[str]], int] | None = None,
    stdout=None,
    stderr=None,
) -> int:
    parser = argparse.ArgumentParser(prog="wf demo", description="Try WorkForce on a small throwaway project.")
    parser.add_argument("--dir", help=f"where to make the demo (default: ~/{DEMO_DIRNAME})")
    parser.add_argument("--force", action="store_true", help="use a folder that is not empty (it is never deleted)")
    parser.add_argument("--no-launch", action="store_true", help="only create the folder; do not start wf")
    try:
        options = parser.parse_args(list(args or []))
    except SystemExit as exc:
        return int(exc.code or 0)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    base = Path(home) if home is not None else Path.home()
    target = Path(options.dir).expanduser() if options.dir else base / DEMO_DIRNAME
    try:
        target = create(target, options.force)
    except (DemoError, OSError, subprocess.SubprocessError) as exc:
        print(f"wf demo: {exc}", file=err)
        return 2
    print(f"Demo project: {target}", file=out)
    if options.no_launch:
        print(f"Start it with: cd {target} && wf", file=out)
        return 0
    print("Claude will plan the fix with Codex, make it, run the test, get two reviews, then commit.", file=out)
    print("This uses a little of your Claude and ChatGPT quota.", file=out)
    os.chdir(target)
    launch = launcher or _default_launcher(home, environ)
    return launch([PROMPT])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
