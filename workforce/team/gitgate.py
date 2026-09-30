"""Analyse a Bash command for the commit gate: which repos would get a commit, and what to refuse outright.

`analyse` walks the command the way a shell would (segments, `cd`, `sh -c`, `xargs`, `find -exec`, `$(…)`, shell keywords,
env assignments, wrappers) and returns the git operations that need both reviewers' approval (`Op`) or a deny reason.
Anything that writes history and cannot be parsed fails closed (see `_fallback`).

The gate protects against accidents, not against a determined agent: indirection through arbitrary programs cannot be
parsed. The git-level checks `wf` adds (`workforce.team.githook`) back it up for every git command started inside `wf`.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from workforce.team import denylist as risk

MAX_DEPTH = 6
COMMIT_LIKE = frozenset({"commit", "merge", "cherry-pick", "revert", "am"})
DENY_HISTORY = {
    "commit-tree": "builds a commit from an arbitrary tree, outside the review gate; stage the changes and use `git commit` after /review",
    "rebase": "rewrites history outside the review gate",
    "filter-branch": "rewrites history outside the review gate",
    "filter-repo": "rewrites history outside the review gate",
    "update-ref": "moves branch tips to arbitrary commits outside the review gate",
    "replace": "replaces commits or objects outside the review gate",
    "notes": "writes commits into refs/notes outside the review gate",
    "stash": "creates commits outside the review gate (stash push/save/create); commit after /review or discard instead",
}
NOTES_READ = frozenset({"list", "show", "get-ref"})
STASH_SAFE = frozenset({"list", "show", "pop", "apply", "drop", "clear", "branch", "store"})
BENIGN_FLAGS = frozenset({"--abort", "--quit"})
CLEAN_OK = frozenset({"merge"})
TREE_SAFE_GIT = risk.READ_ONLY_GIT | frozenset({"add", "fetch", "remote", "config", "notes", "reflog", "worktree", "stash"})
SHELL_BUILTINS = frozenset(
    {"cd", "pushd", "popd", "export", "unset", "set", "exit", "return", "read", "sleep", "wait", ":", "[[", "local", "declare", "alias", "true", "false", "test", "["}
)
HARMLESS_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
ENV_CLEARING = frozenset({"-i", "-", "--ignore-environment", "-u", "--unset"})
BRANCH_NOT_MOVE = frozenset(
    {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-l", "--list", "-u", "--set-upstream-to", "--unset-upstream", "--edit-description"}
)
FORCE_CREATE = {"checkout": ("-B",), "switch": ("-C", "--force-create")}
GIT_BUILTINS = frozenset(
    "add am annotate apply archive bisect blame branch bundle cat-file checkout cherry cherry-pick clean clone commit "
    "commit-graph commit-tree config count-objects describe diff diff-files diff-index diff-tree difftool fast-import "
    "fetch filter-branch for-each-ref format-patch fsck gc grep hash-object help init log ls-files ls-remote ls-tree "
    "maintenance merge merge-base mergetool mv name-rev notes pack-objects prune pull push range-diff read-tree rebase "
    "reflog remote repack replace request-pull reset restore rev-list rev-parse revert rm scalar send-email shortlog "
    "show show-branch show-ref sparse-checkout stash status submodule subtree switch symbolic-ref tag update-index "
    "update-ref version whatchanged worktree write-tree filter-repo".split()
)
GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"})
LEADING_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "{", "}", "!", "coproc"})
SKIPPED_HEADS = frozenset({"fi", "done", "esac", "for", "case", "select", "function"})
INERT = risk.READ_ONLY_COMMANDS | frozenset(
    {"cd", "pushd", "popd", "export", "unset", "set", "exit", "return", "read", "sleep", "wait", ":", "[[", "local", "declare", "alias"}
)
COMMIT_VALUE_LONG = frozenset(
    {"--message", "--file", "--reuse-message", "--reedit-message", "--author", "--date", "--template", "--cleanup", "--trailer", "--fixup", "--squash"}
)
COMMIT_VALUE_SHORT = "mFCct"

_REDIRECT = re.compile(r"^(?:\d*|&)?(?:>>|>\||>&|>|<<<|<<-?|<&|<>|<)(.*)$")
_HISTORY_WORDS = re.compile(r"(?<![\w-])(commit-tree|filter-branch|update-ref|cherry-pick|commit|merge|rebase|revert|replace|notes|stash)(?![\w-])")
_HISTORY_LOOSE = re.compile(r"commit-tree|filter-branch|update-ref|cherry-pick|commit|merge|rebase|revert|replace|notes|stash")
_GIT_AM = re.compile(r"(?<![\w-])git(?:\s+-\S+)*\s+am(?![\w-])|git-am(?![\w-])")
_GIT_AM_LOOSE = re.compile(r"git[^;&|]*?\bam\b")
_GIT_WORD = re.compile(r"(?<![\w-])git(?![\w-])")
_STAGE_ALL_TEXT = re.compile(r"add\s+(?:-\w*a\w*|--all|\.)(?!\w)|commit\s+(?:-\w*a\w*|--all)(?!\w)")
_REWRITE = frozenset(DENY_HISTORY)
_GH_DENY = re.compile(r"git/commits|git/refs|/merges(?![\w-])|merge-upstream")
_APPROVALS_API = ("workforce.team.approvals", "team.approvals", "approvals.record(", "approvals.load_key", "approvals.sign(")
_GH_MUTATING = re.compile(r"^(?:PUT|POST|PATCH|DELETE)$", re.I)


@dataclass
class Target:
    """Where a git command runs: the directory git is started in, plus GIT_DIR / GIT_WORK_TREE when given."""

    cwd: str
    env: dict[str, str] = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.cwd, tuple(sorted(self.env.items())))


@dataclass
class Op:
    """One git operation that needs both reviewers' approval before it may run."""

    label: str
    target: Target
    stage: str = "index"
    dot_only: bool = False
    clean_ok: bool = False
    rev: str | None = None
    rev_may_be_path: bool = False


@dataclass
class Analysis:
    deny: str | None = None
    ops: list[Op] = field(default_factory=list)


@dataclass
class GitCall:
    cdirs: list[str]
    git_dir: str | None
    work_tree: str | None
    subcommand: str | None
    args: list[str]


def normalise(text: str) -> str:
    """Lowercase, with quotes and backslashes removed (`g"i"t` reads `git`)."""
    return risk._normalise(text).lower()


def raw_segments(command: str) -> list[list[str]]:
    """Simple-command token lists (substitutions first), with env assignments and wrappers still in place."""
    command = risk._strip_heredocs(command)
    result: list[list[str]] = []
    for inner in risk._substitutions(command):
        result.extend(raw_segments(inner))
    flat = risk._flatten(command)
    try:
        tokens = risk._tokenize(flat)
    except ValueError:
        tokens = re.findall(r"[;&|()]+|[^\s;&|()]+", flat)
    current: list[str] = []
    for token in tokens:
        if token and set(token) <= set(";&|()") or token in risk.SEPARATORS:
            if current:
                result.append(current)
            current = []
        else:
            current.append(risk._restore(token))
    if current:
        result.append(current)
    return result


def _split_prefix(tokens: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """(env assignments, the command and its arguments) with shell keywords, assignments and wrappers (`env`, `sudo`, …) removed."""
    assigns: dict[str, str] = {}
    rest = list(tokens)
    while rest:
        head = rest[0]
        name = Path(head).name
        if head in LEADING_KEYWORDS:
            rest = rest[1:]
        elif risk._ENV_ASSIGN.match(head):
            key, _, value = head.partition("=")
            assigns[key] = value
            rest = rest[1:]
        elif name in risk.WRAPPERS:
            rest = risk._skip_wrapper_options(name, rest[1:])
        elif head.startswith("-") and len(rest) > 1:
            rest = rest[1:]
        else:
            break
    return assigns, rest


def _strip_redirects(words: Sequence[str]) -> tuple[list[str], bool]:
    """(the words without redirections, whether a here-string or heredoc feeds this command)."""
    kept: list[str] = []
    fed = False
    index = 0
    while index < len(words):
        match = _REDIRECT.match(words[index])
        index += 1
        if not match:
            kept.append(words[index - 1])
            continue
        if words[index - 1].lstrip("0123456789&").startswith("<<"):
            fed = True
        if not match.group(1) and index < len(words):
            index += 1
    return kept, fed


def parse_git(words: Sequence[str]) -> GitCall:
    """The global options (`-C` accumulates, `--git-dir`, `--work-tree`) and the subcommand of a `git …` command."""
    name = Path(words[0]).name
    cdirs: list[str] = []
    git_dir = work_tree = None
    sub: str | None = name[4:] if name.startswith("git-") and len(name) > 4 else None
    index = 1
    while sub is None and index < len(words):
        token = words[index]
        if token in GIT_VALUE_OPTS:
            value = words[index + 1] if index + 1 < len(words) else ""
            if token == "-C":
                cdirs.append(value)
            elif token == "--git-dir":
                git_dir = value
            elif token == "--work-tree":
                work_tree = value
            index += 2
        elif token.startswith("--git-dir="):
            git_dir = token.split("=", 1)[1]
            index += 1
        elif token.startswith("--work-tree="):
            work_tree = token.split("=", 1)[1]
            index += 1
        elif token.startswith("-"):
            index += 1
        else:
            sub = token
            index += 1
            break
    return GitCall(cdirs, git_dir, work_tree, sub, list(words[index:]))


def parse_commit_args(args: Sequence[str]) -> tuple[bool, bool]:
    """(stages tracked changes itself: `-a`/`--all`, has explicit paths or `-o`/`-i`) for `git commit` arguments."""
    stage_a = paths = False
    after_dashes = False
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if after_dashes:
            paths = True
        elif token == "--":
            after_dashes = True
        elif token.startswith("--"):
            name = token.split("=", 1)[0]
            if name == "--all":
                stage_a = True
            elif name in ("--only", "--include", "--pathspec-from-file"):
                paths = True
            elif name in COMMIT_VALUE_LONG and "=" not in token:
                index += 1
        elif token.startswith("-") and len(token) > 1:
            cluster = token[1:]
            for position, char in enumerate(cluster):
                if char == "a":
                    stage_a = True
                elif char in "io":
                    paths = True
                elif char in COMMIT_VALUE_SHORT:
                    if position == len(cluster) - 1:
                        index += 1
                    break
                elif char in "uS":
                    break
        else:
            paths = True
    return stage_a, paths


def parse_move(sub: str, args: Sequence[str]) -> tuple[str, str, bool] | None:
    """(label, revision, the revision may be a path) when the command points a branch or HEAD at a commit.

    Covers `git reset [<mode>] <commit>`, `git branch -f <name> [<start>]`, `git checkout -B <name> [<start>]` and
    `git switch -C <name> [<start>]`. A reset with paths, or with no commit, does not move anything.
    """
    if sub == "reset":
        if any(a in ("-p", "--patch") or a.startswith("--pathspec-from-file") for a in args):
            return None
        before, after = list(args), []
        if "--" in args:
            cut = args.index("--")
            before, after = list(args[:cut]), list(args[cut + 1:])
        positional = [a for a in before if not a.startswith("-")]
        if after or len(positional) != 1:
            return None
        return "git reset", positional[0], "--" not in args
    if sub == "branch":
        flags = [a.split("=", 1)[0] for a in args if a.startswith("-")]
        if not any(f in ("-f", "--force") for f in flags) or any(f in BRANCH_NOT_MOVE for f in flags):
            return None
        positional = [a for a in args if not a.startswith("-")]
        if not positional:
            return None
        return "git branch -f", positional[1] if len(positional) > 1 else "HEAD", False
    if sub in FORCE_CREATE:
        for index, token in enumerate(args):
            if token in FORCE_CREATE[sub] or token.startswith("--force-create="):
                rest = args[index + (1 if "=" in token else 2):]
                if "--" in rest:
                    rest = rest[: rest.index("--")]
                starts = [a for a in rest if not a.startswith("-")]
                return f"git {sub} {token.split('=', 1)[0]}", starts[0] if starts else "HEAD", False
    return None


def _short_flag(args: Sequence[str], flag: str, value_flags: str) -> bool:
    """True when the short option `flag` appears in `args`, alone or in a cluster, before `--`."""
    for token in args:
        if token == "--":
            return False
        if token.startswith("-") and not token.startswith("--") and len(token) > 1:
            for char in token[1:]:
                if char == flag:
                    return True
                if char in value_flags:
                    break
    return False


def _join_fds(tokens: Sequence[str]) -> list[str]:
    """Rejoin a file-descriptor number with the redirection after it (`2`, `>` → `2>`), which tokenising splits apart."""
    joined: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token.isdigit() and following[:1] in (">", "<") and _REDIRECT.match(following):
            joined.append(token + following)
            index += 2
            continue
        joined.append(token)
        index += 1
    return joined


def _clears_env(tokens: Sequence[str]) -> bool:
    """True when an `env` wrapper in `tokens` clears or unsets environment variables (`env -i`, `env -u NAME`, `env -`)."""
    for index, token in enumerate(tokens):
        if Path(token).name != "env":
            continue
        for option in tokens[index + 1:]:
            if option in ENV_CLEARING or option.startswith("--unset=") or (option.startswith("-u") and len(option) > 2):
                return True
            if not option.startswith("-"):
                break
    return False


def _written_file(tokens: Sequence[str]) -> str | None:
    """The file an output redirection in `tokens` writes to, unless it is /dev/null, a terminal or a file descriptor."""
    for index, token in enumerate(tokens):
        match = _REDIRECT.match(token)
        if not match or "<" in token[: len(token) - len(match.group(1))]:
            continue
        target = match.group(1) or (tokens[index + 1] if index + 1 < len(tokens) else "")
        if not target or target.startswith("&") or target in HARMLESS_TARGETS or target.isdigit():
            continue
        return target
    return None


def _add_stage(args: Sequence[str]) -> str | None:
    """"all" for `git add -A/--all/.`, "dot" for `git add .` alone, "update" for `-u`; None for anything narrower."""
    flags = [a for a in args if a.startswith("-")]
    positional = [a for a in args if not a.startswith("-")]
    if any(f == "--all" or (not f.startswith("--") and "A" in f) for f in flags):
        return "all"
    if any(f == "--update" or (not f.startswith("--") and "u" in f) for f in flags):
        return "update"
    if positional == ["."]:
        return "dot"
    return None


def protects_approvals(command: str) -> str | None:
    """A deny reason when a Bash command mentions the HMAC key, or mentions the approvals file without being a plain read."""
    squashed = re.sub(r"[\s'\"\\+]", "", command.lower())
    if "team.key" in squashed:
        return "touches the WorkForce approvals key (~/.workforce/team.key), which is never read or written by the agent"
    if any(marker in squashed for marker in _APPROVALS_API):
        return "uses the approvals module directly; only the WorkForce server records reviews (claude_review / codex_review)"
    if "wf-approvals" in squashed and not risk.is_read_only("Bash", {"command": command}):
        return "may write wf-approvals.json; review approvals are recorded only by the WorkForce server (claude_review / codex_review)"
    if "review-again" in squashed and not risk.is_read_only("Bash", {"command": command}):
        return "may write the review-again marker; only the user can allow more review rounds, by typing /wf-review-again"
    if "plan-reviews.json" in squashed and not risk.is_read_only("Bash", {"command": command}):
        return "may write plan-reviews.json; plan reviews are recorded only by the WorkForce server (codex_plan_review)"
    return None


class Analyzer:
    def __init__(self, cwd: str, home: Path, env: Mapping[str, str]):
        self.cwd = cwd
        self.home = home
        self.where: str | None = cwd
        self.ambient = {k: v for k, v in env.items() if k in ("GIT_DIR", "GIT_WORK_TREE")}
        self.shell_env: dict[str, str] = {}
        self.analysis = Analysis()
        self.staged: list[tuple[tuple, str]] = []
        self.dynamic = False
        self.own_hits: set[str] = set()
        self.raw = ""
        self.seq = 0
        self.changes: list[tuple[int, str]] = []
        self.commit_seqs: list[int] = []
        self.env_cleared = False
        self.hidden_commit = False


    def run(self, command: str) -> Analysis:
        self.raw = command
        self.feed(command, 0)
        if not self.analysis.deny:
            self._fallback()
        if not self.analysis.deny and self.commit_seqs:
            if self.hidden_commit:
                self.analysis.deny = (
                    "this command runs `git commit` in a form WorkForce cannot read (a variable, eval, a piped or generated "
                    "script, or another program), so it cannot tell what the command changes first. Run a plain "
                    "`git commit` command instead."
                )
            elif self.env_cleared:
                self.analysis.deny = (
                    "this command clears or unsets the environment for a git command (`env -i` / `env -u`); "
                    "WorkForce's commit check lives in that environment, so commit without it."
                )
            else:
                last = max(self.commit_seqs)
                earlier = [text for seq, text in self.changes if seq < last]
                if earlier:
                    self.analysis.deny = (
                        f"this command can change files before it commits (`{earlier[0]}`). The review gate checks the files "
                        "before the command runs, so those changes would be committed unreviewed. Make the change first, "
                        "run /review, then commit in a command of its own (`git add -A && git commit …` is fine)."
                    )
        return self.analysis

    def _change(self, text: str) -> None:
        self.changes.append((self.seq, text))

    def feed(self, command: str, depth: int) -> None:
        for tokens in raw_segments(command):
            if self.analysis.deny:
                return
            self.segment(tokens, depth)

    def segment(self, tokens: Sequence[str], depth: int, dynamic: bool = False) -> None:
        self.seq += 1
        tokens = _join_fds(tokens)
        if _clears_env(tokens):
            self.env_cleared = True
        written = _written_file(tokens)
        if written:
            self._change(f"> {written}")
        assigns, rest = _split_prefix(tokens)
        words, fed = _strip_redirects(rest)
        if not words:
            self.shell_env.update(assigns)
            return
        if words[0] in SKIPPED_HEADS:
            return
        if Path(words[0]).name == "export":
            self.shell_env.update(dict(a.split("=", 1) for a in words[1:] if risk._ENV_ASSIGN.match(a)))
            return
        if depth > MAX_DEPTH:
            self.dynamic = True
            return
        saved = self.shell_env
        self.shell_env = {**saved, **assigns}
        try:
            self.command([w for w in words if w != risk.DYNAMIC], depth, dynamic, fed)
        finally:
            self.shell_env = saved

    def command(self, words: list[str], depth: int, dynamic: bool, fed: bool) -> None:
        head = words[0]
        name = Path(head).name
        args = words[1:]
        if "$" in head or "`" in head:
            self.dynamic = True
            self._change(" ".join(words)[:60])
        elif name in ("eval", "source") or head == ".":
            self.dynamic = True
            self._change(" ".join(words)[:60])
        elif name in ("cd", "pushd"):
            target = args[0] if args else str(self.home)
            self.where = risk._resolve(target, self.where, self.home) if self.where is not None else None
        elif name in risk.SHELLS:
            self._shell(words, depth, fed)
        elif name == "xargs":
            inner = risk._xargs_command(args)
            if inner:
                self.segment(inner, depth + 1, True)
        elif name == "find":
            if any(a in risk.FIND_MUTATORS for a in args if a not in risk.FIND_EXEC):
                self._change(" ".join(words)[:60])
            self._find(args, depth)
        elif name == "git" or name.startswith("git-"):
            self._git(words, dynamic)
        elif name == "gh":
            self._gh(words)
        elif name in SHELL_BUILTINS:
            return
        elif name in INERT:
            if not risk._segment_read_only(list(words)):
                self._change(" ".join(words)[:60])
        else:
            self._change(" ".join(words)[:60])
            self._own(words)

    def _shell(self, words: list[str], depth: int, fed: bool) -> None:
        args = words[1:]
        script = risk._shell_command(args)
        if script is not None:
            saved_where = self.where
            self.feed("".join(risk._QUOTED_ANGLES.get(char, char) for char in script), depth + 1)
            self.where = saved_where
            return
        operands = [a for a in args if not a.startswith(("-", "+"))]
        self._change(" ".join(words)[:60])
        if fed or not operands:
            self.dynamic = True
        else:
            self._own(words)

    def _find(self, args: Sequence[str], depth: int) -> None:
        for index, arg in enumerate(args):
            if arg in risk.FIND_EXEC:
                inner: list[str] = []
                for token in args[index + 1 :]:
                    if token in (";", "+"):
                        break
                    inner.append(token)
                if inner:
                    self.segment(inner, depth + 1)

    def _own(self, words: Sequence[str]) -> None:
        """An external program: only its own text can tell whether it is about to write history."""
        text = normalise(" ".join(words))
        if _GIT_WORD.search(text):
            self.own_hits.update(_HISTORY_WORDS.findall(text))
            if _GIT_AM.search(text):
                self.own_hits.add("am")

    def _gh(self, words: Sequence[str]) -> None:
        args = words[1:]
        text = " ".join(args)
        if args[:2] == ["repo", "sync"]:
            self.analysis.deny = "`gh repo sync` creates commits on the remote outside the review gate."
            return
        if args[:1] == ["api"]:
            if _GH_DENY.search(text):
                self.analysis.deny = "`gh api` on the git commits/refs/merges endpoints writes history outside the review gate."
                return
            method = None
            for index, arg in enumerate(args):
                if arg in ("-X", "--method") and index + 1 < len(args):
                    method = args[index + 1]
                elif arg.startswith("--method="):
                    method = arg.split("=", 1)[1]
            fields = any(a in ("-f", "-F", "--field", "--raw-field", "--input") for a in args)
            if "/contents/" in text and ((method and _GH_MUTATING.match(method)) or fields):
                self.analysis.deny = "`gh api` writing repository contents creates commits on the remote outside the review gate."


    def _target(self, call: GitCall) -> Target | None:
        base = self.where
        for directory in call.cdirs:
            if base is None:
                return None
            base = risk._resolve(directory, base, self.home)
        if base is None:
            return None
        env = {**self.ambient, **{k: v for k, v in self.shell_env.items() if k in ("GIT_DIR", "GIT_WORK_TREE")}}
        if call.git_dir is not None:
            env["GIT_DIR"] = call.git_dir
        if call.work_tree is not None:
            env["GIT_WORK_TREE"] = call.work_tree
        resolved: dict[str, str] = {}
        for key, value in env.items():
            path = risk._resolve(value, base, self.home)
            if path is None:
                return None
            resolved[key] = path
        cwd = resolved.get("GIT_WORK_TREE", base)
        return Target(cwd, resolved)

    def _alias(self, target: Target, name: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", target.cwd, "config", "--get", f"alias.{name}"],
                capture_output=True, text=True, timeout=5, env={**os.environ, **target.env},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None

    def _git(self, words: list[str], dynamic: bool) -> None:
        call = parse_git(words)
        reason = risk._check_git(words, self.where, self.home, risk.MODE_COMMITTER)
        if reason:
            self.analysis.deny = f"{reason}. WorkForce never force-pushes or bypasses signing."
            return
        sub, args = call.subcommand, call.args
        if sub is None:
            return
        if "$" in sub or "`" in sub or sub.startswith("("):
            self.dynamic = True
            return
        target = self._target(call)
        for _ in range(5):
            if sub in GIT_BUILTINS or target is None:
                break
            expansion = self._alias(target, sub)
            if expansion is None:
                self._own(words)
                break
            if expansion.startswith("!"):
                self.feed(expansion[1:], 1)
                return
            try:
                parts = shlex.split(expansion)
            except ValueError:
                self.dynamic = True
                return
            parts = _drop_options(parts)
            if not parts:
                return
            sub, args = parts[0], parts[1:] + args
        if sub not in TREE_SAFE_GIT and sub != "commit":
            self._change(" ".join(words)[:60])
        elif sub == "stash" and not (args and args[0] in ("list", "show")):
            self._change(" ".join(words)[:60])
        if sub == "add":
            mode = _add_stage(args)
            if mode and target is not None:
                self.staged.append((target.key(), mode))
            return
        move = parse_move(sub, args)
        if move is not None:
            label, rev, maybe_path = move
            if target is None:
                self.analysis.deny = f"the repository this `{label}` runs in cannot be resolved (a variable or an unknown directory); use a literal path."
            elif "$" in rev or "`" in rev:
                self.analysis.deny = f"`{label}` to a computed revision cannot be checked; use a literal commit."
            else:
                self.analysis.ops.append(Op(label, target, rev=rev, rev_may_be_path=maybe_path))
            return
        if sub in COMMIT_LIKE:
            if any(a in BENIGN_FLAGS for a in args):
                return
            self._commit_op(sub, args, target)
        elif sub in DENY_HISTORY:
            if sub == "notes" and (not args or args[0] in NOTES_READ):
                return
            if sub == "stash" and args and args[0] in STASH_SAFE:
                return
            if sub == "rebase" and any(a in BENIGN_FLAGS for a in args):
                return
            if sub == "replace" and any(a in ("-l", "--list", "--format") for a in args):
                return
            self.analysis.deny = f"`git {sub}` {DENY_HISTORY[sub]}."

    def _commit_op(self, sub: str, args: Sequence[str], target: Target | None) -> None:
        if target is None:
            self.analysis.deny = f"the repository this `git {sub}` runs in cannot be resolved (a variable or an unknown directory); use a literal path."
            return
        stage_a = False
        if sub == "commit" and any(_is_amend(a) for a in args):
            self.analysis.deny = (
                "`git commit --amend` is refused: make a new commit instead. An amended commit replaces one that was "
                "reviewed against a different base."
            )
            return
        if "--no-verify" in args or (sub == "commit" and _short_flag(args, "n", COMMIT_VALUE_SHORT)):
            self.analysis.deny = f"`git {sub} --no-verify` skips WorkForce's commit-time review check. Commit without it."
            return
        self.commit_seqs.append(self.seq)
        if sub == "commit":
            stage_a, paths = parse_commit_args(args)
            if paths:
                self.analysis.deny = (
                    "`git commit` with explicit paths (or -o / -i) commits a different tree from the reviewed one. "
                    "Stage with `git add`, then run a plain `git commit`."
                )
                return
        stagers = [mode for key, mode in self.staged if key == target.key()]
        op = Op(f"git {sub}", target)
        if sub in CLEAN_OK and "--continue" not in args and not stagers:
            op.clean_ok = True
        if stage_a or "all" in stagers or "update" in stagers:
            op.stage = "working"
        elif "dot" in stagers:
            op.stage, op.dot_only = "working", True
        self.analysis.ops.append(op)


    def _fallback(self) -> None:
        """Text-level backstop: history-writing words next to `git` that the parser could not prove read-only."""
        text = normalise(self.raw)
        hits = set(self.own_hits)
        if self.dynamic and "git" in text:
            hits.update(_HISTORY_LOOSE.findall(text))
            if _GIT_AM_LOOSE.search(text):
                hits.add("am")
        if not hits:
            return
        rewrites = sorted(hits & _REWRITE)
        if rewrites:
            self.analysis.deny = (
                f"this command mentions `git {rewrites[0]}` in a form the gate cannot parse, and it writes history "
                f"outside the review gate ({DENY_HISTORY[rewrites[0]]}). Run the git command directly."
            )
            return
        base = self.where if self.where is not None else self.cwd
        env = {k: risk._resolve(v, base, self.home) or v for k, v in self.ambient.items()}
        op = Op("a git commit the gate could not parse", Target(env.get("GIT_WORK_TREE", base), env))
        self.commit_seqs.append(self.seq + 1)
        self.hidden_commit = True
        if _STAGE_ALL_TEXT.search(text):
            op.stage = "working"
        self.analysis.ops.append(op)


def _is_amend(token: str) -> bool:
    """True for `--amend` and its unambiguous abbreviations (`--am`, `--ame`, …)."""
    name = token.split("=", 1)[0]
    return len(name) >= 4 and name.startswith("--") and "--amend".startswith(name)


def _drop_options(parts: list[str]) -> list[str]:
    """An alias expansion without leading global options (`-c k=v`, `-C dir`, `--no-pager`, …)."""
    index = 0
    while index < len(parts) and parts[index].startswith("-"):
        index += 2 if parts[index] in GIT_VALUE_OPTS else 1
    return parts[index:]


def analyse(command: str, cwd: str, home: Path, env: Mapping[str, str] | None = None) -> Analysis:
    """The gate's view of a Bash command: a deny reason, or the operations that need approval."""
    return Analyzer(cwd, home, env or {}).run(command)


def top_level(target: Target) -> bool:
    """True when the target directory is the top of its work tree."""
    done = subprocess.run(
        ["git", "-C", target.cwd, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, env={**os.environ, **target.env},
    )
    return done.returncode == 0 and os.path.realpath(done.stdout.strip()) == os.path.realpath(target.cwd)
