import json
import subprocess
from pathlib import Path

import pytest

from workforce.agents.base import AgentResult
from workforce.agents.guard import (
    check_claude,
    check_codex,
    check_env,
    check_repo,
    check_workspace_repos,
    preflight,
    probe_claude_model,
    probe_codex_model,
)
from workforce.errors import PreflightError

FAKES = Path(__file__).parent / "fakes"
FAKE_CLAUDE = FAKES / "fake_claude.py"
FAKE_CODEX = FAKES / "fake_codex.py"


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    git(path, "config", "commit.gpgsign", "false")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "-A")
    git(path, "commit", "-m", "init")
    return path


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    path = tmp_path / "scenario.json"
    monkeypatch.setenv("WF_FAKE_SCENARIO", str(path))

    def write(**data):
        path.write_text(json.dumps(data))

    write()
    return write


def test_check_env_lists_every_key_set():
    problems = check_env(
        {"ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "y", "ANTHROPIC_AUTH_TOKEN": "z", "HOME": "/h"}
    )
    assert len(problems) == 3
    assert any("ANTHROPIC_API_KEY" in p for p in problems)
    assert any("OPENAI_API_KEY" in p for p in problems)
    assert any("ANTHROPIC_AUTH_TOKEN" in p for p in problems)


def test_check_env_clean_and_empty_values():
    assert check_env({"HOME": "/h"}) == []
    assert check_env({"ANTHROPIC_API_KEY": ""}) == []


def test_check_claude_ok(scenario):
    assert check_claude(FAKE_CLAUDE) == []


def test_check_claude_wrong_auth_method(scenario):
    scenario(claude_auth_status={"loggedIn": True, "authMethod": "api_key"})
    problems = check_claude(FAKE_CLAUDE)
    assert len(problems) == 1 and "api_key" in problems[0]


def test_check_claude_non_json(scenario):
    scenario(claude_auth_status="Not logged in")
    problems = check_claude(FAKE_CLAUDE)
    assert len(problems) == 1 and "JSON" in problems[0]


def test_check_claude_missing_binary(tmp_path):
    problems = check_claude(tmp_path / "nope")
    assert len(problems) == 1 and "--version" in problems[0]


def test_check_codex_ok(scenario):
    assert check_codex(FAKE_CODEX) == []


def test_check_codex_not_logged_in(scenario):
    scenario(codex_login_status="Not logged in")
    problems = check_codex(FAKE_CODEX)
    assert len(problems) == 1 and "ChatGPT" in problems[0]


def test_check_codex_missing_binary(tmp_path):
    problems = check_codex(tmp_path / "nope")
    assert len(problems) == 1 and "--version" in problems[0]


def test_check_repo_clean(repo):
    assert check_repo(repo) == []


def test_check_repo_not_git(tmp_path):
    problems = check_repo(tmp_path)
    assert len(problems) == 1 and "not a git repository" in problems[0]


def test_check_repo_detached(repo):
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    git(repo, "checkout", "--detach", sha)
    problems = check_repo(repo)
    assert len(problems) == 1 and "detached" in problems[0]


@pytest.mark.parametrize("change", ["modify", "untracked", "staged"])
def test_check_repo_accepts_a_dirty_checkout(repo, change):
    if change == "modify":
        (repo / "a.txt").write_text("changed\n")
    elif change == "untracked":
        (repo / "new.txt").write_text("n\n")
    else:
        (repo / "s.txt").write_text("s\n")
        git(repo, "add", "s.txt")
    assert check_repo(repo) == []


def test_check_repo_ignores_workforce_dir(repo):
    (repo / ".workforce").mkdir()
    (repo / ".workforce" / "state.json").write_text("{}")
    assert check_repo(repo) == []


def test_preflight_passes_with_a_dirty_checkout(scenario, repo):
    (repo / "a.txt").write_text("local edit\n")
    (repo / "new.txt").write_text("n\n")
    preflight(FAKE_CLAUDE, FAKE_CODEX, repo, {"HOME": "/h"})


def test_preflight_passes(scenario, repo):
    preflight(FAKE_CLAUDE, FAKE_CODEX, repo, {"HOME": "/h"})


def test_preflight_aggregates_all_problems(scenario, tmp_path):
    scenario(
        claude_auth_status={"authMethod": "console"},
        codex_login_status="Not logged in",
    )
    with pytest.raises(PreflightError) as excinfo:
        preflight(
            FAKE_CLAUDE,
            FAKE_CODEX,
            tmp_path,
            {"ANTHROPIC_API_KEY": "sk", "OPENAI_API_KEY": "sk"},
        )
    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "OPENAI_API_KEY" in message
    assert "console" in message
    assert "ChatGPT" in message
    assert "not a git repository" in message
    assert len([l for l in message.splitlines() if l.strip().startswith("- ")]) == 5


def test_preflight_reports_missing_binaries_together(tmp_path, repo):
    with pytest.raises(PreflightError) as excinfo:
        preflight(tmp_path / "no-claude", tmp_path / "no-codex", repo, {})
    message = str(excinfo.value)
    assert "no-claude" in message and "no-codex" in message


class StubRunner:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def run(self, req, on_event=None):
        self.requests.append(req)
        return self.result


def ok_result():
    return AgentResult(True, "OK", None, "s", "m")


def failed(kind):
    return AgentResult(False, "", None, None, None, error_kind=kind, error="boom")


def test_probe_claude_model_true_false_raise():
    runner = StubRunner(ok_result())
    assert probe_claude_model(runner, "claude-opus-5-5") is True
    req = runner.requests[0]
    assert (req.agent, req.model, req.prompt) == ("claude", "claude-opus-5-5", "Reply OK")
    assert probe_claude_model(StubRunner(failed("model_unavailable")), "x") is False
    with pytest.raises(PreflightError):
        probe_claude_model(StubRunner(failed("rate_limited")), "x")


def test_probe_codex_model_true_false_raise(tmp_path):
    runner = StubRunner(ok_result())
    assert probe_codex_model(runner, "gpt-6-astra", cwd=tmp_path) is True
    req = runner.requests[0]
    assert (req.agent, req.model, req.cwd, req.sandbox) == ("codex", "gpt-6-astra", tmp_path, "read-only")
    assert probe_codex_model(StubRunner(failed("model_unavailable")), "x") is False
    with pytest.raises(PreflightError):
        probe_codex_model(StubRunner(failed("crash")), "x")


def test_probe_with_fake_runners(tmp_path, scenario):
    from workforce.agents.claude import ClaudeRunner
    from workforce.agents.codex import CodexRunner

    scenario(claude=[{"text": "OK"}, {"error": "model_unavailable"}], codex=[{"text": "OK"}, {"error": "model_unavailable"}])
    claude, codex = ClaudeRunner(FAKE_CLAUDE), CodexRunner(FAKE_CODEX)
    assert probe_claude_model(claude, "good", cwd=tmp_path) is True
    assert probe_claude_model(claude, "bad", cwd=tmp_path) is False
    assert probe_codex_model(codex, "good", cwd=tmp_path) is True
    assert probe_codex_model(codex, "bad", cwd=tmp_path) is False


def _version_script(tmp_path, name, output):
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\necho '{output}'\n")
    script.chmod(0o755)
    return script


def test_parse_version():
    from workforce.agents.guard import parse_version

    assert parse_version("2.1.284 (Claude Code)") == (2, 1, 284)
    assert parse_version("codex-cli 0.158.0") == (0, 158, 0)
    assert parse_version("no digits") is None


def test_old_claude_is_refused_with_the_upgrade_command(tmp_path):
    problems = check_claude(_version_script(tmp_path, "claude", "2.1.279 (Claude Code)"))
    assert len(problems) == 1
    assert "2.1.279" in problems[0] and "2.1.280" in problems[0] and "claude update" in problems[0]


def test_claude_at_the_minimum_version_passes_the_version_check(tmp_path):
    problems = check_claude(_version_script(tmp_path, "claude", "2.1.280 (Claude Code)"))
    assert not any("older than" in p for p in problems)


def test_old_codex_is_refused_with_the_upgrade_command(tmp_path):
    problems = check_codex(_version_script(tmp_path, "codex", "codex-cli 0.157.9"))
    assert len(problems) == 1
    assert "0.157.9" in problems[0] and "0.158.0" in problems[0] and "codex update" in problems[0]


def test_unparseable_version_is_a_problem(tmp_path):
    problems = check_codex(_version_script(tmp_path, "codex", "banana"))
    assert len(problems) == 1 and "version" in problems[0]


def test_check_env_also_refuses_codex_api_key():
    problems = check_env({"CODEX_API_KEY": "k"})
    assert len(problems) == 1 and "CODEX_API_KEY" in problems[0]


def test_agent_env_drops_keys_and_endpoints_but_keeps_the_oauth_token():
    from workforce.agents.guard import agent_env

    env = agent_env(
        {"KEEP": "1"},
        {
            "PATH": "/bin",
            "ANTHROPIC_BASE_URL": "u",
            "OPENAI_BASE_URL": "u",
            "OPENAI_ORG_ID": "o",
            "CODEX_API_KEY": "k",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_API_KEY": "k",
            "CLAUDE_CODE_OAUTH_TOKEN": "t",
        },
    )
    assert env["KEEP"] == "1" and env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"
    for name in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "CODEX_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_API_KEY"):
        assert name not in env


@pytest.mark.parametrize(
    "text,gone",
    [
        ("call failed with sk-ant-api03-abcdefghijklmnop1234", "abcdefghijklmnop1234"),
        ("Authorization: Bearer abc.def.ghi-jkl", "abc.def.ghi-jkl"),
        ("authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("curl -H 'x' Bearer 0123456789abcdef", "0123456789abcdef"),
        ("ANTHROPIC_API_KEY=hunter2hunter2 claude", "hunter2hunter2"),
        ("GITHUB_TOKEN: ghp_abcdef123456", "ghp_abcdef123456"),
        ("DB_PASSWORD='p@ss word'", "p@ss word"),
        ("password=letmein", "letmein"),
        ("api_key: abc123", "abc123"),
        ('{"token": "abc123"}', "abc123"),
    ],
)
def test_redact_blanks_secret_shapes(text, gone):
    from workforce.agents.guard import redact

    out = redact(text)
    assert gone not in out and "[redacted]" in out


@pytest.mark.parametrize(
    "text",
    [
        "claude reported apiKeySource='ANTHROPIC_API_KEY'; expected 'none'",
        "task T1 failed 3 times: the checks fail (exit 1)",
        "reviewer_claude: crash: no result event",
        "tokenizer error at line 3",
        "the secret is to keep it small",
    ],
)
def test_redact_leaves_ordinary_text_alone(text):
    from workforce.agents.guard import redact

    assert redact(text) == text


def test_redacted_walks_nested_data():
    from workforce.agents.guard import redacted

    data = {"a": ["ok", "OPENAI_API_KEY=abc123xyz"], "b": {"c": "password=zzz"}, "n": 5, "t": ("x",)}
    out = redacted(data)
    assert out["a"] == ["ok", "OPENAI_API_KEY=[redacted]"]
    assert out["b"] == {"c": "password=[redacted]"} and out["n"] == 5 and out["t"] == ["x"]


def head_sha(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def make_repo(root, name):
    path = root / name
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    git(path, "config", "commit.gpgsign", "false")
    (path / "a.txt").write_text(name)
    git(path, "add", "-A")
    git(path, "commit", "-m", "init")
    return path


@pytest.fixture
def two_repos(tmp_path):
    return make_repo(tmp_path / "ws", "app"), make_repo(tmp_path / "ws", "billing")


def test_workspace_repos_all_good(two_repos):
    app, billing = two_repos
    entries = [("app", app, None, "wf/goal-0929"), ("billing", billing, "main", "wf/goal-0929")]
    assert check_workspace_repos(entries) == []


def test_workspace_repos_dirty_and_on_another_branch_is_fine(two_repos):
    app, _ = two_repos
    git(app, "checkout", "-b", "feature")
    (app / "a.txt").write_text("dirty")
    (app / "new.txt").write_text("n")
    assert check_workspace_repos([("app", app, None, "wf/goal-0929"), ("app", app, "main", "wf/goal-0929")]) == []


def test_workspace_repos_not_a_git_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    problems = check_workspace_repos([("plain", plain, None, "wf/x")])
    assert len(problems) == 1 and "plain" in problems[0] and "not a git repository" in problems[0]


def test_workspace_repos_missing_directory(tmp_path):
    problems = check_workspace_repos([("gone", tmp_path / "gone", "main", "wf/x")])
    assert len(problems) == 1 and "gone" in problems[0]


def test_workspace_repos_base_must_resolve(two_repos):
    app, billing = two_repos
    problems = check_workspace_repos([("app", app, "release", "wf/x"), ("billing", billing, "main", "wf/x")])
    assert len(problems) == 1 and "app" in problems[0] and "'release'" in problems[0]


def test_workspace_repos_base_may_be_a_sha_or_tag(two_repos):
    app, _ = two_repos
    git(app, "tag", "v1")
    assert check_workspace_repos([("app", app, head_sha(app), "wf/x"), ("app", app, "v1", "wf/y")]) == []


def test_workspace_repos_detached_head_needs_a_configured_base(two_repos):
    app, _ = two_repos
    git(app, "checkout", "--detach", head_sha(app))
    problems = check_workspace_repos([("app", app, None, "wf/x")])
    assert len(problems) == 1 and "detached" in problems[0] and "repos.app.base" in problems[0]
    assert check_workspace_repos([("app", app, "main", "wf/x")]) == []


def test_workspace_repos_integration_branch_must_be_free(two_repos):
    app, billing = two_repos
    git(billing, "branch", "wf/goal-0929")
    problems = check_workspace_repos([("app", app, None, "wf/goal-0929"), ("billing", billing, None, "wf/goal-0929")])
    assert len(problems) == 1 and "billing" in problems[0] and "already exists" in problems[0]


def test_workspace_repos_branch_name_must_be_valid(two_repos):
    app, _ = two_repos
    problems = check_workspace_repos([("app", app, None, "wf/bad name..x")])
    assert len(problems) == 1 and "not a valid branch name" in problems[0]


def test_workspace_repos_worktree_path_must_be_free(two_repos, tmp_path):
    app, billing = two_repos
    free = tmp_path / "wt" / "app" / "_integration"
    assert check_workspace_repos([("app", app, None, "wf/x", free)]) == []

    taken = tmp_path / "wt" / "taken"
    taken.mkdir(parents=True)
    (taken / "file").write_text("x")
    problems = check_workspace_repos([("app", app, None, "wf/x", taken)])
    assert len(problems) == 1 and "not empty" in problems[0]

    registered = tmp_path / "wt" / "registered"
    git(app, "worktree", "add", "-b", "other", str(registered))
    problems = check_workspace_repos([("app", app, None, "wf/x", registered)])
    assert len(problems) == 1 and "not empty" in problems[0]
    (registered / ".git").unlink()
    for child in list(registered.iterdir()):
        child.unlink()
    problems = check_workspace_repos([("app", app, None, "wf/x", registered)])
    assert len(problems) == 1 and "already registered" in problems[0]


def test_workspace_repos_never_touch_the_checkout(two_repos):
    app, _ = two_repos
    (app / "a.txt").write_text("dirty")
    before = (head_sha(app), subprocess.run(["git", "status", "--porcelain"], cwd=app, capture_output=True, text=True).stdout)
    check_workspace_repos([("app", app, None, "wf/x")])
    after = (head_sha(app), subprocess.run(["git", "status", "--porcelain"], cwd=app, capture_output=True, text=True).stdout)
    assert before == after
    assert subprocess.run(["git", "branch", "--list", "wf/x"], cwd=app, capture_output=True, text=True).stdout == ""


def test_workspace_repos_collects_problems_from_every_repo(tmp_path, two_repos):
    app, billing = two_repos
    git(app, "branch", "wf/x")
    problems = check_workspace_repos([("app", app, None, "wf/x"), ("billing", billing, "nope", "wf/x"), ("gone", tmp_path / "gone", None, "wf/x")])
    assert len(problems) == 3


def test_preflight_checks_workspace_repos_and_env_together(scenario, two_repos):
    app, billing = two_repos
    git(app, "branch", "wf/x")
    with pytest.raises(PreflightError) as excinfo:
        preflight(
            FAKE_CLAUDE, FAKE_CODEX, None, {"ANTHROPIC_API_KEY": "sk"},
            repos=[("app", app, None, "wf/x"), ("billing", billing, None, "wf/x")],
        )
    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message and "app" in message and "already exists" in message
    assert "billing" not in message


def test_preflight_with_workspace_repos_passes(scenario, two_repos):
    app, billing = two_repos
    preflight(FAKE_CLAUDE, FAKE_CODEX, None, {"HOME": "/h"}, repos=[("app", app, None, "wf/x"), ("billing", billing, "main", "wf/x")])


def test_preflight_without_a_repo_only_runs_the_tool_checks(scenario):
    preflight(FAKE_CLAUDE, FAKE_CODEX, None, {"HOME": "/h"})
