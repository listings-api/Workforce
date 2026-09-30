import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from workforce.team import demo


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.chdir(tmp_path)
    return path


def run(home, *args, launcher=None):
    out, err = io.StringIO(), io.StringIO()
    code = demo.main(list(args), home=home, launcher=launcher, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def git(target: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True, text=True).stdout.strip()


def unittest_run(target: Path):
    return subprocess.run(
        [sys.executable, "-m", "unittest"],
        cwd=target,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_no_launch_creates_the_default_folder_under_home(home):
    code, out, err = run(home, "--no-launch")
    target = home / "workforce-demo"
    assert code == 0 and err == ""
    assert f"Demo project: {target}" in out
    assert sorted(p.name for p in target.iterdir() if p.name != ".git") == sorted(
        [".gitignore", ".workforce-demo", "README.md", "shopping.py", "test_shopping.py"]
    )
    assert "python3 -m unittest" in (target / "README.md").read_text()


def test_the_demo_test_fails_before_the_fix_and_passes_after(tmp_path, home):
    target = tmp_path / "demo"
    assert run(home, "--dir", str(target), "--no-launch")[0] == 0
    before = unittest_run(target)
    assert before.returncode != 0
    assert "test_discount_is_taken_off" in before.stderr and "66" in before.stderr
    module = target / "shopping.py"
    module.write_text(module.read_text().replace("subtotal + subtotal", "subtotal - subtotal"))
    after = unittest_run(target)
    assert after.returncode == 0, after.stderr


def test_only_the_discount_test_fails(tmp_path, home):
    target = tmp_path / "demo"
    run(home, "--dir", str(target), "--no-launch")
    stderr = unittest_run(target).stderr
    assert "FAIL: test_discount_is_taken_off" in stderr
    assert "FAIL: test_no_discount" not in stderr and "FAIL: test_empty_basket" not in stderr


def test_the_demo_is_a_clean_git_repo_with_one_unsigned_commit(tmp_path, home):
    target = tmp_path / "demo"
    run(home, "--dir", str(target), "--no-launch")
    assert git(target, "rev-list", "--count", "HEAD") == "1"
    assert git(target, "status", "--porcelain") == ""
    assert git(target, "config", "--local", "user.name") == "WorkForce demo"
    assert "@" in git(target, "config", "--local", "user.email")
    assert git(target, "log", "-1", "--format=%an") == "WorkForce demo"
    assert git(target, "cat-file", "-p", "HEAD").count("gpgsig") == 0
    assert git(target, "branch", "--show-current") == "main"


def test_refuses_a_non_empty_folder(tmp_path, home):
    target = tmp_path / "mine"
    target.mkdir()
    (target / "notes.txt").write_text("keep me")
    code, out, err = run(home, "--dir", str(target), "--no-launch")
    assert code == 2 and out == ""
    assert "is not empty" in err and "--force" in err
    assert [p.name for p in target.iterdir()] == ["notes.txt"]
    assert (target / "notes.txt").read_text() == "keep me"


def test_force_adds_the_demo_without_deleting_anything(tmp_path, home):
    target = tmp_path / "mine"
    target.mkdir()
    (target / "notes.txt").write_text("keep me")
    code, _, err = run(home, "--dir", str(target), "--force", "--no-launch")
    assert code == 0, err
    assert (target / "notes.txt").read_text() == "keep me"
    assert (target / "shopping.py").is_file()


def test_force_refuses_an_existing_git_repository(tmp_path, home):
    target = tmp_path / "mine"
    target.mkdir()
    git(target, "init", "-b", "main")
    code, _, err = run(home, "--dir", str(target), "--force", "--no-launch")
    assert code == 2 and "already a git repository" in err
    assert not (target / "shopping.py").exists()


def test_a_previous_demo_is_recreated_even_after_edits(tmp_path, home):
    target = tmp_path / "demo"
    run(home, "--dir", str(target), "--no-launch")
    (target / "shopping.py").write_text("changed = True\n")
    (target / "extra.txt").write_text("x")
    code, _, err = run(home, "--dir", str(target), "--no-launch")
    assert code == 0, err
    assert not (target / "extra.txt").exists()
    assert "total_price" in (target / "shopping.py").read_text()
    assert git(target, "rev-list", "--count", "HEAD") == "1"
    assert unittest_run(target).returncode != 0


def test_an_empty_existing_folder_is_fine(tmp_path, home):
    target = tmp_path / "empty"
    target.mkdir()
    assert run(home, "--dir", str(target), "--no-launch")[0] == 0


def test_a_file_in_the_way_is_refused(tmp_path, home):
    target = tmp_path / "file"
    target.write_text("x")
    code, _, err = run(home, "--dir", str(target), "--no-launch")
    assert code == 2 and "not a folder" in err


def test_launch_is_called_with_the_prompt_from_inside_the_demo(tmp_path, home):
    target = tmp_path / "demo"
    seen = []

    def fake_launcher(argv):
        seen.append((list(argv), Path.cwd()))
        return 7

    code, out, _ = run(home, "--dir", str(target), launcher=fake_launcher)
    assert code == 7
    assert seen == [([demo.PROMPT], target.resolve())]
    assert demo.PROMPT == (
        "This is a WorkForce demo. Fix the failing test: ask Codex for a plan, make the fix, run the test, "
        "get both reviews, then commit."
    )
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[0] == f"Demo project: {target}"
    assert "quota" in lines[2] and "Claude" in lines[2] and "ChatGPT" in lines[2]


def test_no_launch_does_not_call_the_launcher(tmp_path, home):
    def fake_launcher(argv):
        raise AssertionError("must not launch")

    assert run(home, "--dir", str(tmp_path / "demo"), "--no-launch", launcher=fake_launcher)[0] == 0


def test_a_refused_folder_does_not_launch(tmp_path, home):
    target = tmp_path / "mine"
    target.mkdir()
    (target / "x").write_text("x")

    def fake_launcher(argv):
        raise AssertionError("must not launch")

    assert run(home, "--dir", str(target), launcher=fake_launcher)[0] == 2


def test_default_launcher_calls_wf_main(tmp_path, home, monkeypatch):
    from workforce.team import launch

    calls = []
    monkeypatch.setattr(launch, "main", lambda argv, **kw: calls.append((list(argv), kw)) or 0)
    assert demo.main(["--dir", str(tmp_path / "demo")], home=home, environ={"X": "1"}, stdout=io.StringIO()) == 0
    assert calls == [([demo.PROMPT], {"home": home, "environ": {"X": "1"}})]


def test_bad_option_returns_two(home):
    assert run(home, "--nope")[0] == 2
