# agent/tests/test_buffered_metric_client_rotation.py
"""BufferStore resync after external deletion/re-creation of buffer.jsonl
(#586) and endpoint-path preservation on AE retarget (#587)."""
from __future__ import annotations

import os
import sys
import types

# The agent runtime ships `requests`; the test venv doesn't. Only
# requests.Session needs to exist for the module import.
if "requests" not in sys.modules:
    _fake = types.ModuleType("requests")
    _fake.Session = type("Session", (), {})
    sys.modules["requests"] = _fake

import buffered_metric_client as bmc


def _store(tmp_path, max_mem=10, max_disk=1024 * 1024):
    return bmc.BufferStore(
        cache_file=tmp_path / "buffer.jsonl",
        max_disk_bytes=max_disk,
        max_memory_samples=max_mem,
    )


def _drain_ids(store, batch_limit=3):
    ids = []
    for _ in range(1000):
        batch, claim = store.snapshot(batch_limit)
        if not batch:
            break
        ids.extend(s["id"] for s in batch)
        store.commit(claim)
    return ids


def test_external_delete_then_respill_resyncs_and_drains(tmp_path):
    # Issue #586 repro: delete the cache file while the store holds disk
    # lines, then trigger a re-spill that re-creates it.
    store = _store(tmp_path, max_mem=16)
    for i in range(100):
        store.enqueue({"id": i})
    assert store.disk_count() > 0
    os.unlink(tmp_path / "buffer.jsonl")

    for i in range(100, 120):
        store.enqueue({"id": i})

    ids = _drain_ids(store)
    # Samples deleted with the file are gone, but every re-spilled and
    # in-memory sample must survive, in order, without phantom leftovers.
    respilled = [i for i in ids if i >= 100]
    assert respilled == sorted(respilled)
    assert set(range(100, 120)) <= set(ids)
    assert store.total() == 0
    assert store.disk_count() == 0
    assert not (tmp_path / "buffer.jsonl").exists()


def test_external_delete_respill_disk_count_matches_file(tmp_path):
    store = _store(tmp_path, max_mem=4)
    for i in range(40):
        store.enqueue({"id": i})
    os.unlink(tmp_path / "buffer.jsonl")
    for i in range(40, 60):
        store.enqueue({"id": i})
    with (tmp_path / "buffer.jsonl").open("rb") as f:
        lines = sum(1 for _ in f)
    assert store.disk_count() == lines


def test_snapshot_offset_past_eof_resets_and_reads_new_file(tmp_path):
    store = _store(tmp_path, max_mem=4)
    for i in range(40):
        store.enqueue({"id": i})
    # Consume some disk lines so _offset advances.
    batch, claim = store.snapshot(10)
    assert batch
    store.commit(claim)
    assert store.disk_count() > 0
    # External rotation: replace the file with fewer bytes than _offset.
    (tmp_path / "buffer.jsonl").write_text('{"id": 900}\n')
    batch, claim = store.snapshot(10)
    got = [s["id"] for s in batch]
    store.commit(claim)
    assert 900 in got
    # Drain to empty; the phantom pre-rotation count must not linger.
    _drain_ids(store)
    assert store.total() == len(store._memory)
    assert store.disk_count() == 0


def _client(tmp_path, endpoint):
    return bmc.BufferedMetricClient(
        endpoint_url=endpoint,
        host="testhost",
        cache_dir=tmp_path,
    )


def test_retarget_preserves_custom_endpoint_path(tmp_path):
    c = _client(tmp_path, "http://ae-one:9800/proxy/prefix/ingest")
    c.update_alarm_engine_url("http://ae-two:9801")
    assert c.endpoint_url == "http://ae-two:9801/proxy/prefix/ingest"


def test_retarget_default_path_unchanged(tmp_path):
    c = _client(tmp_path, "http://ae-one:9800" + bmc.INGEST_PATH)
    c.update_alarm_engine_url("http://ae-two:9801/")
    assert c.endpoint_url == "http://ae-two:9801" + bmc.INGEST_PATH


def test_retarget_empty_url_ignored(tmp_path):
    c = _client(tmp_path, "http://ae-one:9800" + bmc.INGEST_PATH)
    c.update_alarm_engine_url("")
    assert c.endpoint_url == "http://ae-one:9800" + bmc.INGEST_PATH
