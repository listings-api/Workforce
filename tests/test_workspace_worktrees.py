import subprocess

import pytest

from workforce import config as config_mod
from workforce import workspace
from workforce.errors import ConfigError


def _git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


@pytest.fixture
def ws_with_worktrees(git_workspace):
    _git("-C", str(git_workspace / "alpha"), "worktree", "add", "-q", "-b", "side", str(git_workspace / "alpha-side"))
    (git_workspace / "later").mkdir()
    _git("-C", str(git_workspace / "beta"), "worktree", "add", "-q", "-b", "other", str(git_workspace / "later" / "beta-wt"))
    return git_workspace


def test_linked_worktrees_fold_into_their_main_checkout(ws_with_worktrees):
    repos, skipped = workspace.discover_with_skipped(ws_with_worktrees)
    names = [ref.name for ref in repos]
    assert "alpha" in names and "beta" in names
    assert "alpha-side" not in names and "later/beta-wt" not in names
    assert {(s.name, s.of) for s in skipped} == {("alpha-side", "alpha"), ("later/beta-wt", "beta")}


def test_config_listing_a_repo_and_its_worktree_is_rejected(ws_with_worktrees):
    config_mod.write_workspace_default(ws_with_worktrees, ["alpha", "alpha-side"])
    config = config_mod.load(ws_with_worktrees)
    with pytest.raises(ConfigError, match="same git repository"):
        workspace.load_workspace(ws_with_worktrees, config)


def test_a_worktree_alone_is_kept_when_its_main_checkout_is_outside(tmp_path, git_repo):
    ws = tmp_path / "only-wt"
    ws.mkdir()
    _git("-C", str(git_repo), "worktree", "add", "-q", "-b", "wt", str(ws / "feature"))
    repos, skipped = workspace.discover_with_skipped(ws)
    assert [ref.name for ref in repos] == ["feature"] and skipped == []


def test_init_force_backs_up_a_bad_config_and_regenerates_it(ws_with_worktrees, monkeypatch, tmp_path):
    from workforce import cli
    from workforce.agents import guard

    monkeypatch.setattr(guard, "check_claude", lambda binary: [])
    monkeypatch.setattr(guard, "check_codex", lambda binary: [])
    monkeypatch.setattr(guard, "check_env", lambda environ: [])
    monkeypatch.setattr(cli, "_version", lambda binary: "x", raising=False)
    config_mod.write_workspace_default(ws_with_worktrees, ["alpha", "alpha-side"])
    monkeypatch.chdir(ws_with_worktrees)
    assert cli.main(["init"], home=tmp_path / "home") == cli.EXIT_PREFLIGHT
    assert cli.main(["init", "--force"], home=tmp_path / "home") == 0
    assert (ws_with_worktrees / "workforce.toml.bak").is_file()
    repos = config_mod.load(ws_with_worktrees).workspace.repos
    assert "alpha" in repos and "alpha-side" not in repos
