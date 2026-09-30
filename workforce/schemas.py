"""JSON schemas exchanged with the agent CLIs, plus a small validator for them.

Every object lists all of its properties as required and forbids extras, because
both CLIs' structured-output modes demand that shape.
"""

from typing import Any

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _plan(repo_names: list[str] | None) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": _STR,
        "title": _STR,
        "description": _STR,
        "acceptance": _STR_LIST,
        "depends_on": _STR_LIST,
    }
    if repo_names is not None:
        task["repo"] = {"type": "string", "enum": list(repo_names)}
    return _obj(
        {
            "tasks": {"type": "array", "items": _obj(task)},
            "risks": _STR_LIST,
            "open_questions": _STR_LIST,
        }
    )


PLAN: dict[str, Any] = _plan(None)

PLAN_REVIEW: dict[str, Any] = _obj(
    {
        "agree": {"type": "boolean"},
        "objections": _STR_LIST,
        "suggested_changes": _STR_LIST,
    }
)

DEBATE_TURN: dict[str, Any] = _obj(
    {
        "position": _STR,
        "concessions": _STR_LIST,
        "remaining_disagreements": _STR_LIST,
    }
)

VERDICT: dict[str, Any] = _obj(
    {
        "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "BLOCKED"]},
        "summary": _STR,
        "findings": {
            "type": "array",
            "items": _obj(
                {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor", "nit"]},
                    "file": _STR,
                    "line": {"type": ["integer", "null"]},
                    "message": _STR,
                }
            ),
        },
    }
)

PLAN_LOOP_VERDICT: dict[str, Any] = _obj(
    {
        "verdict": {"type": "string", "enum": ["APPROVED", "REVISE", "BLOCKED"]},
        "summary": _STR,
        "findings": {
            "type": "array",
            "items": _obj(
                {
                    "id": _STR,
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "path": {"type": ["string", "null"]},
                    "evidence": _STR,
                    "fix": _STR,
                }
            ),
        },
        "coverage": _STR_LIST,
        "limitations": _STR_LIST,
    }
)

PLAN_REVISION: dict[str, Any] = _obj({"plan": PLAN, "debate": DEBATE_TURN})

PUBLISH_RESULT: dict[str, Any] = _obj(
    {
        "pushed": {"type": "boolean"},
        "pr_url": {"type": ["string", "null"]},
        "error": {"type": ["string", "null"]},
    }
)

COMMIT_RESULT: dict[str, Any] = _obj(
    {
        "committed": {"type": "boolean"},
        "sha": {"type": ["string", "null"]},
        "message": {"type": ["string", "null"]},
        "error": {"type": ["string", "null"]},
    }
)

ALL: dict[str, dict[str, Any]] = {
    "plan": PLAN,
    "plan_review": PLAN_REVIEW,
    "debate_turn": DEBATE_TURN,
    "plan_revision": PLAN_REVISION,
    "verdict": VERDICT,
    "commit_result": COMMIT_RESULT,
    "publish_result": PUBLISH_RESULT,
}


def _require_repos(repo_names: list[str]) -> None:
    if not repo_names:
        raise ValueError("a plan schema needs at least one repo name")


def plan_schema(repo_names: list[str]) -> dict[str, Any]:
    """`PLAN` with a required `repo` enum (the workspace's repo names) on every task."""
    _require_repos(repo_names)
    return _plan(repo_names)


def plan_revision_schema(repo_names: list[str]) -> dict[str, Any]:
    """`PLAN_REVISION` whose plan tasks carry the required `repo` enum."""
    return _obj({"plan": plan_schema(repo_names), "debate": DEBATE_TURN})


def validate_plan_repos(plan: Any, repo_names: list[str]) -> list[str]:
    """Errors for tasks whose `repo` is missing or not one of `repo_names`; empty means every task is tagged."""
    tasks = plan.get("tasks") if isinstance(plan, dict) else None
    if not isinstance(tasks, list):
        return ["plan.tasks must be a list"]
    errors = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"plan.tasks[{index}] must be an object")
            continue
        label = task.get("id") if isinstance(task.get("id"), str) else f"tasks[{index}]"
        repo = task.get("repo")
        if repo is None:
            errors.append(f"task {label} has no repo; expected one of {sorted(repo_names)}")
        elif repo not in repo_names:
            errors.append(f"task {label} names unknown repo {repo!r}; expected one of {sorted(repo_names)}")
    return errors


def _type_ok(type_name: str, value: Any) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "null":
        return value is None
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    raise ValueError(f"unsupported schema type: {type_name}")


def validate(schema: dict[str, Any], obj: Any) -> list[str]:
    """Return a list of human-readable errors; an empty list means `obj` is valid."""
    errors: list[str] = []
    _check(schema, obj, "$", errors)
    return errors


def _check(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    declared = schema.get("type")
    if declared is not None:
        allowed = declared if isinstance(declared, list) else [declared]
        if not any(_type_ok(t, value) for t in allowed):
            errors.append(f"{path}: expected {' or '.join(allowed)}, got {_describe(value)}")
            return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property '{key}'")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected property '{key}'")
        for key, sub in properties.items():
            if key in value:
                _check(sub, value[key], f"{path}.{key}", errors)

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check(schema["items"], item, f"{path}[{index}]", errors)


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    return type(value).__name__
