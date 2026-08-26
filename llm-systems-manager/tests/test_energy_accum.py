"""#470: Accumulator behavior against an injected store view + sink."""
from __future__ import annotations

import sqlite3

import pytest

import energy as en

A1 = "a" * 32
A2 = "b" * 32


class FakeStore:
    def __init__(self):
        self.view: dict = {}

    def __call__(self):
        return self.view

    def set(self, agent_id, provider, sample, last_seen):
        self.view.setdefault(agent_id, {})[provider] = (sample, last_seen)


@pytest.fixture
def rig():
    store = FakeStore()
    sunk: list = []
    acc = en.Accumulator(store, sunk.extend)
    return store, sunk, acc


def _llama_sample(watts=300.0, busy=False, gen=None, prompt=None, host="box"):
    return {"host": host,
            "gpu": {"power_watts": watts},
            "llama": {"requests_processing": 1 if busy else 0,
                      "total_tokens_generated": gen,
                      "total_tokens_prompted": prompt}}


def test_first_tick_baselines_without_attribution(rig):
    store, sunk, acc = rig
    store.set(A1, "llama", _llama_sample(gen=100), 1000.0)
    out = acc.tick(now=1000.0)
    assert out == [] and sunk == []


def test_steady_ticks_attribute_energy_and_tokens(rig):
    store, sunk, acc = rig
    store.set(A1, "llama", _llama_sample(gen=100, prompt=50), 1000.0)
    acc.tick(now=1000.0)
    store.set(A1, "llama", _llama_sample(gen=160, prompt=70, busy=True), 1010.0)
    out = acc.tick(now=1010.0)
    assert len(out) == 1
    inc = out[0]
    assert inc["agent_id"] == A1 and inc["hostname"] == "box"
    assert inc["observed_s"] == 10.0 and inc["active_s"] == 10.0
    assert inc["power_s"] == 10.0
    assert inc["energy_wh"] == pytest.approx(300.0 * 10 / 3600)
    assert inc["active_energy_wh"] == inc["energy_wh"]
    assert inc["tokens_gen"] == 60 and inc["tokens_prompt"] == 20
    assert inc["power_source"] == "gpu"
    assert sunk == out


def test_idle_tick_splits_active_energy(rig):
    store, sunk, acc = rig
    store.set(A1, "llama", _llama_sample(), 1000.0)
    acc.tick(now=1000.0)
    store.set(A1, "llama", _llama_sample(busy=False), 1010.0)
    inc = acc.tick(now=1010.0)[0]
    assert inc["active_s"] == 0.0 and inc["active_energy_wh"] == 0.0
    assert inc["energy_wh"] > 0


def test_gap_beyond_cap_is_clamped(rig):
    store, _, acc = rig
    store.set(A1, "llama", _llama_sample(), 1000.0)
    acc.tick(now=1000.0)
    store.set(A1, "llama", _llama_sample(), 2000.0)
    inc = acc.tick(now=2000.0)[0]
    assert inc["observed_s"] == en.MAX_GAP_S


def test_stale_bucket_is_ignored(rig):
    store, sunk, acc = rig
    store.set(A1, "llama", _llama_sample(), 1000.0)
    acc.tick(now=1000.0)
    # No new push: last_seen falls behind FRESH_S → unobserved, no rows.
    assert acc.tick(now=1000.0 + en.FRESH_S + 20) == []
    assert sunk == []


def test_counter_reset_counts_since_restart(rig):
    store, _, acc = rig
    store.set(A1, "llama", _llama_sample(gen=5000), 1000.0)
    acc.tick(now=1000.0)
    store.set(A1, "llama", _llama_sample(gen=40), 1010.0)
    inc = acc.tick(now=1010.0)[0]
    assert inc["tokens_gen"] == 40


def test_counter_freeze_on_none_then_resume(rig):
    store, _, acc = rig
    store.set(A1, "llama", _llama_sample(gen=500), 1000.0)
    acc.tick(now=1000.0)
    # Sleeping llama reports None counters — nothing added, baseline kept.
    store.set(A1, "llama", _llama_sample(gen=None), 1010.0)
    inc = acc.tick(now=1010.0)[0]
    assert inc["tokens_gen"] == 0
    store.set(A1, "llama", _llama_sample(gen=650), 1020.0)
    inc = acc.tick(now=1020.0)[0]
    assert inc["tokens_gen"] == 150


def test_power_from_freshest_bucket_that_reports_it(rig):
    store, _, acc = rig
    lms = {"system": {"host": "mac", "gpu": {}}, "ps": []}
    store.set(A1, "lms", lms, 1010.0)
    store.set(A1, "llama", _llama_sample(watts=222.0), 1000.0)
    acc.tick(now=1010.0)
    store.set(A1, "lms", lms, 1020.0)
    store.set(A1, "llama", _llama_sample(watts=222.0), 1019.0)
    inc = acc.tick(now=1020.0)[0]
    assert inc["power_source"] == "gpu"
    assert inc["energy_wh"] == pytest.approx(222.0 * 10 / 3600)


def test_no_power_host_counts_time_but_no_energy(rig):
    store, _, acc = rig
    lms = {"system": {"host": "mac", "gpu": {}},
           "ps": [{"status": "GENERATING"}]}
    store.set(A1, "lms", lms, 1000.0)
    acc.tick(now=1000.0)
    store.set(A1, "lms", lms, 1010.0)
    inc = acc.tick(now=1010.0)[0]
    assert inc["observed_s"] == 10.0 and inc["active_s"] == 10.0
    assert inc["power_s"] == 0.0 and inc["energy_wh"] == 0.0
    assert inc["power_source"] is None


def test_multi_provider_counters_merge_not_double(rig):
    store, _, acc = rig
    store.set(A1, "llama", _llama_sample(gen=100), 1000.0)
    vllm = {"host": "box", "system": {"host": "box"},
            "vllm": {"total_tokens_generated": 900,
                     "requests_running": 0}}
    store.set(A1, "vllm", vllm, 1000.0)
    acc.tick(now=1000.0)
    store.set(A1, "llama", _llama_sample(gen=150), 1010.0)
    store.set(A1, "vllm", dict(vllm, vllm={"total_tokens_generated": 1000,
                                           "requests_running": 1}), 1010.0)
    inc = acc.tick(now=1010.0)[0]
    assert inc["tokens_gen"] == 50 + 100
    assert inc["active_s"] == 10.0


def test_two_agents_tracked_independently(rig):
    store, _, acc = rig
    store.set(A1, "llama", _llama_sample(gen=10), 1000.0)
    store.set(A2, "llama", _llama_sample(gen=99, host="other"), 1000.0)
    acc.tick(now=1000.0)
    store.set(A1, "llama", _llama_sample(gen=20), 1010.0)
    store.set(A2, "llama", _llama_sample(gen=100, host="other"), 1010.0)
    out = {i["agent_id"]: i for i in acc.tick(now=1010.0)}
    assert out[A1]["tokens_gen"] == 10 and out[A2]["tokens_gen"] == 1


def test_hour_bucket_assignment(rig):
    store, _, acc = rig
    store.set(A1, "llama", _llama_sample(), 7100.0)
    acc.tick(now=7100.0)
    store.set(A1, "llama", _llama_sample(), 7210.0)
    inc = acc.tick(now=7210.0)[0]
    assert inc["hour_ts"] == 7200


def test_sink_failure_does_not_crash_tick(rig):
    store, _, _ = rig
    calls = []

    def sink(incs):
        calls.append(list(incs))
        raise RuntimeError("disk full")

    acc = en.Accumulator(store, sink)
    store.set(A1, "llama", _llama_sample(), 1000.0)
    store.set(A2, "llama", _llama_sample(host="other"), 1000.0)
    acc.tick(now=1000.0)
    store.set(A1, "llama", _llama_sample(), 1010.0)
    store.set(A2, "llama", _llama_sample(host="other"), 1010.0)
    out = acc.tick(now=1010.0)
    assert len(out) == 2 and calls[-1] == out


def test_store_view_failure_returns_empty(rig):
    def boom():
        raise RuntimeError("store gone")

    acc = en.Accumulator(boom, lambda inc: None)
    assert acc.tick(now=1.0) == []


# ── storage round-trip ───────────────────────────────────────────────

def test_upsert_accumulates_and_queries():
    conn = sqlite3.connect(":memory:")
    en.init_table(conn)
    base = {"hour_ts": 3600, "agent_id": A1, "hostname": "box",
            "observed_s": 10.0, "active_s": 10.0, "power_s": 10.0,
            "energy_wh": 1.0, "active_energy_wh": 1.0,
            "tokens_gen": 5, "tokens_prompt": 2, "power_source": "psu"}
    en.upsert_increment(conn, base)
    en.upsert_increment(conn, dict(base, active_s=0.0, active_energy_wh=0.0,
                                   power_source=None))
    rows = en.query_rows(conn, 0, 7200)
    assert len(rows) == 1
    r = rows[0]
    assert r["observed_s"] == 20.0 and r["active_s"] == 10.0
    assert r["energy_wh"] == 2.0 and r["tokens_gen"] == 10
    assert r["power_source"] == "psu" and r["samples"] == 2
    assert en.first_ts(conn) == 3600
    assert en.query_rows(conn, 7200, 10800) == []


def test_first_ts_empty_table():
    conn = sqlite3.connect(":memory:")
    en.init_table(conn)
    assert en.first_ts(conn) is None


# ── #496: cross-bucket counters + gateway usage source ───────────────

def test_stale_duplicate_bucket_does_not_fake_restart(rig):
    store, _, acc = rig
    # The same vllm counter arrives via two buckets (full host sample +
    # vllm envelope); the llama-bucket copy then goes stale-but-fresh.
    full = {"host": "box", "gpu": {"power_watts": 100.0},
            "vllm": {"total_tokens_generated": 1000}}
    venv = {"host": "box", "system": {"host": "box"},
            "vllm": {"total_tokens_generated": 1000}}
    store.set(A1, "llama", full, 1000.0)
    store.set(A1, "vllm", venv, 1000.0)
    acc.tick(now=1000.0)
    store.set(A1, "vllm", dict(venv, vllm={"total_tokens_generated": 1005}),
              1010.0)
    inc = acc.tick(now=1010.0)[0]
    assert inc["tokens_gen"] == 5
    store.set(A1, "vllm", dict(venv, vllm={"total_tokens_generated": 1010}),
              1020.0)
    inc = acc.tick(now=1020.0)[0]
    assert inc["tokens_gen"] == 5


def _lms_sample():
    return {"system": {"host": "mac", "gpu": {}}, "ps": []}


def test_gateway_usage_counted_for_agent():
    store = FakeStore()
    sunk: list = []
    usage = {A1: {"gen": 100, "prompt": 40}}
    acc = en.Accumulator(store, sunk.extend, usage_view=lambda: usage)
    store.set(A1, "lms", _lms_sample(), 1000.0)
    acc.tick(now=1000.0)
    usage[A1] = {"gen": 160, "prompt": 60}
    store.set(A1, "lms", _lms_sample(), 1010.0)
    inc = acc.tick(now=1010.0)[0]
    assert inc["tokens_gen"] == 60 and inc["tokens_prompt"] == 20


def test_gateway_usage_view_failure_does_not_break_tick():
    store = FakeStore()
    sunk: list = []

    def boom():
        raise RuntimeError("nope")

    acc = en.Accumulator(store, sunk.extend, usage_view=boom)
    store.set(A1, "lms", _lms_sample(), 1000.0)
    acc.tick(now=1000.0)
    store.set(A1, "lms", _lms_sample(), 1010.0)
    inc = acc.tick(now=1010.0)[0]
    assert inc["tokens_gen"] == 0 and inc["observed_s"] == 10.0


# ── #620/#621: retention pruning, batched sink, agent eviction ───────

def test_prune_deletes_only_old_rows():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    en.init_table(conn)
    now = 100 * 86400.0
    old_hour = int((now - 60 * 86400) // 3600) * 3600
    new_hour = int(now // 3600) * 3600
    for hour in (old_hour, new_hour):
        en.upsert_increment(conn, {
            "hour_ts": hour, "agent_id": "a", "hostname": "box",
            "observed_s": 10.0, "active_s": 0.0, "power_s": 10.0,
            "energy_wh": 1.0, "active_energy_wh": 0.0,
            "tokens_gen": 0, "tokens_prompt": 0, "power_source": "psu"})
    assert en.prune(conn, 45, now=now) == 1
    left = conn.execute("SELECT hour_ts FROM energy_hourly").fetchall()
    assert [r[0] for r in left] == [new_hour]
    assert en.prune(conn, 45, now=now) == 0


def test_upsert_increments_single_commit():
    import sqlite3

    class Spy:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.commits = 0

        def execute(self, *a):
            return self.conn.execute(*a)

        def commit(self):
            self.commits += 1
            self.conn.commit()

    spy = Spy()
    en.init_table(spy)
    base = {"hostname": "box", "observed_s": 10.0, "active_s": 0.0,
            "power_s": 10.0, "energy_wh": 1.0, "active_energy_wh": 0.0,
            "tokens_gen": 0, "tokens_prompt": 0, "power_source": "psu"}
    spy.commits = 0
    en.upsert_increments(spy, [{**base, "hour_ts": 0, "agent_id": a}
                               for a in ("a", "b", "c")])
    assert spy.commits == 1
    n = spy.execute("SELECT COUNT(*) FROM energy_hourly").fetchone()[0]
    assert n == 3


def test_tick_calls_sink_once_with_all_incs():
    store = FakeStore()
    calls: list = []
    acc = en.Accumulator(store, calls.append)
    store.set(A1, "llama", _llama_sample(), 1000.0)
    store.set("b" * 32, "llama", _llama_sample(host="box2"), 1000.0)
    acc.tick(now=1000.0)
    calls.clear()
    out = acc.tick(now=1010.0)
    assert len(out) == 2
    assert len(calls) == 1 and calls[0] == out


def test_stale_agent_state_evicted():
    store = FakeStore()
    sunk: list = []
    acc = en.Accumulator(store, sunk.extend)
    store.set(A1, "llama", _llama_sample(gen=100), 1000.0)
    acc.tick(now=1000.0)
    assert A1 in acc._agents
    # Sample goes stale: no fresh buckets for > AGENT_EVICT_S → evicted.
    acc.tick(now=1000.0 + en.AGENT_EVICT_S + 60)
    assert A1 not in acc._agents
    # Agent returns: first tick re-baselines without attributing tokens.
    t = 1000.0 + en.AGENT_EVICT_S + 120
    store.set(A1, "llama", _llama_sample(gen=999_999), t)
    out = acc.tick(now=t)
    assert out == []


def test_retention_days_parses_and_guards():
    import types as _t

    def ctx(v):
        return _t.SimpleNamespace(settings=_t.SimpleNamespace(
            manager=_t.SimpleNamespace(energy=_t.SimpleNamespace(
                retention_days=v))))

    assert en._retention_days(ctx(365)) == 365.0
    assert en._retention_days(ctx(None)) is None
    assert en._retention_days(ctx(0)) is None
    assert en._retention_days(ctx("bogus")) is None
    assert en._retention_days(object()) is None
