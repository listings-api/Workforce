"""Render the plugin's `agents/*.md` from `agent-templates/*.md` with the models set in team.toml.

Plugin agent files carry a static `model:` and `effort:`, so `plugin_runtime.prepare` rewrites them at every launch.
"""

from __future__ import annotations

from pathlib import Path

from workforce.team import config

TEMPLATES_DIRNAME = "agent-templates"
AGENTS_DIRNAME = "agents"
ROLES = {"fast-coder": "fast_coder"}


def render(template: str, model: str, effort: str) -> str:
    return template.replace("{{model}}", model).replace("{{effort}}", effort)


def rendered(plugin: Path, cfg: config.TeamConfig) -> dict[str, str]:
    """`agents/<name>.md` (relative to the plugin) -> its text, for each role whose template exists."""
    files: dict[str, str] = {}
    for name, prefix in ROLES.items():
        template = plugin / TEMPLATES_DIRNAME / f"{name}.md"
        if not template.is_file():
            continue
        files[f"{AGENTS_DIRNAME}/{name}.md"] = render(
            template.read_text(encoding="utf-8"), getattr(cfg, f"{prefix}_model"), getattr(cfg, f"{prefix}_effort")
        )
    return files
