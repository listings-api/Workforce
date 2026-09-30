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

---

## Contents

1. [What you need](#1-what-you-need)
2. [Install Claude Code and Codex](#2-install-claude-code-and-codex)
3. [Connect your accounts](#3-connect-your-accounts)
4. [Install WorkForce](#4-install-workforce)
5. [First run](#5-first-run)
6. [How a task works](#6-how-a-task-works)
7. [Talking to Codex directly](#7-talking-to-codex-directly)
8. [Choosing models and efforts](#8-choosing-models-and-efforts)
9. [Commits and the review gate](#9-commits-and-the-review-gate)
10. [Big tasks: `/plan-loop`](#10-big-tasks-plan-loop)
11. [Usage limits](#11-usage-limits)
12. [All commands](#12-all-commands)
13. [Where things are stored](#13-where-things-are-stored)
14. [Troubleshooting](#14-troubleshooting)
15. [Update or uninstall](#15-update-or-uninstall)
16. [License](#license)

---

## 1. What you need

- **macOS**. It's built and tested on Apple Silicon.
- **Python 3.11** and **[uv](https://docs.astral.sh/uv/)**, to create the virtual environment: `brew install uv`.
- **git**.
- **A Claude subscription** that includes Claude Code (Pro, Max, Team or Enterprise).
- **A ChatGPT subscription** that includes Codex (Plus, Pro, Business or Enterprise).
- Optional: [Ollaya](https://github.com/ollaya-dev/ollaya) with the `laya:en` model, a small local model that gives quick yes/no hints. Everything works without it.

You do **not** need an Anthropic or OpenAI API key. WorkForce refuses to start if `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `OPENAI_API_KEY` is set, so your subscriptions are always the ones billed.

## 2. Install Claude Code and Codex

**Claude Code:** follow the official install guide at <https://code.claude.com/docs/en/setup>. The native installer puts it at `~/.local/bin/claude`, which is where WorkForce looks by default. Then check the version:

```sh
claude --version        # 2.1.280 or newer
```

**Codex CLI** (Homebrew installs to `/opt/homebrew/bin/codex`):

```sh
brew install codex      # or: npm install -g @openai/codex
codex --version         # 0.158 or newer
```

If either program ends up somewhere else, that's fine. You'll give WorkForce its path in step 5.

## 3. Connect your accounts

Log in to each program once with your subscription. WorkForce never sees your login; it uses whatever these two programs are logged in with.

**Claude Code:**

```sh
claude            # on first start it opens a browser to log in; choose your Claude account (not an API key)
```

Inside Claude Code, `/status` shows the account it is using. Quit with `/exit`.

**Codex:**

```sh
codex login       # choose "Sign in with ChatGPT"
codex login status
```

Make sure no API key is set in your shell:

```sh
env | grep -E 'ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|OPENAI_API_KEY'   # should print nothing
```

## 4. Install WorkForce

```sh
git clone https://github.com/listings-api/Workforce.git ~/agent-team
cd ~/agent-team
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
```

Put the `wf` command on your PATH:

```sh
mkdir -p ~/.local/bin
ln -sf ~/agent-team/.venv/bin/wf ~/.local/bin/wf
ln -sf ~/agent-team/.venv/bin/workforce ~/.local/bin/workforce
```

If your shell then says `command not found: wf`, add this line to `~/.zshrc` and open a new terminal:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Keep the `~/agent-team` folder where it is. The plugin's hooks and its Codex server run from `~/agent-team/.venv`.

## 5. First run

```sh
cd ~/code/my-app        # any project folder: one git repo, or a folder that holds several repos
wf
```

On the first run WorkForce writes its settings file, `~/.workforce/team.toml`:

```toml
claude = "~/.local/bin/claude"
codex = "/opt/homebrew/bin/codex"
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

If `claude` or `codex` lives somewhere else (`which claude`, `which codex`), put the right path here. You'll set the models from menus inside `wf` (section 8), so there's no need to edit those lines by hand.

Check that it's connected: inside `wf`, type `/wf-settings`. It should list both models and show your current usage for Claude and Codex.

Every `claude` option still works: `wf "fix the flaky login test"`, `wf -c` (continue), `wf --resume`, `wf --model claude-sonnet-5-5`.

## 6. How a task works

Type a task as you would in Claude Code. Claude follows the team rules:

1. **Codex plans.** For anything that isn't trivial, Claude first asks Codex to read the code and write a plan. Codex only reads; it can't change files.
2. **They settle disagreements.** If Claude disagrees with the plan, they debate for up to 2 rounds. If they still disagree, Claude asks you, showing both positions.
3. **Claude writes the code.** Small mechanical parts go to a faster **fast coder** helper (Sonnet 5.5 by default).
4. **Two reviews.** Before a commit, a fresh Claude **reviewer** and Codex each review the exact changes. Both must say APPROVE.
5. **Claude commits** following your git instructions (branch, message, pull first, and so on). It never force-pushes and never turns off commit signing, and it only pushes when you ask.

Codex's plans and reviews are shown to you **word for word**. Claude gives its own view separately, under a label.

In a folder that holds several repos, name the repo in your task ("in `billing`, fix …"). Claude passes it to Codex and to the reviews, and approvals are kept per repo.

## 7. Talking to Codex directly

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

## 8. Choosing models and efforts

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

## 9. Commits and the review gate

A hook checks every `git` command Claude runs:

- **`git commit` needs both approvals**, from the Claude reviewer and from Codex, for exactly the files being committed. Any edit after the reviews cancels them. Type `/review` to run both reviews and see whether a commit is allowed.
- **The reviews can't be faked.** WorkForce runs both reviewers itself and signs their verdicts. The Claude you chat with can't record or edit an approval.
- **Both reviewers are read-only.** The Claude reviewer can only read files. Codex runs read-only, with its browser and computer-control add-ons switched off.
- **Always allowed:** `git pull`, and `git merge origin/master` with nothing staged. They only bring in existing commits. If a merge stops on conflicts, the commit that resolves them needs both reviews.
- **Needs both reviews:** pointing a branch at a commit that isn't already on it or on a remote branch (`git reset <commit>`, `git branch -f`, `git checkout -B`).
- **Always refused:** force-push, turning off commit signing, `rebase`, `git stash`, `commit-tree`, `update-ref`, and `git reset --hard` on `main`/`master`.

If your git signs commits (for example with Touch ID), you confirm each commit as usual.

## 10. Big tasks: `/plan-loop`

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

Without `/plan-loop`, the lighter flow from section 6 applies.

## 11. Usage limits

The status line shows your Claude 5-hour and weekly usage, and Codex's usage.

- **At 50%** on any window, Claude mentions it and keeps working.
- **At 60%**, every prompt and tool call is paused until that window resets. Type `/wf-continue` to carry on anyway until that window resets.
- `@codex` only checks the Codex windows.
- Change the thresholds with `alert_percent` and `stop_percent` in `~/.workforce/team.toml`.

## 12. All commands

| Command | What it does |
|---|---|
| *(just type)* | A task for the team (section 6) |
| `@codex <text>` | Talk to Codex directly, word for word |
| `/review` | Run both reviews on the current changes and show whether a commit is allowed |
| `/plan <task>` | Ask Codex for a plan only |
| `/plan-loop <task>` | The full plan → review → build process (section 10) |
| `/codex-model`, `/codex-effort` | Codex's model and effort, from menus |
| `/claude-model`, `/claude-effort` | The reviewer's and fast coder's model and effort, from menus |
| `/model` | This chat's Claude (Claude Code's own menu) |
| `/codex-mode read-only\|write` | Whether Codex may edit files when you ask it to. Default `read-only`; plans and reviews are always read-only. |
| `/usage` | Claude and Codex usage windows |
| `/wf-settings` | Every setting in one view |
| `/wf-continue` | Carry on past a 60% stop until that window resets |
| `wf codex` (in a terminal) | Open the team's Codex conversation in Codex's own app |

The plugin commands also work with a prefix, for example `/workforce:codex-model`, if a name clashes with another plugin.

## 13. Where things are stored

| Path | What it is |
|---|---|
| `~/.workforce/team.toml` | Your settings |
| `~/.workforce/team.key` | The key that signs review approvals. Created automatically; keep it private. |
| `~/.workforce/team.log` | A log of tool calls, with secrets redacted |
| `~/.workforce/*.json` | Usage and model-list caches |
| `<project>/.workforce/team/` | Codex's plans, reviews and replies, word for word; the `@codex` thread; `/plan-loop` plans. It's excluded from git automatically, so it never shows up in `git status`. |
| `<repo's .git>/wf-approvals.json` | The signed review approvals, per repo. Never committed. |

## 14. Troubleshooting

| Problem | Fix |
|---|---|
| `command not found: wf` | Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` and open a new terminal (step 4). |
| `ANTHROPIC_API_KEY is set …` and `wf` exits | `unset ANTHROPIC_API_KEY` (and the others). Remove it from `~/.zshrc` if it's set there. |
| `claude not found at …` or `codex not found at …` | Put the right path in `~/.workforce/team.toml` (`which claude`, `which codex`). |
| Codex tools fail with a login error | Run `codex login` again, then `codex login status`. |
| `/codex-model` shows only the current model | Codex's model list couldn't be read (it prints why). Check `codex login status`; you can still type a model directly. |
| A commit is blocked | Read the reason. Usually you need `/review`, or files changed after the review. Run `/review` again, then commit. |
| Everything is paused | A usage window reached 60%. Wait for the reset time shown, or type `/wf-continue`. |
| `.venv/bin/python is missing` | Redo the two `uv` lines from step 4 inside `~/agent-team`. |

Claude Code's own header (the Claude logo and version) still shows under the WF banner. Claude Code has no setting to hide it.

## 15. Update or uninstall

**Update:**

```sh
cd ~/agent-team
git pull
uv pip install --python .venv/bin/python -e ".[dev]"
```

**Uninstall:**

```sh
rm ~/.local/bin/wf ~/.local/bin/workforce
rm -rf ~/agent-team ~/.workforce
```

Claude Code and Codex are left exactly as they were.

---

## For developers

```sh
cd ~/agent-team
.venv/bin/pytest -q                                   # the full suite, with fake claude/codex; no network, no quota
.venv/bin/python scripts/team_livetest.py             # 8 live checks against your real claude and codex
.venv/bin/python scripts/context_livetest.py          # live check that long sessions keep their context
```

The live scripts use a throwaway repo and cheap models by default, so they use a little of your subscription quota.

**The older unattended pipeline** is still included: `wf run "<goal>"` works through a goal without you at the keyboard, in separate git worktrees. See [docs/USAGE.md](docs/USAGE.md) for it and for every detail of the settings. The design notes are in [docs/SPEC-TEAM-CLI.md](docs/SPEC-TEAM-CLI.md) and [docs/SPEC.md](docs/SPEC.md).

## License

[MIT](LICENSE).
