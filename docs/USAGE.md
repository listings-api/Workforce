# WorkForce Agent: usage guide

This guide covers every command, the config file, how to pick models, the usage limits, Laya, and what to do when something goes wrong. For the short version, see the [README](../README.md).

Contents: [`wf`: Claude Code + Codex](#wf-claude-code--codex) · [Commands](#commands) · [Workspaces](#workspaces) · [Console](#the-console) · [Config](#workforcetoml) · [Models and efforts](#models-and-efforts) · [Usage limits](#usage-limits) · [Laya](#laya) · [Questions](#questions-and-answers) · [Step and auto mode](#step-mode-and-auto-mode) · [Troubleshooting](#troubleshooting)

`wf` on its own starts **Claude Code with Codex on the team** (next section). The original unattended pipeline is still here: `wf run …`, `wf status`, `wf init` and the other commands below are the same as `workforce run …`, `workforce status` and so on (`wf <command>` hands them to the `workforce` program). Everything under [Commands](#commands) is that pipeline. Run those commands from inside the git repo the team should work on, or from a folder of repos (a [workspace](#workspaces)), or from any folder inside either.

## `wf`: Claude Code + Codex

```sh
cd ~/code/billing
wf                          # the normal Claude Code screen, with Codex added to the team
wf "fix the flaky test in billing"
wf --resume                 # or -c, --model, ... : every claude option works as usual
```

`wf` prints a small blue WF banner, then starts your real interactive `claude` with three things added:

- **The `workforce` plugin** (shipped inside the package as `workforce/team/plugin/`; `wf` builds a runnable copy in `~/.workforce/plugin/` on every start and loads it with `--plugin-dir`): a Codex tool server, the `fast-coder` sub-agent, eleven slash commands and two hooks.
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
| `~/.workforce/team.toml` | `claude` and `codex` binary paths (defaults `~/.local/bin/claude`, `/opt/homebrew/bin/codex`), Codex model, effort and mode, the sub-agent models and efforts, the 50/60 thresholds. Created on first run. |
| `<project>/.workforce/team/` | `session.json`, `codex-thread.md`, `plan-<n>.md` / `review-<n>.md` / `ask-<n>.md` (see above). |
| `~/.workforce/team.log` | One JSON line per Codex tool call and per hook or status-line error. |
| `<repo>/.git/wf-approvals.json` | The review verdicts by tree hash (in the git common dir, so worktrees share it; never committed). |

The plugin's hooks and Codex server run with the same Python as `wf` itself, wherever it was installed. `wf doctor` checks the whole setup. `wf --help` and `wf --version` are Claude's.

## Commands

These are the unattended pipeline commands. `wf <command>` and `workforce <command>` are identical.

### `wf init`

Writes `workforce.toml` (if there is none), creates `.workforce/`, and checks your setup. Each check prints ✓ or ✗:

- `claude` runs and is logged in with a claude.ai (Claude Team) account.
- `codex` runs and is logged in with ChatGPT.
- No API key variables are set.
- Each repo is on a branch (not a detached HEAD). A dirty working tree is fine.

Where it writes depends on where you run it:

- **Inside a git repo** (any subfolder of it): the file goes in that repo's top level, as a single-repo setup.
- **In a folder that holds git repos** (one or two levels down): it finds them, prints each with its current branch and detected checks, and writes `workforce.toml` with a `[workspace]` section listing them. It reminds you to edit `[workspace] repos` to trim the list.
- **Anywhere else**: exit 3, "not inside a git repo or a workspace".

An existing `workforce.toml` is kept as it is. If there is a `workforce.toml` in a folder above the one you are initialising, a new file would take over from it (the nearest file wins), so `wf init` refuses and asks for `--force`. Exit code 3 if any check fails.

```sh
wf init
wf init --check-models
wf init --force
```

`--check-models` also makes one tiny call for each model and effort in your config, to confirm your subscriptions can use them. It is the only part of `init` that uses model quota, and it is skipped if an earlier check failed.

### `wf run "<goal>"`

Starts a run and drives it until it finishes, pauses, or needs you. It prints events as they happen and asks for model choices and question answers right in the terminal.

```sh
wf run "add CSV export to the reports page"
wf run "migrate the settings page to the new form library" --mode step
```

- `--mode auto` (default) works through the tasks without asking. `--mode step` asks before starting each task. See [Step mode and auto mode](#step-mode-and-auto-mode).
- Only one `wf run` (or `wf resume`, or the console) can drive a repo at a time. A second one exits with code 4.
- Ctrl-C (or SIGTERM, for example from closing the terminal) stops it. Every agent, test and usage-probe process it started is stopped too (first politely, then forcibly after 3 seconds), and a run that was working is paused with the reason `interrupted`. State is saved, and `wf run --resume` (or `wf resume`) continues. If you were at a question prompt, the question stays open.

### `workforce "<goal>"`

With the long name, a goal with no command in front is the same as `workforce run "<goal>"`, and takes the same options.

```sh
workforce "add CSV export to the reports page"
workforce "migrate the settings page to the new form library" --mode step
```

It applies when the first word is not a command (`init`, `run`, `status`, `usage`, `questions`, `answer`, `models`, `pause`, `resume`, `log`) and does not start with `-`. So a goal that begins with one of those words, such as `status page redesign`, needs `workforce run "status page redesign"`. **`wf "<goal>"` is different:** it starts Claude Code with that text as your first message (see [`wf`: Claude Code + Codex](#wf-claude-code--codex)); use `wf run "<goal>"` for the unattended pipeline.

### `wf run --resume`

Continues the run saved in `.workforce/state.json` from where it stopped. It never repeats a step that is already recorded as done. Use it after Ctrl-C, after a crash, after you answered a question, or after a restart.

```sh
wf run --resume
```

You must give either a goal or `--resume`.

### `wf resume`

Clears a pause and continues the run. It does the same job as `wf run --resume`.

```sh
wf resume
```

If the pause was for usage and a window is still at or above the pause line, it refuses, prints the readings, and tells you when to try again, for example `still above 60%; try again after Sat 30 Sep 14:20`.

### `wf pause`

Stops the team from starting new work. A step that is already running finishes first. Works from a second terminal while another one is driving the run.

```sh
wf pause
```

### `wf status`

Shows the run, its tasks and any open questions. In a workspace the task table has a `repo` column, and an "Integration branches" table shows, per repo, the branch, its base and how many commits sit on top of it.

```sh
wf status
```

### `wf repos`

Lists the workspace's repos: name, base (the configured base, or the branch that is checked out, marked `(current)`), current branch, whether the working tree is dirty (information only, since a dirty tree never blocks a run) and the checks that would run.

```sh
wf repos
```

### `wf publish [--repo NAME] [--yes]`

Pushes the run's integration branches and opens pull requests. Nothing is pushed during a run; this is the only command that pushes.

```sh
wf publish
wf publish --repo app
wf publish --yes
```

For each repo whose integration branch has commits, it asks `Push wf/reviews-webhook-0929 (3 commits) in app and open a PR? [y/N]` (`--yes` skips the question). On yes it makes a temporary worktree of that branch and runs the Claude committer there, with your normal setup and rules, telling it to push the branch and open a PR the way you usually do (`gh` if your rules use it). It never force-pushes and never pushes or changes a base branch. It reports back whether the push worked and the PR url. The temporary worktree is removed afterwards, and WorkForce checks that the branch tip and the base branch did not move.

It prints a table: repo, branch, pushed, and the PR url or the error. Exit code 1 if any branch failed.

It refuses while a run is active (planning, running, paused, waiting for you, …): finish or fail the run first. It exits with code 4 if another WorkForce process holds the run lock, and it holds the lock itself while it works.

### `wf questions`

Lists the questions waiting for you. `--all` includes answered ones.

```sh
wf questions
wf questions --all
```

### `wf answer <id> "<text>"`

Answers a question. Your answer is written to `.workforce/decisions.md`, so every agent sees it from then on. It does not continue the run. Follow it with `wf run --resume`.

```sh
wf answer Q1 "Use the existing Postgres table; do not add a new one."
wf run --resume
```

### `wf models`

With no arguments, shows the model and effort for every role.

```sh
wf models
```

### `wf models set <role> <model> [effort]`

Changes one role in `workforce.toml`. If you leave out the effort, the role keeps its current one. The model and effort are checked against the role's agent (a Codex-only effort such as `ultra` is refused for a Claude role).

```sh
wf models set coder claude-opus-5-5 high
wf models set planner gpt-6-sol
```

Roles: `planner`, `plan_reviewer`, `coder`, `reviewer_claude`, `reviewer_codex`, `committer`, `usage_summary`. The change applies to steps that have not started. The `coder` role is the default offered at model-pick time. Each task's coder is fixed once you pick it for that task.

`wf models set` only edits the model and effort lines. Your comments and other settings stay untouched. It does not change a role's `agent`; do that by editing the file.

### `wf models task <task_id> <model> [effort]`

Changes the coder model (and effort) of one task in the current run. Use it for a task that has not started yet, or for any task while the run is paused. If you leave out the effort, the task keeps its own. It is refused for a merged task, and for a task that is in progress while the run is not paused (run `wf pause` first).

```sh
wf models task T2 claude-opus-5-5 high
wf models task T3 claude-sonnet-5-5
```

This is the way out when a coder model you picked turns out to be unavailable: `wf pause` (if it is not paused already), `wf models task <task> <model>`, then `wf resume`.

### `wf usage`

Shows the last saved usage readings for Claude and Codex (5-hour and weekly windows), how fresh each one is, when it resets, and a short plain-English summary. It reads `.workforce/usage.json` and `usage.md` only. It makes no calls and uses no quota. The readings are refreshed while a run or the console is active, so an old reading is shown as `last_known` with its time.

```sh
wf usage
```

### `wf log [task]`

Prints the event log with times. Give a task id to see only that task's events. In a workspace, the lines are tagged `[app/T1]` (repo and task), and you can filter with `T1` (every repo) or `app/T1`.

```sh
wf log
wf log T2
wf log app/T2
```

The unfiltered log includes run-level events, such as usage alerts, that the per-task view leaves out.

### `workforce` (no command)

Opens the interactive console (`wf` with no command starts Claude Code instead). See below. It needs a `workforce.toml` (run `wf init` first). In a folder of repos without one it says `This folder has 3 git repos (app, billing, …). Run wf init to set it up as a workspace.` and exits with code 3.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | OK, done, paused, or a question left open |
| 1 | The run failed, or another error |
| 2 | Wrong command line |
| 3 | Preflight or config problem (not logged in, API key set, bad `workforce.toml`, not inside a git repo or workspace, a folder of repos that has not been `wf init`ed) |
| 4 | Another WorkForce process holds the run lock |
| 130 | Interrupted with Ctrl-C (state is saved) |

## Workspaces

A workspace is a folder that holds several git repos, for example `~/code` with `app`, `billing` and `sdk`. `wf` works like the Claude Code or Codex CLI: start it in the workspace folder, or in any folder inside it, and it sees all the repos. A plain single repo is a workspace of one.

**Quick start from `~/code`:**

```sh
cd ~/code
wf init                 # writes workforce.toml with the repos it found; trim [workspace] repos if needed
wf repos                # check the list, bases and checks
wf run "add a reviews webhook to app, and use it from the sdk"
```

**How it finds the workspace.** It walks up from where you are to the nearest `workforce.toml`. That folder is the workspace root, and `.workforce/` lives there. The repo you started in (if any) is the *focus*: the planner is told "the user started wf inside `app/`", and the console footer shows it. Without a `workforce.toml`, inside a git repo you get a single-repo workspace; commands that need config then ask you to run `wf init`.

**One goal, several repos.** The planner sees every repo (name, base, top-level files, the start of its README, detected checks) and tags each task with the repo it belongs to. A task in one repo may depend on a task in another; it waits until that task has been merged. Tasks in different repos run in parallel (up to `[limits] parallel`), and each repo has its own merge queue. After every task is merged, Claude and Codex review how the pieces fit together across repos (API contracts, field names, config keys, call sites), and any fixes become new tasks.

**Branches, and your checkouts are never touched.**

- Each involved repo gets one integration branch, `<branch_prefix><slug>`, for example `wf/reviews-webhook-0929`. The slug comes from your goal (lowercase, at most 40 characters) plus `-MMDD`. It is created from the repo's base with `git branch`, so your checkout does not move.
- The base is `[repos.<name>] base`, or the branch the repo has checked out when the run starts. If the integration branch name already exists, the run stops before doing anything.
- Each task works on `<branch_prefix><slug>-T1` in a worktree under `~/.workforce/worktrees/`, and is fast-forward merged into the integration branch after both reviewers approve. Task branches and worktrees are removed afterwards. The integration worktrees are removed when the run ends, and the integration branches stay.
- After the model pick, you see the branch for each involved repo and can rename it: `branch for app [wf/reviews-webhook-0929]:`. Enter keeps it. A name that git would reject is refused and asked again.
- WorkForce never checks out, resets, merges into or edits your working copy of any repo, and never commits to, merges into or pushes a base branch (`main`, `master`, …). Your checkout may be dirty and on any branch.
- Agents can only write inside their own task worktree (and `/tmp`). Other repos can be read for context.
- When the run ends you get, per repo, `branch wf/reviews-webhook-0929: 3 commits on top of main (1a2b3c4d)`, and the hint `Run wf publish to push and open PRs.`

**Publishing.** Run `wf publish` (or `/publish` in the console). See [`wf publish`](#wf-publish---repo-name---yes).

**Per-repo config** (all optional) goes in `workforce.toml`, see [the config file](#workforcetoml).

## The console

`workforce` with no arguments opens a prompt that looks like Claude Code. The header shows your default models and whether your subscriptions and "no API keys" checks passed, and the repo path (or, in a workspace, `Workspace ~/code · 3 repos`). The footer shows the model and repo (plus the focus repo in a workspace), the current usage in each window, and the mode (`auto` or `step`).

What you type:

| Input | What it does |
|---|---|
| plain text, when idle | Starts a run with that text as the goal. |
| plain text, while a run is active | Saved as a note in `decisions.md`. Agents pick it up at their next step. It does not interrupt anything. |
| `@claude <question>` | Asks Claude a side question in a fresh, read-only session. The answer prints above the prompt and does not disturb the team. Uses your `plan_reviewer` model. In a workspace it runs at the workspace root and can read every repo. |
| `@codex <question>` | Same, with Codex and your `planner` model. |
| `/run <goal>` | Starts a run. Same as typing a goal when idle. |
| `/status` | The run, its tasks and open questions. |
| `/answer <Q> <text>` | Answers a question. `/answer Q3 use the existing table` |
| `/pause` | Stops new work. Running steps finish. |
| `/resume` | Continues a paused run. |
| `/models` | Every role's agent, model and effort, plus each task's chosen model. |
| `/effort <role\|task> <level>` | Changes an effort level. See below. |
| `/log [task]` | The last 30 events, optionally for one task (`T1`, or `app/T1` in a workspace). |
| `/repos` | The workspace's repos: base, branch, dirty flag and checks. |
| `/publish [repo]` | Lists the integration branches that have commits, asks `y/N`, then pushes them and opens PRs (see `wf publish`). It runs in the background and prints the PR urls. Refused while a run is active. |
| `/computer on\|off <task>` | Turns Codex computer use on or off for a task, overriding the `[browser] computer_use` default it started with. Takes effect from its next coder step. |
| `/help` | Lists these. |
| `/quit` | Leaves. If the team is working it asks: `p` pause, `l` leave, `c` cancel. |

Examples:

```
❯ add CSV export to the reports page
❯ always use pnpm, never npm
❯ @codex where is the auth middleware defined?
❯ /effort coder xhigh
❯ /effort T2 max
❯ /answer Q1 yes, keep the old endpoint
❯ /computer on T3
```

Details:

- **`/effort <role> <level>`** rewrites that role's effort line in `workforce.toml` and applies to steps that have not started yet. **`/effort <task> <level>`** changes one task's effort. It works while you are picking models, or while the task has not started. After it starts, its effort cannot change. Levels: Claude `low medium high xhigh max`; Codex those plus `ultra`. A level that does not fit the role's agent is refused.
- **`/quit` and Ctrl-D.** Both leave. With a team working, the console asks what to do. Both `p` (pause) and `l` (leave) pause the run after the current step. There is no background service, so nothing keeps working after the console closes. Press Ctrl-D twice to quit right away. A step still running may be cut off; state is saved. Ctrl-C only clears the line.
- **Tab** completes commands, `@claude`/`@codex`, question ids and task ids, and efforts for `/effort`.
- **Shift+Tab** switches between `auto` and `step`.
- **Model pick.** When the plan is agreed, the console lists the tasks (as `app/T2` in a workspace) and asks for each one: `coder app/T2 [claude-sonnet-5-5 · high] ❯`. Enter keeps the default. Type `claude-opus-5-5` to change the model, or `claude-opus-5-5 max` to change both. Then it asks for each involved repo's branch: `branch for app [wf/reviews-webhook-0929] ❯`. Enter keeps the name; type another to rename it.
- **History** is kept in `~/.workforce_history`.
- If a run was left unfinished, the console tells you when it starts and says to type `/resume`.
- The console runs tasks in parallel according to `[limits] parallel`.

## `workforce.toml`

`wf init` writes this file with every key. Nothing has a hidden default: a missing key is an error that names the key. Edit it by hand, or use `wf models set` and `/effort` for models and efforts.

```toml
# Where the two programs are. "~" is expanded. claude is often not on PATH, so give the full path.
[bin]
claude = "~/.local/bin/claude"
codex = "/opt/homebrew/bin/codex"

# Each role is one job on the team. agent = "claude" or "codex".
# effort must be valid for that agent:
#   claude: low medium high xhigh max
#   codex:  low medium high xhigh max ultra
# model must be one your subscription can use. See "Models and efforts".

[roles.planner]          # writes the plan (Codex, read-only)
agent = "codex"
model = "gpt-6-sol"
effort = "high"

[roles.plan_reviewer]    # checks the plan; also answers @claude side questions
agent = "claude"
model = "claude-opus-5-5"
effort = "high"

[roles.coder]            # the DEFAULT coder; you confirm or change it for every task
agent = "claude"
model = "claude-sonnet-5-5"
effort = "high"

[roles.reviewer_claude]  # Claude's independent code review
agent = "claude"
model = "claude-opus-5-5"
effort = "high"

[roles.reviewer_codex]   # Codex's independent code review
agent = "codex"
model = "gpt-6-sol"
effort = "high"

[roles.committer]        # makes the commit; runs with YOUR global Claude and git setup
agent = "claude"
model = "claude-sonnet-5-5"
effort = "high"

[roles.usage_summary]    # writes the short usage summary, and pings Claude for a fresh reading
agent = "claude"
model = "claude-haiku-4-5-20251001"
effort = "low"

[limits]
debate_rounds = 3        # plan back-and-forth rounds before it becomes a question for you
review_rounds = 3        # fix-and-review rounds per task (also caps test-fix attempts and rebases)
alert_percent = 50       # warn when any usage window reaches this
pause_percent = 60       # pause the run when any usage window reaches this (must be above alert_percent)
poll_minutes = 10        # how often usage is re-read; a reading older than this shows as "last known"
parallel = 2             # tasks worked on at the same time; 1 = strictly one at a time

[checks]                 # shell commands run in a task's working copy. Empty string = auto-detect. The default for every repo.
test = ""                # auto-detect tries: pytest, npm test, go test ./..., cargo test, make test
lint = ""
build = ""

[browser]
claude = true            # let Claude's coder and reviewer runs use the Chrome integration
codex = true             # let Codex use its browser tool
computer_use = false     # the default for the computer-use flag of each new task; override per task with /computer on|off <task>

[decider]                # Laya, the small local decision model
backend = "laya"         # "laya" or "off". "off" means every uncertain call goes to you.
url = "http://127.0.0.1:11435"   # where Ollaya is listening
confidence = 0.85        # used only when [decider.thresholds] is absent

[decider.thresholds]     # per-question minimum confidence. Questions not listed here are never asked.
task_size = 0.75         # hint text on the model-pick screen
agent_progress = 0.45    # flags a stuck agent in the UI, takes no action
debate_converged = 0.63  # lets a plan debate end early

# Optional: pick a different Laya model for one question.
# [decider.models]
# task_size = "laya:typed-decisions"

# Workspace mode. Absent = a single repo (the folder holding this file is the repo).
# `wf init` writes this section, from the repos it finds in the folder.
# [workspace]
# repos = ["app", "billing", "sdk"]   # paths relative to this folder; each must be a git repo
# branch_prefix = "wf/"                     # integration branches are <prefix><slug>; default wf/

# Optional, per repo (also allowed in single-repo mode).
# [repos.app]
# base = "master"        # default: the branch the repo has checked out when the run starts
# [repos.app.checks]     # overrides [checks] key by key; keys left out inherit from [checks]
# test = "bundle exec rspec"
```

Rules the loader enforces: every listed workspace repo exists and is a git repo; the `[workspace]` and `[repos.*]` tables reject unknown keys (a typo such as `bse` names the key); every role has `agent`, `model` and `effort`; effort matches the agent; `alert_percent` is below `pause_percent`; the numbers in `[limits]` are whole numbers of at least 1; threshold keys are one of `risk_gate`, `agent_progress`, `debate_converged`, `verdict_consistent`, `needs_human`, `task_size`, with values above 0 and up to 1.

Changes to the file are picked up when a run resumes (`wf resume`, `wf run --resume`, `/resume`). A file with an error leaves the run paused and prints what is wrong.

## Models and efforts

You choose. WorkForce never picks a model for you and never swaps one for another.

**Available on this setup** (checked 2026-09-29 against the installed CLIs):

| Agent | Model IDs |
|---|---|
| Claude | `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-4-5-20251001` |
| Codex | `gpt-6-astra`, `gpt-6-sol`, `gpt-reserve`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` |

The Codex roles default to `gpt-6-sol`. To use the top model for a run, switch per role:

```
wf models set planner gpt-6-astra high
wf models set reviewer_codex gpt-6-astra high
```

| Agent | Efforts, lowest to highest |
|---|---|
| Claude | `low`, `medium`, `high`, `xhigh`, `max` |
| Codex | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |

What your Claude Team and ChatGPT Business plans can use can change. `wf init --check-models` tests every model in your config with a real call, so you find out before a run.

**How to choose per task.** The coder is the only model you pick per task. The other roles come from `workforce.toml`. Higher effort costs more of your usage and takes longer.

- Small, clear tasks (rename, add a field, a simple endpoint): `claude-sonnet-5-5` at `medium` or `high`.
- Tricky logic, security-sensitive code, or work across many files: `claude-opus-5-5` at `high` or `xhigh`.
- `max` is for problems that have already beaten a lower setting.
- Keep an eye on `wf usage`. Opus at high effort uses the windows faster than Sonnet.

The model-pick screen shows a size hint from Laya when it is confident, such as `(Laya: small)`. It is only a hint. Laya does not choose.

**Where you set them:**

- At the model-pick prompt, per task: press Enter for the default, type a model, or a model and an effort.
- In the console, `/effort <task> <level>` before the task starts.
- Later, for one task: `wf models task <task_id> <model> [effort]` (task not started, or run paused).
- Computer use is chosen the same way. Each task starts with the `[browser] computer_use` value from `workforce.toml` (set when you pick its coder), and `/computer on|off <task>` in the console overrides it for that task.
- For a role's default, `wf models set <role> <model> [effort]` or `/effort <role> <level>`.

**If a model is not available to you**, the run pauses with `model_unavailable: <model>`. See [Troubleshooting](#a-model-is-unavailable).

**Which agent does what.** Codex plans (read-only) and reviews. Claude checks the plan, writes the code, reviews, and commits. The `agent` for `coder` can be `codex` in the file, but the commit is always made by Claude.

## Usage limits

Your Claude and ChatGPT subscriptions each have a 5-hour and a weekly usage window. WorkForce reads them and protects you from burning through them.

- **Where readings come from.** Claude reports its windows during every Claude run. Codex is asked directly, before and after every Codex run and every `poll_minutes` while a run is active. If Claude has been quiet for `poll_minutes`, a tiny Haiku call (`Reply OK`) refreshes the reading. A reading older than `poll_minutes` is shown as `last_known` with its time, and a source it has never read is `unknown`. It never guesses a number.
- **50% (`alert_percent`):** any window at or above this prints a warning once per window per reset. Work continues.
- **60% (`pause_percent`):** any window at or above this pauses the run. The step already running finishes. Nothing new starts. The run status becomes `paused` with the window named, such as `usage: claude five_hour 61% ≥ 60%`. This is checked before every agent launch, after every run, and on each poll. Because it acts when it sees the number, the real usage can end up a little over 60%.
- **5-hour pauses resume by themselves.** If every window that caused the pause is a 5-hour window, the run resumes 60 seconds after the latest reset, but only if fresh readings show every window below 60%. In `wf run`, the command stays in the foreground and prints something like `⏸ Paused: … Resumes automatically at 14:21 (in 1h41m) if all limits are under 60%. Ctrl-C to stop waiting (the run stays paused).` Ctrl-C leaves the run paused, and you can continue later with `wf resume`. In the console, the run restarts by itself as long as the console is open. If usage is still high at that time, it keeps checking every `poll_minutes`.
- **Weekly pauses never resume by themselves.** They wait for you to run `wf resume` (or `/resume`) after the weekly window resets. If a weekly window joins a 5-hour pause, the pause becomes manual.
- **`wf resume` while still over the line** refuses and prints the readings and when to try again.
- **`wf usage`** shows the saved readings. **`usage.md`** in `.workforce/` has the plain-English summary, rewritten at most every 10 minutes when the numbers change. The summary is for reading only; it never decides anything.
- To change the numbers, edit `alert_percent`, `pause_percent` and `poll_minutes` under `[limits]`. Alert must stay below pause.

## Laya

Laya is a small open-weights decision model (about 850 MB) that runs on your Mac through a local server called Ollaya. It answers quick typed questions and returns how confident it is. It is not a coding model and it does not write anything.

**What Laya decides.** Only small typed questions. Each one has a minimum confidence in `[decider.thresholds]`. Below it, Laya counts as "unsure" and a safe fallback applies.

**What Laya never decides.** It never approves code. It never picks or changes models. It never overrides you. Every call it makes is logged as an event.

### The calibration table, in short

WorkForce measured Laya against 12 labelled examples per question (see [laya.md](laya.md)). With only 12 each, treat this as a rough guide.

| Question | Used for | Result | What happens |
|---|---|---|---|
| `task_size` | Size hint (small/big) on the model-pick screen | 11 of 12 right | On. Hint text only. |
| `agent_progress` | Is an agent progressing, stuck or off task? | Useful only when confident | On. Flags it to you in the UI. Stops nothing. |
| `debate_converged` | Do the plan and the objections now agree? | Useful only when confident | On. Can end a plan debate early. The round cap still applies. |
| `risk_gate` | Is a tool call risky? | Not reliable | Off. Laya never blocks a call. |
| `verdict_consistent` | Does a review's text match its verdict? | Not reliable | Off. A built-in rule catches "APPROVE with a blocking bug". |
| `needs_human` | Can the agents settle this or does it need you? | Not reliable | Off. It always asks you. |

A question that is off has no entry in `[decider.thresholds]`, so it is never sent to Laya and its fallback applies: ask you.

**Safety rules run without Laya.** A fixed deny-list always applies to every Claude and Codex tool call: no `rm -rf` outside the working copy, no `git push --force`, no `git reset --hard` on main, no `--no-gpg-sign` or `commit.gpgsign=false`, and no reading `~/.ssh`, Codex's `auth.json` or Claude credentials. Laya cannot relax these. Blocked calls print `⛔ … blocked: …`. This deny-list is a strong guard, not a sandbox; it inspects commands as text.

### Do I need it?

No. With `backend = "off"`, or with Ollaya not running, the tool works the same way. You just get no size hints, no stuck-agent flags, and no early end to plan debates, and more questions come to you.

### Install and start Ollaya

Details, and what was verified, are in [laya.md](laya.md). The short version:

```sh
curl -fsSL https://ollaya.dev/install.sh -o install.sh    # read it before you run it
OLLAYA_INSTALL_DIR=$HOME/.local sh install.sh
~/.local/bin/ollaya pull laya:en                          # about 850 MB
```

Start it (the port must match `url` in `workforce.toml`):

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

WorkForce does not start or stop Ollaya for you. Start it before `wf` if you want Laya. If it is down when a call happens, that call falls back to "unsure" and the run goes on.

### Recalibrating

Re-run the probes when you change a question's wording, its thresholds, or the model, or when you have new real examples. Ollaya must be running.

```sh
.venv/bin/python -m workforce.decider.calibrate                       # all questions, laya:en
.venv/bin/python -m workforce.decider.calibrate --key task_size       # one question
.venv/bin/python -m workforce.decider.calibrate --model laya:typed-decisions
.venv/bin/python -m workforce.decider.calibrate --details             # every probe with its answer
.venv/bin/python -m workforce.decider.calibrate --json                # machine-readable
```

Without `--url`, the server address comes from `./workforce.toml`. The `agent-team` folder has none, so from there add `--url http://127.0.0.1:11435` to each command, or run it with the full path to `.venv/bin/python` from inside a repo that has one. Other options: `--url` (the server) and `--probes` (a folder of `<key>.jsonl` probe files; default `tests/laya_probes`). For each question it prints accuracy and the lowest threshold that keeps at least 90% of the kept answers right. A key marked `unreliable` should stay out of `[decider.thresholds]`. Put the passing threshold, with some margin, in the file for keys you want on.

Only 12 probes per question is thin. A threshold picked on the same 12 examples is optimistic. Add probes from real runs before you trust a question with more.

## Questions and answers

When the agents cannot settle something, they create a **question** and the run stops in `awaiting_user`. That happens when:

- the plan debate runs out of rounds with a disagreement,
- a reviewer says a task is `BLOCKED`,
- the review rounds run out with the reviewers still disagreeing,
- checks still fail after the allowed fix attempts,
- an agent step fails for a non-model reason (crash, timeout, login, quota, bad output) three times in a row: it is retried twice with a wait first,
- main's checks fail after a merge, or
- in step mode, before each task starts.

Each question shows both sides' positions. To answer:

- In `wf run`, the terminal asks right away: `Q1 answer (Ctrl-C to leave it open):`. Ctrl-C leaves it open and exits.
- From any terminal: `wf answer Q1 "your answer"`. This works even while another terminal is driving.
- In the console: `/answer Q1 your answer`.

List them with `wf questions` or `/status`. An answer is saved in `.workforce/decisions.md` as `User answer to Q1 (…): <your text>`, and every later agent prompt includes it. After answering from the command line, continue with `wf run --resume`. In the console, answering starts the team again by itself if the run was waiting only for that.

You can also write standing rules straight into `decisions.md`, or type them as notes in the console while a run is active. Agents read the whole file before every step.

## Step mode and auto mode

- **Auto** (the default): after you pick models, the team runs all tasks without asking.
- **Step:** before each task starts, you get a question: `Start T2: <title>? Answer yes to start, or no to pause.` Answer `yes`, `y`, `ok`, `go`, `start`, `continue` or `proceed` (or just press Enter) to start it. Answer `no`, `stop`, `pause`, `wait` or `hold` to pause the run. Anything else is saved as a rule in `decisions.md` and the task starts.

Set it with `wf run "goal" --mode step`, or press Shift+Tab in the console. In the console, Shift+Tab changes the running run's mode straight away, or the mode the next run starts with if none is active. In step mode each question stops the whole run until answered, so parallel tasks drain and wait.

## Troubleshooting

Start with `wf init`. It checks most of the problems below at once.

### Not logged in

`wf init` says `claude authMethod is … expected "claude.ai"` or `codex is not logged in with ChatGPT`. Log in with the programs themselves: run `claude` and use `/login` with your Claude Team account, and `codex login` with your ChatGPT Business account. Check with `claude auth status` (look for `"authMethod": "claude.ai"`) and `codex login status` (`Logged in using ChatGPT`). Then run `wf init` again.

If it says a program "failed `--version`" or "not found", the path in `[bin]` in `workforce.toml` is wrong. Find it with `which claude` or `ls ~/.local/bin/claude`.

### An API key is set

`ANTHROPIC_API_KEY is set in the environment; unset it`. WorkForce refuses to start if `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `OPENAI_API_KEY` is set. Unset them in this shell (`unset ANTHROPIC_API_KEY`), and remove them from `~/.zshrc` or `~/.zprofile` if they are exported there. Then run `wf init` again.

### A model is unavailable

The run pauses with `model_unavailable: <model>`. Nothing was swapped in. Choose a model your account can use, then continue:

```sh
wf models set <role> <model> [effort]
wf resume
```

This works for the planner, plan reviewer, both reviewers and the committer, because they are read from `workforce.toml` when the run resumes.

A task's coder is different. The model you picked for a task is stored with that task, so change it with `wf models task`:

```sh
wf models task <task_id> <model> [effort]
wf resume
```

The run is already paused at that point, so this is allowed. WorkForce never swaps a model in by itself. You can also avoid the problem by running `wf init --check-models` first.

### The commit failed

The team shows `👆 committing… (confirm if your git asks)` and waits. If the commit fails, the step is retried once. If it fails a second time it becomes a question: `The commit for T2 failed twice … Was a confirmation prompt cancelled?`. Answer it when you are ready (`wf answer Q4 "ready, try again"`), then `wf run --resume`. Nothing is lost: the finished work sits in the task's working copy under `~/.workforce/worktrees/`, and the reviews are kept.

If the commit fails for another reason, read the error in `wf log <task>`. A commit hook in your global setup may be rejecting it.

### An agent looks stuck

Agents can take several minutes on a hard step. Signs to look for:

- `wf log <task>` (or `/log <task>`) shows no new events for a long time.
- With Laya on, you may see `⚠ [T2] Laya thinks the coder may be stuck`. This is only a flag. Nothing is stopped.

What to do: wait a bit more, then press Ctrl-C (or `/pause`). State is saved and you lose only the step in progress. Run `wf run --resume`. It restarts that step, and the coder continues in its existing working copy. A step that runs longer than an hour is stopped automatically and retried twice before it becomes a question. The raw output of each step is in `.workforce/runs/<run>/<task>/`, if you want to see what it was doing.

### Laya is down

Nothing breaks. Calls to Laya fall back to "unsure", which means "ask the user" or "no hint". You may see fewer size hints and more questions. To bring it back, start Ollaya as described [above](#install-and-start-ollaya) and check `curl http://127.0.0.1:11435/v1/models`. If you do not want Laya, set `backend = "off"` under `[decider]`.

If the first call after a quiet spell falls back, the model had unloaded. Start Ollaya with `OLLAYA_KEEP_ALIVE=-1`.

### A lock is held

`another workforce process holds .workforce/state.lock; only one run can be active` (exit code 4). Only one `wf run`, `wf resume` or console can drive a repo at a time. Look for the other one: another terminal tab, or a console you left open. Stop it with Ctrl-C or `/quit`. If you cannot find one, check what has the file open:

```sh
lsof .workforce/state.lock
```

The lock is released the moment its process exits, even after a crash, so there is nothing stale to delete. The lock file itself can stay. `wf pause`, `wf answer`, `wf status`, `wf usage` and `wf log` do not need the lock and work while another process is driving.

### Other messages

- **`not inside a git repo or a workspace`**: run `wf` from inside a repo, or from a folder of repos (then run `wf init` there once).
- **`This folder has N git repos (…). Run wf init to set it up as a workspace.`**: exactly what it says. Run `wf init` in that folder.
- **`wf init` refuses because a `workforce.toml` exists above**: you are inside a workspace that is already set up, and a new file here would shadow it. Run `wf` from here as it is, or use `wf init --force` if you really want this folder to be its own workspace.
- **`integration branch … already exists`**: a branch with that name is already in one of the repos. Rename it at the branch prompt, or delete the old branch if you no longer need it. The name comes from the goal and the date, so the same goal on the same day gives the same name.
- **`… is on a detached HEAD`**: check out a branch in that repo, or set `[repos.<name>] base` so WorkForce knows where to start from. A dirty working tree is never a problem.
- **`detached HEAD`**: check out a branch.
- **`workforce.toml not found`**: run `wf init`.
- **`workforce.toml … is missing`**: the file lacks a key, and the message names it. Copy it from a fresh `wf init` in another folder.
- **Tests fail in a task and the team keeps retrying**: set `[checks] test` to the right command. Auto-detect only knows a few common setups.
- **The console footer shows `?` or `~`**: `?` means no reading yet. `~` means an old reading (last known).
