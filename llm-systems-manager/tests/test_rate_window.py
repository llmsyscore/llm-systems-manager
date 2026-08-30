"""#745: server-side 60-min rate window stats (avg of active buckets + peak)."""
from __future__ import annotations

import json

import manager_mod


def _pt(ts, v, host="h1"):
    return {"timestamp": ts, "value": v, "hostname": host}


def test_buckets_keep_per_bucket_max_and_skip_non_numeric():
    hb = manager_mod._rate_window_buckets([
        _pt("2026-08-29T12:00:01+00:00", 10.0),
        _pt("2026-08-29T12:00:06+00:00", 40.0),   # same 15 s bucket → max
        _pt("2026-08-29T12:00:20+00:00", None),
        _pt("2026-08-29T12:00:21+00:00", "x"),
        _pt("2026-08-29T12:00:22+00:00", 5),
    ])
    assert hb == {"2026-08-29T12:00:00+00:00": 40.0,
                  "2026-08-29T12:00:15+00:00": 5.0}


def test_stats_sum_hosts_average_active_only_and_peak_latest_tie():
    stats = manager_mod._rate_window_stats({
        "h1": {"t1": 10.0, "t2": 0.0, "t3": 30.0},
        "h2": {"t1": 5.0, "t3": 5.0, "t4": 35.0},
    })
    # t1=15, t2=0 (inactive), t3=35, t4=35 → avg over active = 85/3
    assert stats["avg"] == round((15.0 + 35.0 + 35.0) / 3, 3)
    assert stats["peak"] == {"v": 35.0, "ts": "t4"}


def test_stats_empty_window():
    assert manager_mod._rate_window_stats({}) == {"avg": None, "peak": None}
    assert manager_mod._rate_window_stats({"h1": {"t1": 0.0}}) == {"avg": None, "peak": None}


def test_metrics_resolve_from_legacy_map():
    m = manager_mod._rate_window_metrics("lms")
    assert m["gen"] == ("gateway", "lms_tokens_per_second", "lms_tps")
    assert m["prompt"] == ("gateway", "lms_prompt_tokens_per_second", "lms_pps")
    assert manager_mod._rate_window_metrics("llama")["gen"][:2] == ("llama", "tokens_per_second")
    assert manager_mod._rate_window_metrics("nope") is None


SERIES = {
    ("tokens_per_second", "a"): [_pt("2026-08-29T12:00:00+00:00", 20.0, "a"),
                                 _pt("2026-08-29T12:00:15+00:00", 0.0, "a")],
    ("tokens_per_second", "b"): [_pt("2026-08-29T12:00:00+00:00", 10.0, "b"),
                                 _pt("2026-08-29T12:00:30+00:00", 50.0, "b")],
    ("prompt_tokens_per_second", "a"): [_pt("2026-08-29T12:00:00+00:00", 400.0, "a")],
    ("prompt_tokens_per_second", "b"): [],
}


def _fake_fetch(base, source, name, field, since, limit, hostname=None):
    assert base == "http://ae" and since == 60 and hostname
    return field, SERIES.get((name, hostname), [])


def test_build_rate_window_fleet_and_single_host(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae")
    monkeypatch.setattr(manager_mod, "_fetch_history_series", _fake_fetch)
    fleet = manager_mod._build_rate_window("llama", ["a", "b"])
    assert fleet["window_s"] == 3600 and fleet["grain_s"] == 15 and fleet["hosts"] == 2
    # gen buckets: 12:00:00 → 30, 12:00:15 → 0 (inactive), 12:00:30 → 50
    assert fleet["gen"] == {"avg": 40.0, "peak": {"v": 50.0, "ts": "2026-08-29T12:00:30+00:00"}}
    assert fleet["prompt"] == {"avg": 400.0, "peak": {"v": 400.0, "ts": "2026-08-29T12:00:00+00:00"}}
    single = manager_mod._build_rate_window("llama", ["a"])
    assert single["gen"] == {"avg": 20.0, "peak": {"v": 20.0, "ts": "2026-08-29T12:00:00+00:00"}}
    assert single["hosts"] == 1


def test_build_rate_window_requires_ae_and_hosts(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae")
    assert manager_mod._build_rate_window("llama", []) is None
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "")
    assert manager_mod._build_rate_window("llama", ["a"]) is None


def test_rate_window_for_caches_per_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae")
    monkeypatch.setattr(manager_mod, "_build_rate_window",
                        lambda p, hosts: calls.append((p, tuple(hosts))) or {"gen": {}, "prompt": {}})
    manager_mod._history_scoped_cache.clear()
    assert manager_mod._rate_window_for("lms", ["b", "a", "", None]) is not None
    assert manager_mod._rate_window_for("lms", ["a", "b"]) is not None
    assert calls == [("lms", ("a", "b"))]
    assert manager_mod._rate_window_for("lms", []) is None
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "")
    assert manager_mod._rate_window_for("lms", ["a"]) is None


def test_rate_window_for_swallows_builder_errors(monkeypatch):
    def _boom(p, hosts):
        raise RuntimeError("ae down")
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae")
    monkeypatch.setattr(manager_mod, "_build_rate_window", _boom)
    manager_mod._history_scoped_cache.clear()
    assert manager_mod._rate_window_for("vllm", ["z"]) is None


WIN = {"window_s": 3600, "grain_s": 15, "hosts": 1,
       "gen": {"avg": 1.5, "peak": {"v": 2.0, "ts": "t"}},
       "prompt": {"avg": None, "peak": None}}


def test_fleet_aggregate_attaches_window(monkeypatch):
    monkeypatch.setattr(manager_mod, "_provider_hosts", lambda p: ["a"])
    monkeypatch.setattr(manager_mod, "_rate_window_for", lambda p, hosts: WIN if hosts == ["a"] else None)
    monkeypatch.setattr(manager_mod.provider_state.STORE, "all_for", lambda p: {})
    with manager_mod.app.test_request_context("/api/fleet/llama/aggregate"):
        resp = manager_mod.fleet_aggregate("llama")
    body = json.loads(resp.get_data())
    assert body["throughput"]["window"] == WIN
    assert body["throughput"]["total_tps"] == 0


def test_fleet_aggregate_without_window_keeps_shape(monkeypatch):
    monkeypatch.setattr(manager_mod, "_provider_hosts", lambda p: [])
    monkeypatch.setattr(manager_mod, "_rate_window_for", lambda p, hosts: None)
    monkeypatch.setattr(manager_mod.provider_state.STORE, "all_for", lambda p: {})
    with manager_mod.app.test_request_context("/api/fleet/lms/aggregate"):
        body = json.loads(manager_mod.fleet_aggregate("lms").get_data())
    assert "window" not in body["throughput"]


def test_provider_metrics_payload_attaches_window(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "default_agent_id_for", lambda p: "aid1")
    monkeypatch.setattr(manager_mod, "_agent_hostname", lambda aid: "host1" if aid == "aid1" else None)
    monkeypatch.setattr(manager_mod, "_rate_window_for", lambda p, hosts: WIN if hosts == ["host1"] else None)
    monkeypatch.setattr(manager_mod.provider_state.STORE, "get",
                        lambda p, aid: {"sample": {"x": 1}, "last_seen": 0.0})
    with manager_mod.app.test_request_context("/api/lmstudio/metrics"):
        data = manager_mod._provider_metrics_payload("lms")
    assert data["throughput_window"] == WIN and data["x"] == 1


def test_api_metrics_copies_sample_before_attaching(monkeypatch):
    sample = {"llama": {"tokens_per_second": 1.0}}
    monkeypatch.setattr(manager_mod, "_llama_agent_id_for_request", lambda: "aid1")
    monkeypatch.setattr(manager_mod.provider_state.STORE, "get",
                        lambda p, aid: {"sample": sample, "last_seen": 0.0})
    monkeypatch.setattr(manager_mod, "_agent_hostname", lambda aid: "host1")
    monkeypatch.setattr(manager_mod, "_rate_window_for", lambda p, hosts: WIN)
    with manager_mod.app.test_request_context("/api/metrics"):
        body = json.loads(manager_mod.get_latest().get_data())
    assert body["throughput_window"] == WIN
    assert "throughput_window" not in sample
