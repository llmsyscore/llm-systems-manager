import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_bench_replay", Path(__file__).resolve().parents[1] / "backend" / "_bench_replay.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
BenchReplayBuffer = _mod.BenchReplayBuffer


def _ids(records):
    return [r["id"] for r in records]


def test_append_assigns_monotonic_ids_scoped_to_run():
    b = BenchReplayBuffer()
    b.start_run("run1")
    r1 = b.append({"type": "line", "text": "a"})
    r2 = b.append({"type": "line", "text": "b"})
    assert r1["id"] == "run1:1"
    assert r2["id"] == "run1:2"
    assert r1["event"] == {"type": "line", "text": "a"}


def test_start_run_resets_seq_and_clears_buffer():
    b = BenchReplayBuffer()
    b.start_run("run1")
    b.append({"type": "line"})
    b.start_run("run2")
    assert b.run_id == "run2"
    r = b.append({"type": "line"})
    assert r["id"] == "run2:1"
    assert len(b.replay_after(None)) == 1


def test_replay_after_none_returns_whole_buffer():
    b = BenchReplayBuffer()
    b.start_run("run1")
    b.append({"n": 1}); b.append({"n": 2}); b.append({"n": 3})
    assert _ids(b.replay_after(None)) == ["run1:1", "run1:2", "run1:3"]
    assert _ids(b.replay_after("")) == ["run1:1", "run1:2", "run1:3"]


def test_replay_after_returns_events_after_seq_same_run():
    b = BenchReplayBuffer()
    b.start_run("run1")
    for n in range(1, 5):
        b.append({"n": n})
    assert _ids(b.replay_after("run1:2")) == ["run1:3", "run1:4"]


def test_replay_after_beyond_end_returns_empty():
    b = BenchReplayBuffer()
    b.start_run("run1")
    b.append({"n": 1})
    assert b.replay_after("run1:5") == []


def test_replay_after_run_mismatch_returns_whole_buffer():
    b = BenchReplayBuffer()
    b.start_run("run2")
    b.append({"n": 1}); b.append({"n": 2})
    # client's last id is from a previous run -> replay everything current
    assert _ids(b.replay_after("run1:1")) == ["run2:1", "run2:2"]


def test_replay_after_malformed_id_returns_whole_buffer():
    b = BenchReplayBuffer()
    b.start_run("run1")
    b.append({"n": 1})
    assert _ids(b.replay_after("garbage")) == ["run1:1"]
    assert _ids(b.replay_after("run1:notanumber")) == ["run1:1"]


def test_buffer_is_bounded_and_evicts_oldest():
    b = BenchReplayBuffer(maxlen=3)
    b.start_run("run1")
    for n in range(1, 6):     # 5 appends, maxlen 3
        b.append({"n": n})
    assert _ids(b.replay_after(None)) == ["run1:3", "run1:4", "run1:5"]


def test_buffer_retained_after_done_until_next_run():
    b = BenchReplayBuffer()
    b.start_run("run1")
    b.append({"type": "line"})
    b.append({"type": "done", "ok": True})
    # a client reconnecting right at the end still replays the done event
    assert _ids(b.replay_after("run1:1")) == ["run1:2"]
    assert b.replay_after(None)[-1]["event"]["type"] == "done"


def test_seq_for_resolves_resume_point():
    b = BenchReplayBuffer()
    b.start_run("run1")
    assert b.seq_for(None) == 0
    assert b.seq_for("run1:3") == 3
    assert b.seq_for("other:3") == 0      # run mismatch -> from start
    assert b.seq_for("garbage") == 0       # no separator -> from start
    assert b.seq_for("run1:x") == 0        # malformed seq -> from start


def test_records_after_seq_is_the_live_primitive():
    b = BenchReplayBuffer()
    b.start_run("run1")
    for n in range(1, 4):
        b.append({"n": n})
    assert _ids(b.records_after_seq(0)) == ["run1:1", "run1:2", "run1:3"]
    assert _ids(b.records_after_seq(2)) == ["run1:3"]
    assert b.records_after_seq(3) == []
