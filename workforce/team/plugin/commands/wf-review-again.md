---
description: Allow more review rounds after the reviewers hit the limit of 3 rejections in a row
---

The WorkForce hook has already handled this command when you were prompted: it reset the review round limit for this project. Tell the user in one line that more review rounds are allowed. Then follow what the user said about the disputed findings (in this message or the ones before it), make any changes, and run `mcp__plugin_workforce_codex__claude_review` and `mcp__plugin_workforce_codex__codex_review` again. A commit still needs both approvals.
