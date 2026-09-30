"""Git hooks for `wf` sessions: a commit must carry both reviewers' approval, and the user's own hooks keep running.

`wf` points git's `core.hooksPath` at a folder of small scripts, through `GIT_CONFIG_*` in Claude Code's environment, so
the checks run for every git command started inside `wf` and never for git outside it. Two hooks decide:

* `pre-commit` first runs the hook the repository would have run without `wf` (the user's `core.hooksPath`, or
  `.git/hooks`), with the original environment, then compares the exact tree git is about to commit (`git write-tree` of
  the index as it is after that hook, and after `-a` has staged) with the signed approvals for the current HEAD. A
  formatter hook that rewrites and stages files therefore cannot smuggle unreviewed content past the check.
* `reference-transaction` runs for every ref update, and `--no-verify` does not skip it. Before a branch (or a detached
  HEAD) moves to commits that exist nowhere else, each of those commits must have both approvals for its own tree and
  first parent, or be a clean merge of existing history.

Every other hook hands over to the repository's own hook with the original environment. This is a workflow safeguard
against unreviewed commits made by mistake, not a security boundary.
"""

from __future__ import annotations

import os
import re
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


ZEROS = re.compile(r"^0+$")
REF_STATE = "prepared"


def _git(args: list[str], env: Mapping[str, str], stdin: str | None = None) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, env=dict(env), timeout=60, input=stdin)
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


def _repo_hook(name: str, environ: Mapping[str, str]) -> tuple[Path | None, dict[str, str]]:
    """(the hook the repository would have run without `wf`, or None; the original environment to run it with)."""
    env = original_env(environ)
    try:
        folder = Path(_git(["rev-parse", "--git-path", "hooks"], env))
    except Exception:
        return None, env
    folder = folder if folder.is_absolute() else Path.cwd() / folder
    ours = environ.get(f"GIT_CONFIG_VALUE_{environ.get(INDEX_ENV, '')}")
    if ours and os.path.realpath(folder) == os.path.realpath(ours):
        return None, env
    target = folder / name
    if not (target.is_file() and os.access(target, os.X_OK)):
        return None, env
    return target, env


def _run_repo_hook(target: Path, args: list[str], env: Mapping[str, str], stdin: str | None = None) -> int:
    """Run `target` as a child, output and (unless `stdin` is given) input passed straight through; its exit status."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        done = subprocess.run([str(target), *args], env=dict(env), input=stdin, text=True)
    except OSError as exc:
        print(f"WorkForce could not run the repository hook {target.name}: {exc}", file=sys.stderr)
        return 1
    return done.returncode if done.returncode >= 0 else 1


def _handover(name: str, args: list[str], environ: Mapping[str, str]) -> int:
    """Replace this process with the hook the repository would have run without `wf`; 0 when there is none."""
    target, env = _repo_hook(name, environ)
    if target is None:
        return 0
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(str(target), [str(target), *args], env)
    return 0


def _pre_commit(rest: list[str], environ: Mapping[str, str], home: Path | None) -> int:
    target, env = _repo_hook("pre-commit", environ)
    if target is not None:
        status = _run_repo_hook(target, rest, env)
        if status:
            return status
    problem = commit_problem(environ, home)
    if problem:
        print(f"WorkForce blocked this commit: {problem}", file=sys.stderr)
        return 1
    return 0


def parse_ref_updates(text: str) -> list[tuple[str, str, str]]:
    """`(old, new, ref)` for each `<old> <new> <ref>` line of a reference-transaction hook's input."""
    updates = []
    for line in text.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) == 3 and parts[2]:
            updates.append((parts[0], parts[1], parts[2]))
    return updates


def checked_updates(updates: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """The updates that move a branch or the HEAD to a commit: not deletions, not symbolic-ref changes, not other refs."""
    return [
        (old, new, ref)
        for old, new, ref in updates
        if (ref == "HEAD" or ref.startswith("refs/heads/")) and not ZEROS.match(new) and not new.startswith("ref:") and not old.startswith("ref:")
    ]


def _existing_tips(environ: Mapping[str, str], transaction_refs: set[str]) -> list[str]:
    """Commits that branches and remote-tracking refs outside this transaction (and HEAD, unless it is in it) already point at."""
    out = _git(
        ["for-each-ref", "--format=%(refname)%09%(objecttype)%09%(objectname)%09%(*objecttype)%09%(*objectname)", "refs/heads", "refs/remotes"],
        environ,
    )
    tips: list[str] = []
    for line in out.splitlines():
        ref, kind, sha, peeled_kind, peeled = (line.split("\t") + ["", "", "", "", ""])[:5]
        if ref in transaction_refs:
            continue
        if kind == "commit":
            tips.append(sha)
        elif kind == "tag" and peeled_kind == "commit":
            tips.append(peeled)
    if "HEAD" not in transaction_refs:
        try:
            tips.append(_git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], environ))
        except RuntimeError:
            pass
    return tips


def _fresh_commits(new: str, excluded: list[str], environ: Mapping[str, str]) -> list[tuple[str, str, list[str]]]:
    """`(commit, tree, parents)` for each commit reachable from `new` and from none of `excluded`."""
    stdin = "\n".join([new, *(f"^{sha}" for sha in excluded)]) + "\n"
    out = _git(["rev-list", "--stdin", "--format=%H %T %P"], environ, stdin=stdin)
    found = []
    for line in out.splitlines():
        parts = line.split()
        if not parts or line.startswith("commit ") or len(parts) < 2:
            continue
        found.append((parts[0], parts[1], parts[2:]))
    return found


def _clean_merge_tree(parents: list[str], environ: Mapping[str, str]) -> str | None:
    """The tree of a clean merge of two existing commits (`git merge-tree --write-tree`, git 2.38+), else None."""
    if len(parents) != 2:
        return None
    done = subprocess.run(
        ["git", "merge-tree", "--write-tree", *parents], capture_output=True, text=True, env=dict(environ), timeout=60
    )
    if done.returncode != 0:
        return None
    first = done.stdout.strip().splitlines()[:1]
    return first[0].strip() if first else None


def _commit_ref_problem(commit: str, tree: str, parents: list[str], cwd: Path, environ: Mapping[str, str], home: Path | None) -> str | None:
    from workforce.team import approvals, hooks

    if len(parents) > 1 and _clean_merge_tree(parents, environ) == tree:
        return None
    status = approvals.status(cwd, home, tree=tree, base=parents[0] if parents else None)
    reason = hooks.commit_reason(status)
    if reason is None:
        return None
    kind = "merge commit that is not a clean merge of existing commits" if len(parents) > 1 else "commit"
    return f"{kind} {commit[:12]} (tree {tree[:12]}) has no review: {reason}"


def ref_transaction_problem(updates: list[tuple[str, str, str]], environ: Mapping[str, str], home: Path | None = None) -> str | None:
    """None when every branch/HEAD update in `updates` only reaches reviewed or already existing commits, else the reason."""
    relevant = checked_updates(updates)
    if not relevant:
        return None
    try:
        try:
            cwd = Path(_git(["rev-parse", "--show-toplevel"], environ))
        except RuntimeError:
            cwd = Path(os.getcwd())
        transaction_refs = {ref for _, _, ref in updates}
        excluded = _existing_tips(environ, transaction_refs)
        excluded += [old for old, _, _ in relevant if not ZEROS.match(old) and not old.startswith("ref:")]
        seen: set[str] = set()
        for _, new, ref in sorted(relevant, key=lambda update: update[2] == "HEAD"):
            if new in seen:
                continue
            seen.add(new)
            for commit, tree, parents in _fresh_commits(new, excluded, environ):
                problem = _commit_ref_problem(commit, tree, parents, cwd, environ, home)
                if problem:
                    return f"{ref} would move to {new[:12]}, which brings in {problem}"
    except Exception as exc:
        return f"could not check the reviews for this ref update ({type(exc).__name__}: {exc}), so it is refused."
    return None


def _reference_transaction(rest: list[str], environ: Mapping[str, str], home: Path | None) -> int:
    state = rest[0] if rest else ""
    data = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    if state == REF_STATE:
        problem = ref_transaction_problem(parse_ref_updates(data), environ, home)
        if problem:
            print(f"WorkForce blocked this ref update: {problem}", file=sys.stderr)
            return 1
    target, env = _repo_hook("reference-transaction", environ)
    if target is None:
        return 0
    return _run_repo_hook(target, rest, env, stdin=data)


def main(argv: list[str] | None = None, environ: MutableMapping[str, str] | None = None, home: Path | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    environ = os.environ if environ is None else environ
    name, rest = args[0], args[1:]
    if name == "pre-commit":
        return _pre_commit(rest, environ, home)
    if name == "reference-transaction":
        return _reference_transaction(rest, environ, home)
    return _handover(name, rest, environ)


if __name__ == "__main__":
    sys.exit(main())
