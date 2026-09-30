from collections import namedtuple

from workforce.agents import guard

Usage = namedtuple("Usage", "total used free")


def test_enough_space_is_fine(monkeypatch, tmp_path):
    monkeypatch.setattr(guard.shutil, "disk_usage", lambda p: Usage(10, 1, 5 * 1024**3))
    assert guard.check_disk(tmp_path) == []


def test_low_space_is_a_problem(monkeypatch, tmp_path):
    monkeypatch.setattr(guard.shutil, "disk_usage", lambda p: Usage(10, 9, int(0.4 * 1024**3)))
    problems = guard.check_disk(tmp_path)
    assert len(problems) == 1 and "0.4 GB free" in problems[0] and "at least 2 GB" in problems[0]
