"""`wf doctor`: check the setup in a few seconds, without spending any model quota."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import workforce
from workforce.agents import guard
from workforce.team import config, detect

OK, FAIL, WARN = "ok", "fail", "warn"
SYMBOLS = {OK: "✓", FAIL: "✗", WARN: "!"}
MIN_PYTHON = (3, 11)
MIN_GIT = (2, 31, 0)
CLEAN_MERGE_GIT = (2, 38, 0)
AUTH_TIMEOUT_S = 15
GIT_TIMEOUT_S = 5
LAYA_URL = "http://127.0.0.1:11435"
LAYA_TIMEOUT_S = 1.0
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
STEERING_VARS = ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
INSTALL_HINTS = {
    "claude": "install Claude Code (https://claude.com/claude-code), then run `wf doctor` again",
    "codex": "install the Codex CLI (https://github.com/openai/codex), then run `wf doctor` again",
}
LOGIN_HINTS = {
    "claude": "run `claude auth login` and sign in with your Claude subscription",
    "codex": "run `codex login` and choose Sign in with ChatGPT",
}
NEXT_STEP = "Next: run `wf demo` to try it, or `cd your-project && wf`."
FIX_FIRST = "Fix the ✗ items above, then run `wf doctor` again."


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str | None = None


def _dotted(parts: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in parts)


def _run(cmd: list[str], environ: Mapping[str, str], timeout: float) -> tuple[int | None, str]:
    try:
        proc = subprocess.run(
            cmd,
            env=guard.agent_env(None, dict(environ)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout:g}s"
    except OSError as exc:
        return None, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def check_python(version_info: tuple[int, ...] = tuple(sys.version_info[:3])) -> Check:
    found = _dotted(tuple(version_info[:3]))
    if tuple(version_info[:2]) >= MIN_PYTHON:
        return Check("python", OK, f"Python {found}")
    return Check("python", FAIL, f"Python {found} is too old (need {_dotted(MIN_PYTHON)} or newer)", "install Python 3.11 or newer and reinstall WorkForce with it")


def check_git(environ: Mapping[str, str]) -> Check:
    import shutil

    git = shutil.which("git", path=environ.get("PATH"))
    if not git:
        return Check("git", FAIL, "git not found", f"install git {_dotted(MIN_GIT[:2])} or newer (https://git-scm.com/downloads)")
    code, out = _run([git, "--version"], environ, GIT_TIMEOUT_S)
    found = guard.parse_version(out) if code == 0 else None
    if found is None:
        return Check("git", FAIL, f"could not read the git version from {git}: {out[:120] or 'no output'}", "reinstall git (https://git-scm.com/downloads)")
    if found < MIN_GIT:
        return Check(
            "git",
            FAIL,
            f"git {_dotted(found)} at {git} is too old (session hooks need {_dotted(MIN_GIT[:2])})",
            f"upgrade git to {_dotted(MIN_GIT[:2])} or newer (https://git-scm.com/downloads)",
        )
    if found < CLEAN_MERGE_GIT:
        return Check("git", WARN, f"git {_dotted(found)} at {git}; clean-merge detection needs {_dotted(CLEAN_MERGE_GIT[:2])} or newer", f"upgrade git to {_dotted(CLEAN_MERGE_GIT[:2])} or newer to get it")
    return Check("git", OK, f"git {_dotted(found)} at {git}")


def _configured_binary(name: str, home: Path | None) -> str | None:
    try:
        table = tomllib.loads(config.config_path(home).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = table.get(name)
    return value if isinstance(value, str) and value.strip() else None


def _resolve(name: str, environ: Mapping[str, str], home: Path | None) -> tuple[Path | None, str | None]:
    """(binary to check, note): the configured path while it works, else what detection finds."""
    configured = _configured_binary(name, home)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path, None
    found = detect.find_cli(name, environ, home)
    note = f"team.toml lists {configured}, which is missing" if configured and found else None
    return found, note


def check_cli(name: str, environ: Mapping[str, str], home: Path | None) -> list[Check]:
    binary, note = _resolve(name, environ, home)
    if binary is None:
        return [Check(name, FAIL, f"{name} not found", INSTALL_HINTS[name])]
    shown = f"{binary} ({note}; set `{name} = \"{binary}\"` in ~/.workforce/team.toml)" if note else str(binary)
    code, out = _run([str(binary), "--version"], environ, detect.VERSION_TIMEOUT_S)
    if code != 0:
        return [Check(name, FAIL, f"{name} at {binary} failed `--version`: {out[:120] or f'exit {code}'}", INSTALL_HINTS[name])]
    found = guard.parse_version(out)
    minimum = guard.MIN_VERSIONS[name]
    checks: list[Check] = []
    if found is None:
        checks.append(Check(name, FAIL, f"could not read the {name} version from: {out[:120] or 'nothing'}", f"upgrade it with `{guard.UPGRADE_COMMANDS[name]}`"))
    elif found < minimum:
        checks.append(Check(name, FAIL, f"{name} {_dotted(found)} at {binary} is older than the required {_dotted(minimum)}", f"upgrade it with `{guard.UPGRADE_COMMANDS[name]}`"))
    else:
        checks.append(Check(name, WARN if note else OK, f"{name} {_dotted(found)} at {shown}", None))
    checks.append(_check_login(name, binary, environ))
    return checks


def _check_login(name: str, binary: Path, environ: Mapping[str, str]) -> Check:
    label = f"{name} login"
    if name == "claude":
        code, out = _run([str(binary), "auth", "status"], environ, AUTH_TIMEOUT_S)
        if code is None:
            return Check(label, FAIL, f"`claude auth status` failed: {out}", LOGIN_HINTS[name])
        try:
            method = json.loads(out).get("authMethod")
        except (ValueError, AttributeError):
            return Check(label, FAIL, f"`claude auth status` did not return JSON: {out[:120] or 'nothing'}", LOGIN_HINTS[name])
        if method != "claude.ai":
            return Check(label, FAIL, f"authMethod is {method!r}, not a Claude subscription", LOGIN_HINTS[name])
        return Check(label, OK, "signed in with your Claude subscription")
    code, out = _run([str(binary), "login", "status"], environ, AUTH_TIMEOUT_S)
    if code is None or "Logged in using ChatGPT" not in out:
        return Check(label, FAIL, f"not signed in with ChatGPT (login status said: {out[:120] or 'nothing'})", LOGIN_HINTS[name])
    return Check(label, OK, "signed in with your ChatGPT subscription")


def check_environment(environ: Mapping[str, str]) -> list[Check]:
    checks: list[Check] = []
    keys = [name for name in API_KEY_VARS if environ.get(name)]
    if keys:
        checks.append(Check("api keys", FAIL, f"{', '.join(keys)} is set; WorkForce uses subscriptions only", f"unset {' '.join(keys)} (and remove it from your shell profile)"))
    else:
        checks.append(Check("api keys", OK, "no API keys in the environment"))
    steering = [name for name in STEERING_VARS if environ.get(name)]
    if steering:
        checks.append(Check("endpoint overrides", WARN, f"{', '.join(steering)} is set and can steer Claude Code off your subscription login", f"unset {' '.join(steering)} unless you mean it"))
    return checks


def plugin_source() -> Path | None:
    try:
        from workforce.team import plugin_runtime

        candidate = plugin_runtime.source_dir()
        if candidate.is_dir():
            return candidate
    except (ImportError, AttributeError):
        pass
    package = Path(workforce.__file__).resolve().parent
    for candidate in (package / "team" / "plugin", package.parent / "wf-plugin"):
        if candidate.is_dir():
            return candidate
    return None


def check_plugin() -> Check:
    source = plugin_source()
    if source is None:
        return Check("plugin", FAIL, "the WorkForce plugin files were not found next to the package", "reinstall WorkForce so the plugin folder is included")
    missing = [name for name in (Path(".claude-plugin") / "plugin.json", Path("TEAM.md")) if not (source / name).is_file()]
    if missing:
        return Check("plugin", FAIL, f"the plugin at {source} is missing {', '.join(str(m) for m in missing)}", "reinstall WorkForce so the plugin folder is complete")
    return Check("plugin", OK, f"plugin at {source}")


def check_workforce_dir(home: Path | None) -> Check:
    directory = config.workforce_dir(home)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".doctor-"):
            pass
    except OSError as exc:
        return Check("workforce folder", FAIL, f"cannot write to {directory} ({exc})", f"make {directory} writable (check its owner and permissions)")
    return Check("workforce folder", OK, f"{directory} is writable")


def default_models_probe(binary: Path, home: Path | None) -> tuple[int, str | None]:
    from workforce.team import codex_models

    models, problem = codex_models.load(binary, home)
    return len(models), problem


def default_laya_probe(url: str = LAYA_URL) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/v1/models", timeout=LAYA_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


def check_optional(
    codex: Path | None,
    codex_ready: bool,
    home: Path | None,
    models_probe: Callable[[Path, Path | None], tuple[int, str | None]],
    laya_probe: Callable[[], bool],
) -> list[Check]:
    checks: list[Check] = []
    if codex is not None and codex_ready:
        try:
            count, problem = models_probe(codex, home)
        except Exception as exc:
            count, problem = 0, f"{type(exc).__name__}: {exc}"
        if count:
            checks.append(Check("codex models (optional)", OK, f"{count} Codex models readable"))
        else:
            checks.append(Check("codex models (optional)", WARN, problem or "no Codex models were listed", "only needed for the model picker; `wf` works without it"))
    if laya_probe():
        checks.append(Check("laya (optional)", OK, f"Laya/Ollaya is answering at {LAYA_URL}"))
    else:
        checks.append(Check("laya (optional)", WARN, f"Laya/Ollaya is not running at {LAYA_URL}", "optional: only the old pipeline uses it; everything works without it"))
    return checks


def run_checks(
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    models_probe: Callable[[Path, Path | None], tuple[int, str | None]] = default_models_probe,
    laya_probe: Callable[[], bool] = default_laya_probe,
) -> list[Check]:
    environ = os.environ if environ is None else environ
    checks = [check_python(), check_git(environ)]
    claude_checks = check_cli("claude", environ, home)
    codex_checks = check_cli("codex", environ, home)
    checks += claude_checks + codex_checks
    checks += check_environment(environ)
    checks += [check_plugin(), check_workforce_dir(home)]
    codex_binary, _ = _resolve("codex", environ, home)
    codex_ready = all(check.status != FAIL for check in codex_checks)
    checks += check_optional(codex_binary, codex_ready, home, models_probe, laya_probe)
    return checks


def render(checks: Sequence[Check]) -> str:
    lines = []
    for check in checks:
        lines.append(f"{SYMBOLS[check.status]} {check.detail}")
        if check.fix and check.status != OK:
            lines.append(f"    fix: {check.fix}")
    return "\n".join(lines)


def main(
    args: Sequence[str] | None = None,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    stdout=None,
    models_probe: Callable[[Path, Path | None], tuple[int, str | None]] = default_models_probe,
    laya_probe: Callable[[], bool] = default_laya_probe,
) -> int:
    parser = argparse.ArgumentParser(prog="wf doctor", description="Check the WorkForce setup (uses no model quota).")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    try:
        options = parser.parse_args(list(args or []))
    except SystemExit as exc:
        return int(exc.code or 0)
    out = stdout if stdout is not None else sys.stdout
    checks = run_checks(home, environ, models_probe, laya_probe)
    failed = any(check.status == FAIL for check in checks)
    if options.json:
        payload = {"ok": not failed, "checks": [asdict(check) for check in checks], "next": FIX_FIRST if failed else NEXT_STEP}
        print(json.dumps(payload, indent=2), file=out)
    else:
        print(render(checks), file=out)
        print("", file=out)
        print(FIX_FIRST if failed else NEXT_STEP, file=out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
