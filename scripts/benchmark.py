#!/usr/bin/env python
"""Measure the WorkForce team (`wf -p`) against a single Claude Code agent (`claude -p`) on small, seeded tasks.

    .venv/bin/python scripts/benchmark.py list
    .venv/bin/python scripts/benchmark.py run --mode both --claude-model claude-haiku-4-5-20251001 \\
        --codex-model gpt-6-luna --codex-effort low --repeat 1 --out results.json
    .venv/bin/python scripts/benchmark.py report results.json > report.md

Every run happens in a fresh throwaway git repo under a temp directory. Team runs get a temp WF_HOME whose team.toml
holds the models given on the command line, so your own settings are never read or changed. Hidden tests judge
correctness afterwards and are never copied into the agent's repo. Subscription only: the script refuses to start when an
API key variable is set.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = Path(__file__).resolve().parent / "bench_tasks"

MODES = ("solo", "team")
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PREFLIGHT = 3

API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")
COMMIT_SUFFIX = (
    "\n\nWhen the work is finished and the tests pass, commit it to git with a clear commit message. "
    "Do not push."
)
TEAM_TOOLS_ALLOW = "mcp__plugin_workforce_codex"
ALLOWED_TOOLS = (
    "Edit",
    "Write",
    "Bash(python:*)",
    "Bash(python3:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(ls:*)",
    TEAM_TOOLS_ALLOW,
)
CODEX_TOOLS = ("codex_plan", "codex_ask", "codex_review")
CLAUDE_REVIEW_TOOL = "claude_review"
VERDICTS = ("APPROVE", "REQUEST_CHANGES", "BLOCKED")
DEFAULT_TIMEOUT_S = 1800
HIDDEN_TIMEOUT_S = 120
GITIGNORE = "__pycache__/\n*.pyc\n"
CLAUDE_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
CODEX_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


class Task:
    """A benchmark task folder: `seed/`, `task.md`, `hidden_test.py` (and an optional `reference/` solution)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.name = self.path.name
        self.text = (self.path / "task.md").read_text(encoding="utf-8").strip()

    @property
    def seed(self) -> Path:
        return self.path / "seed"

    @property
    def hidden_test(self) -> Path:
        return self.path / "hidden_test.py"

    def prompt(self) -> str:
        return self.text + COMMIT_SUFFIX


def discover_tasks(tasks_dir: Path = TASKS_DIR, names: Sequence[str] | None = None) -> list[Task]:
    """Every valid task folder (sorted), or exactly the named ones; an unknown or incomplete name is an error."""
    tasks_dir = Path(tasks_dir)
    found = {
        p.name: p
        for p in sorted(tasks_dir.iterdir())
        if p.is_dir() and (p / "task.md").is_file() and (p / "seed").is_dir() and (p / "hidden_test.py").is_file()
    }
    if names is None:
        return [Task(p) for p in found.values()]
    unknown = [name for name in names if name not in found]
    if unknown:
        raise ValueError(f"unknown task(s) {unknown}; available: {sorted(found)}")
    return [Task(found[name]) for name in names]


def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def make_repo(task: Task, dest: Path) -> Path:
    """A fresh git repo at `dest` holding a copy of the task's seed as one initial commit."""
    dest = Path(dest)
    shutil.copytree(task.seed, dest)
    if not (dest / ".gitignore").exists():
        (dest / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.name", "wf-benchmark")
    git(dest, "config", "user.email", "wf-benchmark@example.invalid")
    git(dest, "config", "commit.gpgsign", "false")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "initial")
    return dest


def write_team_toml(
    home: Path, claude: str, codex: str, codex_model: str, codex_effort: str, claude_model: str, claude_effort: str
) -> Path:
    """`<home>/.workforce/team.toml` with absolute binary paths and the models under test (Claude reviewer and sub-agent too)."""
    path = Path(home) / ".workforce" / "team.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'claude = "{claude}"',
        f'codex = "{codex}"',
        f'codex_model = "{codex_model}"',
        f'codex_effort = "{codex_effort}"',
        f'reviewer_model = "{claude_model}"',
        f'reviewer_effort = "{claude_effort}"',
        f'fast_coder_model = "{claude_model}"',
        f'fast_coder_effort = "{claude_effort}"',
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def claude_args(prompt: str, model: str, effort: str) -> list[str]:
    """The arguments both modes share: the same prompt, model, effort, permission mode and allowed tools."""
    return [
        "-p",
        prompt,
        "--model",
        model,
        "--effort",
        effort,
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        ",".join(ALLOWED_TOOLS),
    ]


def build_argv(mode: str, binary: str | Path, prompt: str, model: str, effort: str) -> list[str]:
    """`claude -p …` for solo, `wf -p …` for team: identical arguments, only the executable differs."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    return [str(binary), *claude_args(prompt, model, effort)]


def api_key_vars(environ: Mapping[str, str]) -> list[str]:
    return [name for name in API_KEY_VARS if environ.get(name)]


def run_command(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout_s: float) -> dict:
    """Run one command in its own process group; on timeout the whole group is killed. Returns text, exit code and seconds."""
    started = time.monotonic()
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        stdout, stderr = proc.communicate()
    return {
        "stdout": stdout or "",
        "stderr": stderr or "",
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 2),
    }


def _events(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        events = []
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                events.append(item)
        return events
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return [data] if isinstance(data, dict) else []


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_claude_output(stdout: str) -> dict:
    """The `result` object of `claude -p --output-format json` (an object, or the last `result` event of a list)."""
    result = None
    for event in _events(stdout):
        if event.get("type") == "result" or "usage" in event or "total_cost_usd" in event:
            result = event
    if result is None:
        return {"parsed": False}
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    tokens = {field: _number(usage.get(field)) for field in CLAUDE_TOKEN_FIELDS}
    models = {}
    model_usage = result.get("modelUsage")
    if isinstance(model_usage, dict):
        for name, info in model_usage.items():
            if isinstance(info, dict):
                models[str(name)] = {
                    "input_tokens": _number(info.get("inputTokens")),
                    "output_tokens": _number(info.get("outputTokens")),
                    "cost_usd": _number(info.get("costUSD")),
                }
    text = result.get("result")
    return {
        "parsed": True,
        "is_error": bool(result.get("is_error")),
        "subtype": result.get("subtype"),
        "session_id": result.get("session_id"),
        "num_turns": _number(result.get("num_turns")),
        "duration_ms": _number(result.get("duration_ms")),
        "cost_usd_estimate": _number(result.get("total_cost_usd")),
        "tokens": tokens,
        "models": models,
        "result_excerpt": text[:300] if isinstance(text, str) else None,
    }


def parse_team_log(path: Path) -> list[dict]:
    """The JSONL entries of `team.log` (tool, seconds, ok); unreadable lines are skipped."""
    entries = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("tool"), str):
            entries.append(item)
    return entries


def summarize_calls(entries: Sequence[dict]) -> dict:
    """Codex tool calls (count and seconds) and Claude reviewer calls from `team.log` entries."""

    def pick(names: Sequence[str]) -> list[dict]:
        return [
            {"tool": e["tool"], "seconds": _number(e.get("seconds")) or 0.0, "ok": bool(e.get("ok"))}
            for e in entries
            if e["tool"] in names
        ]

    codex = pick(CODEX_TOOLS)
    claude = pick((CLAUDE_REVIEW_TOOL,))
    return {
        "codex_calls": codex,
        "codex_call_count": len(codex),
        "codex_call_seconds": round(sum(c["seconds"] for c in codex), 2),
        "claude_review_calls": claude,
        "claude_review_call_count": len(claude),
        "claude_review_call_seconds": round(sum(c["seconds"] for c in claude), 2),
    }


_OUTPUT_MARKER = "===== CODEX OUTPUT (verbatim) =====\n"
_ARTIFACT_NAME = re.compile(r"(claude-review|review)-(\d+)\.md")
_SESSION_LINE = re.compile(r"codex session: (\S+)")
_VERDICT_TEXT = re.compile(r'"verdict"\s*:\s*"(APPROVE|REQUEST_CHANGES|BLOCKED)"')


def parse_review_reply(text: str) -> dict:
    """Verdict and finding count from a reviewer's raw reply (JSON when it parses, else a scan for the verdict)."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("verdict") in VERDICTS:
        findings = data.get("findings")
        return {"verdict": data["verdict"], "findings": len(findings) if isinstance(findings, list) else None}
    match = _VERDICT_TEXT.search(text)
    return {"verdict": match.group(1) if match else None, "findings": None}


def parse_artifacts(repo: Path) -> dict:
    """Review verdicts and Codex session ids from the repo's `.workforce/team/*.md` artefacts, in file-number order."""
    directory = Path(repo) / ".workforce" / "team"
    reviews: list[dict] = []
    sessions: list[str] = []
    if not directory.is_dir():
        return {"reviews": reviews, "codex_sessions": sessions}
    files = sorted(
        (p for p in directory.iterdir() if _ARTIFACT_NAME.fullmatch(p.name)),
        key=lambda p: (int(_ARTIFACT_NAME.fullmatch(p.name).group(2)), p.name),
    )
    for path in files:
        kind = _ARTIFACT_NAME.fullmatch(path.name).group(1)
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        header = body.split("\n", 3)[:3]
        reviewer = "claude" if kind == "claude-review" else "codex"
        if reviewer == "codex":
            for line in header:
                match = _SESSION_LINE.search(line)
                if match and match.group(1) != "unknown" and match.group(1) not in sessions:
                    sessions.append(match.group(1))
        reply = body.split(_OUTPUT_MARKER, 1)[1] if _OUTPUT_MARKER in body else ""
        reviews.append({"reviewer": reviewer, "file": path.name, **parse_review_reply(reply.strip())})
    for other in sorted(directory.glob("plan-*.md")) + sorted(directory.glob("ask-*.md")):
        try:
            head = other.read_text(encoding="utf-8").split("\n", 3)[:3]
        except OSError:
            continue
        for line in head:
            match = _SESSION_LINE.search(line)
            if match and match.group(1) != "unknown" and match.group(1) not in sessions:
                sessions.append(match.group(1))
    return {"reviews": reviews, "codex_sessions": sessions}


def review_summary(reviews: Sequence[dict]) -> dict:
    counted = [r["findings"] for r in reviews if isinstance(r.get("findings"), int)]
    return {
        "review_count": len(reviews),
        "changes_requested": sum(1 for r in reviews if r.get("verdict") == "REQUEST_CHANGES"),
        "findings_total": sum(counted) if counted else (0 if reviews else None),
        "final_verdicts": {
            who: next((r["verdict"] for r in reversed(reviews) if r["reviewer"] == who), None)
            for who in ("claude", "codex")
        },
    }


def codex_session_usage(session_ids: Sequence[str], codex_home: Path | None) -> dict | None:
    """Codex token totals for the given sessions, read (read-only) from `<codex_home>/sessions/**/rollout-*-<id>.jsonl`.

    Returns None when no rollout exposes a token count, so callers never report a guess.
    """
    home = Path(codex_home) if codex_home else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    root = home / "sessions"
    totals = {field: 0 for field in CODEX_TOKEN_FIELDS}
    found = 0
    if not root.is_dir():
        return None
    for session in dict.fromkeys(session_ids):
        for rollout in root.rglob(f"rollout-*{session}*.jsonl"):
            last = None
            try:
                with open(rollout, encoding="utf-8") as handle:
                    for line in handle:
                        if '"token_count"' not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        payload = event.get("payload") if isinstance(event, dict) else None
                        info = payload.get("info") if isinstance(payload, dict) else None
                        usage = info.get("total_token_usage") if isinstance(info, dict) else None
                        if isinstance(usage, dict):
                            last = usage
            except OSError:
                continue
            if last is None:
                continue
            found += 1
            for field in CODEX_TOKEN_FIELDS:
                totals[field] += _number(last.get(field)) or 0
    if not found:
        return None
    return {**totals, "sessions_with_usage": found}


def read_codex_usage(codex_bin: str | Path) -> dict:
    """Codex weekly and 5-hour usage percent via `codex app-server` (costs no model quota); errors become a note, not a guess."""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from workforce.usage import codex_limits
        from workforce.usage.claude_limits import KIND_5H, KIND_WEEKLY

        windows = codex_limits.read(codex_bin)
    except Exception as exc:
        return {"weekly": None, "five_hour": None, "error": f"{type(exc).__name__}: {exc}"}
    by_kind = {w.kind: w.percent for w in windows}
    return {"weekly": by_kind.get(KIND_WEEKLY), "five_hour": by_kind.get(KIND_5H), "error": None}


def run_hidden_test(task: Task, repo: Path, python: str | Path = sys.executable) -> dict:
    """Run the task's hidden unittest against the repo's working tree (the test file never enters the repo)."""
    env = {**os.environ, "PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
    argv = [str(python), "-m", "unittest", "discover", "-s", str(task.path), "-p", "hidden_test.py", "-t", str(task.path)]
    try:
        done = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, timeout=HIDDEN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"passed": False, "tests_run": None, "failed": None, "output_tail": f"timed out after {HIDDEN_TIMEOUT_S}s"}
    output = done.stderr + done.stdout
    ran = re.search(r"Ran (\d+) tests?", output)
    failed = re.search(r"FAILED \((.*?)\)", output)
    count = 0
    if failed:
        count = sum(int(n) for n in re.findall(r"=(\d+)", failed.group(1)))
    tests_run = int(ran.group(1)) if ran else None
    return {
        "passed": done.returncode == 0 and bool(tests_run),
        "tests_run": tests_run,
        "failed": count if failed else 0 if tests_run else None,
        "output_tail": "\n".join(output.strip().splitlines()[-12:]),
    }


def repo_state(repo: Path) -> dict:
    """Whether the agent committed, and whether anything was left uncommitted."""
    try:
        commits = int(git(repo, "rev-list", "--count", "HEAD"))
        dirty = bool(git(repo, "status", "--porcelain"))
    except (RuntimeError, ValueError):
        return {"commit_made": False, "commits": None, "uncommitted_changes": None}
    return {"commit_made": commits > 1, "commits": commits, "uncommitted_changes": dirty}


def run_one(
    task: Task,
    mode: str,
    repeat: int,
    workdir: Path,
    *,
    claude_bin: str,
    codex_bin: str,
    wf_bin: str,
    claude_model: str,
    claude_effort: str,
    codex_model: str | None,
    codex_effort: str | None,
    timeout_s: float,
    environ: Mapping[str, str] | None = None,
    usage_reader: Callable[[str], dict] = read_codex_usage,
    codex_home: Path | None = None,
    python: str | Path = sys.executable,
) -> dict:
    """One benchmark run in a fresh repo (and, for team, a fresh WF_HOME); always returns a record, failures included."""
    record: dict[str, Any] = {"task": task.name, "mode": mode, "repeat": repeat, "error": None}
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record["started_at"] = started_at
    try:
        repo = make_repo(task, Path(workdir) / f"{task.name}-{mode}-{repeat}" / "repo")
        env = {k: v for k, v in (environ if environ is not None else os.environ).items()}
        env["GIT_TERMINAL_PROMPT"] = "0"
        binary = claude_bin
        home = None
        usage_before = None
        if mode == "team":
            home = Path(workdir) / f"{task.name}-{mode}-{repeat}" / "wf-home"
            home.mkdir(parents=True)
            write_team_toml(home, claude_bin, codex_bin, codex_model or "", codex_effort or "", claude_model, claude_effort)
            env["WF_HOME"] = str(home)
            binary = wf_bin
            usage_before = usage_reader(codex_bin)
        argv = build_argv(mode, binary, task.prompt(), claude_model, claude_effort)
        ran = run_command(argv, repo, env, timeout_s)
        record.update(
            seconds=ran["seconds"],
            exit_code=ran["exit_code"],
            timed_out=ran["timed_out"],
            stderr_tail="\n".join(ran["stderr"].strip().splitlines()[-6:]),
        )
        claude = parse_claude_output(ran["stdout"])
        record["claude"] = claude
        record["ok"] = bool(
            not ran["timed_out"] and ran["exit_code"] == 0 and claude.get("parsed") and not claude.get("is_error")
        )
        if not record["ok"]:
            reason = "timed out" if ran["timed_out"] else f"exit code {ran['exit_code']}"
            if claude.get("parsed") and claude.get("is_error"):
                reason = f"claude reported an error: {claude.get('result_excerpt')}"
            elif not claude.get("parsed") and not ran["timed_out"]:
                reason += "; no result JSON in the output"
            record["error"] = reason
        record.update(repo_state(repo))
        record["hidden"] = run_hidden_test(task, repo, python)
        if mode == "team":
            usage_after = usage_reader(codex_bin)
            calls = summarize_calls(parse_team_log(home / ".workforce" / "team.log"))
            artifacts = parse_artifacts(repo)
            weekly_before, weekly_after = usage_before.get("weekly"), usage_after.get("weekly")
            delta = (
                round(weekly_after - weekly_before, 2)
                if isinstance(weekly_before, (int, float)) and isinstance(weekly_after, (int, float))
                else None
            )
            record["codex"] = {
                **calls,
                "sessions": artifacts["codex_sessions"],
                "tokens": codex_session_usage(artifacts["codex_sessions"], codex_home),
                "weekly_before": weekly_before,
                "weekly_after": weekly_after,
                "weekly_delta": delta,
                "usage_error": usage_before.get("error") or usage_after.get("error"),
            }
            record["reviews"] = artifacts["reviews"]
            record["review_summary"] = review_summary(artifacts["reviews"])
    except Exception as exc:
        record["ok"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def save_results(path: Path, data: dict) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 2) if values else None


def _sum_optional(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if isinstance(v, (int, float))]
    return round(sum(present), 4) if present else None


def summarize(runs: Sequence[dict]) -> dict[str, dict]:
    """Per-mode totals over every run, failed ones included (`failed` says how many)."""
    out: dict[str, dict] = {}
    for mode in MODES:
        rows = [r for r in runs if r.get("mode") == mode]
        if not rows:
            continue
        seconds = [r["seconds"] for r in rows if isinstance(r.get("seconds"), (int, float))]
        tokens = {
            f: _sum_optional([(r.get("claude") or {}).get("tokens", {}).get(f) for r in rows]) for f in CLAUDE_TOKEN_FIELDS
        }
        codex = [r.get("codex") or {} for r in rows]
        out[mode] = {
            "runs": len(rows),
            "failed": sum(1 for r in rows if not r.get("ok")),
            "commits": sum(1 for r in rows if r.get("commit_made")),
            "hidden_passed": sum(1 for r in rows if (r.get("hidden") or {}).get("passed")),
            "seconds_total": round(sum(seconds), 2),
            "seconds_mean": _mean(seconds),
            "seconds_median": round(statistics.median(seconds), 2) if seconds else None,
            "claude_tokens": tokens,
            "cost_usd_estimate": _sum_optional([(r.get("claude") or {}).get("cost_usd_estimate") for r in rows]),
            "codex_calls": sum(c.get("codex_call_count", 0) for c in codex),
            "codex_call_seconds": round(sum(c.get("codex_call_seconds", 0) for c in codex), 2),
            "claude_review_calls": sum(c.get("claude_review_call_count", 0) for c in codex),
            "codex_tokens_total": _sum_optional([(c.get("tokens") or {}).get("total_tokens") for c in codex]),
            "codex_weekly_delta": _sum_optional([c.get("weekly_delta") for c in codex]),
            "review_findings": _sum_optional([(r.get("review_summary") or {}).get("findings_total") for r in rows]),
            "changes_requested": sum((r.get("review_summary") or {}).get("changes_requested", 0) for r in rows),
        }
    return out


def paired(runs: Sequence[dict]) -> list[dict]:
    """Per task: mean seconds for each mode and team/solo ratio, for tasks that ran in both modes."""
    rows = []
    for name in dict.fromkeys(r["task"] for r in runs):
        solo = [r["seconds"] for r in runs if r["task"] == name and r["mode"] == "solo" and "seconds" in r]
        team = [r["seconds"] for r in runs if r["task"] == name and r["mode"] == "team" and "seconds" in r]
        if solo and team:
            rows.append(
                {"task": name, "solo_seconds": _mean(solo), "team_seconds": _mean(team), "ratio": round(_mean(team) / _mean(solo), 2)}
            )
    return rows


def fmt_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def fmt_num(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}"


def fmt_cost(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _pass(run: dict) -> str:
    hidden = run.get("hidden") or {}
    if not hidden:
        return "n/a"
    passed = "pass" if hidden.get("passed") else "FAIL"
    if hidden.get("tests_run"):
        return f"{passed} ({hidden['tests_run'] - (hidden.get('failed') or 0)}/{hidden['tests_run']})"
    return passed


def _status(run: dict) -> str:
    return "ok" if run.get("ok") else f"FAILED: {run.get('error') or 'unknown'}".replace("|", "/").replace("\n", " ")[:120]


def _reviews(run: dict) -> str:
    info = run.get("review_summary")
    if run.get("mode") != "team" or info is None:
        return "n/a" if run.get("mode") == "team" else "–"
    verdicts = " / ".join(f"{who} {v or 'none'}" for who, v in info["final_verdicts"].items())
    return f"{info['review_count']} reviews, {info['changes_requested']} changes requested, {fmt_num(info['findings_total'])} findings; last: {verdicts}"


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def render_report(data: dict) -> str:
    """Markdown for a results file: method, setup, per-run tables, totals, paired timing and caveats."""
    meta = data.get("meta", {})
    runs = data.get("runs", [])
    out = ["# Benchmark: the WorkForce team against a single agent", ""]
    out += [
        "## Method",
        "",
        "The same small tasks are given to two setups, each in a fresh throwaway git repo:",
        "",
        "- **solo**: plain Claude Code (`claude -p`, no WorkForce plugin).",
        "- **team**: `wf -p`, the real WorkForce path: Codex plans, Claude codes, and a commit needs both reviews to approve.",
        "",
        "Both get the same prompt (the task text, then \"commit when the tests pass\"), the same Claude model and effort, the same permission mode and allowed tools, and JSON output. "
        "Each task is a tiny stdlib-only Python project with defects a review could catch, for example an off-by-one page helper, daylight-saving edge cases, a path-traversal check and cent rounding. "
        "A hidden unittest file, which is never copied into the agent's repo, is run against the final working tree to judge correctness. "
        "Team runs use a temporary `WF_HOME` whose `team.toml` sets the models below, so nothing in your own settings is read or changed.",
        "",
        "Measured per run: wall-clock time; Claude's reported token usage and `total_cost_usd` (an API-equivalent estimate, not what a subscription is billed); "
        "the number and duration of Codex calls (from `team.log`); Codex token usage when the Codex session rollouts under `~/.codex/sessions` expose it; "
        "the change in Codex weekly usage percent (via `codex app-server`); whether a commit was made; hidden-test result; and review verdicts and findings (team only).",
        "",
    ]
    out += ["## Setup", ""]
    setup = [
        ("Date", meta.get("started_at", "n/a")),
        ("Claude model / effort", f"{meta.get('claude_model', 'n/a')} / {meta.get('claude_effort', 'n/a')}"),
        ("Codex model / effort", f"{meta.get('codex_model') or 'n/a'} / {meta.get('codex_effort') or 'n/a'}"),
        ("Tasks", ", ".join(meta.get("tasks", [])) or "n/a"),
        ("Modes", ", ".join(meta.get("modes", [])) or "n/a"),
        ("Repeats per task and mode", str(meta.get("repeat", "n/a"))),
        ("Per-run timeout", fmt_seconds(meta.get("timeout_s"))),
        ("Claude Code", meta.get("claude_version") or "n/a"),
        ("Codex", meta.get("codex_version") or "n/a"),
    ]
    out += [table(["Setting", "Value"], [[a, b] for a, b in setup]), ""]
    if not runs:
        return "\n".join(out + ["No runs recorded.", ""])

    out += ["## Per-run results", ""]
    rows = []
    for r in runs:
        codex = r.get("codex") or {}
        rows.append(
            [
                r["task"],
                r["mode"],
                str(r.get("repeat", 1)),
                fmt_seconds(r.get("seconds")),
                "yes" if r.get("commit_made") else "no",
                _pass(r),
                f"{codex.get('codex_call_count', 0)} ({fmt_seconds(codex.get('codex_call_seconds'))})" if r["mode"] == "team" else "–",
                _reviews(r),
                _status(r),
            ]
        )
    out += [table(["Task", "Mode", "Run", "Time", "Commit", "Hidden tests", "Codex calls (time)", "Reviews", "Status"], rows), ""]

    out += ["### Usage per run", ""]
    rows = []
    for r in runs:
        claude = r.get("claude") or {}
        tokens = claude.get("tokens") or {}
        codex = r.get("codex") or {}
        codex_tokens = codex.get("tokens")
        rows.append(
            [
                r["task"],
                r["mode"],
                fmt_num(tokens.get("input_tokens")),
                fmt_num(tokens.get("output_tokens")),
                f"{fmt_num(tokens.get('cache_read_input_tokens'))} / {fmt_num(tokens.get('cache_creation_input_tokens'))}",
                fmt_cost(claude.get("cost_usd_estimate")),
                fmt_num(codex_tokens.get("total_tokens")) if codex_tokens else ("n/a" if r["mode"] == "team" else "–"),
                (f"{codex.get('weekly_before')}% → {codex.get('weekly_after')}%" if codex.get("weekly_before") is not None and codex.get("weekly_after") is not None else "n/a")
                if r["mode"] == "team"
                else "–",
            ]
        )
    out += [
        table(
            ["Task", "Mode", "Claude in", "Claude out", "Cache read / write", "Est. cost (API-equivalent)", "Codex tokens", "Codex weekly %"],
            rows,
        ),
        "",
        "Claude tokens are the `claude -p` result for the main agent only. The Claude reviewer that `claude_review` starts inside team runs is a separate headless Claude whose usage this result does not include. "
        "A run that timed out has no Claude figures (`n/a`), because the result is only printed when Claude finishes.",
        "",
    ]

    totals = summarize(runs)
    out += ["## Totals", ""]
    labels = [
        ("Runs (failed)", lambda t: f"{t['runs']} ({t['failed']})"),
        ("Commits made", lambda t: str(t["commits"])),
        ("Hidden tests passed", lambda t: f"{t['hidden_passed']}/{t['runs']}"),
        ("Time, total", lambda t: fmt_seconds(t["seconds_total"])),
        ("Time, mean per run", lambda t: fmt_seconds(t["seconds_mean"])),
        ("Time, median per run", lambda t: fmt_seconds(t["seconds_median"])),
        ("Claude input tokens", lambda t: fmt_num(t["claude_tokens"]["input_tokens"])),
        ("Claude output tokens", lambda t: fmt_num(t["claude_tokens"]["output_tokens"])),
        ("Claude cache read tokens", lambda t: fmt_num(t["claude_tokens"]["cache_read_input_tokens"])),
        ("Est. cost, Claude main agent (API-equivalent)", lambda t: fmt_cost(t["cost_usd_estimate"])),
        ("Codex calls (time)", lambda t: f"{t['codex_calls']} ({fmt_seconds(t['codex_call_seconds'])})"),
        ("Claude reviewer calls", lambda t: str(t["claude_review_calls"])),
        ("Codex tokens", lambda t: fmt_num(t["codex_tokens_total"])),
        ("Codex weekly % change (sum)", lambda t: "n/a" if t["codex_weekly_delta"] is None else f"{t['codex_weekly_delta']:+.2f} points"),
        ("Review findings", lambda t: fmt_num(t["review_findings"])),
        ("Reviews that requested changes", lambda t: str(t["changes_requested"])),
    ]
    modes = [m for m in MODES if m in totals]
    out += [table(["Measure", *modes], [[label, *[fn(totals[m]) for m in modes]] for label, fn in labels]), ""]

    pairs = paired(runs)
    if pairs:
        out += ["### Time, team against solo", ""]
        out += [
            table(
                ["Task", "Solo (mean)", "Team (mean)", "Team / solo"],
                [[p["task"], fmt_seconds(p["solo_seconds"]), fmt_seconds(p["team_seconds"]), f"{p['ratio']:.2f}x"] for p in pairs],
            ),
            "",
        ]

    out += [
        "## Caveats",
        "",
        "- The sample is tiny and not statistically significant: a few tasks, few or single runs, and model output varies from run to run. Treat every number as an anecdote, not a benchmark result.",
        "- The models are cheap ones chosen to keep the run affordable. They may behave differently from the models used day to day; rerun with your own models before drawing conclusions.",
        "- Subscription usage is coarse: the Codex weekly percent moves in whole steps and other use of the same account during a run adds noise; Claude subscription usage is not read at all. The cost figure is Claude's own API-equivalent estimate and is not a bill.",
        "- The Claude reviewer's usage inside team runs is not included in the Claude token and cost figures, so team Claude usage is understated.",
        "- Correctness is judged only by the hidden tests, which cover the planted edge cases. They say nothing about code quality, and they cannot show what a review changed along the way, because the state before the review is not captured.",
        "- Both modes load your own global Claude Code settings, and Claude Code and Codex versions can change results. A failed or timed-out run is kept in the tables and totals as a failure.",
        "",
    ]
    return "\n".join(out)


def _version(binary: str, flag: str = "--version") -> str | None:
    try:
        done = subprocess.run([binary, flag], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip().splitlines()[0] if done.returncode == 0 and done.stdout.strip() else None


def resolve_binary(explicit: str | None, name: str, candidates: Sequence[str]) -> str | None:
    """An absolute path to an executable: the explicit one, else the first candidate that exists, else the PATH lookup."""
    options = [explicit] if explicit else [*candidates, shutil.which(name)]
    for option in options:
        if not option:
            continue
        path = Path(option).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    return None


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="benchmark.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the tasks")
    run = sub.add_parser("run", help="run the benchmark")
    run.add_argument("--mode", choices=("solo", "team", "both"), default="both")
    run.add_argument("--tasks", help="comma-separated task names (default: all)")
    run.add_argument("--claude-model", required=True)
    run.add_argument("--claude-effort", default="medium", help="effort for Claude in both modes, and for the team's reviewer and sub-agent")
    run.add_argument("--codex-model")
    run.add_argument("--codex-effort")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--out", default="results.json")
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="seconds allowed per run")
    run.add_argument("--claude-bin")
    run.add_argument("--codex-bin")
    run.add_argument("--wf-bin")
    run.add_argument("--tasks-dir", default=str(TASKS_DIR))
    run.add_argument("--workdir", help="keep repos here instead of a deleted temp directory")
    report = sub.add_parser("report", help="print a markdown report for a results file")
    report.add_argument("results")
    return parser.parse_args(list(argv))


def _fail(message: str, code: int = EXIT_PREFLIGHT) -> int:
    print(f"✗ {message}", file=sys.stderr)
    return code


def cmd_run(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    bad = api_key_vars(environ)
    if bad:
        return _fail(f"{', '.join(bad)} is set. This benchmark uses the Claude and ChatGPT subscriptions only, never API keys.")
    modes = list(MODES) if args.mode == "both" else [args.mode]
    if "team" in modes and not (args.codex_model and args.codex_effort):
        return _fail("team runs need --codex-model and --codex-effort", EXIT_USAGE)
    if args.repeat < 1:
        return _fail("--repeat must be at least 1", EXIT_USAGE)
    try:
        tasks = discover_tasks(Path(args.tasks_dir), args.tasks.split(",") if args.tasks else None)
    except ValueError as exc:
        return _fail(str(exc), EXIT_USAGE)
    if not tasks:
        return _fail(f"no tasks found in {args.tasks_dir}", EXIT_USAGE)
    claude = resolve_binary(args.claude_bin, "claude", ["~/.local/bin/claude"])
    codex = resolve_binary(args.codex_bin, "codex", ["/opt/homebrew/bin/codex"])
    wf = resolve_binary(args.wf_bin, "wf", [str(ROOT / ".venv" / "bin" / "wf")])
    if claude is None:
        return _fail("claude not found; pass --claude-bin")
    if "team" in modes and (codex is None or wf is None):
        return _fail("codex or wf not found; pass --codex-bin and --wf-bin")

    out = Path(args.out)
    data: dict[str, Any] = {
        "meta": {
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "claude_model": args.claude_model,
            "claude_effort": args.claude_effort,
            "codex_model": args.codex_model,
            "codex_effort": args.codex_effort,
            "tasks": [t.name for t in tasks],
            "modes": modes,
            "repeat": args.repeat,
            "timeout_s": args.timeout,
            "claude_version": _version(claude),
            "codex_version": _version(codex) if codex else None,
        },
        "runs": [],
    }
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="wf-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        for task in tasks:
            for repeat in range(1, args.repeat + 1):
                order = modes if repeat % 2 else list(reversed(modes))
                for mode in order:
                    print(f"→ {task.name} · {mode} · run {repeat}", flush=True)
                    record = run_one(
                        task,
                        mode,
                        repeat,
                        workdir,
                        claude_bin=claude,
                        codex_bin=codex or "",
                        wf_bin=wf or "",
                        claude_model=args.claude_model,
                        claude_effort=args.claude_effort,
                        codex_model=args.codex_model,
                        codex_effort=args.codex_effort,
                        timeout_s=args.timeout,
                        environ=environ,
                    )
                    data["runs"].append(record)
                    save_results(out, data)
                    hidden = "pass" if (record.get("hidden") or {}).get("passed") else "FAIL"
                    print(f"  {fmt_seconds(record.get('seconds'))} · hidden {hidden} · {_status(record)}", flush=True)
    finally:
        if not args.workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    print(f"results written to {out}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    if args.command == "list":
        for task in discover_tasks():
            print(task.name)
        return EXIT_OK
    if args.command == "report":
        try:
            data = json.loads(Path(args.results).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _fail(f"cannot read {args.results}: {exc}", EXIT_USAGE)
        print(render_report(data))
        return EXIT_OK
    return cmd_run(args, environ)


if __name__ == "__main__":
    sys.exit(main())
