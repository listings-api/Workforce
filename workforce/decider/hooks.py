"""PreToolUse risk-gate hook for Claude and Codex: `python -m workforce.decider.hooks claude|codex`."""

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

import workforce
from workforce import config as wf_config
from workforce.decider import questions
from workforce.decider.base import Decider, NullDecider
from workforce.decider.laya import build_decider, can_answer, confident
from workforce.errors import ConfigError
from workforce.events import EventLog
from workforce.paths import INTEGRATION_DIR, Paths

AGENTS = ("claude", "codex")
HOOK_TIMEOUT_S = 30
PROTECTED_BRANCHES = ("main", "master")
MODE_ENV = "WF_HOOK_MODE"
MODE_AGENT = "agent"
MODE_COMMITTER = "committer"
MODE_DENYLIST_ONLY = "denylist"
MODES = (MODE_AGENT, MODE_COMMITTER)

READ_ONLY_TOOLS = frozenset(
    {"Read", "Glob", "Grep", "LS", "NotebookRead", "TodoRead", "TodoWrite", "ToolSearch", "WebSearch", "WebFetch"}
)
FILE_TOOLS = frozenset({"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead", "Glob", "Grep", "LS"})
PATH_KEYS = ("file_path", "path", "notebook_path", "pattern", "glob")

READ_ONLY_COMMANDS = frozenset(
    {
        "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "pwd", "echo", "printf", "which",
        "whoami", "date", "stat", "file", "du", "df", "tree", "sort", "uniq", "cut", "tr", "diff", "cmp",
        "basename", "dirname", "realpath", "readlink", "true", "false", "test", "[", "uname", "nl", "jq",
        "column", "less", "more", "find", "sed",
    }
)
READ_ONLY_GIT = frozenset(
    {"status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree", "blame", "describe", "shortlog",
     "cat-file", "merge-base", "rev-list", "branch", "tag"}
)
GIT_LISTING_FLAGS = frozenset({"-a", "-r", "-v", "-vv", "--list", "-l", "--all", "--remotes", "--show-current"})
GIT_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
FIND_MUTATORS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf"})
WRAPPERS = frozenset({"sudo", "doas", "env", "command", "exec", "nohup", "time", "nice", "builtin", "setsid", "timeout"})
WRAPPER_VALUE_FLAGS = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-T", "-r", "-t", "-U"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k"}),
}
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh"})
EVAL_COMMANDS = frozenset({"eval", "source"})
SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "|&", "\n"})
XARGS_OPTS_WITH_VALUE = frozenset({"-I", "-J", "-L", "-n", "-P", "-s", "-E", "-a", "-d", "-R", "-S"})
XARGS_LONG_WITH_VALUE = frozenset(
    {"--arg-file", "--delimiter", "--max-args", "--max-lines", "--max-procs", "--max-chars", "--eof"}
)
FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
FIND_START_OPTIONS = frozenset({"-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x"})
AGENT_DENIED_GIT = frozenset(
    {
        "commit", "push", "merge", "rebase", "reset", "stash", "cherry-pick", "am", "tag", "update-ref",
        "revert", "commit-tree", "filter-branch", "fast-import", "pull",
    }
)
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
PATCH_KEYS = ("command", "cmd", "input", "patch")
TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")
NULL_DEVICE = "/dev/null"
WRITE_ALL_ARGS = frozenset({"rm", "rmdir", "mkdir", "touch", "chmod", "chown", "chgrp", "truncate", "unlink", "shred", "tee"})
WRITE_VALUE_FLAGS = {
    "mkdir": frozenset({"-m", "--mode"}),
    "touch": frozenset({"-t", "-d", "-r", "--date", "--reference", "--time"}),
    "truncate": frozenset({"-s", "-r", "--size", "--reference"}),
    "chmod": frozenset({"--reference"}),
    "chown": frozenset({"--reference"}),
    "chgrp": frozenset({"--reference"}),
}
COPY_LIKE = frozenset({"cp", "mv", "ln", "install"})
COPY_VALUE_FLAGS = frozenset({"-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group", "--backup"})
TARGET_DIR_FLAGS = frozenset({"-t", "--target-directory"})
SED_VALUE_FLAGS = frozenset({"-e", "-f", "-l", "--expression", "--file", "--line-length"})
PROTECTED_DIRS = (".ssh", ".codex", ".claude", ".gnupg")
PROTECTED_NAMES = (*PROTECTED_DIRS, ".claude.json")
DYNAMIC = "\x1fstdin-args"
_PLACEHOLDERS = {";": "\ue000", "(": "\ue001", ")": "\ue002", "&": "\ue003", "|": "\ue004", ">": "\ue005", "<": "\ue006"}
_RESTORE = {value: key for key, value in _PLACEHOLDERS.items() if key not in "<>"}
_QUOTED_ANGLES = {_PLACEHOLDERS[">"]: ">", _PLACEHOLDERS["<"]: "<"}
_GLOB_CHARS = re.compile(r"[*?\[]")
_CD = re.compile(r"(?<![\w-])(?:cd|pushd)(?![\w-])")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HARMLESS_REDIRECTS = re.compile(r"\d?>\s*/dev/null|\d>&\d")
_REDIRECT = re.compile(r"^(?:\d*|&)(>>|>\||>&|>)(.*)$")
_FD_TARGET = re.compile(r"^(?:\d+|-)$")

_SECRET_PATTERNS = (
    (re.compile(r"(?<![\w.-])\.ssh(?![\w-])", re.I), "reads ~/.ssh"),
    (re.compile(r"(?<![\w.-])\.gnupg(?![\w-])", re.I), "reads ~/.gnupg"),
    (re.compile(r"\.codex/auth\.json", re.I), "reads ~/.codex/auth.json"),
    (re.compile(r"(?<![\w.-])\.claude/\.?credentials", re.I), "reads Claude credentials"),
    (re.compile(r"(?<![\w.-])\.claude\.json(?![\w.-])", re.I), "reads ~/.claude.json"),
    (re.compile(r"(?<![\w.-])\.claude/[^\s'\"]*(?:oauth|token)", re.I), "reads Claude credentials"),
    (re.compile(r"Claude Code-credentials", re.I), "reads Claude credentials from the keychain"),
    (re.compile(r"security\s+dump-keychain", re.I), "dumps the keychain"),
)
_NO_GPG = re.compile(r"--no-gpg-sign|commit\.gpgsign\W{0,3}(?:false|0|no|off)\b", re.I)
_GIT_ENV = re.compile(r"(?<![\w])GIT_CONFIG(?:_[A-Z0-9_]+)?(?![\w])")
_GIT_CONFIG_DENY = re.compile(r"^(?:gpg\..*|(?:commit|tag)\.gpgsign|user\.signingkey|alias\..*)$")
_GIT_CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--show-scope"})


@dataclass
class Verdict:
    allow: bool
    stage: str
    reason: str
    action: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _flatten(command: str) -> str:
    """`command` with unquoted newlines and backticks turned into `;`, and quoted or escaped shell punctuation hidden."""
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            following = command[index + 1]
            if following == "\n":
                out.append(" ")
            elif following in _PLACEHOLDERS:
                out.append(_PLACEHOLDERS[following])
            else:
                out.append(char + following)
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
                out.append(char)
            else:
                out.append(_PLACEHOLDERS.get(char, char))
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char in "\n`":
            out.append(" ; ")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _restore(token: str) -> str:
    return "".join(_RESTORE.get(char, char) for char in token)


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies (inert text) unless they are fed to a shell, which would run them."""
    lines = command.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        out.append(line)
        index += 1
        match = re.search(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1", line)
        if not match:
            continue
        before = line[: match.start()]
        feeds_shell = re.search(r"(?:^|[;&|(]\s*)(?:\S*/)?(?:%s)\b[^;&|]*$" % "|".join(sorted(SHELLS)), before)
        body = []
        while index < len(lines) and lines[index].strip() != match.group(2):
            body.append(lines[index])
            index += 1
        index += 1
        if feeds_shell:
            out.extend(body)
    return "\n".join(out)


def _substitutions(command: str) -> list[str]:
    """The text inside every `$(...)` and backtick pair, including ones hiding in double quotes."""
    found: list[str] = []
    index = 0
    while True:
        start = command.find("$(", index)
        if start < 0:
            break
        depth, position = 1, start + 2
        while position < len(command) and depth:
            depth += {"(": 1, ")": -1}.get(command[position], 0)
            position += 1
        found.append(command[start + 2 : position - 1 if depth == 0 else len(command)])
        index = start + 2
    found.extend(re.findall(r"`([^`]*)`", command))
    return found


def _segments(command: str, dynamic: bool = False) -> list[list[str]]:
    """Split a shell command into simple-command token lists, entering `sh -c "…"` strings, `xargs` and `$(…)`."""
    command = _strip_heredocs(command)
    result: list[list[str]] = []
    for inner in _substitutions(command):
        result.extend(_segments(inner, dynamic))
    flat = _flatten(command)
    try:
        tokens = _tokenize(flat)
    except ValueError:
        tokens = re.findall(r"[;&|()]+|[^\s;&|()]+", flat)
    current: list[str] = []
    raw: list[list[str]] = []
    for token in tokens:
        if token and set(token) <= set(";&|()") or token in SEPARATORS:
            if current:
                raw.append(current)
            current = []
        else:
            current.append(_restore(token))
    if current:
        raw.append(current)
    for segment in raw:
        result.extend(_expand_segment(segment, dynamic))
    return result


def _expand_segment(segment: list[str], dynamic: bool) -> list[list[str]]:
    segment = _strip_wrappers(segment)
    if not segment:
        return []
    name = Path(segment[0]).name
    if name in SHELLS:
        script = _shell_command(segment[1:])
        if script is not None:
            return _segments("".join(_QUOTED_ANGLES.get(char, char) for char in script), dynamic)
    if name == "xargs":
        inner = _xargs_command(segment[1:])
        if inner:
            return _expand_segment(inner, True)
    return [segment + [DYNAMIC]] if dynamic else [segment]


def _shell_command(args: Sequence[str]) -> str | None:
    """The script string given to a shell with `-c`, including combined flags such as `-lc` and `-ic`."""
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-o", "+o", "-O", "+O"):
            index += 2
            continue
        if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", token):
            return args[index + 1] if index + 1 < len(args) else None
        if not token.startswith(("-", "+")):
            return None
        index += 1
    return None


def _xargs_command(args: Sequence[str]) -> list[str] | None:
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            return list(args[index:])
        index += 2 if token in XARGS_OPTS_WITH_VALUE or token in XARGS_LONG_WITH_VALUE else 1
    return None


def _strip_wrappers(segment: list[str]) -> list[str]:
    tokens = list(segment)
    while tokens:
        head = tokens[0]
        name = Path(head).name
        if _ENV_ASSIGN.match(head):
            tokens = tokens[1:]
        elif name in WRAPPERS:
            tokens = _skip_wrapper_options(name, tokens[1:])
        elif head.startswith("-") and len(tokens) > 1:
            tokens = tokens[1:]
        else:
            break
    return tokens


def _skip_wrapper_options(name: str, tokens: list[str]) -> list[str]:
    values = WRAPPER_VALUE_FLAGS.get(name, frozenset())
    while tokens and tokens[0].startswith("-") and len(tokens[0]) > 1:
        tokens = tokens[2:] if tokens[0] in values else tokens[1:]
    if name == "timeout" and tokens:
        tokens = tokens[1:]
    return tokens


def _git_parts(segment: Sequence[str]) -> tuple[str | None, list[str], str | None]:
    """(subcommand, args after it, directory given with -C) of a `git …` segment."""
    directory = None
    index = 1
    while index < len(segment):
        token = segment[index]
        if token in GIT_OPTS_WITH_VALUE:
            if token == "-C" and index + 1 < len(segment):
                directory = segment[index + 1]
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, list(segment[index + 1 :]), directory
    return None, [], directory


def _git_config_overrides(segment: Sequence[str]) -> list[str]:
    """The `key=value` strings given as `git -c …` or `git --config-env=…` before the subcommand."""
    overrides: list[str] = []
    index = 1
    while index < len(segment):
        token = segment[index]
        if token in GIT_OPTS_WITH_VALUE:
            if token == "-c" and index + 1 < len(segment):
                overrides.append(segment[index + 1])
            index += 2
        elif token.startswith("--config-env="):
            overrides.append(token.split("=", 1)[1])
            index += 1
        elif token.startswith("-"):
            index += 1
        else:
            break
    return overrides


def _expand(path: str, home: Path) -> str | None:
    """`path` with ~ and $HOME expanded; None when it holds a shell expansion we cannot resolve."""
    if path.startswith("~"):
        path = str(home) + path[1:]
    path = path.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if "$" in path or "`" in path:
        return None
    return path


def _resolve(path: str, cwd: str | None, home: Path) -> str | None:
    expanded = _expand(path, home)
    if expanded is None:
        return None
    if not os.path.isabs(expanded):
        if cwd is None:
            return None
        expanded = os.path.join(cwd, expanded)
    literal = re.split(r"[*?\[]", expanded, maxsplit=1)[0]
    return os.path.realpath(os.path.normpath(literal or "/"))


def _inside(path: str, root: str) -> bool:
    root = os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _current_branch(directory: str | None) -> str | None:
    if directory is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _normalise(text: str) -> str:
    """`text` without quotes and backslashes, so a quote-split or backslash-split `.ssh` reads as `.ssh`."""
    return text.replace("'", "").replace('"', "").replace("\\", "")


def _brace_expand(word: str, limit: int = 64) -> list[str]:
    match = re.search(r"\{([^{}]*,[^{}]*)\}", word)
    if not match:
        return [word]
    results: list[str] = []
    for alternative in match.group(1).split(","):
        results.extend(_brace_expand(word[: match.start()] + alternative + word[match.end() :], limit))
        if len(results) >= limit:
            break
    return results[:limit]


def _secret_reason(text: str) -> str | None:
    for pattern, reason in _SECRET_PATTERNS:
        if pattern.search(text):
            return reason
    return None


def _glob_reason(word: str, where: str | None, worktree: str | None, home: Path, allow_project: bool) -> str | None:
    """Why a glob in `word` may reach ~/.ssh, ~/.codex, ~/.claude or ~/.gnupg, or None."""
    if not _GLOB_CHARS.search(word):
        return None
    parts = word.split("/")
    for index, part in enumerate(parts):
        if not _GLOB_CHARS.search(part):
            continue
        matches_protected = part[0] not in "*?" and any(
            fnmatch.fnmatchcase(name.lower(), part.lower()) for name in PROTECTED_NAMES
        )
        after_protected = any(earlier.lower() in PROTECTED_DIRS for earlier in parts[:index])
        if not (matches_protected or after_protected):
            continue
        prefix = "/".join(parts[:index]) or ("/" if word.startswith("/") else ".")
        if allow_project and worktree and not _GLOB_CHARS.search(prefix):
            resolved = _resolve(prefix, where, home)
            if resolved is not None and _inside(resolved, worktree):
                continue
        return "a glob that can reach ~/.ssh, ~/.codex, ~/.claude or ~/.gnupg"
    return None


def _text_reason(command: str, where: str | None, worktree: str | None, home: Path) -> str | None:
    normalised = _normalise(command)
    for text in (command, normalised):
        if _NO_GPG.search(text):
            return "disables commit signing (--no-gpg-sign / commit.gpgsign=false)"
        if _GIT_ENV.search(text):
            return "overrides git config through GIT_CONFIG_* environment variables"
        reason = _secret_reason(text)
        if reason:
            return reason
    allow_project = not _CD.search(normalised)
    for word in re.split(r"[\s;&|()<>=]+", normalised):
        for expansion in _brace_expand(word):
            reason = _secret_reason(expansion) or _glob_reason(expansion, where, worktree, home, allow_project)
            if reason:
                return reason
    return None


def _check_bash(command: str, worktree: str | None, cwd: str | None, home: Path, mode: str = MODE_COMMITTER) -> str | None:
    where = cwd or worktree
    reason = _text_reason(command, where, worktree, home)
    if reason:
        return reason
    return _scan(_segments(command), where, worktree, home, mode)


def _scan(segments: Sequence[Sequence[str]], where: str | None, worktree: str | None, home: Path, mode: str) -> str | None:
    for segment in segments:
        dynamic = DYNAMIC in segment
        words = [word for word in segment if word != DYNAMIC]
        name = Path(words[0]).name
        args = words[1:]
        if name in EVAL_COMMANDS or words[0] == ".":
            return f"'{words[0]}' runs shell code the risk gate cannot inspect"
        reason = None
        if name == "cd":
            target = args[0] if args else str(home)
            where = _resolve(target, where, home)
        elif name == "rm":
            reason = _check_rm(args, dynamic, where, worktree, home)
        elif name == "git":
            reason = _check_git(words, where, home, mode)
        elif name == "find":
            reason = _check_find(args, where, worktree, home, mode)
        reason = reason or _scan_writes(words, dynamic, where, worktree, home)
        if reason:
            return reason
    return None


def _temp_roots(environ: Mapping[str, str] | None = None) -> list[str]:
    environ = os.environ if environ is None else environ
    roots = [os.path.realpath(root) for root in TEMP_ROOTS]
    if environ.get("TMPDIR"):
        roots.append(os.path.realpath(environ["TMPDIR"]))
    return roots


def _write_path(raw: str, where: str | None, worktree: str | None, home: Path) -> str | None:
    """The real path a write target points at, with ~, $HOME, $TMPDIR and $WF_WORKTREE expanded; None if it cannot be known."""
    path = raw
    if path.startswith("~"):
        path = str(home) + path[1:]
    for name, value in (("HOME", str(home)), ("TMPDIR", os.environ.get("TMPDIR")), ("WF_WORKTREE", worktree)):
        if value:
            path = path.replace("${%s}" % name, value).replace("$%s" % name, value)
    if "$" in path or "`" in path:
        return None
    if not os.path.isabs(path):
        if where is None:
            return None
        path = os.path.join(where, path)
    literal = re.split(r"[*?\[]", path, maxsplit=1)[0]
    return os.path.realpath(literal or "/")


def _write_reason(raw: str, where: str | None, worktree: str | None, home: Path) -> str | None:
    """Why writing to `raw` is refused (outside the worktree, /tmp and $TMPDIR), or None when it is allowed."""
    for expansion in _brace_expand(raw):
        resolved = _write_path(expansion, where, worktree, home)
        if resolved is None:
            return f"write to '{raw}', a path the risk gate cannot resolve"
        if resolved == NULL_DEVICE or any(_inside(resolved, root) for root in _temp_roots()):
            continue
        if worktree is None:
            return f"write to '{raw}' with no known worktree"
        if _inside(resolved, worktree):
            continue
        return f"write to '{raw}' outside the worktree (agents may only write inside {worktree}, /tmp and $TMPDIR)"
    return None


def _positional(args: Sequence[str], value_flags: frozenset[str] = frozenset()) -> list[str]:
    result: list[str] = []
    options_done = False
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if options_done or not arg.startswith("-") or arg == "-":
            result.append(arg)
        elif arg == "--":
            options_done = True
        elif arg in value_flags:
            index += 1
    return result


def _redirect_targets(words: Sequence[str]) -> list[str]:
    targets: list[str] = []
    index = 0
    while index < len(words):
        match = _REDIRECT.match(words[index])
        index += 1
        if not match:
            continue
        operator, target = match.groups()
        if not target and index < len(words):
            target = words[index]
            index += 1
        if not target or (operator == ">&" and _FD_TARGET.match(target)):
            continue
        targets.append("".join(_QUOTED_ANGLES.get(char, char) for char in target))
    return targets


def _sed_in_place_files(args: Sequence[str]) -> list[str]:
    if not any(arg == "--in-place" or arg.startswith("--in-place=") or re.fullmatch(r"-[A-Za-z]*i[^-]*", arg) for arg in args):
        return []
    positional = _positional(args, SED_VALUE_FLAGS)
    has_script = any(arg in SED_VALUE_FLAGS or arg.startswith(("--expression=", "--file=")) for arg in args)
    return positional if has_script else positional[1:]


def _command_write_targets(name: str, args: Sequence[str]) -> list[str]:
    """Paths a file-writing command (tee, cp, mv, rm, mkdir, touch, ln, chmod, dd, sed -i …) changes."""
    if name in WRITE_ALL_ARGS:
        return _positional(args, WRITE_VALUE_FLAGS.get(name, frozenset()))
    if name in COPY_LIKE:
        positional = _positional(args, COPY_VALUE_FLAGS | TARGET_DIR_FLAGS)
        directories = [
            args[index + 1] for index, arg in enumerate(args[:-1]) if arg in TARGET_DIR_FLAGS
        ] + [arg.split("=", 1)[1] for arg in args if arg.startswith("--target-directory=")]
        targets = directories + (positional[-1:] if not directories else [])
        return targets + (positional if name == "mv" else [])
    if name == "dd":
        return [arg[3:] for arg in args if arg.startswith("of=")]
    if name == "sed":
        return _sed_in_place_files(args)
    return []


def _scan_writes(words: Sequence[str], dynamic: bool, where: str | None, worktree: str | None, home: Path) -> str | None:
    name = Path(words[0]).name
    args = words[1:]
    for target in _redirect_targets(words):
        reason = _write_reason(target, where, worktree, home)
        if reason:
            return reason
    targets = _command_write_targets(name, args)
    if dynamic and (targets or name in WRITE_ALL_ARGS or name in COPY_LIKE):
        return f"{name} with targets taken from standard input (xargs), which cannot be checked against the worktree"
    for target in targets:
        reason = _write_reason(target, where, worktree, home)
        if reason:
            return reason
    if name == "git":
        return _git_write_reason(words, where, worktree, home)
    return None


def _git_write_reason(words: Sequence[str], where: str | None, worktree: str | None, home: Path) -> str | None:
    if _segment_read_only(words):
        return None
    _, _, directory = _git_parts(words)
    target = _write_path(directory, where, worktree, home) if directory else where
    if target is None:
        return "git command in a directory the risk gate cannot resolve"
    if worktree is None or _inside(target, worktree) or any(_inside(target, root) for root in _temp_roots()):
        return None
    return f"git command that changes '{directory or target}', outside the worktree"


def _check_find(args: Sequence[str], where: str | None, worktree: str | None, home: Path, mode: str) -> str | None:
    mutators = [arg for arg in args if arg in FIND_MUTATORS]
    if not mutators:
        return None
    if worktree is None:
        return f"find {mutators[0]} with no known worktree"
    start = 0
    while start < len(args) and args[start] in FIND_START_OPTIONS:
        start += 1
    roots: list[str] = []
    for arg in args[start:]:
        if arg.startswith("-") or arg in ("(", ")", "!"):
            break
        roots.append(arg)
    for root in roots or ["."]:
        resolved = _resolve(root, where, home)
        if resolved is None or not _inside(resolved, worktree):
            return f"find {mutators[0]} on '{root}' outside the worktree"
    for index, arg in enumerate(args):
        if arg in FIND_EXEC:
            inner = []
            for token in args[index + 1 :]:
                if token in (";", "+"):
                    break
                inner.append(token)
            reason = _scan(_expand_segment(inner, False), where, worktree, home, mode)
            if reason:
                return reason
    return None


def _check_rm(args: Sequence[str], dynamic: bool, where: str | None, worktree: str | None, home: Path) -> str | None:
    recursive = any(
        arg == "--recursive" or (arg.startswith("-") and not arg.startswith("--") and re.search(r"[rR]", arg))
        for arg in args
    )
    if not recursive:
        return None
    if dynamic:
        return "recursive rm with targets taken from standard input (xargs)"
    targets: list[str] = []
    options_done = False
    for arg in args:
        if arg == "--" and not options_done:
            options_done = True
        elif options_done or not arg.startswith("-"):
            targets.append(arg)
    if worktree is None:
        return "recursive rm with no known worktree"
    for target in targets:
        resolved = _resolve(target, where, home)
        if resolved is None or not _inside(resolved, worktree):
            return f"recursive rm of '{target}' outside the worktree"
    return None


def _check_git(segment: Sequence[str], where: str | None, home: Path, mode: str = MODE_COMMITTER) -> str | None:
    subcommand, args, directory = _git_parts(segment)
    for override in _git_config_overrides(segment):
        key = override.split("=", 1)[0].strip().lower()
        if _GIT_CONFIG_DENY.match(key):
            return f"git -c overrides '{key}' (signing or alias configuration)"
    if subcommand == "config" and not any(arg in _GIT_CONFIG_READS for arg in args):
        for arg in args:
            if _GIT_CONFIG_DENY.match(arg.strip().lower()):
                return f"git config changes '{arg}' (signing or alias configuration)"
    if subcommand == "push":
        for arg in args:
            if arg.startswith("--force") or re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", arg) or (arg.startswith("+") and len(arg) > 1):
                return "git push with force"
    if subcommand == "reset" and "--hard" in args:
        refs = [arg for arg in args if not arg.startswith("-")]
        if any(re.search(r"(?:^|/)(?:%s)$" % "|".join(PROTECTED_BRANCHES), ref) for ref in refs):
            return "git reset --hard to main"
        base = _resolve(directory, where, home) if directory else where
        branch = _current_branch(base)
        if branch is None:
            return "git reset --hard on an unknown branch"
        if branch in PROTECTED_BRANCHES:
            return "git reset --hard on main"
    if mode == MODE_AGENT:
        if "--amend" in args:
            return "git --amend is reserved for the commit run"
        listing_tag = subcommand == "tag" and all(arg in GIT_LISTING_FLAGS for arg in args)
        if subcommand in AGENT_DENIED_GIT and not listing_tag:
            return f"git {subcommand} is reserved for the commit run; only Claude's commit run changes history"
    return None


def _file_paths(tool_input: Mapping[str, Any]) -> list[str]:
    paths = [str(tool_input[key]) for key in PATH_KEYS if isinstance(tool_input.get(key), str)]
    for edit in tool_input.get("edits", []) or []:
        if isinstance(edit, Mapping) and isinstance(edit.get("file_path"), str):
            paths.append(edit["file_path"])
    return paths


def _patch_paths(patch: str) -> list[str]:
    return [path.strip() for path in re.findall(r"^\*\*\* (?:(?:Update|Add|Delete) File|Move to): (.+)$", patch, re.M)]


def _patch_text(tool_input: Mapping[str, Any]) -> str | None:
    """The apply_patch body of a tool call: a string or argv array under `command`, `cmd`, `input` or `patch`."""
    for key in PATCH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return shlex.join(str(part) for part in value)
    return None


def _command_text(tool_input: Mapping[str, Any]) -> str | None:
    """The shell command of a tool call, joining argv-style arrays into one string."""
    for key in ("command", "cmd"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return shlex.join(str(part) for part in value)
    return None


def denylist_reason(
    tool_name: str,
    tool_input: Mapping[str, Any] | Any,
    cwd: str | None,
    worktree: str | None,
    home: Path | None = None,
    mode: str = MODE_COMMITTER,
) -> str | None:
    """Why the hard deny-list blocks this call, or None. Runs before Laya and cannot be overridden.

    In `agent` mode the list also refuses every git command that commits, publishes or rewrites history.
    """
    home = Path(home) if home is not None else Path.home()
    if isinstance(tool_input, (str, list, tuple)):
        tool_input = {"command": tool_input}
    if not isinstance(tool_input, Mapping):
        return None
    where = cwd or worktree
    command = _command_text(tool_input)
    paths: list[str] = []
    write_paths: list[str] = []
    if tool_name == "apply_patch":
        write_paths = _patch_paths(_patch_text(tool_input) or "")
        paths = list(write_paths)
    elif command is not None:
        reason = _check_bash(command, worktree, cwd, home, mode)
        if reason:
            return reason
        if "*** Begin Patch" in command:
            write_paths = _patch_paths(command)
            paths = list(write_paths)
    if tool_name in FILE_TOOLS:
        paths.extend(_file_paths(tool_input))
    if tool_name in WRITE_TOOLS:
        write_paths.extend(_file_paths(tool_input))
    for raw in paths:
        normalised = _normalise(raw)
        resolved = _resolve(normalised, where, home) or normalised
        for candidate in (resolved, raw, normalised):
            reason = _secret_reason(candidate)
            if reason:
                return reason
        for expansion in _brace_expand(normalised):
            reason = _secret_reason(expansion) or _glob_reason(expansion, where, worktree, home, True)
            if reason:
                return reason
    for raw in write_paths:
        reason = _write_reason(raw, where, worktree, home)
        if reason:
            return reason
    return None


def is_read_only(tool_name: str, tool_input: Mapping[str, Any] | Any) -> bool:
    """True for tool calls that cannot change anything: read tools and inspection-only shell commands."""
    if tool_name in READ_ONLY_TOOLS:
        return True
    if tool_name != "Bash" or not isinstance(tool_input, Mapping):
        return False
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    stripped = _HARMLESS_REDIRECTS.sub("", command)
    if re.search(r"[<>]|\$\(|`", stripped):
        return False
    segments = _segments(stripped)
    return bool(segments) and all(_segment_read_only(segment) for segment in segments)


def _segment_read_only(segment: Sequence[str]) -> bool:
    name = Path(segment[0]).name
    args = list(segment[1:])
    if name == "git":
        subcommand, rest, _ = _git_parts(segment)
        if subcommand not in READ_ONLY_GIT:
            return False
        if subcommand in ("branch", "tag"):
            return all(arg in GIT_LISTING_FLAGS for arg in rest)
        return True
    if name not in READ_ONLY_COMMANDS:
        return False
    if name == "find":
        return not any(arg in FIND_MUTATORS for arg in args)
    if name == "sed":
        return not any(arg == "--in-place" or re.fullmatch(r"-[A-Za-z]*i[A-Za-z]*", arg) or arg.startswith("-i") for arg in args)
    return True


def _risk_gate_trusted(decider: Decider) -> bool:
    """Whether Laya should be asked about `risk_gate`: the key has a threshold and the server is available."""
    threshold_for = getattr(decider, "threshold_for", None)
    if threshold_for is not None and threshold_for("risk_gate") is None:
        return False
    return can_answer(decider, "risk_gate")


def evaluate(
    agent: str,
    payload: Mapping[str, Any],
    decider: Decider,
    threshold: float,
    worktree: str | None,
    home: Path | None = None,
    denylist_only: bool = False,
    mode: str | None = None,
) -> Verdict:
    """Deny-list first (always blocks), then Laya's risk_gate, which is advisory.

    `mode` is `agent` (every non-committer run: the deny-list also refuses git history changes, then Laya) or
    `committer` (the commit run: today's deny-list only, Laya is not consulted). Without `mode`, `denylist_only`
    picks `committer`.

    When `risk_gate` is not trusted (no threshold, backend off, or Laya unavailable) Laya is not asked and nothing is
    flagged: the call passes with a quiet `denylist_pass` event. Otherwise Laya blocks only when it says "risky"
    with confidence at or above the key's threshold; anything else is allowed, and an unsure answer on a call that
    is not read-only is flagged in the event for the UI.
    """
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_name, str) or not tool_name:
        return Verdict(False, "payload", "hook payload has no tool_name")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    root = worktree or cwd

    mode = mode or (MODE_COMMITTER if denylist_only else MODE_AGENT)
    reason = denylist_reason(tool_name, tool_input, cwd, root, home, mode=mode)
    if reason:
        return Verdict(False, "denylist", f"hard deny-list: {reason}")
    if mode == MODE_COMMITTER:
        return Verdict(True, "denylist_only", "passed the hard deny-list; Laya is not consulted in this mode")

    if not _risk_gate_trusted(decider):
        return Verdict(True, "denylist_pass", "passed the hard deny-list; the risk gate is not trusted, so Laya is not asked")

    state, question, _ = questions.risk_gate(tool_name, tool_input, root)
    decision = decider.ask_bool("risk_gate", state, question)
    extra = {"laya_answer": decision.answer, "laya_confidence": decision.confidence, "laya_source": decision.source}
    if confident(decision, threshold):
        if decision.answer is True:
            return Verdict(False, "laya_risky", f"Laya judged this risky (confidence {decision.confidence:.2f})", extra=extra)
        return Verdict(True, "laya_safe", f"Laya judged this safe (confidence {decision.confidence:.2f})", extra=extra)
    if is_read_only(tool_name, tool_input):
        return Verdict(True, "unsure_read_only", "Laya unsure; call is read-only", extra=extra)
    return Verdict(
        True,
        "unsure_flagged",
        "Laya unsure and the call is not read-only; allowed, flagged for the user",
        action="flag_unsure_not_read_only",
        extra={**extra, "flagged": True},
    )


def _summary(tool_name: str, tool_input: Any) -> str:
    if isinstance(tool_input, Mapping):
        command = _command_text(tool_input)
        if command is not None:
            return questions.clip(command, 300)
        for key in ("file_path", "path", "pattern", "url"):
            if isinstance(tool_input.get(key), str):
                return questions.clip(tool_input[key], 300)
    return questions.clip(tool_input if tool_input is not None else "", 300)


def _log(events: "EventLog | _TaggedEvents | None", agent: str, payload: Mapping[str, Any], verdict: Verdict) -> None:
    if events is None:
        return
    try:
        events.emit(
            "decision",
            key="risk_gate",
            stage=f"hook:{verdict.stage}",
            agent=agent,
            tool=payload.get("tool_name"),
            summary=_summary(str(payload.get("tool_name")), payload.get("tool_input")),
            allowed=verdict.allow,
            reason=verdict.reason,
            action=verdict.action,
            session_id=payload.get("session_id"),
            **verdict.extra,
        )
    except Exception as exc:
        print(f"workforce hook: could not write event log: {exc}", file=sys.stderr)


def managed_ids(worktree: str | None, paths: Paths) -> dict[str, Any]:
    """Event tags for a managed `worktree`, or an empty dict when it is not one.

    Relative to `paths.worktrees_root`: `<run_id>/<repo_name>/<task_id>` gives `run_id`, `repo` (the middle segments, which may
    contain `/`) and `task_id`; `<run_id>/<repo_name>/_integration` gives `run_id`, `repo`, `task_id=None` and
    `integration=True`. The old layout `<run_id>/<task_id>` gives `run_id` and `task_id` only.
    """
    if not worktree:
        return {}
    root = Path(os.path.realpath(paths.worktrees_root))
    try:
        parts = Path(os.path.realpath(worktree)).relative_to(root).parts
    except ValueError:
        return {}
    if len(parts) == 2:
        return {"run_id": parts[0], "task_id": parts[1]}
    if len(parts) < 3:
        return {}
    run_id, repo, last = parts[0], "/".join(parts[1:-1]), parts[-1]
    if last == INTEGRATION_DIR:
        return {"run_id": run_id, "repo": repo, "task_id": None, "integration": True}
    return {"run_id": run_id, "repo": repo, "task_id": last}


class _TaggedEvents:
    """Adds fixed fields (run_id, repo, task_id, integration) to every event emitted through it."""

    def __init__(self, events: EventLog, tags: Mapping[str, str]):
        self._events = events
        self._tags = dict(tags)

    def emit(self, kind: str, **data: Any) -> dict[str, Any]:
        return self._events.emit(kind, **{**self._tags, **data})


def _block_message(verdict: Verdict) -> str:
    return (
        f"WorkForce risk gate blocked this tool call: {verdict.reason}. "
        "Do not retry it or work around it; state what you were trying to do and stop for the user."
    )


def _parse_mode(value: str | None) -> str:
    """`committer` for the commit run's env value (or its legacy name `denylist`); anything else is `agent`."""
    return MODE_COMMITTER if value in (MODE_COMMITTER, MODE_DENYLIST_ONLY) else MODE_AGENT


def _resolve_mode(mode: str | None, denylist_only: bool) -> str:
    if mode is None:
        return MODE_COMMITTER if denylist_only else MODE_AGENT
    if mode not in MODES:
        raise ValueError(f"hook mode must be one of {', '.join(MODES)}, not {mode!r}")
    return mode


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    env: Mapping[str, str] | None = None,
    decider: Decider | None = None,
    threshold: float | None = None,
    home: Path | None = None,
) -> int:
    """Hook entry point: exit 0 with no output allows the call, exit 2 with a stderr reason blocks it.

    Events carry `run_id`, `repo` and `task_id` (or `integration`) when `WF_WORKTREE` is a managed worktree under `home` (default: the user's home).
    Any failure after the payload is read is a deny: `risk gate error: <exception type>`.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    env = os.environ if env is None else env
    if len(args) != 1 or args[0] not in AGENTS:
        print(f"usage: python -m workforce.decider.hooks {'|'.join(AGENTS)}", file=stderr)
        return 2
    agent = args[0]

    repo = env.get("WF_REPO")
    if not repo:
        print("WorkForce risk gate blocked this call: WF_REPO is not set, so the run cannot be audited.", file=stderr)
        return 2
    mode = _parse_mode(env.get(MODE_ENV))
    if decider is not None and threshold is None and mode != MODE_COMMITTER:
        raise ValueError("threshold is required when a decider is injected")
    try:
        payload = json.loads(stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except (ValueError, RecursionError) as exc:
        print(f"WorkForce risk gate blocked this call: unreadable hook payload ({exc}).", file=stderr)
        return 2

    try:
        return _decide(agent, payload, Path(repo), env, mode, decider, threshold, home, stderr)
    except BaseException as exc:
        return _deny_on_error(exc, agent, payload, Path(repo), env, home, stderr)


def _decide(
    agent: str,
    payload: Mapping[str, Any],
    repo: Path,
    env: Mapping[str, str],
    mode: str,
    decider: Decider | None,
    threshold: float | None,
    home: Path | None,
    stderr: TextIO,
) -> int:
    paths = Paths(repo, home=home)
    events = _TaggedEvents(EventLog(paths), managed_ids(env.get("WF_WORKTREE"), paths))
    if mode == MODE_COMMITTER:
        decider = NullDecider()
        threshold = 1.0
    elif decider is None:
        try:
            cfg = wf_config.load(repo).decider
            threshold = cfg.confidence
            decider = build_decider(cfg, events)
        except ConfigError as exc:
            events.emit("error", source="hook", message=f"could not load workforce.toml for the risk gate: {exc}")
            decider = NullDecider()
            threshold = 1.0
    try:
        verdict = evaluate(agent, payload, decider, threshold, env.get("WF_WORKTREE"), home=home, mode=mode)
        _log(events, agent, payload, verdict)
    finally:
        close = getattr(decider, "close", None)
        if close:
            close()
    if verdict.allow:
        return 0
    print(_block_message(verdict), file=stderr)
    return 2


def _deny_on_error(
    exc: BaseException,
    agent: str,
    payload: Mapping[str, Any],
    repo: Path,
    env: Mapping[str, str],
    home: Path | None,
    stderr: TextIO,
) -> int:
    verdict = Verdict(False, "error", f"risk gate error: {type(exc).__name__}")
    try:
        paths = Paths(repo, home=home)
        events = _TaggedEvents(EventLog(paths), managed_ids(env.get("WF_WORKTREE"), paths))
        _log(events, agent, payload, verdict)
        events.emit("error", source="hook", message=f"risk gate error, tool call denied: {type(exc).__name__}: {exc}")
    except BaseException as log_exc:
        print(f"workforce hook: could not log the risk gate error: {log_exc}", file=sys.stderr)
    try:
        print(_block_message(verdict), file=stderr)
    except BaseException:
        pass
    return 2


def _hook_command(agent: str, repo: Path, worktree: Path | None, python: str | None, mode: str) -> str:
    package_root = Path(workforce.__file__).resolve().parent.parent
    assignments = [f"WF_REPO={shlex.quote(str(Path(repo).resolve()))}", f"PYTHONPATH={shlex.quote(str(package_root))}"]
    if worktree is not None:
        assignments.append(f"WF_WORKTREE={shlex.quote(str(Path(worktree).resolve()))}")
    assignments.append(f"{MODE_ENV}={MODE_DENYLIST_ONLY if mode == MODE_COMMITTER else MODE_AGENT}")
    interpreter = shlex.quote(python or sys.executable)
    return f"{' '.join(assignments)} {interpreter} -m workforce.decider.hooks {agent} || exit 2"


def claude_settings_json(
    repo: Path,
    worktree: Path | None = None,
    python: str | None = None,
    denylist_only: bool = False,
    mode: str | None = None,
) -> str:
    """The per-run `--settings` JSON that registers the PreToolUse risk-gate hook for every Claude tool.

    `mode="agent"` (default) is for every run but the commit run: the deny-list also refuses git commit, push and
    other history changes, then Laya is consulted. `mode="committer"` (or `denylist_only=True`) is for the commit run:
    today's deny-list only, and Laya is never consulted. The command exits 2 (deny) if the hook itself fails.
    """
    hook_mode = _resolve_mode(mode, denylist_only)
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": _hook_command("claude", repo, worktree, python, hook_mode), "timeout": HOOK_TIMEOUT_S}
                    ],
                }
            ]
        }
    }
    return json.dumps(settings)


def _toml_string(value: str) -> str:
    """`value` as a TOML basic string; non-ASCII characters stay literal so astral-plane paths survive."""
    escapes = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    out = ['"']
    for char in value:
        if char in escapes:
            out.append(escapes[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        elif 0xD800 <= ord(char) <= 0xDFFF:
            raise ConfigError("cannot register the risk-gate hook: a path is not valid Unicode")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def codex_hook_args(
    repo: Path,
    worktree: Path | None = None,
    python: str | None = None,
    denylist_only: bool = False,
    mode: str | None = None,
) -> list[str]:
    """Arguments for `codex exec` that register the PreToolUse risk-gate hook and let it run untrusted.

    `mode` is as for `claude_settings_json`. Raises `ConfigError` when the override would not parse as TOML.
    """
    hook_mode = _resolve_mode(mode, denylist_only)
    command = _hook_command("codex", repo, worktree, python, hook_mode)
    value = f'[{{hooks=[{{type="command", command={_toml_string(command)}, timeout={HOOK_TIMEOUT_S}}}]}}]'
    try:
        parsed = tomllib.loads(f"value = {value}")["value"]
        registered = parsed[0]["hooks"][0]["command"]
    except (tomllib.TOMLDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ConfigError(f"cannot register the risk-gate hook: the Codex override is not valid TOML ({exc})") from exc
    if registered != command:
        raise ConfigError("cannot register the risk-gate hook: the Codex override does not round-trip")
    return ["--dangerously-bypass-hook-trust", "-c", f"hooks.PreToolUse={value}"]


if __name__ == "__main__":
    sys.exit(main())
