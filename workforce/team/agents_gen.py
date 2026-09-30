"""Regenerate `wf-plugin/agents/*.md` from `wf-plugin/agent-templates/*.md` with the models set in team.toml.

Plugin agent files carry a static `model:` and `effort:`, so `wf` rewrites them at launch. A file is only rewritten when
its content would change.
"""

from __future__ import annotations

from pathlib import Path

from workforce.team import config

TEMPLATES_DIRNAME = "agent-templates"
AGENTS_DIRNAME = "agents"
ROLES = {"fast-coder": "fast_coder"}
OBSOLETE = ("reviewer",)


def render(template: str, model: str, effort: str) -> str:
    return template.replace("{{model}}", model).replace("{{effort}}", effort)


def regenerate(plugin: Path, cfg: config.TeamConfig) -> list[Path]:
    """Write each agent file whose content differs from what team.toml says; returns the files changed."""
    changed: list[Path] = []
    for name in OBSOLETE:
        stale = plugin / AGENTS_DIRNAME / f"{name}.md"
        if stale.is_file():
            stale.unlink()
            changed.append(stale)
    for name, prefix in ROLES.items():
        template = plugin / TEMPLATES_DIRNAME / f"{name}.md"
        if not template.is_file():
            continue
        text = render(template.read_text(encoding="utf-8"), getattr(cfg, f"{prefix}_model"), getattr(cfg, f"{prefix}_effort"))
        target = plugin / AGENTS_DIRNAME / f"{name}.md"
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            continue
        config.atomic_write(target, text)
        changed.append(target)
    return changed
