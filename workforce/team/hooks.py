"""Claude Code hooks for the WorkForce plugin: `python -m workforce.team.hooks pretooluse|userpromptsubmit`.

PreToolUse: the commit gate (`git commit` and every other command that writes history needs both reviewers' APPROVE for
the exact tree that would be committed; history rewrites are refused), the protection of the signed approvals and their
key, the scan of written and run scripts for bypass words, the always-deny list (no signing bypass, no force-push) and the
usage stop. UserPromptSubmit: the usage stop, the
usage alert, the `/wf-continue` override and the `@codex` relay.

Errors fail CLOSED for commit checks (deny with the reason) and OPEN for everything else, so a bug here never bricks the
session. Errors are logged to `<home>/.workforce/team.log`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from workforce.team import gitgate

EVENTS = ("pretooluse", "userpromptsubmit")
CONTINUE_COMMANDS = ("/wf-continue", "/workforce:wf-continue")
REVIEW_AGAIN_COMMANDS = ("/wf-review-again", "/workforce:wf-review-again")
REVIEW_AGAIN_FILE = "review-again.json"
REVIEWERS = (("claude", "the Claude reviewer (claude_review)"), ("codex", "Codex (codex_review)"))
_GIT_WORD = re.compile(r"git|gpg|\bgh\b", re.I)
MAX_OVERRIDES = 8
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
PATH_TOOLS = frozenset({"Read", "Grep", "Glob", "NotebookRead", "LS"}) | WRITE_TOOLS
DIR_TOOLS = frozenset({"Grep", "Glob", "LS"})


def looks_like_history(text: str) -> bool:
    """True when the raw text mentions git next to a history-writing word (the fail-closed check for unreadable input)."""
    lowered = gitgate.normalise(text)
    return "git" in lowered and bool(gitgate._HISTORY_LOOSE.search(lowered) or gitgate._GIT_AM_LOOSE.search(lowered))


def _log(home: Path | None, **fields: Any) -> None:
    try:
        from workforce.team import config

        wf_dir = config.workforce_dir(home)
        wf_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(wf_dir / "team.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), "source": "hook", **fields}) + "\n")
    except OSError:
        pass


def _log_exception(home: Path | None, event: str, exc: BaseException) -> None:
    _log(home, event=event, error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc(limit=6)[-1500:])


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _block(reason: str) -> dict[str, Any]:
    return {"decision": "block", "reason": reason}


def _context(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def _cwd(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else env.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _missing(status: Mapping[str, Any]) -> list[str]:
    listed = [str(item) for item in status.get("missing") or []]
    if any("bad signature" in item or "reviewed against" in item for item in listed):
        return listed
    missing = []
    for key, label in REVIEWERS:
        entry = status.get(key)
        verdict = entry.get("verdict") if isinstance(entry, Mapping) else None
        if verdict != "APPROVE":
            missing.append(f"{label}: {'no review of these exact changes' if verdict is None else verdict}")
    return missing


def commit_reason(status: Mapping[str, Any]) -> str | None:
    """None when the commit may go ahead, else the deny reason naming each reviewer that has not approved."""
    missing = _missing(status)
    if status.get("commit_allowed") is True and not missing:
        return None
    missing = missing or [str(item) for item in status.get("missing") or ["review status unavailable"]]
    return (
        "run /review: the Claude reviewer and Codex must both approve these exact changes before a commit "
        f"(any edit voids an approval). Missing: {'; '.join(missing)}."
    )


def analyse_bash(command: str, cwd: str, home: Path | None = None, env: Mapping[str, str] | None = None) -> gitgate.Analysis:
    """The always-deny reason (or None) and the git operations in a Bash command that need both reviewers' approval."""
    if not _GIT_WORD.search(command):
        return gitgate.Analysis()
    from workforce.team import denylist as risk

    for text in (command, risk._normalise(command)):
        if risk._NO_GPG.search(text):
            return gitgate.Analysis("disables commit signing (--no-gpg-sign / commit.gpgsign=false). Signing is never bypassed.")
        if risk._GIT_ENV.search(text):
            return gitgate.Analysis("overrides git config through GIT_CONFIG_* environment variables.")
    return gitgate.analyse(command, cwd, Path(home) if home is not None else Path.home(), env)


def op_status(op: gitgate.Op, home: Path | None) -> tuple[dict[str, Any], str]:
    """(review status, note) for the tree `op` would commit: the staged tree, or the working state when everything is staged first."""
    from workforce.team import approvals

    target, cwd = op.target, Path(op.target.cwd)
    stage = op.stage
    if stage == "working" and op.dot_only and not gitgate.top_level(target):
        stage = "index"
    if stage == "working":
        tree = approvals.current_tree(cwd, target.env)
        if approvals.stage_all_tree(cwd, target.env) != tree:
            status = approvals.status(cwd, home, tree=tree, git_env=target.env)
            status = {**status, "commit_allowed": False, "missing": ["the index holds content the reviewed working state does not (a force-added ignored file, or skip-worktree entries)"]}
            return status, "Unstage that content (git reset -- <path>) or add it to the reviewed changes, then run /review again."
        return approvals.status(cwd, home, tree=tree, git_env=target.env), ""
    tree = approvals.index_tree(cwd, target.env)
    status = approvals.status(cwd, home, tree=tree, git_env=target.env)
    if status.get("commit_allowed") is True:
        return status, ""
    note = f"The commit would record the staged (index) tree {tree[:12]}; reviews are of the working state."
    try:
        reviewed = approvals.current_tree(cwd, target.env)
        if reviewed != tree and approvals.status(cwd, home, tree=reviewed, git_env=target.env).get("commit_allowed") is True:
            note = (
                f"The working state is approved, but the staged (index) tree {tree[:12]} differs from it. "
                "Stage everything that was reviewed in the same command (`git add -A && git commit …`), or run `git add -A` first."
            )
    except Exception:
        pass
    return status, note


def nothing_staged(target: gitgate.Target) -> bool:
    """True when the index matches HEAD, so a merge, cherry-pick or revert can only record commits that already exist."""
    from workforce.team import approvals

    cwd = Path(target.cwd)
    return approvals.index_tree(cwd, target.env) == approvals._git(cwd, "rev-parse", "HEAD^{tree}", env=target.env)


def move_reason(op: gitgate.Op, home: Path | None) -> str | None:
    """None when `op` points a branch at a commit already on this branch or on a remote branch, or at one whose files both reviewers approved."""
    from workforce.team import approvals

    cwd, env = Path(op.target.cwd), op.target.env
    try:
        commit = approvals._git(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{op.rev}^{{commit}}", env=env)
    except approvals.GitError:
        if op.rev_may_be_path:
            return None
        return f"WorkForce blocked this command: `{op.rev}` is not a commit it can check for this {op.label}."
    try:
        approvals._git(cwd, "merge-base", "--is-ancestor", commit, "HEAD", env=env)
        return None
    except approvals.GitError:
        pass
    if approvals._git(cwd, "for-each-ref", f"--contains={commit}", "--count=1", "--format=%(refname)", "refs/remotes/", env=env):
        return None
    tree = approvals._git(cwd, "rev-parse", f"{commit}^{{tree}}", env=env)
    reason = commit_reason(approvals.status(cwd, home, tree=tree, git_env=env))
    if reason is None:
        return None
    here = approvals.head_commit(cwd, env)
    return (
        f"`{op.label}` would point a branch at {commit[:12]}, which is not on this branch or any remote branch. {reason} "
        f"To review that commit: `git checkout --detach {commit[:12]}`, run claude_review and codex_review with "
        f"base = {(here or 'HEAD')[:12]} (the commit you are on now), switch back, then run the command again."
    )


def _protect_bash(command: str) -> dict[str, Any] | None:
    reason = gitgate.protects_approvals(command)
    return _deny(f"WorkForce blocked this command: {reason}.") if reason else None


def _protect_tool(tool_name: str, tool_input: Mapping[str, Any], home: Path | None, cwd: str) -> dict[str, Any] | None:
    """Deny file tools that would write the signed approvals, or read/search the approvals key."""
    from workforce.team import denylist as risk
    from workforce.team import approvals, config

    home_dir = Path(home) if home is not None else Path.home()
    workforce_dir = os.path.realpath(config.workforce_dir(home))
    for raw in risk._file_paths(tool_input):
        squashed = re.sub(r"[\s'\"\\+]", "", raw.lower())
        if "team.key" in squashed:
            return _deny("WorkForce blocked this: the approvals key (~/.workforce/team.key) is never read or written by the agent.")
        resolved = risk._resolve(raw, cwd, home_dir) or raw
        if tool_name in WRITE_TOOLS and os.path.basename(resolved).startswith(approvals.FILENAME):
            return _deny(
                "WorkForce blocked this: wf-approvals.json is written only by the WorkForce server "
                "(claude_review / codex_review), never by the agent."
            )
        if tool_name in WRITE_TOOLS and os.path.basename(resolved).startswith(REVIEW_AGAIN_FILE):
            return _deny("WorkForce blocked this: only the user can allow more review rounds, by typing /wf-review-again.")
        if tool_name in WRITE_TOOLS and os.path.basename(resolved).startswith("plan-reviews.json"):
            return _deny("WorkForce blocked this: plan-reviews.json is written only by the WorkForce server (codex_plan_review), never by the agent.")
        if tool_name in WRITE_TOOLS and os.path.basename(resolved) == "REVIEW-LOG.md" and "/plans/" in resolved.replace(os.sep, "/"):
            return _deny("WorkForce blocked this: a plan's REVIEW-LOG.md holds Codex's verbatim replies and is appended only by the WorkForce server.")
        if tool_name in DIR_TOOLS and risk._inside(resolved, workforce_dir):
            return _deny("WorkForce blocked this: ~/.workforce holds the approvals key, which is never read or searched by the agent.")
    return None


BYPASS_TAIL = "which can switch off or get around the commit review."


def _new_content(tool_input: Mapping[str, Any]) -> list[str]:
    """The text a Write/Edit/MultiEdit/NotebookEdit call would put into a file."""
    pieces = [tool_input[key] for key in ("content", "new_string", "new_source") if isinstance(tool_input.get(key), str)]
    for edit in tool_input.get("edits") or []:
        if isinstance(edit, Mapping):
            pieces.extend(edit[key] for key in ("new_string", "new_source") if isinstance(edit.get(key), str))
    return pieces


def _scan_write(tool_name: str, tool_input: Mapping[str, Any]) -> dict[str, Any] | None:
    """Deny writing code that carries a commit-review bypass; prose files (.md, .txt, .rst) may mention these words."""
    from workforce.team import scriptscan

    if tool_name not in WRITE_TOOLS:
        return None
    from workforce.team import denylist as risk

    paths = risk._file_paths(tool_input)
    if paths and all(scriptscan.is_doc(path) for path in paths):
        return None
    for text in _new_content(tool_input):
        found = scriptscan.scan_text(text)
        if found:
            return _deny(
                f"WorkForce blocked writing this file: it contains {found}, {BYPASS_TAIL} "
                "If you really need it, ask the user to make this change by hand."
            )
    return None


def _scan_scripts(command: str, cwd: str) -> dict[str, Any] | None:
    """Deny a Bash command that runs a script (or build file) containing a bypass, or names a script it may be creating in the same command."""
    from workforce.team import scriptscan

    for path in scriptscan.scripts_run_by(command, cwd):
        found = scriptscan.scan_file(path)
        if found:
            return _deny(
                f"WorkForce blocked this command: {path.name} contains {found}, {BYPASS_TAIL} "
                "If you really need it, ask the user to run it by hand."
            )
    missing = scriptscan.missing_scripts(command, cwd)
    found = scriptscan.scan_text(command) if missing else None
    if found:
        return _deny(
            f"WorkForce blocked this command: it runs {missing[0].name}, which it may be writing itself, and the command contains {found}, "
            f"{BYPASS_TAIL} If you really need it, ask the user to run it by hand."
        )
    return None


def _bash_decision(command: str, cwd: str, home: Path | None, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    try:
        protected = _protect_bash(command)
    except Exception as exc:
        _log_exception(home, "pretooluse:protect", exc)
        protected = None
    if protected:
        return protected
    try:
        analysis = analyse_bash(command, cwd, home, env)
    except Exception as exc:
        _log_exception(home, "pretooluse:bash", exc)
        if looks_like_history(command):
            return _deny(f"WorkForce could not check this git command ({type(exc).__name__}), so it is blocked. Run /review and try again.")
        return None
    if analysis.deny:
        return _deny(f"WorkForce blocked this command: {analysis.deny}")
    seen: set[tuple] = set()
    for op in analysis.ops:
        marker = (op.target.key(), op.stage, op.dot_only, op.rev)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            if op.rev is not None:
                moved = move_reason(op, home)
                if moved:
                    return _deny(moved)
                continue
            if op.clean_ok and nothing_staged(op.target):
                continue
            status, note = op_status(op, home)
        except Exception as exc:
            _log_exception(home, "pretooluse:commit", exc)
            return _deny(
                f"WorkForce could not verify the reviews for this {op.label} ({type(exc).__name__}: {exc}). "
                f"If {op.target.cwd} is not a git repository yet, create it first. Run /review and try again."
            )
        reason = commit_reason(status)
        if reason:
            return _deny(f"{reason} {note}".strip())
    return None


def _when(resets_at: Any) -> str:
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
        return time.strftime("%a %H:%M", time.localtime(resets_at))
    return "an unknown time"


def _usage_state(home: Path | None, now: float | None) -> dict[str, Any] | None:
    from workforce.team import config, usage_cache

    return usage_cache.state(config.load(home), home, now)


def _stop_reason(state: Mapping[str, Any]) -> str:
    percent = state.get("percent")
    shown = f" at {round(percent)}%" if isinstance(percent, (int, float)) else ""
    return (
        f"WorkForce usage stop: {state.get('window')}{shown}, resets {_when(state.get('resets_at'))}. "
        "Run /wf-continue to carry on for this window anyway, or wait for the reset."
    )


def _is_continue(prompt: str) -> bool:
    words = prompt.strip().split(None, 1)
    return bool(words) and words[0] in CONTINUE_COMMANDS


def _allow_more_reviews(cwd: str, home: Path | None) -> str:
    """Record that the user typed /wf-review-again, which lifts the review round limit for this project."""
    from workforce.team import config, relay

    try:
        folder = relay.ensure_team_dir(cwd)
        config.atomic_write(folder / REVIEW_AGAIN_FILE, json.dumps({"at": time.time()}))
    except OSError as exc:
        _log_exception(home, "userpromptsubmit:review-again", exc)
        return f"WorkForce could not record /wf-review-again ({exc}). Tell the user."
    return (
        "WorkForce: the user allowed more review rounds (/wf-review-again). The round limit is reset: run claude_review "
        "and codex_review on the current changes again, and follow the user's instructions on the disputed findings."
    )


def _apply_override(home: Path | None, now: float | None) -> str:
    from workforce.team import usage_cache

    done: list[str] = []
    for _ in range(MAX_OVERRIDES):
        state = _usage_state(home, now)
        if not state or state.get("level") != "stop":
            break
        resets_at = state.get("resets_at")
        usage_cache.override_set(str(state["window_key"]), int(resets_at or 0), home)
        done.append(f"{state['window']} (until {_when(resets_at)})")
    if done:
        return "WorkForce usage override recorded for: " + "; ".join(done) + ". Tell the user, then continue their work."
    return "WorkForce: no usage window is over the stop limit, so there was nothing to override. Tell the user."


def _usage_pretool(home: Path | None, now: float | None) -> dict[str, Any] | None:
    try:
        state = _usage_state(home, now)
        if state and state.get("level") == "stop":
            return _deny(_stop_reason(state))
    except Exception as exc:
        _log_exception(home, "pretooluse:usage", exc)
    return None


def _usage_prompt(payload: Mapping[str, Any], home: Path | None, now: float | None) -> dict[str, Any] | None:
    prompt = payload.get("prompt")
    try:
        if isinstance(prompt, str) and _is_continue(prompt):
            return _context(_apply_override(home, now))
        state = _usage_state(home, now)
        if not state:
            return None
        if state.get("level") == "stop":
            return _block(_stop_reason(state))
        if state.get("level") == "alert":
            return _context(f"usage alert: {state.get('text') or state.get('window')}. Mention it to the user briefly.")
    except Exception as exc:
        _log_exception(home, "userpromptsubmit", exc)
    return None


def _codex_stop(home: Path | None, now: float | None) -> str | None:
    """The refusal for an `@codex` line when a Codex usage window is at the stop limit with no override; Claude's windows do not count."""
    from workforce.team import config, usage_cache

    try:
        state = usage_cache.state(config.load(home), home, now, sources=("codex",))
    except Exception as exc:
        _log_exception(home, "userpromptsubmit:codex-usage", exc)
        return None
    if state.get("level") == "stop":
        return f"Codex: your @codex line was not sent. {_stop_reason(state)}"
    return None


def _direct_codex(payload: Mapping[str, Any], env: Mapping[str, str], home: Path | None, now: float | None = None) -> dict[str, Any] | None:
    """`@codex <text>`: send the exact text to Codex, block the prompt (Claude never sees it), show Codex's reply verbatim."""
    from workforce.team import config, relay

    text = relay.parse_direct(payload.get("prompt"))
    if text is None:
        return None
    stop = _codex_stop(home, now)
    if stop:
        return _block(stop)
    project = _cwd(payload, env)
    try:
        reason = relay.direct_codex(config.load(home), project, text)
    except Exception as exc:
        _log_exception(home, "userpromptsubmit:codex", exc)
        reason = f"Codex: the @codex line was not sent ({type(exc).__name__}: {exc}). Claude did not receive it either."
    _log(home, event="userpromptsubmit:codex", chars=len(text))
    return _block(reason)


def _with_thread(output: dict[str, Any] | None, payload: Mapping[str, Any], env: Mapping[str, str], home: Path | None) -> dict[str, Any] | None:
    """Add the direct user↔Codex entries Claude has not seen yet, verbatim, to a normal prompt's additionalContext."""
    if output and output.get("decision") == "block":
        return output
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and _is_continue(prompt):
        return output
    try:
        from workforce.team import relay

        unseen = relay.take_unseen(_cwd(payload, env))
    except Exception as exc:
        _log_exception(home, "userpromptsubmit:thread", exc)
        return output
    if not unseen:
        return output
    existing = ((output or {}).get("hookSpecificOutput") or {}).get("additionalContext")
    return _context(f"{existing}\n\n{unseen}" if existing else unseen)


def evaluate(
    event: str,
    payload: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """The hook's JSON output for `payload`, or None to allow silently."""
    env = os.environ if env is None else env
    if event == "userpromptsubmit":
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and prompt.strip().split(None, 1)[:1] and prompt.strip().split(None, 1)[0] in REVIEW_AGAIN_COMMANDS:
            return _context(_allow_more_reviews(_cwd(payload, env), home))
        direct = _direct_codex(payload, env, home, now)
        if direct:
            return direct
        return _with_thread(_usage_prompt(payload, home, now), payload, env, home)
    tool_input = payload.get("tool_input")
    tool_name = payload.get("tool_name")
    if isinstance(tool_input, Mapping):
        if tool_name in PATH_TOOLS:
            try:
                decision = _protect_tool(tool_name, tool_input, home, _cwd(payload, env))
            except Exception as exc:
                _log_exception(home, "pretooluse:protect", exc)
                decision = None
            if decision:
                return decision
            try:
                decision = _scan_write(tool_name, tool_input)
            except Exception as exc:
                _log_exception(home, "pretooluse:scriptscan", exc)
                decision = None
            if decision:
                return decision
        if tool_name == "Bash" and isinstance(tool_input.get("command"), str):
            try:
                decision = _scan_scripts(tool_input["command"], _cwd(payload, env))
            except Exception as exc:
                _log_exception(home, "pretooluse:scriptscan", exc)
                decision = None
            if decision:
                return decision
            decision = _bash_decision(tool_input["command"], _cwd(payload, env), home, env)
            if decision:
                return decision
    return _usage_pretool(home, now)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    now: float | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    if len(args) != 1 or args[0] not in EVENTS:
        print(f"usage: python -m workforce.team.hooks {'|'.join(EVENTS)}", file=stderr)
        return 2
    raw = stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except ValueError as exc:
        _log(home, event=args[0], error=f"unreadable payload: {exc}")
        if args[0] == "pretooluse" and looks_like_history(raw):
            print(json.dumps(_deny("WorkForce could not read this tool call, and it looks like a git commit, so it is blocked.")), file=stdout)
        return 0
    try:
        output = evaluate(args[0], payload, env=env, home=home, now=now)
    except Exception as exc:
        _log_exception(home, args[0], exc)
        output = None
    if output:
        print(json.dumps(output), file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
