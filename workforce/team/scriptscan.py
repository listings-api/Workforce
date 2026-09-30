"""Scan scripts for the easy ways around the commit review (a workflow safeguard against unreviewed commits, not a security boundary).

The Bash check in `gitgate` only reads the command line, so `python3 ship.py` hides whatever `ship.py` does. This module
finds the files a Bash command would run (`scripts_run_by`) and looks for the words a bypass needs (`scan_text`).
It matches text: obfuscated, downloaded or generated code is not caught.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Iterator, Sequence

MAX_BYTES = 1_000_000
MAX_FILES = 20
DOC_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".adoc"})

_PYTHON = re.compile(r"^python(\d+(\.\d+)*)?$")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_INTERPRETERS = frozenset({"node", "ruby", "perl"})
_WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "exec", "command", "nice", "builtin"})
_PY_ARG_FLAGS = frozenset({"-W", "-X", "-m", "-c", "--check-hash-based-pycs"})
_UV_ARG_FLAGS = frozenset(
    {"--with", "--with-requirements", "--python", "-p", "--project", "--directory", "--extra", "--group", "--env-file", "--package", "--with-editable"}
)
_MAKE_ARG_FLAGS = frozenset({"-C", "--directory", "-I", "-j", "-o", "-W"})
_NPM_SCRIPT_VERBS = frozenset({"run", "run-script", "test", "start", "stop", "restart", "t", "tst", "rum", "urn"})
_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn"})
_MAKEFILES = ("GNUmakefile", "makefile", "Makefile")
_JUSTFILES = ("justfile", "Justfile", ".justfile")
_OPERATORS = frozenset({"&&", "||", ";", "|", "&", "|&", "(", ")", ";;", "\n"})

BYPASS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), reason)
    for pattern, reason in (
        (r"--no-verify", "skips the commit hooks"),
        (r"commit-tree", "writes a commit without going through the review"),
        (r"update-ref", "moves a branch without going through the review"),
        (r"hookspath", "redirects git's hooks"),
        (r"git_config_(?:count|key|value|parameters)", "overrides git config through the environment"),
        (r"git_index_file", "commits from a separate index"),
        (r"team\.key", "touches the approvals key"),
        (r"wf-approvals", "touches the signed approvals file"),
        (r"workforce\.team(?:\.|import\(?)approvals|team\.approvals|approvals\.record|load_key", "uses the approvals module directly"),
        (r"reference-transaction", "hooks or bypasses git's ref updates"),
    )
)
_ENV_PATTERN = re.compile(r"(?<![\w.-])env (?:-i(?![\w-])|--ignore-environment|-u(?![\w-]))", re.I)
_ENV_REASON = "clears the environment the commit check relies on"
_GIT_DIR = re.compile(r"git_dir[^a-z0-9_]{0,3}=", re.I)
_GIT_DIR_REASON = "points a commit at another git directory"


def _views(text: str) -> tuple[str, str, str]:
    """The text with whitespace, quotes, backslashes and `+` joins squashed away; the same with brackets and commas too; and one that keeps single spaces."""
    lowered = text.lower()
    squashed = re.sub(r"[\s'\"\\+]", "", lowered)
    dense = re.sub(r"[^a-z0-9_.=()-]", "", lowered)
    spaced = re.sub(r"[\s,\[\]()]+", " ", re.sub(r"['\"\\+]", "", lowered))
    return squashed, dense, spaced


def _shown(match: str) -> str:
    return match if len(match) <= 40 else match[:37] + "..."


def scan_text(text: str) -> str | None:
    """"`<match>` (<reason>)" for the first bypass word in `text`, else None."""
    squashed, dense, spaced = _views(text)
    for view in (squashed, dense):
        for pattern, reason in BYPASS_PATTERNS:
            found = pattern.search(view)
            if found:
                return f"`{_shown(found.group(0))}` ({reason})"
    found = _ENV_PATTERN.search(spaced)
    if found:
        return f"`{found.group(0)}` ({_ENV_REASON})"
    for view in (squashed, dense):
        found = _GIT_DIR.search(view)
        if found and "commit" in view:
            return f"`{found.group(0)}` ({_GIT_DIR_REASON})"
    return None


def is_doc(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in DOC_SUFFIXES


def _read(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def _package_scripts(text: str) -> str | None:
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    scripts = parsed.get("scripts") if isinstance(parsed, dict) else None
    if not isinstance(scripts, dict):
        return None
    return "\n".join(str(value) for value in scripts.values())


def scan_file(path: str | os.PathLike[str]) -> str | None:
    """`scan_text` over a file (a package.json only over its scripts). None when it is clean, too large, binary or unreadable."""
    target = Path(path)
    text = _read(target)
    if text is None:
        return None
    if target.name == "package.json":
        text = _package_scripts(text)
        if text is None:
            return None
    return scan_text(text)


def _tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return command.split()


def _segments(command: str) -> Iterator[list[str]]:
    current: list[str] = []
    for token in _tokens(command):
        if token in _OPERATORS or (token and set(token) <= set("&|;()")):
            if current:
                yield current
            current = []
            yield [token]
        else:
            current.append(token)
    if current:
        yield current


def _plain(token: str) -> bool:
    return bool(token) and not any(ch in token for ch in "$`*?[{")


def _resolve(token: str, cwd: Path) -> Path | None:
    if not _plain(token):
        return None
    path = Path(os.path.expanduser(token))
    return path if path.is_absolute() else cwd / path


def _strip_prefix(tokens: list[str]) -> list[str]:
    """Drop `VAR=value` words and wrappers such as `env`, `sudo`, `time`, with their flags."""
    index = 0
    while index < len(tokens):
        word = tokens[index]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            index += 1
        elif os.path.basename(word) in _WRAPPERS:
            index += 1
            while index < len(tokens) and (tokens[index].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index])):
                index += 1
        else:
            break
    return tokens[index:]


def _first_arg(args: Sequence[str], value_flags: frozenset[str]) -> str | None:
    """The first argument that is not a flag (skipping the values of `value_flags`); None if a `-c`/`-m` style flag ends the search."""
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if word in ("-c", "-m") and word in value_flags:
            return None
        if word in value_flags:
            index += 2
        elif word.startswith("-") and len(word) > 1:
            index += 1
        else:
            return word
    return None


def _python_target(args: Sequence[str], cwd: Path) -> Path | None:
    index = 0
    while index < len(args):
        word = args[index]
        if word == "-c":
            return None
        if word == "-m":
            module = args[index + 1] if index + 1 < len(args) else ""
            if re.fullmatch(r"[A-Za-z_][\w.]*", module):
                relative = Path(*module.split("."))
                for candidate in (relative.with_suffix(".py"), relative / "__main__.py"):
                    if (cwd / candidate).is_file():
                        return cwd / candidate
            return None
        if word == "--":
            return _resolve(args[index + 1], cwd) if index + 1 < len(args) else None
        if word in _PY_ARG_FLAGS:
            index += 2
        elif word.startswith("-") and len(word) > 1:
            index += 1
        else:
            return _resolve(word, cwd)
    return None


def _makefile_targets(args: Sequence[str], cwd: Path) -> list[Path]:
    directory, named = cwd, []
    index = 0
    while index < len(args):
        word = args[index]
        if word in ("-f", "--file", "--makefile") and index + 1 < len(args):
            named.append(args[index + 1])
            index += 2
        elif word.startswith("--file=") or word.startswith("--makefile="):
            named.append(word.split("=", 1)[1])
            index += 1
        elif word in ("-C", "--directory") and index + 1 < len(args):
            resolved = _resolve(args[index + 1], directory)
            directory = resolved or directory
            index += 2
        elif word.startswith("--directory="):
            resolved = _resolve(word.split("=", 1)[1], directory)
            directory = resolved or directory
            index += 1
        elif word in _MAKE_ARG_FLAGS:
            index += 2
        else:
            index += 1
    if named:
        return [path for path in (_resolve(name, directory) for name in named) if path]
    return [directory / name for name in _MAKEFILES if (directory / name).is_file()][:1]


def _find_up(cwd: Path, names: Sequence[str]) -> list[Path]:
    for directory in (cwd, *cwd.parents):
        for name in names:
            if (directory / name).is_file():
                return [directory / name]
    return []


def _segment_targets(tokens: list[str], cwd: Path) -> list[Path]:
    tokens = _strip_prefix(tokens)
    if not tokens:
        return []
    head, args = tokens[0], tokens[1:]
    name = os.path.basename(head)
    if _PYTHON.match(name):
        target = _python_target(args, cwd)
        return [target] if target else []
    if name in _SHELLS:
        first = _first_arg(args, frozenset({"-c", "-o", "-O", "--rcfile", "--init-file"}))
        target = _resolve(first, cwd) if first else None
        return [target] if target else []
    if name in _INTERPRETERS:
        first = _first_arg(args, frozenset({"-e", "-c", "-r", "--require", "-I", "-E"}))
        target = _resolve(first, cwd) if first else None
        return [target] if target else []
    if name in ("source", "."):
        target = _resolve(args[0], cwd) if args else None
        return [target] if target else []
    if name in ("make", "gmake"):
        return _makefile_targets(args, cwd)
    if name == "just":
        return _find_up(cwd, _JUSTFILES)
    if name in _PACKAGE_MANAGERS:
        verb = _first_arg(args, frozenset({"--prefix", "--cwd", "-C", "--dir", "--filter", "-F"}))
        if verb and (verb in _NPM_SCRIPT_VERBS or name != "npm"):
            return _find_up(cwd, ("package.json",))
        return []
    if name == "uv":
        rest = args[1:] if args and args[0] == "run" else None
        if rest is None:
            return []
        index = 0
        while index < len(rest) and rest[index].startswith("-") and rest[index] != "--":
            index += 2 if rest[index] in _UV_ARG_FLAGS else 1
        rest = rest[index + 1 :] if index < len(rest) and rest[index] == "--" else rest[index:]
        if not rest:
            return []
        if rest[0].endswith(".py"):
            target = _resolve(rest[0], cwd)
            return [target] if target else []
        return _segment_targets(rest, cwd)
    if "/" in head:
        target = _resolve(head, cwd)
        if target and target.is_file() and os.access(target, os.X_OK):
            return [target]
    return []


def _cd_target(tokens: list[str], cwd: Path) -> Path | None:
    tokens = _strip_prefix(tokens)
    if len(tokens) == 2 and tokens[0] in ("cd", "pushd"):
        return _resolve(tokens[1], cwd)
    return None


def _targets(command: str, cwd: str | os.PathLike[str]) -> list[Path]:
    where = Path(cwd)
    found: list[Path] = []
    for segment in _segments(command):
        if len(segment) == 1 and segment[0] in _OPERATORS:
            continue
        moved = _cd_target(segment, where)
        if moved is not None:
            where = moved
            continue
        for path in _segment_targets(segment, where):
            if path not in found:
                found.append(path)
    return found


def _readable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size <= MAX_BYTES and os.access(path, os.R_OK)
    except OSError:
        return False


def scripts_run_by(command: str, cwd: str | os.PathLike[str]) -> list[Path]:
    """The existing, readable files (at most MAX_FILES, each at most 1 MB) that a Bash command would execute or read as a build script."""
    try:
        targets = _targets(command, cwd)
    except Exception:
        return []
    return [path for path in targets if _readable(path)][:MAX_FILES]


def missing_scripts(command: str, cwd: str | os.PathLike[str]) -> list[Path]:
    """Script paths the command would run that do not exist yet, so the same command may be creating them."""
    try:
        return [path for path in _targets(command, cwd) if not path.exists()][:MAX_FILES]
    except Exception:
        return []
