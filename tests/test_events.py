import json
import threading
from pathlib import Path

import pytest

from workforce import events as events_mod
from workforce.events import KINDS, EventLog
from workforce.paths import Paths


@pytest.fixture
def log(tmp_path: Path) -> EventLog:
    return EventLog(Paths(tmp_path))


def test_emit_writes_json_line_with_ts_and_fields(log: EventLog):
    event = log.emit("task_status", run_id="r1", task_id="T1", status="coding")
    lines = log.paths.events.read_text().splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored == event
    assert stored["kind"] == "task_status"
    assert stored["run_id"] == "r1" and stored["task_id"] == "T1" and stored["status"] == "coding"
    assert stored["ts"].endswith("+00:00")


def test_emit_without_optional_ids(log: EventLog):
    event = log.emit("note", text="hello")
    assert "run_id" not in event and "task_id" not in event


def test_unknown_kind_rejected_and_nothing_written(log: EventLog):
    with pytest.raises(ValueError, match="unknown event kind 'bogus'"):
        log.emit("bogus")
    assert not log.paths.events.exists()


def test_ts_is_reserved(log: EventLog):
    with pytest.raises(ValueError, match="reserved"):
        log.emit("note", ts="yesterday")


def test_all_spec_kinds_present():
    expected = {
        "run_started", "plan_ready", "debate_round", "question", "answered", "task_status",
        "agent_event", "check_result", "review", "commit_waiting", "committed", "merged",
        "alert", "paused", "resumed", "decision", "note", "error", "run_done",
    }
    assert KINDS == frozenset(expected)


def test_non_json_values_are_stringified(log: EventLog, tmp_path: Path):
    log.emit("note", path=tmp_path)
    assert log.read()[0][0]["path"] == str(tmp_path)


def test_read_missing_file(log: EventLog):
    assert log.read() == ([], 0)
    assert log.read(17) == ([], 17)


def test_tailing_across_offsets(log: EventLog):
    log.emit("note", n=1)
    log.emit("note", n=2)
    first, offset = log.read()
    assert [e["n"] for e in first] == [1, 2]
    assert offset == log.paths.events.stat().st_size

    assert log.read(offset) == ([], offset)

    log.emit("note", n=3)
    second, offset2 = log.read(offset)
    assert [e["n"] for e in second] == [3]
    assert offset2 == log.paths.events.stat().st_size > offset

    log.emit("note", n=4)
    log.emit("note", n=5)
    third, offset3 = log.read(offset2)
    assert [e["n"] for e in third] == [4, 5]

    everything, _ = log.read(0)
    assert [e["n"] for e in everything] == [1, 2, 3, 4, 5]
    assert log.read(offset3) == ([], offset3)


def test_partial_trailing_line_is_not_consumed(log: EventLog):
    log.emit("note", n=1)
    _, offset = log.read()
    with open(log.paths.events, "ab") as handle:
        handle.write(b'{"ts": "x", "kind": "note", "n"')
    events, same = log.read(offset)
    assert events == [] and same == offset

    with open(log.paths.events, "ab") as handle:
        handle.write(b": 2}\n")
    events, new_offset = log.read(offset)
    assert [e["n"] for e in events] == [2]
    assert new_offset == log.paths.events.stat().st_size


def test_malformed_complete_line_is_skipped(log: EventLog):
    log.emit("note", n=1)
    with open(log.paths.events, "ab") as handle:
        handle.write(b"not json\n")
    log.emit("note", n=2)
    events, _ = log.read()
    assert [e["n"] for e in events] == [1, 2]


def test_offset_past_end_restarts_from_beginning(log: EventLog):
    log.emit("note", n=1)
    events, offset = log.read(10_000)
    assert [e["n"] for e in events] == [1]
    assert offset == log.paths.events.stat().st_size


def test_multibyte_offsets_are_bytes(log: EventLog):
    log.emit("note", text="héllo ✓")
    _, offset = log.read()
    log.emit("note", text="next")
    events, _ = log.read(offset)
    assert [e["text"] for e in events] == ["next"]


def test_subscribers_receive_events_synchronously_in_order(log: EventLog):
    seen: list[dict] = []
    log.subscribe(seen.append)
    log.emit("note", n=1)
    assert [e["n"] for e in seen] == [1]
    log.emit("error", message="bad")
    assert [e["kind"] for e in seen] == ["note", "error"]


def test_unsubscribe(log: EventLog):
    seen: list[dict] = []
    off = log.subscribe(seen.append)
    log.emit("note", n=1)
    off()
    off()
    log.emit("note", n=2)
    assert [e["n"] for e in seen] == [1]


def test_failing_subscriber_does_not_break_emit_or_others(log: EventLog, caplog):
    seen: list[dict] = []

    def bad(event):
        raise RuntimeError("ui exploded")

    log.subscribe(bad)
    log.subscribe(seen.append)
    with caplog.at_level("ERROR", logger=events_mod.logger.name):
        log.emit("note", n=1)
    assert len(seen) == 1
    assert "ui exploded" in caplog.text
    assert len(log.read()[0]) == 1


def test_subscriber_may_emit_without_deadlock(log: EventLog):
    def echo(event):
        if event["kind"] == "note":
            log.emit("decision", source="echo")

    log.subscribe(echo)
    log.emit("note", n=1)
    assert [e["kind"] for e in log.read()[0]] == ["note", "decision"]


def test_concurrent_emits_produce_intact_lines(log: EventLog):
    def worker(w: int):
        for i in range(25):
            log.emit("agent_event", worker=w, i=i, pad="x" * 200)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events, _ = log.read()
    assert len(events) == 150
    assert len({(e["worker"], e["i"]) for e in events}) == 150
