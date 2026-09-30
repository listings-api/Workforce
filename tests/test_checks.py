import pytest
import json
from pathlib import Path

from workforce import checks
from workforce.checks import CheckReport, CheckResult


def _python_tests(root):
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_sample.py").write_text("")


@pytest.fixture(autouse=True)
def _interpreters_have_pytest(request, monkeypatch):
    if "real_pytest_probe" in request.keywords:
        return
    monkeypatch.setattr(checks, "_has_pytest", lambda interpreter: True)


@pytest.mark.real_pytest_probe
def test_python_without_pytest_is_skipped_with_a_reason(tmp_path, monkeypatch):
    _python_tests(tmp_path)
    monkeypatch.setattr(checks, "_has_pytest", lambda interpreter: False)
    commands, reasons = checks.detect_with_reasons(tmp_path)
    assert commands.get("test", []) == []
    assert "no interpreter with pytest installed" in reasons["test"]
    assert "[checks] test" in reasons["test"]


@pytest.mark.real_pytest_probe
def test_first_interpreter_with_pytest_wins(tmp_path, monkeypatch):
    _python_tests(tmp_path)
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("")
    monkeypatch.setattr(checks, "_has_pytest", lambda interpreter: not interpreter.endswith(".venv/bin/python"))
    commands, _ = checks.detect_with_reasons(tmp_path)
    assert commands["test"] and ".venv/bin/python" not in commands["test"][0]


def empty_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    return repo


def test_detect_nothing(tmp_path):
    assert checks.detect(empty_repo(tmp_path)) == {"test": [], "lint": [], "build": []}


def fake_venv(root: Path) -> Path:
    venv_python = root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    return venv_python


def patch_which(monkeypatch, found: dict[str, str]):
    monkeypatch.setattr(checks.shutil, "which", lambda name: found.get(name))


def test_detect_python_pyproject_uses_path_python3(tmp_path, monkeypatch):
    patch_which(monkeypatch, {"python3": "/usr/bin/python3", "python": "/usr/bin/python"})
    repo = empty_repo(tmp_path)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert checks.detect(repo)["test"] == ["/usr/bin/python3 -m pytest -q"]


def test_detect_python_pytest_ini_and_tests_dir_single_entry(tmp_path, monkeypatch):
    patch_which(monkeypatch, {"python3": "/usr/bin/python3"})
    repo = empty_repo(tmp_path)
    (repo / "pytest.ini").write_text("[pytest]\n")
    _python_tests(repo)
    assert checks.detect(repo)["test"] == ["/usr/bin/python3 -m pytest -q"]


def test_detect_python_falls_back_to_python_when_no_python3(tmp_path, monkeypatch):
    patch_which(monkeypatch, {"python": "/usr/bin/python"})
    repo = empty_repo(tmp_path)
    _python_tests(repo)
    assert checks.detect(repo)["test"] == ["/usr/bin/python -m pytest -q"]


def test_detect_python_prefers_repo_venv(tmp_path, monkeypatch):
    patch_which(monkeypatch, {"python3": "/usr/bin/python3"})
    repo = empty_repo(tmp_path)
    env_root = tmp_path / "main"
    _python_tests(repo)
    repo_python = fake_venv(repo)
    fake_venv(env_root)
    assert checks.detect(repo, env_root=env_root)["test"] == [f"{repo_python} -m pytest -q"]


def test_detect_python_uses_env_root_venv_when_repo_has_none(tmp_path, monkeypatch):
    patch_which(monkeypatch, {"python3": "/usr/bin/python3"})
    worktree = empty_repo(tmp_path)
    _python_tests(worktree)
    env_root = tmp_path / "main"
    env_python = fake_venv(env_root)
    assert checks.detect(worktree, env_root=env_root)["test"] == [f"{env_python} -m pytest -q"]


def test_detect_python_env_root_without_venv_falls_to_path(tmp_path, monkeypatch):
    patch_which(monkeypatch, {"python3": "/usr/bin/python3"})
    worktree = empty_repo(tmp_path)
    _python_tests(worktree)
    env_root = tmp_path / "main"
    env_root.mkdir()
    assert checks.detect(worktree, env_root=env_root)["test"] == ["/usr/bin/python3 -m pytest -q"]


def test_detect_python_quotes_interpreter_path(tmp_path, monkeypatch):
    patch_which(monkeypatch, {})
    repo = tmp_path / "my proj"
    repo.mkdir()
    _python_tests(repo)
    venv_python = fake_venv(repo)
    assert checks.detect(repo)["test"] == [f"'{venv_python}' -m pytest -q"]


def test_detect_python_no_interpreter_records_reason(tmp_path, monkeypatch):
    patch_which(monkeypatch, {})
    repo = empty_repo(tmp_path)
    _python_tests(repo)
    found, reasons = checks.detect_with_reasons(repo, env_root=tmp_path / "nowhere")
    assert found["test"] == []
    assert "no interpreter" in reasons["test"]


def test_run_python_no_interpreter_is_skipped_with_reason(tmp_path, monkeypatch):
    patch_which(monkeypatch, {})
    repo = empty_repo(tmp_path)
    _python_tests(repo)
    report = checks.run(repo)
    test = next(r for r in report.results if r.kind == "test")
    assert test.skipped
    assert "no interpreter" in test.skip_reason
    assert report.no_checks and report.ok
    assert "no interpreter" in checks.summary(report)


def test_configured_test_command_bypasses_missing_interpreter(tmp_path, monkeypatch):
    patch_which(monkeypatch, {})
    repo = empty_repo(tmp_path)
    _python_tests(repo)
    found, reasons = checks.detect_with_reasons(repo, {"test": "echo hi"})
    assert found["test"] == ["echo hi"]
    assert reasons == {}
    assert checks.run(repo, {"test": "echo hi"}).ok


def test_missing_interpreter_reason_dropped_when_other_test_command_exists(tmp_path, monkeypatch):
    patch_which(monkeypatch, {})
    repo = empty_repo(tmp_path)
    _python_tests(repo)
    (repo / "Makefile").write_text("test:\n\t@echo made\n")
    found, reasons = checks.detect_with_reasons(repo)
    assert found["test"] == ["make test"]
    assert reasons == {}


def test_run_passes_env_root_to_detection(tmp_path, monkeypatch):
    patch_which(monkeypatch, {})
    worktree = empty_repo(tmp_path)
    _python_tests(worktree)
    env_root = tmp_path / "main"
    env_python = env_root / ".venv" / "bin" / "python"
    env_python.parent.mkdir(parents=True)
    env_python.write_text("#!/bin/sh\necho ran-with-env-python \"$@\"\n")
    env_python.chmod(0o755)
    report = checks.run(worktree, env_root=env_root)
    test = next(r for r in report.results if r.kind == "test")
    assert report.ok
    assert "ran-with-env-python -m pytest -q" in test.output_tail


def test_detect_npm_only_existing_scripts(tmp_path):
    repo = empty_repo(tmp_path)
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest", "build": "tsc"}}))
    found = checks.detect(repo)
    assert found["test"] == ["npm run test"]
    assert found["build"] == ["npm run build"]
    assert found["lint"] == []


def test_detect_npm_without_scripts_or_malformed(tmp_path):
    repo = empty_repo(tmp_path)
    (repo / "package.json").write_text("{not json")
    assert checks.detect(repo) == {"test": [], "lint": [], "build": []}
    (repo / "package.json").write_text(json.dumps({"name": "x"}))
    assert checks.detect(repo) == {"test": [], "lint": [], "build": []}


def test_detect_go(tmp_path):
    repo = empty_repo(tmp_path)
    (repo / "go.mod").write_text("module x\n")
    found = checks.detect(repo)
    assert found["test"] == ["go test ./..."]
    assert found["lint"] == ["go vet ./..."]


def test_detect_cargo(tmp_path):
    repo = empty_repo(tmp_path)
    (repo / "Cargo.toml").write_text("[package]\nname='x'\n")
    assert checks.detect(repo)["test"] == ["cargo test"]


def test_detect_makefile_with_test_target(tmp_path):
    repo = empty_repo(tmp_path)
    (repo / "Makefile").write_text("build:\n\techo b\n\ntest:\n\techo t\n")
    assert checks.detect(repo)["test"] == ["make test"]


def test_detect_makefile_without_test_target(tmp_path):
    repo = empty_repo(tmp_path)
    (repo / "Makefile").write_text("build:\n\techo b\n# test: not a target\n")
    assert checks.detect(repo)["test"] == []


def test_configured_overrides_per_kind_and_empty_means_detect(tmp_path):
    repo = empty_repo(tmp_path)
    (repo / "go.mod").write_text("module x\n")
    found = checks.detect(repo, {"test": "make check", "lint": "", "build": ["a", "b"]})
    assert found["test"] == ["make check"]
    assert found["lint"] == ["go vet ./..."]
    assert found["build"] == ["a", "b"]


def test_run_pass(tmp_path):
    repo = empty_repo(tmp_path)
    report = checks.run(repo, {"test": "echo hello && true"})
    assert report.ok and not report.no_checks
    ran = [r for r in report.results if not r.skipped]
    assert len(ran) == 1
    assert ran[0].kind == "test" and ran[0].exit_code == 0
    assert "hello" in ran[0].output_tail
    assert ran[0].duration_s >= 0
    assert {r.kind for r in report.results if r.skipped} == {"lint", "build"}


def test_run_fail_records_exit_code_and_output(tmp_path):
    repo = empty_repo(tmp_path)
    report = checks.run(repo, {"test": "true", "lint": "echo bad >&2; exit 3"})
    assert not report.ok
    lint = next(r for r in report.results if r.kind == "lint")
    assert lint.exit_code == 3
    assert "bad" in lint.output_tail
    test = next(r for r in report.results if r.kind == "test")
    assert test.exit_code == 0


def test_run_multiple_commands_all_run_and_one_failure_fails(tmp_path):
    repo = empty_repo(tmp_path)
    report = checks.run(repo, {"test": ["exit 1", "echo second"]})
    assert not report.ok
    cmds = [r.cmd for r in report.results if r.kind == "test"]
    assert cmds == ["exit 1", "echo second"]


def test_run_timeout(tmp_path):
    repo = empty_repo(tmp_path)
    report = checks.run(repo, {"test": "echo start; sleep 30"}, timeout_s=1)
    assert not report.ok
    result = next(r for r in report.results if r.kind == "test")
    assert result.timed_out
    assert result.exit_code is None
    assert "start" in result.output_tail
    assert result.duration_s < 20


def test_run_uses_repo_as_cwd(tmp_path):
    repo = empty_repo(tmp_path)
    (repo / "marker.txt").write_text("here")
    report = checks.run(repo, {"test": "cat marker.txt"})
    assert report.ok
    assert "here" in report.results[0].output_tail


def test_run_no_checks_flag(tmp_path):
    report = checks.run(empty_repo(tmp_path), {"test": "", "lint": "", "build": ""})
    assert report.ok
    assert report.no_checks
    assert all(r.skipped for r in report.results)
    assert "NO CHECKS" in checks.summary(report)


def test_run_autodetected(tmp_path):
    repo = empty_repo(tmp_path)
    (repo / "Makefile").write_text("test:\n\t@echo made\n")
    report = checks.run(repo)
    assert report.ok and not report.no_checks
    assert "made" in report.results[0].output_tail


def test_output_tail_limited_to_200_lines(tmp_path):
    repo = empty_repo(tmp_path)
    report = checks.run(repo, {"test": "seq 1 500"})
    lines = report.results[0].output_tail.splitlines()
    assert len(lines) == 200
    assert lines[0] == "301" and lines[-1] == "500"


def test_summary_pass_and_fail(tmp_path):
    repo = empty_repo(tmp_path)
    ok = checks.summary(checks.run(repo, {"test": "true"}))
    assert "Checks: OK" in ok and "test: PASS" in ok and "SKIPPED" in ok

    bad = checks.summary(checks.run(repo, {"test": "echo boom; exit 2"}))
    assert "Checks: FAILED" in bad and "FAIL (exit 2)" in bad and "boom" in bad


def test_summary_capped_at_60_lines():
    results = [
        CheckResult("test", "t1", 1, 1.0, "\n".join(f"t{i}" for i in range(200))),
        CheckResult("lint", "l1", 1, 1.0, "\n".join(f"l{i}" for i in range(200))),
        CheckResult("build", "b1", None, 1.0, "\n".join(f"b{i}" for i in range(200)), timed_out=True),
    ]
    text = checks.summary(CheckReport(ok=False, results=results))
    assert len(text.splitlines()) <= 60
    assert "t199" in text and "l199" in text and "b199" in text
    assert "TIMEOUT" in text


def test_summary_capped_with_many_results():
    results = [CheckResult("test", f"cmd{i}", 1, 0.1, "x\ny") for i in range(80)]
    text = checks.summary(CheckReport(ok=False, results=results))
    assert len(text.splitlines()) <= 60


def test_run_in_an_integration_worktree_uses_the_users_venv_as_env_root(git_repo, tmp_path):
    import subprocess

    from workforce import git_ops

    (git_repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    _python_tests(git_repo)
    (git_repo / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    for args in (("add", "-A"), ("commit", "-m", "add project")):
        subprocess.run(["git", "-C", str(git_repo), *args], check=True, capture_output=True)
    marker = tmp_path / "ran-here.txt"
    venv_python = git_repo / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(f"#!/bin/sh\npwd > {marker}\necho \"$@\" >> {marker}\nexit 0\n")
    venv_python.chmod(0o755)

    git_ops.create_branch(git_repo, "wf/goal-0929", git_ops.resolve_ref(git_repo, "main"))
    integration = tmp_path / "worktrees" / "slug" / "run1" / "app" / "_integration"
    git_ops.add_worktree_for_branch(git_repo, integration, "wf/goal-0929")
    assert not (integration / ".venv").exists()

    report = checks.run(integration, {"test": "", "lint": "", "build": ""}, env_root=git_repo)
    assert report.ok and not report.no_checks
    test = next(r for r in report.results if r.kind == "test")
    assert test.cmd == f"{venv_python} -m pytest -q" and test.exit_code == 0
    lines = marker.read_text().splitlines()
    assert Path(lines[0]).resolve() == integration.resolve()
    assert lines[1] == "-m pytest -q"


def test_run_in_an_integration_worktree_honours_per_repo_commands(git_repo, tmp_path):
    from workforce import git_ops

    git_ops.create_branch(git_repo, "wf/goal-0929", git_ops.resolve_ref(git_repo, "main"))
    integration = tmp_path / "wt" / "_integration"
    git_ops.add_worktree_for_branch(git_repo, integration, "wf/goal-0929")
    report = checks.run(integration, {"test": "pwd > seen.txt && test -f README.md"}, env_root=git_repo)
    assert report.ok
    assert (integration / "seen.txt").read_text().strip() == str(integration.resolve())
    assert not (git_repo / "seen.txt").exists()


def test_a_bare_tests_folder_in_a_node_repo_is_not_python(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "client.test.js").write_text("")
    commands, _ = checks.detect_with_reasons(tmp_path)
    assert commands["test"] == ["npm run test"]


def test_python_test_files_under_tests_are_python(tmp_path):
    _python_tests(tmp_path)
    (tmp_path / "tests" / "test_x.py").write_text("")
    commands, _ = checks.detect_with_reasons(tmp_path)
    assert any("pytest" in c for c in commands["test"])
