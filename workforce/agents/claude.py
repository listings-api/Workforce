from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Callable

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

MODEL_UNAVAILABLE_MARKERS = (
    "unrecognized_model",
    "does not support this model",
    "may not exist or you may not have access",
)
AUTH_MARKERS = ("401", "403", "login", "log in", "authenticat", "unauthorized")
RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "usage limit")


def classify_error(text: str) -> str:
    """Map a Claude error result string to an AgentResult error_kind."""
    lowered = text.lower()
    if any(m in lowered for m in MODEL_UNAVAILABLE_MARKERS):
        return "model_unavailable"
    if lowered.startswith("api error: 400") and "model" in lowered:
        return "model_unavailable"
    if any(m in lowered for m in AUTH_MARKERS):
        return "auth"
    if any(m in lowered for m in RATE_LIMIT_MARKERS):
        return "rate_limited"
    return "crash"


class ClaudeRunner:
    """Drives the official `claude` CLI headlessly on the user's subscription login."""

    def __init__(self, binary: Path, extra_env: dict | None = None):
        self.binary = Path(binary).expanduser()
        self.extra_env = extra_env

    def build_command(self, req: AgentRequest, prompt_on_stdin: bool = False) -> list[str]:
        """The claude argv. Read-only requests run in plan mode, which denies Write, Edit and Bash.

        With `prompt_on_stdin` the prompt is left out: `claude -p` reads it from stdin, which keeps long
        prompts (diffs, decisions) out of `ps` and clear of ARG_MAX.
        """
        cmd = [str(self.binary), "-p"]
        if not prompt_on_stdin:
            cmd.append(req.prompt)
        cmd += [
            "--model",
            req.model,
            "--effort",
            req.effort,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "dontAsk" if req.tools is not None else "plan" if req.sandbox == "read-only" else "auto",
        ]
        if req.tools is not None:
            names = ",".join(req.tools)
            cmd += ["--tools", names, "--allowedTools", names, "--permission-prompts", "none", "--safe-mode"]
        if not req.user_setup:
            cmd += [
                "--setting-sources",
                "project",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
            ]
        if req.schema is not None:
            cmd += ["--json-schema", json.dumps(req.schema)]
        if req.resume_session:
            cmd += ["--resume", req.resume_session]
        if req.browser:
            cmd.append("--chrome")
        if req.repo is not None:
            cmd += [
                "--settings",
                hooks.claude_settings_json(
                    req.repo, worktree=req.cwd, denylist_only=req.user_setup
                ),
            ]
        for directory in req.add_dirs:
            cmd += ["--add-dir", str(directory)]
        return cmd

    def run(
        self, req: AgentRequest, on_event: Callable[[dict], None] | None = None
    ) -> AgentResult:
        stdin_prompt = "stdin_text" in inspect.signature(stream_process).parameters
        cmd = self.build_command(req, prompt_on_stdin=stdin_prompt)
        env = guard.agent_env(self.extra_env)
        log = EventLog(req.log_path)
        seen: dict = {
            "session_id": req.resume_session,
            "model": None,
            "rate_limits": None,
            "result": None,
            "bad_key": None,
            "init": False,
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
            if etype == "system" and event.get("subtype") == "init":
                seen["init"] = True
                seen["session_id"] = event.get("session_id") or seen["session_id"]
                seen["model"] = event.get("model")
                source = event.get("apiKeySource")
                if source != "none":
                    seen["bad_key"] = source
                    return True
            elif etype == "rate_limit_event":
                windows = (event.get("rate_limit_info") or {}).get("unifiedWindows")
                if windows is not None:
                    seen["rate_limits"] = windows
            elif etype == "result":
                seen["result"] = event
                seen["session_id"] = event.get("session_id") or seen["session_id"]
            return False

        kwargs = {"stdin_text": req.prompt} if stdin_prompt else {}
        try:
            outcome = stream_process(cmd, req.cwd, env, req.timeout_s, handle, **kwargs)
        except OSError as exc:
            return failure("crash", f"cannot start claude at {self.binary}: {exc}")
        finally:
            log.close()

        common = {
            "session_id": seen["session_id"],
            "model": seen["model"],
            "rate_limits": seen["rate_limits"],
        }
        if seen["bad_key"] is not None:
            return failure(
                "auth",
                f"{guard.SUBSCRIPTION_VIOLATION}: claude reported apiKeySource={seen['bad_key']!r}; "
                "WorkForce only runs on the subscription login (expected 'none'). Unset any API key "
                "or provider variable, log in with `claude auth login`, then resume",
                **common,
            )
        if outcome.timed_out:
            return failure("timeout", f"claude run exceeded {req.timeout_s}s", **common)
        result = seen["result"]
        if result is None:
            detail = outcome.stderr.strip()[-500:]
            return failure(
                "crash",
                f"claude exited with code {outcome.returncode} and no result event"
                + (f": {detail}" if detail else ""),
                **common,
            )

        if not seen["init"]:
            return failure(
                "crash",
                "claude produced a result without a system/init event, so apiKeySource "
                "could not be verified",
                **common,
            )

        text = result.get("result") or ""
        if not isinstance(text, str):
            text = json.dumps(text)
        usage = dict(result.get("usage") or {})
        if result.get("modelUsage"):
            usage["modelUsage"] = result["modelUsage"]
        common["usage"] = usage
        common["text"] = text

        if result.get("is_error") or text.startswith("API Error"):
            return failure(classify_error(text), text or "claude reported an error", **common)

        structured = result.get("structured_output")
        if req.schema is not None:
            if not isinstance(structured, dict):
                return failure("schema", "no structured_output returned for a schema run", **common)
            problems = schemas.validate(req.schema, structured)
            if problems:
                return failure("schema", f"structured output invalid: {'; '.join(problems[:3])}", **common)
        return AgentResult(
            ok=True,
            text=text,
            structured=structured if isinstance(structured, dict) else None,
            session_id=seen["session_id"],
            model=seen["model"],
            usage=usage,
            rate_limits=seen["rate_limits"],
        )
