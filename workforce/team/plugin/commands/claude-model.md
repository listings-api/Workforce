---
description: Pick the model and effort of the team's Claude reviewer or fast coder from a menu (this chat's own model is /model)
argument-hint: "[reviewer|fast-coder|both] [model] [effort]"
allowed-tools: mcp__plugin_workforce_codex__picker, mcp__plugin_workforce_codex__team_models, AskUserQuestion
---

Arguments: $ARGUMENTS

This sets the team's other Claudes: the **reviewer** (the fresh Claude that reviews every change before a commit) and the **fast coder** sub-agent. This chat's own Claude is changed with `/model`, Claude Code's own picker. Say that in one line if the user seems to want the chat's model.

- **No arguments:** call `mcp__plugin_workforce_codex__picker` with `kind` = `claude_model`. Pass its `questions` unchanged to AskUserQuestion in one call. Then do what its `then` says with the answers.
- **Arguments given:** the first word is `reviewer`, `fast-coder` or `both`, then the model, then the optional effort. Call `mcp__plugin_workforce_codex__team_models` with the matching keys.

A reviewer change applies at the next review. A fast-coder change applies the next time `wf` starts. If the tool returns an error, show it with the valid values. Never guess a value. Finish with one line saying what is now set.
