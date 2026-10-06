"""Verbatim relay between the user, Claude and Codex (SPEC-TEAM-CLI section 5).

Nothing here redacts, trims or rewrites model text shown to the user or to the other model. The only exception is a reply
over `MAX_REPLY_CHARS`, which is cut with a clearly marked notice (the full text stays in the saved artefact file).
Everything lives under `<project>/.workforce/team/`:

    session.json        the last team Codex session (`wf codex` resumes it)
    codex-thread.md     direct `@codex` exchanges, verbatim, with timestamps
    codex-thread.seen   byte offset of the thread already given to Claude
    {plan,review,ask,claude-review}-<n>.md   raw reviewer/Codex output plus the exact prompt Claude sent
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from workforce.agents.base import AgentRequest, AgentResult
from workforce.agents.codex import CodexRunner
from workforce.errors import WorkforceError
from workforce.team import config

TEAM_SUBDIR = Path(".workforce") / "team"
THREAD_FILE = "codex-thread.md"
SEEN_FILE = "codex-thread.seen"
SESSION_FILE = "session.json"
LOCK_FILE = ".relay.lock"
ARTIFACT_KINDS = ("plan", "review", "ask", "claude-review", "plan-review")
MAX_REPLY_CHARS = 200_000
DIRECT_TIMEOUT_S = 570
CODEX_TIMEOUT_S = 1500
THREAD_HEADING = "Direct user↔Codex exchange (verbatim, not summarised):"
_PREFIX = re.compile(r"@codex(?![\w-])", re.I)


class CodexFailed(WorkforceError):
    """A Codex run did not complete; the message is safe to show as is."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def team_dir(project: Path | str) -> Path:
    return Path(project) / TEAM_SUBDIR


def in_git_repo(cwd: Path) -> bool:
    proc = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _hide_from_git(project: Path) -> None:
    """Add `.workforce/` to the repo's local `info/exclude`, so saved files never change the tree hash the reviews are tied to."""
    proc = subprocess.run(["git", "-C", str(project), "rev-parse", "--git-path", "info/exclude"], capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return
    path = Path(proc.stdout.strip())
    path = path if path.is_absolute() else Path(project) / path
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if any(line.strip() in (".workforce", ".workforce/") for line in text.splitlines()):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(("" if text.endswith("\n") or not text else "\n") + ".workforce/\n")
    except OSError:
        pass


def ensure_team_dir(project: Path | str) -> Path:
    directory = team_dir(project)
    fresh = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    if fresh:
        _hide_from_git(Path(project))
    return directory


def truncate_reply(text: str, saved_to: str | None = None) -> str:
    """The text unchanged, unless it is over the cap: then cut, with a notice saying how much and where the rest is."""
    if len(text) <= MAX_REPLY_CHARS:
        return text
    where = f"; the full text is in {saved_to}" if saved_to else ""
    return text[:MAX_REPLY_CHARS] + f"\n\n[WorkForce truncation notice: {len(text) - MAX_REPLY_CHARS} more characters omitted{where}]"


def read_session(project: Path | str) -> dict[str, Any]:
    try:
        data = json.loads((team_dir(project) / SESSION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def session_id(project: Path | str) -> str | None:
    value = read_session(project).get("session_id")
    return value if isinstance(value, str) and value else None


def write_session(project: Path | str, session: str | None, model: str, effort: str) -> None:
    if not session:
        return
    directory = ensure_team_dir(project)
    entry = {"session_id": session, "model": model, "effort": effort, "updated_at": now_iso()}
    config.atomic_write(directory / SESSION_FILE, json.dumps(entry, indent=2) + "\n")


def find_session_dir(start: Path | str) -> Path | None:
    """The nearest folder at or above `start` that holds a team session file."""
    here = Path(start).resolve()
    for folder in (here, *here.parents):
        if (team_dir(folder) / SESSION_FILE).is_file():
            return folder
    return None


def sandbox_for(cfg: config.TeamConfig, kind: str) -> str:
    """Only `ask` (and the direct `@codex` line) may write, and only in write mode; plan and review never do."""
    return "workspace-write" if kind == "ask" and cfg.codex_mode == "write" else "read-only"


def browser_note(cfg: config.TeamConfig, project: Path | str, role: str) -> str:
    """The Ego Lite note for a Codex prompt: only with `browser = "ego"`, only for roles that may browse, only when it is installed."""
    from workforce.team import browser

    if getattr(cfg, "browser", "off") != "ego" or role not in browser.CODEX_ROLES:
        return ""
    try:
        state = browser.status(cfg.browser)
    except OSError:
        return ""
    return browser.codex_text(project, state.cli, role) if state.codex_ready else ""


def run_codex(
    cfg: config.TeamConfig,
    project: Path,
    role: str,
    prompt: str,
    model: str,
    effort: str,
    sandbox: str = "read-only",
    timeout_s: int = CODEX_TIMEOUT_S,
    **extra: Any,
) -> AgentResult:
    request = AgentRequest(
        role=role,
        agent="codex",
        model=model,
        effort=effort,
        prompt=prompt,
        cwd=project,
        sandbox=sandbox,
        no_mcp=sandbox == "read-only",
        skip_git_check=not in_git_repo(project),
        timeout_s=timeout_s,
        **extra,
    )
    result = CodexRunner(Path(cfg.codex)).run(request)
    if not result.ok:
        raise CodexFailed(f"codex {role} failed ({result.error_kind}): {result.error}")
    return result


def save_artifact(
    project: Path | str,
    kind: str,
    *,
    sent: Mapping[str, Any],
    prompt: str,
    reply: str,
    model: str,
    effort: str,
    sandbox: str,
    session: str | None,
) -> Path:
    """Write `<kind>-<n>.md`: what Claude sent (each argument raw), the full prompt given to Codex, and Codex's raw output."""
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"unknown artefact kind {kind!r}")
    directory = ensure_team_dir(project)
    pattern = re.compile(rf"{kind}-(\d+)\.md")
    number = 1 + max((int(m.group(1)) for p in directory.iterdir() if (m := pattern.fullmatch(p.name))), default=0)
    parts = [
        f"# {kind} {number} · {now_iso()}",
        f"model: {model} · effort: {effort} · sandbox: {sandbox} · codex session: {session or 'unknown'}",
        "",
    ]
    for name, value in sent.items():
        parts += [f"===== SENT BY CLAUDE: {name} (exact) =====", "" if value is None else str(value), ""]
    parts += ["===== FULL PROMPT GIVEN TO CODEX (exact) =====", prompt, "", "===== CODEX OUTPUT (verbatim) =====", reply, ""]
    body = "\n".join(parts)
    while True:
        path = directory / f"{kind}-{number}.md"
        try:
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(body)
            return path
        except FileExistsError:
            number += 1
            body = body.replace(f"# {kind} {number - 1} ·", f"# {kind} {number} ·", 1)


PLAN_RECORDS = "plan-reviews.json"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _plan_records(project: Path | str) -> dict:
    try:
        data = json.loads((team_dir(project) / PLAN_RECORDS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def plan_record(project: Path | str, plan: str) -> dict:
    """The last Codex plan review of `plan` (path relative to the project): sha256, verdict, rounds, model, time; {} if none."""
    entry = _plan_records(project).get(plan)
    return entry if isinstance(entry, dict) else {}


def record_plan(project: Path | str, plan: str, sha: str, verdict: str, round_no: int, model: str, effort: str) -> None:
    records = _plan_records(project)
    records[plan] = {"sha256": sha, "verdict": verdict, "rounds": round_no, "model": model, "effort": effort, "at": now_iso()}
    target = ensure_team_dir(project) / PLAN_RECORDS
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _fenced(text: str) -> str:
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def append_plan_log(
    plan: Path, round_no: int, model: str, effort: str, sha: str, feedback: str | None, reply: str, verdict: str, note: str | None
) -> Path:
    """Append one review round, verbatim, to `REVIEW-LOG.md` next to the plan; returns the log path."""
    log = Path(plan).parent / "REVIEW-LOG.md"
    parts = []
    if not log.exists():
        parts.append(f"# Plan review log: {Path(plan).name}\n")
    parts += [
        f"## Round {round_no} · {now_iso()} · Codex {model} · {effort}",
        f"plan sha256: {sha} · recorded verdict: {verdict}" + (f" ({note})" if note else ""),
        "",
        "### Claude's dispositions sent to Codex (verbatim)",
        _fenced(feedback or "(none)"),
        "",
        "### Codex's reply (verbatim)",
        _fenced(reply),
        "",
    ]
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("\n".join(parts) + "\n")
    return log


def artifact_reply(path: Path) -> str:
    """The verbatim Codex output section of a saved artefact (for tests and tools that re-read it)."""
    text = path.read_text(encoding="utf-8")
    marker = "===== CODEX OUTPUT (verbatim) =====\n"
    return text.split(marker, 1)[1].removesuffix("\n")


def parse_direct(prompt: Any) -> str | None:
    """The exact text after a leading `@codex` / `@codex:` (case-insensitive), or None if the prompt is not a direct line."""
    if not isinstance(prompt, str):
        return None
    match = _PREFIX.match(prompt.lstrip())
    if not match:
        return None
    rest = prompt.lstrip()[match.end():]
    if rest.startswith(":"):
        rest = rest[1:]
    return rest.lstrip(" \t\r\n")


def _lock(directory: Path):
    handle = open(directory / LOCK_FILE, "a+")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def append_thread(project: Path | str, who: str, text: str) -> None:
    directory = ensure_team_dir(project)
    entry = f"### {now_iso()} · {who}\n\n{text}\n\n"
    lock = _lock(directory)
    try:
        with open(directory / THREAD_FILE, "a", encoding="utf-8") as handle:
            handle.write(entry)
    finally:
        lock.close()


def take_unseen(project: Path | str) -> str | None:
    """Every thread entry Claude has not been given yet, verbatim under a heading; each byte is handed out exactly once."""
    directory = team_dir(project)
    thread = directory / THREAD_FILE
    if not thread.is_file():
        return None
    lock = _lock(directory)
    try:
        data = thread.read_bytes()
        try:
            offset = int((directory / SEEN_FILE).read_text().strip())
        except (OSError, ValueError):
            offset = 0
        if not 0 <= offset <= len(data):
            offset = 0
        chunk = data[offset:]
        if not chunk.strip():
            return None
        config.atomic_write(directory / SEEN_FILE, str(len(data)))
    finally:
        lock.close()
    return f"{THREAD_HEADING}\n\n{chunk.decode('utf-8', errors='replace')}"


def direct_codex(cfg: config.TeamConfig, project: Path | str, text: str) -> str:
    """Send `text` (unwrapped) to the team Codex session; returns the hook's block reason: `Codex (<model>):` and the reply."""
    project = Path(project)
    header = f"Codex ({cfg.codex_model}):"
    if not text.strip():
        return f"{header}\nNothing to send. Type your message after @codex."
    append_thread(project, "user → codex", text)
    resume = session_id(project)
    sandbox = sandbox_for(cfg, "ask")
    try:
        result = run_codex(
            cfg, project, "team_direct", text + browser_note(cfg, project, "team_direct"), cfg.codex_model, cfg.codex_effort,
            sandbox=sandbox, timeout_s=DIRECT_TIMEOUT_S, resume_session=resume,
        )
    except WorkforceError as exc:
        note = f"[Codex did not reply: {exc}]" + (
            f"\n(It tried to resume session {resume}; delete {team_dir(project) / SESSION_FILE} to start a new one.)" if resume else ""
        )
        append_thread(project, f"system ({cfg.codex_model})", note)
        return f"{header}\n{note}"
    write_session(project, result.session_id, cfg.codex_model, cfg.codex_effort)
    saved = None
    try:
        saved = save_artifact(
            project, "ask", sent={"message (direct @codex line)": text}, prompt=text, reply=result.text,
            model=cfg.codex_model, effort=cfg.codex_effort, sandbox=sandbox, session=result.session_id,
        )
    except OSError:
        pass
    shown = truncate_reply(result.text, str(saved) if saved else None)
    append_thread(project, f"codex ({cfg.codex_model}) → user", shown)
    return f"{header}\n{shown}"
