import copy

import pytest

from workforce import schemas

GOOD_PLAN = {
    "tasks": [
        {"id": "T1", "title": "a", "description": "b", "acceptance": ["c"], "depends_on": []},
        {"id": "T2", "title": "a", "description": "b", "acceptance": [], "depends_on": ["T1"]},
    ],
    "risks": ["r"],
    "open_questions": [],
}
GOOD_VERDICT = {
    "verdict": "REQUEST_CHANGES",
    "summary": "needs work",
    "findings": [
        {"severity": "major", "file": "a.py", "line": 12, "message": "bug"},
        {"severity": "nit", "file": "b.py", "line": None, "message": "style"},
    ],
}
GOOD = {
    "plan": GOOD_PLAN,
    "plan_review": {"agree": False, "objections": ["x"], "suggested_changes": []},
    "debate_turn": {"position": "p", "concessions": [], "remaining_disagreements": ["d"]},
    "plan_revision": {
        "plan": GOOD_PLAN,
        "debate": {"position": "p", "concessions": ["c"], "remaining_disagreements": []},
    },
    "verdict": GOOD_VERDICT,
    "commit_result": {"committed": True, "sha": "abc", "message": "m", "error": None},
    "publish_result": {"pushed": True, "pr_url": "https://example.com/pr/1", "error": None},
}


@pytest.mark.parametrize("name", list(schemas.ALL))
def test_good_objects_accepted(name):
    assert schemas.validate(schemas.ALL[name], GOOD[name]) == []


def _walk_objects(schema):
    if schema.get("type") == "object":
        yield schema
        for sub in schema["properties"].values():
            yield from _walk_objects(sub)
    if schema.get("type") == "array":
        yield from _walk_objects(schema["items"])


@pytest.mark.parametrize("name", list(schemas.ALL))
def test_every_object_is_strict_and_fully_required(name):
    objects = list(_walk_objects(schemas.ALL[name]))
    assert objects
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def test_field_lists_match_spec():
    assert list(schemas.PLAN["properties"]) == ["tasks", "risks", "open_questions"]
    assert list(schemas.PLAN["properties"]["tasks"]["items"]["properties"]) == [
        "id",
        "title",
        "description",
        "acceptance",
        "depends_on",
    ]
    assert list(schemas.PLAN_REVIEW["properties"]) == ["agree", "objections", "suggested_changes"]
    assert list(schemas.DEBATE_TURN["properties"]) == ["position", "concessions", "remaining_disagreements"]
    assert list(schemas.PLAN_REVISION["properties"]) == ["plan", "debate"]
    assert list(schemas.VERDICT["properties"]) == ["verdict", "summary", "findings"]
    assert list(schemas.VERDICT["properties"]["findings"]["items"]["properties"]) == [
        "severity",
        "file",
        "line",
        "message",
    ]
    assert list(schemas.COMMIT_RESULT["properties"]) == ["committed", "sha", "message", "error"]
    assert schemas.VERDICT["properties"]["verdict"]["enum"] == ["APPROVE", "REQUEST_CHANGES", "BLOCKED"]
    assert schemas.VERDICT["properties"]["findings"]["items"]["properties"]["severity"]["enum"] == [
        "blocker",
        "major",
        "minor",
        "nit",
    ]


def _bad(name, mutate):
    obj = copy.deepcopy(GOOD[name])
    mutate(obj)
    return schemas.validate(schemas.ALL[name], obj)


def test_missing_required_property():
    errors = _bad("plan", lambda o: o.pop("risks"))
    assert errors == ["$: missing required property 'risks'"]


def test_additional_property_rejected():
    errors = _bad("verdict", lambda o: o.__setitem__("extra", 1))
    assert errors == ["$: unexpected property 'extra'"]


def test_nested_additional_and_missing():
    errors = _bad("plan", lambda o: o["tasks"][1].pop("title"))
    assert errors == ["$.tasks[1]: missing required property 'title'"]
    errors = _bad("verdict", lambda o: o["findings"][0].__setitem__("bonus", True))
    assert errors == ["$.findings[0]: unexpected property 'bonus'"]


def test_enum_violation():
    errors = _bad("verdict", lambda o: o.__setitem__("verdict", "MAYBE"))
    assert len(errors) == 1 and "'MAYBE' is not one of" in errors[0]
    errors = _bad("verdict", lambda o: o["findings"][0].__setitem__("severity", "critical"))
    assert len(errors) == 1 and errors[0].startswith("$.findings[0].severity")


def test_wrong_types():
    assert "expected boolean, got str" in _bad("plan_review", lambda o: o.__setitem__("agree", "yes"))[0]
    assert "expected string, got int" in _bad("debate_turn", lambda o: o.__setitem__("position", 3))[0]
    errors = _bad("plan", lambda o: o["tasks"][0]["acceptance"].append(5))
    assert errors == ["$.tasks[0].acceptance[1]: expected string, got int"]
    assert "expected array, got str" in _bad("plan", lambda o: o.__setitem__("risks", "none"))[0]
    assert "expected object, got list" in schemas.validate(schemas.PLAN, [])[0]


def test_integer_rejects_bool_and_float():
    assert _bad("verdict", lambda o: o["findings"][0].__setitem__("line", True))
    assert _bad("verdict", lambda o: o["findings"][0].__setitem__("line", 1.5))


def test_nullable_integer_accepts_null_and_int_only():
    assert _bad("verdict", lambda o: o["findings"][0].__setitem__("line", None)) == []
    errors = _bad("verdict", lambda o: o["findings"][0].__setitem__("line", "12"))
    assert errors == ["$.findings[0].line: expected integer or null, got str"]


def test_commit_result_nullable_strings():
    failed = {"committed": False, "sha": None, "message": None, "error": "cancelled"}
    assert schemas.validate(schemas.COMMIT_RESULT, failed) == []
    assert schemas.validate(schemas.COMMIT_RESULT, {**failed, "sha": 5})


def test_multiple_errors_collected():
    errors = schemas.validate(schemas.PLAN_REVIEW, {"agree": 1, "objections": "x", "nope": None})
    assert len(errors) == 4
    assert any("missing required property 'suggested_changes'" in e for e in errors)
    assert any("unexpected property 'nope'" in e for e in errors)


def test_validator_supports_number_and_rejects_unknown_type():
    assert schemas.validate({"type": "number"}, 1.5) == []
    assert schemas.validate({"type": "number"}, True) != []
    with pytest.raises(ValueError, match="unsupported schema type"):
        schemas.validate({"type": "set"}, 1)


def test_plan_revision_embeds_plan_and_debate_turn():
    assert schemas.PLAN_REVISION["properties"]["plan"] == schemas.PLAN
    assert schemas.PLAN_REVISION["properties"]["debate"] == schemas.DEBATE_TURN
    assert schemas.PLAN_REVISION["required"] == ["plan", "debate"]
    assert schemas.PLAN_REVISION["additionalProperties"] is False


def test_plan_revision_rejects_bad_objects():
    assert _bad("plan_revision", lambda o: o.pop("debate")) == ["$: missing required property 'debate'"]
    assert _bad("plan_revision", lambda o: o.pop("plan")) == ["$: missing required property 'plan'"]
    assert _bad("plan_revision", lambda o: o.__setitem__("extra", 1)) == ["$: unexpected property 'extra'"]
    assert _bad("plan_revision", lambda o: o["plan"]["tasks"][0].pop("id")) == [
        "$.plan.tasks[0]: missing required property 'id'"
    ]
    assert _bad("plan_revision", lambda o: o["debate"].__setitem__("concessions", "none")) == [
        "$.debate.concessions: expected array, got str"
    ]
    assert schemas.validate(schemas.PLAN_REVISION, schemas.PLAN)
