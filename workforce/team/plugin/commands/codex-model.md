---
description: Pick the Codex model and effort from a menu (or give them directly)
argument-hint: "[model] [effort]"
allowed-tools: mcp__plugin_workforce_codex__picker, mcp__plugin_workforce_codex__codex_settings, AskUserQuestion
---

Arguments: $ARGUMENTS

- **No arguments:** call `mcp__plugin_workforce_codex__picker` with `kind` = `codex_model`. Pass its `questions` unchanged to AskUserQuestion in one call. Then do what its `then` says with the answers.
- **Arguments given:** the first word is the model and the optional second word is the effort. Call `mcp__plugin_workforce_codex__codex_settings` with them.

If the settings tool returns an error (an unknown model, or an effort that model does not support), show it with the valid values and offer the menu again. Never guess a value. Finish with one line: the Codex model and effort now set.
