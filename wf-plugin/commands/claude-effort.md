---
description: Pick the effort of the team's Claude reviewer or fast coder from a menu (this chat's own effort is /model)
argument-hint: "[reviewer|fast-coder|both] [effort]"
allowed-tools: mcp__plugin_workforce_codex__picker, mcp__plugin_workforce_codex__team_models, AskUserQuestion
---

Arguments: $ARGUMENTS

This chat's own effort is changed in `/model` (the left and right arrows).

- **No arguments:** call `mcp__plugin_workforce_codex__picker` with `kind` = `claude_effort`. Pass its `questions` unchanged to AskUserQuestion in one call. Then do what its `then` says with the answers.
- **Arguments given:** `reviewer`, `fast-coder` or `both`, then the effort. Call `mcp__plugin_workforce_codex__team_models` with the matching keys.

If the tool returns an error, show it with the valid values. Never guess a value. Finish with one line saying what is now set.
