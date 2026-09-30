"""`wf publish`: the Claude committer (a fake here) pushes each integration branch from a temporary worktree."""

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.ws_shim import StubOrch, make_branch, seed_run, snapshot
from workforce import cli, publish
from workforce import config as config_mod
from workforce.agents.claude import ClaudeRunner
from workforce.errors import StateLockedError, WorkforceError
from workforce.workspace import find_workspace

FAKES = Path(__file__).parent / "fakes"
FAKE_CLAUDE = FAKES / "fake_claude.py"
FAKE_CODEX = FAKES / "fake_codex.py"
BRANCH = "wf/add-things-0929"
PR_URL = "https://example.test/acme/alpha/pull/7"
PUSHED_JSON = json.dumps({"pushed": True, "pr_url": PR_URL, "error": None})
API_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")


def git(repo, *args) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def fake_workspace_toml(repos) -> str:
    text = config_mod.workspace_toml(repos)
    text = text.replace('claude = "~/.local/bin/claude"', f'claude = "{FAKE_CLAUDE}"')
    text = text.replace('codex = "/opt/homebrew/bin/codex"', f'codex = "{FAKE_CODEX}"')
    text = text.replace("parallel = 2", "parallel = 1")
    return text.replace('backend = "laya"', 'backend = "off"')


class Rig:
    def __init__(self, tmp_path, ws_root, monkeypatch):
        for var in API_VARS:
            monkeypatch.delenv(var, raising=False)
        self.tmp_path = tmp_path
        self.root = ws_root
        self.home = tmp_path / "home"
        self.monkeypatch = monkeypatch
        self.scenario_file = tmp_path / "scenario.json"
        (ws_root / "workforce.toml").write_text(fake_workspace_toml(["alpha", "beta"]))
        self.config = config_mod.load(ws_root)
        self.ws = find_workspace(ws_root)
        self.remote = tmp_path / "remote-alpha.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        git(ws_root / "alpha", "remote", "add", "origin", str(self.remote))
        self.orch = StubOrch(
            self.ws,
            self.config,
            self.home,
            runners={"claude": ClaudeRunner(FAKE_CLAUDE)},
        )
        self.alpha = ws_root / "alpha"
        self.beta = ws_root / "beta"

    def scenario(self, *steps: dict) -> None:
        self.scenario_file.write_text(json.dumps({"claude": list(steps)}))
        self.monkeypatch.setenv("WF_FAKE_SCENARIO", str(self.scenario_file))

    def calls(self) -> list[dict]:
        path = Path(str(self.scenario_file) + ".calls.jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def make_branches(self, alpha=True, beta=False, status="done") -> None:
        branches = {}
        if alpha:
            base, _ = make_branch(self.alpha, BRANCH, {"a.txt": "alpha work\n"})
            branches["alpha"] = (BRANCH, "main", base)
        if beta:
            base, _ = make_branch(self.beta, BRANCH, {"b.txt": "beta work\n"})
            branches["beta"] = (BRANCH, "main", base)
        seed_run(self.orch, status=status, tasks=[("alpha", "T1"), ("beta", "T2")], branches=branches)

    def remote_branches(self) -> list[str]:
        out = git(self.remote, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        return out.splitlines()


def publisher_step(**extra) -> dict:
    step = {
        "match": "# Role: publisher",
        "run": f"git push -q origin HEAD:refs/heads/{BRANCH} && printf '%s' '{PUSHED_JSON}'",
        "structured_from_run": True,
    }
    step.update(extra)
    return step


@pytest.fixture
def rig(tmp_path, git_workspace, monkeypatch):
    return Rig(tmp_path, git_workspace, monkeypatch)


def test_publish_pushes_the_branch_and_reports_the_pr(rig):
    rig.make_branches()
    rig.scenario(publisher_step())
    rows = publish.publish(rig.orch)
    assert [(r.repo, r.branch, r.pushed, r.pr_url, r.error, r.status) for r in rows] == [
        ("alpha", BRANCH, True, PR_URL, None, "pushed")
    ]
    assert rig.remote_branches() == [BRANCH]
    assert git(rig.remote, "rev-parse", BRANCH) == git(rig.alpha, "rev-parse", BRANCH)


def test_the_publisher_runs_with_the_users_setup_in_a_temporary_worktree_of_the_branch(rig):
    rig.make_branches()
    rig.scenario(publisher_step())
    publish.publish(rig.orch)
    (call,) = rig.calls()
    assert "--setting-sources" not in call["argv"]
    assert "--strict-mcp-config" not in call["argv"]
    assert "--json-schema" in call["argv"]
    cwd = Path(call["cwd"]).resolve()
    assert rig.orch.paths.worktrees_root.resolve() in cwd.parents
    assert cwd.name == "_publish"
    prompt = call["prompt"]
    assert f"`{BRANCH}`" in prompt and "`main`" in prompt and "alpha" in prompt
    assert "Never force-push" in prompt and "Never push, commit to, merge into" in prompt
    assert "open one pull request" in prompt.lower()
    assert "(none yet)" in prompt


def test_the_temporary_worktree_is_removed_and_the_checkout_is_untouched(rig):
    rig.make_branches()
    (rig.alpha / "README.md").write_text("dirty\n")
    (rig.alpha / "scratch.txt").write_text("untracked\n")
    before = snapshot(rig.alpha)
    beta_before = snapshot(rig.beta)
    rig.scenario(publisher_step())
    publish.publish(rig.orch)
    assert snapshot(rig.alpha) == before
    assert snapshot(rig.beta) == beta_before
    assert (rig.alpha / "README.md").read_text() == "dirty\n"
    worktrees = git(rig.alpha, "worktree", "list", "--porcelain")
    assert "_publish" not in worktrees
    assert not list(rig.orch.paths.worktrees_root.rglob("_publish"))


def test_the_temporary_worktree_is_removed_even_when_the_publisher_fails(rig):
    rig.make_branches()
    rig.scenario({"match": "# Role: publisher", "error": "crash"})
    rows = publish.publish(rig.orch)
    assert rows[0].pushed is False and rows[0].status == "failed" and rows[0].error
    assert "_publish" not in git(rig.alpha, "worktree", "list", "--porcelain")


def test_a_reported_error_is_shown_and_nothing_is_marked_pushed(rig):
    rig.make_branches()
    failure = json.dumps({"pushed": False, "pr_url": None, "error": "gh: not logged in"})
    rig.scenario(publisher_step(run=f"printf '%s' '{failure}'"))
    (row,) = publish.publish(rig.orch)
    assert (row.pushed, row.pr_url, row.error, row.status) == (False, None, "gh: not logged in", "failed")
    assert rig.remote_branches() == []


def test_pushed_without_a_pr_is_still_pushed(rig):
    rig.make_branches()
    body = json.dumps({"pushed": True, "pr_url": None, "error": None})
    rig.scenario(publisher_step(run=f"git push -q origin HEAD:refs/heads/{BRANCH} && printf '%s' '{body}'"))
    (row,) = publish.publish(rig.orch)
    assert row.pushed and row.pr_url is None and row.status == "pushed"


def test_a_publisher_that_moves_the_branch_tip_is_flagged(rig):
    rig.make_branches()
    sneaky = "git commit -q --allow-empty -m sneaky && printf '%s' '" + PUSHED_JSON + "'"
    rig.scenario(publisher_step(run=sneaky))
    (row,) = publish.publish(rig.orch)
    assert row.status == "failed"
    assert "moved the branch tip" in row.error


def test_a_publisher_that_moves_the_base_branch_is_flagged(rig):
    rig.make_branches()
    move_base = (
        shlex.join(["git", "-C", str(rig.alpha), "update-ref", "refs/heads/main"])
        + " $(git rev-parse HEAD) && printf '%s' '"
        + PUSHED_JSON
        + "'"
    )
    rig.scenario(publisher_step(run=move_base))
    (row,) = publish.publish(rig.orch)
    assert row.status == "failed"
    assert "base branch main moved" in row.error


def test_only_branches_with_commits_are_published(rig):
    base, _ = make_branch(rig.alpha, BRANCH, {"a.txt": "x\n"})
    base_b, _ = make_branch(rig.beta, BRANCH)
    seed_run(
        rig.orch,
        tasks=[("alpha", "T1"), ("beta", "T2")],
        branches={"alpha": (BRANCH, "main", base), "beta": (BRANCH, "main", base_b)},
    )
    assert [e["repo"] for e in publish.publishable(rig.orch)] == ["alpha"]


def test_nothing_to_publish_is_an_error(rig):
    base, _ = make_branch(rig.alpha, BRANCH)
    seed_run(rig.orch, branches={"alpha": (BRANCH, "main", base)})
    with pytest.raises(WorkforceError, match="no integration branch has commits to publish"):
        publish.publish(rig.orch)


def test_no_run_at_all_is_refused(rig):
    with pytest.raises(WorkforceError, match="no run yet"):
        publish.publish(rig.orch)


@pytest.mark.parametrize("status", ["planning", "debating", "awaiting_models", "running", "paused", "awaiting_user"])
def test_publish_is_refused_while_a_run_is_active(rig, status):
    rig.make_branches(status=status)
    rig.scenario(publisher_step())
    with pytest.raises(WorkforceError, match=f"run 20260929-120000 is {status}"):
        publish.publish(rig.orch)
    assert rig.calls() == []
    assert rig.remote_branches() == []


@pytest.mark.parametrize("status", ["preparing", "integrating"])
def test_the_new_workspace_statuses_count_as_active(status):
    from types import SimpleNamespace

    assert f"is {status}" in publish.refusal(SimpleNamespace(id="R1", status=status))
    assert publish.refusal(SimpleNamespace(id="R1", status="done")) is None
    assert publish.refusal(SimpleNamespace(id="R1", status="failed")) is None


def test_a_failed_run_can_still_be_published(rig):
    rig.make_branches(status="failed")
    rig.scenario(publisher_step())
    assert publish.publish(rig.orch)[0].pushed


def test_publish_is_refused_while_another_process_holds_the_lock(rig):
    rig.make_branches()
    rig.scenario(publisher_step())
    with rig.orch.store.lock():
        with pytest.raises(StateLockedError):
            publish.publish(rig.orch)
    assert rig.calls() == []


def test_the_lock_is_held_while_publishing_and_released_after(rig):
    rig.make_branches()
    seen = []

    class Watching(ClaudeRunner):
        def run(self, req, on_event=None):
            try:
                with rig.orch.store.lock():
                    seen.append("free")
            except StateLockedError:
                seen.append("held")
            return super().run(req, on_event)

    rig.orch.runners["claude"] = Watching(FAKE_CLAUDE)
    rig.scenario(publisher_step())
    publish.publish(rig.orch)
    assert seen == ["held"]
    with rig.orch.store.lock():
        pass


def test_a_paused_usage_gate_refuses_before_launching_anything(rig):
    rig.make_branches()
    rig.orch.gate_reason = "Claude 5-hour window at 61%"
    rig.scenario(publisher_step())
    with pytest.raises(WorkforceError, match="usage is paused"):
        publish.publish(rig.orch)
    assert rig.calls() == []


def test_repo_filter_and_unknown_repo(rig):
    rig.make_branches(alpha=True, beta=True)
    with pytest.raises(WorkforceError, match="no repo 'nope'"):
        publish.publish(rig.orch, repo_name="nope")
    rig.scenario(publisher_step())
    rows = publish.publish(rig.orch, repo_name="alpha")
    assert [r.repo for r in rows] == ["alpha"]
    assert len(rig.calls()) == 1


def test_a_repo_without_commits_named_explicitly_is_an_error(rig):
    rig.make_branches(alpha=True, beta=False)
    with pytest.raises(WorkforceError, match="for beta"):
        publish.publish(rig.orch, repo_name="beta")


def test_declined_branches_are_skipped_and_the_agent_never_runs(rig):
    rig.make_branches(alpha=True, beta=True)
    rig.scenario(publisher_step())
    asked = []
    rows = publish.publish(rig.orch, confirm=lambda entry: asked.append(entry["repo"]) or False)
    assert asked == ["alpha", "beta"]
    assert [r.status for r in rows] == ["skipped", "skipped"]
    assert rig.calls() == []


def test_two_repos_are_published_one_at_a_time_with_a_confirmation_each(rig):
    rig.make_branches(alpha=True, beta=True)
    git(rig.beta, "remote", "add", "origin", str(rig.remote))
    other = json.dumps({"pushed": True, "pr_url": "https://example.test/acme/beta/pull/2", "error": None})
    rig.scenario(
        publisher_step(),
        publisher_step(run=f"printf '%s' '{other}'"),
    )
    answers = iter([True, True])
    rows = publish.publish(rig.orch, confirm=lambda entry: next(answers))
    assert [(r.repo, r.pr_url) for r in rows] == [("alpha", PR_URL), ("beta", "https://example.test/acme/beta/pull/2")]
    cwds = [Path(c["cwd"]).resolve() for c in rig.calls()]
    assert cwds[0] != cwds[1]


def test_a_subscription_violation_stops_the_remaining_branches(rig):
    rig.make_branches(alpha=True, beta=True)
    rig.scenario({"match": "# Role: publisher", "api_key_source": "env", "structured": {"pushed": True, "pr_url": None, "error": None}})
    rows = publish.publish(rig.orch)
    assert [r.status for r in rows] == ["aborted", "not tried"]
    assert len(rig.calls()) == 1


def test_a_branch_checked_out_elsewhere_is_a_clear_row_error(rig):
    rig.make_branches()
    git(rig.alpha, "worktree", "add", str(rig.tmp_path / "elsewhere"), BRANCH)
    rig.scenario(publisher_step())
    (row,) = publish.publish(rig.orch)
    assert row.status == "failed" and "cannot check the branch out" in row.error
    assert rig.calls() == []


def test_the_committer_role_must_be_claude(rig):
    rig.make_branches()
    import dataclasses

    role = dataclasses.replace(rig.config.role("committer"), agent="codex")
    rig.orch.config = dataclasses.replace(rig.config, roles={**rig.config.roles, "committer": role})
    (row,) = publish.publish(rig.orch)
    assert "must be a Claude role" in row.error


def test_events_and_logs_are_written(rig):
    rig.make_branches()
    rig.scenario(publisher_step())
    said = []
    publish.publish(rig.orch, say=said.append)
    events, _ = rig.orch.events.read(0)
    assert any(e["kind"] == "note" and BRANCH in e["text"] for e in events)
    assert said and BRANCH in said[0]
    logs = list((rig.orch.paths.runs / "20260929-120000" / "publish").glob("alpha-*.jsonl"))
    assert len(logs) == 1


class TestCli:
    @pytest.fixture
    def wf(self, rig, monkeypatch, capsys):
        monkeypatch.chdir(rig.root / "alpha")
        monkeypatch.setattr(cli, "build_orchestrator", lambda ctx, config: rig.orch)
        answers: list[str] = []
        prompts: list[str] = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            if not answers:
                raise EOFError
            return answers.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)

        def run(*argv):
            capsys.readouterr()
            code = cli.main(list(argv), home=rig.home)
            return code, capsys.readouterr().out

        run.answers = answers
        run.prompts = prompts
        return run

    def test_publish_yes_prints_the_table_and_exits_0(self, rig, wf):
        rig.make_branches()
        rig.scenario(publisher_step())
        code, out = wf("publish", "--yes")
        assert code == 0, out
        assert "repo" in out and "pushed" in out and "alpha" in out and BRANCH in out and PR_URL in out
        assert wf.prompts == []

    def test_publish_asks_before_each_branch(self, rig, wf):
        rig.make_branches()
        rig.scenario(publisher_step())
        wf.answers.append("y")
        code, out = wf("publish")
        assert code == 0, out
        assert wf.prompts == [f"Push {BRANCH} (1 commit) in alpha and open a PR? [y/N] "]
        assert rig.remote_branches() == [BRANCH]

    def test_declining_publishes_nothing_and_exits_0(self, rig, wf):
        rig.make_branches()
        rig.scenario(publisher_step())
        wf.answers.append("n")
        code, out = wf("publish")
        assert code == 0
        assert "skipped" in out
        assert rig.calls() == []

    def test_end_of_input_counts_as_no(self, rig, wf):
        rig.make_branches()
        rig.scenario(publisher_step())
        code, out = wf("publish")
        assert code == 0 and "skipped" in out
        assert rig.calls() == []

    def test_an_error_row_exits_1_and_shows_the_error(self, rig, wf):
        rig.make_branches()
        failure = json.dumps({"pushed": False, "pr_url": None, "error": "gh: not logged in"})
        rig.scenario(publisher_step(run=f"printf '%s' '{failure}'"))
        code, out = wf("publish", "--yes")
        assert code == 1
        assert "gh: not logged in" in out and "no" in out

    def test_refused_while_a_run_is_active_exits_1(self, rig, wf):
        rig.make_branches(status="running")
        code, out = wf("publish", "--yes")
        assert code == 1
        assert "run 20260929-120000 is running" in out
        assert rig.calls() == []

    def test_refused_while_the_lock_is_held_exits_4(self, rig, wf):
        rig.make_branches()
        with rig.orch.store.lock():
            code, out = wf("publish", "--yes")
        assert code == 4
        assert "another workforce process" in out

    def test_repo_option(self, rig, wf):
        rig.make_branches(alpha=True, beta=True)
        rig.scenario(publisher_step())
        code, out = wf("publish", "--yes", "--repo", "alpha")
        assert code == 0
        assert "alpha" in out and "beta" not in out
        assert len(rig.calls()) == 1

    def test_unknown_repo_exits_1_and_lists_the_valid_ones(self, rig, wf):
        rig.make_branches()
        code, out = wf("publish", "--yes", "--repo", "nope")
        assert code == 1
        assert "no repo 'nope'" in out and "alpha" in out
