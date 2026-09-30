"""Detect and run test/lint/build commands for a target repo."""

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from workforce.agents.base import kill_group, register, unregister, without_build_artifacts

KINDS = ("test", "lint", "build")
TAIL_LINES = 200
SUMMARY_MAX_LINES = 60


@dataclass
class CheckResult:
    kind: str
    cmd: str | None
    exit_code: int | None
    duration_s: float
    output_tail: str
    skipped: bool = False
    timed_out: bool = False
    skip_reason: str | None = None

    @property
    def passed(self) -> bool:
        return not self.skipped and not self.timed_out and self.exit_code == 0


@dataclass
class CheckReport:
    ok: bool
    results: list[CheckResult] = field(default_factory=list)
    no_checks: bool = False


_PYTEST_CACHE: dict[str, bool] = {}


def _has_pytest(interpreter: str) -> bool:
    if interpreter not in _PYTEST_CACHE:
        try:
            proc = subprocess.run(
                [interpreter, "-c", "import pytest"], capture_output=True, timeout=20, check=False
            )
            _PYTEST_CACHE[interpreter] = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _PYTEST_CACHE[interpreter] = False
    return _PYTEST_CACHE[interpreter]


def _python_candidates(repo: Path, env_root: Path | None) -> list[str]:
    candidates = []
    for root in (repo, env_root):
        if root is None:
            continue
        venv_python = root / ".venv" / "bin" / "python"
        if venv_python.exists():
            candidates.append(str(venv_python))
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found and found not in candidates:
            candidates.append(found)
    return candidates


def _python_interpreter(repo: Path, env_root: Path | None) -> str | None:
    """The first candidate interpreter (repo .venv, env_root .venv, python3, python) that can import pytest."""
    for candidate in _python_candidates(repo, env_root):
        if _has_pytest(candidate):
            return candidate
    return None


PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini", "tox.ini")


def _looks_like_python(repo: Path) -> bool:
    """A Python project marker, or Python test files under tests/ (a bare tests/ folder is not enough)."""
    if any((repo / name).is_file() for name in PYTHON_MARKERS):
        return True
    tests = repo / "tests"
    return tests.is_dir() and any(tests.rglob("test*.py"))


def _package_json_scripts(repo: Path) -> dict:
    try:
        data = json.loads((repo / "package.json").read_text())
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _makefile_has_test(repo: Path) -> bool:
    try:
        text = (repo / "Makefile").read_text()
    except OSError:
        return False
    return re.search(r"^test\s*:", text, re.MULTILINE) is not None


def _autodetect(repo: Path, env_root: Path | None) -> tuple[dict[str, list[str]], dict[str, str]]:
    found: dict[str, list[str]] = {kind: [] for kind in KINDS}
    reasons: dict[str, str] = {}

    if _looks_like_python(repo):
        interpreter = _python_interpreter(repo, env_root)
        if interpreter:
            found["test"].append(f"{shlex.quote(interpreter)} -m pytest -q")
        else:
            tried = ", ".join(_python_candidates(repo, env_root)) or "none found"
            reasons["test"] = (
                "Python project detected but no interpreter with pytest installed "
                f"(tried: {tried}); set [checks] test (or [repos.<name>.checks] test) in workforce.toml"
            )

    if (repo / "package.json").is_file():
        scripts = _package_json_scripts(repo)
        for kind in KINDS:
            if kind in scripts:
                found[kind].append(f"npm run {kind}")

    if (repo / "go.mod").is_file():
        found["test"].append("go test ./...")
        found["lint"].append("go vet ./...")

    if (repo / "Cargo.toml").is_file():
        found["test"].append("cargo test")

    if _makefile_has_test(repo):
        found["test"].append("make test")

    return found, reasons


def _as_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [c for c in value if c and c.strip()]


def detect_with_reasons(
    repo: Path | str,
    configured: dict[str, list[str] | str] | None = None,
    env_root: Path | str | None = None,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Like `detect`, plus {kind: reason} for kinds that had to be skipped."""
    repo = Path(repo)
    found, reasons = _autodetect(repo, Path(env_root) if env_root else None)
    for kind, value in (configured or {}).items():
        override = _as_list(value)
        if override:
            found[kind] = override
            reasons.pop(kind, None)
        else:
            found.setdefault(kind, [])
    return found, {k: v for k, v in reasons.items() if not found.get(k)}


def detect(
    repo: Path | str,
    configured: dict[str, list[str] | str] | None = None,
    env_root: Path | str | None = None,
) -> dict[str, list[str]]:
    """Return {kind: [commands]} for test/lint/build.

    A non-empty configured command for a kind replaces detection for that kind;
    an empty string or empty list means auto-detect. For Python, `.venv/bin/python`
    is looked up in `repo`, then `env_root` (worktrees have no .venv), then on PATH.
    """
    return detect_with_reasons(repo, configured, env_root)[0]


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-TAIL_LINES:])


def _run_one(repo: Path, kind: str, cmd: str, timeout_s: int) -> CheckResult:
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
        env=without_build_artifacts(dict(os.environ)),
    )
    register(proc)
    try:
        try:
            output, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            output, _ = proc.communicate()
            return CheckResult(
                kind, cmd, None, time.monotonic() - start,
                _tail((output or "") + f"\n[timed out after {timeout_s}s]"), timed_out=True,
            )
        except BaseException:
            kill_group(proc)
            proc.wait()
            raise
    finally:
        unregister(proc)
    return CheckResult(kind, cmd, proc.returncode, time.monotonic() - start, _tail(output or ""))


def run(
    repo: Path | str,
    commands: dict[str, list[str] | str] | None = None,
    timeout_s: int = 1800,
    env_root: Path | str | None = None,
) -> CheckReport:
    """Run configured/detected checks in `repo`; `timeout_s` applies to each command."""
    repo = Path(repo)
    resolved, reasons = detect_with_reasons(repo, commands, env_root)
    kinds = [k for k in KINDS] + [k for k in resolved if k not in KINDS]
    results: list[CheckResult] = []
    for kind in kinds:
        cmds = resolved.get(kind, [])
        if not cmds:
            results.append(
                CheckResult(kind, None, None, 0.0, "", skipped=True, skip_reason=reasons.get(kind))
            )
            continue
        for cmd in cmds:
            results.append(_run_one(repo, kind, cmd, timeout_s))
    ran = [r for r in results if not r.skipped]
    return CheckReport(ok=all(r.passed for r in ran), results=results, no_checks=not ran)


def _status_word(r: CheckResult) -> str:
    if r.skipped:
        return "SKIPPED"
    if r.timed_out:
        return "TIMEOUT"
    return "PASS" if r.exit_code == 0 else f"FAIL (exit {r.exit_code})"


def summary(report: CheckReport) -> str:
    """Compact text for prompts, at most 60 lines; failing output is tail-trimmed to fit."""
    lines: list[str] = []
    if report.no_checks:
        lines.append("NO CHECKS: no test/lint/build commands were configured or detected.")
    lines.append(f"Checks: {'OK' if report.ok else 'FAILED'}")
    for r in report.results:
        if r.skipped:
            lines.append(f"- {r.kind}: SKIPPED ({r.skip_reason or 'no command'})")
        else:
            lines.append(f"- {r.kind}: {_status_word(r)} in {r.duration_s:.1f}s: {r.cmd}")

    failed = [r for r in report.results if not r.skipped and not r.passed]
    budget = SUMMARY_MAX_LINES - len(lines)
    if failed and budget > 0:
        per = max(1, (budget - 2 * len(failed)) // len(failed))
        for r in failed:
            if len(lines) + 2 > SUMMARY_MAX_LINES:
                break
            lines.append(f"--- {r.kind} output (last {per} lines) ---")
            room = SUMMARY_MAX_LINES - len(lines)
            lines.extend(r.output_tail.splitlines()[-min(per, max(room, 0)):] if room > 0 else [])
    return "\n".join(lines[:SUMMARY_MAX_LINES])
