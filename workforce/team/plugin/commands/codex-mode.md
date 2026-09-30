---
description: Show or set Codex's mode: read-only (default) or write (Codex may edit files when you ask it to)
argument-hint: "read-only|write"
allowed-tools: mcp__plugin_workforce_codex__codex_settings
---

Call `mcp__plugin_workforce_codex__codex_settings`.

Arguments: $ARGUMENTS

- With no arguments, call it with no parameters and show the current `codex_mode`.
- With `read-only` or `write`, call it with `mode` set to that word and show the result. Do not guess another value if the tool returns an error; show the error.
- Explain in one line: in `write` mode `mcp__plugin_workforce_codex__codex_ask` runs Codex with `workspace-write` in this project (so it can edit files when the user asks it to). `mcp__plugin_workforce_codex__codex_plan` and `mcp__plugin_workforce_codex__codex_review` always stay read-only, and a commit still needs both reviews.
