import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from workforce import config as config_mod
from workforce import schemas
from workforce.config import Checks
from workforce.errors import ConfigError
from workforce.paths import PathEscapeError, Paths
from workforce.state import IntegrationInfo, Run, StateError, StateStore, Task
from workforce.workspace import (
    NoWorkspace,
    NotInitialized,
    Workspace,
    WorkspaceError,
    discover,
    find_workspace,
    load_workspace,
    slugify,
)


def _mkrepo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def _workspace_toml(*repos: str, extra: str = "") -> str:
    listing = ", ".join(json.dumps(name) for name in repos)
    return f"{config_mod.default_toml()}\n[workspace]\nrepos = [{listing}]\nbranch_prefix = \"wf/\"\n{extra}"


def _config_error(text: str) -> str:
    with pytest.raises(ConfigError) as info:
        config_mod.parse(text)
    return str(info.value)


def test_discover_finds_depth_one_and_two_sorted(git_workspace: Path):
    refs = discover(git_workspace)
    assert [ref.name for ref in refs] == ["alpha", "beta", "tools/gamma"]
    assert [ref.path for ref in refs] == [git_workspace / "alpha", git_workspace / "beta", git_workspace / "tools" / "gamma"]


def test_discover_skips_hidden_and_vendored_dirs(tmp_path: Path):
    for skipped in (".hidden", "node_modules", ".venv", "venv", "vendor", "dist", "build"):
        _mkrepo(tmp_path / skipped)
        _mkrepo(tmp_path / skipped / "inner")
        _mkrepo(tmp_path / "group" / skipped)
    _mkrepo(tmp_path / "group" / "real")
    assert [ref.name for ref in discover(tmp_path)] == ["group/real"]


def test_discover_ignores_depth_three_and_never_descends_into_repos(tmp_path: Path):
    _mkrepo(tmp_path / "a" / "b" / "deep")
    _mkrepo(tmp_path / "app")
    _mkrepo(tmp_path / "app" / "vendor-lib")
    _mkrepo(tmp_path / "grp" / "svc")
    _mkrepo(tmp_path / "grp" / "svc" / "sub")
    assert [ref.name for ref in discover(tmp_path)] == ["app", "grp/svc"]


def test_discover_accepts_gitfile_and_skips_symlinks(tmp_path: Path):
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /elsewhere\n")
    real = _mkrepo(tmp_path / "real")
    (tmp_path / "alias").symlink_to(real)
    assert [ref.name for ref in discover(tmp_path)] == ["linked", "real"]


def test_discover_empty_and_missing_root(tmp_path: Path):
    assert discover(tmp_path) == []
    assert discover(tmp_path / "missing") == []


def test_find_workspace_single_repo_without_toml(git_repo: Path):
    ws = find_workspace(git_repo)
    assert ws.single and ws.root == git_repo
    assert list(ws.repos) == ["repo"]
    info = ws.repo("repo")
    assert info.path == git_repo and info.base is None
    assert info.checks == Checks(test="", lint="", build="")
    assert ws.focus == "repo"


def test_find_workspace_single_repo_from_subdirectory(git_repo: Path):
    sub = git_repo / "src" / "pkg"
    sub.mkdir(parents=True)
    ws = find_workspace(sub)
    assert ws.root == git_repo and ws.single and ws.focus == "repo"


def test_find_workspace_nearest_toml_wins(git_workspace: Path):
    config_mod.write_workspace_default(git_workspace, ["alpha", "beta", "tools/gamma"])
    inner = git_workspace / "beta"
    (inner / "workforce.toml").write_text(config_mod.default_toml())
    ws = find_workspace(inner)
    assert ws.root == inner and ws.single and list(ws.repos) == ["beta"]

    outer = find_workspace(git_workspace / "alpha")
    assert outer.root == git_workspace and not outer.single


def test_find_workspace_focus_is_the_repo_containing_cwd(git_workspace: Path):
    config_mod.write_workspace_default(git_workspace, ["alpha", "beta", "tools/gamma"])
    assert find_workspace(git_workspace).focus is None
    assert find_workspace(git_workspace / "notes").focus is None
    assert find_workspace(git_workspace / "alpha").focus == "alpha"
    deep = git_workspace / "tools" / "gamma"
    (deep / "src").mkdir()
    ws = find_workspace(deep / "src")
    assert ws.focus == "tools/gamma"
    assert ws.root == git_workspace
    assert list(ws.repos) == ["alpha", "beta", "tools/gamma"]


def test_find_workspace_applies_base_and_per_repo_checks(git_workspace: Path):
    extra = '[repos.alpha]\nbase = "master"\n[repos.alpha.checks]\ntest = "bundle exec rspec"\n'
    (git_workspace / "workforce.toml").write_text(_workspace_toml("alpha", "beta", extra=extra))
    ws = find_workspace(git_workspace)
    assert ws.repo("alpha").base == "master"
    assert ws.repo("alpha").checks == Checks(test="bundle exec rspec", lint="", build="")
    assert ws.repo("beta").base is None
    assert ws.repo("beta").checks == Checks(test="", lint="", build="")


def test_find_workspace_not_initialized_lists_repos(git_workspace: Path):
    with pytest.raises(NotInitialized) as info:
        find_workspace(git_workspace)
    assert info.value.root == git_workspace
    assert [ref.name for ref in info.value.found] == ["alpha", "beta", "tools/gamma"]
    assert str(info.value) == (
        "This folder has 3 git repos (alpha, beta, tools/gamma). Run wf init to set it up as a workspace."
    )
    assert isinstance(info.value, WorkspaceError)


def test_find_workspace_no_workspace(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(NoWorkspace) as info:
        find_workspace(empty)
    assert info.value.cwd == empty
    assert "not inside a git repo or a workspace" in str(info.value)


def test_find_workspace_home_is_accepted(git_repo: Path, tmp_path: Path):
    assert find_workspace(git_repo, home=tmp_path / "home").root == git_repo
    assert not (tmp_path / "home").exists()


def test_repo_lookup_error_names_valid_repos(git_workspace: Path):
    config_mod.write_workspace_default(git_workspace, ["alpha", "beta"])
    ws = find_workspace(git_workspace)
    with pytest.raises(WorkspaceError, match=r"no repo 'zeta'.*\['alpha', 'beta'\]"):
        ws.repo("zeta")


def test_workspace_is_frozen(git_repo: Path):
    ws = find_workspace(git_repo)
    assert isinstance(ws, Workspace)
    with pytest.raises(AttributeError):
        ws.focus = "x"


def test_load_workspace_single_mode_uses_root_repo(git_repo: Path):
    (git_repo / "workforce.toml").write_text(
        f'{config_mod.default_toml()}\n[repos.repo]\nbase = "main"\n[repos.repo.checks]\nlint = "ruff ."\n'
    )
    ws = load_workspace(git_repo, config_mod.load(git_repo))
    assert ws.single and ws.focus is None
    assert ws.repo("repo").base == "main"
    assert ws.repo("repo").checks == Checks(test="", lint="ruff .", build="")


def test_load_workspace_single_mode_rejects_other_repo_names(git_repo: Path):
    (git_repo / "workforce.toml").write_text(f'{config_mod.default_toml()}\n[repos.other]\nbase = "x"\n')
    with pytest.raises(ConfigError, match=r"repos\.other"):
        load_workspace(git_repo, config_mod.load(git_repo))


def test_load_workspace_single_mode_needs_a_git_root(tmp_path: Path):
    (tmp_path / "workforce.toml").write_text(config_mod.default_toml())
    with pytest.raises(ConfigError, match="not a git repo"):
        find_workspace(tmp_path)


def test_load_workspace_missing_or_non_repo_listing_is_a_config_error(git_workspace: Path):
    cfg = config_mod.parse(_workspace_toml("alpha", "ghost"))
    with pytest.raises(ConfigError, match=r"workspace\.repos\[1\] 'ghost' does not exist"):
        load_workspace(git_workspace, cfg)
    cfg = config_mod.parse(_workspace_toml("alpha", "notes"))
    with pytest.raises(ConfigError, match=r"workspace\.repos\[1\] 'notes' is not a git repo"):
        load_workspace(git_workspace, cfg)


def test_default_toml_has_no_workspace_and_is_single_repo():
    cfg = config_mod.parse(config_mod.default_toml())
    assert cfg.workspace is None and cfg.repo_settings == {}
    assert cfg.branch_prefix == "wf/"
    assert "[workspace]" not in config_mod.default_toml()


def test_workspace_toml_round_trip(tmp_path: Path):
    names = ["alpha", "beta", "tools/gamma"]
    text = config_mod.workspace_toml(names)
    assert text.startswith(config_mod.default_toml())
    assert 'repos = [\n  "alpha",\n  "beta",\n  "tools/gamma",\n]' in text
    cfg = config_mod.parse(text)
    assert cfg.workspace is not None
    assert cfg.workspace.repos == names and cfg.workspace.branch_prefix == "wf/"

    path = config_mod.write_workspace_default(tmp_path, names)
    assert path == tmp_path / "workforce.toml"
    assert config_mod.load(tmp_path).workspace == cfg.workspace
    with pytest.raises(ConfigError, match="already exists"):
        config_mod.write_workspace_default(tmp_path, names)


def test_workspace_toml_rejects_bad_repo_lists(tmp_path: Path):
    with pytest.raises(ConfigError, match=r"workspace\.repos"):
        config_mod.workspace_toml([])
    with pytest.raises(ConfigError, match="more than once"):
        config_mod.workspace_toml(["a", "a"])
    assert not (tmp_path / "workforce.toml").exists()
    with pytest.raises(ConfigError):
        config_mod.write_workspace_default(tmp_path, ["../x"])
    assert not (tmp_path / "workforce.toml").exists()


def test_save_role_still_works_on_a_workspace_file(tmp_path: Path):
    config_mod.write_workspace_default(tmp_path, ["alpha", "beta"])
    cfg = config_mod.save_role(tmp_path, "coder", "claude-opus-5-5", "max")
    assert cfg.role("coder").model == "claude-opus-5-5"
    assert cfg.workspace is not None and cfg.workspace.repos == ["alpha", "beta"]


def test_repo_settings_parse_and_checks_merge():
    extra = (
        '[repos.alpha]\nbase = "master"\n'
        '[repos.alpha.checks]\ntest = "bundle exec rspec"\nlint = ""\n'
        '[repos."tools/gamma"]\nbase = "develop"\n'
    )
    text = _workspace_toml("alpha", "tools/gamma", extra=extra).replace('lint = ""', 'lint = "ruff ."', 1)
    cfg = config_mod.parse(text)
    assert cfg.repo_settings["alpha"].base == "master"
    assert cfg.repo_settings["alpha"].checks == {"test": "bundle exec rspec", "lint": ""}
    assert cfg.repo_settings["tools/gamma"].base == "develop"
    assert cfg.repo_settings["tools/gamma"].checks == {}
    assert cfg.repo_checks("alpha") == Checks(test="bundle exec rspec", lint="", build="")
    assert cfg.repo_checks("tools/gamma") == cfg.checks


@pytest.mark.parametrize(
    "workspace_body, message",
    [
        ("branch_prefix = 'wf/'", r"workspace\.repos is missing"),
        ("repos = []", r"workspace\.repos must be a non-empty list"),
        ("repos = 'app'", r"workspace\.repos must be a non-empty list"),
        ("repos = ['app', 'app']", r"workspace\.repos lists 'app' more than once"),
        ("repos = ['app', '../x']", r"workspace\.repos\[1\]"),
        ("repos = ['/abs']", r"workspace\.repos\[0\]"),
        ("repos = ['a//b']", r"workspace\.repos\[0\]"),
        ("repos = ['app/']", r"workspace\.repos\[0\]"),
        ("repos = ['.']", r"workspace\.repos\[0\]"),
        ("repos = [3]", r"workspace\.repos\[0\]"),
        ("repos = ['app']\nbranch_prefix = ''", r"workspace\.branch_prefix must not be empty"),
        ("repos = ['app']\nbranch_prefix = 'wf'", r"workspace\.branch_prefix 'wf' must end with"),
        ("repos = ['app']\nbranch_prefix = 'w f/'", r"workspace\.branch_prefix 'w f/' may only contain"),
        ("repos = ['app']\nbranch_prefix = 4", r"workspace\.branch_prefix must be a string"),
        ("repos = ['app']\nextra = 1", r"workspace\.extra is not a known key"),
    ],
)
def test_workspace_section_errors_name_the_key(workspace_body, message):
    text = f"{config_mod.default_toml()}\n[workspace]\n{workspace_body}\n"
    assert __import__("re").search(message, _config_error(text))


def test_branch_prefix_accepts_dash_and_defaults_when_absent():
    text = f"{config_mod.default_toml()}\n[workspace]\nrepos = ['app']\nbranch_prefix = 'team-x/wf-'\n"
    assert config_mod.parse(text).branch_prefix == "team-x/wf-"
    text = f"{config_mod.default_toml()}\n[workspace]\nrepos = ['app']\n"
    assert config_mod.parse(text).branch_prefix == "wf/"


@pytest.mark.parametrize(
    "repos_body, message",
    [
        ('[repos.app]\nbase = ""', r"repos\.app\.base must not be empty"),
        ("[repos.app]\nbase = 3", r"repos\.app\.base must be a string"),
        ("[repos.app]\nbse = 'x'", r"repos\.app\.bse is not a known key"),
        ("[repos.app]\nchecks = 'x'", r"repos\.app\.checks must be a table"),
        ("[repos.app.checks]\ntest = 1", r"repos\.app\.checks\.test must be a string"),
        ("[repos.app.checks]\ntests = 'x'", r"repos\.app\.checks\.tests is not a known key"),
        ("[repos.other]\nbase = 'x'", r"repos\.other is not listed in workspace\.repos"),
        ("[repos.'../x']\nbase = 'x'", r"repos\.\.\./x is not a valid repo path"),
    ],
)
def test_repo_settings_errors_name_the_key(repos_body, message):
    text = f"{_workspace_toml('app')}{repos_body}\n"
    assert __import__("re").search(message, _config_error(text))


def test_repos_must_be_a_table():
    assert "repos must be a table" in _config_error(f"repos = 3\n{config_mod.default_toml()}")


@pytest.mark.parametrize(
    "goal, expected",
    [
        ("Reviews webhook", "reviews-webhook-0929"),
        ("  Add CSV export!!  to the reports page ", "add-csv-export-to-the-reports-page-0929"),
        ("x" * 100, "x" * 40 + "-0929"),
        ("word " * 20, "word-" * 7 + "word-0929"),
        ("héllo wörld", "h-llo-w-rld-0929"),
        ("!!!", "run-0929"),
        ("", "run-0929"),
    ],
)
def test_slugify(goal, expected):
    assert slugify(goal, date(2026, 9, 29)) == expected


def test_slugify_shape_and_date_suffix():
    slug = slugify("Fix the login bug on the Mobile app: it crashes", date(2026, 1, 5))
    assert slug.endswith("-0105")
    text = slug[: -len("-0105")]
    assert len(text) <= 40 and text == text.strip("-")
    assert all(ch in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in slug)


def test_ws_slug_and_layout(tmp_path: Path):
    root = tmp_path / "code"
    root.mkdir()
    home = tmp_path / "home"
    paths = Paths(root, home=home)
    digest = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:8]
    assert paths.ws_slug == f"code-{digest}" == paths.repo_slug
    assert paths.workspace_root == root == paths.repo
    assert paths.root == root / ".workforce"
    assert paths.config == root / "workforce.toml"
    assert paths.worktrees_root == home / ".workforce" / "worktrees" / paths.ws_slug

    base = paths.worktrees_root / "r1"
    assert paths.worktree("r1", "T1") == base / "T1"
    assert paths.worktree("r1", "T1", "alpha") == base / "alpha" / "T1"
    assert paths.worktree("r1", "T1", "tools/gamma") == base / "tools" / "gamma" / "T1"
    assert paths.integration_worktree("r1", "alpha") == base / "alpha" / "_integration"
    assert paths.integration_worktree("r1", "tools/gamma") == base / "tools" / "gamma" / "_integration"
    assert not home.exists()


@pytest.mark.parametrize("repo_name", ["..", "../x", "a/../../x", "a/..", "/abs", "a//b", "", ".", "a/.", "a\\b", "a\x00b"])
def test_worktree_paths_reject_repo_names_that_escape(tmp_path: Path, repo_name: str):
    paths = Paths(tmp_path / "ws", home=tmp_path / "home")
    with pytest.raises(PathEscapeError):
        paths.worktree("r1", "T1", repo_name)
    with pytest.raises(PathEscapeError):
        paths.integration_worktree("r1", repo_name)


def test_worktree_paths_reject_escaping_run_and_task_ids(tmp_path: Path):
    paths = Paths(tmp_path / "ws", home=tmp_path / "home")
    for bad in ("..", "a/b", ""):
        with pytest.raises(PathEscapeError):
            paths.worktree(bad, "T1", "alpha")
        with pytest.raises(PathEscapeError):
            paths.worktree("r1", bad, "alpha")
        with pytest.raises(PathEscapeError):
            paths.integration_worktree(bad, "alpha")
    with pytest.raises(PathEscapeError, match="reserved"):
        paths.worktree("r1", "_integration", "alpha")


def test_worktree_paths_reject_symlink_escape(tmp_path: Path):
    paths = Paths(tmp_path / "ws", home=tmp_path / "home")
    run_dir = paths.worktree_dir_for("r1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / "alpha").symlink_to(outside)
    with pytest.raises(PathEscapeError):
        paths.worktree("r1", "T1", "alpha")
    with pytest.raises(PathEscapeError):
        paths.integration_worktree("r1", "alpha")


def test_ensure_on_non_git_root_skips_exclude(git_workspace: Path, tmp_path: Path):
    paths = Paths(git_workspace, home=tmp_path / "home")
    paths.ensure()
    paths.ensure()
    assert paths.root.is_dir() and paths.runs.is_dir()
    assert not (git_workspace / ".git").exists()
    for name in ("alpha", "beta", "tools/gamma"):
        status = subprocess.run(
            ["git", "-C", str(git_workspace / name), "status", "--porcelain"], capture_output=True, text=True, check=True
        )
        assert status.stdout == ""


def test_ensure_does_not_write_into_an_enclosing_repo(git_repo: Path, tmp_path: Path):
    nested = git_repo / "sub"
    nested.mkdir()
    Paths(nested, home=tmp_path / "home").ensure()
    assert (nested / ".workforce" / "runs").is_dir()
    exclude = git_repo / ".git" / "info" / "exclude"
    assert ".workforce/" not in (exclude.read_text() if exclude.exists() else "")


def test_ensure_still_excludes_at_a_repo_top_level(git_repo: Path, tmp_path: Path):
    Paths(git_repo, home=tmp_path / "home").ensure()
    lines = (git_repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".workforce/" in lines and "/workforce.toml" in lines


OLD_STATE = {
    "id": "20260901-000000",
    "goal": "old goal",
    "created_at": "2026-09-01T00:00:00+00:00",
    "status": "running",
    "pause_reason": None,
    "tasks": [
        {
            "id": "T1",
            "title": "t",
            "description": "d",
            "acceptance": ["a"],
            "depends_on": [],
            "model": "claude-sonnet-5-5",
            "effort": "high",
            "agent": "claude",
            "status": "merged",
            "branch": "wf/r/T1",
            "worktree": None,
            "base_sha": "abc",
            "head_sha": "def",
            "coder_session": None,
            "review_round": 0,
            "reviews": [],
            "infra_failures": 0,
            "computer_use": False,
        }
    ],
    "questions": [],
    "mode": "auto",
}


def test_old_state_file_loads_with_new_fields_defaulted(tmp_path: Path):
    paths = Paths(tmp_path / "repo", home=tmp_path / "home")
    paths.root.mkdir(parents=True)
    paths.state.write_text(json.dumps(OLD_STATE))
    run = StateStore(paths).load()
    assert run is not None
    assert run.slug is None and run.integration == {}
    assert run.task("T1").repo is None and run.task("T1").status == "merged"


def test_new_state_fields_round_trip(tmp_path: Path):
    store = StateStore(Paths(tmp_path / "repo", home=tmp_path / "home"))
    run = Run.create("r1", "goal", slug="goal-0929")
    run.tasks.append(Task(id="T1", title="t", description="d", repo="alpha"))
    run.integration["alpha"] = IntegrationInfo(
        repo="alpha",
        base_ref="main",
        base_sha="a" * 40,
        branch="wf/goal-0929",
        worktree="/w/r1/alpha/_integration",
        created=True,
    )
    store.save(run)
    loaded = store.load()
    assert loaded == run
    assert loaded.integration["alpha"].created is True
    assert json.loads(store.paths.state.read_text())["integration"]["alpha"]["branch"] == "wf/goal-0929"


def test_integration_state_validation(tmp_path: Path):
    data = dict(OLD_STATE, integration={"alpha": {"repo": "beta", "base_ref": "m", "base_sha": "s", "branch": "b", "worktree": "w", "created": True}})
    with pytest.raises(StateError, match=r"integration\['alpha'\]"):
        Run.from_dict(data)
    data = dict(OLD_STATE, integration={"alpha": {"repo": "alpha"}})
    with pytest.raises(StateError, match="missing keys"):
        Run.from_dict(data)
    with pytest.raises(StateError, match="must be an object"):
        Run.from_dict(dict(OLD_STATE, integration=[]))


def test_write_lock_is_unchanged_with_workspace_paths(tmp_path: Path):
    store = StateStore(Paths(tmp_path / "ws", home=tmp_path / "home"))
    with store.write_lock(), store.write_lock():
        pass
    assert (store.paths.root / "state.write.lock").exists()


REPOS = ["alpha", "beta"]


def _plan(*repos):
    return {
        "tasks": [
            {"id": f"T{i + 1}", "title": "t", "description": "d", "acceptance": [], "depends_on": [], "repo": repo}
            for i, repo in enumerate(repos)
        ],
        "risks": [],
        "open_questions": [],
    }


def test_plan_schema_requires_a_repo_enum_per_task():
    schema = schemas.plan_schema(REPOS)
    task = schema["properties"]["tasks"]["items"]
    assert task["properties"]["repo"] == {"type": "string", "enum": REPOS}
    assert "repo" in task["required"] and task["additionalProperties"] is False
    assert schemas.validate(schema, _plan("alpha", "beta")) == []
    assert any("not one of" in e for e in schemas.validate(schema, _plan("zeta")))
    without = _plan("alpha")
    del without["tasks"][0]["repo"]
    assert any("missing required property 'repo'" in e for e in schemas.validate(schema, without))


def test_plan_revision_schema_wraps_the_repo_plan():
    schema = schemas.plan_revision_schema(REPOS)
    debate = {"position": "p", "concessions": [], "remaining_disagreements": []}
    assert schemas.validate(schema, {"plan": _plan("beta"), "debate": debate}) == []
    assert schemas.validate(schema, {"plan": _plan("zeta"), "debate": debate})
    assert schema["required"] == ["plan", "debate"]


def test_static_schemas_are_unchanged_and_do_not_share_state():
    assert "repo" not in schemas.PLAN["properties"]["tasks"]["items"]["properties"]
    schemas.plan_schema(REPOS)
    assert "repo" not in schemas.PLAN["properties"]["tasks"]["items"]["properties"]
    assert schemas.PLAN_REVISION["properties"]["plan"] == schemas.PLAN
    assert schemas.plan_schema(["a"]) != schemas.plan_schema(["b"])


def test_plan_schema_needs_repos():
    with pytest.raises(ValueError):
        schemas.plan_schema([])
    with pytest.raises(ValueError):
        schemas.plan_revision_schema([])


def test_validate_plan_repos():
    assert schemas.validate_plan_repos(_plan("alpha", "beta"), REPOS) == []
    errors = schemas.validate_plan_repos(_plan("alpha", "zeta"), REPOS)
    assert len(errors) == 1 and "T2" in errors[0] and "'zeta'" in errors[0] and "alpha" in errors[0]
    missing = _plan("alpha")
    del missing["tasks"][0]["repo"]
    assert "T1 has no repo" in schemas.validate_plan_repos(missing, REPOS)[0]
    assert schemas.validate_plan_repos({"tasks": ["x"]}, REPOS) == ["plan.tasks[0] must be an object"]
    assert schemas.validate_plan_repos({}, REPOS) == ["plan.tasks must be a list"]
    assert schemas.validate_plan_repos(None, REPOS) == ["plan.tasks must be a list"]


def test_publish_result_schema():
    ok = {"pushed": True, "pr_url": "https://x/pr/1", "error": None}
    assert schemas.validate(schemas.PUBLISH_RESULT, ok) == []
    assert schemas.validate(schemas.PUBLISH_RESULT, {"pushed": False, "pr_url": None, "error": "no remote"}) == []
    assert schemas.validate(schemas.PUBLISH_RESULT, {"pushed": "yes", "pr_url": None, "error": None})
    assert schemas.validate(schemas.PUBLISH_RESULT, {"pushed": True})
    assert schemas.ALL["publish_result"] is schemas.PUBLISH_RESULT
