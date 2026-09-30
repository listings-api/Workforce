import os
import stat
from pathlib import Path

import pytest

from workforce.team import config, detect


def make_exe(path: Path, body: str = "echo ok") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setattr(detect, "WELL_KNOWN_DIRS", ("~/.local/bin", "~/.claude/local", "~/.bun/bin"))
    return path


@pytest.fixture
def empty_path(tmp_path):
    path = tmp_path / "emptybin"
    path.mkdir()
    return {"PATH": str(path)}


def test_find_cli_prefers_path(home, tmp_path):
    on_path = make_exe(tmp_path / "bin" / "claude")
    make_exe(home / ".local" / "bin" / "claude")
    assert detect.find_cli("claude", {"PATH": str(on_path.parent)}, home) == on_path


@pytest.mark.parametrize("folder", [".local/bin", ".claude/local", ".bun/bin"])
def test_find_cli_well_known_folders_under_home(home, empty_path, folder):
    exe = make_exe(home / folder / "codex")
    assert detect.find_cli("codex", empty_path, home) == exe


def test_find_cli_well_known_order(home, empty_path):
    first = make_exe(home / ".local" / "bin" / "claude")
    make_exe(home / ".bun" / "bin" / "claude")
    assert detect.find_cli("claude", empty_path, home) == first


def test_find_cli_skips_non_executable_and_returns_none(home, empty_path):
    plain = home / ".local" / "bin" / "claude"
    plain.parent.mkdir(parents=True)
    plain.write_text("not executable")
    plain.chmod(0o644)
    assert detect.find_cli("claude", empty_path, home) is None


def test_find_cli_uses_npm_global_bin_when_npm_is_on_path(home, tmp_path):
    prefix = tmp_path / "npmprefix"
    exe = make_exe(prefix / "bin" / "claude")
    npm = make_exe(tmp_path / "npmdir" / "npm", f'echo "{prefix}"')
    assert detect.find_cli("claude", {"PATH": str(npm.parent)}, home) == exe


def test_find_cli_ignores_npm_when_it_fails(home, tmp_path):
    npm = make_exe(tmp_path / "npmdir" / "npm", "exit 1")
    assert detect.find_cli("claude", {"PATH": str(npm.parent)}, home) is None


def test_find_cli_skips_npm_lookup_without_npm(home, empty_path):
    assert detect.find_cli("codex", empty_path, home) is None


def test_version_reads_first_line(tmp_path):
    exe = make_exe(tmp_path / "claude", 'echo "2.1.300 (Claude Code)"; echo more')
    assert detect.version(exe) == "2.1.300 (Claude Code)"


def test_version_none_when_broken(tmp_path):
    assert detect.version(make_exe(tmp_path / "bad", "exit 3")) is None
    assert detect.version(tmp_path / "missing") is None


def test_version_none_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(detect, "VERSION_TIMEOUT_S", 0.2)
    assert detect.version(make_exe(tmp_path / "slow", "sleep 5")) is None


def test_first_run_writes_detected_paths(home, tmp_path):
    claude = make_exe(tmp_path / "bin" / "claude")
    codex = make_exe(tmp_path / "bin" / "codex")
    cfg = config.load(home, {"PATH": str(claude.parent)})
    assert (cfg.claude, cfg.codex) == (str(claude), str(codex))
    text = config.config_path(home).read_text()
    assert f'claude = "{claude}"' in text and f'codex = "{codex}"' in text
    assert config.load(home) == cfg


def test_first_run_keeps_defaults_for_what_is_not_found(home, tmp_path, empty_path):
    claude = make_exe(tmp_path / "bin" / "claude")
    cfg = config.load(home, {"PATH": f"{claude.parent}{os.pathsep}{empty_path['PATH']}"})
    assert cfg.claude == str(claude)
    assert cfg.codex == config.DEFAULTS["codex"]
    nothing = config.load(home / "other", empty_path)
    assert (nothing.claude, nothing.codex) == (config.DEFAULTS["claude"], config.DEFAULTS["codex"])


def test_explicit_home_without_environ_does_not_detect(home, tmp_path):
    make_exe(home / ".local" / "bin" / "claude")
    cfg = config.load(home)
    assert cfg.claude == config.DEFAULTS["claude"]


def test_existing_file_is_never_rewritten_by_load(home, tmp_path):
    path = config.config_path(home)
    path.parent.mkdir()
    path.write_text('claude = "/nowhere/claude"\n')
    claude = make_exe(tmp_path / "bin" / "claude")
    assert config.load(home, {"PATH": str(claude.parent)}).claude == "/nowhere/claude"
    assert path.read_text() == 'claude = "/nowhere/claude"\n'


def write_toml(home: Path, text: str) -> Path:
    path = config.config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_repair_rewrites_a_missing_binary(home, tmp_path):
    found = make_exe(tmp_path / "bin" / "claude")
    path = write_toml(home, 'claude = "/nowhere/claude"\ncodex_model = "gpt-6-sol"\n')
    notes = config.repair_binaries(home, {"PATH": str(found.parent)})
    assert notes == [f"claude not found at /nowhere/claude; using {found} (saved in ~/.workforce/team.toml)"]
    assert f'claude = "{found}"' in path.read_text()
    assert 'codex_model = "gpt-6-sol"' in path.read_text()
    assert config.load(home).claude == str(found)


def test_repair_never_overwrites_a_working_path(home, tmp_path):
    working = make_exe(tmp_path / "mine" / "claude")
    other = make_exe(tmp_path / "bin" / "claude")
    path = write_toml(home, f'claude = "{working}"\n')
    before = path.read_text()
    assert config.repair_binaries(home, {"PATH": str(other.parent)}) == []
    assert path.read_text() == before


def test_repair_repairs_a_non_executable_path(home, tmp_path):
    broken = tmp_path / "broken" / "codex"
    broken.parent.mkdir()
    broken.write_text("x")
    found = make_exe(tmp_path / "bin" / "codex")
    path = write_toml(home, f'codex = "{broken}"\nclaude = "{make_exe(tmp_path / "ok" / "claude")}"\n')
    notes = config.repair_binaries(home, {"PATH": str(found.parent)})
    assert len(notes) == 1 and notes[0].startswith(f"codex not found at {broken}; using {found}")
    assert f'codex = "{found}"' in path.read_text()


def test_repair_leaves_the_file_alone_when_nothing_is_found(home, empty_path):
    path = write_toml(home, 'claude = "/nowhere/claude"\n')
    assert config.repair_binaries(home, empty_path) == []
    assert path.read_text() == 'claude = "/nowhere/claude"\n'


def test_repair_handles_the_default_when_the_key_is_absent(home, tmp_path):
    found = make_exe(tmp_path / "bin" / "claude")
    path = write_toml(home, 'codex_model = "gpt-6-sol"\n')
    notes = config.repair_binaries(home, {"PATH": str(found.parent)})
    assert notes and notes[0].startswith(f"claude not found at {config.DEFAULTS['claude']}; using {found}")
    assert f'claude = "{found}"' in path.read_text()


def test_repair_without_a_file_or_with_a_bad_file_does_nothing(home, tmp_path):
    found = make_exe(tmp_path / "bin" / "claude")
    environ = {"PATH": str(found.parent)}
    assert config.repair_binaries(home, environ) == []
    assert not config.config_path(home).exists()
    path = write_toml(home, "not toml [\n")
    assert config.repair_binaries(home, environ) == []
    assert path.read_text() == "not toml [\n"
