"""The review hard stop: after 3 rejections in a row the review tools refuse, until the user types /wf-review-again."""

import json

from workforce.team import hooks, relay
from tests.test_team_hooks import NOW, denied, home  # noqa: F401  (fixture + helpers)
from tests.test_team_mcp import env, finding, verdict  # noqa: F401  (fixtures + helpers)

REJECT = verdict("REQUEST_CHANGES", "needs work", [finding("major", "fix the bug")])


def review(client, tool="codex_review"):
    return client.call(tool, {})


def body(result):
    return json.loads(result["content"][0]["text"])


def test_reviews_stop_after_three_rejections_in_a_row(env):
    (env.repo / "app.py").write_text("print('changed')\n")
    env.scenario([REJECT] * 4)
    client = env.start()
    for _ in range(3):
        assert body(review(client))["verdict"] == "REQUEST_CHANGES"
    fourth = review(client)
    text = fourth["content"][0]["text"]
    assert fourth.get("isError") and "round limit reached" in text and "do not commit" in text and "/wf-review-again" in text
    assert len(env.calls()) == 3


def test_an_approval_resets_the_count(env):
    (env.repo / "app.py").write_text("print('changed')\n")
    env.scenario([REJECT, REJECT, verdict("APPROVE"), REJECT, REJECT, REJECT])
    client = env.start()
    for _ in range(6):
        assert not review(client).get("isError")


def test_each_reviewer_is_counted_on_its_own(env):
    (env.repo / "app.py").write_text("print('changed')\n")
    env.scenario([REJECT] * 3, claude=[REJECT])
    client = env.start()
    for _ in range(3):
        review(client)
    assert review(client).get("isError")
    assert not review(client, "claude_review").get("isError")


def test_only_the_users_wf_review_again_lifts_the_stop(env):
    (env.repo / "app.py").write_text("print('changed')\n")
    env.scenario([REJECT] * 4)
    client = env.start()
    for _ in range(3):
        review(client)
    assert review(client).get("isError")
    out = hooks.evaluate("userpromptsubmit", {"prompt": "/wf-review-again keep the retry, drop the cache", "cwd": str(env.repo)}, env={"CLAUDE_PROJECT_DIR": str(env.repo)}, home=env.home, now=NOW)
    assert "allowed more review rounds" in out["hookSpecificOutput"]["additionalContext"]
    assert not review(client).get("isError")


def test_claude_cannot_write_the_marker_itself(tmp_path, home):
    marker = relay.team_dir(tmp_path) / "review-again.json"
    for tool, tool_input in (("Write", {"file_path": str(marker), "content": "{}"}), ("Edit", {"file_path": str(marker), "old_string": "a", "new_string": "b"})):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(tmp_path)}
        assert "/wf-review-again" in denied(hooks.evaluate("pretooluse", payload, env={}, home=home, now=NOW))
    for command in ("touch .workforce/team/review-again.json", "echo '{\"at\": 9e9}' > .workforce/team/review-again.json"):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}
        assert denied(hooks.evaluate("pretooluse", payload, env={}, home=home, now=NOW))


def test_the_plugin_has_the_command_and_the_rule():
    from workforce.team import plugin_runtime

    source = plugin_runtime.source_dir()
    assert "reset the review round limit" in (source / "commands" / "wf-review-again.md").read_text()
    assert "/wf-review-again" in (source / "TEAM.md").read_text()
