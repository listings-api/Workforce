"""The Claude half of the two-reviewer gate: the MCP server spawns a fresh headless Claude and records its verdict itself.

Claude never records its own verdict. The reviewer is `claude -p` in safe mode with only Read, Glob and Grep, no user settings, no MCP
servers and no plugin, so it can neither change anything nor call back into WorkForce.
"""

from __future__ import annotations

from pathlib import Path

from workforce import schemas
from workforce.agents.base import AgentRequest, AgentResult
from workforce.agents.claude import ClaudeRunner
from workforce.errors import WorkforceError
from workforce.team import config

ROLE = "team_claude_review"
PROMPT = """# Role: independent code reviewer (read-only)

You review the current uncommitted changes on your own. A separate reviewer (Codex) is looking at the same changes; you cannot see their work and your verdict must not depend on it.

## Hard rules
- You are read-only. Do not edit, create, delete or commit anything.
- The diff below is the starting point, not the whole picture. Read any repository file you need (callers, tests, config) before judging.
- Check correctness, missing or weak tests, broken callers, security problems and anything that contradicts the stated focus.
- Verdict `APPROVE` only when no blocker or major problem remains. Minor issues and nits may accompany an approval.
- Verdict `REQUEST_CHANGES` when the author can fix the problems.
- Verdict `BLOCKED` only for a problem that needs a human decision. Never use it for ordinary bugs.
- Build artifacts or caches in the diff (`__pycache__/`, `*.pyc`, `node_modules/`, `dist/`, `.DS_Store`) are a `major` finding.
- Your verdict and your findings must agree: do not approve while listing a blocker or major finding.

## Focus from the requester
{focus}

## Snapshot under review
Tree hash: {tree}
Compared against: {base}

Changed files:
{files}

Summary of changes:
{stat}

## Diff (working state including new files)
{diff}

## Output contract
Reply with one JSON object matching the verdict schema exactly:
- `verdict`: `APPROVE`, `REQUEST_CHANGES` or `BLOCKED`
- `summary`: a few sentences explaining the verdict
- `findings`: array of objects with `severity` (`blocker`, `major`, `minor`, `nit`), `file`, `line` (or null) and `message` saying what to change (empty only when there is nothing to report)
"""


READ_TOOLS = ("Read", "Glob", "Grep")


def build_prompt(**fields: str) -> str:
    return PROMPT.format(**fields)


def run(cfg: config.TeamConfig, repo: Path, prompt: str, timeout_s: int) -> AgentResult:
    """Run the reviewer in `repo` (read-only, verdict schema); raises `WorkforceError` when it does not produce a verdict."""
    request = AgentRequest(
        role=ROLE,
        agent="claude",
        model=cfg.reviewer_model,
        effort=cfg.reviewer_effort,
        prompt=prompt,
        cwd=repo,
        schema=schemas.VERDICT,
        sandbox="read-only",
        tools=READ_TOOLS,
        timeout_s=timeout_s,
    )
    result = ClaudeRunner(Path(cfg.claude)).run(request)
    if not result.ok or not isinstance(result.structured, dict):
        raise WorkforceError(f"claude reviewer failed ({result.error_kind}): {result.error}")
    return result
