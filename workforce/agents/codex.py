from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, Mapping

from workforce import schemas
from workforce.decider import hooks
from workforce.agents import guard
from workforce.agents.base import (
    AgentRequest,
    AgentResult,
    EventLog,
    failure,
    stream_process,
)

AUTH_MARKERS = ("401", "403", "login", "log in", "authenticat", "unauthorized")
RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "usage limit", "429", "too many requests")


def classify_error(message: str) -> str:
    """Map a Codex failure message to an AgentResult error_kind."""
    lowered = message.lower()
    if "not supported when using codex with a chatgpt account" in lowered:
        return "model_unavailable"
    if "model metadata" in lowered and "not found" in lowered:
        return "model_unavailable"
    if any(m in lowered for m in RATE_LIMIT_MARKERS):
        return "rate_limited"
    if any(m in lowered for m in AUTH_MARKERS):
        return "auth"
    return "crash"


def configured_mcp_servers(extra_env: Mapping[str, str] | None = None) -> list[str]:
    """Names of the MCP servers in the user's Codex `config.toml`, so a read-only run can switch each one off."""
    env = {**os.environ, **(extra_env or {})}
    home = Path(env["CODEX_HOME"]).expanduser() if env.get("CODEX_HOME") else Path(env.get("HOME") or Path.home()) / ".codex"
    try:
        data = tomllib.loads((home / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    return [name for name in servers if re.fullmatch(r"[A-Za-z0-9_-]+", name)]


class CodexRunner:
    """Drives the official `codex exec` CLI headlessly on the user's ChatGPT login."""

    def __init__(self, binary: Path, extra_env: dict | None = None):
        self.binary = Path(binary).expanduser()
        self.extra_env = extra_env

    def build_command(
        self,
        req: AgentRequest,
        output_file: Path,
        schema_file: Path | None,
    ) -> list[str]:
        """The codex argv. `req.add_dirs` is ignored on purpose: Codex reads outside its cwd, and `--add-dir` would make them writable."""
        cmd = [str(self.binary), "exec"]
        if req.resume_session:
            cmd += ["resume", req.resume_session]
        if req.repo is not None:
            cmd += hooks.codex_hook_args(req.repo, worktree=req.cwd)
        cmd += [
            "--json",
            "-m",
            req.model,
            "-c",
            f"model_reasoning_effort={req.effort}",
        ]
        if req.resume_session:
            cmd += ["-c", f'sandbox_mode="{req.sandbox}"']
        else:
            cmd += ["-s", req.sandbox, "-C", str(req.cwd)]
        if not req.browser:
            cmd += ["--disable", "browser_use", "--disable", "computer_use"]
        elif not req.computer_use:
            cmd += ["--disable", "computer_use"]
        if req.no_mcp:
            cmd += ["-c", "features.plugins=false"]
            for name in configured_mcp_servers(self.extra_env):
                cmd += ["-c", f"mcp_servers.{name}.enabled=false"]
        if req.skip_git_check:
            cmd.append("--skip-git-repo-check")
        if schema_file is not None:
            cmd += ["--output-schema", str(schema_file)]
        cmd += ["-o", str(output_file), req.prompt]
        return cmd

    def run(
        self, req: AgentRequest, on_event: Callable[[dict], None] | None = None
    ) -> AgentResult:
        env = guard.agent_env(self.extra_env)
        log = EventLog(req.log_path)
        seen: dict = {
            "session_id": req.resume_session,
            "text": "",
            "usage": {},
            "completed": False,
            "failed": None,
            "last_error": None,
        }

        def handle(line: str) -> bool:
            if not line.strip():
                return False
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("not an object")
            except ValueError:
                event = {"type": "raw", "line": line}
            log.write(event)
            if on_event is not None:
                on_event(event)
            etype = event.get("type")
            if etype == "thread.started":
                seen["session_id"] = event.get("thread_id") or seen["session_id"]
            elif etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    seen["text"] = item.get("text") or ""
            elif etype == "turn.completed":
                seen["completed"] = True
                seen["usage"] = event.get("usage") or {}
            elif etype == "turn.failed":
                seen["failed"] = (event.get("error") or {}).get("message") or "turn failed"
            elif etype == "error":
                seen["last_error"] = event.get("message") or "error"
            return False

        with tempfile.TemporaryDirectory(prefix="wf-codex-") as tmp:
            output_file = Path(tmp) / "last_message.txt"
            schema_file = None
            if req.schema is not None:
                schema_file = Path(tmp) / "schema.json"
                schema_file.write_text(json.dumps(req.schema))
            cmd = self.build_command(req, output_file, schema_file)
            try:
                outcome = stream_process(cmd, req.cwd, env, req.timeout_s, handle)
            except OSError as exc:
                return failure("crash", f"cannot start codex at {self.binary}: {exc}")
            finally:
                log.close()
            file_text = output_file.read_text() if output_file.exists() else None

        common = {"session_id": seen["session_id"], "usage": seen["usage"], "text": seen["text"]}
        if outcome.timed_out:
            return failure("timeout", f"codex run exceeded {req.timeout_s}s", **common)
        if seen["failed"] is not None:
            return failure(classify_error(seen["failed"]), seen["failed"], **common)
        if not seen["completed"]:
            message = seen["last_error"] or outcome.stderr.strip()[-500:]
            kind = classify_error(message) if message else "crash"
            return failure(
                kind,
                f"codex exited with code {outcome.returncode} without completing a turn"
                + (f": {message}" if message else ""),
                **common,
            )

        text = seen["text"] or (file_text or "")
        common["text"] = text
        structured = None
        if req.schema is not None:
            structured, problem = _load_structured(file_text, text, req.schema)
            if problem:
                return failure("schema", problem, **common)
        return AgentResult(
            ok=True,
            text=text,
            structured=structured,
            session_id=seen["session_id"],
            model=req.model,
            usage=seen["usage"],
            rate_limits=None,
        )


def _load_structured(file_text: str | None, text: str, schema: dict):
    for candidate in (file_text, text):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(value, dict):
            return None, "structured output is not a JSON object"
        problems = schemas.validate(schema, value)
        if problems:
            return None, f"structured output invalid: {'; '.join(problems[:3])}"
        return value, None
    return None, "no valid JSON structured output was produced"
