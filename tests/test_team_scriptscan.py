import json
import os
import stat

import pytest

from workforce.team import hooks, scriptscan

SHIP = 'import subprocess\nsubprocess.run(["git", "commit", "--no-verify", "-m", "x"])\n'


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


@pytest.fixture
def proj(tmp_path):
    path = tmp_path / "proj"
    path.mkdir()
    return path


def pre(tool, tool_input, cwd, home):
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}
    return hooks.evaluate("pretooluse", payload, env={}, home=home, now=1_800_000_000.0)


def bash(command, cwd, home):
    return pre("Bash", {"command": command}, cwd, home)


def denied(output):
    assert output and output["hookSpecificOutput"]["permissionDecision"] == "deny"
    return output["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "text",
    [
        "git commit --no-verify",
        "git commit-tree abc",
        "git update-ref HEAD abc",
        "git config core.hooksPath /x",
        "os.environ['GIT_CONFIG_COUNT'] = '1'",
        "GIT_CONFIG_KEY_0=a",
        "GIT_CONFIG_VALUE_0=a",
        "GIT_CONFIG_PARAMETERS=x",
        "env -i git commit",
        "env --ignore-environment git commit",
        "env -u HOME git commit",
        '["env", "-i", "git", "commit"]',
        "GIT_INDEX_FILE=/tmp/x git commit",
        "open(os.path.expanduser('~/.workforce/team.key'))",
        "open('~/.workforce/te' + 'am.k' + 'ey')",
        "open(''.join(['team', '.key']))",
        "cat wf-approvals.json",
        "import workforce.team.approvals",
        "from workforce.team import approvals",
        "from workforce.team import (approvals)",
        "approvals.record(repo, 'codex', 'APPROVE', 'x')",
        "load_key()",
        "reference-transaction",
        "GIT_DIR=/tmp/x.git git commit -m x",
        "git commit -m x; export GIT_DIR=/tmp/y",
        'GIT_DIR = "/tmp/x"; run("commit")',
        "GIT --NO-VERIFY",
    ],
)
def test_scan_text_matches(text):
    found = scriptscan.scan_text(text)
    assert found and found.startswith("`") and "(" in found


@pytest.mark.parametrize(
    "text",
    [
        "print('hello')",
        "git status && git diff",
        "git commit -m 'fix env handling'",
        "GIT_DIR=/tmp/x git status",
        "environment-user = 1",
        "def commit(): pass",
        "",
    ],
)
def test_scan_text_clean(text):
    assert scriptscan.scan_text(text) is None


def test_scan_text_reports_match_and_reason():
    assert scriptscan.scan_text("git commit --no-verify") == "`--no-verify` (skips the commit hooks)"


def test_write_of_ship_py_denied(proj, home):
    reason = denied(pre("Write", {"file_path": str(proj / "ship.py"), "content": SHIP}, proj, home))
    assert "--no-verify" in reason
    assert "ask the user to make this change by hand" in reason
    assert "blocked writing this file" in reason


def test_written_by_hand_then_run_denied(proj, home):
    (proj / "ship.py").write_text(SHIP)
    reason = denied(bash("python3 ship.py", proj, home))
    assert "ship.py" in reason and "--no-verify" in reason


@pytest.mark.parametrize(
    "command",
    ["python ship.py", "python3 -u ship.py", "python3.12 ship.py", ".venv/bin/python -W ignore ship.py", "python -B -- ship.py", "FOO=1 python ship.py", "cd . && python3 ship.py", "python3 ./ship.py; true", "sudo python3 ship.py", "time python3 ship.py"],
)
def test_python_invocations_scanned(proj, home, command):
    (proj / "ship.py").write_text(SHIP)
    denied(bash(command, proj, home))


def test_python_module_scanned(proj, home):
    (proj / "ship.py").write_text(SHIP)
    denied(bash("python3 -m ship", proj, home))


def test_python_flag_after_script_does_not_hide_it(proj, home):
    (proj / "ship.py").write_text(SHIP)
    denied(bash("python3 ship.py -c x", proj, home))


def test_inline_c_has_no_file(proj, home):
    assert scriptscan.scripts_run_by("python3 -c 'print(1)'", proj) == []


def test_script_reading_key_by_concatenation(proj, home):
    (proj / "forge.py").write_text("import os\nkey = open(os.path.expanduser('~/.workforce/' + 'team' + '.ke' + 'y')).read()\n")
    reason = denied(bash("python3 forge.py", proj, home))
    assert "forge.py" in reason


def test_write_of_key_reader_denied(proj, home):
    content = "p = '~/.workforce/' + 'team' + '.key'\n"
    denied(pre("Write", {"file_path": str(proj / "x.py"), "content": content}, proj, home))


@pytest.mark.parametrize("shell", ["bash", "sh", "zsh"])
def test_shell_scripts_scanned(proj, home, shell):
    (proj / "ship.sh").write_text("#!/bin/sh\ngit commit --no-verify -m x\n")
    denied(bash(f"{shell} ship.sh", proj, home))
    denied(bash("source ship.sh", proj, home))


def test_executable_path_scanned(proj, home):
    script = proj / "ship"
    script.write_text("#!/bin/sh\ngit commit --no-verify -m x\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    denied(bash("./ship", proj, home))
    denied(bash(f"{script}", proj, home))


def test_non_executable_path_not_a_script(proj):
    script = proj / "ship"
    script.write_text("#!/bin/sh\ngit commit --no-verify\n")
    script.chmod(0o644)
    assert scriptscan.scripts_run_by("./ship", proj) == []


@pytest.mark.parametrize("interpreter", ["node", "ruby", "perl"])
def test_other_interpreters(proj, home, interpreter):
    (proj / "ship.js").write_text("run('git commit --no-verify')\n")
    denied(bash(f"{interpreter} ship.js", proj, home))


@pytest.mark.parametrize("name", ["Makefile", "makefile", "GNUmakefile"])
def test_make_scans_makefile(proj, home, name):
    (proj / name).write_text("ship:\n\tgit commit --no-verify -m x\n")
    reason = denied(bash("make ship", proj, home))
    assert name.lower() in reason.lower()
    denied(bash("make", proj, home))


def test_make_dash_f_and_dash_c(proj, home):
    (proj / "build.mk").write_text("ship:\n\tgit update-ref HEAD abc\n")
    denied(bash("make -f build.mk ship", proj, home))
    sub = proj / "sub"
    sub.mkdir()
    (sub / "Makefile").write_text("ship:\n\tgit commit --no-verify\n")
    denied(bash("make -C sub ship", proj, home))
    denied(bash("cd sub && make ship", proj, home))


def test_clean_makefile_allowed(proj, home):
    (proj / "Makefile").write_text("test:\n\tpytest -q\n")
    assert bash("make test", proj, home) is None


@pytest.mark.parametrize("command", ["npm run ship", "pnpm run ship", "yarn run ship", "npm test", "yarn ship", "pnpm ship"])
def test_package_json_scripts_scanned(proj, home, command):
    (proj / "package.json").write_text(json.dumps({"name": "x", "scripts": {"ship": "git commit --no-verify -m x", "test": "jest"}}))
    denied(bash(command, proj, home))


def test_package_json_outside_scripts_ignored(proj, home):
    (proj / "package.json").write_text(json.dumps({"name": "x", "description": "about --no-verify", "scripts": {"test": "jest"}}))
    assert bash("npm run test", proj, home) is None


def test_package_json_found_in_parent(proj, home):
    (proj / "package.json").write_text(json.dumps({"scripts": {"ship": "git commit --no-verify"}}))
    sub = proj / "pkg"
    sub.mkdir()
    denied(bash("npm run ship", sub, home))


def test_uv_run_file_and_wrapped_python(proj, home):
    (proj / "ship.py").write_text(SHIP)
    denied(bash("uv run ship.py", proj, home))
    denied(bash("uv run --with requests ship.py", proj, home))
    denied(bash("uv run python ship.py", proj, home))
    denied(bash("uv run -- python3 ship.py", proj, home))


@pytest.mark.parametrize("name", ["justfile", "Justfile"])
def test_just_scans_justfile(proj, home, name):
    (proj / name).write_text("ship:\n    git commit --no-verify\n")
    denied(bash("just ship", proj, home))


def test_benign_script_allowed(proj, home):
    (proj / "hello.py").write_text("print('hello')\nimport subprocess\nsubprocess.run(['git', 'status'])\n")
    assert bash("python3 hello.py", proj, home) is None
    assert pre("Write", {"file_path": str(proj / "hello.py"), "content": "print('hi')\n"}, proj, home) is None


@pytest.mark.parametrize("name", ["NOTES.md", "guide.txt", "docs/setup.rst", "README.MD"])
def test_docs_files_may_mention_the_words(proj, home, name):
    content = "Do not use `git commit --no-verify` or read team.key.\n"
    assert pre("Write", {"file_path": str(proj / name), "content": content}, proj, home) is None
    assert pre("Edit", {"file_path": str(proj / name), "old_string": "a", "new_string": content}, proj, home) is None


def test_docs_exemption_needs_a_doc_path(proj, home):
    denied(pre("Write", {"file_path": str(proj / "run.sh"), "content": "git commit --no-verify\n"}, proj, home))
    denied(pre("Write", {"file_path": str(proj / "Makefile"), "content": "x:\n\tgit commit --no-verify\n"}, proj, home))
    denied(pre("Write", {"file_path": str(proj / "package.json"), "content": '{"scripts":{"a":"git commit --no-verify"}}'}, proj, home))
    denied(pre("Write", {"file_path": str(proj / "ci.yaml"), "content": "run: git commit --no-verify\n"}, proj, home))


def test_edit_new_string_scanned(proj, home):
    denied(pre("Edit", {"file_path": str(proj / "a.py"), "old_string": "x", "new_string": "git update-ref HEAD abc"}, proj, home))


def test_multiedit_edits_scanned(proj, home):
    edits = [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "run('git commit --no-verify')"}]
    denied(pre("MultiEdit", {"file_path": str(proj / "a.py"), "edits": edits}, proj, home))
    clean = [{"old_string": "a", "new_string": "b"}]
    assert pre("MultiEdit", {"file_path": str(proj / "a.py"), "edits": clean}, proj, home) is None


def test_notebook_new_source_scanned(proj, home):
    denied(pre("NotebookEdit", {"notebook_path": str(proj / "n.ipynb"), "new_source": "!git commit --no-verify"}, proj, home))


def test_old_string_is_not_scanned(proj, home):
    tool_input = {"file_path": str(proj / "a.py"), "old_string": "git commit --no-verify", "new_string": "pass"}
    assert pre("Edit", tool_input, proj, home) is None


def test_large_file_skipped(proj, home):
    (proj / "big.py").write_text(SHIP + "#" * (scriptscan.MAX_BYTES + 10))
    assert scriptscan.scripts_run_by("python3 big.py", proj) == []
    assert bash("python3 big.py", proj, home) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_file_skipped(proj, home):
    script = proj / "locked.py"
    script.write_text(SHIP)
    script.chmod(0)
    try:
        assert scriptscan.scripts_run_by("python3 locked.py", proj) == []
        assert bash("python3 locked.py", proj, home) is None
    finally:
        script.chmod(0o644)


def test_missing_and_binary_files_skipped(proj, home):
    assert scriptscan.scripts_run_by("python3 nothere.py", proj) == []
    binary = proj / "tool"
    binary.write_bytes(b"\x7fELF\0\0git commit --no-verify")
    binary.chmod(0o755)
    assert scriptscan.scan_file(binary) is None
    assert bash("./tool", proj, home) is None


def test_at_most_twenty_files(proj):
    for index in range(25):
        (proj / f"s{index}.py").write_text("pass\n")
    command = " && ".join(f"python3 s{index}.py" for index in range(25))
    assert len(scriptscan.scripts_run_by(command, proj)) == scriptscan.MAX_FILES


def test_dynamic_paths_ignored(proj):
    assert scriptscan.scripts_run_by("python3 $SCRIPT && bash *.sh", proj) == []


def test_script_written_in_the_same_command(proj, home):
    command = "printf 'git commit --no-verify -m x' > ship.sh && sh ship.sh"
    reason = denied(bash(command, proj, home))
    assert "ship.sh" in reason
    assert bash("printf 'echo hi' > ok.sh && sh ok.sh", proj, home) is None


def test_scan_errors_fail_open(proj, home, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(scriptscan, "scripts_run_by", boom)
    assert bash("python3 hello.py", proj, home) is None


def test_existing_protection_still_applies(proj, home):
    assert "approvals key" in denied(pre("Read", {"file_path": str(home / ".workforce" / "team.key")}, proj, home))
