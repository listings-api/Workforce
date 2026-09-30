"""MCP stdio server "codex": Codex plans, reviews and answers read-only inside a Claude Code session.

Hand-written JSON-RPC 2.0, one message per line. Run with `python -m workforce.team.mcp_server`.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from workforce import git_ops, prompts, schemas
from workforce.agents import base as agent_base
from workforce.agents import guard
from workforce.config import EFFORTS
from workforce.decider.base import is_confident
from workforce.decider.laya import LayaDecider
from workforce.errors import WorkforceError
from workforce.team import approvals, claude_review, codex_models, config, picker, relay, usage_cache
from workforce.usage import codex_limits

SERVER_NAME = "workforce-codex"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-06-18"
LAYA_URL = "http://127.0.0.1:11435"
LAYA_CONFIDENCE = 0.85
LAYA_KEY = "task_size"
ASK_TIMEOUT_S = 600
REVIEW_TIMEOUT_S = 1200
DIFF_LIMIT = 60_000
CODEX_CACHE_TTL_S = 120
LOG_FIELD_CHARS = 500
LOG_NAME = "team.log"
WORKERS = 4


class ToolError(Exception):
    """A tool failed in a way worth showing to the model; becomes an `isError` result."""


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_MODEL = {"type": "string", "description": "Codex model; defaults to the configured one"}
_EFFORT = {
    "type": "string",
    "enum": list(EFFORTS["codex"]),
    "description": "Codex reasoning effort; defaults to the configured one",
}
_REPO = {
    "type": "string",
    "description": "The git repo to work in: a path, relative to the project folder or absolute. Pass it when the repo is a subfolder of the project folder (a folder holding several repos); defaults to the project folder.",
}

TOOLS: list[dict] = [
    {
        "name": "codex_plan",
        "description": "Ask Codex (read-only) to research the repo and return a numbered plan plus risks. Returns the plan and a session_id for codex_ask follow-ups.",
        "inputSchema": _obj(
            {"task": _STR, "context": _STR, "model": _MODEL, "effort": _EFFORT, "repo": _REPO},
            ["task"],
        ),
    },
    {
        "name": "codex_ask",
        "description": "Ask Codex (read-only) a question or debate a point. Pass session_id from an earlier Codex call to continue that conversation.",
        "inputSchema": _obj({"message": _STR, "session_id": _STR}, ["message"]),
    },
    {
        "name": "codex_review",
        "description": "Codex independently reviews the current changes (working state versus base, including untracked files) and returns a verdict JSON. The server records the verdict itself for the current tree hash.",
        "inputSchema": _obj({"focus": _STR, "base": {"type": "string", "description": "Git ref to compare against; default HEAD. Only a review against HEAD can allow a commit"}, "repo": _REPO}),
    },
    {
        "name": "claude_review",
        "description": "A fresh, read-only Claude reviewer (its own headless session, the configured reviewer model) independently reviews the current changes (working state versus base, including untracked files) and returns a verdict JSON. The server runs the reviewer and records the verdict itself for the current tree hash; nobody can record a verdict by hand.",
        "inputSchema": _obj({"focus": _STR, "base": {"type": "string", "description": "Git ref to compare against; default HEAD. Only a review against HEAD can allow a commit"}, "repo": _REPO}),
    },
    {
        "name": "codex_plan_review",
        "description": "Only for /plan-loop. Codex (read-only) independently reviews a plan file and returns APPROVED, REVISE or BLOCKED with evidence-backed findings. The approval is bound to the plan file's exact sha256; any edit to the plan needs another review. Each round is appended verbatim to REVIEW-LOG.md next to the plan. Pass session_id from the previous round to keep the same reviewer, and feedback with your disposition of each finding.",
        "inputSchema": _obj(
            {
                "plan": {"type": "string", "description": "Path of the plan file, inside the project folder"},
                "feedback": {"type": "string", "description": "Your disposition of each earlier finding (accepted and how, or rejected and why); omit in round 1"},
                "session_id": _STR,
                "model": _MODEL,
                "effort": _EFFORT,
                "repo": _REPO,
            },
            ["plan"],
        ),
    },
    {
        "name": "plan_status",
        "description": "Only for /plan-loop. Whether the plan file as it is now has a Codex APPROVED review (bound to its sha256), and how many review rounds it has had.",
        "inputSchema": _obj({"plan": {"type": "string", "description": "Path of the plan file, inside the project folder"}}, ["plan"]),
    },
    {
        "name": "review_status",
        "description": "Both reviewers' verdicts for the current tree, whether a commit is allowed, and what is missing.",
        "inputSchema": _obj({"repo": _REPO}),
    },
    {
        "name": "usage",
        "description": "Claude 5h/weekly and Codex usage windows with the alert/stop state.",
        "inputSchema": _obj({}),
    },
    {
        "name": "laya",
        "description": "Ask the local Laya model a quick yes/no or pick-one question. A hint only: never for approvals or model choice.",
        "inputSchema": _obj(
            {
                "question": _STR,
                "options": {"type": "array", "items": _STR, "minItems": 2, "description": "Omit for a yes/no question"},
                "context": _STR,
            },
            ["question"],
        ),
    },
    {
        "name": "codex_settings",
        "description": "Get or set the default Codex model, effort and mode (saved in ~/.workforce/team.toml). Call with no arguments to read them. mode 'write' lets codex_ask edit files in the project (workspace-write); plan and review stay read-only.",
        "inputSchema": _obj({"model": _STR, "effort": _EFFORT, "mode": {"type": "string", "enum": list(config.CODEX_MODES)}}),
    },
    {
        "name": "team_models",
        "description": "Get or set the models and efforts of the Claude reviewer (used by claude_review, applies immediately) and the fast-coder sub-agent (saved in ~/.workforce/team.toml; the agent file is regenerated the next time `wf` starts). Call with no arguments to read them.",
        "inputSchema": _obj(
            {
                "reviewer_model": _STR,
                "reviewer_effort": {"type": "string", "enum": list(EFFORTS["claude"])},
                "fast_coder_model": _STR,
                "fast_coder_effort": {"type": "string", "enum": list(EFFORTS["claude"])},
            }
        ),
    },
    {
        "name": "picker",
        "description": "Menus for /codex-model, /codex-effort, /claude-model and /claude-effort. Returns `questions` to pass unchanged to AskUserQuestion (one call), and `then`: which settings tool to call with the answers. The Codex models come live from Codex.",
        "inputSchema": _obj({"kind": {"type": "string", "enum": ["codex_model", "codex_effort", "claude_model", "claude_effort"]}}, ["kind"]),
    },
    {
        "name": "settings",
        "description": "One text view of every WorkForce setting (Codex model/effort/mode, sub-agent models, alert/stop percent), the current usage and the Laya status, with the command that changes each. Show the text to the user exactly as returned.",
        "inputSchema": _obj({}),
    },
]


class _NoEvents:
    def emit(self, kind: str, **data: Any) -> None:
        return None


def project_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def _text(text: str, is_error: bool = False) -> dict:
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _json_text(value: Any) -> dict:
    return _text(json.dumps(value, indent=2))


def _string_arg(args: dict, name: str, required: bool = False) -> str | None:
    value = args.get(name)
    if value is None or value == "":
        if required:
            raise ToolError(f"'{name}' is required")
        return None
    if not isinstance(value, str):
        raise ToolError(f"'{name}' must be a string")
    return value


def _codex_settings(cfg: config.TeamConfig, args: dict) -> tuple[str, str]:
    model = _string_arg(args, "model") or cfg.codex_model
    effort = _string_arg(args, "effort") or cfg.codex_effort
    if effort not in EFFORTS["codex"]:
        raise ToolError(f"effort '{effort}' is not valid for codex; expected one of {list(EFFORTS['codex'])}")
    return model, effort


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters of diff omitted; read the files directly ...]"


def _consistent(verdict: str, summary: str, findings: list) -> tuple[str, str]:
    """An APPROVE that lists a blocker or major finding cannot stand; it is recorded as REQUEST_CHANGES."""
    serious = [f for f in findings if isinstance(f, dict) and f.get("severity") in ("blocker", "major")]
    if verdict == "APPROVE" and serious:
        return "REQUEST_CHANGES", f"{summary}\n[recorded as REQUEST_CHANGES: an APPROVE listed {len(serious)} blocker/major finding(s)]"
    return verdict, summary


class Server:
    """Holds the home and cwd sources; `handle` maps one JSON-RPC message to its response (or None)."""

    def __init__(self, home: Path | None = None):
        self.home = home
        self.log_lock = threading.Lock()

    def cwd(self) -> Path:
        return project_dir()

    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            request_id = message.get("id") if isinstance(message, dict) else None
            return self._error(request_id, -32600, "invalid request")
        method = message["method"]
        request_id = message.get("id")
        is_request = "id" in message
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            version = params.get("protocolVersion")
            return self._result(
                request_id,
                {
                    "protocolVersion": version if isinstance(version, str) else DEFAULT_PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        if method == "ping":
            return self._result(request_id, {}) if is_request else None
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            if name not in {tool["name"] for tool in TOOLS}:
                return self._error(request_id, -32602, f"unknown tool: {name!r}")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return self._result(request_id, _text("arguments must be an object", True)) if is_request else None
            result = self.call(name, arguments)
            return self._result(request_id, result) if is_request else None
        if not is_request:
            return None
        return self._error(request_id, -32601, f"method not found: {method}")

    @staticmethod
    def _result(request_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def call(self, name: str, args: dict) -> dict:
        """Run one tool; every failure becomes an `isError` result, and the call is logged either way."""
        started = time.monotonic()
        try:
            result = getattr(self, f"tool_{name}")(args)
        except ToolError as exc:
            result = _text(str(exc), True)
        except WorkforceError as exc:
            result = _text(f"{name} failed: {exc}", True)
        except Exception as exc:
            result = _text(f"{name} failed unexpectedly: {type(exc).__name__}: {exc}", True)
        self._log(name, args, result, time.monotonic() - started)
        return result

    def _log(self, name: str, args: dict, result: dict, seconds: float) -> None:
        def clip(value: Any) -> Any:
            if isinstance(value, str):
                return value if len(value) <= LOG_FIELD_CHARS else value[:LOG_FIELD_CHARS] + "…"
            if isinstance(value, dict):
                return {k: clip(v) for k, v in value.items()}
            if isinstance(value, list):
                return [clip(v) for v in value]
            return value

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": name,
            "args": guard.redacted(clip(args)),
            "ok": not result.get("isError", False),
            "seconds": round(seconds, 2),
            "result": guard.redacted(clip(result["content"][0]["text"])),
        }
        try:
            path = config.workforce_dir(self.home) / LOG_NAME
            path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_lock:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def _cfg(self) -> config.TeamConfig:
        return config.load(self.home)

    def _repo(self, args: dict) -> Path:
        """The folder a tool works in: `repo` (relative to the project folder, or absolute), else the project folder."""
        raw = _string_arg(args, "repo")
        if raw is None:
            return self.cwd()
        path = Path(raw).expanduser()
        path = (path if path.is_absolute() else self.cwd() / path).resolve()
        if not path.is_dir():
            raise ToolError(f"repo '{raw}' is not a folder (looked for {path})")
        return path

    def _run_codex(
        self, cfg: config.TeamConfig, role: str, prompt: str, model: str, effort: str,
        cwd: Path | None = None, timeout_s: int = ASK_TIMEOUT_S, **extra: Any,
    ):
        try:
            return relay.run_codex(
                cfg, cwd or self.cwd(), role, prompt, model, effort,
                sandbox=relay.sandbox_for(cfg, role.removeprefix("team_")), timeout_s=timeout_s, **extra,
            )
        except relay.CodexFailed as exc:
            raise ToolError(str(exc)) from exc

    def _save(self, kind: str, sent: dict, prompt: str, result, cfg: config.TeamConfig, model: str, effort: str) -> tuple[str | None, str]:
        """Save the raw Codex output with the prompt to `.workforce/team/<kind>-<n>.md`; returns (path, note for the tool result)."""
        try:
            path = relay.save_artifact(
                self.cwd(), kind, sent=sent, prompt=prompt, reply=result.text, model=model, effort=effort,
                sandbox=relay.sandbox_for(cfg, kind), session=result.session_id,
            )
        except OSError as exc:
            return None, f"[could not save the raw Codex output: {exc}]"
        return str(path), f"[raw Codex output and the exact prompt saved: {path}]"

    def _remember_session(self, result, model: str, effort: str) -> None:
        try:
            relay.write_session(self.cwd(), result.session_id, model, effort)
        except OSError:
            pass

    def tool_codex_plan(self, args: dict) -> dict:
        task = _string_arg(args, "task", required=True)
        cfg = self._cfg()
        model, effort = _codex_settings(cfg, args)
        context = _string_arg(args, "context")
        prompt = prompts.render("team_codex_plan", task=task, context=context or "(none)")
        result = self._run_codex(cfg, "team_plan", prompt, model, effort, cwd=self._repo(args))
        self._remember_session(result, model, effort)
        saved, note = self._save("plan", {"task": task, "context": context}, prompt, result, cfg, model, effort)
        return _text(f"{relay.truncate_reply(result.text, saved)}\n\n[codex session_id: {result.session_id}]\n{note}")

    def tool_codex_ask(self, args: dict) -> dict:
        message = _string_arg(args, "message", required=True)
        session_id = _string_arg(args, "session_id")
        cfg = self._cfg()
        model, effort = _codex_settings(cfg, args)
        prompt = prompts.render("team_codex_ask", message=message)
        result = self._run_codex(cfg, "team_ask", prompt, model, effort, resume_session=session_id)
        self._remember_session(result, model, effort)
        saved, note = self._save("ask", {"message": message, "session_id": session_id}, prompt, result, cfg, model, effort)
        return _text(f"{relay.truncate_reply(result.text, saved)}\n\n[codex session_id: {result.session_id}]\n{note}")

    def _review_material(self, cwd: Path, args: dict) -> tuple[str, dict[str, str], str, bool, str]:
        """(tree, prompt fields, base, diff truncated, base commit) for the changes in `cwd`; raises `ToolError` when there is nothing to review."""
        base = _string_arg(args, "base") or "HEAD"
        base_sha = git_ops.resolve_ref(cwd, base)
        tree = approvals.current_tree(cwd)
        diff = git_ops.diff_against(cwd, base_sha)
        if not diff.strip():
            raise ToolError(f"nothing to review: the working state has no changes versus {base}")
        fields = {
            "focus": _string_arg(args, "focus") or "General review of correctness, tests and callers.",
            "tree": tree,
            "base": f"{base} ({base_sha})",
            "files": "\n".join(git_ops.changed_files(cwd, base_sha)),
            "stat": git_ops.diff_stat(cwd, base_sha).strip(),
            "diff": _trim(diff, DIFF_LIMIT),
        }
        return tree, fields, base, len(diff) > DIFF_LIMIT, base_sha

    def _finish_review(self, cwd: Path, reviewer: str, verdict: dict, tree: str, truncated: bool, note: str, base_sha: str) -> dict:
        """Record the server-run reviewer's verdict for `tree` against `base_sha` and report it with the resulting commit status."""
        recorded_verdict, summary = _consistent(verdict["verdict"], verdict["summary"], verdict["findings"])
        approvals.record(cwd, reviewer, recorded_verdict, summary, verdict["findings"], tree=tree, home=self.home, base=base_sha)
        status = approvals.status(cwd, self.home)
        stale = status["tree"] != tree
        off_head = status.get("base") != base_sha
        return _json_text(
            {
                **verdict,
                "verdict": recorded_verdict,
                "summary": summary,
                "tree": tree,
                "commit_allowed": status["commit_allowed"],
                "missing": status["missing"],
                "raw_output": note,
                **({"diff_truncated": "the diff in the prompt was cut; the reviewer was told to read the files directly"} if truncated else {}),
                "base": base_sha,
                **({"note": "the working tree changed during the review; this verdict no longer applies to it"} if stale else {}),
                **(
                    {"base_note": "this review compared against a commit other than HEAD, so it cannot allow a commit on HEAD; review again without `base`"}
                    if off_head
                    else {}
                ),
            }
        )

    def tool_codex_review(self, args: dict) -> dict:
        cwd = self._repo(args)
        cfg = self._cfg()
        model, effort = _codex_settings(cfg, args)
        tree, fields, base, truncated, base_sha = self._review_material(cwd, args)
        prompt = prompts.render("team_codex_review", **fields)
        result = self._run_codex(cfg, "team_review", prompt, model, effort, cwd=cwd, timeout_s=REVIEW_TIMEOUT_S, schema=schemas.VERDICT)
        _, note = self._save(
            "review", {"focus": _string_arg(args, "focus"), "base": base}, prompt, result, cfg, model, effort
        )
        return self._finish_review(cwd, "codex", result.structured, tree, truncated, note, base_sha)

    def tool_claude_review(self, args: dict) -> dict:
        cwd = self._repo(args)
        cfg = self._cfg()
        tree, fields, base, truncated, base_sha = self._review_material(cwd, args)
        prompt = claude_review.build_prompt(**fields)
        try:
            result = claude_review.run(cfg, cwd, prompt, REVIEW_TIMEOUT_S)
        except WorkforceError as exc:
            raise ToolError(str(exc)) from exc
        note = "[could not save the raw reviewer output]"
        try:
            path = relay.save_artifact(
                self.cwd(), "claude-review", sent={"focus": _string_arg(args, "focus"), "base": base}, prompt=prompt,
                reply=result.text or json.dumps(result.structured, indent=2), model=cfg.reviewer_model,
                effort=cfg.reviewer_effort, sandbox="read-only", session=result.session_id,
            )
            note = f"[raw reviewer output and the exact prompt saved: {path}]"
        except OSError as exc:
            note = f"[could not save the raw reviewer output: {exc}]"
        return self._finish_review(cwd, "claude", result.structured, tree, truncated, note, base_sha)

    def _plan_file(self, args: dict) -> tuple[Path, str, str]:
        """(absolute plan path, path relative to the project, sha256) of the `plan` argument; raises `ToolError` when unusable."""
        raw = _string_arg(args, "plan", required=True)
        project = self.cwd().resolve()
        path = Path(raw).expanduser()
        path = (path if path.is_absolute() else project / path).resolve()
        if not path.is_relative_to(project):
            raise ToolError(f"plan '{raw}' is outside the project folder {project}")
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise ToolError(f"cannot read plan '{raw}': {exc}") from exc
        if not body.strip():
            raise ToolError(f"plan '{raw}' is empty")
        return path, str(path.relative_to(project)), hashlib.sha256(body).hexdigest()

    def tool_codex_plan_review(self, args: dict) -> dict:
        path, rel, sha = self._plan_file(args)
        cfg = self._cfg()
        model, effort = _codex_settings(cfg, args)
        feedback = _string_arg(args, "feedback")
        session_id = _string_arg(args, "session_id")
        done = relay.plan_record(self.cwd(), rel)
        round_no = int(done.get("rounds", 0)) + 1
        prompt = prompts.render(
            "team_codex_plan_review", round=round_no, plan_path=rel, sha256=sha,
            plan=path.read_text(encoding="utf-8", errors="replace"), feedback=feedback or "(none)",
        )
        extra: dict[str, Any] = {"schema": schemas.PLAN_LOOP_VERDICT}
        if session_id:
            extra["resume_session"] = session_id
        else:
            extra["cwd"] = self._repo(args)
        result = self._run_codex(cfg, "team_plan_review", prompt, model, effort, timeout_s=REVIEW_TIMEOUT_S, **extra)
        verdict = result.structured
        if not isinstance(verdict, dict) or verdict.get("verdict") not in ("APPROVED", "REVISE", "BLOCKED"):
            raise ToolError("codex plan review returned no valid verdict; nothing was recorded")
        recorded = verdict["verdict"]
        note = None
        if recorded == "APPROVED" and any(f.get("severity") in ("high", "medium") for f in verdict.get("findings") or []):
            recorded, note = "REVISE", "Codex said APPROVED but listed a high or medium finding, so it is recorded as REVISE."
        if relay.sha256_of(path) != sha:
            recorded, note = "REVISE", "the plan file changed during the review; this verdict does not apply to it."
        _, saved = self._save("plan-review", {"plan": rel, "feedback": feedback, "session_id": session_id}, prompt, result, cfg, model, effort)
        log = relay.append_plan_log(path, round_no, model, effort, sha, feedback, result.text, recorded, note)
        relay.record_plan(self.cwd(), rel, sha, recorded, round_no, model, effort)
        return _json_text(
            {
                **verdict,
                "verdict": recorded,
                "round": round_no,
                "plan": rel,
                "plan_sha256": sha,
                "session_id": result.session_id,
                "log": str(log),
                "raw_output": saved,
                **({"note": note} if note else {}),
            }
        )

    def tool_plan_status(self, args: dict) -> dict:
        path, rel, sha = self._plan_file(args)
        done = relay.plan_record(self.cwd(), rel)
        approved = done.get("verdict") == "APPROVED" and done.get("sha256") == sha
        status = {"plan": rel, "plan_sha256": sha, "approved": approved, "rounds": int(done.get("rounds", 0)), "last": done or None}
        if done and not approved and done.get("sha256") != sha:
            status["note"] = "the plan changed after its last review; review it again"
        return _json_text(status)

    def tool_review_status(self, args: dict) -> dict:
        return _json_text(approvals.status(self._repo(args), self.home))

    def refresh_codex_usage(self, cfg: config.TeamConfig) -> str | None:
        """Refresh the Codex cache when older than 120s; returns a problem message, or None."""
        read_at = usage_cache.codex_read_at(self.home)
        if read_at is not None and time.time() - read_at < CODEX_CACHE_TTL_S:
            return None
        try:
            usage_cache.write_codex(codex_limits.read(Path(cfg.codex).expanduser()), self.home)
        except WorkforceError as exc:
            return f"codex usage unavailable: {exc}"
        return None

    def tool_usage(self, args: dict) -> dict:
        cfg = self._cfg()
        problem = self.refresh_codex_usage(cfg)
        cache = usage_cache.read(self.home)
        state = usage_cache.state(cfg, self.home)
        payload = {
            "summary": state["text"],
            "state": state,
            "claude": cache["claude"] if cache["claude"] is not None else "no reading yet (the status line writes it while Claude Code runs)",
            "codex": cache["codex"] if cache["codex"] is not None else "no reading yet",
        }
        if problem:
            payload["warning"] = problem
        return _json_text(payload)

    def _laya(self) -> LayaDecider:
        return LayaDecider(os.environ.get("WF_LAYA_URL") or LAYA_URL, LAYA_CONFIDENCE, _NoEvents())

    def tool_laya(self, args: dict) -> dict:
        question = _string_arg(args, "question", required=True)
        options = args.get("options")
        if options is not None and (not isinstance(options, list) or len(options) < 2 or not all(isinstance(o, str) for o in options)):
            raise ToolError("'options' must be a list of at least two strings")
        context = _string_arg(args, "context") or question
        decider = self._laya()
        try:
            if not decider.available():
                return _text(f"Laya not running at {decider.url}: no hint available. Decide yourself or ask the user.")
            decision = decider.ask_choice(LAYA_KEY, context, question, options) if options else decider.ask_bool(LAYA_KEY, context, question)
        finally:
            decider.close()
        if decision.source != "laya":
            return _text("Laya is unsure (no usable answer). Decide yourself or ask the user.")
        confident = is_confident(decision, LAYA_CONFIDENCE)
        payload = {"answer": decision.answer, "confidence": decision.confidence, "confident": confident}
        if not confident:
            payload["note"] = "below the confidence threshold: treat as unsure"
        return _json_text(payload)

    def tool_picker(self, args: dict) -> dict:
        kind = _string_arg(args, "kind", required=True)
        cfg = self._cfg()
        models, problem = codex_models.load(cfg.codex, self.home)
        try:
            return _json_text(picker.build(kind, cfg, models, problem))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    def _check_codex_choice(self, cfg: config.TeamConfig, model: str | None, effort: str | None) -> None:
        """Refuse a model Codex does not list, or an effort that model does not support (when the live list is available)."""
        models, _ = codex_models.load(cfg.codex, self.home)
        if not models:
            return
        target = model or cfg.codex_model
        found = codex_models.find(models, target)
        if found is None:
            if model:
                raise ToolError(f"Codex does not list a model '{model}'. Available: {', '.join(m.id for m in models)}")
            return
        wanted = effort or (cfg.codex_effort if model else None)
        if wanted and found.efforts and wanted not in found.efforts:
            raise ToolError(f"{target} does not support effort '{wanted}'. It supports: {', '.join(found.efforts)}. Pick one of those too.")

    def tool_codex_settings(self, args: dict) -> dict:
        model = _string_arg(args, "model")
        effort = _string_arg(args, "effort")
        mode = _string_arg(args, "mode")
        try:
            cfg = self._cfg()
            if model or effort:
                self._check_codex_choice(cfg, model, effort)
            if mode:
                config._validate_mode(mode)
            if model or effort:
                cfg = config.set_codex(model, effort, self.home)
            if mode:
                cfg = config.set_codex_mode(mode, self.home)
        except WorkforceError as exc:
            raise ToolError(str(exc)) from exc
        return _json_text(
            {
                "codex_model": cfg.codex_model,
                "codex_effort": cfg.codex_effort,
                "codex_mode": cfg.codex_mode,
                "efforts": list(EFFORTS["codex"]),
                "modes": list(config.CODEX_MODES),
            }
        )

    def tool_team_models(self, args: dict) -> dict:
        keys = ("reviewer_model", "reviewer_effort", "fast_coder_model", "fast_coder_effort")
        values = {key: _string_arg(args, key) for key in keys}
        try:
            cfg = config.set_team_models(**values, home=self.home) if any(values.values()) else self._cfg()
        except WorkforceError as exc:
            raise ToolError(str(exc)) from exc
        return _json_text({**{key: getattr(cfg, key) for key in keys}, "efforts": list(EFFORTS["claude"]), "applies": "the next time `wf` starts"})

    def tool_settings(self, args: dict) -> dict:
        cfg = self._cfg()
        problem = self.refresh_codex_usage(cfg)
        state = usage_cache.state(cfg, self.home)
        decider = self._laya()
        try:
            laya = f"running at {decider.url}" if decider.available() else f"not running at {decider.url} (the laya tool gives no hints)"
        finally:
            decider.close()
        write_note = "Codex may edit files in this project when you ask it to" if cfg.codex_mode == "write" else "Codex only reads; it never edits files"
        lines = [
            "WorkForce settings",
            "",
            "This chat    Claude's model and effort: /model",
            f"Codex        {cfg.codex_model} · {cfg.codex_effort} · mode {cfg.codex_mode} ({write_note})",
            f"             change: /codex-model   /codex-effort   /codex-mode read-only|write",
            f"Reviewer     {cfg.reviewer_model} · {cfg.reviewer_effort}",
            f"Fast coder   {cfg.fast_coder_model} · {cfg.fast_coder_effort}",
            f"             change: /claude-model   /claude-effort  (reviewer: next review; fast coder: next time `wf` starts)",
            f"Usage limits alert at {cfg.alert_percent}% · stop at {cfg.stop_percent}%",
            f"             change: edit alert_percent / stop_percent in {config.config_path(self.home)}",
            f"             carry on past a stop: /wf-continue",
            f"Usage now    {state['text']}" + (f"  ({problem})" if problem else ""),
            f"             details: /usage",
            f"Laya         {laya}",
            f"             (a local hint model; set WF_LAYA_URL to point at another address)",
        ]
        return _text("\n".join(lines))


class _Call:
    """One in-flight `tools/call`: the agent processes it started, so a cancellation can kill exactly those."""

    def __init__(self) -> None:
        self.procs: set = set()
        self.cancelled = False


_local = threading.local()
_tracking_installed = False


def _install_tracking() -> None:
    """Make `agents.base.register` also note each child process on the tool call (thread) that started it."""
    global _tracking_installed
    if _tracking_installed:
        return
    original = agent_base.register

    def register(proc) -> None:
        original(proc)
        call = getattr(_local, "call", None)
        if call is not None:
            call.procs.add(proc)
            if call.cancelled:
                agent_base.kill_group(proc)

    agent_base.register = register
    _tracking_installed = True


def _call_key(message: dict) -> Any:
    """The request id when it is a plain string or number (usable as a dict key), else None."""
    value = message.get("id")
    return value if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def shutdown_children(grace_s: float = 2.0) -> None:
    """Refuse new agent processes and stop every Codex/Claude child (their own process groups)."""
    agent_base.request_shutdown()
    agent_base.kill_all(grace_s)


def serve(stdin=None, stdout=None, server: Server | None = None) -> None:
    """Read newline-delimited JSON-RPC from stdin until EOF; tool calls run on worker threads so `ping` never waits.

    On EOF every Codex/Claude child is killed instead of waited for. `notifications/cancelled` kills the children of the
    cancelled call and sends no response for it.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = server or Server()
    write_lock = threading.Lock()
    calls: dict[Any, _Call] = {}
    calls_lock = threading.Lock()
    _install_tracking()

    def send(message: dict | None) -> None:
        if message is None:
            return
        with write_lock:
            stdout.write(json.dumps(message) + "\n")
            stdout.flush()

    def respond(message: Any) -> None:
        try:
            send(server.handle(message))
        except Exception as exc:
            request_id = message.get("id") if isinstance(message, dict) else None
            send(Server._error(request_id, -32603, f"internal error: {exc}"))

    def run_call(message: dict, call: _Call) -> None:
        _local.call = call
        try:
            response = server.handle(message)
        except Exception as exc:
            response = Server._error(message.get("id"), -32603, f"internal error: {exc}")
        finally:
            _local.call = None
            with calls_lock:
                calls.pop(_call_key(message), None)
        if not call.cancelled:
            send(response)

    def cancel(params: Any) -> None:
        request_id = params.get("requestId") if isinstance(params, dict) else None
        with calls_lock:
            call = calls.get(request_id) if isinstance(request_id, (str, int)) else None
            if call is None:
                return
            call.cancelled = True
            procs = list(call.procs)
        for proc in procs:
            agent_base.kill_group(proc)

    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                send(Server._error(None, -32700, "parse error"))
                continue
            method = message.get("method") if isinstance(message, dict) else None
            if method == "notifications/cancelled":
                cancel(message.get("params"))
            elif method == "tools/call":
                call = _Call()
                key = _call_key(message)
                if key is not None:
                    with calls_lock:
                        calls[key] = call
                pool.submit(run_call, message, call)
            else:
                respond(message)
    finally:
        shutdown_children()
        pool.shutdown(wait=False, cancel_futures=True)


def _terminate(signum: int, _frame: Any) -> None:
    shutdown_children()
    os._exit(128 + signum)


def main() -> None:
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    try:
        serve()
    finally:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
    os._exit(0)


if __name__ == "__main__":
    main()
