"""Load, validate and edit `workforce.toml`. Missing keys are errors; nothing has a silent default."""

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from workforce.decider.base import KEYS as DECISION_KEYS
from workforce.errors import ConfigError

CONFIG_NAME = "workforce.toml"

AGENTS = ("claude", "codex")
EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("low", "medium", "high", "xhigh", "max", "ultra"),
}
ROLE_NAMES = (
    "planner",
    "plan_reviewer",
    "coder",
    "reviewer_claude",
    "reviewer_codex",
    "committer",
    "usage_summary",
)
DECIDER_BACKENDS = ("laya", "off")
CHECK_KEYS = ("test", "lint", "build")
DEFAULT_BRANCH_PREFIX = "wf/"
_BRANCH_PREFIX_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


@dataclass(frozen=True)
class Role:
    agent: str
    model: str
    effort: str


@dataclass(frozen=True)
class Bin:
    claude: str
    codex: str


@dataclass(frozen=True)
class Limits:
    debate_rounds: int
    review_rounds: int
    alert_percent: int
    pause_percent: int
    poll_minutes: int
    parallel: int


@dataclass(frozen=True)
class Checks:
    test: str
    lint: str
    build: str


@dataclass(frozen=True)
class Browser:
    claude: bool
    codex: bool
    computer_use: bool


@dataclass(frozen=True)
class DeciderConfig:
    backend: str
    url: str
    confidence: float
    thresholds: dict[str, float] | None = None
    models: dict[str, str] | None = None


@dataclass(frozen=True)
class WorkspaceConfig:
    """The optional `[workspace]` table: the repos (relative paths under the root) and the branch prefix."""

    repos: list[str]
    branch_prefix: str


@dataclass(frozen=True)
class RepoSettings:
    """One `[repos.<name>]` table: an optional base ref and per-key check overrides (test/lint/build)."""

    base: str | None = None
    checks: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    bin: Bin
    roles: dict[str, Role]
    limits: Limits
    checks: Checks
    browser: Browser
    decider: DeciderConfig
    workspace: WorkspaceConfig | None = None
    repo_settings: dict[str, RepoSettings] = field(default_factory=dict)

    @property
    def branch_prefix(self) -> str:
        """The integration-branch prefix: `[workspace] branch_prefix`, or `wf/` in single-repo mode."""
        return self.workspace.branch_prefix if self.workspace else DEFAULT_BRANCH_PREFIX

    def repo_checks(self, name: str) -> Checks:
        """The `[checks]` defaults with any `[repos.<name>.checks]` overrides applied."""
        overrides = self.repo_settings[name].checks if name in self.repo_settings else {}
        return Checks(**{key: overrides.get(key, getattr(self.checks, key)) for key in CHECK_KEYS})

    def role(self, name: str) -> Role:
        try:
            return self.roles[name]
        except KeyError:
            raise ConfigError(f"roles.{name} is not a known role") from None


_DEFAULT_TOML = """\
[bin]
claude = "~/.local/bin/claude"
codex = "/opt/homebrew/bin/codex"

[roles.planner]        # Codex
agent = "codex"
model = "gpt-6-sol"
effort = "high"

[roles.plan_reviewer]  # Claude checks the plan
agent = "claude"
model = "claude-opus-5-5"
effort = "high"

[roles.coder]          # per-task default; user overrides per task
agent = "claude"
model = "claude-sonnet-5-5"
effort = "high"

[roles.reviewer_claude]
agent = "claude"
model = "claude-opus-5-5"
effort = "high"

[roles.reviewer_codex]
agent = "codex"
model = "gpt-6-sol"
effort = "high"

[roles.committer]
agent = "claude"
model = "claude-sonnet-5-5"
effort = "high"

[roles.usage_summary]
agent = "claude"
model = "claude-haiku-4-5-20251001"
effort = "low"

[limits]
debate_rounds = 3
review_rounds = 3
alert_percent = 50
pause_percent = 60
poll_minutes = 10
parallel = 2

[checks]               # empty = auto-detect
test = ""
lint = ""
build = ""

[browser]
claude = true           # adds --chrome for coder/reviewer_claude runs
codex = true
computer_use = false    # per-task toggle

[decider]
backend = "laya"        # "laya" | "off"
url = "http://127.0.0.1:11435"
confidence = 0.85

[decider.thresholds]
task_size = 0.75
agent_progress = 0.45
debate_converged = 0.63
"""


def default_toml() -> str:
    """Return the complete default `workforce.toml` text."""
    return _DEFAULT_TOML


def workspace_toml(repo_names: list[str]) -> str:
    """Return the default config plus a `[workspace]` section listing `repo_names`, one per line."""
    listing = "".join(f"  {json.dumps(name)},\n" for name in repo_names)
    text = (
        f"{_DEFAULT_TOML}\n"
        "[workspace]\n"
        "repos = [\n"
        f"{listing}"
        "]\n"
        f"branch_prefix = {json.dumps(DEFAULT_BRANCH_PREFIX)}\n"
    )
    parse(text)
    return text


def write_default(repo: Path) -> Path:
    """Write the default config into `repo`; refuse to overwrite an existing file."""
    return _write_new(Path(repo) / CONFIG_NAME, _DEFAULT_TOML)


def write_workspace_default(root: Path, repo_names: list[str]) -> Path:
    """Write the workspace config listing `repo_names` into `root`; refuse to overwrite an existing file."""
    return _write_new(Path(root) / CONFIG_NAME, workspace_toml(repo_names))


def _write_new(target: Path, text: str) -> Path:
    if target.exists():
        raise ConfigError(f"{target} already exists; refusing to overwrite it")
    target.write_text(text)
    return target


def load(repo: Path) -> Config:
    """Read and validate `repo/workforce.toml`."""
    path = Path(repo) / CONFIG_NAME
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"{path} not found; run `workforce init` first") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse(text)


def parse(text: str) -> Config:
    """Validate config text and build a `Config`."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{CONFIG_NAME} is not valid TOML: {exc}") from exc

    bin_table = _table(raw, "bin")
    roles_table = _table(raw, "roles")
    limits_table = _table(raw, "limits")
    checks_table = _table(raw, "checks")
    browser_table = _table(raw, "browser")
    decider_table = _table(raw, "decider")

    bins = Bin(
        claude=os.path.expanduser(_string(bin_table, "bin", "claude")),
        codex=os.path.expanduser(_string(bin_table, "bin", "codex")),
    )

    roles = {name: _role(roles_table, name) for name in ROLE_NAMES}

    limits = Limits(
        debate_rounds=_int(limits_table, "limits", "debate_rounds", minimum=1),
        review_rounds=_int(limits_table, "limits", "review_rounds", minimum=1),
        alert_percent=_int(limits_table, "limits", "alert_percent", minimum=1, maximum=100),
        pause_percent=_int(limits_table, "limits", "pause_percent", minimum=1, maximum=100),
        poll_minutes=_int(limits_table, "limits", "poll_minutes", minimum=1),
        parallel=_int(limits_table, "limits", "parallel", minimum=1),
    )
    if limits.alert_percent >= limits.pause_percent:
        raise ConfigError(
            f"limits.alert_percent ({limits.alert_percent}) must be lower than "
            f"limits.pause_percent ({limits.pause_percent})"
        )

    checks = Checks(
        test=_string(checks_table, "checks", "test", allow_empty=True),
        lint=_string(checks_table, "checks", "lint", allow_empty=True),
        build=_string(checks_table, "checks", "build", allow_empty=True),
    )

    browser = Browser(
        claude=_bool(browser_table, "browser", "claude"),
        codex=_bool(browser_table, "browser", "codex"),
        computer_use=_bool(browser_table, "browser", "computer_use"),
    )

    backend = _string(decider_table, "decider", "backend")
    if backend not in DECIDER_BACKENDS:
        raise ConfigError(f"decider.backend '{backend}' is invalid; expected one of {list(DECIDER_BACKENDS)}")
    confidence = _number(decider_table, "decider", "confidence")
    if not 0 < confidence <= 1:
        raise ConfigError(f"decider.confidence ({confidence}) must be greater than 0 and at most 1")
    thresholds = None
    if "thresholds" in decider_table:
        thresholds = {}
        for key, value in _table(decider_table, "thresholds").items():
            if key not in DECISION_KEYS:
                raise ConfigError(f"decider.thresholds.{key} is not a decision key; expected one of {sorted(DECISION_KEYS)}")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
                raise ConfigError(f"decider.thresholds.{key} ({value!r}) must be a number greater than 0 and at most 1")
            thresholds[key] = float(value)
    models = None
    if "models" in decider_table:
        models = {}
        for key, value in _table(decider_table, "models").items():
            if key not in DECISION_KEYS:
                raise ConfigError(f"decider.models.{key} is not a decision key; expected one of {sorted(DECISION_KEYS)}")
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"decider.models.{key} must be a non-empty model name")
            models[key] = value
    decider = DeciderConfig(
        backend=backend,
        url=_string(decider_table, "decider", "url"),
        confidence=float(confidence),
        thresholds=thresholds,
        models=models,
    )

    workspace = _workspace(raw)
    repo_settings = _repo_settings(raw)
    if workspace is not None:
        for name in repo_settings:
            if name not in workspace.repos:
                raise ConfigError(f"repos.{name} is not listed in workspace.repos {workspace.repos}")

    return Config(
        bin=bins,
        roles=roles,
        limits=limits,
        checks=checks,
        browser=browser,
        decider=decider,
        workspace=workspace,
        repo_settings=repo_settings,
    )


def _reject_unknown(table: dict[str, Any], section: str, allowed: tuple[str, ...]) -> None:
    for key in table:
        if key not in allowed:
            raise ConfigError(f"{section}.{key} is not a known key; expected one of {list(allowed)}")


def _valid_repo_path(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and str(path) == name and ".." not in path.parts and name != "."


def _workspace(raw: dict[str, Any]) -> WorkspaceConfig | None:
    if "workspace" not in raw:
        return None
    table = _table(raw, "workspace")
    _reject_unknown(table, "workspace", ("repos", "branch_prefix"))
    repos = _require(table, "workspace", "repos")
    if not isinstance(repos, list) or not repos:
        raise ConfigError("workspace.repos must be a non-empty list of relative paths")
    seen: set[str] = set()
    for index, name in enumerate(repos):
        if not isinstance(name, str) or not _valid_repo_path(name):
            raise ConfigError(
                f"workspace.repos[{index}] ({name!r}) must be a normalised relative path inside the workspace"
            )
        if name in seen:
            raise ConfigError(f"workspace.repos lists '{name}' more than once")
        seen.add(name)
    if "branch_prefix" in table:
        prefix = _string(table, "workspace", "branch_prefix")
        if not _BRANCH_PREFIX_RE.match(prefix):
            raise ConfigError(f"workspace.branch_prefix '{prefix}' may only contain letters, digits, '.', '_', '/' and '-'")
        if not prefix.endswith(("/", "-")):
            raise ConfigError(f"workspace.branch_prefix '{prefix}' must end with '/' or '-'")
    else:
        prefix = DEFAULT_BRANCH_PREFIX
    return WorkspaceConfig(repos=list(repos), branch_prefix=prefix)


def _repo_settings(raw: dict[str, Any]) -> dict[str, RepoSettings]:
    settings: dict[str, RepoSettings] = {}
    for name, table in _table(raw, "repos").items():
        section = f"repos.{name}"
        if not _valid_repo_path(name):
            raise ConfigError(f"{section} is not a valid repo path")
        if not isinstance(table, dict):
            raise ConfigError(f"{section} must be a table")
        _reject_unknown(table, section, ("base", "checks"))
        base = _string(table, section, "base") if "base" in table else None
        checks: dict[str, str] = {}
        if "checks" in table:
            checks_table = table["checks"]
            if not isinstance(checks_table, dict):
                raise ConfigError(f"{section}.checks must be a table")
            _reject_unknown(checks_table, f"{section}.checks", CHECK_KEYS)
            for key in checks_table:
                checks[key] = _string(checks_table, f"{section}.checks", key, allow_empty=True)
        settings[name] = RepoSettings(base=base, checks=checks)
    return settings


def save_role(repo: Path, role_name: str, model: str, effort: str) -> Config:
    """Set `model` and `effort` on one role, leaving every other byte of the file untouched."""
    path = Path(repo) / CONFIG_NAME
    try:
        original = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{path} not found; run `workforce init` first") from None
    if role_name not in ROLE_NAMES:
        raise ConfigError(f"roles.{role_name} is not a known role")
    if not model or any(ch in model for ch in '"\\') or any(ord(ch) < 32 for ch in model):
        raise ConfigError(f"roles.{role_name}.model '{model}' is not a valid model name")

    if effort not in EFFORTS["claude"] + EFFORTS["codex"]:
        raise ConfigError(f"roles.{role_name}.effort '{effort}' is not a valid effort level")

    lines = original.splitlines(keepends=True)
    start, end = _role_span(lines, role_name)
    lines[start:end] = _replace_in_table(lines[start:end], role_name, {"model": model, "effort": effort})
    updated = "".join(lines)

    config = parse(updated)

    _atomic_write(path, updated.encode("utf-8"))
    return config


_HEADER = r"^\s*\[\s*roles\s*\.\s*{name}\s*\]\s*(#.*)?$"
_ANY_HEADER = re.compile(r"^\s*\[")


def _role_span(lines: list[str], role_name: str) -> tuple[int, int]:
    header = re.compile(_HEADER.format(name=re.escape(role_name)))
    for index, line in enumerate(lines):
        if header.match(line.rstrip("\r\n")):
            end = index + 1
            while end < len(lines) and not _ANY_HEADER.match(lines[end]):
                end += 1
            return index, end
    raise ConfigError(f"roles.{role_name} is missing")


def _replace_in_table(table_lines: list[str], role_name: str, values: dict[str, str]) -> list[str]:
    result = list(table_lines)
    for key, value in values.items():
        pattern = re.compile(rf"^(\s*{key}\s*=\s*)(\"[^\"]*\"|'[^']*')(.*)$")
        for index in range(1, len(result)):
            match = pattern.match(result[index])
            if match:
                result[index] = f'{match.group(1)}"{value}"{match.group(3)}{result[index][match.end():]}'
                break
        else:
            raise ConfigError(f"roles.{role_name}.{key} is missing")
    return result


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, path.stat().st_mode & 0o7777)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table")
    return value


def _role(roles_table: dict[str, Any], name: str) -> Role:
    section = f"roles.{name}"
    table = roles_table.get(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f"{section} must be a table")
    agent = _string(table, section, "agent")
    if agent not in AGENTS:
        raise ConfigError(f"{section}.agent '{agent}' is invalid; expected one of {list(AGENTS)}")
    model = _string(table, section, "model")
    effort = _string(table, section, "effort")
    if effort not in EFFORTS[agent]:
        raise ConfigError(
            f"{section}.effort '{effort}' is not valid for {agent}; expected one of {list(EFFORTS[agent])}"
        )
    return Role(agent=agent, model=model, effort=effort)


def _require(table: dict[str, Any], section: str, key: str) -> Any:
    if key not in table:
        raise ConfigError(f"{section}.{key} is missing")
    return table[key]


def _string(table: dict[str, Any], section: str, key: str, allow_empty: bool = False) -> str:
    value = _require(table, section, key)
    if not isinstance(value, str):
        raise ConfigError(f"{section}.{key} must be a string")
    if not allow_empty and not value.strip():
        raise ConfigError(f"{section}.{key} must not be empty")
    return value


def _bool(table: dict[str, Any], section: str, key: str) -> bool:
    value = _require(table, section, key)
    if not isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be true or false")
    return value


def _int(
    table: dict[str, Any], section: str, key: str, minimum: int, maximum: int | None = None
) -> int:
    value = _require(table, section, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{section}.{key} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ConfigError(f"{section}.{key} ({value}) must be {bound}")
    return value


def _number(table: dict[str, Any], section: str, key: str) -> float:
    value = _require(table, section, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{section}.{key} must be a number")
    return value
