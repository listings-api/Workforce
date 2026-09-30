from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from workforce.agents.base import AgentRequest, AgentResult, AgentRunner, SCRUBBED_ENV_VARS, scrubbed_env
from workforce.errors import PreflightError

COMMAND_TIMEOUT_S = 30
API_KEY_ENV_VARS = SCRUBBED_ENV_VARS + ("CODEX_API_KEY",)
ENDPOINT_ENV_VARS = (
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
EXTRA_SCRUBBED_ENV_VARS = ("CODEX_API_KEY",) + ENDPOINT_ENV_VARS
MIN_VERSIONS = {"claude": (2, 1, 280), "codex": (0, 158, 0)}
UPGRADE_COMMANDS = {"claude": "claude update", "codex": "codex update"}
SUBSCRIPTION_VIOLATION = "subscription violation"
_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")
REDACTED = "[redacted]"
_VALUE = r"""(?:"[^"\n]*"|'[^'\n]*'|[^\s"',;]+)"""
_SECRET_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer|basic|token)?\s*[^\s\"',;]+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {REDACTED}"),
    (
        re.compile(rf"\b([A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)[A-Z0-9_]*)[\"']?\s*[=:]\s*{_VALUE}"),
        rf"\1={REDACTED}",
    ),
    (
        re.compile(
            rf"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd|token))[\"']?\s*[=:]\s*{_VALUE}"
        ),
        rf"\1={REDACTED}",
    ),
)


def agent_env(extra_env: dict | None = None, environ: dict | None = None) -> dict:
    """Environment for a child agent: no API keys, no endpoint or cloud-provider overrides.

    The three spec variables come from `scrubbed_env`; `CODEX_API_KEY` and `ENDPOINT_ENV_VARS` are
    removed here too, so neither CLI can be steered off the subscription login by ambient variables.
    `CLAUDE_CODE_OAUTH_TOKEN` is deliberately kept: it may be how the user's subscription login works.
    """
    env = scrubbed_env(extra_env, environ)
    for name in EXTRA_SCRUBBED_ENV_VARS:
        env.pop(name, None)
    return env


def redact(text: str) -> str:
    """Blank out the obvious secret shapes (`sk-...`, bearer tokens, `KEY=value`, `password: value`) in event text."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redacted(value):
    """`value` with `redact` applied to every string inside it (dicts, lists and tuples are walked)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redacted(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redacted(item) for item in value]
    return value


def is_subscription_violation(result: AgentResult) -> bool:
    """True when a Claude run was refused because it was not on the subscription login (never retried)."""
    return result.error_kind == "auth" and (result.error or "").startswith(SUBSCRIPTION_VIOLATION)


def parse_version(text: str) -> tuple[int, int, int] | None:
    """The first `major.minor.patch` in a `--version` output, or None."""
    match = _VERSION_PATTERN.search(text)
    return tuple(int(part) for part in match.groups()) if match else None


def check_version(name: str, binary: Path, output: str) -> list[str]:
    """Problems if a CLI's `--version` output is missing or older than the minimum WorkForce supports."""
    found = parse_version(output)
    minimum = ".".join(str(part) for part in MIN_VERSIONS[name])
    if found is None:
        return [f"could not read the {name} version from `--version` output: {output[:200] or 'nothing'}"]
    if found < MIN_VERSIONS[name]:
        return [
            f"{name} {'.'.join(str(part) for part in found)} at {binary} is older than the required {minimum}; "
            f"upgrade it with `{UPGRADE_COMMANDS[name]}`"
        ]
    return []


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=agent_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _safe_run(cmd: list[str], cwd: Path | None = None) -> tuple[int | None, str]:
    try:
        return _run(cmd, cwd)
    except FileNotFoundError:
        return None, f"{cmd[0]} not found"
    except PermissionError:
        return None, f"{cmd[0]} is not executable"
    except subprocess.TimeoutExpired:
        return None, f"{' '.join(cmd)} timed out after {COMMAND_TIMEOUT_S}s"


def check_env(environ: Mapping[str, str]) -> list[str]:
    """Problems for every API-key variable set in the environment."""
    return [
        f"{name} is set in the environment; unset it (WorkForce runs on subscriptions only)"
        for name in API_KEY_ENV_VARS
        if environ.get(name)
    ]


def check_claude(binary: Path) -> list[str]:
    """Problems with the claude CLI: missing, too old, or not logged in with claude.ai."""
    binary = Path(binary).expanduser()
    code, out = _safe_run([str(binary), "--version"])
    if code != 0:
        return [f"claude CLI at {binary} failed `--version`: {out or f'exit {code}'}"]
    old = check_version("claude", binary, out)
    if old:
        return old
    code, out = _safe_run([str(binary), "auth", "status"])
    if code is None:
        return [f"claude auth status failed: {out}"]
    try:
        status = json.loads(out)
        method = status.get("authMethod")
    except (ValueError, AttributeError):
        return [f"claude auth status did not return JSON: {out[:200]}"]
    if method != "claude.ai":
        return [
            f'claude authMethod is {method!r}, expected "claude.ai" (log in with your Claude subscription, not an API key)'
        ]
    return []


def check_codex(binary: Path) -> list[str]:
    """Problems with the codex CLI: missing, too old, or not logged in with ChatGPT.

    The login mode is asserted here, at preflight, and not on every run. Codex has no per-run
    equivalent of Claude's `apiKeySource`. Its auth mode is decided by `~/.codex/auth.json`, which
    only the user's own login changes, and every run's environment is stripped of the variables that
    could switch it to an API key (`OPENAI_API_KEY`, `CODEX_API_KEY`) or another endpoint.
    """
    binary = Path(binary).expanduser()
    code, out = _safe_run([str(binary), "--version"])
    if code != 0:
        return [f"codex CLI at {binary} failed `--version`: {out or f'exit {code}'}"]
    old = check_version("codex", binary, out)
    if old:
        return old
    code, out = _safe_run([str(binary), "login", "status"])
    if "Logged in using ChatGPT" not in out:
        return [f"codex is not logged in with ChatGPT (login status said: {out[:200] or 'nothing'})"]
    return []


def check_repo(repo: Path) -> list[str]:
    """Problems with a target repo: not a git repo, or a detached HEAD. A dirty tree is fine (WorkForce never touches it)."""
    repo = Path(repo)
    code, out = _safe_run(["git", "rev-parse", "--is-inside-work-tree"], repo)
    if code != 0 or out != "true":
        return [f"{repo} is not a git repository"]
    code, out = _safe_run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], repo)
    if code != 0 or not out:
        return [f"{repo} is on a detached HEAD; check out a branch first"]
    return []


def _git_ok(cmd: list[str], repo: Path) -> tuple[bool, str]:
    code, out = _safe_run(cmd, repo)
    return code == 0, out


def check_workspace_repo(
    name: str, path: Path, base_ref: str | None, branch: str, worktree_path: Path | None = None
) -> list[str]:
    """Problems for one involved repo: not a git repo, base unresolvable, integration branch taken, worktree path in use."""
    path = Path(path)
    label = f"repo {name}"
    ok, out = _git_ok(["git", "rev-parse", "--is-inside-work-tree"], path)
    if not ok or out != "true":
        return [f"{label}: {path} is not a git repository"]
    if base_ref is None:
        ok, out = _git_ok(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], path)
        if not ok or not out:
            return [f"{label}: {path} is on a detached HEAD and no base is configured; set repos.{name}.base"]
        base_ref = out
    problems = []
    ok, out = _git_ok(["git", "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"], path)
    if not ok:
        problems.append(f"{label}: base ref {base_ref!r} does not resolve to a commit in {path}")
    ok, _ = _git_ok(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], path)
    if ok:
        problems.append(f"{label}: integration branch {branch!r} already exists; pick another name")
    else:
        ok, out = _git_ok(["git", "check-ref-format", "--branch", branch], path)
        if not ok:
            problems.append(f"{label}: {branch!r} is not a valid branch name")
    if worktree_path is not None:
        problems.extend(_worktree_path_problems(name, path, Path(worktree_path)))
    return problems


def _worktree_path_problems(name: str, repo: Path, worktree_path: Path) -> list[str]:
    if worktree_path.exists() and (not worktree_path.is_dir() or any(worktree_path.iterdir())):
        return [f"repo {name}: worktree path {worktree_path} already exists and is not empty"]
    ok, out = _git_ok(["git", "worktree", "list", "--porcelain"], repo)
    if not ok:
        return [f"repo {name}: git worktree list failed: {out}"]
    resolved = worktree_path.resolve()
    for line in out.splitlines():
        if line.startswith("worktree ") and Path(line[len("worktree "):]).resolve() == resolved:
            return [f"repo {name}: worktree path {worktree_path} is already registered as a worktree"]
    return []


def check_workspace_repos(entries: list[tuple]) -> list[str]:
    """Problems across the involved repos of a run, one string each.

    Each entry is `(name, path, base_ref or None for the checked-out branch, integration branch name)`. Per repo:
    a git repo, a base that resolves, a free integration branch name. An optional fifth element is the integration
    worktree path, which must be free for `git worktree add`. The user's checkout may be dirty and on any branch.
    """
    problems: list[str] = []
    for name, path, base_ref, branch, *rest in entries:
        problems.extend(check_workspace_repo(name, path, base_ref, branch, rest[0] if rest else None))
    return problems


MIN_FREE_BYTES = 2 * 1024**3


def check_disk(path: Path, min_free_bytes: int = MIN_FREE_BYTES) -> list[str]:
    """A problem when the volume holding `path` (worktrees, logs, test runs) has less free space than `min_free_bytes`."""
    free = shutil.disk_usage(path).free
    if free >= min_free_bytes:
        return []
    return [
        f"only {free / 1024**3:.1f} GB free on the disk holding {path}; "
        f"WorkForce needs at least {min_free_bytes / 1024**3:.0f} GB for worktrees, logs and test runs"
    ]


def preflight(
    claude_bin: Path,
    codex_bin: Path,
    repo: Path | None,
    environ: Mapping[str, str],
    repos: list[tuple] | None = None,
) -> None:
    """Run every startup check and raise one PreflightError listing all problems.

    `repos` (see `check_workspace_repos`) checks each involved repo; otherwise a single `repo` is checked for being a git
    repo on a branch. Neither requires a clean working tree.
    """
    problems = check_env(environ) + check_claude(claude_bin) + check_codex(codex_bin)
    problems += check_disk(Path.home())
    if repos is not None:
        problems += check_workspace_repos(repos)
    elif repo is not None:
        problems += check_repo(repo)
    if problems:
        raise PreflightError(
            "preflight failed:\n" + "\n".join(f"  - {p}" for p in problems)
        )


def _probe(runner: AgentRunner, agent: str, model: str, effort: str, cwd: Path) -> bool:
    result = runner.run(
        AgentRequest(
            role="probe",
            agent=agent,
            model=model,
            effort=effort,
            prompt="Reply OK",
            cwd=cwd,
            sandbox="read-only",
            timeout_s=COMMAND_TIMEOUT_S * 4,
        )
    )
    if result.ok:
        return True
    if result.error_kind == "model_unavailable":
        return False
    raise PreflightError(
        f"could not probe {agent} model {model}: {result.error_kind}: {result.error}"
    )


def probe_claude_model(
    runner: AgentRunner, model: str, effort: str = "low", cwd: Path | None = None
) -> bool:
    """1-turn probe: True if the model works, False if unavailable, raises on infra errors."""
    return _probe(runner, "claude", model, effort, cwd or Path.cwd())


def probe_codex_model(
    runner: AgentRunner, model: str, effort: str = "low", cwd: Path | None = None
) -> bool:
    """1-turn probe: True if the model works, False if unavailable, raises on infra errors.

    codex exec refuses to run outside a git repo, so cwd must be inside one.
    """
    return _probe(runner, "codex", model, effort, cwd or Path.cwd())
