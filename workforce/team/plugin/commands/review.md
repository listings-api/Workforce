---
description: Run the Claude reviewer and Codex on the current changes and show whether a commit is allowed
argument-hint: "[focus] [repo=<path>]"
allowed-tools: mcp__plugin_workforce_codex__claude_review, mcp__plugin_workforce_codex__codex_review, mcp__plugin_workforce_codex__review_status
---

Review the current uncommitted changes with both reviewers, independently:

1. In one step, call both tools: `mcp__plugin_workforce_codex__claude_review` and `mcp__plugin_workforce_codex__codex_review` (pass the arguments below as `focus`, if there are any; pass `repo` when the changes are in a subfolder repo of the project folder). The server runs each reviewer and records its verdict itself; you cannot record one by hand.
2. Then call `mcp__plugin_workforce_codex__review_status` (with the same `repo`) and show: each reviewer's verdict, the top findings, and whether a commit is now allowed.
3. If either says REQUEST_CHANGES, list what to fix. Do not fix anything unless the user asks; the normal flow is to fix and run /review again, because any edit voids both approvals.

Arguments: $ARGUMENTS
