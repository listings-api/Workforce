import subprocess

from workforce.paths import Paths


def test_ensure_excludes_config_file_so_repo_stays_clean(git_repo, tmp_path):
    paths = Paths(git_repo, home=tmp_path / "home")
    paths.ensure()
    (git_repo / "workforce.toml").write_text("[bin]\n")
    out = subprocess.run(["git", "-C", str(git_repo), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert out.strip() == ""


def test_ensure_is_idempotent(git_repo, tmp_path):
    paths = Paths(git_repo, home=tmp_path / "home")
    paths.ensure()
    paths.ensure()
    exclude = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "--git-path", "info/exclude"], capture_output=True, text=True
    ).stdout.strip()
    text = (git_repo / exclude).read_text() if not exclude.startswith("/") else open(exclude).read()
    assert text.count("/workforce.toml") == 1
    assert text.count(".workforce/") == 1
