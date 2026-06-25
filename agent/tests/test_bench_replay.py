"""The agent copy of _bench_replay must behave identically to the manager's
(both back the SSE Last-Event-ID resume). Mirror of the manager test."""
import _bench_replay

BenchReplayBuffer = _bench_replay.BenchReplayBuffer


def _ids(records):
    return [r["id"] for r in records]


def test_append_assigns_monotonic_ids_scoped_to_run():
    b = BenchReplayBuffer()
    b.start_run("run1")
    assert b.append({"type": "line"})["id"] == "run1:1"
    assert b.append({"type": "line"})["id"] == "run1:2"


def test_start_run_resets_and_clears():
    b = BenchReplayBuffer()
    b.start_run("run1")
    b.append({"n": 1})
    b.start_run("run2")
    assert b.run_id == "run2"
    assert b.append({"n": 1})["id"] == "run2:1"
    assert len(b.replay_after(None)) == 1


def test_replay_after_same_run_returns_tail():
    b = BenchReplayBuffer()
    b.start_run("run1")
    for n in range(1, 5):
        b.append({"n": n})
    assert _ids(b.replay_after("run1:2")) == ["run1:3", "run1:4"]


def test_replay_after_run_mismatch_or_malformed_returns_all():
    b = BenchReplayBuffer()
    b.start_run("run2")
    b.append({"n": 1})
    assert _ids(b.replay_after("run1:1")) == ["run2:1"]
    assert _ids(b.replay_after("garbage")) == ["run2:1"]


def test_buffer_bounded():
    b = BenchReplayBuffer(maxlen=2)
    b.start_run("r")
    for n in range(3):
        b.append({"n": n})
    assert _ids(b.replay_after(None)) == ["r:2", "r:3"]


def test_buffer_retained_after_done():
    b = BenchReplayBuffer()
    b.start_run("run1")
    b.append({"type": "line"})
    b.append({"type": "done"})
    assert _ids(b.replay_after("run1:1")) == ["run1:2"]


def test_seq_for_and_records_after_seq():
    b = BenchReplayBuffer()
    b.start_run("run1")
    for n in range(1, 4):
        b.append({"n": n})
    assert b.seq_for(None) == 0
    assert b.seq_for("run1:2") == 2
    assert b.seq_for("other:2") == 0
    assert _ids(b.records_after_seq(2)) == ["run1:3"]
