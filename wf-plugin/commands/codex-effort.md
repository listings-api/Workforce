---
description: Pick the Codex effort from a menu (or give it directly)
argument-hint: "[effort]"
allowed-tools: mcp__plugin_workforce_codex__picker, mcp__plugin_workforce_codex__codex_settings, AskUserQuestion
---

Arguments: $ARGUMENTS

- **No arguments:** call `mcp__plugin_workforce_codex__picker` with `kind` = `codex_effort`. Pass its `questions` unchanged to AskUserQuestion in one call. Then do what its `then` says with the answer.
- **An argument given:** call `mcp__plugin_workforce_codex__codex_settings` with `effort` = that word.

If the settings tool returns an error, show it with the valid values. Never guess a value. Finish with one line: the Codex model and effort now set.
