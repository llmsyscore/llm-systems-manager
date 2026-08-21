# agent/tests/test_buffered_metric_client_rotation.py
"""BufferStore resync after external deletion/re-creation of buffer.jsonl
(#586) and endpoint-path preservation on AE retarget (#587)."""
from __future__ import annotations

import os
import sys
import types

# Stub `requests` with the one attribute the module import touches.
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


def test_restored_file_content_is_not_lost(tmp_path):
    # File content appears externally while bookkeeping is all-zero (e.g. a
    # backup restore); later spills must not undercount and lose lines.
    (tmp_path / "buffer.jsonl").write_text('{"id": 900}\n{"id": 901}\n')
    store = _store(tmp_path, max_mem=4)
    for i in range(10):
        store.enqueue({"id": i})
    ids = _drain_ids(store)
    assert {900, 901} <= set(ids)
    assert set(range(10)) <= set(ids)
    assert store.total() == 0


def test_restored_file_after_drain_is_detected(tmp_path):
    store = _store(tmp_path, max_mem=4)
    for i in range(10):
        store.enqueue({"id": i})
    _drain_ids(store)
    assert store.total() == 0
    (tmp_path / "buffer.jsonl").write_text('{"id": 900}\n')
    for i in range(10, 20):
        store.enqueue({"id": i})
    ids = _drain_ids(store)
    assert 900 in ids
    assert set(range(10, 20)) <= set(ids)


def test_accessors_resync_after_external_delete(tmp_path):
    store = _store(tmp_path, max_mem=4)
    for i in range(20):
        store.enqueue({"id": i})
    assert store.disk_count() > 0
    os.unlink(tmp_path / "buffer.jsonl")
    assert store.disk_count() == 0
    assert store.total() == store.memory_count()
    assert store.breakdown() == (store.memory_count(), 0)


def test_abort_after_external_rotation_keeps_new_file(tmp_path):
    # Rotation during an in-flight POST that then fails: abort() must not
    # compact/unlink the rotated-in file from a stale offset.
    store = _store(tmp_path, max_mem=4)
    for i in range(40):
        store.enqueue({"id": i})
    batch, claim = store.snapshot(10)
    assert batch
    store.commit(claim)
    batch, claim = store.snapshot(10)
    assert batch
    (tmp_path / "buffer.jsonl").write_text('{"id": 900}\n')
    store.abort()
    ids = _drain_ids(store)
    assert 900 in ids


def _client(tmp_path, endpoint, **kw):
    return bmc.BufferedMetricClient(
        endpoint_url=endpoint,
        host="testhost",
        cache_dir=tmp_path,
        **kw,
    )


def test_retarget_preserves_custom_ingest_path(tmp_path):
    c = _client(tmp_path, "http://ae-one:9800/proxy/prefix/ingest",
                ingest_path="/proxy/prefix/ingest")
    c.update_alarm_engine_url("http://ae-two:9801")
    assert c.endpoint_url == "http://ae-two:9801/proxy/prefix/ingest"


def test_retarget_default_path_unchanged(tmp_path):
    c = _client(tmp_path, "http://ae-one:9800" + bmc.INGEST_PATH)
    c.update_alarm_engine_url("http://ae-two:9801/")
    assert c.endpoint_url == "http://ae-two:9801" + bmc.INGEST_PATH


def test_retarget_prefixed_ae_base_is_not_doubled(tmp_path):
    # AE base URL carrying a proxy prefix: the same base echoed back on
    # retarget must yield base + default ingest path, no prefix doubling.
    base = "https://gw.example/ae-prefix"
    c = _client(tmp_path, base + bmc.INGEST_PATH)
    c.update_alarm_engine_url(base)
    assert c.endpoint_url == base + bmc.INGEST_PATH


def test_retarget_empty_url_ignored(tmp_path):
    c = _client(tmp_path, "http://ae-one:9800" + bmc.INGEST_PATH)
    c.update_alarm_engine_url("")
    assert c.endpoint_url == "http://ae-one:9800" + bmc.INGEST_PATH
