"""#966: /api/llama-state carries residency + freshness; stale/offline edges broadcast."""
from __future__ import annotations

import time

import manager_mod as M
import provider_state


def _wrap(llama, age=0.0):
    return {"sample": {"llama": llama}, "last_seen": time.time() - age}


def test_payload_adds_aggregate_residency_power_and_freshness(monkeypatch):
    res = {"aggregate": "loading", "models": [], "servers": {"llama": "up"}, "ts": 1, "source": "reconciled"}
    w = _wrap({"state": "awake", "model": "x", "residency": res, "power": {"mode": "full", "applied": "performance"}})
    monkeypatch.setattr(M.provider_state.STORE, "get", lambda k, a: w)
    p = M._build_llama_state_payload("a1")
    assert p["state"] == "awake" and p["aggregate"] == "loading"
    assert p["residency"] == res and p["power"]["applied"] == "performance"
    assert p["stale"] is False and p["agent_online"] is True and p["age_s"] < 5
    assert p["pulled"] is False


def test_payload_projects_legacy_sample_without_residency(monkeypatch):
    monkeypatch.setattr(M.provider_state.STORE, "get", lambda k, a: _wrap({"state": "sleeping", "model": "m (sleeping)"}))
    p = M._build_llama_state_payload("a1")
    assert p["aggregate"] == "sleeping" and p["residency"]["source"] == "legacy" and p["model"] == "m"
    assert p["pulled"] is False
    monkeypatch.setattr(M.provider_state.STORE, "get", lambda k, a: _wrap({"state": "awake", "model": "m (unloaded)"}))
    assert M._build_llama_state_payload("a1")["aggregate"] == "idle"
    monkeypatch.setattr(M.provider_state.STORE, "get", lambda k, a: _wrap({"state": "unknown"}))
    assert M._build_llama_state_payload("a1")["aggregate"] == "off"


def test_stale_flag_after_15s_offline_after_30s(monkeypatch):
    monkeypatch.setattr(M.provider_state.STORE, "get", lambda k, a: _wrap({"state": "awake"}, age=20))
    p = M._build_llama_state_payload("a1")
    assert p["stale"] is True and p["agent_online"] is True
    monkeypatch.setattr(M.provider_state.STORE, "get", lambda k, a: _wrap({"state": "awake"}, age=40))
    p = M._build_llama_state_payload("a1")
    assert p["stale"] is True and p["agent_online"] is False


def test_fingerprint_includes_stale_and_aggregate(monkeypatch):
    st = provider_state._ProviderSampleStore()
    monkeypatch.setattr(M.provider_state, "STORE", st)
    st.put("llama", "a1", {"llama": {"state": "awake", "residency": {"aggregate": "active", "models": [], "servers": {}}}})
    import queue
    q = queue.Queue()
    st.subscribe("llama", "a1", q)
    M._broadcast_llama_state_if_changed("a1")
    st._samples["llama"]["a1"]["last_seen"] -= 20          # goes stale, same state
    M._broadcast_llama_state_if_changed("a1")
    frames = []
    while not q.empty():
        frames.append(q.get())
    assert len(frames) == 2 and '"stale": true' in frames[1]


def test_offline_sweep_broadcasts_llama_edge(monkeypatch):
    st = provider_state._ProviderSampleStore()
    monkeypatch.setattr(M.provider_state, "STORE", st)
    st.put("llama", "a1", {"llama": {"state": "awake"}})
    st.mark_online("llama", "a1")
    st._samples["llama"]["a1"]["last_seen"] -= 40
    calls = []
    monkeypatch.setattr(M, "_broadcast_llama_state_if_changed", lambda aid: calls.append(aid))
    monkeypatch.setattr(M, "_pull_llama_state_if_stale", lambda aid: None)
    M._offline_sweep_once(time.time())
    assert calls == ["a1"]


def test_set_llama_awake_from_sample_uses_aggregate(monkeypatch):
    seen = []
    monkeypatch.setattr(M, "set_llama_awake", lambda v: seen.append(v))
    M._set_llama_awake_from_sample({"llama": {"state": "sleeping", "residency": {"aggregate": "loading"}}})
    M._set_llama_awake_from_sample({"llama": {"state": "awake", "residency": {"aggregate": "idle"}}})
    M._set_llama_awake_from_sample({"llama": {"state": "awake"}})
    assert seen == [True, False, True]


class _FakePullResponse:
    ok = True

    def json(self):
        return {"state": "sleeping",
                "residency": {"aggregate": "sleeping", "models": [], "servers": {"llama": "up"}},
                "power": {"mode": "full"}}


def _stub_pull_agent(monkeypatch, calls=None):
    agent = {"agent_id": "a1", "token": "tok"}
    monkeypatch.setattr(M.agent_registry, "resolve_agent_by_id", lambda aid: agent)
    monkeypatch.setattr(M.agent_registry, "agent_callback_urls", lambda a: ["http://a"])
    monkeypatch.setattr(M.agent_registry, "agent_tls_kwargs", lambda url: {})

    def fake_get(url, **kwargs):
        if calls is not None:
            calls.append(url)
        return _FakePullResponse()
    monkeypatch.setattr(M.requests, "get", fake_get)
    monkeypatch.setattr(M, "_pull_last", {})


def test_pull_llama_state_broadcasts_and_preserves_unrelated_keys(monkeypatch):
    st = provider_state._ProviderSampleStore()
    monkeypatch.setattr(M.provider_state, "STORE", st)
    st.put("llama", "a1", {"llama": {"state": "awake"}, "system": {"cpu": 12}})
    _stub_pull_agent(monkeypatch)
    broadcasts = []
    monkeypatch.setattr(M, "_broadcast_llama_state_if_changed", lambda aid: broadcasts.append(aid))
    M._pull_llama_state_if_stale("a1")
    sample = st.get("llama", "a1")["sample"]
    assert sample["system"] == {"cpu": 12}
    assert sample["llama"]["residency"]["aggregate"] == "sleeping"
    assert sample["llama"]["pulled_at"] is not None
    assert broadcasts == ["a1"]
    p = M._build_llama_state_payload("a1")
    assert p["pulled"] is True


def test_pull_does_not_fake_liveness_but_clears_stale(monkeypatch):
    st = provider_state._ProviderSampleStore()
    monkeypatch.setattr(M.provider_state, "STORE", st)
    st.put("llama", "a1", {"llama": {"state": "awake"}})
    st._samples["llama"]["a1"]["last_seen"] -= 40          # pushes stopped 40s ago
    pushed_at = st.get("llama", "a1")["last_seen"]
    _stub_pull_agent(monkeypatch)
    monkeypatch.setattr(M, "_broadcast_llama_state_if_changed", lambda aid: None)
    M._pull_llama_state_if_stale("a1")
    assert st.get("llama", "a1")["last_seen"] == pushed_at
    p = M._build_llama_state_payload("a1")
    assert p["stale"] is False and p["pulled"] is True and p["age_s"] < 5
    assert p["agent_online"] is False and p["agent_age_s"] >= 40


def test_pull_on_a_never_pushed_agent_keeps_last_seen_zero(monkeypatch):
    st = provider_state._ProviderSampleStore()
    monkeypatch.setattr(M.provider_state, "STORE", st)
    _stub_pull_agent(monkeypatch)
    monkeypatch.setattr(M, "_broadcast_llama_state_if_changed", lambda aid: None)
    M._pull_llama_state_if_stale("a1")
    assert st.get("llama", "a1")["last_seen"] == 0.0
    p = M._build_llama_state_payload("a1")
    assert p["agent_online"] is False and p["agent_age_s"] is None and p["stale"] is False


def test_offline_sweep_still_marks_offline_when_only_pulls_succeed(monkeypatch):
    st = provider_state._ProviderSampleStore()
    monkeypatch.setattr(M.provider_state, "STORE", st)
    st.put("llama", "a1", {"llama": {"state": "awake"}})
    st.mark_online("llama", "a1")
    st._samples["llama"]["a1"]["last_seen"] -= 40
    _stub_pull_agent(monkeypatch)
    monkeypatch.setattr(M, "_broadcast_llama_state_if_changed", lambda aid: None)
    edges = []
    real_mark_offline = st.mark_offline

    def _record(prov, aid):
        out = real_mark_offline(prov, aid)
        edges.append((prov, aid, out))
        return out
    monkeypatch.setattr(st, "mark_offline", _record)
    M._offline_sweep_once(time.time())
    assert st.get("llama", "a1")["sample"]["llama"]["pulled_at"] is not None
    assert ("llama", "a1", True) in edges


def test_payload_ignores_a_junk_or_future_pulled_at(monkeypatch):
    monkeypatch.setattr(M.provider_state.STORE, "get",
                        lambda k, a: _wrap({"state": "awake", "pulled_at": float("inf")}))
    p = M._build_llama_state_payload("a1")
    assert p["age_s"] >= 0 and p["stale"] is False
    monkeypatch.setattr(M.provider_state.STORE, "get",
                        lambda k, a: _wrap({"state": "awake", "pulled_at": "soon"}, age=40))
    assert M._build_llama_state_payload("a1")["stale"] is True
    monkeypatch.setattr(M.provider_state.STORE, "get",
                        lambda k, a: _wrap({"state": "awake", "pulled_at": time.time() + 60}, age=40))
    p = M._build_llama_state_payload("a1")
    assert p["age_s"] == 0.0 and p["agent_online"] is False


def test_pull_llama_state_rate_limited_to_one_call_per_30s(monkeypatch):
    st = provider_state._ProviderSampleStore()
    monkeypatch.setattr(M.provider_state, "STORE", st)
    st.put("llama", "a1", {"llama": {"state": "awake"}})
    calls = []
    _stub_pull_agent(monkeypatch, calls)
    monkeypatch.setattr(M, "_broadcast_llama_state_if_changed", lambda aid: None)
    M._pull_llama_state_if_stale("a1")
    M._pull_llama_state_if_stale("a1")
    assert len(calls) == 1


def test_sweep_budgets_pulls_per_pass(monkeypatch):
    st = provider_state._ProviderSampleStore()
    monkeypatch.setattr(M.provider_state, "STORE", st)
    for aid in ("a1", "a2", "a3"):
        st.put("llama", aid, {"llama": {"state": "awake"}})
        st._samples["llama"][aid]["last_seen"] -= 40
    monkeypatch.setattr(M, "_broadcast_llama_state_if_changed", lambda aid: None)
    calls = []
    monkeypatch.setattr(M, "_pull_llama_state_if_stale", lambda aid: calls.append(aid))
    M._offline_sweep_once(time.time())
    assert len(calls) == 2


def _capture_perf_target(monkeypatch):
    """Records the agent_id proxy_to_primary was pinned to for /api/benchmark/perf-mode."""
    seen = {}

    def _fake(kind, method, path, **kw):
        seen.update(kind=kind, method=method, path=path, **kw)
        return {"ok": True}

    monkeypatch.setattr(M.proxies, "proxy_to_primary", _fake)
    monkeypatch.setattr(M.agent_registry, "default_agent_id_for", lambda k: "d1")
    return seen


def test_perf_mode_pins_the_requested_agent(monkeypatch):
    seen = _capture_perf_target(monkeypatch)
    with M.app.test_request_context("/api/benchmark/perf-mode?agent=a9",
                                    method="POST", json={"mode": "auto"}):
        M.benchmark_perf_mode()
    assert seen["agent_id"] == "a9"
    assert seen["path"] == "/llama/bench/perf-mode"
    assert seen["json"] == {"mode": "auto"}


def test_perf_mode_falls_back_to_the_default_agent_not_pool_round_robin(monkeypatch):
    seen = _capture_perf_target(monkeypatch)
    with M.app.test_request_context("/api/benchmark/perf-mode",
                                    method="POST", json={"mode": "performance"}):
        M.benchmark_perf_mode()
    assert seen["agent_id"] == "d1"


def test_perf_mode_404s_when_no_llama_agent_is_configured(monkeypatch):
    called = []
    monkeypatch.setattr(M.proxies, "proxy_to_primary",
                        lambda *a, **kw: called.append(a) or {"ok": True})
    monkeypatch.setattr(M.agent_registry, "default_agent_id_for", lambda k: None)
    with M.app.test_request_context("/api/benchmark/perf-mode",
                                    method="POST", json={"mode": "performance"}):
        body, code = M.benchmark_perf_mode()
    assert code == 404 and body.get_json()["ok"] is False
    assert "no llama agent" in body.get_json()["error"] and called == []


def test_power_changes_alone_fan_out_over_sse():
    assert "power" in M._LLAMA_STATE_FP_KEYS
    import queue
    store = provider_state._ProviderSampleStore()
    q = queue.Queue()
    store.subscribe("llama", "a1", q)
    base = {"state": "sleeping", "aggregate": "sleeping", "model": "m", "agent_online": True, "stale": False,
            "port": 8080, "power": {"applied": "powersave", "owner": "policy", "outcome": "verified"}}
    store.broadcast_if_changed("llama", "a1", base, fingerprint_keys=M._LLAMA_STATE_FP_KEYS)
    store.broadcast_if_changed("llama", "a1", dict(base), fingerprint_keys=M._LLAMA_STATE_FP_KEYS)
    changed = dict(base, power={"applied": "performance", "owner": "manual", "outcome": "verified"})
    store.broadcast_if_changed("llama", "a1", changed, fingerprint_keys=M._LLAMA_STATE_FP_KEYS)
    assert q.qsize() == 2
