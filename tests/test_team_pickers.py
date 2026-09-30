"""Model and effort menus (/codex-model, /codex-effort, /claude-model, /claude-effort), the live Codex model list, the banner label."""

import io
import json
import os

import pytest

from workforce.team import claude_info, codex_models, config, launch, picker
from tests.test_team_launch import run as launch_run, setup as launch_setup  # noqa: F401  (fixture)
from tests.test_team_mcp import env  # noqa: F401  (fixture)

MODEL_LIST = [
    {"id": "gpt-6-astra", "displayName": "GPT-6-Astra", "description": "Frontier model", "hidden": False, "isDefault": True,
     "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
    {"id": "gpt-6-sol", "displayName": "GPT-6-Sol", "description": "Everyday model", "hidden": False, "isDefault": False,
     "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
    {"id": "gpt-6-luna", "displayName": "GPT-6-Luna", "description": "Small, fast", "hidden": False, "isDefault": False,
     "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh", "max")]},
    {"id": "gpt-5.6-terra", "displayName": "GPT-5.6-Terra", "description": "", "hidden": False, "isDefault": False,
     "defaultReasoningEffort": "medium", "supportedReasoningEfforts": ["low", "medium", "high"]},
    {"id": "gpt-5.5", "displayName": "GPT-5.5", "description": "", "hidden": False, "isDefault": False,
     "defaultReasoningEffort": "xhigh", "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}]},
    {"id": "secret-model", "hidden": True, "supportedReasoningEfforts": []},
]


@pytest.fixture
def cfg(tmp_path):
    return config.load(tmp_path / "wf")


@pytest.mark.parametrize(
    "model, name",
    [
        ("claude-opus-5-5", "Opus 5.5"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        ("claude-sonnet-5", "Sonnet 5"),
        ("claude-fable-5-1", "Fable 5.1"),
        ("opus", "Opus"),
        ("sonnet[1m]", "Sonnet"),
        ("some-gateway-model", "some-gateway-model"),
    ],
)
def test_display_name(model, name):
    assert claude_info.display_name(model) == name


def write_settings(home, data):
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(data))


def test_banner_label_prefers_the_model_argument_then_settings_then_what_was_last_seen(tmp_path):
    home = tmp_path / "home"
    write_settings(home, {"effortLevel": "high", "modelSettings": {"claude-sonnet-5-5": {"effortLevel": "low"}}})
    assert claude_info.banner_label([], {}, home=home, wf_home=home) is None
    claude_info.record_seen({"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, "effort": {"level": "max"}}, home)
    assert claude_info.banner_label([], {}, home=home, wf_home=home) == "Opus 5.5 · high"
    write_settings(home, {"model": "claude-sonnet-5-5", "modelSettings": {"claude-sonnet-5-5": {"effortLevel": "low"}}})
    assert claude_info.banner_label([], {}, home=home, wf_home=home) == "Sonnet 5.5 · low"
    assert claude_info.banner_label(["--model", "claude-fable-5-1", "--effort", "xhigh"], {}, home=home, wf_home=home) == "Fable 5.1 · xhigh"
    assert claude_info.banner_label(["--model=opus"], {}, home=home, wf_home=home) == "Opus"


def test_the_banner_shows_the_claude_model(launch_setup, tmp_path):
    root, home, _, calls = launch_setup
    write_settings(home, {"model": "claude-opus-5-5", "effortLevel": "high"})

    class Tty(io.StringIO):
        def isatty(self):
            return True

    out = Tty()
    launch_run(launch_setup, [], environ={"NO_COLOR": "1"}, stdout=out)
    assert "Claude Opus 5.5 · high  │  Codex gpt-6-sol · high" in out.getvalue()


def test_parse_skips_hidden_models_and_reads_efforts_in_both_shapes():
    models = codex_models.parse({"data": MODEL_LIST})
    assert [m.id for m in models] == ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-terra", "gpt-5.5"]
    assert models[3].efforts == ("low", "medium", "high") and models[2].efforts[-1] == "max"
    assert codex_models.parse({"data": "nope"}) == [] and codex_models.parse(None) == []


def test_the_model_list_is_cached_for_an_hour_and_the_last_list_survives_a_failed_read(tmp_path, monkeypatch):
    reads = []
    monkeypatch.setattr(codex_models, "fetch", lambda binary: reads.append(1) or codex_models.parse({"data": MODEL_LIST}))
    models, problem = codex_models.load("codex", tmp_path, now=1000.0)
    assert problem is None and len(models) == 5 and len(reads) == 1
    codex_models.load("codex", tmp_path, now=1000.0 + codex_models.MAX_AGE_S - 1)
    assert len(reads) == 1

    def broken(binary):
        raise RuntimeError("app-server down")

    monkeypatch.setattr(codex_models, "fetch", broken)
    models, problem = codex_models.load("codex", tmp_path, now=1000.0 + codex_models.MAX_AGE_S + 1)
    assert len(models) == 5 and "app-server down" in problem


def check_question(question):
    assert 2 <= len(question["options"]) <= picker.MAX_OPTIONS
    assert len(question["header"]) <= 12 and question["multiSelect"] is False
    assert all(option["label"] and option["description"] for option in question["options"])


def test_codex_model_menu_puts_the_current_model_first_and_names_the_rest(cfg):
    models = codex_models.parse({"data": MODEL_LIST})
    result = picker.build("codex_model", cfg, models)
    model_q, effort_q = result["questions"]
    for question in result["questions"]:
        check_question(question)
    assert [o["label"] for o in model_q["options"]] == ["gpt-6-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-terra"]
    assert model_q["options"][0]["description"].startswith("Current.")
    assert "gpt-5.5" in model_q["question"]
    assert [o["label"] for o in effort_q["options"]] == ["medium", "high", "xhigh", "max"]
    assert "low" in effort_q["question"] and "ultra" in effort_q["question"]
    assert "codex_settings" in result["then"]


def test_menus_without_a_live_model_list_still_offer_the_saved_model(cfg):
    result = picker.build("codex_model", cfg, [], "could not read the Codex model list")
    labels = [o["label"] for o in result["questions"][0]["options"]]
    assert labels == ["gpt-6-sol"] or labels[0] == "gpt-6-sol"
    assert result["note"].startswith("could not read")


@pytest.mark.parametrize("kind", ["claude_model", "claude_effort"])
def test_claude_menus_ask_which_team_claude_and_mark_who_uses_what(cfg, kind):
    result = picker.build(kind, cfg, [])
    for question in result["questions"]:
        check_question(question)
    assert [o["label"] for o in result["questions"][0]["options"]] == ["Reviewer", "Fast coder", "Both"]
    assert "/model" in result["questions"][0]["question"]
    descriptions = {o["label"]: o["description"] for q in result["questions"][1:] for o in q["options"]}
    if kind == "claude_model":
        assert descriptions["claude-opus-5-5"].startswith("Now: reviewer")
        assert descriptions["claude-sonnet-5-5"].startswith("Now: fast coder")
    else:
        assert descriptions["high"].startswith("Now: reviewer and fast coder")
    assert "team_models" in result["then"]


def test_an_unknown_picker_kind_is_refused(cfg):
    with pytest.raises(ValueError):
        picker.build("nope", cfg, [])


def test_the_picker_tool_uses_the_live_codex_list(env):
    env.scenario(codex_models=MODEL_LIST)
    client = env.start()
    body = json.loads(client.text("picker", {"kind": "codex_model"}))
    assert body["questions"][0]["options"][1]["label"] == "gpt-6-astra"
    assert "/model" in body["main_chat"]


def test_codex_settings_refuses_a_model_codex_does_not_list_or_an_unsupported_effort(env):
    env.scenario(codex_models=MODEL_LIST)
    client = env.start()
    bad_model = client.call("codex_settings", {"model": "gpt-9"})
    assert bad_model.get("isError") and "gpt-6-astra" in bad_model["content"][0]["text"]
    client.text("codex_settings", {"effort": "ultra"})
    bad_effort = client.call("codex_settings", {"model": "gpt-6-luna"})
    assert bad_effort.get("isError") and "does not support effort 'ultra'" in bad_effort["content"][0]["text"]
    body = json.loads(client.text("codex_settings", {"model": "gpt-6-luna", "effort": "max"}))
    assert body["codex_model"] == "gpt-6-luna" and body["codex_effort"] == "max"
