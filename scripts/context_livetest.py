"""Live context-retention test: does the team keep context over a long session, on the real CLIs?

Part A, Codex: one team Codex session (codex_plan, then codex_ask resumes) grows to tens of thousands of tokens
(it reads a 30-module project and is sent two large log dumps). The last turn asks, without opening files, for
facts planted at the start, in the middle of a log dump and inside the files.

Part B, Claude: the real Claude Code with the WorkForce plugin, one resumed session. Claude reads the whole project,
the user talks to Codex with `@codex`, then Claude must quote Codex's reply word for word and recall the early facts,
before and after `/compact`.

Everything runs in a throwaway repo; WorkForce state goes to a temp WF_HOME, so the real ~/.workforce is untouched.
Usage: .venv/bin/python scripts/context_livetest.py [--codex-model gpt-6-luna] [--codex-effort low] [--claude-model <id>] [--part a|b|both]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLUGIN = ROOT / "wf-plugin"
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")
MODULES = 30

FACTS_TOLD = {"codename": "BLUE-HERON-47", "deadline": "Friday 14:00 IST", "forbidden": "payments_v1"}
FACT_IN_LOG = ("rollback ticket", "OPS-8812")
FACT_IN_FILE = ("MAX_RETRIES in src/billing/retry.py", "17")
FACT_OWNER = ("owner named in src/mod_23.py", "team-kestrel")
CODEX_PHRASE = "amber-otter-lantern"

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, evidence: str) -> None:
    results.append((name, ok, evidence))
    print(f"{'✓' if ok else '✗'} {name}: {evidence}", flush=True)


def clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in API_KEY_VARS}
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def module_source(index: int, rng: random.Random) -> str:
    """About 120 lines of plausible Python, so reading the project costs real context."""
    lines = [f'"""Module {index}: {rng.choice(["ingest", "normalise", "score", "export", "audit"])} step {index}."""', ""]
    if index % 3 == 0:
        lines += ["import json", ""]
    if index == 23:
        lines[0] = f'"""Module 23: scoring rules. Owner: {FACT_OWNER[1]}."""'
    for fn in range(8):
        name = f"step_{index}_{fn}"
        lines += [
            f"def {name}(records, limit={rng.randint(5, 500)}):",
            f'    """Filter records for stage {fn} of module {index}."""',
            "    kept = []",
            "    for record in records:",
            f"        if record.get('weight', 0) > {rng.randint(1, 99)}:",
            f"            record['stage'] = '{name}'",
            "            kept.append(record)",
            "        if len(kept) >= limit:",
            "            break",
            f"    return kept[: {rng.randint(1, 50)}]",
            "",
            "",
        ]
    return "\n".join(lines) + "\n"


def make_repo(base: Path) -> Path:
    rng = random.Random(7)
    repo = base / "repo"
    (repo / "src" / "billing").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "user.name", "wf-contexttest")
    git(repo, "config", "user.email", "wf-contexttest@example.invalid")
    for index in range(MODULES):
        (repo / "src" / f"mod_{index:02d}.py").write_text(module_source(index, rng))
    (repo / "src" / "billing" / "retry.py").write_text(
        '"""Retry policy for billing calls."""\n\nMAX_RETRIES = 17\nBACKOFF_SECONDS = 2.5\n\n\ndef should_retry(attempt):\n    return attempt < MAX_RETRIES\n'
    )
    (repo / "README.md").write_text("# scoring pipeline\n\nThirty processing modules under src/.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def make_wf_home(base: Path, codex_model: str, codex_effort: str, claude_model: str) -> Path:
    home = base / "wfhome"
    (home / ".workforce").mkdir(parents=True)
    (home / ".workforce" / "team.toml").write_text(
        f'claude = "{Path.home()}/.local/bin/claude"\ncodex = "{shutil.which("codex") or "/opt/homebrew/bin/codex"}"\n'
        f'codex_model = "{codex_model}"\ncodex_effort = "{codex_effort}"\nreviewer_model = "{claude_model}"\nreviewer_effort = "low"\n'
    )
    return home


def log_dump(rng: random.Random, lines: int, buried: str | None = None) -> str:
    services = ["ingest", "scorer", "exporter", "auditor", "gateway"]
    out = []
    for n in range(lines):
        out.append(
            f"2026-09-30T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}Z "
            f"{rng.choice(['INFO', 'WARN', 'DEBUG'])} {rng.choice(services)} req={rng.randint(10**6, 10**7)} "
            f"latency_ms={rng.randint(3, 900)} rows={rng.randint(0, 5000)} status={rng.choice([200, 200, 200, 204, 409, 500])}"
        )
        if buried and n == lines // 2:
            out.append(f"2026-09-30T11:02:17Z NOTE ops the {FACT_IN_LOG[0]} for this incident is {FACT_IN_LOG[1]}")
    return "\n".join(out)


def recall_score(text: str, expected: dict[str, str]) -> dict[str, bool]:
    lowered = text.lower()
    return {name: value.lower() in lowered for name, value in expected.items()}


# ------------------------------------------------------------------ part A: Codex keeps context in one long session


def part_a(repo: Path, wf_home: Path) -> None:
    os.environ.update({"WF_HOME": str(wf_home), "CLAUDE_PROJECT_DIR": str(repo)})
    from workforce.team import mcp_server

    usages: list[dict] = []

    class Recording(mcp_server.Server):
        def _run_codex(self, *a, **k):
            result = super()._run_codex(*a, **k)
            usages.append(result.usage or {})
            return result

    server = Recording()

    def call(name: str, args: dict) -> str:
        started = time.monotonic()
        out = server.call(name, args)
        text = out["content"][0]["text"]
        if out.get("isError"):
            raise RuntimeError(f"{name} failed: {text[:400]}")
        print(f"   · {name} ok in {time.monotonic() - started:.0f}s, tokens processed this turn {usages[-1].get('input_tokens', '?')}", flush=True)
        return text

    rng = random.Random(11)
    context = (
        f"Facts to keep for the whole conversation: the release codename is {FACTS_TOLD['codename']}; "
        f"the deadline is {FACTS_TOLD['deadline']}; never touch the module called {FACTS_TOLD['forbidden']}."
    )
    plan = call("codex_plan", {"task": "Read every module under src/ (all of them) and plan how to add structured logging to each step function.", "context": context})
    session = plan.rsplit("[codex session_id: ", 1)[1].split("]", 1)[0]
    call("codex_ask", {"session_id": session, "message": "Here is a log dump from staging. Which service shows the most 500s?\n\n" + log_dump(rng, 700, buried="yes")})
    call("codex_ask", {"session_id": session, "message": "Read src/billing/retry.py, src/mod_23.py and every module that imports json. List those modules, say who owns src/mod_23.py, and say how retries would interact with the logging plan."})
    call("codex_ask", {"session_id": session, "message": "Another dump, from production. Summarise the latency distribution in two lines.\n\n" + log_dump(rng, 700)})
    final = call(
        "codex_ask",
        {
            "session_id": session,
            "message": (
                "Memory check. Do NOT open any file or run any command: answer only from this conversation. "
                "1) the release codename, 2) the deadline, 3) the module we must never touch, "
                f"4) the {FACT_IN_LOG[0]} buried in the first log dump, 5) the value of {FACT_IN_FILE[0]}, "
                f"6) the {FACT_OWNER[0]}. One line each."
            ),
        },
    )
    expected = {**FACTS_TOLD, "log fact": FACT_IN_LOG[1], "file fact": FACT_IN_FILE[1], "owner": FACT_OWNER[1]}
    score = recall_score(final, expected)
    peak = max((u.get("input_tokens") or 0) for u in usages) if usages else 0
    same_session = all(session in line for line in [final])
    report("A1 Codex answered in the same session across 5 turns", same_session, f"session {session}")
    report(
        "A2 Codex recalled every planted fact without re-reading",
        all(score.values()),
        f"{sum(score.values())}/{len(score)} recalled {score}; most tokens processed in one turn ≈ {peak}",
    )
    print("   Codex's final answer:\n" + "\n".join("     " + l for l in final.splitlines()[:12]), flush=True)


# ------------------------------------------------------------------ part B: Claude Code keeps context and gets Codex's words verbatim


def run_claude(repo: Path, wf_home: Path, model: str, prompt: str, resume: str | None = None, timeout: int = 900) -> dict:
    cmd = [
        str(Path.home() / ".local" / "bin" / "claude"), "-p", prompt, "--plugin-dir", str(PLUGIN), "--model", model,
        "--output-format", "json", "--allowedTools", "Read", "Glob", "Grep",
        "--append-system-prompt", (PLUGIN / "TEAM.md").read_text(),
    ]
    if resume:
        cmd += ["--resume", resume]
    started = time.monotonic()
    done = subprocess.run(cmd, cwd=repo, env=clean_env({"WF_HOME": str(wf_home)}), capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        data = {"result": done.stdout + done.stderr, "session_id": None}
    usage = data.get("usage") or {}
    context = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
    print(f"   · claude turn ok in {time.monotonic() - started:.0f}s, tokens processed ≈ {context}", flush=True)
    return data


def part_b(repo: Path, wf_home: Path, model: str) -> None:
    first = run_claude(
        repo, wf_home, model,
        f"Read every file under src/ (all {MODULES + 1} of them, including src/billing/retry.py) and summarise the project in one paragraph. "
        f"Also remember for later: my release codename is {FACTS_TOLD['codename']}. Do not call any Codex tool.",
    )
    session = first.get("session_id")
    report("B1 Claude read the whole project", bool(session), f"session {session}")
    if not session:
        return
    run_claude(repo, wf_home, model, f"@codex Remember this exact phrase: {CODEX_PHRASE}. Reply with exactly: stored {CODEX_PHRASE}", resume=session)
    thread = repo / ".workforce" / "team" / "codex-thread.md"
    thread_text = thread.read_text() if thread.exists() else ""
    report("B2 @codex went straight to Codex and was logged verbatim", CODEX_PHRASE in thread_text and "stored" in thread_text.lower(), f"thread file {'has' if thread_text else 'missing'} the exchange")

    third = run_claude(
        repo, wf_home, model,
        "Without opening any file: 1) quote word for word what Codex replied to my last @codex message, "
        f"2) my release codename, 3) the value of {FACT_IN_FILE[0]}, 4) the {FACT_OWNER[0]}.",
        resume=session,
    )
    text = str(third.get("result"))
    score = recall_score(text, {"codex reply": f"stored {CODEX_PHRASE}", "codename": FACTS_TOLD["codename"], "file fact": FACT_IN_FILE[1], "owner": FACT_OWNER[1]})
    report("B3 Claude quoted Codex verbatim and recalled the early facts", all(score.values()), f"{sum(score.values())}/{len(score)} {score}")

    compacted = run_claude(repo, wf_home, model, "/compact", resume=session)
    report("B4 /compact ran on the long session", "error" not in str(compacted.get("subtype", "")).lower(), str(compacted.get("result"))[:120].replace("\n", " "))
    after = run_claude(
        repo, wf_home, model,
        "After the compaction: 1) my release codename, 2) the exact phrase I asked Codex to remember, "
        f"3) the value of {FACT_IN_FILE[0]}. If you are not sure, read .workforce/team/codex-thread.md or the source file; do not guess.",
        resume=session,
    )
    text = str(after.get("result"))
    score = recall_score(text, {"codename": FACTS_TOLD["codename"], "codex phrase": CODEX_PHRASE, "file fact": FACT_IN_FILE[1]})
    report("B5 after /compact Claude still had the facts (or recovered them from the saved files)", all(score.values()), f"{sum(score.values())}/{len(score)} {score}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-model", default="gpt-6-luna")
    parser.add_argument("--codex-effort", default="low")
    parser.add_argument("--claude-model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--part", choices=("a", "b", "both"), default="both")
    parser.add_argument("--keep", action="store_true", help="keep the temp folder for inspection")
    opts = parser.parse_args()
    if any(os.environ.get(name) for name in API_KEY_VARS):
        print("an API key variable is set; this test uses the subscriptions only", file=sys.stderr)
        return 3
    base = Path(tempfile.mkdtemp(prefix="wf-contexttest-"))
    print(f"temp folder: {base}\nCodex {opts.codex_model}·{opts.codex_effort} │ Claude {opts.claude_model}\n", flush=True)
    try:
        repo = make_repo(base)
        wf_home = make_wf_home(base, opts.codex_model, opts.codex_effort, opts.claude_model)
        for part, fn, args in (("a", part_a, (repo, wf_home)), ("b", part_b, (repo, wf_home, opts.claude_model))):
            if opts.part in (part, "both"):
                print(f"Part {part.upper()}", flush=True)
                try:
                    fn(*args)
                except Exception as exc:
                    report(f"part {part.upper()} crashed", False, f"{type(exc).__name__}: {exc}")
    finally:
        if not opts.keep:
            shutil.rmtree(base, ignore_errors=True)
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed} passed, {len(results) - passed} failed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
