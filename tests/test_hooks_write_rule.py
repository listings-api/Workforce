"""Hook rule 9: an agent writes only inside WF_WORKTREE (plus /tmp, $TMPDIR and /dev/null)."""

import os
from pathlib import Path

import pytest

from tests.test_hooks import FakeDecider, bash, events_of, run_main
from workforce.decider import hooks

HOME = Path("/Users/tester")
WT = "/work/repo-wt"
OUTSIDE = "/opt/wf-outside"
MODES = ["agent", "committer"]


def reason(tool, tool_input, mode="agent", cwd=WT, worktree=WT):
    return hooks.denylist_reason(tool, tool_input, cwd, worktree, HOME, mode=mode)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_file_tools_are_confined_to_the_worktree(tool, key, mode):
    assert reason(tool, {key: f"{WT}/src/a.py"}, mode) is None
    assert reason(tool, {key: "src/a.py"}, mode) is None
    assert reason(tool, {key: f"{OUTSIDE}/a.py"}, mode)
    assert reason(tool, {key: "/etc/hosts"}, mode)
    assert reason(tool, {key: "~/notes.txt"}, mode)
    assert reason(tool, {key: f"{WT}/../other/a.py"}, mode)
    assert reason(tool, {key: "../../../etc/x"}, mode)


@pytest.mark.parametrize("path", ["/tmp/x.txt", "/private/tmp/x.txt", "/var/folders/ab/cd/T/x.txt", "/private/var/folders/ab/cd/T/x.txt", "/dev/null"])
def test_temp_locations_and_dev_null_are_allowed(path):
    assert reason("Write", {"file_path": path}) is None
    assert reason("Bash", {"command": f"echo hi > {path}"}) is None


def test_tmpdir_from_the_environment_is_allowed(monkeypatch, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    assert reason("Write", {"file_path": str(scratch / "x")}) is None
    assert reason("Bash", {"command": "echo hi > $TMPDIR/x"}) is None
    assert reason("Bash", {"command": "echo hi > ${TMPDIR}/x"}) is None


def test_multiedit_checks_every_edit():
    ok = {"file_path": f"{WT}/a.py", "edits": [{"file_path": f"{WT}/b.py"}]}
    bad = {"file_path": f"{WT}/a.py", "edits": [{"file_path": f"{OUTSIDE}/b.py"}]}
    assert reason("MultiEdit", ok) is None
    assert "outside the worktree" in reason("MultiEdit", bad)


def test_read_tools_can_read_anywhere():
    assert reason("Read", {"file_path": "/etc/hosts"}) is None
    assert reason("Glob", {"pattern": "/opt/**/*.py"}) is None
    assert reason("Grep", {"pattern": "x", "path": OUTSIDE}) is None
    assert reason("Bash", {"command": f"cat /etc/hosts | grep localhost && ls {OUTSIDE}"}) is None


def test_credential_reads_stay_denied():
    assert reason("Read", {"file_path": "/Users/tester/.ssh/id_rsa"})


def test_symlink_escape_is_resolved(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "escape").symlink_to("/opt/wf-outside")
    (worktree / "inner").mkdir()
    (worktree / "inner_link").symlink_to(worktree / "inner")
    wt = str(worktree)
    assert reason("Write", {"file_path": f"{wt}/escape/a.py"}, worktree=wt, cwd=wt)
    assert reason("Write", {"file_path": "escape/a.py"}, worktree=wt, cwd=wt)
    assert reason("Bash", {"command": "echo hi > escape/a.txt"}, worktree=wt, cwd=wt)
    assert reason("Write", {"file_path": f"{wt}/inner_link/a.py"}, worktree=wt, cwd=wt) is None
    assert reason("Write", {"file_path": f"{wt}/escape/../../etc/x"}, worktree=wt, cwd=wt)


def test_dotdot_after_a_symlink_is_resolved_physically(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "hop").symlink_to("/opt/wf-outside/deep")
    wt = str(worktree)
    assert reason("Write", {"file_path": f"{wt}/hop/../x"}, worktree=wt, cwd=wt)


def test_worktree_given_through_a_symlink_still_matches(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert reason("Write", {"file_path": f"{real}/a.py"}, worktree=str(link), cwd=str(link)) is None
    assert reason("Write", {"file_path": "/opt/x"}, worktree=str(link), cwd=str(link))


PATCH_INSIDE = "*** Begin Patch\n*** Update File: src/a.py\n@@\n-a\n+b\n*** Add File: new.py\n+x\n*** End Patch\n"
PATCH_OUTSIDE_UPDATE = "*** Begin Patch\n*** Update File: /opt/wf-outside/a.py\n@@\n-a\n+b\n*** End Patch\n"
PATCH_OUTSIDE_ADD = "*** Begin Patch\n*** Update File: ok.py\n@@\n-a\n+b\n*** Add File: ../escape.py\n+x\n*** End Patch\n"
PATCH_OUTSIDE_MOVE = "*** Begin Patch\n*** Update File: ok.py\n*** Move to: /opt/wf-outside/moved.py\n@@\n-a\n+b\n*** End Patch\n"
PATCH_OUTSIDE_DELETE = "*** Begin Patch\n*** Delete File: /etc/hosts\n*** End Patch\n"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("shape", ["command", "input", "argv"])
def test_apply_patch_targets_are_confined(shape, mode):
    def call(patch):
        if shape == "argv":
            return reason("shell", {"command": ["apply_patch", patch]}, mode)
        return reason("apply_patch", {shape: patch}, mode)

    assert call(PATCH_INSIDE) is None
    for patch in (PATCH_OUTSIDE_UPDATE, PATCH_OUTSIDE_ADD, PATCH_OUTSIDE_MOVE, PATCH_OUTSIDE_DELETE):
        assert call(patch), patch


def test_apply_patch_heredoc_in_bash_is_checked():
    inside = f"apply_patch <<'EOF'\n{PATCH_INSIDE}EOF"
    outside = f"apply_patch <<'EOF'\n{PATCH_OUTSIDE_UPDATE}EOF"
    assert reason("Bash", {"command": inside}) is None
    assert reason("Bash", {"command": outside})


ALLOWED_COMMANDS = [
    "echo hi > out.txt",
    "echo hi >> logs/out.txt",
    f"echo hi > {WT}/out.txt",
    "echo hi > /dev/null",
    "make test > /dev/null 2>&1",
    "pytest 2>/dev/null",
    "ls &> /dev/null",
    "cat a 2>&1 | tee build.log",
    "echo hi | tee -a out.txt",
    "tee out.txt < in.txt",
    "cp a.txt b.txt",
    "cp -r src dst",
    "cp /etc/hosts ./hosts.copy",
    "cp /etc/hosts /tmp/hosts.copy",
    "mv a.txt sub/b.txt",
    "mkdir -p build/out",
    "mkdir -p /tmp/x/y",
    "touch a b c",
    "touch -t 202001010000 a",
    "ln -s ../a b",
    "chmod +x run.sh",
    "chmod -R 755 scripts",
    "chmod -x run.sh",
    "rm a.txt",
    "rm -f build/x.o",
    "rm /tmp/scratch.txt",
    "sed -i 's/a/b/' f.txt",
    "sed -i.bak -e 's/a/b/' f.txt",
    "sed -n 1,5p /etc/hosts",
    "echo '>' is a symbol",
    "echo \"a > b\"",
    "grep -e '>' /etc/hosts",
    "awk '$1 > 5' /etc/hosts",
    "cd build && touch x",
    "cd /tmp && touch x",
    "bash -c 'echo hi > out.txt'",
    "cat /etc/hosts | head -1",
    "dd if=/dev/zero of=/tmp/blob bs=1 count=1",
    "cp -t sub a b",
    f"ln -sf {OUTSIDE}/target link",
    "git status",
    "git -C /opt/wf-outside status",
    "git -C /opt/wf-outside log --oneline",
    "git -C /opt/wf-outside branch --list",
    "git add -A",
    "echo hi > out.txt && cp out.txt sub/",
    "echo $(cat /etc/hosts | head -1) > out.txt",
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_allowed_shell_writes(command, mode):
    assert reason("Bash", {"command": command}, mode) is None, command


DENIED_COMMANDS = [
    f"echo hi > {OUTSIDE}/x",
    f"echo hi >> {OUTSIDE}/x",
    f"echo hi >| {OUTSIDE}/x",
    f"echo hi &> {OUTSIDE}/x",
    f"echo hi 2> {OUTSIDE}/x",
    f"echo hi 2>{OUTSIDE}/x",
    f"echo hi >{OUTSIDE}/x",
    f"echo hi >& {OUTSIDE}/x",
    "echo hi > /etc/hosts",
    "echo hi > ~/.zshrc",
    "echo hi > $HOME/.zshrc",
    "echo hi > $UNSET/x",
    "echo hi > ../x",
    "echo hi > ../../../../etc/x",
    f"echo hi > {OUTSIDE}/{{a,b}}",
    f"cat a | tee {OUTSIDE}/x",
    f"cat a | tee -a {OUTSIDE}/x",
    f"cp a.txt {OUTSIDE}/a.txt",
    f"cp -r src {OUTSIDE}",
    f"cp -t {OUTSIDE} a b",
    f"cp --target-directory={OUTSIDE} a b",
    f"mv a.txt {OUTSIDE}/a.txt",
    f"mv {OUTSIDE}/secret.txt .",
    "mv /etc/hosts .",
    f"mkdir {OUTSIDE}/d",
    f"mkdir -p -m 755 {OUTSIDE}/d",
    f"touch {OUTSIDE}/f",
    f"touch -t 202001010000 {OUTSIDE}/f",
    f"ln -s a {OUTSIDE}/link",
    f"ln -s a {OUTSIDE}",
    f"chmod +x {OUTSIDE}/run.sh",
    f"chmod -R 777 {OUTSIDE}",
    f"chown me {OUTSIDE}/f",
    f"rm {OUTSIDE}/f",
    "rm -f /etc/hosts",
    "rm ../x",
    f"rmdir {OUTSIDE}/d",
    f"truncate -s 0 {OUTSIDE}/f",
    f"dd if=a of={OUTSIDE}/blob",
    f"sed -i 's/a/b/' {OUTSIDE}/f.txt",
    f"sed -i -e 's/a/b/' {OUTSIDE}/f.txt",
    f"sed --in-place 's/a/b/' {OUTSIDE}/f.txt",
    f"bash -c 'echo hi > {OUTSIDE}/x'",
    f"sh -c \"cp a {OUTSIDE}/a\"",
    f"true && echo hi > {OUTSIDE}/x",
    f"echo $(echo hi > {OUTSIDE}/x)",
    f"cd {OUTSIDE} && touch x",
    "cd .. && touch x",
    "cd /etc && echo hi > hosts",
    f"cd {OUTSIDE}; echo hi > x",
    f"sudo tee {OUTSIDE}/x",
    f"env FOO=1 cp a {OUTSIDE}/a",
    f"cat a | xargs rm",
    f"ls | xargs touch",
    f"find . -name '*.o' | xargs -I{{}} cp {{}} {OUTSIDE}",
    f"git -C {OUTSIDE} add -A",
    f"git -C {OUTSIDE} checkout main",
    f"git -C {OUTSIDE} restore .",
    f"git -C {OUTSIDE} clean -fd",
    "cd /opt && git add -A",
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("command", DENIED_COMMANDS)
def test_denied_shell_writes(command, mode):
    assert reason("Bash", {"command": command}, mode), command


def test_denied_reason_names_the_target_and_the_rule():
    message = reason("Bash", {"command": f"echo hi > {OUTSIDE}/x"})
    assert OUTSIDE in message and "outside the worktree" in message
    message = reason("Write", {"file_path": f"{OUTSIDE}/x"})
    assert OUTSIDE in message and "outside the worktree" in message


def test_codex_shell_argv_command_is_checked():
    assert reason("shell", {"command": ["bash", "-lc", f"echo hi > {OUTSIDE}/x"]})
    assert reason("shell", {"command": ["bash", "-lc", "echo hi > out.txt"]}) is None
    assert reason("shell", {"command": ["cp", "a", OUTSIDE]})


def test_with_no_known_worktree_writes_fail_closed():
    assert reason("Write", {"file_path": "/anything/x"}, worktree=None, cwd=None)
    assert reason("Bash", {"command": "echo hi > /anything/x"}, worktree=None, cwd=None)
    assert reason("Write", {"file_path": "/tmp/x"}, worktree=None, cwd=None) is None


def test_main_denies_an_outside_write_and_logs_it(tmp_path):
    payload = {"tool_name": "Write", "tool_input": {"file_path": f"{OUTSIDE}/a.py"}, "cwd": WT, "session_id": "s1"}
    code, stderr = run_main("claude", payload, tmp_path, decider=FakeDecider(answer=False))
    assert code == 2 and "outside the worktree" in stderr
    logged = events_of(tmp_path)
    assert logged[-1]["allowed"] is False and logged[-1]["stage"] == "hook:denylist"


def test_main_allows_writes_inside_and_to_tmp(tmp_path):
    for target in (f"{WT}/a.py", "/tmp/a.py"):
        payload = {"tool_name": "Write", "tool_input": {"file_path": target}, "cwd": WT, "session_id": "s1"}
        assert run_main("claude", payload, tmp_path, decider=FakeDecider(answer=False))[0] == 0


def test_main_committer_mode_denies_outside_writes_too(tmp_path):
    env = {"WF_HOOK_MODE": "denylist"}
    denied = bash(f"echo hi > {OUTSIDE}/x")
    assert run_main("claude", denied, tmp_path, extra_env=env)[0] == 2
    commit = bash("git commit -m x")
    assert run_main("claude", commit, tmp_path, extra_env=env)[0] == 0
    assert run_main("claude", bash("git commit -m x"), tmp_path, decider=FakeDecider())[0] == 2


def test_main_uses_wf_worktree_from_the_environment(tmp_path):
    other = "/work/other-wt"
    payload = {"tool_name": "Write", "tool_input": {"file_path": f"{other}/a.py"}, "cwd": WT, "session_id": "s1"}
    assert run_main("claude", payload, tmp_path, decider=FakeDecider(answer=False))[0] == 2
    assert run_main("claude", payload, tmp_path, decider=FakeDecider(answer=False), extra_env={"WF_WORKTREE": other})[0] == 0


from workforce.paths import Paths


def managed(tmp_path, *segments):
    repo, home = tmp_path / "target", tmp_path / "home"
    repo.mkdir(parents=True, exist_ok=True)
    paths = Paths(repo, home=home)
    path = paths.worktrees_root.joinpath(*segments)
    path.mkdir(parents=True)
    return repo, home, paths, path


def test_managed_ids_single_segment_repo(tmp_path):
    _, _, paths, path = managed(tmp_path, "R1", "app", "T2")
    assert paths.worktree("R1", "T2", "app") == path
    assert hooks.managed_ids(str(path), paths) == {"run_id": "R1", "repo": "app", "task_id": "T2"}


def test_managed_ids_nested_repo(tmp_path):
    _, _, paths, path = managed(tmp_path, "R1", "tools", "gamma", "T4")
    assert paths.worktree("R1", "T4", "tools/gamma") == path
    assert hooks.managed_ids(str(path), paths) == {"run_id": "R1", "repo": "tools/gamma", "task_id": "T4"}


def test_managed_ids_integration_worktrees(tmp_path):
    _, _, paths, path = managed(tmp_path, "R1", "app", "_integration")
    assert paths.integration_worktree("R1", "app") == path
    expected = {"run_id": "R1", "repo": "app", "task_id": None, "integration": True}
    assert hooks.managed_ids(str(path), paths) == expected
    _, _, paths, nested = managed(tmp_path / "second", "R2", "tools", "gamma", "_integration")
    assert hooks.managed_ids(str(nested), paths) == {"run_id": "R2", "repo": "tools/gamma", "task_id": None, "integration": True}


def test_managed_ids_old_layout_has_no_repo(tmp_path):
    _, _, paths, path = managed(tmp_path, "R1", "T2")
    assert hooks.managed_ids(str(path), paths) == {"run_id": "R1", "task_id": "T2"}


def test_managed_ids_ignores_paths_that_are_not_worktrees(tmp_path):
    _, _, paths, path = managed(tmp_path, "R1", "app", "T2")
    assert hooks.managed_ids(str(paths.worktrees_root), paths) == {}
    assert hooks.managed_ids(str(paths.worktrees_root / "R1"), paths) == {}
    assert hooks.managed_ids(str(tmp_path), paths) == {}
    assert hooks.managed_ids(None, paths) == {}


@pytest.mark.parametrize(
    "segments,expected",
    [
        (("R1", "app", "T2"), {"run_id": "R1", "repo": "app", "task_id": "T2"}),
        (("R1", "tools", "gamma", "T4"), {"run_id": "R1", "repo": "tools/gamma", "task_id": "T4"}),
        (("R1", "app", "_integration"), {"run_id": "R1", "repo": "app", "task_id": None, "integration": True}),
        (("R1", "T2"), {"run_id": "R1", "task_id": "T2"}),
    ],
)
def test_events_carry_the_worktree_tags(tmp_path, segments, expected):
    repo, home, _, path = managed(tmp_path, *segments)
    payload = bash("ls", str(path))
    code, _ = run_main("claude", payload, repo, decider=FakeDecider(answer=False), extra_env={"WF_WORKTREE": str(path)}, home=home)
    assert code == 0
    event = events_of(repo)[0]
    assert {key: event[key] for key in expected} == expected
    for key in ("repo", "integration"):
        assert (key in event) == (key in expected)


def test_denied_call_in_a_nested_repo_worktree_is_tagged(tmp_path):
    repo, home, _, path = managed(tmp_path, "R1", "tools", "gamma", "T4")
    payload = {"tool_name": "Write", "tool_input": {"file_path": f"{OUTSIDE}/a.py"}, "cwd": str(path), "session_id": "s1"}
    code, _ = run_main("claude", payload, repo, decider=FakeDecider(answer=False), extra_env={"WF_WORKTREE": str(path)}, home=home)
    assert code == 2
    event = events_of(repo)[0]
    assert (event["run_id"], event["repo"], event["task_id"], event["allowed"]) == ("R1", "tools/gamma", "T4", False)
