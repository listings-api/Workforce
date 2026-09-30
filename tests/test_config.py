import re
import tomllib
from pathlib import Path

import pytest

from workforce import config
from workforce.errors import ConfigError


def _mutated(mutate):
    data = tomllib.loads(config.default_toml())
    mutate(data)
    return _dump(data)


def _dump(data: dict) -> str:
    lines: list[str] = []

    def emit(prefix: str, table: dict) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        subtables = {k: v for k, v in table.items() if isinstance(v, dict)}
        if prefix and (scalars or not subtables):
            lines.append(f"[{prefix}]")
        for key, value in scalars.items():
            lines.append(f"{key} = {_value(value)}")
        for key, sub in subtables.items():
            emit(f"{prefix}.{key}" if prefix else key, sub)

    emit("", data)
    return "\n".join(lines) + "\n"


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(value)


def _error(text: str) -> str:
    with pytest.raises(ConfigError) as info:
        config.parse(text)
    return str(info.value)


def test_default_loads_and_matches_spec(tmp_path: Path):
    config.write_default(tmp_path)
    cfg = config.load(tmp_path)
    assert cfg.bin.claude == str(Path("~/.local/bin/claude").expanduser())
    assert cfg.bin.codex == "/opt/homebrew/bin/codex"
    assert cfg.role("planner") == config.Role("codex", "gpt-6-sol", "high")
    assert cfg.role("plan_reviewer") == config.Role("claude", "claude-opus-5-5", "high")
    assert cfg.role("coder") == config.Role("claude", "claude-sonnet-5-5", "high")
    assert cfg.role("reviewer_claude") == config.Role("claude", "claude-opus-5-5", "high")
    assert cfg.role("reviewer_codex") == config.Role("codex", "gpt-6-sol", "high")
    assert cfg.role("committer") == config.Role("claude", "claude-sonnet-5-5", "high")
    assert cfg.role("usage_summary") == config.Role("claude", "claude-haiku-4-5-20251001", "low")
    assert (cfg.limits.debate_rounds, cfg.limits.review_rounds) == (3, 3)
    assert (cfg.limits.alert_percent, cfg.limits.pause_percent) == (50, 60)
    assert (cfg.limits.poll_minutes, cfg.limits.parallel) == (10, 2)
    assert cfg.checks == config.Checks("", "", "")
    assert cfg.browser == config.Browser(True, True, False)
    assert cfg.decider == config.DeciderConfig(
        "laya",
        "http://127.0.0.1:11435",
        0.85,
        thresholds={"task_size": 0.75, "agent_progress": 0.45, "debate_converged": 0.63},
    )


def test_write_default_refuses_overwrite(tmp_path: Path):
    config.write_default(tmp_path)
    with pytest.raises(ConfigError, match="already exists"):
        config.write_default(tmp_path)


def test_load_missing_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        config.load(tmp_path)


def test_invalid_toml():
    assert "not valid TOML" in _error("[bin\nclaude=")


def test_unknown_role_lookup():
    cfg = config.parse(config.default_toml())
    with pytest.raises(ConfigError, match="roles.nope"):
        cfg.role("nope")


@pytest.mark.parametrize(
    "section,key",
    [
        ("bin", "claude"),
        ("bin", "codex"),
        ("limits", "debate_rounds"),
        ("limits", "review_rounds"),
        ("limits", "alert_percent"),
        ("limits", "pause_percent"),
        ("limits", "poll_minutes"),
        ("limits", "parallel"),
        ("checks", "test"),
        ("checks", "lint"),
        ("checks", "build"),
        ("browser", "claude"),
        ("browser", "codex"),
        ("browser", "computer_use"),
        ("decider", "backend"),
        ("decider", "url"),
        ("decider", "confidence"),
    ],
)
def test_missing_key_named(section, key):
    text = _mutated(lambda d: d[section].pop(key))
    assert _error(text) == f"{section}.{key} is missing"


@pytest.mark.parametrize("role", config.ROLE_NAMES)
@pytest.mark.parametrize("key", ["agent", "model", "effort"])
def test_missing_role_key_named(role, key):
    text = _mutated(lambda d: d["roles"][role].pop(key))
    assert _error(text) == f"roles.{role}.{key} is missing"


def test_missing_section_reports_first_key():
    text = _mutated(lambda d: d.pop("limits"))
    assert _error(text) == "limits.debate_rounds is missing"


def test_missing_role_reports_agent():
    text = _mutated(lambda d: d["roles"].pop("coder"))
    assert _error(text) == "roles.coder.agent is missing"


def test_section_not_a_table():
    text = _mutated(lambda d: d.__setitem__("limits", 5))
    assert "limits must be a table" in _error(text)


def test_role_not_a_table():
    text = _mutated(lambda d: d["roles"].__setitem__("coder", "x"))
    assert "roles.coder must be a table" in _error(text)


def test_bad_agent():
    text = _mutated(lambda d: d["roles"]["coder"].__setitem__("agent", "gemini"))
    assert "roles.coder.agent 'gemini' is invalid" in _error(text)


@pytest.mark.parametrize(
    "agent,effort,ok",
    [
        ("claude", "ultra", False),
        ("claude", "max", True),
        ("claude", "turbo", False),
        ("codex", "ultra", True),
        ("codex", "max", True),
        ("codex", "turbo", False),
    ],
)
def test_effort_valid_per_agent(agent, effort, ok):
    def mutate(d):
        d["roles"]["coder"]["agent"] = agent
        d["roles"]["coder"]["effort"] = effort

    text = _mutated(mutate)
    if ok:
        assert config.parse(text).role("coder").effort == effort
    else:
        assert f"roles.coder.effort '{effort}' is not valid for {agent}" in _error(text)


def test_empty_model_rejected():
    text = _mutated(lambda d: d["roles"]["coder"].__setitem__("model", " "))
    assert _error(text) == "roles.coder.model must not be empty"


def test_wrong_types():
    assert "bin.claude must be a string" in _error(_mutated(lambda d: d["bin"].__setitem__("claude", 3)))
    assert "browser.claude must be true or false" in _error(
        _mutated(lambda d: d["browser"].__setitem__("claude", "yes"))
    )
    assert "limits.parallel must be an integer" in _error(
        _mutated(lambda d: d["limits"].__setitem__("parallel", "2"))
    )
    assert "limits.parallel must be an integer" in _error(
        _mutated(lambda d: d["limits"].__setitem__("parallel", True))
    )
    assert "decider.confidence must be a number" in _error(
        _mutated(lambda d: d["decider"].__setitem__("confidence", "high"))
    )


@pytest.mark.parametrize("key,value", [("alert_percent", 0), ("pause_percent", 101), ("alert_percent", -5)])
def test_percent_range(key, value):
    text = _mutated(lambda d: d["limits"].__setitem__(key, value))
    assert f"limits.{key} ({value}) must be between 1 and 100" in _error(text)


@pytest.mark.parametrize("alert,pause", [(60, 60), (70, 60)])
def test_alert_must_be_below_pause(alert, pause):
    def mutate(d):
        d["limits"]["alert_percent"] = alert
        d["limits"]["pause_percent"] = pause

    assert "alert_percent" in _error(_mutated(mutate)) and "lower than" in _error(_mutated(mutate))


@pytest.mark.parametrize("key", ["debate_rounds", "review_rounds", "poll_minutes", "parallel"])
def test_counts_must_be_positive(key):
    text = _mutated(lambda d: d["limits"].__setitem__(key, 0))
    assert f"limits.{key} (0) must be at least 1" in _error(text)


def test_bad_decider_backend_and_confidence():
    assert "decider.backend 'gpt' is invalid" in _error(
        _mutated(lambda d: d["decider"].__setitem__("backend", "gpt"))
    )
    assert "decider.confidence (1.5)" in _error(_mutated(lambda d: d["decider"].__setitem__("confidence", 1.5)))
    assert "decider.confidence (0)" in _error(_mutated(lambda d: d["decider"].__setitem__("confidence", 0)))


def test_decider_off_accepted():
    cfg = config.parse(_mutated(lambda d: d["decider"].__setitem__("backend", "off")))
    assert cfg.decider.backend == "off"


def test_tilde_expanded_in_bin():
    text = _mutated(lambda d: d["bin"].__setitem__("codex", "~/bin/codex"))
    assert config.parse(text).bin.codex == str(Path("~/bin/codex").expanduser())


def test_save_role_updates_only_that_role(tmp_path: Path):
    config.write_default(tmp_path)
    path = tmp_path / "workforce.toml"
    before = path.read_text()

    cfg = config.save_role(tmp_path, "coder", "claude-opus-5-5", "max")

    after = path.read_text()
    assert cfg.role("coder") == config.Role("claude", "claude-opus-5-5", "max")
    assert config.load(tmp_path).role("coder") == cfg.role("coder")

    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    assert len(before_lines) == len(after_lines)
    changed = [i for i, (a, b) in enumerate(zip(before_lines, after_lines)) if a != b]
    header = next(i for i, line in enumerate(before_lines) if line.startswith("[roles.coder]"))
    assert changed == [header + 2, header + 3]
    assert after_lines[header + 2] == 'model = "claude-opus-5-5"\n'
    assert after_lines[header + 3] == 'effort = "max"\n'
    assert after_lines[header] == before_lines[header]


def test_save_role_is_byte_identical_outside_role(tmp_path: Path):
    text = (
        "# my notes\r\n"
        + config.default_toml().replace("effort = \"high\"\n", "effort = \"high\"   # keep me\n", 1)
        + "\n# trailing comment without newline"
    )
    path = tmp_path / "workforce.toml"
    path.write_bytes(text.encode())

    config.save_role(tmp_path, "reviewer_codex", "gpt-6-sol", "ultra")

    updated = path.read_bytes().decode()
    expected = text.replace(
        '[roles.reviewer_codex]\nagent = "codex"\nmodel = "gpt-6-sol"\neffort = "high"\n',
        '[roles.reviewer_codex]\nagent = "codex"\nmodel = "gpt-6-sol"\neffort = "ultra"\n',
    )
    assert updated == expected
    assert "# keep me" in updated
    assert config.load(tmp_path).role("reviewer_codex").model == "gpt-6-sol"


def test_save_role_preserves_trailing_comments_on_value_lines(tmp_path: Path):
    text = config.default_toml().replace(
        'model = "gpt-6-sol"\neffort = "high"\n\n[roles.plan_reviewer]',
        'model = "gpt-6-sol"   # pinned\neffort = "high"   # deep\n\n[roles.plan_reviewer]',
        1,
    )
    (tmp_path / "workforce.toml").write_text(text)
    config.save_role(tmp_path, "planner", "gpt-6-sol", "medium")
    updated = (tmp_path / "workforce.toml").read_text()
    assert 'model = "gpt-6-sol"   # pinned\n' in updated
    assert 'effort = "medium"   # deep\n' in updated


def test_save_role_last_role_in_file(tmp_path: Path):
    config.write_default(tmp_path)
    config.save_role(tmp_path, "usage_summary", "claude-sonnet-5-5", "medium")
    assert config.load(tmp_path).role("usage_summary") == config.Role("claude", "claude-sonnet-5-5", "medium")


def test_save_role_validates_and_leaves_file_untouched(tmp_path: Path):
    config.write_default(tmp_path)
    path = tmp_path / "workforce.toml"
    before = path.read_bytes()

    with pytest.raises(ConfigError, match="not valid for claude"):
        config.save_role(tmp_path, "coder", "claude-opus-5-5", "ultra")
    with pytest.raises(ConfigError, match="not a valid effort level"):
        config.save_role(tmp_path, "coder", "claude-opus-5-5", "turbo")
    with pytest.raises(ConfigError, match="not a valid model name"):
        config.save_role(tmp_path, "coder", 'x"\nagent = "codex', "high")
    with pytest.raises(ConfigError, match="not a valid model name"):
        config.save_role(tmp_path, "coder", "", "high")
    with pytest.raises(ConfigError, match="not a known role"):
        config.save_role(tmp_path, "ghost", "m", "high")

    assert path.read_bytes() == before


def test_save_role_missing_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        config.save_role(tmp_path, "coder", "m", "high")


def test_save_role_missing_table_or_key(tmp_path: Path):
    path = tmp_path / "workforce.toml"
    path.write_text(re.sub(r"\[roles\.usage_summary\].*?\n\n", "", config.default_toml(), flags=re.S))
    with pytest.raises(ConfigError, match="roles.usage_summary is missing"):
        config.save_role(tmp_path, "usage_summary", "m", "low")

    path.write_text(config.default_toml().replace('model = "gpt-6-sol"\neffort = "high"\n\n[roles.plan', 'effort = "high"\n\n[roles.plan', 1))
    with pytest.raises(ConfigError, match="roles.planner.model is missing"):
        config.save_role(tmp_path, "planner", "gpt-6-sol", "high")


def test_decider_thresholds_and_models_parse(tmp_path):
    from workforce import config as cfg
    text = cfg.default_toml().replace(
        "[decider.thresholds]\n", "[decider.models]\ntask_size = \"laya:en\"\n\n[decider.thresholds]\n"
    )
    (tmp_path / "workforce.toml").write_text(text)
    c = cfg.load(tmp_path)
    assert c.decider.thresholds == {"task_size": 0.75, "agent_progress": 0.45, "debate_converged": 0.63}
    assert c.decider.models == {"task_size": "laya:en"}


def test_decider_threshold_rejects_unknown_key(tmp_path):
    import pytest
    from workforce import config as cfg
    from workforce.errors import ConfigError
    text = cfg.default_toml().replace("task_size = 0.75", "bogus = 0.75")
    (tmp_path / "workforce.toml").write_text(text)
    with pytest.raises(ConfigError, match="decider.thresholds.bogus"):
        cfg.load(tmp_path)


def test_decider_tables_are_optional(tmp_path):
    from workforce import config as cfg
    text = cfg.default_toml().split("[decider.thresholds]")[0]
    (tmp_path / "workforce.toml").write_text(text)
    c = cfg.load(tmp_path)
    assert c.decider.thresholds is None and c.decider.models is None
