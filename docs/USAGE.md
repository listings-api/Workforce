# WorkForce: usage guide

This guide covers `wf` in detail: how the team works, every slash command, talking to Codex word for word, the settings, the usage limits and Laya. For install and a quick start, see the [README](../README.md).

WorkForce only runs as a chat. `wf -p` / `wf --print`, and the unattended `wf run` pipeline of earlier versions, were removed.

## `wf`: Claude Code + Codex

```sh
cd ~/code/billing
wf                          # the normal Claude Code screen, with Codex added to the team
wf "fix the flaky test in billing"
wf --resume                 # or -c, --model, ...: every claude option works, except -p (WorkForce only runs as a chat)
```

`wf` prints a small blue WF banner, then starts your real interactive `claude` with three things added:

- **The `workforce` plugin** (shipped inside the package as `workforce/team/plugin/`; `wf` builds a runnable copy in `~/.workforce/plugin/` on every start and loads it with `--plugin-dir`): a Codex tool server, the `fast-coder` sub-agent, twelve slash commands and two hooks.
- **A status line**: `WF │ Claude 5h 23% · wk 44% │ Codex wk 3% │ codex gpt-6-sol·high`. Yellow from 50%, red from 60%. `?` means Claude Code did not report that number; `~` after a Codex number means the last reading is more than 2 minutes old (a background refresh is on its way).
- **Team rules** appended to Claude's system prompt (`TEAM.md` in the plugin).

Everything else is Claude Code as you know it: the same input box, permissions, `/model`, shift+tab, sub-agents and git. Claude Code's own header can't be changed, so the WF banner sits above it. `wf` uses your Claude Team login. If `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `OPENAI_API_KEY` is set, it refuses to start (exit 3).

### How the team works

1. For a non-trivial task Claude first asks Codex for a plan (`codex_plan`; Codex reads the repo, read-only) and briefly debates any disagreement. After 2 rounds it asks you.
2. Claude writes the code. Small mechanical sub-tasks go to the `fast-coder` sub-agent (Sonnet 5.5).
3. Before a commit, two reviews of the exact changes, both started by the tool server: `claude_review` runs a fresh headless Claude (`reviewer_model`, default Opus 5.5) in safe mode with only Read, Glob and Grep, no MCP servers and no WorkForce plugin, and `codex_review` runs Codex read-only. Every read-only Codex run (plans, reviews, asks, `@codex`) also switches off Codex plugins and each MCP server in `~/.codex/config.toml`, because the read-only sandbox does not stop an MCP tool such as browser or computer control from acting. The server records each verdict (APPROVE, REQUEST_CHANGES or BLOCKED) for the tree hash, signed with a key in `~/.workforce/team.key`. No tool takes a verdict, an unsigned or altered entry is ignored, and a hook denies edits to the approvals file and the key. Any edit voids both approvals.
   **The commit hook** blocks `git commit`, `merge --continue`, `am` and staged merges/cherry-picks/reverts in any form (`git -C x commit`, `a && git commit`, `env … git commit`, `bash -c '…'`, aliases, `GIT_DIR=…`) until both approve the tree that would be committed. That is the staged tree for a plain `git commit`, and the whole working state for `git add -A && git commit …` or `git commit -a`. `git commit <paths>` is denied: stage, then commit. A merge, cherry-pick or revert of existing commits with nothing staged (`git merge origin/master`, `git pull`) needs no review; if it stops on conflicts, the resolution commit is gated. `git reset <commit>`, `git branch -f`, `git checkout -B` and `git switch -C` may point a branch at a commit that is already on the current branch or on a remote branch; any other commit needs both approvals of its files first (review it with `git checkout --detach <sha>` and /review). `git reset --hard` on or to `main`/`master` stays blocked. Commands it can't parse are denied. `rebase`, `commit-tree`, `update-ref`, `filter-branch`, `replace`, creating a `git stash`, and `gh api` writes to refs or commits are refused. In a folder of several repos, pass `repo` to the review tools; approvals are per repo.
4. Force-push is always denied. Claude follows your git instructions; it does not push unless you ask.
5. Approvals record the commit the reviewers compared against; only a review against the current HEAD allows a commit. A command that changes files and then commits in one go is refused, and inside `wf` git runs a WorkForce pre-commit check that compares the exact committed files with the approvals before handing over to your repository's own hooks.
5. Laya gives yes/no hints only. You pick the models.

### Slash commands

| Command | What it does |
|---|---|
| `/plan <task>` | Codex researches and plans. |
| `/plan-loop <task> [rounds=<n>]` | Opt-in heavy workflow, only when you type it: recon with an assumptions ledger, an interview on the decisions that matter, a written plan, Codex reviewing that plan until APPROVED (default cap 5 rounds, then you decide), then the build and both code reviews. The plan is `.workforce/team/plans/<slug>/PLAN.md`, and every review round is appended verbatim to `REVIEW-LOG.md` beside it. An approval covers only the exact plan file it reviewed. |
| `/review` | Runs `claude_review` and `codex_review` on the current changes and shows who approved and whether a commit is allowed. |
| `/codex-effort [effort]` | A menu of the efforts the current Codex model supports, or give one directly. |
| `/codex-model [model] [effort]` | A menu of the models Codex lists for your account (read live, cached for an hour) and their efforts; pick with the arrow keys. Or give them directly: `/codex-model gpt-6-astra xhigh`. A model Codex doesn't list, or an effort it doesn't support, is refused. Saved in `~/.workforce/team.toml`. Defaults: `gpt-6-sol`, `high`. |
| `/usage` | The Claude and Codex usage windows with alert/stop state. |
| `/wf-continue` | Overrides the 60% stop for the window(s) that are over it, until they reset. |
| `/wf-review-again` | After a reviewer rejected the changes 3 times in a row, the review tools stop and Claude asks you what to do. This allows more rounds. Only you can type it. |
| `/codex-mode [read-only\|write]` | Shows or sets Codex's mode (below). Default `read-only`. |
| `/claude-model`, `/claude-effort` | A menu for the team's other Claudes: the reviewer behind `claude_review` (applies at the next review) and the `fast-coder` sub-agent (applies the next time `wf` starts). Defaults: `claude-opus-5-5` / `claude-sonnet-5-5`, both `high`. Or give them directly: `/claude-model reviewer claude-opus-5-5 high`. This chat's own Claude is `/model`. |
| `/wf-settings` | One view of every setting, current usage and the Laya status, with the command that changes each. Shown exactly as the tool returns it. |

### Talking to Codex word for word

No model rephrases or drops what you or Codex said.

- **`@codex <text>`** (case-insensitive, optional `:`, at the start of the line): the exact text after the prefix is sent to Codex in the team's Codex session (read-only unless `/codex-mode write`). The prompt is blocked so Claude never receives it, and Codex's reply is shown to you unchanged under a `Codex (<model>):` line. The wait can be long: the hook allows 600 seconds. A reply over 200,000 characters is cut with a marked notice (the whole reply is in the saved file).
- **`.workforce/team/codex-thread.md`** in your project keeps every such message and reply, verbatim, with timestamps. On your next normal prompt, Claude is given the entries it has not seen yet, verbatim, under "Direct user↔Codex exchange (verbatim, not summarised):", once. `/wf-continue` and blocked prompts don't use them up.
- **Raw artefacts:** every `codex_plan`, `codex_review` and `codex_ask` result is saved to `.workforce/team/{plan,review,ask}-<n>.md` together with what Claude sent and the exact prompt given to Codex; the tool result names the file. Claude's rules say to quote Codex verbatim (or point at the file) and keep its own view separate.
- **`wf codex [args]`** starts the interactive Codex CLI resuming the team's latest Codex session (`codex resume <id>`; the id is in `.workforce/team/session.json`, written whenever Codex answers a plan, an ask or an `@codex` line). It looks in the current folder and above. To send Claude a task that starts with the word "codex", quote it: `wf "codex is slow, fix it"`.

The `.workforce/` folder is added to the repo's local `.git/info/exclude`, so these files never show up in `git status` or change the reviewed tree. Logs (`~/.workforce/team.log`) redact secrets; nothing above does.

### Codex mode, sub-agent models and `/wf-settings`

- **`/codex-mode write`** lets `codex_ask` and the `@codex` line run Codex with `-s workspace-write` in your project, so it can edit files when you ask it to. `codex_plan` and `codex_review` stay read-only. The commit gate is unchanged: both reviews of the final files are still required, and an edit voids them. Saved as `codex_mode` in `~/.workforce/team.toml`.
- **`/claude-model` and `/claude-effort`** save `reviewer_model`, `reviewer_effort`, `fast_coder_model`, `fast_coder_effort`. The reviewer settings are read by the server on each `claude_review`. A plugin agent file has a fixed `model:`, so `wf` writes `agents/fast-coder.md` in `~/.workforce/plugin/` from the packaged template each time it starts. Efforts: `low medium high xhigh max`.
- **`/wf-settings`** calls the `settings` tool and prints its text as is.

### Usage alert and stop

The status line saves Claude's 5-hour and weekly percentages to `~/.workforce/usage.json`; the Codex server saves Codex's to `~/.workforce/codex_usage.json`. At 50% on any window Claude is told to mention it (no blocking). At 60% every prompt and every tool call is blocked with the window and its reset time, until it resets or you type `/wf-continue`. A new window needs a new `/wf-continue`. If the usage files are missing or unreadable the hooks let everything through. The thresholds are `alert_percent` and `stop_percent` in `~/.workforce/team.toml`.

### Files and settings

| Path | What it is |
|---|---|
| `~/.workforce/team.toml` | `claude` and `codex` binary paths (found automatically on first run), Codex model, effort and mode, the sub-agent models and efforts, the 50/60 thresholds. |
| `<project>/.workforce/team/` | `session.json`, `codex-thread.md`, `plan-<n>.md` / `review-<n>.md` / `ask-<n>.md` (see above). |
| `~/.workforce/team.log` | One JSON line per Codex tool call and per hook or status-line error. |
| `<repo>/.git/wf-approvals.json` | The review verdicts by tree hash (in the git common dir, so worktrees share it; never committed). |

The plugin's hooks and Codex server run with the same Python as `wf` itself, wherever it was installed. `wf doctor` checks the whole setup. `wf --help` and `wf --version` are Claude's.

## Laya (optional)

Laya is a small open-weights decision model (about 850 MB) that runs on your machine through a local server called Ollaya. The team uses it only for quick yes/no hints (the `laya` tool), never for approvals or model choice. Everything works without it; `wf doctor` shows whether it is running.

```sh
curl -fsSL https://ollaya.dev/install.sh -o install.sh    # read it before you run it
OLLAYA_INSTALL_DIR=$HOME/.local sh install.sh
~/.local/bin/ollaya pull laya:en                          # about 850 MB
```

Start it:

```sh
OLLAYA_HOST=127.0.0.1:11435 OLLAYA_KEEP_ALIVE=-1 nohup ~/.local/bin/ollaya serve > /dev/null 2>&1 &
```

`OLLAYA_KEEP_ALIVE=-1` keeps the model loaded. Without it the model unloads after 5 minutes idle, and the first request after that can be slow enough to time out (2 seconds) and fall back to "unsure" once.

Check it is up:

```sh
curl http://127.0.0.1:11435/            # prints: Ollaya is running
curl http://127.0.0.1:11435/v1/models   # should list laya:en
```

Stop it:

```sh
pkill -f "ollaya serve"        # stop the server
~/.local/bin/ollaya stop laya:en   # or just unload the model
```

WorkForce does not start or stop Ollaya for you. Set `WF_LAYA_URL` to use another address.

## Troubleshooting

Run `wf doctor` first: it checks every requirement and prints the fix for anything missing. The README's troubleshooting table covers the common messages.
