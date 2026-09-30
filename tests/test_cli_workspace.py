"""Workspace mode in the CLI: init, finding the workspace, `wf repos`, the model-pick branches, event tags, status."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_cli import LOW_CODEX_LIMITS, SCENARIOS, fake_toml
from tests.ws_shim import StubOrch, make_branch, seed_run, snapshot
from workforce import cli, render_text
from workforce import config as config_mod
from workforce.events import EventLog
from workforce.render_text import EventFormatter
from workforce.state import Run, StateStore, Task
from workforce.workspace import find_workspace

API_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
NOT_INITIALIZED = "This folder has 3 git repos (alpha, beta, tools/gamma). Run wf init to set it up as a workspace."
NO_WORKSPACE = "not inside a git repo or a workspace; cd into one, or run wf init in a folder of repos"


def git(repo, *args) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def fake_workspace_toml(repos=("alpha", "beta", "tools/gamma"), extra="") -> str:
    base = fake_toml()
    listing = "".join(f'  "{name}",\n' for name in repos)
    return f'{base}\n[workspace]\nrepos = [\n{listing}]\nbranch_prefix = "wf/"\n{extra}'


class WsEnv:
    def __init__(self, tmp_path, ws_root, monkeypatch, capsys):
        for var in API_VARS:
            monkeypatch.delenv(var, raising=False)
        self.tmp_path = tmp_path
        self.root = ws_root
        self.home = tmp_path / "home"
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.scenario_file = tmp_path / "scenario.json"
        payload = json.loads((SCENARIOS / "happy_path.json").read_text())
        payload["codex_limits"] = LOW_CODEX_LIMITS
        self.scenario_file.write_text(json.dumps(payload))
        monkeypatch.setenv("WF_FAKE_SCENARIO", str(self.scenario_file))
        monkeypatch.setattr(config_mod, "_DEFAULT_TOML", fake_toml())
        monkeypatch.chdir(ws_root)

    def cd(self, path) -> None:
        self.monkeypatch.chdir(path)

    def write_toml(self, **kw) -> None:
        (self.root / "workforce.toml").write_text(fake_workspace_toml(**kw))

    def wf(self, *argv: str) -> tuple[int, str]:
        self.capsys.readouterr()
        code = cli.main(list(argv), home=self.home)
        return code, self.capsys.readouterr().out


@pytest.fixture
def wsenv(tmp_path, git_workspace, monkeypatch, capsys):
    return WsEnv(tmp_path, git_workspace, monkeypatch, capsys)


class TestInit:
    def test_in_a_folder_of_repos_writes_the_workspace_config(self, wsenv):
        before = {name: snapshot(wsenv.root / name) for name in ("alpha", "beta", "tools/gamma")}
        code, out = wsenv.wf("init")
        assert code == 0, out
        config = config_mod.load(wsenv.root)
        assert config.workspace.repos == ["alpha", "beta", "tools/gamma"]
        assert config.workspace.branch_prefix == "wf/"
        assert "notes" not in config.workspace.repos
        assert "✓ wrote workforce.toml (3 repos)" in out
        assert "Workspace" in out and "3 repos" in out
        for line in ("alpha  on main", "beta  on main", "tools/gamma  on main"):
            assert line in out
        assert "checks: none detected" in out
        assert "Edit [workspace] repos in workforce.toml to trim the list" in out
        assert (wsenv.root / ".workforce" / "runs").is_dir()
        assert not (wsenv.home / ".workforce").exists()
        assert {name: snapshot(wsenv.root / name) for name in before} == before

    def test_the_workspace_root_gets_no_git_side_effects(self, wsenv):
        wsenv.wf("init")
        assert not (wsenv.root / ".git").exists()
        for name in ("alpha", "beta", "tools/gamma"):
            exclude = wsenv.root / name / ".git" / "info" / "exclude"
            assert not exclude.exists() or ".workforce" not in exclude.read_text()

    def test_it_lists_the_checks_detected_per_repo(self, wsenv):
        (wsenv.root / "beta" / "go.mod").write_text("module beta\n")
        code, out = wsenv.wf("init")
        assert code == 0
        beta_line = next(line for line in out.splitlines() if line.lstrip("✓ ").startswith("beta  on main"))
        assert "test: go test ./..." in beta_line and "lint: go vet ./..." in beta_line

    def test_running_it_again_keeps_the_file(self, wsenv):
        wsenv.wf("init")
        text = (wsenv.root / "workforce.toml").read_text()
        code, out = wsenv.wf("init")
        assert code == 0
        assert "✓ workforce.toml already exists, kept as it is" in out
        assert (wsenv.root / "workforce.toml").read_text() == text

    def test_a_detached_repo_fails_the_preflight(self, wsenv):
        git(wsenv.root / "beta", "checkout", "-q", "--detach")
        code, out = wsenv.wf("init")
        assert code == 3
        assert "detached HEAD" in out and "preflight failed" in out

    def test_a_workspace_config_above_is_not_shadowed_without_force(self, wsenv):
        wsenv.wf("init")
        wsenv.cd(wsenv.root / "alpha")
        code, out = wsenv.wf("init")
        assert code == 3
        assert "would shadow it" in out and "--force" in out
        assert str(wsenv.root / "workforce.toml") in out.replace("\n", "")
        assert not (wsenv.root / "alpha" / "workforce.toml").exists()

    def test_force_writes_the_shadowing_config(self, wsenv):
        wsenv.wf("init")
        wsenv.cd(wsenv.root / "alpha")
        code, out = wsenv.wf("init", "--force")
        assert code == 0, out
        assert "✓ wrote workforce.toml" in out
        assert (wsenv.root / "alpha" / "workforce.toml").is_file()
        assert config_mod.load(wsenv.root / "alpha").workspace is None

    def test_a_folder_with_no_repos_says_so(self, wsenv, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        wsenv.cd(empty)
        code, out = wsenv.wf("init")
        assert code == 3
        assert NO_WORKSPACE in out
        assert not (empty / "workforce.toml").exists()

    def test_in_a_repo_it_still_writes_the_single_repo_file(self, wsenv):
        wsenv.cd(wsenv.root / "alpha")
        code, out = wsenv.wf("init")
        assert code == 0, out
        assert "✓ repo on branch main" in out
        assert config_mod.load(wsenv.root / "alpha").workspace is None
        assert "Workspace" not in out

    def test_from_inside_a_repo_subfolder_it_initialises_the_repo_top(self, wsenv):
        sub = wsenv.root / "alpha" / "src"
        sub.mkdir()
        wsenv.cd(sub)
        code, out = wsenv.wf("init")
        assert code == 0, out
        assert (wsenv.root / "alpha" / "workforce.toml").is_file()
        assert not (sub / "workforce.toml").exists()


class TestFindingTheWorkspace:
    def test_wf_from_inside_a_repo_subfolder_finds_the_workspace_with_focus(self, wsenv, monkeypatch):
        wsenv.write_toml()
        sub = wsenv.root / "alpha" / "src"
        sub.mkdir()
        wsenv.cd(sub)
        seen = {}

        def fake_run_console(repo, **kwargs):
            seen["repo"] = Path(repo)
            seen["workspace"] = kwargs["workspace"]
            return 0

        monkeypatch.setattr("workforce.console.app.run_console", fake_run_console)
        code, _ = wsenv.wf()
        assert code == 0
        assert seen["repo"].resolve() == wsenv.root.resolve()
        assert seen["workspace"].focus == "alpha"
        assert seen["workspace"].single is False
        assert sorted(seen["workspace"].repos) == ["alpha", "beta", "tools/gamma"]

    def test_commands_run_from_a_subfolder_operate_on_the_workspace_root(self, wsenv):
        wsenv.write_toml()
        sub = wsenv.root / "tools" / "gamma"
        wsenv.cd(sub)
        code, out = wsenv.wf("repos")
        assert code == 0
        assert "alpha" in out and "beta" in out and "tools/gamma" in out

    def test_an_uninitialized_folder_of_repos_exits_3_with_the_repo_list(self, wsenv):
        for argv in (["status"], ["repos"], ["run", "do it"], ["publish"], []):
            code, out = wsenv.wf(*argv)
            assert code == 3, argv
            assert " ".join(out.split()) == "✗ " + NOT_INITIALIZED

    def test_outside_a_repo_and_a_workspace_exits_3(self, wsenv, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        wsenv.cd(plain)
        for argv in (["status"], ["repos"], ["publish"], []):
            code, out = wsenv.wf(*argv)
            assert code == 3, argv
            assert " ".join(out.split()) == "✗ " + NO_WORKSPACE

    def test_a_listed_repo_that_is_missing_exits_3(self, wsenv):
        wsenv.write_toml(repos=("alpha", "ghost"))
        code, out = wsenv.wf("repos")
        assert code == 3
        assert "ghost" in out and "workspace.repos" in out


class TestReposCommand:
    def test_the_table_shows_base_branch_dirty_flag_and_checks(self, wsenv):
        wsenv.write_toml(extra='\n[repos.alpha]\nbase = "main"\n')
        (wsenv.root / "beta" / "README.md").write_text("changed\n")
        (wsenv.root / "tools" / "gamma" / "go.mod").write_text("module gamma\n")
        git(wsenv.root / "tools" / "gamma", "checkout", "-q", "-b", "feature")
        code, out = wsenv.wf("repos")
        assert code == 0, out
        rows = {line.split()[0]: line for line in out.splitlines() if line.split() and line.split()[0] in ("alpha", "beta", "tools/gamma")}
        assert set(rows) == {"alpha", "beta", "tools/gamma"}
        assert re.search(r"alpha\s+main\s+main\s+no\s+none detected", rows["alpha"])
        assert re.search(r"beta\s+main \(current\)\s+main\s+yes\s+none detected", rows["beta"])
        assert re.search(r"tools/gamma\s+feature \(current\)\s+feature\s+yes\s+test: go test", rows["tools/gamma"])
        header = next(line for line in out.splitlines() if line.split()[:1] == ["repo"])
        assert header.split() == ["repo", "base", "branch", "dirty", "checks"]
        assert "3 repos" in out.splitlines()[0]

    def test_it_never_touches_the_checkouts(self, wsenv):
        wsenv.write_toml()
        (wsenv.root / "beta" / "README.md").write_text("changed\n")
        before = {name: snapshot(wsenv.root / name) for name in ("alpha", "beta", "tools/gamma")}
        wsenv.wf("repos")
        assert {name: snapshot(wsenv.root / name) for name in before} == before

    def test_per_repo_check_overrides_show_up(self, wsenv):
        wsenv.write_toml(extra='\n[repos.beta.checks]\ntest = "bundle exec rspec"\n')
        code, out = wsenv.wf("repos")
        beta = next(line for line in out.splitlines() if line.split()[:1] == ["beta"])
        assert "test: bundle exec rspec" in beta

    def test_a_single_repo_lists_itself(self, tmp_path, git_repo, monkeypatch, capsys):
        for var in API_VARS:
            monkeypatch.delenv(var, raising=False)
        (git_repo / "workforce.toml").write_text(fake_toml())
        monkeypatch.chdir(git_repo)
        capsys.readouterr()
        code = cli.main(["repos"], home=tmp_path / "home")
        out = capsys.readouterr().out
        assert code == 0
        assert out.splitlines()[0].startswith("Repo ")
        assert git_repo.name in out and "main" in out


class ModelPick:
    """Drive `cli.pick_models` against a stub orchestrator with scripted input."""

    def __init__(self, tmp_path, ws_root, monkeypatch, repos=("alpha", "beta")):
        (ws_root / "workforce.toml").write_text(fake_workspace_toml(repos=("alpha", "beta")))
        self.config = config_mod.load(ws_root)
        self.ws = find_workspace(ws_root)
        self.orch = StubOrch(
            self.ws,
            self.config,
            tmp_path / "home",
            proposed={name: "wf/add-things-0929" for name in repos},
        )
        seed_run(self.orch, status="awaiting_models", tasks=[("alpha", "T1"), ("beta", "T2"), ("beta", "T3")])
        self.prompts: list[str] = []
        self.answers: list[str] = []
        monkeypatch.setattr(cli, "_read_line", self._read)

    def _read(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError("ran out of scripted input")
        return self.answers.pop(0)

    def run(self):
        from rich.console import Console
        import io

        buffer = io.StringIO()
        console = Console(file=buffer, width=200, highlight=False)
        cli.pick_models(self.orch, console)
        return buffer.getvalue()


@pytest.fixture
def pick(tmp_path, git_workspace, monkeypatch):
    return ModelPick(tmp_path, git_workspace, monkeypatch)


class TestModelPick:
    def test_task_rows_carry_the_repo_and_each_involved_repo_asks_for_a_branch(self, pick):
        pick.answers = ["", "", "", "", ""]
        pick.run()
        assert pick.prompts[0].startswith("alpha/T1  Add t1")
        assert pick.prompts[1].startswith("beta/T2  Add t2")
        assert pick.prompts[2].startswith("beta/T3  Add t3")
        assert pick.prompts[3:] == [
            "branch for alpha [wf/add-things-0929]: ",
            "branch for beta [wf/add-things-0929]: ",
        ]
        assert pick.orch.set_branch_calls == []
        assert [c[0] for c in pick.orch.model_calls] == ["T1", "T2", "T3"]

    def test_enter_keeps_the_branch_and_a_name_renames_it(self, pick):
        pick.answers = ["", "", "", "", "wf/beta-custom"]
        pick.run()
        assert pick.orch.set_branch_calls == [("beta", "wf/beta-custom")]
        assert pick.orch.proposed == {"alpha": "wf/add-things-0929", "beta": "wf/beta-custom"}

    def test_an_invalid_name_is_reported_and_asked_again(self, pick):
        pick.answers = ["", "", "", "not a branch", "wf/alpha-x", ""]
        out = pick.run()
        assert "✗" in out and "'not a branch' is not a valid branch name" in out
        assert pick.prompts[3] == pick.prompts[4] == "branch for alpha [wf/add-things-0929]: "
        assert pick.orch.set_branch_calls == [("alpha", "wf/alpha-x")]

    def test_a_single_repo_run_keeps_bare_task_ids_and_still_offers_the_branch(self, tmp_path, git_repo, monkeypatch):
        (git_repo / "workforce.toml").write_text(fake_toml())
        ws = find_workspace(git_repo)
        orch = StubOrch(ws, config_mod.load(git_repo), tmp_path / "home", proposed={git_repo.name: "wf/add-things-0929"})
        seed_run(orch, status="awaiting_models", tasks=[(git_repo.name, "T1")])
        prompts = []
        answers = ["", ""]
        monkeypatch.setattr(cli, "_read_line", lambda p: prompts.append(p) or answers.pop(0))
        from rich.console import Console
        import io

        cli.pick_models(orch, Console(file=io.StringIO(), width=200))
        assert prompts[0].startswith("T1  Add t1")
        assert prompts[1] == f"branch for {git_repo.name} [wf/add-things-0929]: "

    def test_no_involved_repos_means_no_branch_prompt(self, pick):
        pick.orch.proposed = {}
        pick.answers = ["", "", ""]
        pick.run()
        assert len(pick.prompts) == 3


def event(kind, **data):
    return {"ts": "2026-09-29T12:00:00.000+00:00", "kind": kind, **data}


class TestEventTags:
    def lines(self, workspace, *events):
        formatter = EventFormatter(workspace)
        return [line for e in events for line in formatter.format(e)]

    def test_workspace_mode_tags_are_repo_slash_task(self):
        lines = self.lines(
            True,
            event("agent_event", phase="start", role="coder", task="T1", repo="app", model="claude-sonnet-5-5", step="code"),
            event("review", task="T1", repo="app", model="claude-opus-5-5", verdict="APPROVE", sha="3f9c2a1abc", findings=0),
            event("merged", task="T3", repo="billing", sha="abcdef123456", branch="wf/x"),
            event("task_status", task="T2", repo="sdk", status="pending"),
            event("check_result", task="T1", repo="app", ok=True),
        )
        assert lines[0].startswith("[app/T1]") and "coding" in lines[0] and "sonnet-5-5" in lines[0]
        assert lines[1].startswith("[app/T1]") and "review" in lines[1] and "APPROVE @ 3f9c2a1" in lines[1]
        assert lines[2].startswith("[billing/T3]") and "merged" in lines[2]
        assert lines[3].startswith("[sdk/T2]")
        assert lines[4].startswith("[app/T1]") and "checks" in lines[4]
        assert not any(re.match(r"\[[^/\]]*T\d\]", line) for line in lines)

    def test_a_label_is_always_separated_from_the_tag(self):
        lines = self.lines(True, event("agent_event", phase="start", role="coder", task="T1", repo="app", model="m", step="code"))
        assert re.match(r"\[app/T1\]\s+coding\s", lines[0])
        long = self.lines(True, event("check_result", task="T12", repo="sdk-extras", ok=True))
        assert re.match(r"\[sdk-extras/T12\] checks", long[0])

    def test_single_mode_keeps_the_bare_task_tag_even_though_events_carry_the_repo(self):
        lines = self.lines(
            False,
            event("agent_event", phase="start", role="coder", task="T1", repo="agent-team", model="claude-sonnet-5-5", step="code"),
            event("review", task="T1", repo="agent-team", model="claude-opus-5-5", verdict="APPROVE", sha="3f9c2a1", findings=0),
        )
        assert lines[0].startswith("[T1]    coding")
        assert lines[1].startswith("[T1]    review")
        assert all("agent-team" not in line for line in lines)

    def test_plan_and_run_events_have_no_repo_in_their_tag(self):
        lines = self.lines(
            True,
            event("plan_ready", tasks=3),
            event("agent_event", phase="start", role="planner", model="gpt-6-astra", step="plan"),
        )
        assert lines[0].startswith("[plan]") and lines[1].startswith("[plan]")

    def test_blocked_calls_questions_errors_and_progress_flags_name_the_repo(self):
        lines = self.lines(
            True,
            event("decision", key="risk_gate", allowed=False, task="T1", repo="app", summary="rm -rf /", reason="deny"),
            event("question", question="Q1", task="T2", repo="billing", text="which?"),
            event("error", task="T1", repo="app", message="boom"),
            event("decision", key="agent_progress", flag=True, answer="stuck", task="T1", repo="app", confidence=0.9),
        )
        assert "⛔ [app/T1] blocked" in lines[0]
        assert "(billing/T2)" in lines[1]
        assert lines[2] == "✗ app/T1: boom"
        assert "[app/T1] Laya thinks" in lines[3] and "workforce log app/T1" in lines[3]

    def test_the_console_lines_use_the_same_tags(self):
        from workforce.console import render

        lines = render.event_lines(
            EventFormatter(True),
            event("agent_event", phase="start", role="coder", task="T1", repo="app", model="claude-sonnet-5-5", step="code"),
            color=False,
        )
        assert lines[0].startswith("  [app/T1]")

    def test_run_done_prints_the_branch_summary_and_the_publish_hint(self):
        summary = [
            "app: branch wf/x-0929: 2 commits on top of main (1a2b3c4d)",
            "Run wf publish to push and open PRs.",
        ]
        lines = self.lines(True, event("run_done", run="R1", tasks=3, summary=summary))
        assert lines[0] == "✓ run done: 3 tasks merged"
        assert lines[1].strip() == summary[0] and lines[2].strip() == summary[1]

    def test_event_matching_accepts_bare_and_qualified_task_names(self):
        e = event("review", task="T1", repo="app")
        assert render_text.event_matches_task(e, "T1")
        assert render_text.event_matches_task(e, "app/T1")
        assert not render_text.event_matches_task(e, "beta/T1")
        assert not render_text.event_matches_task(event("plan_ready"), "T1")
        assert render_text.normalize_task_name("app/t1") == "app/T1"
        assert render_text.normalize_task_name("t1") == "T1"

    def test_the_tail_names_the_repo_of_the_one_task_in_flight_on_a_blocked_call(self, wsenv):
        wsenv.write_toml()
        orch = StubOrch(find_workspace(wsenv.root), config_mod.load(wsenv.root), wsenv.home)
        run = seed_run(orch, status="running", tasks=[("beta", "T1")])
        run.tasks[0].status = "coding"
        orch.store.save(run)
        from rich.console import Console
        import io

        buffer = io.StringIO()
        tail = cli.EventTail(orch.events, Console(file=buffer, width=200, highlight=False), True)
        orch.events.emit("decision", key="risk_gate", allowed=False, summary="rm -rf /", reason="deny")
        tail.drain()
        assert "⛔ [beta/T1] blocked" in buffer.getvalue()


class TestLogCommand:
    def seed_events(self, wsenv):
        wsenv.write_toml()
        events = EventLog(cli.Paths(wsenv.root, home=wsenv.home))
        events.emit("review", task="T1", repo="alpha", model="claude-opus-5-5", verdict="APPROVE", sha="aaaaaaa", findings=0)
        events.emit("review", task="T1", repo="beta", model="claude-opus-5-5", verdict="APPROVE", sha="bbbbbbb", findings=0)
        events.emit("plan_ready", tasks=2)

    def test_the_log_shows_repo_tags_and_filters_by_qualified_task(self, wsenv):
        self.seed_events(wsenv)
        code, out = wsenv.wf("log")
        assert code == 0
        assert "[alpha/T1]" in out and "[beta/T1]" in out and "[plan]" in out
        code, out = wsenv.wf("log", "alpha/T1")
        assert "[alpha/T1]" in out and "[beta/T1]" not in out and "[plan]" not in out
        code, out = wsenv.wf("log", "T1")
        assert "[alpha/T1]" in out and "[beta/T1]" in out
        code, out = wsenv.wf("log", "gamma/T1")
        assert "No events for gamma/T1." in out


class TestStatus:
    def test_the_status_has_a_repo_column_and_an_integration_branch_table(self, wsenv, monkeypatch):
        wsenv.write_toml()
        alpha, beta = wsenv.root / "alpha", wsenv.root / "beta"
        base_a, _ = make_branch(alpha, "wf/add-things-0929", {"a.txt": "a\n"})
        base_b, _ = make_branch(beta, "wf/add-things-0929", {"b.txt": "b\n"})
        orch = StubOrch(find_workspace(wsenv.root), config_mod.load(wsenv.root), wsenv.home)
        run = seed_run(
            orch,
            status="running",
            tasks=[("alpha", "T1"), ("beta", "T2")],
            branches={"alpha": ("wf/add-things-0929", "main", base_a), "beta": ("wf/add-things-0929", "main", base_b)},
        )
        monkeypatch.setattr(cli, "build_orchestrator", lambda ctx, config: orch)
        code, out = wsenv.wf("status")
        assert code == 0, out
        header = next(line for line in out.splitlines() if line.split()[:2] == ["id", "repo"])
        assert header.split()[:3] == ["id", "repo", "title"]
        t1 = next(line for line in out.splitlines() if line.split()[:1] == ["T1"])
        assert t1.split()[:2] == ["T1", "alpha"]
        assert "Integration branches" in out
        rows = [line.split() for line in out.splitlines() if "wf/add-things-0929" in line]
        assert [r[:2] for r in rows] == [["alpha", "wf/add-things-0929"], ["beta", "wf/add-things-0929"]]
        assert all(r[-1] == "1" for r in rows)
        assert f"main ({base_a[:8]})" in out

    def test_a_single_repo_status_has_neither_the_repo_column_nor_the_branch_table(self, tmp_path, git_repo, monkeypatch, capsys):
        for var in API_VARS:
            monkeypatch.delenv(var, raising=False)
        (git_repo / "workforce.toml").write_text(fake_toml())
        monkeypatch.chdir(git_repo)
        store = StateStore(cli.Paths(git_repo, home=tmp_path / "home"))
        run = Run.create("R1", "goal")
        run.status = "running"
        run.tasks.append(Task(id="T1", title="Add a", description="d", repo=git_repo.name))
        store.save(run)
        capsys.readouterr()
        code = cli.main(["status"], home=tmp_path / "home")
        out = capsys.readouterr().out
        assert code == 0
        header = next(line for line in out.splitlines() if line.split()[:1] == ["id"])
        assert "repo" not in header.split()
        assert "Integration branches" not in out


def commit_step(subject: str) -> dict:
    return {
        "run": (
            f"git add -A && git commit -q -m '{subject}' && printf '{{\"committed\":true,\"sha\":\"%s\","
            "\"message\":\"m\",\"error\":null}' \"$(git rev-parse HEAD)\""
        ),
        "structured_from_run": True,
        "match": "# Role: committer",
    }


def verdict_step(summary: str, match: str = "You review one task's changes on your own") -> dict:
    return {"structured": {"verdict": "APPROVE", "summary": summary, "findings": []}, "match": match}


def two_repo_scenario(pushes: list[str]) -> dict:
    plan = {
        "tasks": [
            {
                "id": "T1",
                "repo": "alpha",
                "title": "Add a.txt",
                "description": "Create a.txt containing the word alpha",
                "acceptance": ["a.txt exists"],
                "depends_on": [],
            },
            {
                "id": "T2",
                "repo": "beta",
                "title": "Add b.txt",
                "description": "Create b.txt containing the word beta",
                "acceptance": ["b.txt exists"],
                "depends_on": ["T1"],
            },
        ],
        "risks": [],
        "open_questions": [],
    }
    integration = "# Role: independent integration reviewer"
    return {
        "codex_limits": LOW_CODEX_LIMITS,
        "claude": [
            {"structured": {"agree": True, "objections": [], "suggested_changes": []}, "match": "# Role: plan reviewer"},
            {"write_files": {"a.txt": "alpha\n"}, "text": "Created a.txt", "match": "## Task T1:"},
            verdict_step("T1 ok"),
            commit_step("T1: add a"),
            {"write_files": {"b.txt": "beta\n"}, "text": "Created b.txt", "match": "## Task T2:"},
            verdict_step("T2 ok"),
            commit_step("T2: add b"),
            verdict_step("fits together", integration),
            *[
                {
                    "match": "# Role: publisher",
                    "run": f"git push -q origin HEAD:refs/heads/{branch} && printf '{{\"pushed\":true,\"pr_url\":\"https://example.test/{branch}\",\"error\":null}}'",
                    "structured_from_run": True,
                }
                for branch in pushes
            ],
        ],
        "codex": [
            {"structured": plan, "match": "# Role: planner", "text": "plan"},
            verdict_step("T1 fine"),
            verdict_step("T2 fine"),
            verdict_step("nothing conflicts", integration),
        ],
    }


class TestWorkspaceRunEndToEnd:
    """`wf run`, `wf status` and `wf publish` in a folder of two repos, with the real orchestrator and the fake CLIs."""

    def test_a_cross_repo_run_lands_on_branches_and_publishes(self, wsenv, monkeypatch, tmp_path):
        wsenv.write_toml(repos=("alpha", "beta"))
        alpha, beta = wsenv.root / "alpha", wsenv.root / "beta"
        remotes = {}
        for name, repo in (("alpha", alpha), ("beta", beta)):
            remotes[name] = tmp_path / f"remote-{name}.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remotes[name])], check=True)
            git(repo, "remote", "add", "origin", str(remotes[name]))
        (alpha / "README.md").write_text("dirty on purpose\n")
        before = {name: snapshot(repo) for name, repo in (("alpha", alpha), ("beta", beta))}
        wsenv.scenario_file.write_text(json.dumps(two_repo_scenario(["wf/reviews-hook-0929", "wf/beta-custom"])))
        answers = ["", ""]
        prompts: list[str] = []
        branch_answers = ["", "wf/beta-custom"]

        def fake_input(prompt=""):
            prompts.append(prompt)
            if prompt.startswith("branch for "):
                return branch_answers.pop(0)
            return answers.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)

        code, out = wsenv.wf("run", "Reviews hook")
        assert code == 0, out
        assert prompts[0].startswith("alpha/T1  Add a.txt") and prompts[1].startswith("beta/T2  Add b.txt")
        assert [p.split(" [")[0] for p in prompts[2:]] == ["branch for alpha", "branch for beta"]
        assert "[alpha/T1]" in out and "[beta/T2]" in out and "[T1]" not in out
        assert "✓ run done: 2 tasks merged" in out
        assert "wf publish" in out

        run = StateStore(cli.Paths(wsenv.root, home=wsenv.home)).load()
        assert run.status == "done"
        assert {name: info.branch for name, info in run.integration.items()} == {
            "alpha": f"wf/{run.slug}",
            "beta": "wf/beta-custom",
        }
        assert git(alpha, "ls-tree", "-r", "--name-only", f"wf/{run.slug}").split() == ["README.md", "a.txt"]
        assert git(beta, "ls-tree", "-r", "--name-only", "wf/beta-custom").split() == ["README.md", "b.txt"]
        assert {name: snapshot(repo) for name, repo in (("alpha", alpha), ("beta", beta))} == before
        assert (alpha / "README.md").read_text() == "dirty on purpose\n"

        code, out = wsenv.wf("status")
        assert code == 0, out
        assert "Integration branches" in out
        rows = {line.split()[0]: line.split() for line in out.splitlines() if "wf/" in line and line.split()[0] in ("alpha", "beta")}
        assert rows["alpha"][1] == f"wf/{run.slug}" and rows["alpha"][-1] == "1"
        assert rows["beta"][1] == "wf/beta-custom" and rows["beta"][-1] == "1"

        scenario = json.loads(wsenv.scenario_file.read_text())
        publishers = [s for s in scenario["claude"] if s["match"] == "# Role: publisher"]
        publishers[0]["run"] = publishers[0]["run"].replace("wf/reviews-hook-0929", f"wf/{run.slug}")
        wsenv.scenario_file.write_text(json.dumps(scenario))
        code, out = wsenv.wf("publish", "--yes")
        assert code == 0, out
        assert "pushed" in out and f"https://example.test/wf/{run.slug}" in out and "https://example.test/wf/beta-custom" in out
        assert git(remotes["alpha"], "for-each-ref", "--format=%(refname:short)", "refs/heads").split() == [f"wf/{run.slug}"]
        assert git(remotes["beta"], "for-each-ref", "--format=%(refname:short)", "refs/heads").split() == ["wf/beta-custom"]
        assert {name: snapshot(repo) for name, repo in (("alpha", alpha), ("beta", beta))} == before
