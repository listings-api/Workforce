"""Prompt templates: wording lives only in `workforce/prompts/*.md`."""

from importlib import resources

from workforce.errors import WorkforceError


class _Strict(dict):
    def __missing__(self, key: str) -> str:
        raise KeyError(f"missing prompt variable {key!r}")


def render(name: str, **vars: object) -> str:
    """Fill `workforce/prompts/<name>.md` with `vars`; a missing variable raises KeyError."""
    template = resources.files("workforce").joinpath("prompts", f"{name}.md")
    try:
        text = template.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise WorkforceError(f"no prompt template named '{name}'") from None
    return text.format_map(_Strict(vars))
