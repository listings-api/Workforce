---
description: "Opt-in heavy workflow: recon, interview, a written plan that Codex reviews until APPROVED, then build and both code reviews"
argument-hint: "<task> [rounds=<n>] [repo=<path>]"
allowed-tools: mcp__plugin_workforce_codex__codex_plan_review, mcp__plugin_workforce_codex__plan_status, mcp__plugin_workforce_codex__codex_ask, mcp__plugin_workforce_codex__claude_review, mcp__plugin_workforce_codex__codex_review, mcp__plugin_workforce_codex__review_status
---

The user typed /plan-loop, so run the full plan loop for this one task. Outside /plan-loop the normal lighter team flow applies; do not use this process unless asked.

Task and options: $ARGUMENTS

Settings: `rounds=<n>` is the plan-review round cap (default 5). `repo=<path>` is the repo for a subfolder repo of the project folder; pass it as `repo` to the review tools. Pick a short slug for the task. The plan is `.workforce/team/plans/<slug>/PLAN.md` in the project folder; the server appends each review round verbatim to `REVIEW-LOG.md` beside it. Never edit REVIEW-LOG.md yourself. Echo the slug, plan path, round cap and the Codex model before starting.

1. **Recon.** Read the code the task touches: callers, writers of shared state, tests, config, conventions, and any CONTEXT.md, ADRs or docs. Present one assumptions ledger, each line with its source (file path or link). Ask the user to correct the material ones, in one batch.
2. **Interview.** Keep a short visible list of open decisions. Ask only about decisions that change the outcome, and never ask what the code can answer. For each question give your recommendation, why it matters and the cost of guessing wrong. Batch independent questions with AskUserQuestion; ask dependent ones in order. Offer "accept all remaining recommendations" when the list is long.
3. **Write the plan** at the plan path, containing:
   - the goal and observable acceptance criteria;
   - the approach, key decisions, trade-offs and non-goals;
   - confirmed assumptions with sources, and the remaining risks;
   - the ordered steps, naming files and functions;
   - verification: the exact proof commands and their expected results, plus any manual checks.
   Tell the user the path.
4. **Codex reviews the plan.** Call `mcp__plugin_workforce_codex__codex_plan_review` with `plan`. Quote Codex's verdict and findings verbatim, then give "Claude's view" of each finding separately: accept it (and edit the plan) or reject it with evidence.
   - For the next round, pass the `session_id` from the last result and `feedback` holding your disposition of every finding, so the same reviewer checks the revision. Any edit to the plan needs another round; an approval covers only the exact file it reviewed.
   - Stop at `APPROVED`. A `BLOCKED` verdict or a failed call is never an approval: explain what is missing and ask the user.
   - At the round cap, stop. Show the unresolved findings with both positions and ask the user: more rounds, accept the plan as it is (then call it unapproved, never approved), or change direction.
5. **Build.** If the user's /plan-loop request did not already ask for the implementation, ask now. Call `mcp__plugin_workforce_codex__plan_status` first; build only when `approved` is true or the user explicitly chose to go ahead with an unapproved plan. Build from the plan (delegate mechanical steps to `fast-coder`) and run every proof command.
6. **Code review and commit.** Call `mcp__plugin_workforce_codex__claude_review` and `mcp__plugin_workforce_codex__codex_review` in one step, fix every REQUEST_CHANGES and review again. The commit gate applies as always. Commit and push only as the user's git instructions say.

Finish with: the plan path, the rounds used and the final plan verdict, the proof results, both code-review verdicts, any unresolved findings or deviations from the plan, and the diff summary.
