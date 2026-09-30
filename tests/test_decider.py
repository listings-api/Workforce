import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from workforce.config import DeciderConfig
from workforce.decider import questions
from workforce.decider.base import KEYS, Decision, NullDecider, is_confident
from workforce.decider.fallback import UNSURE, FallbackDecider, settle
from workforce.decider import calibrate
from workforce.decider.laya import (
    MAX_STATE_CHARS,
    MODEL_STATE_CHARS,
    LayaDecider,
    build_decider,
    can_answer,
    confident,
    truncate_state,
)
from workforce.events import EventLog
from workforce.paths import Paths


class FakeOllaya(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode = "confident"
        self.choose = None
        self.confidence = 0.95
        self.models = ["laya:en"]
        self.requests: list[dict] = []
        self.model_gets = 0
        self.delay_s = 3.0
        self.reject_over: int | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status: int, body: dict | str):
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/v1/models":
            self.server.model_gets += 1
            if self.server.mode == "error":
                return self._send(500, {"error": "boom"})
            return self._send(200, {"models": [{"name": n, "description": "", "release_date": "2026-09-23"} for n in self.server.models]})
        self._send(200, "Ollaya is running")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        self.server.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        mode = self.server.mode
        if mode == "timeout":
            time.sleep(self.server.delay_s)
            return self._send(200, {})
        if mode == "error":
            return self._send(500, {"error": "boom", "code": "INTERNAL"})
        if mode == "unprocessable":
            return self._send(422, {"error": "state truncated", "code": "STATE_TRUNCATED"})
        if mode == "garbage":
            return self._send(200, "not json at all")
        if mode == "wrong_shape":
            return self._send(200, {"model": "laya:en", "answers": {}})
        if self.server.reject_over is not None and len(body["state"]) > self.server.reject_over:
            return self._send(422, {"error": "state: part of state was dropped", "code": "STATE_TRUNCATED"})
        criteria = body["questions"]["q"]["criteria"]
        labels = list(criteria)
        choice = "not-an-option" if mode == "bad_choice" else (self.server.choose or labels[0])
        confidence = 0.2 if mode == "unsure" else self.server.confidence
        probabilities = {label: (0.8 if label == choice else 0.2 / max(len(labels) - 1, 1)) for label in labels}
        self._send(
            200,
            {
                "model": "laya:en",
                "answers": {"q": {"type": "choice", "choice": choice, "confidence": confidence, "probabilities": probabilities}},
                "usage": {"input_tokens": 12, "output_tokens": 0},
            },
        )


@pytest.fixture
def server():
    fake = FakeOllaya()
    thread = threading.Thread(target=fake.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield fake
    fake.shutdown()
    fake.server_close()


@pytest.fixture
def events(tmp_path: Path) -> EventLog:
    return EventLog(Paths(tmp_path))


def make(server: FakeOllaya, events: EventLog, **kwargs) -> LayaDecider:
    return LayaDecider(server.url, 0.85, events, **kwargs)


def kinds(events: EventLog, kind: str) -> list[dict]:
    return [event for event in events.read()[0] if event["kind"] == kind]


def test_confident_choice_returns_laya_decision(server, events):
    server.choose = "stuck"
    decider = make(server, events)
    decision = decider.ask_choice("agent_progress", "log lines", "progressing?", ["progressing", "stuck", "off_task"])
    assert decision.source == "laya"
    assert decision.answer == "stuck"
    assert decision.confidence == 0.95
    assert decision.key == "agent_progress"
    assert decision.raw["choice"] == "stuck"
    assert is_confident(decision, 0.85)


def test_request_shape_is_typesafe_systemone(server, events):
    make(server, events).ask_choice("agent_progress", "the state", "Is it stuck?", ["progressing", "stuck", "off_task"])
    request = server.requests[0]
    assert request["path"] == "/v1/systemone"
    assert request["auth"] == "Bearer local"
    body = request["body"]
    assert body["model"] == "laya:en"
    assert body["state"] == "the state"
    question = body["questions"]["q"]
    assert question["type"] == "choice"
    assert question["instructions"] == "Is it stuck?"
    assert list(question["criteria"]) == ["progressing", "stuck", "off_task"]
    assert question["criteria"]["stuck"] == questions.OPTION_DESCRIPTIONS[("agent_progress", "stuck")]


def test_ask_bool_sends_yes_no_choice_and_returns_bool(server, events):
    decider = make(server, events)
    server.choose = "yes"
    yes = decider.ask_bool("risk_gate", "Tool: Bash", "risky?")
    server.choose = "no"
    no = decider.ask_bool("risk_gate", "Tool: Bash", "risky?")
    assert yes.answer is True and no.answer is False
    assert yes.source == no.source == "laya"
    sent = server.requests[0]["body"]["questions"]["q"]
    assert sent["type"] == "choice"
    assert list(sent["criteria"]) == ["yes", "no"]


def test_below_threshold_keeps_answer_and_confidence(server, events):
    server.mode = "unsure"
    server.choose = "user"
    decision = make(server, events).ask_choice("needs_human", "state", "needs the user?", ["agents", "user"])
    assert decision.source == "laya"
    assert decision.answer == "user"
    assert decision.confidence == 0.2
    assert not is_confident(decision, 0.85)
    assert is_confident(decision, 0.1)


def test_every_decision_is_logged(server, events):
    make(server, events).ask_bool("debate_converged", "s", "agree?")
    logged = kinds(events, "decision")
    assert len(logged) == 1
    assert logged[0]["key"] == "debate_converged"
    assert logged[0]["source"] == "laya"
    assert logged[0]["threshold"] == 0.85


@pytest.mark.parametrize("mode", ["error", "unprocessable", "garbage", "wrong_shape", "bad_choice"])
def test_bad_server_answers_fall_back_and_log_error(server, events, mode):
    server.mode = mode
    decision = make(server, events).ask_bool("risk_gate", "s", "risky?")
    assert decision.source == "fallback"
    assert decision.answer is True
    assert decision.confidence is None
    assert "error" in decision.raw
    assert not is_confident(decision, 0.0)
    assert len(kinds(events, "error")) == 1
    assert kinds(events, "decision")[0]["source"] == "fallback"


def test_timeout_falls_back_without_raising(server, events):
    server.mode = "timeout"
    server.delay_s = 1.5
    decider = make(server, events, timeout_s=0.2)
    started = time.monotonic()
    decision = decider.ask_choice("needs_human", "s", "human?", ["agents", "user"])
    assert time.monotonic() - started < 1.2
    assert decision.source == "fallback"
    assert decision.answer == "user"
    assert "timed out" in kinds(events, "error")[0]["message"]


def test_connection_refused_falls_back(events):
    decider = LayaDecider("http://127.0.0.1:1", 0.85, events, timeout_s=0.5)
    decision = decider.ask_choice("task_size", "s", "size?", ["small", "big"])
    assert decision.source == "fallback"
    assert decision.answer is None
    assert len(kinds(events, "error")) == 1


def test_event_log_failure_does_not_raise(server, tmp_path):
    class BrokenLog:
        def emit(self, kind, **data):
            raise OSError("disk full")

    decision = LayaDecider(server.url, 0.85, BrokenLog()).ask_bool("risk_gate", "s", "q")
    assert decision.source == "laya"


def test_available_is_cached_for_sixty_seconds(server, events):
    now = [1000.0]
    decider = make(server, events, clock=lambda: now[0])
    assert decider.available() is True
    assert decider.available() is True
    assert server.model_gets == 1
    now[0] += 59
    decider.available()
    assert server.model_gets == 1
    now[0] += 2
    decider.available()
    assert server.model_gets == 2


def test_available_reflects_server_state_after_recheck(server, events):
    now = [0.0]
    decider = make(server, events, clock=lambda: now[0])
    assert decider.available() is True
    server.mode = "error"
    assert decider.available() is True
    now[0] += 61
    assert decider.available() is False
    server.mode = "confident"
    now[0] += 61
    assert decider.available() is True


def test_available_false_when_model_not_pulled(server, events):
    server.models = ["laya:multilingual"]
    assert make(server, events).available() is False


def test_available_false_when_nothing_listens(events):
    assert LayaDecider("http://127.0.0.1:1", 0.85, events, timeout_s=0.3).available() is False


def test_truncate_state_passthrough_and_exact_limit():
    assert truncate_state("short") == ("short", 0)
    exact = "x" * MAX_STATE_CHARS
    assert truncate_state(exact) == (exact, 0)


def test_truncate_state_keeps_head_and_tail_within_limit():
    state = "HEAD" + "m" * 6000 + "TAIL"
    text, cut = truncate_state(state)
    assert len(text) <= MAX_STATE_CHARS
    assert text.startswith("HEAD")
    assert text.endswith("TAIL")
    assert "characters cut" in text
    marker = text[text.index("\n…[") : text.index("]…\n") + 3]
    assert len(text) - len(marker) + cut == len(state)


@pytest.mark.parametrize("size", [MAX_STATE_CHARS + 1, MAX_STATE_CHARS + 50, 4000, 100000])
def test_truncate_state_never_exceeds_limit(size):
    text, cut = truncate_state("a" * size)
    assert len(text) <= MAX_STATE_CHARS
    assert cut > 0


def test_long_state_is_truncated_on_the_wire_and_logged(server, events):
    state = "START-" + "z" * 5000 + "-END"
    make(server, events).ask_bool("verdict_consistent", state, "consistent?")
    sent = server.requests[0]["body"]["state"]
    assert len(sent) <= MAX_STATE_CHARS
    assert sent.startswith("START-") and sent.endswith("-END")
    notes = kinds(events, "note")
    assert len(notes) == 1
    assert "truncated" in notes[0]["message"]


def test_short_state_is_not_truncated_or_noted(server, events):
    make(server, events).ask_bool("verdict_consistent", "brief", "consistent?")
    assert server.requests[0]["body"]["state"] == "brief"
    assert kinds(events, "note") == []


@pytest.mark.parametrize("key", sorted(KEYS))
def test_fallback_decider_handles_every_key(key):
    decider = FallbackDecider()
    for decision in (decider.ask_bool(key, "s", "q"), decider.ask_choice(key, "s", "q", ["a", "b"])):
        assert decision.source == "fallback"
        assert decision.key == key
        assert decision.confidence is None
        assert decision.answer == UNSURE[key].answer
        assert decision.raw == {"action": UNSURE[key].action}
        assert not is_confident(decision, 0.0)


def test_fallback_table_matches_spec():
    assert FallbackDecider().decide("risk_gate").answer is True
    assert FallbackDecider().decide("agent_progress").raw["action"] == "flag_to_user"
    assert FallbackDecider().decide("debate_converged").answer is False
    assert FallbackDecider().decide("verdict_consistent").answer is False
    assert FallbackDecider().decide("needs_human").answer == "user"
    assert FallbackDecider().decide("task_size").answer is None
    assert set(UNSURE) == KEYS


def test_fallback_rejects_unknown_key():
    with pytest.raises(KeyError):
        FallbackDecider().decide("approve_code")
    assert FallbackDecider().available() is False


def test_settle_keeps_confident_laya_and_replaces_the_rest():
    confident = Decision("needs_human", "q", False, 0.99, "laya")
    unsure = Decision("needs_human", "q", False, 0.4, "laya")
    assert settle(confident, 0.85) is confident
    replaced = settle(unsure, 0.85)
    assert replaced.source == "fallback" and replaced.answer == "user"
    assert settle(NullDecider().ask_bool("needs_human", "s", "q"), 0.85).source == "fallback"


def test_build_decider_by_backend(events):
    laya = build_decider(DeciderConfig(backend="laya", url="http://127.0.0.1:11435", confidence=0.85), events)
    assert isinstance(laya, LayaDecider)
    assert isinstance(build_decider(DeciderConfig(backend="off", url="http://x", confidence=0.85), events), NullDecider)


def test_questions_cover_every_key_with_expected_shapes():
    builders = {
        "risk_gate": questions.risk_gate("Bash", {"command": "ls"}, "/wt"),
        "agent_progress": questions.agent_progress(["a", "b"], "T1"),
        "debate_converged": questions.debate_converged("x", "y"),
        "verdict_consistent": questions.verdict_consistent("APPROVE", [{"severity": "high", "file": "a.py", "line": 3, "message": "bug"}]),
        "needs_human": questions.needs_human("which db?", {"claude": "pg", "codex": "sqlite"}),
        "task_size": questions.task_size("Add CSV", "export", ["works"]),
    }
    assert set(builders) == KEYS
    for key, (state, question, options) in builders.items():
        assert state and question == questions.QUESTION_TEXT[key]
        assert options == questions.CHOICE_OPTIONS.get(key)


def test_agent_progress_keeps_last_forty_lines_and_clips_long_ones():
    lines = [f"line {i}" for i in range(100)] + ["x" * 1000]
    state, _, options = questions.agent_progress(lines)
    body = state.splitlines()[1:]
    assert len(body) == 40
    assert body[0] == "line 61"
    assert len(body[-1]) <= questions.LOG_LINE_CHARS
    assert options == ["progressing", "stuck", "off_task"]


def test_risk_gate_state_clips_huge_inputs():
    state, _, _ = questions.risk_gate("Write", {"file_path": "a.py", "content": "y" * 50000}, "/wt")
    assert len(state) < 600
    assert "Worktree: /wt" in state


def test_verdict_state_lists_findings_and_none_case():
    with_findings, _, _ = questions.verdict_consistent("APPROVE", [{"severity": "high", "file": "a.py", "line": 3, "message": "crash"}])
    assert "Verdict: APPROVE" in with_findings and "a.py:3" in with_findings and "crash" in with_findings
    assert "Findings: none" in questions.verdict_consistent("APPROVE", [])[0]


@pytest.mark.laya
@pytest.mark.skipif(os.environ.get("WF_LAYA_LIVE") != "1", reason="set WF_LAYA_LIVE=1 with Ollaya serving laya:en on 127.0.0.1:11435")
def test_live_laya_answers_a_real_boolean(events):
    decider = LayaDecider("http://127.0.0.1:11435", 0.85, events)
    assert decider.available() is True
    state, question, _ = questions.risk_gate("Bash", {"command": "rm -rf ~/Documents"}, "/work/app")
    decision = decider.ask_bool("risk_gate", state, question)
    assert decision.source == "laya"
    assert isinstance(decision.answer, bool)
    assert 0.0 <= decision.confidence <= 1.0


def test_thresholds_are_per_key_and_recorded_on_the_decision(server, events):
    server.confidence = 0.6
    server.choose = "yes"
    decider = make(server, events, thresholds={"debate_converged": 0.55, "needs_human": 0.9})
    converged = decider.ask_bool("debate_converged", "s", "q")
    server.choose = "user"
    human = decider.ask_choice("needs_human", "s", "q", ["agents", "user"])
    assert converged.raw["threshold"] == 0.55 and human.raw["threshold"] == 0.9
    assert confident(converged, 0.99) is True
    assert confident(human, 0.1) is False
    logged = {event["key"]: event for event in kinds(events, "decision")}
    assert logged["debate_converged"]["threshold"] == 0.55
    assert logged["needs_human"]["threshold"] == 0.9


def test_key_missing_from_thresholds_is_unreliable_and_never_calls_laya(server, events):
    decider = make(server, events, thresholds={"task_size": 0.5})
    decision = decider.ask_bool("risk_gate", "s", "q")
    assert server.requests == []
    assert decision.source == "fallback" and decision.answer is True
    assert decision.raw["unreliable"] is True
    assert not confident(decision, 0.0)
    assert kinds(events, "decision")[0]["source"] == "fallback"
    assert kinds(events, "error") == []


def test_no_thresholds_table_means_every_key_uses_the_global_confidence(server, events):
    decider = make(server, events)
    assert all(decider.threshold_for(key) == 0.85 for key in KEYS)
    assert decider.ask_bool("risk_gate", "s", "q").source == "laya"


def test_empty_thresholds_table_makes_every_key_unreliable(server, events):
    decider = make(server, events, thresholds={})
    assert all(decider.threshold_for(key) is None for key in KEYS)
    assert decider.ask_choice("task_size", "s", "q", ["small", "big"]).source == "fallback"
    assert server.requests == []


def test_models_are_per_key(server, events):
    server.models = ["laya:en", "laya:typed-decisions"]
    decider = make(server, events, models={"debate_converged": "laya:typed-decisions"})
    decider.ask_bool("debate_converged", "s", "q")
    decider.ask_bool("needs_human", "s", "q")
    assert [request["body"]["model"] for request in server.requests] == ["laya:typed-decisions", "laya:en"]
    assert kinds(events, "decision")[0]["model"] == "laya:typed-decisions"


def test_available_requires_every_model_in_use(server, events):
    decider = make(server, events, models={"debate_converged": "laya:typed-decisions"})
    assert decider.available() is False
    server.models = ["laya:en", "laya:typed-decisions"]
    assert make(server, events, models={"debate_converged": "laya:typed-decisions"}).available() is True


@pytest.mark.parametrize(
    "kwargs",
    [{"thresholds": {"nonsense": 0.5}}, {"models": {"nonsense": "laya:en"}}, {"thresholds": {"risk_gate": 0}}, {"thresholds": {"risk_gate": 1.5}}],
)
def test_bad_threshold_or_model_tables_are_rejected(server, events, kwargs):
    with pytest.raises(ValueError):
        make(server, events, **kwargs)


def test_can_answer_skips_unreliable_keys_and_unavailable_servers(server, events):
    reliable = make(server, events, thresholds={"task_size": 0.5})
    assert can_answer(reliable, "task_size") is True
    assert can_answer(reliable, "verdict_consistent") is False
    assert can_answer(make(server, events), "verdict_consistent") is True
    assert can_answer(NullDecider(), "task_size") is False
    server.mode = "error"
    assert can_answer(make(server, events), "task_size") is False


def test_confident_falls_back_to_the_default_threshold_without_a_recorded_one():
    assert confident(Decision("task_size", "q", "big", 0.9, "laya"), 0.85) is True
    assert confident(Decision("task_size", "q", "big", 0.5, "laya"), 0.85) is False
    assert confident(Decision("task_size", "q", "big", 0.9, "fallback"), 0.1) is False


def test_build_decider_reads_optional_tables(events):
    cfg = DeciderConfig(backend="laya", url="http://127.0.0.1:11435", confidence=0.85)
    plain = build_decider(cfg, events)
    assert plain.thresholds is None and plain.models == {}

    class WithTables:
        backend = "laya"
        url = "http://127.0.0.1:11435"
        confidence = 0.85
        thresholds = {"task_size": 0.5}
        models = {"task_size": "laya:typed-decisions"}

    tuned = build_decider(WithTables(), events)
    assert tuned.thresholds == {"task_size": 0.5}
    assert tuned.model_for("task_size") == "laya:typed-decisions"


def test_512_token_model_gets_a_shorter_state_and_1024_model_the_default(server, events):
    server.models = ["laya:en", "laya:typed-decisions"]
    decider = make(server, events, models={"debate_converged": "laya:typed-decisions"})
    long_state = "s" * 5000
    decider.ask_bool("risk_gate", long_state, "q")
    decider.ask_bool("debate_converged", long_state, "q")
    en_state, td_state = (request["body"]["state"] for request in server.requests)
    assert len(en_state) <= MODEL_STATE_CHARS["laya:en"]
    assert MODEL_STATE_CHARS["laya:en"] < len(td_state) <= MAX_STATE_CHARS


def test_state_still_too_long_for_the_model_is_retried_shorter(server, events):
    server.reject_over = 400
    decision = make(server, events).ask_bool("risk_gate", "x" * 1400, "q")
    assert decision.source == "laya"
    lengths = [len(request["body"]["state"]) for request in server.requests]
    assert len(lengths) >= 3 and lengths == sorted(lengths, reverse=True) and lengths[-1] <= 400
    assert kinds(events, "note")


def test_state_too_long_forever_falls_back_after_bounded_retries(server, events):
    server.reject_over = 0
    decision = make(server, events).ask_bool("risk_gate", "x" * 1400, "q")
    assert decision.source == "fallback"
    assert len(server.requests) == 3
    assert len(kinds(events, "error")) == 1


PROBE_KEYS = sorted(KEYS)


@pytest.mark.parametrize("key", PROBE_KEYS)
def test_probe_files_are_well_formed_balanced_and_buildable(key):
    probes = calibrate.load_probes(key)
    assert len(probes) == 12
    labels = [probe["label"] for probe in probes]
    counts = {label: labels.count(label) for label in set(labels)}
    assert len(set(counts.values())) == 1, counts
    if calibrate.is_bool_key(key):
        assert set(labels) == {True, False}
    else:
        assert set(labels) == set(questions.CHOICE_OPTIONS[key])
    assert any("hard" in probe.get("note", "") for probe in probes)
    for probe in probes:
        state, question, options = questions.BUILDERS[key](**probe["args"])
        assert state and question
        assert len(state) <= MODEL_STATE_CHARS["laya:en"], "probe states must fit the smallest context untruncated"
        assert options == questions.CHOICE_OPTIONS.get(key)


def test_all_six_keys_have_probes():
    assert {path.stem for path in calibrate.PROBE_DIR.glob("*.jsonl")} == KEYS


def test_load_probes_rejects_bad_lines_and_unknown_keys(tmp_path):
    (tmp_path / "task_size.jsonl").write_text('{"args": {}}\n')
    with pytest.raises(ValueError, match="not a probe line"):
        calibrate.load_probes("task_size", tmp_path)
    (tmp_path / "risk_gate.jsonl").write_text("")
    with pytest.raises(ValueError, match="no probes"):
        calibrate.load_probes("risk_gate", tmp_path)
    with pytest.raises(ValueError, match="unknown"):
        calibrate.load_probes("approve_code", tmp_path)


def R(label, answer, confidence, error=None):
    return calibrate.ProbeResult(label, answer, confidence, error)


def test_analyse_finds_the_lowest_threshold_with_ninety_percent_precision():
    results = [R(True, True, 0.9), R(True, True, 0.8), R(False, False, 0.7), R(True, True, 0.6), R(True, True, 0.5),
               R(False, True, 0.3), R(True, False, 0.2), R(False, False, 0.1)]
    report = calibrate.analyse("debate_converged", results)
    assert report.n == 8 and report.errors == 0
    assert report.accuracy == pytest.approx(6 / 8)
    assert report.threshold == 0.5
    assert report.kept == 5 and report.precision == 1.0
    assert report.coverage == pytest.approx(5 / 8)
    assert report.mean_conf_right == pytest.approx((0.9 + 0.8 + 0.7 + 0.6 + 0.5 + 0.1) / 6)
    assert report.mean_conf_wrong == pytest.approx((0.3 + 0.2) / 2)
    assert report.chance == 0.625


def test_analyse_marks_a_key_unreliable_when_no_threshold_reaches_the_target():
    results = [R(True, False, 0.9), R(True, True, 0.8), R(False, False, 0.7), R(False, True, 0.6), R(True, True, 0.5), R(False, False, 0.4)]
    report = calibrate.analyse("risk_gate", results)
    assert report.threshold is None and report.kept == 0 and report.precision is None
    assert report.coverage == pytest.approx(0 / 6)


def test_analyse_ignores_thresholds_that_keep_too_few_answers():
    results = [R(True, True, 0.95), R(True, True, 0.9), R(False, True, 0.4), R(False, False, 0.3), R(True, False, 0.2), R(False, True, 0.1)]
    assert calibrate.analyse("risk_gate", results).threshold is None
    assert calibrate.analyse("risk_gate", results, min_kept=2).threshold == 0.9


def test_analyse_excludes_errors_from_accuracy():
    results = [R(True, True, 0.9), R(True, True, 0.8), R(True, True, 0.7), R(True, True, 0.6), R(True, None, None, "timeout")]
    report = calibrate.analyse("task_size", results)
    assert report.errors == 1 and report.accuracy == 1.0 and report.threshold == 0.6


def test_run_probes_records_errors_from_a_failing_server(server, events):
    server.mode = "error"
    probes = calibrate.load_probes("task_size")
    results = calibrate.run_probes(make(server, events), "task_size", probes)
    assert all(result.error for result in results)
    assert calibrate.analyse("task_size", results).accuracy is None


def _calibrate_main(*args):
    import io

    out, err = io.StringIO(), io.StringIO()
    return calibrate.main(list(args), stdout=out, stderr=err), out.getvalue(), err.getvalue()


def test_calibrate_cli_prints_a_table_for_every_key(server, tmp_path):
    server.confidence = 0.95
    code, out, err = _calibrate_main("--url", server.url)
    assert code == 0, err
    assert out.startswith("model laya:en")
    for key in KEYS:
        assert key in out
    assert "threshold" in out and "unreliable" not in out.split("threshold", 1)[0]
    assert len(server.requests) == 72


def test_calibrate_cli_json_and_single_key_and_model(server):
    server.models = ["laya:typed-decisions"]
    code, out, _ = _calibrate_main("--url", server.url, "--model", "laya:typed-decisions", "--key", "task_size", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["model"] == "laya:typed-decisions"
    assert [entry["key"] for entry in payload["keys"]] == ["task_size"]
    assert all(request["body"]["model"] == "laya:typed-decisions" for request in server.requests)


def test_calibrate_cli_details_lists_each_probe(server):
    code, out, _ = _calibrate_main("--url", server.url, "--key", "risk_gate", "--details")
    assert code == 0 and out.count("conf=") == 12


def test_calibrate_cli_reports_a_missing_server_and_missing_url(server, tmp_path, monkeypatch):
    code, _, err = _calibrate_main("--url", "http://127.0.0.1:1")
    assert code == 1 and "not serving" in err
    server.models = ["laya:multilingual"]
    code, _, err = _calibrate_main("--url", server.url)
    assert code == 1 and "ollama pull" not in err and "laya:en" in err
    monkeypatch.chdir(tmp_path)
    code, _, err = _calibrate_main()
    assert code == 2 and "workforce.toml" in err


def test_calibrate_cli_takes_the_url_from_workforce_toml(server, tmp_path, monkeypatch):
    from workforce import config as wf_config

    (tmp_path / "workforce.toml").write_text(wf_config.default_toml().replace("http://127.0.0.1:11435", server.url))
    monkeypatch.chdir(tmp_path)
    code, out, _ = _calibrate_main("--key", "task_size")
    assert code == 0 and "task_size" in out
