<p align="center">
  <img src="docs/assets/wf-logo.svg" alt="WF" width="260">
</p>

<h1 align="center">WorkForce</h1>

<p align="center"><b>Claude Code with Codex on the team</b></p>

**WorkForce runs your normal Claude Code with Codex as a teammate. Codex plans and reviews, Claude writes the code, and both must approve before every commit. It all runs on your own Claude and ChatGPT subscriptions, never on API keys.**

```
$ wf
██╗    ██╗███████╗
██║    ██║██╔════╝   WorkForce · Claude + Codex team
██║ █╗ ██║█████╗     Claude Opus 5.5 · high  │  Codex gpt-6-sol · high
██║███╗██║██╔══╝     ~/code/my-app
╚███╔███╔╝██║

WF │ Claude 5h 12% · wk 40% │ Codex wk 8% │ claude Opus 5.5·high │ codex gpt-6-sol·high
```

`wf` starts the official `claude` program with a plugin added, and the plugin calls the official `codex` program. Neither program is changed: plain `claude` and plain `codex` keep working exactly as before.

## Try it in a few minutes

You need Claude Code and the Codex CLI installed and signed in with your subscriptions (steps 1 and 2 below), and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/listings-api/Workforce.git   # 1. install wf
wf doctor                                                           # 2. check the setup
wf demo                                                             # 3. a sample task in a throwaway project
```

`wf doctor` checks everything WorkForce needs and prints a fix for anything missing. It uses no model quota. `wf demo` creates a tiny project with one failing test in `~/workforce-demo` and starts `wf` there with the task "fix the failing test". You watch Codex plan, Claude fix it, both review, and the team commit. It uses a little of your Claude and ChatGPT quota.

---

## Contents

1. [Install Claude Code and Codex](#1-install-claude-code-and-codex)
2. [Connect your accounts](#2-connect-your-accounts)
3. [Install WorkForce](#3-install-workforce)
4. [First run](#4-first-run)
5. [How a task works](#5-how-a-task-works)
6. [Talking to Codex directly](#6-talking-to-codex-directly)
7. [Choosing models and efforts](#7-choosing-models-and-efforts)
8. [Commits and the review gate](#8-commits-and-the-review-gate)
9. [Big tasks: `/plan-loop`](#9-big-tasks-plan-loop)
10. [Usage limits](#10-usage-limits)
11. [All commands](#11-all-commands)
12. [Where things are stored](#12-where-things-are-stored)
13. [Troubleshooting](#13-troubleshooting)
14. [Update or uninstall](#14-update-or-uninstall)
15. [License](#license)

---

## 1. Install Claude Code and Codex

You need:

- **A Claude subscription** that includes Claude Code (Pro, Max, Team or Enterprise).
- **A ChatGPT subscription** that includes Codex (Plus, Pro, Business or Enterprise).
- **git** 2.31 or newer (2.38 or newer is best).

**Claude Code:** follow the official install guide at <https://code.claude.com/docs/en/setup>, then check the version:

```sh
claude --version        # 2.1.280 or newer
```

**Codex CLI:**

```sh
npm install -g @openai/codex      # or: brew install codex
codex --version                   # 0.158 or newer
```

WorkForce finds both programs by itself, on your PATH or in the usual install folders.

You do **not** need an Anthropic or OpenAI API key. WorkForce refuses to start if `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `OPENAI_API_KEY` is set, so your subscriptions are always the ones used.

## 2. Connect your accounts

Sign in to each program once with your subscription. WorkForce never sees your login; it uses whatever these two programs are signed in with.

```sh
claude            # on first start it opens a browser; choose your Claude account (not an API key). Quit with /exit.
codex login       # choose "Sign in with ChatGPT"
```

`wf doctor` confirms both are signed in with a subscription.

## 3. Install WorkForce

With [uv](https://docs.astral.sh/uv/) (it brings its own Python if yours is older than 3.11):

```sh
uv tool install git+https://github.com/listings-api/Workforce.git
```

Or with pipx and Python 3.11+: `pipx install git+https://github.com/listings-api/Workforce.git`.

This puts `wf` (and the long name `workforce`) on your PATH. If your shell says `command not found: wf`, run `uv tool update-shell` (or add `~/.local/bin` to your PATH) and open a new terminal.

Then:

```sh
wf doctor
```

## 4. First run

```sh
cd ~/code/my-app        # any project folder: one git repo, or a folder that holds several repos
wf
```

On the first run WorkForce writes its settings file, `~/.workforce/team.toml`, with the paths it found for `claude` and `codex`:

```toml
claude = "/path/it/found/claude"
codex = "/path/it/found/codex"
codex_model = "gpt-6-sol"
codex_effort = "high"
codex_mode = "read-only"
reviewer_model = "claude-opus-5-5"
reviewer_effort = "high"
fast_coder_model = "claude-sonnet-5-5"
fast_coder_effort = "high"
alert_percent = 50
stop_percent = 60
```

If a program later moves, `wf` finds it again and updates the file. You set the models from menus inside `wf` (section 7), so there's no need to edit those lines by hand.

Every other `claude` option still works: `wf "fix the flaky login test"`, `wf -c` (continue), `wf --resume`, `wf --model claude-sonnet-5-5`.

WorkForce only runs as a chat: `wf -p` / `wf --print` (Claude Code's no-chat mode) is refused, so you can always step in.

## 5. How a task works

Type a task as you would in Claude Code. Claude follows the team rules:

1. **Codex plans.** For anything that isn't trivial, Claude first asks Codex to read the code and write a plan. Codex only reads; it can't change files.
2. **They settle disagreements.** If Claude disagrees with the plan, they debate for up to 2 rounds. If they still disagree, Claude asks you, showing both positions.
3. **Claude writes the code.** Small mechanical parts go to a faster **fast coder** helper (Sonnet 5.5 by default).
4. **Two reviews.** Before a commit, a fresh Claude **reviewer** and Codex each review the exact changes. Both must say APPROVE.
5. **Claude commits** following your git instructions (branch, message, pull first, and so on). It never force-pushes, and it only pushes when you ask.

Codex's plans and reviews are shown to you **word for word**. Claude gives its own view separately, under a label.

In a folder that holds several repos, name the repo in your task ("in `billing`, fix …"). Claude passes it to Codex and to the reviews, and approvals are kept per repo.

## 6. Talking to Codex directly

Start a line with `@codex`:

```
@codex what's the riskiest part of the retry change?
```

- Your exact words go straight to Codex, and Claude never receives that line. Codex's reply is shown to you unchanged, under `Codex (<model>):`.
- On your next normal message, Claude is given a word-for-word copy of that exchange, so it knows what you and Codex said.
- `@codex` lines continue the same Codex conversation, so Codex remembers the earlier ones.

To open that Codex conversation in Codex's own app, run this in a **separate terminal**, in the same folder:

```sh
wf codex
```

If there's no team Codex conversation yet, it starts a new Codex chat on your chosen model.

## 7. Choosing models and efforts

| To change | Type | What you get |
|---|---|---|
| This chat's Claude | `/model` | Claude Code's own menu. The left and right arrows change the effort. |
| Codex's model and effort | `/codex-model` | A menu of the models Codex offers your account, then the efforts that model supports |
| Codex's effort only | `/codex-effort` | A menu of efforts |
| The Claude reviewer or fast coder | `/claude-model` | Pick Reviewer, Fast coder or Both, then the model and effort |
| Their effort only | `/claude-effort` | Pick Reviewer, Fast coder or Both, then the effort |

Use the arrow keys and press Enter. Each menu shows up to 4 choices. The rest are listed in the question: choose **Other** and type the name, for example `ultra` or `gpt-5.5`.

You can also type the values directly: `/codex-model gpt-6-astra xhigh`, `/claude-model reviewer claude-opus-5-5 high`. A model Codex doesn't offer, or an effort a model doesn't support, is refused rather than guessed.

The banner and the status line always show which Claude and Codex models are active. The agents never pick or change a model themselves.

## 8. Commits and the review gate

The review gate is a **workflow safeguard**: it stops the team from committing work that both reviewers haven't seen, by accident or by shortcut. It is not a security boundary. Everything runs as your own user, so a determined program could get around it (see the limits below).

**What must happen before a commit**

- **A hard stop on review loops.** If a reviewer rejects the changes 3 times in a row, both review tools stop. Claude doesn't commit; it shows you both positions and the open findings word for word and asks what to do. Only you can allow more rounds, by typing `/wf-review-again` (with your decision, if you like: `/wf-review-again keep the retry, drop the cache`).
- **Both reviewers approve the exact files.** The Claude reviewer and Codex each review the changes, and both must say APPROVE for exactly the files being committed. Any edit afterwards cancels both approvals. Type `/review` to run both reviews and see whether a commit is allowed.
- **Reviews are against the current commit.** Each approval records which commit the reviewers compared against, and only a review against the current HEAD counts. A review against an older commit, or one made before a `git pull`, has to be redone.
- **Commit in a command of its own.** A command that changes files and then commits (`format && git commit`, `echo … > file && git commit`) is refused, because those changes would go in unreviewed. Make the change, run `/review`, then commit. `git add -A && git commit …` is fine.

**How it's checked** (three layers, all inside `wf` sessions only)

1. **Claude's commands.** A hook reads every command Claude runs, refuses commits without both approvals, and refuses commands that skip the later checks (`--no-verify`, `-c core.hooksPath=…`, `env -i`). It also refuses to write or run scripts that contain those tricks, or that touch the approvals or their key.
2. **Git's pre-commit step.** Inside `wf`, git runs your repository's own pre-commit hook first (formatters included), then a WorkForce check that the staged files still match the approved ones.
3. **Every branch update.** Git also runs a WorkForce check whenever a branch moves, and `--no-verify` can't skip it. New commits need both approvals; fast-forwards to existing commits, clean merges of existing history, `git pull` and `git reset` to older commits are allowed.

Your repository's own git hooks keep running as usual. Commits you make yourself, outside `wf`, aren't affected.

**Other rules**

- **Both reviewers are read-only.** The Claude reviewer can only read files. Codex runs read-only, with its browser and computer-control add-ons switched off. Codex can't commit even in write mode, because its sandbox blocks the `.git` folder.
- **Needs both reviews:** new commits of any kind, including a cherry-pick or revert (use `git cherry-pick -n`, review, then commit), and pointing a branch at a commit that isn't on it or on a remote branch (`git reset <commit>`, `git branch -f`, `git checkout -B`).
- **Always refused:** force-push, `rebase`, `git stash`, `git commit --amend` (make a new commit instead), `commit-tree`, `update-ref`, and `git reset --hard` on `main`/`master`.
- **Also always refused, for every tool:** reading credential files (`~/.ssh`, `~/.gnupg`, the Claude and Codex login files, the keychain), and deleting or writing files outside the project folder. Writing to a temp folder is fine; a recursive delete (`rm -rf`) works only inside the project. If a task genuinely needs such a step, you do it by hand.

**Known limits.** The checks read commands and files as text, so obfuscated or downloaded code, or a program that talks to git's files directly, can get past them. Approvals are signed with a key in `~/.workforce/team.key`, but that key belongs to your user account like everything else, so the signature stops accidental or casual edits, not a determined agent. If you need a hard guarantee, protect your branches on the server too (required reviews on GitHub or GitLab).

## 9. Big tasks: `/plan-loop`

For a large or risky change, type:

```
/plan-loop migrate the billing retries to the new queue
```

This runs a heavier process, **only when you ask for it**:

1. **Recon.** Claude reads the relevant code and lists its assumptions, each with a source. You correct any that are wrong.
2. **Interview.** Claude asks only the questions that change the outcome, each with its recommendation.
3. **Plan.** Claude writes `.workforce/team/plans/<name>/PLAN.md`: the goal, what counts as done, the steps, the risks and the exact test commands.
4. **Plan review.** Codex reviews the plan: APPROVED, REVISE or BLOCKED. Claude fixes what's valid, and later rounds continue the same Codex conversation. There are 5 rounds at most (change it with `rounds=<n>`); then you decide. An approval only covers that exact plan, so any edit needs another review. Every round is saved word for word in `REVIEW-LOG.md` next to the plan.
5. **Build, then the normal two reviews and commit.**

Without `/plan-loop`, the lighter flow from section 5 applies.

## 10. Usage limits

The status line shows your Claude 5-hour and weekly usage, and Codex's usage.

- **At 50%** on any window, Claude mentions it and keeps working.
- **At 60%**, every prompt and tool call is paused until that window resets. Type `/wf-continue` to carry on anyway until that window resets.
- `@codex` only checks the Codex windows.
- Change the thresholds with `alert_percent` and `stop_percent` in `~/.workforce/team.toml`.

## 11. All commands

| Command | What it does |
|---|---|
| *(just type)* | A task for the team (section 5) |
| `@codex <text>` | Talk to Codex directly, word for word |
| `/review` | Run both reviews on the current changes and show whether a commit is allowed |
| `/plan <task>` | Ask Codex for a plan only |
| `/plan-loop <task>` | The full plan → review → build process (section 9) |
| `/codex-model`, `/codex-effort` | Codex's model and effort, from menus |
| `/claude-model`, `/claude-effort` | The reviewer's and fast coder's model and effort, from menus |
| `/model` | This chat's Claude (Claude Code's own menu) |
| `/codex-mode read-only\|write` | Whether Codex may edit files when you ask it to. Default `read-only`; plans and reviews are always read-only. |
| `/usage` | Claude and Codex usage windows |
| `/wf-settings` | Every setting in one view |
| `/wf-continue` | Carry on past a 60% stop until that window resets |
| `/wf-review-again` | Allow more review rounds after a reviewer rejected the changes 3 times in a row |
| `wf codex` (in a terminal) | Open the team's Codex conversation in Codex's own app |

The plugin commands also work with a prefix, for example `/workforce:codex-model`, if a name clashes with another plugin.

## 12. Where things are stored

| Path | What it is |
|---|---|
| `~/.workforce/team.toml` | Your settings |
| `~/.workforce/team.key` | The key that signs review approvals. Created automatically. |
| `~/.workforce/team.log` | A log of tool calls, with secrets redacted |
| `~/.workforce/*.json` | Usage and model-list caches |
| `~/.workforce/plugin/` | The Claude Code plugin `wf` loads, rebuilt on every start from the installed package. |
| `~/.workforce/git-hooks/` | The pre-commit and branch-update checks `wf` points git at inside its sessions. They hand every hook on to your repository's own hooks. |
| `<project>/.workforce/team/` | Codex's plans, reviews and replies, word for word; the `@codex` thread; `/plan-loop` plans. It's excluded from git automatically, so it never shows up in `git status`. |
| `<repo's .git>/wf-approvals.json` | The signed review approvals, per repo. Never committed. |

## 13. Troubleshooting

| Problem | Fix |
|---|---|
| Something isn't working | Run `wf doctor`. It checks every requirement and prints the fix. |
| `command not found: wf` | Run `uv tool update-shell`, or add `~/.local/bin` to your PATH, then open a new terminal. |
| `ANTHROPIC_API_KEY is set …` and `wf` exits | `unset ANTHROPIC_API_KEY` (and the others). Remove it from `~/.zshrc` if it's set there. |
| `claude not found at …` or `codex not found at …` | Install it (section 1), or put its path in `~/.workforce/team.toml` (`which claude`, `which codex`). |
| Codex tools fail with a login error | Run `codex login` again, then `codex login status`. |
| `/codex-model` shows only the current model | Codex's model list couldn't be read (it prints why). Check `codex login status`; you can still type a model directly. |
| Reviews stopped: "round limit reached" | A reviewer rejected the changes 3 times in a row. Decide which way to go, tell Claude, and type `/wf-review-again`. |
| A commit is blocked | Read the reason. Usually you need `/review`, files changed after the review (a formatter hook counts), or the review was against an older commit. Run `/review` again, then commit in a command of its own. |
| Everything is paused | A usage window reached 60%. Wait for the reset time shown, or type `/wf-continue`. |

Claude Code's own header (the Claude logo and version) still shows under the WF banner. Claude Code has no setting to hide it.

## 14. Update or uninstall

```sh
uv tool upgrade workforce          # update (pipx: pipx upgrade workforce)
uv tool uninstall workforce        # uninstall (pipx: pipx uninstall workforce)
rm -rf ~/.workforce                # optional: remove settings, logs and approvals
```

Claude Code and Codex are left exactly as they were.

---

## For developers

```sh
git clone https://github.com/listings-api/Workforce.git && cd Workforce
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q                                   # the full suite, with fake claude/codex; no network, no quota
.venv/bin/python scripts/team_livetest.py             # live checks against your real claude and codex
.venv/bin/python scripts/context_livetest.py          # live check that long sessions keep their context
```

The live scripts use a throwaway repo and cheap models by default, so they use a little of your subscription quota.

More detail on every command and setting is in [docs/USAGE.md](docs/USAGE.md), and the design notes are in [docs/SPEC-TEAM-CLI.md](docs/SPEC-TEAM-CLI.md).

## License

[MIT](LICENSE).
