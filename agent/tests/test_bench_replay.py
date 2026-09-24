"""The agent copy of _bench_replay must behave identically to the manager's
(both back the SSE Last-Event-ID resume). Mirror of the manager test."""
import _bench_replay

BenchReplayBuffer = _bench_replay.BenchReplayBuffer


def _ids(records):
    return [r["id"] for r in records]


def test_append_assigns_monotonic_ids_scoped_to_run():
    b = BenchReplayBuffer()
    b.start_run("run1")
    r1 = b.append({"type": "line"})
    r2 = b.append({"type": "line"})
    assert r1["id"] == "run1:1"
    assert r2["id"] == "run1:2"


def test_start_run_resets_and_clears():
    b = BenchReplayBuffer()
    b.start_run("run1")
    b.append({"n": 1})
    b.start_run("run2")
    assert b.run_id == "run2"
    rec = b.append({"n": 1})
    assert rec["id"] == "run2:1"
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


# #912: a long run must not push its shape out of the buffer.
def test_run_shape_events_survive_a_full_line_buffer():
    b = BenchReplayBuffer(maxlen=3)
    b.start_run("r")
    b.append({"type": "model_start"})
    b.append({"type": "level_result", "level": 1})
    for n in range(10):
        b.append({"type": "line", "text": str(n)})
    b.append({"type": "level_result", "level": 2})
    kinds = [r["event"]["type"] for r in b.replay_after(None)]
    assert kinds[:2] == ["model_start", "level_result"]
    assert kinds.count("line") == 3 and kinds[-1] == "level_result"
    seqs = [r["seq"] for r in b.replay_after(None)]
    assert seqs == sorted(seqs)


def test_only_the_latest_progress_frame_is_replayed():
    b = BenchReplayBuffer(maxlen=3)
    b.start_run("r")
    b.append({"type": "progress", "done": 1, "total": 9})
    b.append({"type": "line", "text": "a"})
    b.append({"type": "progress", "done": 5, "total": 9})
    recs = b.replay_after(None)
    prog = [r for r in recs if r["event"]["type"] == "progress"]
    assert len(prog) == 1 and prog[0]["event"]["done"] == 5 and prog[0]["id"] == "r:3"
    assert [r["id"] for r in recs] == ["r:2", "r:3"]
    # A client past the progress frame does not get it again.
    assert b.replay_after("r:3") == []


def test_start_run_clears_kept_and_progress_records():
    b = BenchReplayBuffer()
    b.start_run("r1")
    b.append({"type": "model_start"})
    b.append({"type": "progress", "done": 1, "total": 2})
    b.start_run("r2")
    assert b.replay_after(None) == []
