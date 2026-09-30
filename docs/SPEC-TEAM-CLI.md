# WorkForce team CLI: `wf` = Claude Code + Codex

The user wants exactly the Claude Code CLI experience, with Codex added to the team. **No workspace config, integration branches or model-pick screens.** `wf` launches the real interactive `claude`, with a plugin, settings and appended instructions. WorkForce only runs as a chat: `wf -p` / `--print` and the earlier unattended pipeline (`wf run`, `status`, `init`, …) were removed and are refused with a clear message.

Everything here uses the subscriptions via the official CLIs: never an API key, and never a model API called directly.

## 1. What the user sees
```
$ wf                      # or: wf "fix the flaky test in billing"
██╗    ██╗███████╗
██║ █╗ ██║██╔════╝        WorkForce · Claude + Codex team
╚███╔███╔╝██║             Codex gpt-6-sol · high  │  Claude Opus 5.5 · high
 ╚══╝╚══╝ ╚═╝             ~/code/billing
[then the normal Claude Code screen: its own header, input box, footer]
status line:  WF │ Claude 5h 23% · wk 44% │ Codex wk 3% │ codex gpt-6-sol·high
```
- The **WF banner** is a 4–5 row block-letter "WF" in the logo blue gradient (`#5AA8FF` → `#2F6FE0`), truecolor with a 256-colour fallback. It's printed once before exec'ing claude. Claude Code's own banner can't be changed (confirmed in its docs), so ours sits above it.
- Everything else is Claude Code: same input box, permissions, `/model`, shift+tab, sub-agents, git.

## 2. Pieces (all code under `workforce/team/`; the plugin is package data in `workforce/team/plugin/`)

### 2a. Launcher (`workforce/team/launch.py`, entry point `wf`)
- `wf [args…]`: `wf doctor`, `wf demo` and `wf codex` are WorkForce's own subcommands; `-p` / `--print` and the removed pipeline subcommands exit 3 with a message. Otherwise print the banner and `os.execvp` the claude binary with:
  - `--plugin-dir ~/.workforce/plugin` (built from the packaged plugin by `plugin_runtime.prepare`, with `sys.executable` written into `.mcp.json` and `hooks/hooks.json`)
  - `--settings <json>`: only `{"statusLine": {"type": "command", "command": "<python> -m workforce.team.statusline"}, "env": {...}}`
  - `--append-system-prompt "<contents of the plugin's TEAM.md>"`
  - then all the user's args unchanged (so `wf "task"`, `wf --resume`, `wf -c` all work like `claude`).
- Refuse (clear message, exit 3) if `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `OPENAI_API_KEY` is set. The binaries come from `~/.workforce/team.toml` (created on first run with the paths `detect.find_cli` found; `config.repair_binaries` updates a path that stopped working).
- `workforce` is another name for `wf`.

### 2b. MCP server "codex" (`workforce/team/mcp_server.py`, stdio JSON-RPC, no new deps)
Implement the MCP stdio protocol by hand: `initialize`, `tools/list`, `tools/call`, `notifications/initialized`, `ping`. Tools (the working dir is the process cwd = the user's project dir, via `${CLAUDE_PROJECT_DIR}` or cwd):

| tool | args | does |
|---|---|---|
| `codex_plan` | task, context?, model?, effort? | Codex `exec -s read-only -C <cwd>` (plus `--skip-git-repo-check` if not a repo). Researches and returns a numbered plan plus risks. Returns text plus `session_id`. |
| `codex_ask` | message, session_id? | Follow-up in the same Codex session (`exec resume`), or a fresh read-only question. For debate and research. |
| `codex_review` | focus?, base? | Codex read-only review of the current changes: diff of the working state vs `base` (default HEAD), including untracked files, via `git_ops.diff_against` + `tree_hash`. Returns the VERDICT JSON (APPROVE/REQUEST_CHANGES/BLOCKED + findings). **Records the verdict for the tree hash** in approvals. |
| `claude_review` | repo?, focus?, base? | The server runs a fresh headless Claude reviewer (plan mode, read-only, `reviewer_model`) and records its signed verdict itself (replaced an earlier self-reported `record_claude_review`). |
| `review_status` | none | For the current tree: both verdicts, whether commit is allowed, and what's missing. |
| `usage` | none | Claude 5h/weekly (from the statusline cache) plus Codex windows (app-server, cached 120s), with alert/stop state. |
| `codex_settings` | model?, effort? | Get/set the Codex default model/effort (persisted in `~/.workforce/team.toml`). Defaults: `gpt-6-sol` / `high`. |

- Reuse the existing modules: `agents.codex.CodexRunner` (keeps env scrubbing and the subscription checks), `git_ops`, `usage.codex_limits`, `schemas.VERDICT`.
- Codex reviews/plans always run read-only.
- Tool errors come back as MCP `isError` results with a clear message, never a crash.
- Log every call to `~/.workforce/team.log` (JSONL, prompt text truncated, secrets redacted via `guard.redact`).

### 2c. Approvals (`workforce/team/approvals.py`)
- File: `<git common dir>/wf-approvals.json` (shared by the repo's worktrees; never committed). Content: `{tree_hash: {"claude": {verdict, summary, at}, "codex": {...}}}`, pruned to the last 50 trees.
- `current_tree(cwd)`: `git_ops.tree_hash`, the whole working state including untracked files.
- `record(cwd, reviewer, verdict, summary, findings)`
- `status(cwd) -> {tree, claude, codex, commit_allowed, missing}`
- commit allowed ⇔ both are APPROVE for the current tree.

### 2d. Plugin (`workforce/team/plugin/`)
- `.claude-plugin/plugin.json`: name `workforce`.
- `.mcp.json`: server `codex` → `<python> -m workforce.team.mcp_server`, where `<python>` is `sys.executable`, written by `wf` into the prepared copy at launch.
- `agents/`:
  - `fast-coder.md`: `model: claude-sonnet-5-5`, for small, well-specified sub-tasks.
- `commands/`:
  - `/codex-model <model> [effort]` (calls `codex_settings`)
  - `/usage`
  - `/review` (run both reviews on the current changes and show `review_status`)
  - `/plan <task>` (`codex_plan`)
  - `/wf-continue` (override the 60% stop for the current usage window)
- `hooks/hooks.json` → `<venv python> -m workforce.team.hooks <event>`:
  - **PreToolUse `Bash`:** if the command runs `git commit` (any form, including `-C`, chained with `&&`/`;`), check `approvals.status`. If not both APPROVE for the current tree → deny with the reason ("run /review: Claude reviewer + Codex must both approve these exact changes; missing: …"). Also always deny `--no-gpg-sign`, `commit.gpgsign=false` and `git push --force` (reuse the deny-list from `team/denylist.py`).
  - **UserPromptSubmit** and **PreToolUse `*`:** read the usage cache. At ≥ 60% on any window, block, with a reason naming the window and the reset time, unless a `/wf-continue` override exists for that window's reset time. At ≥ 50%, don't block: for UserPromptSubmit, add the context "usage alert: …" so Claude mentions it.
- `TEAM.md` (appended system prompt), short and firm:
  1. For any non-trivial task, first call `codex_plan` (Codex plans and researches), and debate disagreements briefly with `codex_ask`. If you still disagree after 2 rounds, ask the user (AskUserQuestion) with both positions.
  2. Claude writes the code. Delegate small, mechanical sub-tasks to the `fast-coder` sub-agent (Sonnet 5.5). Keep hard or cross-cutting work yourself.
  3. Before any commit, run both reviews: `claude_review` and `codex_review`. Fix REQUEST_CHANGES and re-review. If they disagree after 2 rounds, ask the user. The commit hook enforces this.
  4. Follow the user's git instructions exactly (branch names, pulling, where to commit). Never force-push, never bypass signing, and don't push unless asked.
  5. The user picks models: don't change the Codex model/effort unless the user asks (`/codex-model`).

### 2e. Status line (`workforce/team/statusline.py`)
- Reads Claude Code's stdin JSON: `rate_limits.five_hour.used_percentage`, `.resets_at`, `rate_limits.seven_day.*`, `model.display_name`, `workspace.current_dir`.
- Prints one line: `WF │ Claude 5h 23% · wk 44% │ Codex wk 3% │ codex gpt-6-sol·high`, coloured yellow ≥ 50% and red ≥ 60%.
- Writes the Claude part to `~/.workforce/usage.json` (atomic) for the hooks and the `usage` tool.
- The Codex part comes from the cache the MCP server writes (`~/.workforce/codex_usage.json`), refreshed at most every 120s. It never blocks for more than 300ms: if stale, print last-known with `~`.
- Missing fields → `?`, never made-up numbers.

## 3. Tests (fakes only; never the real home: pass `home`/env overrides)
- the MCP server: a JSON-RPC handshake, tools/list, and each tool against the fake codex (`tests/fakes/fake_codex.py`), with error paths;
- approvals: tree hash changes void, both-approve logic, the common-dir location with worktrees;
- the commit hook: `git commit` variants blocked/allowed; the deny-list; non-commit Bash allowed;
- usage hooks at 49/50/60% with and without the override;
- the status line with sample stdin JSON (including missing fields), colours and cache writes;
- the launcher: the argv built for `wf`, `wf "task"`, `wf --resume`, and delegation for old subcommands; refuses with an API key set;
- the banner renders 4–5 rows with ANSI and a 256-colour fallback.

## 5. Verbatim relay (user requirement: no model may rephrase or drop what another model or the user said)

1. **`@codex <text>` (a direct line):** a UserPromptSubmit hook sees the raw prompt. If it starts with `@codex` (case-insensitive, optional `:`):
   - it sends the exact text after the prefix to Codex via `codex_ask` logic (same team Codex session, read-only);
   - it blocks the prompt so Claude never receives it;
   - its `reason` is Codex's reply, **verbatim**, prefixed by one line `Codex (<model>):`.
   - The hook timeout is 600s.
   - Both the message and the reply are appended verbatim to `<project>/.workforce/team/codex-thread.md`, with timestamps.
2. **Claude stays informed from the originals:** on the next normal prompt, the UserPromptSubmit hook injects `additionalContext` = every thread entry since Claude's last turn, verbatim, headed "Direct user↔Codex exchange (verbatim, not summarised):".
3. **Raw artefacts:** every `codex_plan` / `codex_review` / `codex_ask` result is saved verbatim to `<project>/.workforce/team/{plan,review,ask}-<n>.md`, together with the exact prompt Claude sent. The tool result tells Claude the file path.
4. **TEAM.md rule:** when presenting Codex's plan or verdict, quote it verbatim (or point to the file), then add Claude's own view separately, clearly labelled.
5. **`wf codex`:** opens the interactive Codex CLI resuming the team's latest Codex session (`codex resume <session_id>`, the id read from `.workforce/team/session.json`).
