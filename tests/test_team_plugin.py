import json
import re
import shlex
import sys
import tomllib
from pathlib import Path

import pytest

from workforce.team import config, mcp_server, plugin_runtime

SOURCE = plugin_runtime.source_dir()
ROOT = Path(__file__).resolve().parent.parent
TOOL_PATTERN = re.compile(r"mcp__plugin_workforce_codex__(\w+)")
TOOL_NAMES = {tool["name"] for tool in mcp_server.TOOLS}


@pytest.fixture(scope="module")
def PLUGIN(tmp_path_factory):
    """The runnable plugin, prepared into a throwaway home from the packaged files."""
    home = tmp_path_factory.mktemp("home")
    return plugin_runtime.prepare(home, config.TeamConfig("c", "x", "m", "high", 50, 60))


def frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    assert match, f"{path.name} has no frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, match.group(2)


def test_manifest_is_valid_json_named_workforce():
    manifest = json.loads((SOURCE / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "workforce"


def test_packaged_mcp_json_and_hooks_carry_python_placeholders_not_a_venv_path():
    mcp = (SOURCE / ".mcp.json").read_text()
    hooks = (SOURCE / "hooks" / "hooks.json").read_text()
    assert "{{python}}" in mcp and "{{python_quoted}}" in hooks
    assert ".venv" not in mcp + hooks and "${CLAUDE_PLUGIN_ROOT}/.." not in mcp + hooks


def test_mcp_json_runs_this_python_module(PLUGIN):
    config = json.loads((PLUGIN / ".mcp.json").read_text())
    server = config["mcpServers"]["codex"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "workforce.team.mcp_server"]


def test_hooks_json_registers_both_events_with_this_python(PLUGIN):
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    pre = hooks["PreToolUse"][0]
    assert pre["matcher"] == "*"
    assert pre["hooks"][0]["command"].endswith("-m workforce.team.hooks pretooluse")
    prompt = hooks["UserPromptSubmit"][0]["hooks"][0]
    assert prompt["command"].endswith("-m workforce.team.hooks userpromptsubmit")
    for command in (pre["hooks"][0]["command"], prompt["command"]):
        assert shlex.split(command)[0] == sys.executable
        assert "{{" not in command


@pytest.mark.parametrize(
    "name, model",
    [("fast-coder", "claude-sonnet-5-5")],
)
def test_agents_have_frontmatter_and_models(PLUGIN, name, model):
    fields, body = frontmatter(PLUGIN / "agents" / f"{name}.md")
    assert fields["name"] == name
    assert fields["model"] == model
    assert fields["description"]
    assert body.strip()


def test_no_agent_can_record_a_verdict(PLUGIN):
    """Reviews are run by the MCP server: there is no reviewer agent file and no tool that records a verdict."""
    assert not (PLUGIN / "agents" / "reviewer.md").exists()
    assert not (SOURCE / "agent-templates" / "reviewer.md").exists()
    assert "record_claude_review" not in TOOL_NAMES and "claude_review" in TOOL_NAMES
    for path in [*PLUGIN.glob("agents/*.md"), *PLUGIN.glob("commands/*.md"), PLUGIN / "TEAM.md"]:
        assert "record_claude_review" not in path.read_text(), path.name


def test_commands_exist_and_call_their_tools(PLUGIN):
    expected = {
        "codex-model": "codex_settings",
        "usage": "usage",
        "review": "codex_review",
        "plan": "codex_plan",
        "codex-mode": "codex_settings",
        "claude-model": "team_models",
        "claude-effort": "team_models",
        "codex-effort": "codex_settings",
        "wf-settings": "settings",
    }
    for command, tool in expected.items():
        fields, body = frontmatter(PLUGIN / "commands" / f"{command}.md")
        assert fields["description"]
        assert f"mcp__plugin_workforce_codex__{tool}" in body
    review = (PLUGIN / "commands" / "review.md").read_text()
    assert "review_status" in review and "mcp__plugin_workforce_codex__claude_review" in review
    fields, body = frontmatter(PLUGIN / "commands" / "wf-continue.md")
    assert fields["description"] and body.strip()


def plugin_texts(PLUGIN) -> dict[str, str]:
    files = [*PLUGIN.glob("commands/*.md"), *PLUGIN.glob("agents/*.md"), PLUGIN / "TEAM.md"]
    return {path.name: path.read_text() for path in files}


def test_every_referenced_mcp_tool_exists_on_the_server(PLUGIN):
    referenced = set()
    for name, text in plugin_texts(PLUGIN).items():
        found = set(TOOL_PATTERN.findall(text))
        assert found or name in {"wf-continue.md", "wf-allow-outside.md", "fast-coder.md"}, f"{name} references no MCP tool"
        referenced |= found
    assert referenced
    assert referenced <= TOOL_NAMES, referenced - TOOL_NAMES
    # no bare, unprefixed use of a tool name in a backtick call
    for name, text in plugin_texts(PLUGIN).items():
        for bare in re.findall(r"`((?:codex_plan|codex_ask|codex_review|claude_review|review_status|codex_settings))`", text):
            pytest.fail(f"{name} uses unprefixed tool name {bare}")


def test_team_md_has_the_eight_rules_and_is_tight(PLUGIN):
    text = (PLUGIN / "TEAM.md").read_text()
    rules = re.findall(r"^(\d)\. ", text, re.M)
    assert rules == ["1", "2", "3", "4", "5", "6", "7", "8"]
    assert len(text) < 3000
    for needle in ("codex_plan", "fast-coder", "codex_review", "claude_review", "/codex-model", "force-push", "verbatim", "/codex-mode", "`repo`"):
        assert needle in text


def test_pyproject_entry_points():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["wf"] == "workforce.team.launch:main"
    assert scripts["workforce"] == "workforce.team.launch:main"
