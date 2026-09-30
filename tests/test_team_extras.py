"""Verbatim relay (@codex, thread, artefacts), `wf codex`, /codex-mode, /team-models, /wf-settings.

Fakes only: the fake codex, tmp homes and tmp projects. The real claude/codex are never run.
"""

import io
import json
import re
import subprocess
import threading
from pathlib import Path

import pytest

from tests.test_team_launch import run as launch_run
from tests.test_team_launch import setup as launch_setup  # noqa: F401  (fixture)
from tests.test_team_mcp import FAKE, ROOT, env, finding, verdict  # noqa: F401  (fixtures + helpers)
from workforce.errors import ConfigError
from workforce.team import agents_gen, approvals, config, hooks, launch, plugin_runtime, relay, usage_cache

NOW = 1_800_000_000.0
TRICKY = "why does `f(x)` return {y}?\n```python\ndef f(x):\n    return '{}'.format(x) % 5\n```\nünïcode 日本語 🚀\n  keep   spacing  \n"
TRICKY_REPLY = "Because:\n\n```py\nx = {'a': 1}\n```\n  - 100% sure\n{not a placeholder} é 日本 🚀\n\n"


# ------------------------------------------------------------------ helpers for the hook path


@pytest.fixture
def hk(tmp_path, monkeypatch):
    """A tmp home whose team.toml points at the fake codex, a git project, and a scenario file."""
    home = tmp_path / "home"
    (home / ".workforce").mkdir(parents=True)
    (home / ".workforce" / "team.toml").write_text(f'codex = "{FAKE}"\n')
    project = tmp_path / "proj"
    project.mkdir()
    for cmd in (["init", "-b", "main"], ["config", "user.name", "T"], ["config", "user.email", "t@example.com"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(project), *cmd], check=True, capture_output=True)
    (project / "app.py").write_text("print('hi')\n")
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project), "commit", "-m", "initial"], check=True, capture_output=True)
    scenario = tmp_path / "scenario.json"
    monkeypatch.setenv("WF_FAKE_SCENARIO", str(scenario))
    monkeypatch.setenv("HOME", str(home))

    class Kit:
        pass

    kit = Kit()
    kit.home, kit.project, kit.scenario_file = home, project, scenario
    kit.scenario = lambda steps: scenario.write_text(json.dumps({"codex": list(steps)}))
    kit.calls = lambda: [json.loads(l) for l in Path(str(scenario) + ".calls.jsonl").read_text().splitlines()] if Path(str(scenario) + ".calls.jsonl").exists() else []
    kit.scenario([])

    def submit(text, **extra):
        payload = {"prompt": text, "cwd": str(project), **extra}
        return hooks.evaluate("userpromptsubmit", payload, env={}, home=home, now=NOW)

    kit.submit = submit
    kit.thread = lambda: (project / ".workforce" / "team" / "codex-thread.md").read_text(encoding="utf-8")
    return kit


def reason_of(output):
    assert output["decision"] == "block", output
    return output["reason"]


def context_of(output):
    assert "decision" not in output, output
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return output["hookSpecificOutput"]["additionalContext"]


# ------------------------------------------------------------------ @codex


@pytest.mark.parametrize("prefix", ["@codex ", "@codex: ", "@Codex:", "@CODEX\n", "  @codex   "])
def test_at_codex_sends_the_text_byte_for_byte_and_returns_the_reply_unchanged(hk, prefix):
    hk.scenario([{"text": TRICKY_REPLY, "session_id": "thread-A"}])
    out = hk.submit(prefix + TRICKY)
    # Claude receives nothing: the prompt is blocked and there is no additionalContext
    assert set(out) == {"decision", "reason"} and out["decision"] == "block"
    assert reason_of(out) == f"Codex (gpt-6-sol):\n{TRICKY_REPLY}"
    (call,) = hk.calls()
    assert call["prompt"] == TRICKY
    argv = call["argv"]
    assert argv[0] == "exec" and "resume" not in argv
    assert argv[argv.index("-s") + 1] == "read-only" and argv[argv.index("-C") + 1] == str(hk.project)
    assert argv[argv.index("-m") + 1] == "gpt-6-sol"


def test_hook_json_round_trips_unicode_and_newlines(hk):
    hk.scenario([{"text": TRICKY_REPLY}])
    stdin = io.StringIO(json.dumps({"prompt": "@codex " + TRICKY, "cwd": str(hk.project)}))
    stdout = io.StringIO()
    assert hooks.main(["userpromptsubmit"], stdin=stdin, stdout=stdout, env={}, home=hk.home, now=NOW) == 0
    assert json.loads(stdout.getvalue())["reason"] == f"Codex (gpt-6-sol):\n{TRICKY_REPLY}"
    assert hk.calls()[0]["prompt"] == TRICKY


@pytest.mark.parametrize("text", ["hello @codex", "@codexfoo bar", "@codex-x y", "please ask @codex: this", "codex: hi", ""])
def test_only_a_leading_at_codex_is_a_direct_line(hk, text):
    assert relay.parse_direct(text) is None
    assert hk.submit(text) is None
    assert hk.calls() == []


def test_parse_direct_keeps_everything_after_the_prefix_but_the_separator():
    assert relay.parse_direct("@codex hi  \n") == "hi  \n"
    assert relay.parse_direct("@codex:hi") == "hi"
    assert relay.parse_direct("@codex\n\n  indented") == "indented"
    assert relay.parse_direct("@codex") == ""
    assert relay.parse_direct(None) is None


def test_at_codex_appends_message_and_reply_verbatim_to_the_thread(hk):
    hk.scenario([{"text": TRICKY_REPLY}])
    hk.submit("@codex " + TRICKY)
    thread = hk.thread()
    assert TRICKY in thread and TRICKY_REPLY.rstrip("\n") in thread
    heads = re.findall(r"^### (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) · (.+)$", thread, re.M)
    assert [who for _, who in heads] == ["user → codex", "codex (gpt-6-sol) → user"]
    assert thread.index(TRICKY) < thread.index("Because:")


def test_next_normal_prompt_carries_unseen_entries_verbatim_exactly_once(hk):
    hk.scenario([{"text": TRICKY_REPLY, "session_id": "thread-A"}, {"text": "second reply", "session_id": "thread-A"}])
    hk.submit("@codex " + TRICKY)
    ctx = context_of(hk.submit("now implement it"))
    assert ctx.startswith("Direct user↔Codex exchange (verbatim, not summarised):")
    assert TRICKY in ctx and TRICKY_REPLY.rstrip("\n") in ctx
    assert ctx.count(TRICKY) == 1
    assert hk.submit("and another normal prompt") is None  # exactly once

    hk.submit("@codex second question")
    ctx2 = context_of(hk.submit("go on"))
    assert "second question" in ctx2 and "second reply" in ctx2
    assert TRICKY not in ctx2  # only what is new since Claude's last turn
    assert hk.submit("again") is None


def test_at_codex_prompts_do_not_consume_the_thread_and_blocked_or_continue_prompts_keep_it(hk):
    hk.scenario([{"text": "r1"}, {"text": "r2"}])
    hk.submit("@codex one")
    hk.submit("@codex two")  # still unseen by Claude
    usage_cache.write_claude({"five_hour": {"used_percentage": 70, "resets_at": int(NOW) + 3600}}, hk.home, now=NOW)
    blocked = hk.submit("work please")
    assert blocked["decision"] == "block" and "usage stop" in blocked["reason"]
    assert "Direct user↔Codex" not in json.dumps(blocked)
    hk.submit("/wf-continue")  # carries the override message, not the thread
    ctx = context_of(hk.submit("now work"))
    assert "one" in ctx and "r1" in ctx and "two" in ctx and "r2" in ctx


def test_thread_and_usage_alert_share_one_context(hk):
    hk.scenario([{"text": "r1"}])
    hk.submit("@codex one")
    usage_cache.write_claude({"five_hour": {"used_percentage": 55, "resets_at": int(NOW) + 3600}}, hk.home, now=NOW)
    ctx = context_of(hk.submit("work"))
    assert "usage alert" in ctx and "Direct user↔Codex exchange (verbatim, not summarised):" in ctx and "r1" in ctx


def test_second_at_codex_resumes_the_same_team_session(hk):
    hk.scenario([{"text": "first", "session_id": "thread-A"}, {"text": "second"}])
    hk.submit("@codex first question")
    session = json.loads((hk.project / ".workforce" / "team" / "session.json").read_text())
    assert session["session_id"] == "thread-A" and session["model"] == "gpt-6-sol"
    hk.submit("@codex second question")
    argv = hk.calls()[1]["argv"]
    assert argv[:3] == ["exec", "resume", "thread-A"]
    assert 'sandbox_mode="read-only"' in argv


def test_at_codex_failure_is_shown_clearly_and_recorded(hk):
    hk.scenario([{"error": "rate_limited"}])
    reason = reason_of(hk.submit("@codex hello"))
    assert reason.startswith("Codex (gpt-6-sol):\n[Codex did not reply:") and "rate_limited" in reason
    assert "(system" in hk.thread() or "system (gpt-6-sol)" in hk.thread()
    assert not (hk.project / ".workforce" / "team" / "session.json").exists()


def test_empty_at_codex_calls_nothing(hk):
    reason = reason_of(hk.submit("@codex"))
    assert "Nothing to send" in reason and hk.calls() == []


def test_a_broken_relay_still_blocks_and_says_so(hk, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(relay, "direct_codex", boom)
    reason = reason_of(hk.submit("@codex hi"))
    assert "not sent" in reason and "disk on fire" in reason


def test_reply_over_200k_chars_is_cut_with_a_marked_notice_and_kept_whole_in_the_file(hk):
    big = "x" * (relay.MAX_REPLY_CHARS + 50)
    hk.scenario([{"text": big}])
    reason = reason_of(hk.submit("@codex big one"))
    body = reason.split("\n", 1)[1]
    assert body.startswith("x" * relay.MAX_REPLY_CHARS) and "WorkForce truncation notice: 50 more characters" in body
    saved = next((hk.project / ".workforce" / "team").glob("ask-*.md"))
    assert str(saved) in body and relay.artifact_reply(saved) == big
    small = "y" * relay.MAX_REPLY_CHARS
    assert relay.truncate_reply(small) == small


def test_take_unseen_recovers_when_the_cursor_is_stale(hk):
    relay.append_thread(hk.project, "user → codex", "hello")
    assert "hello" in relay.take_unseen(hk.project)
    assert relay.take_unseen(hk.project) is None
    (hk.project / ".workforce" / "team" / "codex-thread.seen").write_text("999999")
    assert "hello" in relay.take_unseen(hk.project)
    (hk.project / ".workforce" / "team" / "codex-thread.seen").write_text("garbage")
    assert "hello" in relay.take_unseen(hk.project)


def test_concurrent_prompts_hand_out_each_entry_once(hk):
    relay.append_thread(hk.project, "user → codex", "once only")
    got = []
    threads = [threading.Thread(target=lambda: got.append(relay.take_unseen(hk.project))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [g for g in got if g] and len([g for g in got if g]) == 1


# ------------------------------------------------------------------ artefacts (MCP tools)


def only(directory: Path, pattern: str) -> Path:
    (path,) = sorted(directory.glob(pattern))
    return path


def test_plan_and_ask_save_the_raw_reply_with_the_exact_prompt(env):
    plan_reply = "1. do the thing\n```sh\nls {a}\n```\nRisks: 100% none é日本\n"
    env.scenario([{"text": plan_reply, "session_id": "thread-plan"}, {"text": "ask reply\n\n", "session_id": "thread-plan"}])
    client = env.start()
    task = "fix `billing` {now}\nsecond line 日本"
    text = client.text("codex_plan", {"task": task, "context": "ctx with {braces}"})
    team = env.repo / ".workforce" / "team"
    plan_file = only(team, "plan-*.md")
    assert plan_file.name == "plan-1.md"
    assert plan_reply in text  # the reply reaches Claude unchanged
    assert f"[raw Codex output and the exact prompt saved: {plan_file}]" in text
    assert "[codex session_id: thread-plan]" in text
    saved = plan_file.read_text(encoding="utf-8")
    assert relay.artifact_reply(plan_file) == plan_reply
    sent_prompt = env.calls()[0]["prompt"]
    assert task in sent_prompt
    assert sent_prompt in saved  # the exact prompt given to Codex
    assert f"===== SENT BY CLAUDE: task (exact) =====\n{task}\n" in saved
    assert "ctx with {braces}" in saved

    ask = client.text("codex_ask", {"message": "and retries? ```x```", "session_id": "thread-plan"})
    ask_file = only(team, "ask-*.md")
    assert ask_file.name == "ask-1.md" and str(ask_file) in ask
    assert relay.artifact_reply(ask_file) == "ask reply\n\n"
    assert env.calls()[1]["prompt"] in ask_file.read_text()


def test_artefact_numbers_count_up_per_kind(env):
    env.scenario([{"text": "a"}, {"text": "b"}, {"text": "c"}])
    client = env.start()
    client.text("codex_ask", {"message": "1"})
    client.text("codex_plan", {"task": "t"})
    client.text("codex_ask", {"message": "2"})
    names = sorted(p.name for p in (env.repo / ".workforce" / "team").glob("*.md"))
    assert names == ["ask-1.md", "ask-2.md", "plan-1.md"]


def test_review_saves_the_raw_output_and_names_the_file(env):
    env.scenario([verdict("APPROVE", "fine", [finding("nit", "tidy `x`")])])
    (env.repo / "app.py").write_text("print('changed')\n")
    client = env.start()
    result = json.loads(client.text("codex_review", {"focus": "look at {x}"}))
    review = only(env.repo / ".workforce" / "team", "review-*.md")
    assert str(review) in result["raw_output"]
    saved = review.read_text(encoding="utf-8")
    assert env.calls()[0]["prompt"] in saved and "look at {x}" in saved
    assert json.loads(relay.artifact_reply(review))["summary"] == "fine"


def test_saving_artefacts_never_changes_the_tree_hash_the_reviews_are_tied_to(env):
    env.scenario([{"text": "plan"}])
    before = approvals.current_tree(env.repo)
    client = env.start()
    client.text("codex_plan", {"task": "t"})
    assert (env.repo / ".workforce" / "team" / "plan-1.md").exists()
    assert approvals.current_tree(env.repo) == before
    status = subprocess.run(["git", "-C", str(env.repo), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert status.strip() == ""
    exclude = subprocess.run(["git", "-C", str(env.repo), "rev-parse", "--git-path", "info/exclude"], capture_output=True, text=True).stdout.strip()
    assert ".workforce/" in (env.repo / exclude).read_text()


def test_a_reply_over_200k_is_cut_in_the_tool_result_but_whole_in_the_file(env):
    big = "z" * (relay.MAX_REPLY_CHARS + 10)
    env.scenario([{"text": big}])
    client = env.start()
    text = client.text("codex_ask", {"message": "big"})
    assert "WorkForce truncation notice: 10 more characters" in text
    assert relay.artifact_reply(only(env.repo / ".workforce" / "team", "ask-*.md")) == big


def test_plan_and_ask_remember_the_session_but_review_does_not(env):
    env.scenario([{"text": "p", "session_id": "thread-1"}, verdict(), {"text": "q", "session_id": "thread-2"}])
    (env.repo / "app.py").write_text("x = 1\n")
    client = env.start()
    session = env.repo / ".workforce" / "team" / "session.json"
    client.text("codex_plan", {"task": "t"})
    assert json.loads(session.read_text())["session_id"] == "thread-1"
    client.text("codex_review")
    assert json.loads(session.read_text())["session_id"] == "thread-1"
    client.text("codex_ask", {"message": "m"})
    assert json.loads(session.read_text())["session_id"] == "thread-2"


# ------------------------------------------------------------------ /codex-mode


def test_codex_mode_defaults_to_read_only_and_persists(env):
    client = env.start()
    assert json.loads(client.text("codex_settings"))["codex_mode"] == "read-only"
    got = json.loads(client.text("codex_settings", {"mode": "write"}))
    assert got["codex_mode"] == "write" and got["modes"] == ["read-only", "write"]
    assert config.load(env.home).codex_mode == "write"
    assert 'codex_mode = "write"' in (env.home / ".workforce" / "team.toml").read_text()
    assert "not valid" in client.error_text("codex_settings", {"mode": "yolo"})
    assert config.load(env.home).codex_mode == "write"
    both = json.loads(client.text("codex_settings", {"mode": "read-only", "model": "gpt-6-astra"}))
    assert (both["codex_mode"], both["codex_model"]) == ("read-only", "gpt-6-astra")


def sandbox_of(argv):
    if "-s" in argv:
        return argv[argv.index("-s") + 1]
    (value,) = [a for a in argv if a.startswith("sandbox_mode=")]
    return value.split('"')[1]


def test_write_mode_makes_codex_ask_workspace_write_but_not_plan_or_review(env):
    env.scenario([{"text": "ask fresh"}, {"text": "ask resume", "session_id": "thread-x"}, {"text": "plan"}, verdict()])
    (env.repo / "app.py").write_text("x = 2\n")
    client = env.start()
    client.text("codex_settings", {"mode": "write"})
    client.text("codex_ask", {"message": "fresh"})
    client.text("codex_ask", {"message": "resume", "session_id": "thread-x"})
    client.text("codex_plan", {"task": "t"})
    client.text("codex_review")
    calls = env.calls()
    assert sandbox_of(calls[0]["argv"]) == "workspace-write"
    assert calls[0]["argv"][calls[0]["argv"].index("-C") + 1] == str(env.repo)
    assert sandbox_of(calls[1]["argv"]) == "workspace-write" and calls[1]["argv"][:3] == ["exec", "resume", "thread-x"]
    assert sandbox_of(calls[2]["argv"]) == "read-only"
    assert sandbox_of(calls[3]["argv"]) == "read-only"


def test_read_only_mode_keeps_ask_read_only(env):
    env.scenario([{"text": "a"}, {"text": "b", "session_id": "t"}])
    client = env.start()
    client.text("codex_ask", {"message": "1"})
    client.text("codex_ask", {"message": "2", "session_id": "t"})
    assert [sandbox_of(c["argv"]) for c in env.calls()] == ["read-only", "read-only"]


def test_write_mode_applies_to_the_direct_at_codex_line_and_the_commit_gate_still_needs_both(hk):
    (hk.home / ".workforce" / "team.toml").write_text(f'codex = "{FAKE}"\ncodex_mode = "write"\n')
    hk.scenario([{"text": "ok"}])
    hk.submit("@codex edit it")
    assert sandbox_of(hk.calls()[0]["argv"]) == "workspace-write"
    # an edit voids approvals; a commit still needs both reviews
    approvals.record(hk.project, "claude", "APPROVE", "fine")
    (hk.project / "app.py").write_text("edited by codex\n")
    out = hooks.evaluate("pretooluse", {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(hk.project)}, env={}, home=hk.home, now=NOW)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_config_validates_mode_and_models(tmp_path):
    home = tmp_path
    assert config.load(home).codex_mode == "read-only"
    with pytest.raises(ConfigError):
        config.set_codex_mode("danger", home)
    for bad in ({"reviewer_effort": "ultra"}, {"reviewer_model": "has space"}, {"fast_coder_model": 'q"uote'}, {"fast_coder_model": "-x"}):
        with pytest.raises(ConfigError):
            config.set_team_models(**bad, home=home)
    assert config.load(home).reviewer_effort == "high"  # nothing half-written
    (home / ".workforce" / "team.toml").write_text('codex_mode = "yolo"\n')
    with pytest.raises(ConfigError):
        config.load(home)
    (home / ".workforce" / "team.toml").write_text('reviewer_effort = "ultra"\n')
    with pytest.raises(ConfigError):
        config.load(home)


# ------------------------------------------------------------------ /team-models and the agent files


def test_team_models_defaults_set_get_and_errors(env):
    client = env.start()
    got = json.loads(client.text("team_models"))
    assert (got["reviewer_model"], got["reviewer_effort"]) == ("claude-opus-5-5", "high")
    assert (got["fast_coder_model"], got["fast_coder_effort"]) == ("claude-sonnet-5-5", "high")
    done = json.loads(client.text("team_models", {"reviewer_model": "claude-opus-5-5[1m]", "reviewer_effort": "max", "fast_coder_effort": "low"}))
    assert (done["reviewer_model"], done["reviewer_effort"], done["fast_coder_effort"], done["fast_coder_model"]) == ("claude-opus-5-5[1m]", "max", "low", "claude-sonnet-5-5")
    saved = config.load(env.home)
    assert (saved.reviewer_model, saved.reviewer_effort, saved.fast_coder_effort) == ("claude-opus-5-5[1m]", "max", "low")
    assert "not valid" in client.error_text("team_models", {"reviewer_effort": "turbo"})
    assert "not a valid model" in client.error_text("team_models", {"fast_coder_model": "two words"})
    assert config.load(env.home).reviewer_effort == "max"


def frontmatter_of(path: Path) -> dict:
    match = re.match(r"---\n(.*?)\n---\n", path.read_text(), re.S)
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


def test_wf_regenerates_agent_files_from_team_toml_at_launch(launch_setup):
    _, home, _, calls = launch_setup
    agents = home / ".workforce" / "plugin" / "agents"
    config.set_team_models("claude-opus-5-5[1m]", "max", "claude-haiku-4-5-20251001", "low", home)
    assert launch_run(launch_setup, []) == 0
    coder = frontmatter_of(agents / "fast-coder.md")
    assert (coder["model"], coder["effort"]) == ("claude-haiku-4-5-20251001", "low")
    assert not (agents / "reviewer.md").exists()  # the Claude review is run by the MCP server, with reviewer_model/effort
    assert len(calls) == 1
    mtime = (agents / "fast-coder.md").stat().st_mtime_ns
    launch_run(launch_setup, [])
    assert (agents / "fast-coder.md").stat().st_mtime_ns == mtime
    config.set_team_models(fast_coder_model="claude-sonnet-5-5", home=home)
    launch_run(launch_setup, [])
    assert frontmatter_of(agents / "fast-coder.md")["model"] == "claude-sonnet-5-5"


def test_an_agent_file_of_an_earlier_version_is_removed_at_launch(launch_setup):
    _, home, _, _ = launch_setup
    stale = home / ".workforce" / "plugin" / "agents" / "reviewer.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("---\nname: reviewer\n---\nold reviewer that recorded its own verdict\n")
    assert launch_run(launch_setup, []) == 0
    assert not stale.exists()


def test_packaged_templates_render_with_the_defaults_and_no_placeholder_is_left():
    plugin = plugin_runtime.source_dir()
    cfg = config.TeamConfig("c", "x", "m", "high", 50, 60)
    files = agents_gen.rendered(plugin, cfg)
    assert set(files) == {f"agents/{name}.md" for name in agents_gen.ROLES}
    for name, text in files.items():
        assert "{{" not in text
        assert frontmatter_of_text(text)["model"] == config.DEFAULTS["fast_coder_model"]


def test_a_missing_template_renders_no_agent_file(tmp_path):
    assert agents_gen.rendered(tmp_path, config.TeamConfig("c", "x", "m", "high", 50, 60)) == {}


def frontmatter_of_text(text: str) -> dict:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


# ------------------------------------------------------------------ wf codex


def test_wf_codex_execs_codex_resume_with_the_saved_session_id(launch_setup, monkeypatch):
    root, home, _, calls = launch_setup
    project = root.parent / "myproj"
    relay.write_session(project, "thread-42", "gpt-6-sol", "high")
    monkeypatch.chdir(project)
    (home / ".workforce" / "team.toml").write_text(f'claude = "/x/claude"\ncodex = "{FAKE}"\n')
    assert launch_run(launch_setup, ["codex"]) == 0
    assert calls == [(str(FAKE), [str(FAKE), "resume", "thread-42"])]
    launch_run(launch_setup, ["codex", "--no-alt-screen"])
    assert calls[1][1] == [str(FAKE), "resume", "thread-42", "--no-alt-screen"]


def test_wf_codex_finds_the_session_from_a_subdirectory(launch_setup, monkeypatch):
    root, home, _, calls = launch_setup
    project = root.parent / "myproj"
    relay.write_session(project, "thread-7", "m", "high")
    (project / "src" / "deep").mkdir(parents=True)
    monkeypatch.chdir(project / "src" / "deep")
    (home / ".workforce" / "team.toml").write_text(f'codex = "{FAKE}"\n')
    launch_run(launch_setup, ["codex"])
    assert calls[0][1][-2:] == ["resume", "thread-7"]


def test_wf_codex_without_a_session_starts_a_new_codex_chat_on_the_team_model(launch_setup, monkeypatch, capsys):
    root, home, _, calls = launch_setup
    empty = root.parent / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    (home / ".workforce" / "team.toml").write_text(f'codex = "{FAKE}"\ncodex_model = "gpt-6-astra"\ncodex_effort = "xhigh"\n')
    assert launch_run(launch_setup, ["codex"]) == 0
    assert "starts a new Codex chat (gpt-6-astra · xhigh)" in capsys.readouterr().err
    assert calls[0][1][1:] == ["-m", "gpt-6-astra", "-c", "model_reasoning_effort=xhigh"]


def test_wf_codex_without_a_binary_is_a_clear_exit_3(launch_setup, monkeypatch, capsys):
    root, home, _, calls = launch_setup
    empty = root.parent / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    relay.write_session(empty, "thread-1", "m", "high")
    (home / ".workforce" / "team.toml").write_text('codex = "/nope/codex"\n')
    assert launch_run(launch_setup, ["codex"]) == 3
    assert "codex not found" in capsys.readouterr().err
    assert calls == []


def test_wf_codex_refuses_with_an_api_key(launch_setup, monkeypatch, capsys):
    root, home, _, calls = launch_setup
    relay.write_session(root.parent / "p", "t", "m", "high")
    monkeypatch.chdir(root.parent / "p")
    assert launch_run(launch_setup, ["codex"], environ={"OPENAI_API_KEY": "sk-x"}) == 3
    assert "OPENAI_API_KEY" in capsys.readouterr().err and calls == []


def test_wf_codex_argv_builder():
    assert launch.codex_argv("/c/codex", "abc") == ["/c/codex", "resume", "abc"]


def test_wf_with_a_task_that_starts_with_another_word_still_goes_to_claude(launch_setup):
    launch_run(launch_setup, ["codex is slow, fix it"])
    assert launch_setup[3][0][1][-1] == "codex is slow, fix it"


# ------------------------------------------------------------------ /wf-settings


def test_settings_shows_everything_with_the_command_to_change_each(env):
    config.set_team_models("claude-opus-5-5", "max", None, None, env.home)
    config.set_codex_mode("write", env.home)
    usage_cache.write_claude(
        {"five_hour": {"used_percentage": 23, "resets_at": 4_000_000_000}, "seven_day": {"used_percentage": 44, "resets_at": 4_000_100_000}}, env.home
    )
    usage_cache.write_codex([], env.home)
    client = env.start()
    text = client.text("settings")
    assert text.startswith("WorkForce settings")
    assert "gpt-6-sol · high · mode write" in text and "Codex may edit files" in text
    assert "/codex-model" in text and "/codex-effort" in text and "/codex-mode read-only|write" in text and "/model" in text
    assert "claude-opus-5-5 · max" in text and "claude-sonnet-5-5 · high" in text
    assert "/claude-model" in text and "/claude-effort" in text
    assert "alert at 50% · stop at 60%" in text and "alert_percent" in text and "/wf-continue" in text
    assert "23" in text and "44" in text  # current usage
    assert "/usage" in text


def test_settings_reports_read_only_mode(env):
    client = env.start()
    text = client.text("settings")
    assert "mode read-only (Codex only reads" in text
    assert "Usage now" in text


# ------------------------------------------------------------------ plugin files


def test_plugin_wires_the_new_commands_hook_timeout_and_verbatim_rule():
    plugin = plugin_runtime.source_dir()
    hooks_json = json.loads((plugin / "hooks" / "hooks.json").read_text())["hooks"]
    assert hooks_json["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 600
    for command, tool in (("codex-mode", "codex_settings"), ("claude-model", "team_models"), ("wf-settings", "settings")):
        assert f"mcp__plugin_workforce_codex__{tool}" in (plugin / "commands" / f"{command}.md").read_text()
    assert "exactly as returned" in (plugin / "commands" / "wf-settings.md").read_text()
    team = (plugin / "TEAM.md").read_text()
    assert "verbatim" in team and "Direct user↔Codex exchange" in team and "write mode" in team
