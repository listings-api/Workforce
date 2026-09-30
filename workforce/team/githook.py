"""Git hooks for `wf` sessions: a commit must carry both reviewers' approval, then the user's own hooks run as usual.

`wf` points git's `core.hooksPath` at a folder of small scripts, through `GIT_CONFIG_*` in Claude Code's environment, so
the check runs for every git command started inside `wf` and never for git outside it. `pre-commit` compares the exact
tree git is about to commit (`git write-tree` of the index git uses, after `-a` has staged) with the signed approvals for
the current HEAD. Every hook then hands over to the hook the repository would have run without `wf` (the user's
`core.hooksPath`, or `.git/hooks`), with the original environment, so existing checks keep working.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, MutableMapping

HOOKS_DIRNAME = "git-hooks"
INDEX_ENV = "WF_GIT_HOOKS_INDEX"
HOOK_NAMES = (
    "applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit", "prepare-commit-msg",
    "commit-msg", "post-commit", "pre-rebase", "post-checkout", "post-merge", "pre-push", "pre-receive", "update",
    "proc-receive", "post-receive", "post-update", "reference-transaction", "push-to-checkout", "pre-auto-gc",
    "post-rewrite", "sendemail-validate", "fsmonitor-watchman", "p4-changelist", "p4-prepare-changelist",
    "p4-post-changelist", "p4-pre-submit", "post-index-change",
)


def hooks_dir(home: Path | None = None) -> Path:
    from workforce.team import config

    return config.workforce_dir(home) / HOOKS_DIRNAME


def install(python: str | Path, home: Path | None = None) -> Path:
    """Write one script per git hook name into `~/.workforce/git-hooks`; each runs this module with its own name."""
    folder = hooks_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        path = folder / name
        text = f'#!/bin/sh\nexec "{python}" -m workforce.team.githook {name} "$@"\n'
        if not path.is_file() or path.read_text() != text:
            tmp = folder / f".{name}.tmp"
            tmp.write_text(text)
            tmp.chmod(0o755)
            os.replace(tmp, path)
    return folder


def session_env(environ: Mapping[str, str], folder: Path) -> dict[str, str]:
    """The variables that point git at `folder` for everything started from this environment."""
    try:
        count = int(environ.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        count = 0
    return {
        "GIT_CONFIG_COUNT": str(count + 1),
        f"GIT_CONFIG_KEY_{count}": "core.hooksPath",
        f"GIT_CONFIG_VALUE_{count}": str(folder),
        INDEX_ENV: str(count),
    }


def original_env(environ: Mapping[str, str]) -> dict[str, str]:
    """`environ` without the entry `wf` added, so git behaves as it would outside `wf`."""
    env = dict(environ)
    raw = env.pop(INDEX_ENV, None)
    if raw is None or not raw.isdigit():
        return env
    index = int(raw)
    env.pop(f"GIT_CONFIG_KEY_{index}", None)
    env.pop(f"GIT_CONFIG_VALUE_{index}", None)
    if index == 0:
        env.pop("GIT_CONFIG_COUNT", None)
    else:
        env["GIT_CONFIG_COUNT"] = str(index)
    return env


def _git(args: list[str], env: Mapping[str, str]) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, env=dict(env), timeout=60)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or f"git {' '.join(args)} failed")
    return done.stdout.strip()


def commit_problem(environ: Mapping[str, str], home: Path | None = None) -> str | None:
    """None when the tree about to be committed has both approvals for the current HEAD, else the reason to refuse."""
    from workforce.team import approvals, hooks

    try:
        tree = _git(["write-tree"], environ)
        cwd = Path(_git(["rev-parse", "--show-toplevel"], environ) or os.getcwd())
        status = approvals.status(cwd, home, tree=tree)
    except Exception as exc:
        return f"could not check the reviews for this commit ({type(exc).__name__}: {exc}), so it is refused."
    reason = hooks.commit_reason(status)
    if reason:
        return f"{reason} (checked at commit time: the files being committed are tree {tree[:12]})"
    return None


def _handover(name: str, args: list[str], environ: Mapping[str, str]) -> int:
    """Run the hook the repository would have run without `wf`, with the original environment; 0 when there is none."""
    env = original_env(environ)
    try:
        folder = Path(_git(["rev-parse", "--git-path", "hooks"], env))
    except Exception:
        return 0
    folder = folder if folder.is_absolute() else Path.cwd() / folder
    ours = environ.get(f"GIT_CONFIG_VALUE_{environ.get(INDEX_ENV, '')}")
    if ours and os.path.realpath(folder) == os.path.realpath(ours):
        return 0
    target = folder / name
    if not (target.is_file() and os.access(target, os.X_OK)):
        return 0
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(str(target), [str(target), *args], env)
    return 0


def main(argv: list[str] | None = None, environ: MutableMapping[str, str] | None = None, home: Path | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    environ = os.environ if environ is None else environ
    name, rest = args[0], args[1:]
    if name == "pre-commit":
        problem = commit_problem(environ, home)
        if problem:
            print(f"WorkForce blocked this commit: {problem}", file=sys.stderr)
            return 1
    return _handover(name, rest, environ)


if __name__ == "__main__":
    sys.exit(main())
