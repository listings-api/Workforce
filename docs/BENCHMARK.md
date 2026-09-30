# Benchmark: the WorkForce team against a single agent

A reviewer asked whether the team's two reviews per commit are worth their overhead. `scripts/benchmark.py` measures it: completion time, usage and defects for the team against a single Claude Code agent. This page holds the method, the results of one small pilot, and what they do and do not show.

The pilot numbers below were produced by the command in "Rerun it" and pasted from `benchmark.py report`.

## Method

The same small tasks are given to two setups, each in a fresh throwaway git repo:

- **solo**: plain Claude Code (`claude -p`, no WorkForce plugin).
- **team**: `wf -p`, the real WorkForce path: Codex plans, Claude codes, and a commit needs both reviews to approve.

Both get the same prompt (the task text, then "commit when the tests pass"), the same Claude model and effort, the same permission mode and allowed tools, and JSON output. Each task is a tiny stdlib-only Python project with defects a review could catch, for example an off-by-one page helper, daylight-saving edge cases, a path-traversal check and cent rounding. A hidden unittest file, which is never copied into the agent's repo, is run against the final working tree to judge correctness. Team runs use a temporary `WF_HOME` whose `team.toml` sets the models below, so nothing in your own settings is read or changed.

Measured per run: wall-clock time; Claude's reported token usage and `total_cost_usd` (an API-equivalent estimate, not what a subscription is billed); the number and duration of Codex calls (from `team.log`); Codex token usage when the Codex session rollouts under `~/.codex/sessions` expose it; the change in Codex weekly usage percent (via `codex app-server`); whether a commit was made; hidden-test result; and review verdicts and findings (team only).

## Setup

| Setting | Value |
|---|---|
| Date | 2026-09-30T13:29:38+00:00 |
| Claude model / effort | claude-haiku-4-5-20251001 / low |
| Codex model / effort | gpt-6-luna / low |
| Tasks | bill_split, dst_schedule, pagination, static_files |
| Modes | solo, team |
| Repeats per task and mode | 1 |
| Per-run timeout | 25m00s |
| Claude Code | 2.1.285 (Claude Code) |
| Codex | codex-cli 0.158.0 |

## Per-run results

| Task | Mode | Run | Time | Commit | Hidden tests | Codex calls (time) | Reviews | Status |
|---|---|---|---|---|---|---|---|---|
| bill_split | solo | 1 | 2m24s | yes | FAIL (11/16) | – | – | ok |
| bill_split | team | 1 | 9m37s | yes | pass (16/16) | 3 (1m03s) | 6 reviews, 3 changes requested, 6 findings; last: claude APPROVE / codex APPROVE | ok |
| dst_schedule | solo | 1 | 4m47s | yes | pass (13/13) | – | – | ok |
| dst_schedule | team | 1 | 25m00s | no | pass (13/13) | 7 (2m45s) | 13 reviews, 8 changes requested, 20 findings; last: claude APPROVE / codex REQUEST_CHANGES | FAILED: timed out |
| pagination | solo | 1 | 1m43s | yes | FAIL (7/10) | – | – | ok |
| pagination | team | 1 | 10m01s | yes | FAIL (9/10) | 4 (1m24s) | 8 reviews, 4 changes requested, 9 findings; last: claude APPROVE / codex APPROVE | ok |
| static_files | solo | 1 | 3m01s | yes | FAIL (16/19) | – | – | ok |
| static_files | team | 1 | 25m00s | no | FAIL (16/19) | 7 (2m42s) | 14 reviews, 11 changes requested, 19 findings; last: claude APPROVE / codex REQUEST_CHANGES | FAILED: timed out |

### Usage per run

| Task | Mode | Claude in | Claude out | Cache read / write | Est. cost (API-equivalent) | Codex tokens | Codex weekly % |
|---|---|---|---|---|---|---|---|
| bill_split | solo | 107 | 6,405 | 549,480 / 26,335 | $0.1410 | – | – |
| bill_split | team | 317 | 16,999 | 2,236,611 / 72,931 | $0.4561 | 190,447 | 11.0% → 11.0% |
| dst_schedule | solo | 171 | 10,892 | 998,925 / 35,734 | $0.2272 | – | – |
| dst_schedule | team | n/a | n/a | n/a / n/a | n/a | 443,851 | 11.0% → 11.0% |
| pagination | solo | 83 | 5,674 | 388,656 / 21,654 | $0.1118 | – | – |
| pagination | team | 268 | 10,797 | 1,539,854 / 34,431 | $0.2783 | 204,441 | 11.0% → 11.0% |
| static_files | solo | 90 | 13,928 | 466,312 / 29,548 | $0.1767 | – | – |
| static_files | team | n/a | n/a | n/a / n/a | n/a | 427,815 | 11.0% → 11.0% |

Claude tokens are the `claude -p` result for the main agent only. The Claude reviewer that `claude_review` starts inside team runs is a separate headless Claude whose usage this result does not include. A run that timed out has no Claude figures (`n/a`), because the result is only printed when Claude finishes.

## Totals

| Measure | solo | team |
|---|---|---|
| Runs (failed) | 4 (0) | 4 (2) |
| Commits made | 4 | 2 |
| Hidden tests passed | 1/4 | 2/4 |
| Time, total | 11m55s | 69m38s |
| Time, mean per run | 2m59s | 17m25s |
| Time, median per run | 2m42s | 17m30s |
| Claude input tokens | 451 | 585 |
| Claude output tokens | 36,899 | 27,796 |
| Claude cache read tokens | 2,403,373 | 3,776,465 |
| Est. cost, Claude main agent (API-equivalent) | $0.6567 | $0.7344 |
| Codex calls (time) | 0 (0s) | 21 (7m55s) |
| Claude reviewer calls | 0 | 21 |
| Codex tokens | n/a | 1,266,554 |
| Codex weekly % change (sum) | n/a | +0.00 points |
| Review findings | n/a | 54 |
| Reviews that requested changes | 0 | 26 |

### Time, team against solo

| Task | Solo (mean) | Team (mean) | Team / solo |
|---|---|---|---|
| bill_split | 2m24s | 9m37s | 4.02x |
| dst_schedule | 4m47s | 25m00s | 5.23x |
| pagination | 1m43s | 10m01s | 5.82x |
| static_files | 3m01s | 25m00s | 8.28x |

## Caveats

- The sample is tiny and not statistically significant: a few tasks, few or single runs, and model output varies from run to run. Treat every number as an anecdote, not a benchmark result.
- The models are cheap ones chosen to keep the run affordable. They may behave differently from the models used day to day; rerun with your own models before drawing conclusions.
- Subscription usage is coarse: the Codex weekly percent moves in whole steps and other use of the same account during a run adds noise; Claude subscription usage is not read at all. The cost figure is Claude's own API-equivalent estimate and is not a bill.
- The Claude reviewer's usage inside team runs is not included in the Claude token and cost figures, so team Claude usage is understated.
- Correctness is judged only by the hidden tests, which cover the planted edge cases. They say nothing about code quality, and they cannot show what a review changed along the way, because the state before the review is not captured.
- Both modes load your own global Claude Code settings, and Claude Code and Codex versions can change results. A failed or timed-out run is kept in the tables and totals as a failure.

## What the pilot shows

These are observations about one run of each cell, not findings.

- **The team was several times slower.** Every team run took 4x to 8x the solo time, and two of the four hit the 25-minute limit. Both timed-out runs (`dst_schedule`, `static_files`) were review loops: Codex (`gpt-6-luna`, low effort) kept answering REQUEST_CHANGES with one or two new findings each round (8 and 11 requests in total), and the Claude reviewer approved several times in between. The commit gate stayed closed, so nothing was committed. In `dst_schedule` the working tree passed all hidden tests long before the limit.
- **Cheap models probably make the loop worse.** Haiku at low effort fixes one finding, and the next review raises another. A stronger coder or a stronger reviewer may converge faster, or may not. The pilot cannot say.
- **The reviews did not reliably find the hidden defects.** In `bill_split` the team passed all 16 hidden tests and the solo agent failed 5 (missing input validation). In `pagination` both modes returned a tuple where the task asked for a list; the team's final reviews approved it and the solo run had two more failures. In `static_files` both modes failed three hidden tests (the solo run committed; the team run never did). With one run per cell, nothing here tells a real effect from luck.
- **Usage: on the two team runs that finished, Claude's main agent cost about 2.9x the estimate of the matching solo runs** ($0.73 against $0.25 for `bill_split` and `pagination`; API-equivalent estimates, not bills). The two timed-out team runs have no Claude figures, and team runs also start a separate Claude reviewer for every review, which is not measured at all, so the real gap is larger. Codex used about 1.27 million tokens (mostly cached input) across 21 calls. The Codex weekly percentage did not move at this resolution (11% before and after).
- **Headless runs cannot ask the user.** The team rules say to ask the user when the reviewers disagree after two rounds. In `-p` mode nobody answers, so the loop continues until the timeout. Benchmarks of the team therefore measure the cost of unresolved disagreement as well as the cost of a review.

## Rerun it

```
.venv/bin/python scripts/benchmark.py list
.venv/bin/python scripts/benchmark.py run --mode both --repeat 1 \
    --claude-model claude-haiku-4-5-20251001 --claude-effort low \
    --codex-model gpt-6-luna --codex-effort low \
    --timeout 1500 --out results.json
.venv/bin/python scripts/benchmark.py report results.json > report.md
```

The pilot took about 1 hour 20 minutes. To measure the models you use every day, pass them instead (for example `--claude-model claude-opus-5-5 --claude-effort high --codex-model gpt-6-sol --codex-effort high`), raise `--repeat` to 3 or more, and allow for a much longer and heavier run on your subscription. `--mode solo` or `--mode team` runs one side only, `--tasks a,b` picks tasks, and `--workdir DIR` keeps the throwaway repos for inspection. The script refuses to start when an API key variable is set.

## Tasks

Each folder in `scripts/bench_tasks/` holds `seed/` (the starting project, copied into a fresh git repo), `task.md` (what the user asks), `hidden_test.py` (judges the result; never copied into the repo) and `reference/` (a known-good solution used only by the unit tests to check that the hidden tests pass on a correct answer and fail on plausible mistakes).

| Task | What it asks for | Planted or likely defects |
|---|---|---|
| `pagination` | `paginate()` for a product list | the seed's `page_bounds` helper is wrong for 1-based pages; exact multiples; empty lists; boolean and float page numbers; returning the input type instead of a list |
| `dst_schedule` | daily reminder times in an IANA timezone | the seed's `add_days` shifts the wall clock across daylight-saving changes; clock gaps and repeats; a 30-minute shift; unknown zone names |
| `static_files` | a static file server function | the seed's `safe_join` accepts a sibling folder that shares the root's prefix; encoded dots; symlinks out of the root; dotted file names that are not traversal |
| `bill_split` | split a bill without losing a cent | the seed's `share` rounds each share separately; ties; huge amounts (floats lose cents); input validation |

## Adding a task

Create `scripts/bench_tasks/<name>/` with `seed/` (stdlib only, with its own passing tests), `task.md`, `hidden_test.py` and, so `tests/test_benchmark.py` can check the hidden tests, a `reference/` solution. The task text should say what the code must do, not list the hidden cases.
