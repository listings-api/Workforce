# WorkForce Agent: workspace mode (spec addendum)

This extends `docs/SPEC.md`. Everything in SPEC.md still holds unless this file overrides it, especially section 1, the hard rules.

## 1. Goal

`wf` works like the Claude Code or Codex CLI: start it in any folder and it sees the whole workspace. A workspace is a folder containing several git repos (e.g. `~/code` with `app`, `billing`, `sdk`…). One goal can produce tasks in several repos, with cross-repo dependencies. A plain single repo is simply a workspace of one repo, and keeps working exactly as today, apart from the integration-branch change in section 4.

## 2. New hard rules (added to SPEC.md section 1)

7. **Never touch the user's own checkouts.** WorkForce never checks out, resets, merges into or edits the user's working copy of any repo. All work happens in worktrees under `~/.workforce/worktrees/`, and the user's checkout doesn't need to be clean.
8. **Never write to a base branch.** Work lands only on an integration branch created by WorkForce (default `wf/<slug>`). `master`, `main` and the repo's base branch are never merged into, committed to or pushed by WorkForce.
9. **An agent writes only inside its own task worktree.** The risk-gate hook hard-denies Write/Edit/NotebookEdit/apply_patch, and shell redirections or `cp`/`mv`/`rm`/`tee` targets, that are outside `WF_WORKTREE`. `/tmp` and `$TMPDIR` are allowed. Other repos can be *read* for context.
10. **Pushing and PRs are explicit.** Nothing is pushed during a run. `wf publish` (a user command) asks the Claude committer, with the user's own setup, to push each integration branch and open PRs following the user's rules.

## 3. Finding the workspace (`workforce/workspace.py`)

`find_workspace(cwd: Path, home: Path | None = None) -> Workspace`:
1. Walk up from `cwd` to `/`. The **nearest** `workforce.toml` wins. Its folder is the workspace root. Record `focus` = the repo containing `cwd` (or None), which the planner is told about ("the user started wf inside `app/`").
2. No `workforce.toml` found and `cwd` is inside a git repo → a **single-repo workspace**: root = that repo's top level, `repos = {<dir name>: that repo}`. `NotInitialized` is raised only by commands that need config. `wf init` creates it.
3. No toml and not in a repo, but `discover(cwd)` finds repos → raise `NotInitialized(root=cwd, found=[…])`. The CLI prints: `This folder has N git repos (app, billing, …). Run wf init to set it up as a workspace.`
4. Otherwise raise `NoWorkspace(cwd)`: "not inside a git repo or a workspace".

`discover(root) -> list[RepoRef]`:
- Scan depth 1 and 2 for directories containing `.git` (dir or file).
- Skip hidden dirs, `node_modules`, `.venv`, `venv`, `vendor`, `dist` and `build`.
- Don't descend into a found repo; its submodules are not separate workspace repos.
- Sort by name. The name is the path relative to root (`app`, `tools/scanner`).

```python
@dataclass(frozen=True)
class RepoInfo:
    name: str            # relative path from the root, unique
    path: Path           # absolute path of the user's checkout (read-only for WorkForce)
    base: str | None     # configured base ref, or None = the repo's current branch at run start
    checks: Checks       # per-repo checks; empty strings = auto-detect

@dataclass(frozen=True)
class Workspace:
    root: Path                     # where .workforce/ and workforce.toml live
    repos: dict[str, RepoInfo]     # name -> repo
    focus: str | None              # repo containing the launch cwd
    single: bool                   # True when root is itself the only repo
    def repo(self, name: str) -> RepoInfo: ...   # KeyError → WorkspaceError naming the valid repos
```

## 4. Config additions (`workforce.toml`)

```toml
[workspace]                  # absent = single-repo workspace (root is the repo)
repos = ["app", "billing", "sdk"]   # written by `wf init` from discover(); editable
branch_prefix = "wf/"

[repos.app]                  # optional per repo
base = "master"              # optional; default = the repo's checked-out branch at run start
[repos.app.checks]           # optional; overrides [checks] for this repo
test = "bundle exec rspec"
```
- `[checks]` stays the default for all repos.
- A listed repo that doesn't exist or isn't a git repo → `ConfigError`.
- In single-repo mode `[workspace]` is absent, and `[repos.<name>]` may still set `base`/`checks` for the one repo.
- `wf init` in a folder that isn't a repo writes `[workspace]` with the discovered repos, and prints them with a hint to trim the list.
- `wf init` in a repo writes today's file, unchanged.

## 5. Branches and worktrees (both modes)

A run gets a **slug** from the goal: lowercase, `[a-z0-9-]`, at most 40 characters, plus `-MMDD`, e.g. `reviews-webhook-0929`.

For each repo the plan touches (the "involved repos"):
- **Base ref:** `repos.<name>.base`, or the repo's current branch. Record `base_sha` = the ref's commit at run start.
- **Integration branch:** `<branch_prefix><slug>` (e.g. `wf/reviews-webhook-0929`), created from `base_sha` with `git branch` (no checkout of the user's copy).
  - If it already exists → PreflightError.
  - On the model-pick screen the user may rename it (Enter keeps it).
- **Integration worktree:** `~/.workforce/worktrees/<ws_slug>/<run_id>/<repo_name>/_integration`, checked out on the integration branch.
- **Task branch:** `<branch_prefix><slug>-<task_id>` (a sibling, not a child ref, to avoid git ref-namespace clashes), created from the integration branch HEAD.
- **Task worktree:** `~/.workforce/worktrees/<ws_slug>/<run_id>/<repo_name>/<task_id>`.
- **Merge:** Python `git -C <integration worktree> merge --ff-only <task branch>`. The rebase in the merge queue rebases the task branch onto the integration branch (via the committer, as today).
- **Post-merge checks:** these run in the integration worktree with `env_root = RepoInfo.path` (for `.venv` detection).
- **End of run:** remove the task worktrees and the integration worktrees, and keep the integration branches. The summary prints, per repo, `branch wf/<slug>: N commits on top of <base> (<base_sha[:8]>)`, plus `Run wf publish to push and open PRs.`

`ws_slug` = `<root dir name>-<first 8 hex of sha1(abs root)>` (same rule as today's repo_slug).

## 6. State additions

- `Task.repo: str`: must be a workspace repo name. Old state files without it load with the single repo's name.
- `Run.slug: str`, `Run.integration: dict[str, IntegrationInfo]`. `IntegrationInfo(repo, base_ref, base_sha, branch, worktree, created: bool)`.
- `.workforce/` lives at the workspace root. If the root isn't a git repo, `Paths.ensure()` skips the git-exclude step. If it is (single-repo), it's excluded as today.

## 7. Planning

- The planner (Codex, `-s read-only`, `-C <root>`, plus `--skip-git-repo-check` when the root isn't a git repo) gets:
  - the goal, `decisions.md` and the focus repo;
  - per repo: name, path, base ref, top-level file list (≤ 40 entries), the first 30 lines of the README if any, and the detected languages/check commands.
- The **PLAN schema is built per run**: each task gets `"repo": {"type": "string", "enum": [<repo names>]}`, which is required.
  - `depends_on` may reference tasks in other repos.
  - Python validates this again: an unknown repo → schema error back to the planner.
- The plan reviewer (Claude, plan mode, cwd = root) sees the same repo overview.
- `plan.md` groups tasks by repo.
- The model-pick screen shows a repo column and the integration branch name per involved repo, and lets the user rename branches.

## 8. Coding and reviewing across repos

- The coder runs in its task worktree.
  - If the task depends on tasks in *other* repos, their integration worktrees are listed in the prompt ("already merged work you can read").
  - Claude coders get `--add-dir <that integration worktree>` for each. `AgentRequest.add_dirs: list[Path]`.
  - Writes are still confined to their own worktree (rule 9, enforced by the hook).
- Per-task double review is unchanged, but runs in the task worktree against its `base_sha` (the integration branch HEAD when the task started, updated on rebase as today).
- **Integration review** (new, runs once after all tasks merge):
  - Both reviewers run fresh and independently, read-only, in the workspace root, with `--add-dir` for every integration worktree (Claude) or `-C <root>` (Codex).
  - Inputs: the goal, the plan, and per involved repo `git diff <base_sha>..<integration branch>`, plus a file list.
  - They check that the pieces fit together (API contracts, field names, versions, config keys, migrations, call sites across repos), and return a `VERDICT`.
  - Both APPROVE → the run is done.
  - Otherwise the planner turns the merged findings into **fix tasks** (a PLAN_REVISION with repo-tagged new tasks). They go through the normal pipeline, and the integration review runs again.
  - This is capped at `review_rounds`, then a Question.

## 9. Parallelism

- One `limits.parallel` pool across all repos.
- One merge queue **per repo**, since different repos never conflict.
- A dependency across repos waits for the dependency task to be *merged* into its integration branch.

## 10. Preflight (replaces the clean-tree check)

For each **involved** repo:
- a git repo;
- the base ref resolves;
- the integration branch name is free;
- `git worktree add` works (no locked path).

The user's checkout may be dirty and on any branch. The env/CLI/login/version checks are unchanged and run once.

## 11. CLI and console

- `wf` / `workforce` with no args, from any folder:
  - `find_workspace`, then the console;
  - `NotInitialized` → the message from section 3;
  - `NoWorkspace` → a clear message.
- `wf init`:
  - in a repo → as today;
  - in a non-repo folder with repos → workspace init (section 4);
  - it prints the repo list and the checks detected per repo.
- `wf status`: a repo column in the tasks table, plus integration branches and commit counts per repo.
- `wf repos`: lists the workspace repos with base, current branch, dirty flag (informational only) and detected checks.
- `wf publish [--repo NAME]`:
  - for each integration branch with commits, run the Claude committer (user setup) in a temporary worktree of that branch;
  - prompt: push this branch and open a PR per the user's rules; never force-push; never push base branches;
  - result JSON `{pushed: bool, pr_url: str|null, error: str|null}`;
  - print a table.
  - Refuses while a run is active.
- The console header shows `Workspace ~/code · 3 repos` (or the repo path in single mode). Event lines carry the repo: `[app/T1] coding …`.

## 12. Tests

- Fakes only, as always.
- New fixture `git_workspace(tmp_path)`: a folder with 2–3 throwaway repos, each with one commit and `commit.gpgsign false` set locally (the test-only exception).
- Required scenarios:
  - discover/find_workspace (nearest toml, single repo, NotInitialized, NoWorkspace, focus)
  - config parsing with repos/base/per-repo checks, and errors
  - a two-repo plan where T2 in repo B depends on T1 in repo A → both merged into their integration branches, and the user checkouts are untouched (HEAD, branch and dirty file all unchanged)
  - integration review REQUEST_CHANGES → fix task → re-review → APPROVE
  - the hook denies a write outside the worktree; allows a write inside and to /tmp
  - an integration branch name clash → PreflightError
  - a dirty user checkout still runs
  - `wf publish` with the fake committer
  - an old single-repo state file loads
