"""Independent double review: two reviewers, fresh sessions, one reconciliation, verdict sanity checks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from workforce import git_ops, prompts, schemas
from workforce.agents import guard
from workforce.agents.base import AgentRequest, AgentResult, AgentRunner, failure
from workforce.config import Config
from workforce.decider.base import Decider
from workforce.decider.laya import can_answer, confident
from workforce.state import Finding, Review, Task

REVIEWER_NAMES = ("claude", "codex")
BLOCKING = ("blocker", "major")
SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}
MAX_DIFF_CHARS = 150_000
MAX_INTEGRATION_DIFF_CHARS = 150_000
MIN_REPO_DIFF_CHARS = 8_000
MAX_STAT_CHARS = 6_000
INTEGRATION_SCOPE = "integration"
CONSISTENCY_QUESTION = (
    "Does this review text match its verdict (for example APPROVE while listing a blocking bug)?"
)


def _no_run_hook(agent: str, result: AgentResult) -> None:
    return None


@dataclass
class ReviewContext:
    """Everything one double review needs. `log_path(step)` names the raw-stream log for a run.

    `progress` is the `ReviewOutcome.progress` of an earlier attempt that was cut short; verdicts in it
    are reused, but only while `tree` and `base_sha` are unchanged. `after_run(agent, result)` is called
    after every agent run (the usage monitor's hook).
    """

    worktree: Path
    task: Task
    base_sha: str
    tree: str
    checks_summary: str
    decisions: str
    config: Config
    runners: dict[str, AgentRunner]
    decider: Decider
    log_path: Callable[[str], Path | None]
    emit: Callable[..., None]
    gate: Callable[[], str | None]
    progress: dict | None = None
    after_run: Callable[[str, AgentResult], None] = _no_run_hook
    repo: Path | None = None


@dataclass
class ReviewOutcome:
    approved: bool
    blocked: bool
    reviews: list[Review]
    merged_findings: list[dict]
    final_reviews: list[Review] = field(default_factory=list)
    infra_error: str | None = None
    model_unavailable: str | None = None
    pause_reason: str | None = None
    head_moved: str | None = None
    subscription_violation: str | None = None
    tree_changed: bool = False
    summaries: dict[str, str] = field(default_factory=dict)
    progress: dict | None = None


@dataclass
class _Run:
    name: str
    result: AgentResult
    review: Review | None
    summary: str


class _Abort(Exception):
    def __init__(self, **fields):
        super().__init__(str(fields))
        self.fields = fields


def double_review(ctx: ReviewContext) -> ReviewOutcome:
    """Run both reviewers concurrently, reconcile once if needed, and decide approval.

    `approved` is true only when both final verdicts are APPROVE for `ctx.tree`. Infra failures
    (crash, timeout, auth, quota, malformed output) set `infra_error` and are never a verdict. If HEAD
    moved during any reviewer run, `head_moved` describes it: only the committer may commit, so nothing
    from that review counts. A reviewer refused for not being on the subscription login sets
    `subscription_violation` and is never retried.
    """
    recorded: list[Review] = []
    progress = _seed_progress(ctx)
    try:
        finals = _review_all(ctx, recorded, progress)
    except _Abort as abort:
        return ReviewOutcome(
            approved=False,
            blocked=False,
            reviews=recorded,
            merged_findings=[],
            progress=progress,
            **abort.fields,
        )
    return _outcome(ctx, recorded, finals)


def _seed_progress(ctx: ReviewContext) -> dict:
    earlier = ctx.progress
    if earlier and earlier.get("tree") == ctx.tree and earlier.get("base_sha") == ctx.base_sha:
        return copy.deepcopy(earlier)
    return {"tree": ctx.tree, "base_sha": ctx.base_sha, "firsts": {}, "seconds": {}}


def _restore(name: str, item: dict) -> _Run:
    saved = Review.from_dict(item["review"])
    stub = AgentResult(
        ok=True, text="", structured=None, session_id=saved.session, model=saved.model
    )
    return _Run(name, stub, saved, item["summary"])


def _stage(
    ctx: ReviewContext,
    progress: dict,
    key: str,
    recorded: list[Review],
    launch: Callable[[str], _Run],
) -> list[_Run]:
    """Run `launch(name)` for each reviewer without a saved verdict at this stage; reuse the others."""
    kept = progress[key]

    def job(name: str) -> _Run:
        if name not in kept:
            return launch(name)
        ctx.emit("note", text=f"reusing reviewer {name}'s {key[:-1]} verdict for this unchanged tree")
        run = _restore(name, kept[name])
        recorded.append(run.review)
        return run

    runs = _parallel([lambda n=name: job(n) for name in REVIEWER_NAMES])
    for run in runs:
        if run.review is not None and run.name not in kept:
            kept[run.name] = {"review": asdict(run.review), "summary": run.summary}
    return runs


def _review_all(ctx: ReviewContext, recorded: list[Review], progress: dict) -> dict[str, _Run]:
    _gate(ctx)
    prompt = _reviewer_prompt(ctx)
    firsts = _stage(ctx, progress, "firsts", recorded, lambda n: _fresh(ctx, n, prompt, recorded))
    _raise_failures(ctx, firsts)
    finals = {run.name: run for run in firsts}

    if _needs_reconciliation(finals):
        _gate(ctx)
        seconds = _stage(
            ctx, progress, "seconds", recorded, lambda n: _reconcile(ctx, n, finals, recorded)
        )
        _raise_failures(ctx, seconds)
        finals = {run.name: run for run in seconds}

    for name in REVIEWER_NAMES:
        finals[name] = _ensure_consistent(ctx, finals[name], prompt, recorded)
    return finals


def _outcome(ctx: ReviewContext, recorded: list[Review], finals: dict[str, _Run]) -> ReviewOutcome:
    reviews = [finals[name].review for name in REVIEWER_NAMES]
    summaries = {name: finals[name].summary for name in REVIEWER_NAMES}
    merged = _merge_findings(finals)
    tree_changed = git_ops.tree_hash(ctx.worktree) != ctx.tree
    verdicts = {review.verdict for review in reviews}
    same_tree = all(review.sha == ctx.tree for review in reviews)
    approved = verdicts == {"APPROVE"} and same_tree and not tree_changed
    blocked = "BLOCKED" in verdicts and not tree_changed
    return ReviewOutcome(
        approved=approved,
        blocked=blocked,
        reviews=recorded,
        merged_findings=merged,
        final_reviews=reviews,
        tree_changed=tree_changed,
        summaries=summaries,
    )


def _gate(ctx: "ReviewContext | IntegrationContext") -> None:
    reason = ctx.gate()
    if reason:
        raise _Abort(pause_reason=reason)


def _parallel(jobs: list[Callable[[], _Run]]) -> list[_Run]:
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(job) for job in jobs]
        return [future.result() for future in futures]


def _raise_failures(ctx: "ReviewContext | IntegrationContext", runs: list[_Run]) -> None:
    for run in runs:
        if run.result.error_kind == "model_unavailable":
            role = ctx.config.role(f"reviewer_{run.name}")
            raise _Abort(model_unavailable=role.model)
        if guard.is_subscription_violation(run.result):
            raise _Abort(subscription_violation=f"reviewer_{run.name}: {run.result.error}")
    problems = [
        f"reviewer_{run.name}: {run.result.error_kind or 'error'}: {run.result.error or 'no detail'}"
        for run in runs
        if run.review is None
    ]
    if problems:
        raise _Abort(infra_error="; ".join(problems))


def _diff_text(ctx: ReviewContext) -> str:
    diff = git_ops.diff_against(ctx.worktree, ctx.base_sha)
    if len(diff) > MAX_DIFF_CHARS:
        return (
            diff[:MAX_DIFF_CHARS]
            + f"\n... diff truncated after {MAX_DIFF_CHARS} characters; read the files themselves for the rest."
        )
    return diff if diff.strip() else "(no changes against the base commit)"


def _acceptance_text(task: Task) -> str:
    return "\n".join(f"- {item}" for item in task.acceptance) or "(none listed)"


def _reviewer_prompt(ctx: ReviewContext) -> str:
    return prompts.render(
        "reviewer",
        decisions=ctx.decisions,
        task_id=ctx.task.id,
        title=ctx.task.title,
        description=ctx.task.description,
        acceptance=_acceptance_text(ctx.task),
        tree=ctx.tree,
        base_sha=ctx.base_sha,
        checks=ctx.checks_summary,
        diff=_diff_text(ctx),
    )


def _request(ctx: ReviewContext, name: str, prompt: str, resume: str | None, step: str) -> AgentRequest:
    role_name = f"reviewer_{name}"
    role = ctx.config.role(role_name)
    return AgentRequest(
        role=role_name,
        agent=role.agent,
        model=role.model,
        effort=role.effort,
        prompt=prompt,
        cwd=ctx.worktree,
        schema=schemas.VERDICT,
        resume_session=resume,
        sandbox="read-only",
        browser=getattr(ctx.config.browser, role.agent),
        computer_use=ctx.task.computer_use,
        log_path=ctx.log_path(f"{step}_{name}"),
        repo=ctx.repo,
    )


def _execute(ctx: ReviewContext, name: str, prompt: str, resume: str | None, step: str, recorded: list[Review]) -> _Run:
    role = ctx.config.role(f"reviewer_{name}")
    request = _request(ctx, name, prompt, resume, step)
    ctx.emit("agent_event", phase="start", role=request.role, task=ctx.task.id, model=role.model, step=step)
    head_before = git_ops.head_sha(ctx.worktree)
    result = ctx.runners[role.agent].run(request)
    ctx.after_run(role.agent, result)
    ctx.emit(
        "agent_event",
        phase="end",
        role=request.role,
        task=ctx.task.id,
        model=role.model,
        step=step,
        ok=result.ok,
        error_kind=result.error_kind,
    )
    head_after = git_ops.head_sha(ctx.worktree)
    if head_after != head_before:
        raise _Abort(
            head_moved=f"{request.role} moved HEAD from {head_before[:8]} to {head_after[:8]} during {step}"
        )
    if not result.ok or result.structured is None:
        return _Run(name, result, None, "")
    problems = schemas.validate(schemas.VERDICT, result.structured)
    if problems:
        bad = failure("schema", f"verdict invalid: {'; '.join(problems[:3])}", session_id=result.session_id)
        return _Run(name, bad, None, "")
    review = Review(
        reviewer=name,
        model=result.model or role.model,
        session=result.session_id,
        sha=ctx.tree,
        base_sha=ctx.base_sha,
        verdict=result.structured["verdict"],
        findings=[Finding(**item) for item in result.structured["findings"]],
        at=datetime.now(timezone.utc).isoformat(),
    )
    recorded.append(review)
    return _Run(name, result, review, result.structured["summary"])


def _fresh(ctx: ReviewContext, name: str, prompt: str, recorded: list[Review]) -> _Run:
    return _execute(ctx, name, prompt, None, "review", recorded)


def _reconcile(ctx: ReviewContext, name: str, firsts: dict[str, _Run], recorded: list[Review]) -> _Run:
    mine = firsts[name]
    other = firsts[_other(name)]
    session = mine.review.session if mine.review else None
    if session is None:
        return _Run(
            name,
            AgentResult(
                ok=False, text="", structured=None, session_id=None, model=None,
                error_kind="crash", error="no session id from the first review to resume",
            ),
            None,
            "",
        )
    prompt = prompts.render(
        "reviewer_reconcile",
        decisions=ctx.decisions,
        task_id=ctx.task.id,
        title=ctx.task.title,
        description=ctx.task.description,
        acceptance=_acceptance_text(ctx.task),
        tree=ctx.tree,
        other_reviewer=other.name,
        other_verdict=other.review.verdict,
        other_summary=other.summary,
        other_findings=format_findings(other.review.findings),
    )
    return _execute(ctx, name, prompt, session, "reconcile", recorded)


def _other(name: str) -> str:
    return REVIEWER_NAMES[1] if name == REVIEWER_NAMES[0] else REVIEWER_NAMES[0]


def _needs_reconciliation(finals: dict[str, _Run]) -> bool:
    verdicts = {run.review.verdict for run in finals.values()}
    if len(verdicts) > 1:
        return True
    return any(f.severity in BLOCKING for run in finals.values() for f in run.review.findings)


def _verdict_consistent(decider: Decider, emit: Callable[..., None], config: Config, scope: str, run: _Run) -> bool:
    review = run.review
    if review.verdict == "APPROVE" and any(f.severity in BLOCKING for f in review.findings):
        return False
    if not can_answer(decider, "verdict_consistent"):
        return True
    lines = [f"verdict: {review.verdict}", f"summary: {run.summary[:600]}", "findings:"]
    lines += [f"- [{f.severity}] {f.message[:160]}" for f in review.findings[:8]] or ["- none"]
    decision = decider.ask_bool("verdict_consistent", "\n".join(lines), CONSISTENCY_QUESTION)
    emit(
        "decision",
        key="verdict_consistent",
        task=scope,
        reviewer=run.name,
        answer=decision.answer,
        confidence=decision.confidence,
        source=decision.source,
    )
    if confident(decision, config.decider.confidence):
        return decision.answer is True
    return False


def _consistent(ctx: ReviewContext, run: _Run) -> bool:
    return _verdict_consistent(ctx.decider, ctx.emit, ctx.config, ctx.task.id, run)


def _ensure_consistent(ctx: ReviewContext, run: _Run, prompt: str, recorded: list[Review]) -> _Run:
    return _ensure_consistent_with(
        ctx, run, lambda: _fresh(ctx, run.name, prompt, recorded), recorded, ctx.task.id
    )


def _ensure_consistent_with(
    ctx: "ReviewContext | IntegrationContext",
    run: _Run,
    fresh: Callable[[], _Run],
    recorded: list[Review],
    scope: str,
) -> _Run:
    """Keep `run` if its verdict matches its findings; otherwise rerun it once fresh, then force REQUEST_CHANGES."""
    if _verdict_consistent(ctx.decider, ctx.emit, ctx.config, scope, run):
        return run
    ctx.emit("note", text=f"reviewer {run.name} verdict looks inconsistent or unsure; rerunning it once fresh")
    _gate(ctx)
    again = fresh()
    _raise_failures(ctx, [again])
    if _verdict_consistent(ctx.decider, ctx.emit, ctx.config, scope, again):
        return again
    ctx.emit("note", text=f"reviewer {run.name} is still inconsistent; counting it as REQUEST_CHANGES")
    findings = list(again.review.findings) + [
        Finding(
            severity="major",
            file="",
            line=None,
            message="The reviewer's verdict did not match its findings, so it was counted as REQUEST_CHANGES.",
        )
    ]
    forced = Review(
        reviewer=again.review.reviewer,
        model=again.review.model,
        session=again.review.session,
        sha=again.review.sha,
        base_sha=again.review.base_sha,
        verdict="REQUEST_CHANGES",
        findings=findings,
        at=datetime.now(timezone.utc).isoformat(),
    )
    recorded.append(forced)
    return _Run(again.name, again.result, forced, again.summary)


def _merge_findings(finals: dict[str, _Run]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple] = set()
    for name in REVIEWER_NAMES:
        for finding in finals[name].review.findings:
            key = (finding.file, finding.line, finding.message)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "reviewer": name,
                    "severity": finding.severity,
                    "file": finding.file,
                    "line": finding.line,
                    "message": finding.message,
                }
            )
    merged.sort(key=lambda item: SEVERITY_ORDER[item["severity"]])
    return merged


def format_findings(findings: list[Finding]) -> str:
    """Render findings as a bullet list for a prompt."""
    if not findings:
        return "- (none)"
    lines = []
    for finding in findings:
        where = finding.file if finding.line is None else f"{finding.file}:{finding.line}"
        lines.append(f"- [{finding.severity}] {where + ': ' if where else ''}{finding.message}")
    return "\n".join(lines)


def format_feedback(outcome: ReviewOutcome) -> str:
    """Render an outcome's verdicts, summaries and merged findings as coder feedback."""
    lines: list[str] = []
    for review in outcome.final_reviews:
        lines.append(f"Reviewer {review.reviewer}: {review.verdict}")
        summary = outcome.summaries.get(review.reviewer)
        if summary:
            lines.append(f"  {summary}")
    lines.append("")
    lines.append("Findings, most severe first:")
    if not outcome.merged_findings:
        lines.append("- (no itemised findings; see the reviewer summaries above)")
    for item in outcome.merged_findings:
        where = item["file"] if item["line"] is None else f"{item['file']}:{item['line']}"
        lines.append(f"- [{item['severity']}] ({item['reviewer']}) {where + ': ' if where else ''}{item['message']}")
    return "\n".join(lines)


@dataclass
class IntegrationRepo:
    """One involved repo's integration branch: its worktree, branch and the commit the run started from."""

    name: str
    worktree: Path
    branch: str
    base_sha: str


@dataclass
class IntegrationContext:
    """Everything the cross-repo review needs. Reviewers run fresh and read-only at `root`.

    `repo` is the workspace root used for the risk-gate hook, like `ReviewContext.repo`.
    """

    root: Path
    goal: str
    plan: str
    repos: list[IntegrationRepo]
    decisions: str
    config: Config
    runners: dict[str, AgentRunner]
    decider: Decider
    log_path: Callable[[str], Path | None]
    emit: Callable[..., None]
    gate: Callable[[], str | None]
    after_run: Callable[[str, AgentResult], None] = _no_run_hook
    repo: Path | None = None


def integration_heads(repos: list[IntegrationRepo]) -> dict[str, str]:
    """The current HEAD of every integration worktree, by repo name."""
    return {item.name: git_ops.head_sha(item.worktree) for item in repos}


def integration_fingerprint(repos: list[IntegrationRepo]) -> str:
    """A short id for the exact set of integration heads that were reviewed."""
    heads = integration_heads(repos)
    return hashlib.sha1("\n".join(f"{n}={h}" for n, h in sorted(heads.items())).encode()).hexdigest()[:12]


def _clip_text(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... {what} truncated after {limit} characters; read the files themselves for the rest."


def integration_repos_text(ctx: IntegrationContext) -> str:
    """Per repo: its branch, base, worktree, diffstat and (budgeted) diff, as one block for the prompt."""
    budget = max(MIN_REPO_DIFF_CHARS, MAX_INTEGRATION_DIFF_CHARS // max(1, len(ctx.repos)))
    blocks = []
    for item in ctx.repos:
        diff = git_ops.diff_between(item.worktree, item.base_sha, item.branch)
        stat = git_ops.diff_stat_between(item.worktree, item.base_sha, item.branch)
        blocks.append(
            "\n".join(
                [
                    f"### Repo {item.name}: branch {item.branch}, started from {item.base_sha[:8]}",
                    f"Worktree (read it for full files): {item.worktree}",
                    "",
                    "Files changed:",
                    _clip_text(stat, MAX_STAT_CHARS, "stat").strip() or "(no changes)",
                    "",
                    "Diff:",
                    _clip_text(diff, budget, "diff").strip() or "(no changes)",
                ]
            )
        )
    return "\n\n".join(blocks)


def _integration_prompt(ctx: IntegrationContext) -> str:
    return prompts.render(
        "integration_reviewer",
        decisions=ctx.decisions,
        goal=ctx.goal,
        plan=ctx.plan,
        repos=integration_repos_text(ctx),
    )


def _integration_execute(
    ctx: IntegrationContext, name: str, prompt: str, fingerprint: str, recorded: list[Review]
) -> _Run:
    role_name = f"reviewer_{name}"
    role = ctx.config.role(role_name)
    request = AgentRequest(
        role=role_name,
        agent=role.agent,
        model=role.model,
        effort=role.effort,
        prompt=prompt,
        cwd=ctx.root,
        schema=schemas.VERDICT,
        sandbox="read-only",
        browser=getattr(ctx.config.browser, role.agent),
        log_path=ctx.log_path(f"integration_{name}"),
        repo=ctx.repo,
        add_dirs=[item.worktree for item in ctx.repos],
        skip_git_check=not (Path(ctx.root) / ".git").exists(),
    )
    step = "integration_review"
    ctx.emit("agent_event", phase="start", role=role_name, task=INTEGRATION_SCOPE, model=role.model, step=step)
    before = integration_heads(ctx.repos)
    result = ctx.runners[role.agent].run(request)
    ctx.after_run(role.agent, result)
    ctx.emit(
        "agent_event",
        phase="end",
        role=role_name,
        task=INTEGRATION_SCOPE,
        model=role.model,
        step=step,
        ok=result.ok,
        error_kind=result.error_kind,
    )
    after = integration_heads(ctx.repos)
    if after != before:
        moved = ", ".join(f"{n} {before[n][:8]}->{after[n][:8]}" for n in before if before[n] != after[n])
        raise _Abort(head_moved=f"{role_name} moved an integration branch during {step}: {moved}")
    if not result.ok or result.structured is None:
        return _Run(name, result, None, "")
    problems = schemas.validate(schemas.VERDICT, result.structured)
    if problems:
        bad = failure("schema", f"verdict invalid: {'; '.join(problems[:3])}", session_id=result.session_id)
        return _Run(name, bad, None, "")
    review = Review(
        reviewer=name,
        model=result.model or role.model,
        session=result.session_id,
        sha=fingerprint,
        base_sha="",
        verdict=result.structured["verdict"],
        findings=[Finding(**item) for item in result.structured["findings"]],
        at=datetime.now(timezone.utc).isoformat(),
    )
    recorded.append(review)
    return _Run(name, result, review, result.structured["summary"])


def integration_review(ctx: IntegrationContext) -> ReviewOutcome:
    """Both reviewers judge, fresh and independently, whether the repos' integration branches fit together.

    `approved` needs both APPROVE. Failure fields mean the same as in `double_review`; the outcome's
    `head_moved` is set when a reviewer moved an integration branch.
    """
    recorded: list[Review] = []
    try:
        _gate(ctx)
        fingerprint = integration_fingerprint(ctx.repos)
        prompt = _integration_prompt(ctx)
        runs = _parallel(
            [lambda n=name: _integration_execute(ctx, n, prompt, fingerprint, recorded) for name in REVIEWER_NAMES]
        )
        _raise_failures(ctx, runs)
        finals = {run.name: run for run in runs}
        for name in REVIEWER_NAMES:
            finals[name] = _ensure_consistent_with(
                ctx,
                finals[name],
                lambda n=name: _integration_execute(ctx, n, prompt, fingerprint, recorded),
                recorded,
                INTEGRATION_SCOPE,
            )
    except _Abort as abort:
        return ReviewOutcome(
            approved=False, blocked=False, reviews=recorded, merged_findings=[], **abort.fields
        )
    reviews = [finals[name].review for name in REVIEWER_NAMES]
    verdicts = {review.verdict for review in reviews}
    return ReviewOutcome(
        approved=verdicts == {"APPROVE"},
        blocked="BLOCKED" in verdicts,
        reviews=recorded,
        merged_findings=_merge_findings(finals),
        final_reviews=reviews,
        summaries={name: finals[name].summary for name in REVIEWER_NAMES},
    )
