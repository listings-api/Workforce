#!/usr/bin/env python
"""Live checks for the `wf` team plugin, run against the installed claude and codex (subscriptions only).

    .venv/bin/python scripts/team_livetest.py [--keep]

Everything happens in a throwaway repo under /private/tmp/wf-livetest-<ts>. Our own servers and status line run with
HOME pointed at a temp home (CODEX_HOME keeps the real Codex login), so nothing is written to the real home. The
only real-home read is ~/.workforce/team.toml, which is copied into the temp home when it exists. The `claude -p`
runs need the real login, so they use the real HOME: the plugin's hooks there only read ~/.workforce and never write it
unless they hit an internal error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"
PLUGIN = ROOT / "wf-plugin"
HAIKU = "claude-haiku-4-5-20251001"
TOOL_NAMES = [
    "codex_plan",
    "codex_ask",
    "codex_review",
    "claude_review",
    "review_status",
    "usage",
    "laya",
    "codex_settings",
]
PLUGIN_PREFIX = "mcp__plugin_workforce_codex__"
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

OK, FAIL, SKIP = "✓", "✗", "SKIP"
RESULTS: list[tuple[str, str, str]] = []


class Skip(Exception):
    pass


def report(name: str, mark: str, evidence: str) -> None:
    RESULTS.append((name, mark, evidence))
    lines = evidence.strip().splitlines() or [""]
    print(f"{mark} {name}: {lines[0]}", flush=True)
    for line in lines[1:]:
        print(f"    {line}", flush=True)


def check(name: str) -> Callable:
    def wrap(fn: Callable) -> Callable:
        def run(*args, **kwargs):
            started = time.monotonic()
            try:
                ok, evidence = fn(*args, **kwargs)
                mark = OK if ok else FAIL
            except Skip as exc:
                mark, evidence = SKIP, f"(not built yet) {exc}"
            except Exception as exc:  # a broken check must not stop the rest
                mark, evidence = FAIL, f"{type(exc).__name__}: {exc}"
            report(name, mark, f"{evidence}  [{time.monotonic() - started:.1f}s]")

        return run

    return wrap


def module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def need_modules(*names: str) -> None:
    for name in names:
        if not module_exists(name):
            raise Skip(f"{name} does not exist")


def clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in API_KEY_VARS}
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.update(extra or {})
    return env


# ------------------------------------------------------------------ throwaway repo and home


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def make_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "user.name", "wf-livetest")
    git(repo, "config", "user.email", "wf-livetest@example.invalid")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (repo / "README.md").write_text("# tiny project\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def make_home(base: Path) -> Path:
    home = base / "home"
    (home / ".workforce").mkdir(parents=True)
    real = Path.home() / ".workforce" / "team.toml"
    if real.is_file():  # the one permitted real-home read
        shutil.copy(real, home / ".workforce" / "team.toml")
    absolute_binary_paths(home / ".workforce" / "team.toml")
    return home


def absolute_binary_paths(toml: Path) -> None:
    """`~/…` in team.toml would expand against the temp HOME the servers run with, so write the real home's path instead."""
    if toml.is_file():
        toml.write_text(toml.read_text().replace('"~/', f'"{Path.home()}/'))
    else:
        toml.write_text(f'claude = "{Path.home()}/.local/bin/claude"\n')


def exclude_strays(repo: Path, keep: tuple[str, ...]) -> list[str]:
    """Untracked files that the earlier claude runs left in the throwaway repo (not part of the change under review)."""
    paths = [line[3:] for line in git(repo, "status", "--porcelain", "--untracked-files=all").splitlines() if line.startswith("?? ")]
    strays = [path for path in paths if path not in keep]
    if strays:
        with open(repo / ".git" / "info" / "exclude", "a") as handle:
            handle.write("".join(f"/{path}\n" for path in strays))
    return strays


def server_env(home: Path, repo: Path) -> dict:
    return clean_env(
        {
            "HOME": str(home),
            "CODEX_HOME": str(Path.home() / ".codex"),  # keep the real Codex login while HOME is the temp home
            "CLAUDE_PROJECT_DIR": str(repo),
        }
    )


def review_server_env(home: Path, repo: Path) -> dict:
    """For the real reviewers: the real HOME (so claude and codex find their subscription logins), while WF_HOME sends
    everything WorkForce writes (team.toml, the approvals key, caches, logs) to the temp home."""
    return clean_env({"WF_HOME": str(home), "CLAUDE_PROJECT_DIR": str(repo)})


# ------------------------------------------------------------------ JSON-RPC client for the MCP server


class McpClient:
    def __init__(self, env: dict, cwd: Path):
        self.proc = subprocess.Popen(
            [str(PY), "-m", "workforce.team.mcp_server"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.lines: queue.Queue = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, method: str, params: dict | None = None, notify: bool = False) -> int | None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        request_id = None
        if not notify:
            self.next_id += 1
            request_id = message["id"] = self.next_id
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        return request_id

    def request(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        request_id = self.send(method, params)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no reply to {method} within {timeout:.0f}s")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                raise TimeoutError(f"no reply to {method} within {timeout:.0f}s") from None
            if line is None:
                raise RuntimeError(f"server exited: {self.proc.stderr.read()[-500:]}")
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if reply.get("id") == request_id:
                return reply

    def handshake(self) -> dict:
        reply = self.request(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "wf-livetest", "version": "1"}},
        )
        self.send("notifications/initialized", notify=True)
        return reply

    def call(self, name: str, arguments: dict | None = None, timeout: float = 60) -> tuple[bool, str]:
        reply = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        if "error" in reply:
            return True, f"JSON-RPC error: {reply['error']}"
        result = reply["result"]
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
        return bool(result.get("isError")), text

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


# ------------------------------------------------------------------ claude -p helpers


def claude_bin() -> str:
    for candidate in (Path.home() / ".local" / "bin" / "claude", shutil.which("claude")):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise Skip("claude binary not found")


def run_claude(repo: Path, prompt: str, *extra: str, timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = [claude_bin(), "-p", prompt, "--plugin-dir", str(PLUGIN), "--model", HAIKU, *extra]
    return subprocess.run(cmd, cwd=repo, env=clean_env(), capture_output=True, text=True, timeout=timeout)


def commit_count(repo: Path) -> int:
    return int(git(repo, "rev-list", "--count", "HEAD"))


GATE_PROMPT = (
    "Run exactly: git add -A && git commit -m test\n"
    "The user has already authorised this. Do not ask for confirmation; just run the command with the Bash tool."
)


def gate_attempt(repo: Path, expect_commit: bool = False) -> tuple[subprocess.CompletedProcess, int, int]:
    """Ask claude to commit. Haiku sometimes asks instead of acting, so one retry if it made no attempt at all."""
    before = commit_count(repo)
    for _ in range(2):
        done = run_claude(
            repo,
            GATE_PROMPT,
            "--permission-mode",
            "acceptEdits",
            # headless acceptEdits still asks before Bash; allow just these two so only OUR hook decides the commit
            "--allowedTools",
            "Bash(git add:*)",
            "Bash(git commit:*)",
            "--output-format",
            "stream-json",
            "--verbose",
        )
        after = commit_count(repo)
        attempted = '"name":"Bash"' in done.stdout.replace(" ", "")
        if after != before or attempted or deny_evidence(done.stdout):
            break
    return done, before, after


def deny_evidence(stream: str) -> str | None:
    """The hook's deny reason as seen in the stream, or None."""
    for line in stream.splitlines():
        if "must both approve" in line or "WorkForce blocked" in line:
            match = re.search(r"run /review[^\"]*", line)
            return (match.group(0) if match else line)[:300]
    return None


# ------------------------------------------------------------------ the checks


@check("1. plugin loads (claude -p lists the 8 MCP tools)")
def check_plugin_loads(repo: Path):
    if not (PLUGIN / ".claude-plugin" / "plugin.json").is_file():
        raise Skip("wf-plugin/.claude-plugin/plugin.json is missing")
    done = run_claude(
        repo,
        f"List the MCP tools whose names start with {PLUGIN_PREFIX}, one per line, nothing else.",
        "--output-format",
        "json",
    )
    if done.returncode != 0:
        return False, f"claude exited {done.returncode}: {(done.stderr or done.stdout).strip()[:400]}"
    try:
        text = json.loads(done.stdout).get("result", "")
    except ValueError:
        text = done.stdout
    missing = [t for t in TOOL_NAMES if PLUGIN_PREFIX + t not in text]
    if missing:
        return False, f"missing {missing}; claude said: {text.strip()[:400]}"
    return True, f"all 8 tools listed: {', '.join(PLUGIN_PREFIX + t for t in TOOL_NAMES[:2])}, …"


@check("2. MCP server alone (initialize + tools/list)")
def check_server_alone(home: Path, repo: Path):
    need_modules("workforce.team.mcp_server")
    client = McpClient(server_env(home, repo), repo)
    try:
        init = client.handshake()
        info = init.get("result", {}).get("serverInfo")
        listed = client.request("tools/list").get("result", {}).get("tools", [])
    finally:
        client.close()
    names = sorted(t.get("name") for t in listed)
    missing = [t for t in TOOL_NAMES if t not in names]
    if missing:
        return False, f"missing {missing}; got {names}"
    return True, f"serverInfo={info}; tools={names}"


@check("3. codex_settings and usage (direct JSON-RPC)")
def check_settings_usage(home: Path, repo: Path):
    need_modules("workforce.team.mcp_server")
    client = McpClient(server_env(home, repo), repo)
    try:
        client.handshake()
        err, defaults = client.call("codex_settings")
        if err:
            return False, f"codex_settings read failed: {defaults[:300]}"
        err, changed = client.call("codex_settings", {"model": "gpt-6-luna", "effort": "low"})
        if err or "gpt-6-luna" not in changed:
            return False, f"codex_settings set failed: {changed[:300]}"
        err, back = client.call("codex_settings")
        if err or "gpt-6-luna" not in back:
            return False, f"codex_settings did not persist (temp home): {back[:300]}"
        err, usage = client.call("usage", timeout=90)
    finally:
        client.close()
    if err:
        return False, f"usage failed: {usage[:400]}"
    if "codex" not in usage.lower():
        return False, f"usage has no codex data: {usage[:400]}"
    warning = re.search(r'"warning": "([^"]*)"', usage)
    return (
        warning is None,
        f"settings default: {defaults.strip()[:160]}; roundtrip ok\nusage: {usage.strip()[:500]}",
    )


@check("4. codex_plan (real Codex, gpt-6-luna low)")
def check_codex_plan(home: Path, repo: Path):
    need_modules("workforce.team.mcp_server")
    client = McpClient(server_env(home, repo), repo)
    try:
        client.handshake()
        err, text = client.call(
            "codex_plan",
            {"task": "Add a subtract(a, b) function to calc.py with a test. Keep the plan to 3 steps.", "model": "gpt-6-luna", "effort": "low"},
            timeout=600,
        )
    finally:
        client.close()
    if err:
        return False, f"codex_plan failed: {text[:500]}"
    session = re.search(r"session_id:\s*([0-9a-zA-Z-]+)", text)
    if not session or not text.strip():
        return False, f"no session_id/text in reply: {text[:500]}"
    return True, f"session_id={session.group(1)}; plan: {text.strip()[:300]}"


@check("5. commit gate blocks an unreviewed commit")
def check_gate_blocks(repo: Path):
    need_modules("workforce.team.hooks", "workforce.team.approvals")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    done, before, after = gate_attempt(repo)
    reason = deny_evidence(done.stdout)
    if after != before:
        return False, f"commit HAPPENED ({before} -> {after} commits); the gate did not hold"
    if not reason:
        return False, f"commit did not happen, but no hook deny reason in the stream (claude exit {done.returncode}); tail: {done.stdout[-400:]}"
    if "review" not in reason.lower():
        return False, f"deny reason does not mention review: {reason}"
    return True, f"commits {before} -> {after}; hook said: {reason}"


def hook_decision(home: Path, repo: Path, command: str) -> dict | None:
    """What the plugin's PreToolUse hook says about a Bash command (run as its own process, with `home` as HOME)."""
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    done = subprocess.run(
        [str(PY), "-m", "workforce.team.hooks", "pretooluse"],
        input=json.dumps(payload), capture_output=True, text=True, cwd=repo, timeout=120, env=clean_env({"HOME": str(home)}),
    )
    if done.returncode != 0:
        raise RuntimeError(f"hook exited {done.returncode}: {done.stderr[-300:]}")
    return json.loads(done.stdout) if done.stdout.strip() else None


def denies(decision: dict | None) -> bool:
    return bool(decision and decision.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")


@check("6. claude_review + codex_review (run by the server, signed) open the gate; a forged approval does not")
def check_gate_opens(home: Path, repo: Path):
    need_modules("workforce.team.hooks", "workforce.team.approvals", "workforce.team.mcp_server")
    from workforce.team import approvals, config

    # cheapest models, temp home only: Haiku (low) for the Claude reviewer, gpt-6-luna (low) for Codex
    config.set_team_models(reviewer_model=HAIKU, reviewer_effort="low", home=home)
    (repo / "test_calc.py").write_text(
        "from calc import add, sub\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n"
    )
    strays = exclude_strays(repo, keep=("calc.py", "test_calc.py"))  # left by the earlier claude runs, not part of the change
    git(repo, "add", "-A")  # a plain `git commit` records the index, so what is reviewed must be what is staged
    focus = "Small change: a sub(a, b) function and its test. Approve unless something is actually wrong."
    client = McpClient(review_server_env(home, repo), repo)
    try:
        client.handshake()
        claude_err, claude_text = client.call("claude_review", {"focus": focus}, timeout=900)
        codex_err, codex_text = client.call("codex_review", {"focus": focus, "model": "gpt-6-luna", "effort": "low"}, timeout=900)
    finally:
        client.close()
    if claude_err or codex_err:
        return False, f"a review call failed: claude={claude_text[:300]!r} codex={codex_text[:300]!r}"
    state = approvals.status(repo, home)
    verdicts = {who: (state[who] or {}).get("verdict") for who in approvals.REVIEWERS}
    if None in verdicts.values():
        return False, f"a verdict was not recorded with a valid signature: {verdicts}"
    key = home / ".workforce" / "team.key"
    if oct(key.stat().st_mode & 0o777) != "0o600":
        return False, f"team.key mode is {oct(key.stat().st_mode & 0o777)}, not 0600"
    command = "git commit -m livetest"
    if not state["commit_allowed"]:
        stayed = denies(hook_decision(home, repo, command))
        return False, f"the reviewers did not both approve {verdicts} (the hook {'did' if stayed else 'did NOT'} keep the gate closed); {state['missing']}"
    if denies(hook_decision(home, repo, command)):
        return False, "both approved, but the hook still denies the commit"
    path = approvals.approvals_path(repo)
    genuine = path.read_text()
    forged = {state["tree"]: {who: {"verdict": "APPROVE", "summary": "forged", "findings": [], "at": "now"} for who in approvals.REVIEWERS}}
    path.write_text(json.dumps(forged))
    try:
        forged_denied = denies(hook_decision(home, repo, command))
    finally:
        path.write_text(genuine)
    if not forged_denied:
        return False, "an unsigned approvals file was accepted by the hook"
    before = commit_count(repo)
    git(repo, "commit", "-q", "-m", "livetest: reviewed and approved")
    after = commit_count(repo)
    return after == before + 1, (
        f"tree {state['tree'][:10]} approved by both {verdicts}; unsigned forgery denied; key mode 0600; "
        f"commits {before} -> {after}; claude: {claude_text.strip()[:160]!r}; left out as strays: {strays}"
    )


@check("7. status line format")
def check_statusline(home: Path, repo: Path):
    need_modules("workforce.team.statusline")
    sample = {
        "model": {"display_name": "Opus 5.5"},
        "workspace": {"current_dir": str(repo)},
        "rate_limits": {
            "five_hour": {"used_percentage": 23, "resets_at": int(time.time()) + 3600},
            "seven_day": {"used_percentage": 44, "resets_at": int(time.time()) + 86400},
        },
    }
    done = subprocess.run(
        [str(PY), "-m", "workforce.team.statusline"],
        input=json.dumps(sample),
        env=clean_env({"HOME": str(home), "CODEX_HOME": str(Path.home() / ".codex")}),
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    line = ANSI.sub("", done.stdout).strip()
    pattern = r"^WF │ Claude 5h 23% · wk 44% │ Codex .+ │ codex \S+·\S+$"
    cache = home / ".workforce" / "usage.json"
    cached = cache.is_file() and "23" in cache.read_text()
    if done.returncode != 0 or not re.match(pattern, line):
        return False, f"unexpected line: {line!r} (exit {done.returncode}) {done.stderr[-200:]}"
    if not cached:
        return False, f"line ok ({line!r}) but usage.json was not written in the temp home"
    return True, line


@check("8. launcher argv")
def check_launcher(home: Path):
    need_modules("workforce.team.launch")
    import io

    from workforce.team import launch

    plugin, python = launch.plugin_dir(), launch.venv_python()
    team_text = (plugin / launch.TEAM_FILE).read_text()
    argv = launch.build_argv("claude", plugin, python, team_text, [])
    needed = ("--plugin-dir", "--settings", "--append-system-prompt")
    lacking = [flag for flag in needed if flag not in argv]
    if lacking:
        return False, f"build_argv is missing {lacking}: {argv[:6]}"
    seen: list = []
    out = io.StringIO()
    code = launch.main(
        ["--resume"],
        home=home,
        environ={"PATH": os.environ.get("PATH", "")},
        execvp=lambda binary, command: seen.append((binary, command)),
        stdout=out,
    )
    if code != 0 or not seen:
        return False, f"dry-run main() returned {code} without exec; output: {out.getvalue()[-200:]}"
    command = seen[0][1]
    if not all(flag in command for flag in needed) or command[-1] != "--resume":
        return False, f"exec argv wrong: {command[:8]}"
    return True, f"argv flags present; dry-run exec: {command[0]} {' '.join(command[1:4])[:80]}… {command[-1]}; banner rows: {len(out.getvalue().splitlines())}"


def wait_for_refresh(timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = subprocess.run(["pgrep", "-f", "workforce.team.statusline --refresh-codex"], capture_output=True)
        if found.returncode != 0:
            return
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live checks for the wf team plugin (real claude/codex, subscription only).")
    parser.add_argument("--keep", action="store_true", help="keep the temp repo and home instead of deleting them")
    args = parser.parse_args()
    if not PY.is_file():
        print(f"{PY} is missing; create the venv first.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(ROOT))
    base = Path(f"/private/tmp/wf-livetest-{int(time.time())}")
    base.mkdir(parents=True)
    repo, home = make_repo(base), make_home(base)
    print(f"temp repo: {repo}\ntemp home: {home}\n", flush=True)
    try:
        check_plugin_loads(repo)
        check_server_alone(home, repo)
        check_settings_usage(home, repo)
        check_codex_plan(home, repo)
        check_gate_blocks(repo)
        check_gate_opens(home, repo)
        check_statusline(home, repo)
        check_launcher(home)
    finally:
        if args.keep:
            print(f"\nkept: {base}")
        else:
            wait_for_refresh()  # the status line's detached Codex refresh would recreate the temp home after we delete it
            shutil.rmtree(base, ignore_errors=True)
    failed = [n for n, mark, _ in RESULTS if mark == FAIL]
    skipped = [n for n, mark, _ in RESULTS if mark == SKIP]
    print(f"\n{len(RESULTS) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
