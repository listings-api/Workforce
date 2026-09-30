---
description: Ask Codex to research the task and write a plan
argument-hint: "<task>"
allowed-tools: mcp__plugin_workforce_codex__codex_plan, mcp__plugin_workforce_codex__codex_ask
---

Call `mcp__plugin_workforce_codex__codex_plan` with `task` set to the text below, plus any context you already know that Codex would need.

Task: $ARGUMENTS

Show the plan and its risks. Say where you agree and where you would do it differently. If you disagree, you may debate with `mcp__plugin_workforce_codex__codex_ask` (reuse the `session_id`) for up to 2 rounds, then ask the user to choose.
