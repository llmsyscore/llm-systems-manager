# agent/tests/test_buffered_metric_client_claim.py
"""BufferStore snapshot/commit claim invariants: no drop, no duplicate,
deferred spills while a claim is open, emergency valve past 2x the bound."""
from __future__ import annotations

import sys
import types

# The agent runtime ships `requests`; the test venv doesn't. Only
# requests.Session needs to exist for the module import.
if "requests" not in sys.modules:
    _fake = types.ModuleType("requests")
    _fake.Session = type("Session", (), {})
    sys.modules["requests"] = _fake

import buffered_metric_client as bmc


def _store(tmp_path, max_mem=10):
    return bmc.BufferStore(
        cache_file=tmp_path / "buffer.jsonl",
        max_disk_bytes=1024 * 1024,
        max_memory_samples=max_mem,
    )


def _sample(i):
    return {"id": i}


def _drain_ids(store, batch_limit=100):
    """Snapshot+commit until empty; return the ids in send order."""
    ids = []
    for _ in range(1000):
        batch, claim = store.snapshot(batch_limit)
        if not batch:
            break
        ids.extend(s["id"] for s in batch)
        store.commit(claim)
    return ids


def test_spill_during_open_claim_is_deferred(tmp_path):
    store = _store(tmp_path, max_mem=10)
    for i in range(10):
        store.enqueue(_sample(i))

    batch, claim = store.snapshot(5)
    assert [s["id"] for s in batch] == [0, 1, 2, 3, 4]

    # Push past the memory bound mid-claim (but under the 2x valve):
    # no spill may run yet.
    for i in range(10, 20):
        store.enqueue(_sample(i))
    assert store.disk_count() == 0
    assert store.memory_count() == 20

    spilled, _, _ = store.commit(claim)
    # Claim closed: the deferred spill runs and the front is intact.
    assert spilled > 0
    assert store.disk_count() > 0
    assert _drain_ids(store) == list(range(5, 20))


def test_commit_pops_exactly_the_claimed_samples(tmp_path):
    # Regression for the original bug shape: the old code popped a COUNT
    # off a front that a concurrent spill had already shifted.
    store = _store(tmp_path, max_mem=10)
    for i in range(10):
        store.enqueue(_sample(i))
    batch, claim = store.snapshot(5)
    for i in range(10, 26):
        store.enqueue(_sample(i))
    store.commit(claim)

    remaining = _drain_ids(store)
    sent = [s["id"] for s in batch]
    assert sorted(sent + remaining) == list(range(26))
    assert len(set(sent + remaining)) == 26


def test_abort_preserves_everything_and_order(tmp_path):
    store = _store(tmp_path, max_mem=10)
    for i in range(10):
        store.enqueue(_sample(i))
    batch, claim = store.snapshot(5)
    for i in range(10, 20):
        store.enqueue(_sample(i))
    store.abort()

    assert _drain_ids(store) == list(range(20))


def test_snapshot_while_claim_open_returns_empty(tmp_path):
    store = _store(tmp_path, max_mem=10)
    for i in range(5):
        store.enqueue(_sample(i))
    batch, claim = store.snapshot(3)
    assert batch
    again, _ = store.snapshot(3)
    assert again == []
    store.commit(claim)
    assert _drain_ids(store) == [3, 4]


def test_disk_only_claim_defers_spill(tmp_path):
    # A claim over disk lines must also freeze the file: a mid-claim spill
    # could trigger budget eviction/compaction and shift the byte offsets.
    store = _store(tmp_path, max_mem=10)
    for i in range(30):
        store.enqueue(_sample(i))
    assert store.disk_count() > 0

    batch, claim = store.snapshot(store.disk_count())
    assert claim.disk_lines > 0 and claim.mem == 0
    disk_before = store.disk_count()
    for i in range(30, 38):
        store.enqueue(_sample(i))
    assert store.disk_count() == disk_before
    store.commit(claim)

    sent = [s["id"] for s in batch]
    assert _drain_ids(store) == [i for i in range(38) if i not in set(sent)]


def test_valve_spills_past_double_bound_but_protects_claim(tmp_path):
    store = _store(tmp_path, max_mem=10)
    for i in range(10):
        store.enqueue(_sample(i))
    batch, claim = store.snapshot(5)

    # Below 2x the bound: fully deferred.
    for i in range(10, 20):
        store.enqueue(_sample(i))
    assert store.disk_count() == 0

    # Past 2x: the valve spills, but never the claimed front.
    for i in range(20, 26):
        store.enqueue(_sample(i))
    assert store.disk_count() > 0

    spilled_ids = set(range(26)) - {s["id"] for s in list(store._memory)}
    assert not spilled_ids & {0, 1, 2, 3, 4}

    store.commit(claim)
    remaining = _drain_ids(store)
    assert sorted([s["id"] for s in batch] + remaining) == list(range(26))


class _FlakySession:
    """post() enqueues mid-flight samples (the race window), then fails
    once and succeeds afterwards."""

    def __init__(self):
        self.client = None
        self.calls = 0
        self.posted_ids = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls += 1
        if self.calls <= 2:
            for i in range(100, 120):
                self.client.enqueue({"id": i + self.calls * 100})
        ids = [s["id"] for s in json["samples"]]
        if self.calls == 1:
            raise RuntimeError("simulated POST failure")
        self.posted_ids.extend(ids)
        return types.SimpleNamespace(raise_for_status=lambda: None)


def test_flush_race_no_drop_no_duplicate(tmp_path):
    session = _FlakySession()
    client = bmc.BufferedMetricClient(
        endpoint_url="http://example.invalid/api/alarm/metrics/ingest",
        host="testhost",
        cache_dir=tmp_path,
        max_memory_samples=10,
        batch_limit=8,
        session=session,
    )
    session.client = client
    initial = list(range(10))
    for i in initial:
        client.enqueue({"id": i})

    for _ in range(100):
        client._flush_once()
        if client.buffered_count() == 0:
            break

    assert client.buffered_count() == 0
    assert len(session.posted_ids) == len(set(session.posted_ids))
    assert set(initial).issubset(set(session.posted_ids))
    assert client.stats.samples_posted == len(session.posted_ids)
