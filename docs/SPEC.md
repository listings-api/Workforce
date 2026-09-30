# WorkForce Agent: build spec

WorkForce Agent is a local CLI (`workforce`, alias `wf`) that runs a team of AI agents on the user's Mac. Codex researches and plans, Claude writes code, both review independently, and the user is asked when the agents cannot agree. A small local decision model (Laya) makes quick yes/no and pick-one calls beside them.

This file is the design of the unattended pipeline (`wf run`). The interactive `wf` (Claude Code plus Codex) is described in `docs/SPEC-TEAM-CLI.md`.

## 1. Hard rules (never break these)

1. **Subscription only.** The tool drives the official `claude` and `codex` CLIs logged in with the user's Claude Team and ChatGPT Business accounts. It never calls a model API directly, never reads their OAuth tokens, and refuses to start if `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `OPENAI_API_KEY` is set in the environment. Every Claude run must report `apiKeySource: "none"` in its `system/init` event; any other value aborts the run.
2. **The user picks every model and effort.** Defaults come from `workforce.toml`; per task the user confirms or overrides. Agents never choose models. If a configured model is unavailable, pause and tell the user. Never fall back to a different model.
3. **Only Claude commits.** Every `git commit` / `git push` in a target repo is done by a Claude run using the user's normal global setup (no `--setting-sources` restriction), because the user's commit rules live there. Codex and Python never commit. Python may create worktrees and branches, read diffs, and run `git merge`, but a merge that needs a commit is also done by that Claude commit run.
4. **Commits keep the user's own git setup.** WorkForce never changes how git commits (no config overrides, no skipped hooks). One commit per task, after both reviewers approve.
5. **Laya decides only small typed questions.** It never approves code, never picks models, never overrides the user.
6. **No inline code comments** explaining what code does. Docstrings on public functions are fine. No hardcoded fallback values for config: missing config is an error with a clear message.

## 2. Environment facts (verified 2026-09-29)

| Thing | Value |
|---|---|
| Python | `/opt/homebrew/bin/python3.11` (project venv at `.venv`, managed with `uv`) |
| claude | `~/.local/bin/claude` 2.1.284, not on the default PATH, so resolve it via config `bin.claude` |
| codex | `/opt/homebrew/bin/codex` 0.158.0 |
| Claude models | `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-4-5-20251001` |
| Claude effort | `--effort low\|medium\|high\|xhigh\|max` |
| Codex models | `gpt-6-astra`, `gpt-6-sol`, `gpt-reserve`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` |
| Codex effort | `-c model_reasoning_effort=low\|medium\|high\|xhigh\|max\|ultra` |

### Claude headless

```
claude -p "<prompt>" --model <id> --effort <level> \
  --output-format stream-json --verbose \
  --permission-mode auto \
  --setting-sources project --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  [--json-schema '<schema json>'] [--resume <session_id>] [--chrome]
```
- `--bare` does NOT work with subscription login. Do not use it.
- Events are JSONL. Relevant ones:
  - `{"type":"system","subtype":"init","session_id":…,"model":…,"apiKeySource":"none"}`
  - `{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{"five_hour":{"utilization":0.03,"resetsAt":1790676000},"seven_day":{"utilization":0.31,"resetsAt":1790838000}},"status":"allowed",…}}`. Utilization is 0–1 and `resetsAt` is unix seconds.
  - final `{"type":"result","is_error":false,"result":"…","session_id":…,"structured_output":{…},"usage":{…},"modelUsage":{…}}`. With `--json-schema`, the parsed object is in `structured_output`.
- Resume with `--resume <session_id>`. Always use an explicit ID; never use `--continue`.
- An unknown model returns `is_error: true` with a message, or a result string starting `API Error: 400`. Treat as `ModelUnavailable`.
- The commit runner uses the same command minus `--setting-sources/--strict-mcp-config/--mcp-config`.

### Codex headless

```
codex exec --json -m <model> -c model_reasoning_effort=<level> \
  -s <read-only|workspace-write> -C <dir> \
  [--output-schema <file>] -o <last_message_file> "<prompt>"  </dev/null
codex exec resume <thread_id> --json "<prompt>" </dev/null
```
- stdin MUST be `/dev/null`, or it waits for input.
- Events: `{"type":"thread.started","thread_id":…}`, `{"type":"item.completed","item":{"type":"agent_message","text":…}}`, `{"type":"turn.completed","usage":{…}}`, `{"type":"turn.failed","error":{"message":…}}`, `{"type":"error","message":…}`.
- An unsupported model gives `turn.failed` with "model is not supported when using Codex with a ChatGPT account". Treat as `ModelUnavailable`.
- With `--output-schema`, the final JSON is written to the `-o` file.

### Codex quota (costs no model quota)

Spawn `codex app-server` (JSON-RPC over stdio, one JSON object per line):
```
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"workforce","version":"0.1"}}}
→ {"jsonrpc":"2.0","method":"initialized"}
→ {"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read"}
← {"id":2,"result":{"rateLimits":{"primary":{"usedPercent":0,"windowDurationMins":10080,"resetsAt":1791265456},"secondary":null,"planType":"…"}}}
```
`primary`/`secondary` may each be null. Map each window by `windowDurationMins` (300 = 5-hour, 10080 = weekly).

## 3. Layout

```
agent-team/
  pyproject.toml            # package "workforce", scripts: workforce, wf -> workforce.cli:main
  workforce/
    __init__.py             # __version__ = "0.1.0"
    config.py               # load/validate workforce.toml → Config
    paths.py                # .workforce/ layout helpers
    state.py                # durable state store (JSON, atomic write, lock)
    events.py               # append-only event log + in-process subscribers
    errors.py               # exception types
    agents/
      base.py               # AgentRequest, AgentResult, AgentRunner protocol
      claude.py             # ClaudeRunner
      codex.py              # CodexRunner
      guard.py              # subscription/env/model checks
    schemas.py              # JSON schemas: plan, plan_review, verdict, debate_turn, commit_result
    prompts/*.md            # prompt templates (str.format placeholders)
    git_ops.py              # worktrees, branches, diff, merge-base, head sha, merge
    checks.py               # detect + run tests/lint/build for a target repo
    review.py               # independent double review + reconciliation
    commit.py               # Claude user-setup commit step
    orchestrator.py         # v1 sequential loop
    usage/
      claude_limits.py      # parse rate_limit_event, keep last known
      codex_limits.py       # app-server client
      monitor.py            # thresholds, pause/resume, usage.json + usage.md
      summary.py            # Haiku plain-English summary
    decider/
      base.py               # Decider protocol, Decision dataclass
      laya.py               # Laya via local Ollaya server (TypeSafe-compatible API)
      fallback.py           # used when Laya is unavailable
      hooks.py              # PreToolUse risk-gate hook entry point for claude + codex
    scheduler.py            # v3 DAG scheduler + merge queue
    cli.py                  # argparse subcommands
    console/
      app.py                # interactive console (prompt_toolkit)
      logo.py               # ANSI truecolor logo
      render.py             # status panel, footer, event formatting
  tests/
    fakes/fake_claude.py    # scripted stand-in for `claude`, emits real event shapes
    fakes/fake_codex.py     # scripted stand-in for `codex`
    …
```

Dependencies: `prompt_toolkit`, `rich`, `httpx`, `pytest`. Nothing else without asking.

## 4. Config: `workforce.toml` (in the target repo root; `workforce init` writes it)

```toml
[bin]
claude = "~/.local/bin/claude"
codex = "/opt/homebrew/bin/codex"

[roles.planner]        # Codex
agent = "codex"
model = "gpt-6-sol"
effort = "high"

[roles.plan_reviewer]  # Claude checks the plan
agent = "claude"
model = "claude-opus-5-5"
effort = "high"

[roles.coder]          # per-task default; user overrides per task
agent = "claude"
model = "claude-sonnet-5-5"
effort = "high"

[roles.reviewer_claude]
agent = "claude"
model = "claude-opus-5-5"
effort = "high"

[roles.reviewer_codex]
agent = "codex"
model = "gpt-6-sol"
effort = "high"

[roles.committer]
agent = "claude"
model = "claude-sonnet-5-5"
effort = "high"

[roles.usage_summary]
agent = "claude"
model = "claude-haiku-4-5-20251001"
effort = "low"

[limits]
debate_rounds = 3
review_rounds = 3
alert_percent = 50
pause_percent = 60
poll_minutes = 10

[checks]               # empty = auto-detect
test = ""
lint = ""
build = ""

[browser]
claude = true           # adds --chrome for coder/reviewer_claude runs
codex = true
computer_use = false    # per-task toggle

[decider]
backend = "laya"        # "laya" | "off"
url = "http://127.0.0.1:11435"
confidence = 0.85
```
Validation: every role must have `agent`, `model` and `effort`, and effort must be valid for that agent. There are no silent defaults: `workforce init` writes the full file, and loading a file with missing keys raises `ConfigError` naming the key.

## 5. State: `.workforce/` (in the target repo, git-ignored)

```
.workforce/
  state.json            # authoritative, only written by state.py
  state.lock            # fcntl lock; a second `workforce run` refuses to start
  events.jsonl          # append-only log of everything that happened
  plan.md               # Codex's plan, human-readable
  decisions.md          # user rules and answers, fed into every agent prompt
  usage.json            # latest readings (machine)
  usage.md              # Haiku summary (human)
  runs/<run_id>/<task_id>/<step>-<n>.jsonl   # raw agent event streams
```

Worktrees live **outside** the target repo, so the repo's own tools (pytest discovery, linters, file watchers) never see them: `~/.workforce/worktrees/<repo_slug>/<run_id>/<task_id>/`, where `repo_slug` = `<repo dir name>-<first 8 hex of sha1(abs repo path)>`. Only worktrees under `~/.workforce/worktrees/` may be force-removed.

`state.json` schema (dataclasses in `state.py`, serialized with `asdict`):
- `Run`: `id`, `goal`, `status` (`planning|debating|awaiting_models|running|paused|awaiting_user|done|failed`), `pause_reason`, `created_at`, `tasks: list[Task]`, `questions: list[Question]`, `mode` (`auto|step`).
- `Task`: `id` (`T1`…), `title`, `description`, `acceptance` (list), `depends_on` (list of ids), `model`, `effort`, `agent`, `status` (`pending|coding|testing|reviewing|fixing|awaiting_commit|committing|merged|failed|blocked`), `branch`, `worktree`, `base_sha`, `head_sha`, `coder_session`, `review_round`, `reviews: list[Review]`, `infra_failures`, `computer_use` (bool).
- `Review`: `reviewer` (`claude|codex`), `model`, `session`, `sha`, `base_sha`, `verdict` (`APPROVE|REQUEST_CHANGES|BLOCKED`), `findings` (list of `{severity, file, line, message}`), `at`.
- `Question`: `id` (`Q1`…), `task_id`, `text`, `positions` (dict agent → text), `answer`, `status` (`open|answered`).

Writes are atomic (write temp file, `fsync`, `os.replace`). Every state change also appends an event.

## 6. Agent runner interface (`agents/base.py`)

```python
@dataclass
class AgentRequest:
    role: str; agent: str; model: str; effort: str
    prompt: str; cwd: Path
    schema: dict | None = None
    resume_session: str | None = None
    sandbox: str = "workspace-write"      # codex only
    browser: bool = False
    user_setup: bool = False              # claude only: True for the commit step
    log_path: Path | None = None
    timeout_s: int = 3600

@dataclass
class AgentResult:
    ok: bool; text: str; structured: dict | None
    session_id: str | None; model: str | None
    usage: dict; rate_limits: dict | None       # claude: unifiedWindows, codex: None
    error_kind: str | None   # None|"model_unavailable"|"auth"|"rate_limited"|"timeout"|"crash"|"schema"
    error: str | None

class AgentRunner(Protocol):
    def run(self, req: AgentRequest, on_event: Callable[[dict], None] | None = None) -> AgentResult: ...
```
- Runners stream stdout line by line. Each parsed event goes to `on_event` and to `log_path`.
- `error_kind` distinguishes infrastructure failures from disagreement. Infra failures never consume a review or debate round. They retry up to 2 times with backoff, then create a Question for the user.
- A result whose `structured` fails schema validation has `ok=False, error_kind="schema"`. It never counts as approval.
- The binary path comes from config `bin.*` so tests can point it at `tests/fakes/*`.

## 7. The v1 loop (`orchestrator.py`)

1. **Preflight** (`guard.py`): no API key env vars; both binaries run `--version`; `claude auth status` shows `"authMethod": "claude.ai"`; `codex login status` shows "Logged in using ChatGPT"; the target is a git repo with a clean working tree on a branch.
2. **Plan:** the planner (Codex, `-s read-only`, browser allowed) gets the goal, `decisions.md` and a repo listing. It returns `plan` schema JSON: `tasks[{id,title,description,acceptance[],depends_on[]}]`, `risks[]`, `open_questions[]`. Python writes `plan.md` and the state.
3. **Plan debate:** the plan_reviewer (Claude, fresh session) returns `plan_review` JSON: `{agree: bool, objections: [..], suggested_changes: [..]}`. If it disagrees, Codex gets the objections (resuming its planner session) and returns a revised plan plus a `debate_turn` (`{position, concessions[], remaining_disagreements[]}`). This repeats up to `debate_rounds`. If still unresolved, create a Question with both positions and set the run to `awaiting_user`. When the decider is on, after each round ask Laya `converged?` (yes/no). If yes with confidence ≥ threshold, stop early.
4. **Model pick:** set status `awaiting_models`. The CLI (or console) shows each task with the default coder model/effort and the Laya size hint if available. The user presses Enter to accept or types a model/effort. Store them on the task.
5. **For each task in dependency order (v1: one at a time):**
   1. `git_ops.create_worktree(task)`: branch `wf/<run_id>/<task_id>` from current HEAD. Record `base_sha`.
   2. **Code:** the coder (Claude, chosen model/effort, `cwd=worktree`) gets the task, acceptance criteria, `decisions.md` and a summary of previously merged tasks. It must not commit. Store `coder_session`.
   3. **Checks:** `checks.run(worktree)` runs test/lint/build (configured or auto-detected: `pytest`, `npm test`, `go test ./...`, `cargo test`, `make test`). If any fail, send the output back to the coder (resume session), up to `review_rounds` total fix attempts, then ask the user.
   4. **Snapshot:** Python records `head_sha` as a tree hash of the worktree (`git add -A` into a temporary index via `GIT_INDEX_FILE`, then `git write-tree`). Approvals attach to this tree hash plus `base_sha`, because Python doesn't commit.
   5. **Review** (`review.py`): both reviewers run **in parallel, fresh sessions, neither sees the other's output**. Inputs: task + acceptance, `git diff base_sha` (working tree incl. untracked), check results, and read access to the worktree (Codex `-s read-only -C worktree`; Claude cwd=worktree with a prompt forbidding edits). Output: `verdict` schema. After both are recorded, if they differ or either has findings, run one **reconciliation** step: each reviewer sees the other's findings and returns an updated verdict.
   6. If both say `APPROVE` for the same tree hash, move to commit. Otherwise the coder gets the merged findings (resume session), then checks, snapshot and review run again (fresh reviewer sessions). After `review_rounds`, create a Question. `BLOCKED` from either reviewer creates a Question immediately.
   7. **Commit** (`commit.py`): status `awaiting_commit`. Print `👆 committing… (confirm if your git asks)`. The committer (Claude, `user_setup=True`, cwd=worktree) is told to commit exactly the current changes on branch `wf/<run>/<task>` with a message summarizing the task, following the user's own commit rules. It returns `commit_result` JSON `{committed: bool, sha, message, error}`. Python verifies `git rev-parse HEAD^{tree}` equals the approved tree hash. If not, the approvals are void and it goes back to review.
   8. **Merge:** Python runs `git merge --ff-only wf/<run>/<task>` in the main worktree. If fast-forward isn't possible (only in v3), the committer Claude does the merge commit. After the merge, the tree must match, then Python removes the worktree. The task becomes `merged`.
6. When all tasks are merged the run is `done`, with a summary event.

Every prompt includes `decisions.md` (the user's standing rules). Prompts live in `workforce/prompts/*.md` and are the only place wording lives.

**Restart safety:** `workforce run --resume` (and the console) picks up the run from `state.json` at the task's current status. It never repeats a step already recorded as done.

## 8. v2: usage monitor (`usage/`)

- **Readings:**
  - Claude: every Claude run's `rate_limit_event` updates `usage.json` (`five_hour`, `seven_day` utilization and resetsAt, `read_at`).
  - Codex: `codex_limits.read()` via app-server, before and after every Codex run and every `poll_minutes` while a run is active.
  - Claude when idle: if the last Claude reading is older than `poll_minutes`, a 1-turn Haiku ping (`"Reply OK"`) refreshes it.
- **States:** each value is `fresh` (< poll interval), `last_known` (older; shown with its timestamp) or `unknown`. Never invent numbers.
- **Checks happen before launching any agent run, after each run, and on each poll tick.** At or above `alert_percent` on any window, emit an `alert` event once per window per reset period (no stopping). At or above `pause_percent`:
  - Finish the in-flight run: don't kill it, but launch nothing new.
  - Set the run to `paused` with `pause_reason` naming the window.
- **Auto-resume:** if every paused window is a 5-hour window, schedule a resume at `resetsAt + 60s`. At that time, take fresh readings, and resume only if **all** windows are below `pause_percent`.
- **Weekly pauses** never auto-resume. They wait for `workforce resume`, even if a 5-hour window resets.
- **Haiku summary:** after each reading change, at most every 10 minutes, Haiku gets `usage.json` and writes 2–4 plain sentences to `usage.md`. It never decides anything.
- The UI promise is "pauses when usage at or above 60% is detected", not an exact ceiling.

## 9. v2: Laya decider (`decider/`)

Laya is a local open-weights decision model (Apache-2.0, Convai Innovations; `convaiinnovations/laya`, `convaiinnovations/laya-typed-decisions`). It's served by **Ollaya** (`github.com/ollaya-dev/ollaya`) behind a TypeSafe-Jev-compatible HTTP API. It takes a `state` (context text) plus typed `questions` (choice / score / boolean) and returns calibrated probabilities. Its context is short (~512–1024 tokens), so always send compact summaries, never whole diffs.

The build agent for this module must first read Ollaya's README and Laya's model card, install Ollaya, pull Laya, and record the exact request/response shape in `docs/laya.md` before writing the client. Anything not verified goes to `docs/QUESTIONS.md`.

```python
@dataclass
class Decision:
    question: str; answer: str | bool | float | None
    confidence: float | None; source: str   # "laya" | "fallback" | "off"
    raw: dict | None

class Decider(Protocol):
    def ask_bool(self, key: str, state: str, question: str) -> Decision: ...
    def ask_choice(self, key: str, state: str, question: str, options: list[str]) -> Decision: ...
    def available(self) -> bool: ...
```

Every decision is logged as an event. Below `confidence`, the result counts as "unsure" and the listed fallback applies:

| key | question | used by | when unsure |
|---|---|---|---|
| `risk_gate` | Is this tool call risky (deletes data, force-pushes, touches secrets/credentials, prod, billing, or files outside the worktree)? | PreToolUse hook for Claude runs and Codex hooks | block and create a Question |
| `agent_progress` | progressing / stuck / off_task, from the last ~40 log lines | monitor loop every 3 min per active run | flag it to the user in the UI (no automatic action) |
| `debate_converged` | Do these two positions now agree? | plan debate, review reconciliation | keep going (rounds are still capped) |
| `verdict_consistent` | Does this review text match its verdict (e.g. APPROVE but lists a blocking bug)? | review.py | treat as not approved; rerun that reviewer once |
| `needs_human` | Can the agents settle this themselves, or does it need the user? | before creating any Question after the round cap | ask the user |
| `task_size` | small / big | model-pick screen, **hint text only** | show no hint |

`fallback.py` returns `source="fallback"` with the "when unsure" behaviour and no model calls. It never calls Opus or Codex on its own. The risk gate also has a hard deny-list that runs before Laya and can't be overridden: `rm -rf` outside the worktree, `git push --force`, `git reset --hard` on main, `--no-gpg-sign`, and reading `~/.ssh`, `~/.codex/auth.json` or `~/.claude` credentials.

Hook modes: every non-committer run (planner, coder, reviewers, side questions, usage) uses mode `agent`, which also denies `git commit`, `push`, `merge`, `rebase`, `reset`, `stash`, `cherry-pick`, `am`, `tag`, `--amend` and `update-ref`. The commit run uses mode `committer`, which allows commit and push but keeps every other deny-list rule (force-push, signing bypass, credential reads, deletes outside the worktree). The hook fails closed on any internal error. Known gaps are listed in `docs/laya.md`.

Hook wiring: `hooks.py` is invoked as `python -m workforce.decider.hooks <agent>`. It is registered for Claude via a per-run `--settings` JSON containing a `PreToolUse` hook, and for Codex via its `hooks` feature (stable in 0.158; the build agent verifies the config format and documents it in `docs/laya.md`).

## 10. v3: parallel + console

**Scheduler** (`scheduler.py`):
- Tasks whose `depends_on` are all merged run concurrently. The limit is `[limits] parallel = 2` (add that key).
- Each task has its own worktree.
- Merges go through a **merge queue**, one at a time, oldest-approved first. If the base moved, the task is rebased onto the new base by the coder (resume session). The rebase goes through the Claude committer, since it rewrites commits. Then checks run again, and **both reviews are voided and redone** before the merge.
- After each merge, run the full checks on main. If they fail, stop the queue and create a Question.

**Console** (`console/`), run with `workforce` or `wf` and no arguments. It looks and feels like the Claude Code CLI:

```
   ▄███████▄     WorkForce Agent v0.1
  ██▀ ▄▄▄ ▀██    Claude opus-5-5 · high │ Codex gpt-6-astra · high
  ██ █<  >█ ██   Claude Team · ChatGPT Business · no API keys ✓
   ██▄▀▀▀▄██     ~/code/my-app
     ▀███▀
       ▀

  ⚠ Claude 5-hour window at 51%. Still working.
  ❓ 1 question waiting (Q3), type /answer Q3 …

  [plan]  codex   gpt-6-astra · high    ✓ plan.md: 3 tasks
  [T1]    review  opus-5-5 → APPROVE · gpt-6-astra → APPROVE @ 3f9c2a1
  [T3]    coding  sonnet-5-5 · high     3m02s

 ─────────────────────────────────────────────────────────────────────────
 ❯ Try "add CSV export to the reports page" or @codex / @claude …
 ─────────────────────────────────────────────────────────────────────────
  opus-5-5 · high │ ~/my-app                Claude 5h 51% · wk 33% │ Codex wk 4%
  ▸▸ auto · running T1, T3 · shift+tab: auto ⇄ step · /help
```

Console details:
- **Logo:** the block letters `WF` (`workforce/console/logo.py`). Draw it as truecolor half-block ANSI art with a vertical gradient from `#5AA8FF` (top) to `#2F6FE0` (bottom). Fall back to 256-colour if `COLORTERM` isn't truecolor.
- **Input:**
  - plain text → a new goal if idle, otherwise a note appended to `decisions.md`, which agents pick up at their next step
  - `@claude …` / `@codex …` → a side question in a fresh read-only session; the answer is printed and doesn't disturb running work
  - `/run <goal>`, `/status`, `/answer <Q> <text>`, `/pause`, `/resume`, `/models`, `/effort <role|task> <level>`, `/log [task]`, `/computer on|off <task>`, `/help`, `/quit`
- History file at `~/.workforce_history`, and tab completion for commands and task/question ids.
- **Shift+Tab** toggles `auto ⇄ step`. In step mode, it asks before starting each task.
- The event log streams above the input box without breaking it (prompt_toolkit `patch_stdout`).
- The console runs the orchestrator in a background thread and communicates only through `state.py` and `events.py`.

## 11. CLI (`cli.py`)

`workforce init | run "<goal>" [--resume] [--mode auto|step] | status | usage | questions | answer <id> "<text>" | models [set <role> <model> [effort]] | pause | resume | log [task]`. With no subcommand it opens the console (v3); in v1/v2 it prints status. `wf` is the same entry point.

## 12. Testing rules

- **Unit and integration tests use `tests/fakes/fake_claude.py` and `fake_codex.py`.** These are executable Python scripts that accept the same flags as the real CLIs and emit the exact event shapes in section 2. They're driven by a scenario file given in env var `WF_FAKE_SCENARIO`: a JSON list of scripted responses per role, including errors, schema failures, `REQUEST_CHANGES` rounds, rate-limit values and model-unavailable cases. They must never call the network.
- **Every module ships tests.** `.venv/bin/pytest -q` must pass before a change is considered done.
- **Required end-to-end fake scenarios:**
  - happy path
  - review disagreement → fix → approve
  - 3 failed rounds → Question
  - reviewer crash (never counts as approval)
  - model unavailable → pause
  - API key env set → refuse
  - commit tree mismatch → re-review
  - usage 50% alert and 60% pause/auto-resume with a fake clock
  - weekly pause needs the user
  - Laya unsure → fallback behaviour
  - v3: two independent tasks in parallel + merge-queue rebase
- **Real smoke tests:** run on a throwaway repo under `/private/tmp` with the cheapest models.
