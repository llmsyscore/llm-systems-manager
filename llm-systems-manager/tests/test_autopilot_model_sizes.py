"""#472/#474: production model-size discovery — fan-out, merge, TTL cache."""
from __future__ import annotations

import pytest

import agent_registry
import autopilot as ap
import autopilot_planner as pl

A1, A2 = "a" * 32, "b" * 32


@pytest.fixture(autouse=True)
def _reset_sizes_cache():
    """Module-level cache/guard must not leak state between tests."""
    ap._sizes_cache["ts"] = 0.0
    ap._sizes_cache["sizes"] = {}
    ap._sizes_refreshing = False
    yield
    ap._sizes_cache["ts"] = 0.0
    ap._sizes_cache["sizes"] = {}
    ap._sizes_refreshing = False


# ── _llama_agent_sizes: bytes -> MB, isolated failure ─────────────────

def test_llama_agent_sizes_converts_bytes_to_mb(monkeypatch):
    class _Resp:
        ok = True
        def json(self):
            return {"ok": True, "sizes": {"m1": 8 * 1024 * 1024 * 1024}}
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (_Resp(), [], None))
    assert ap._llama_agent_sizes({"hostname": "h1", "token": "t"}) == {"m1": 8192}


def test_llama_agent_sizes_empty_on_no_response(monkeypatch):
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (None, [], "no callback URL recorded"))
    assert ap._llama_agent_sizes({"hostname": "h1", "token": "t"}) == {}


def test_llama_agent_sizes_empty_on_old_agent_404(monkeypatch):
    class _Resp:
        ok = False
        status_code = 404
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (_Resp(), [], None))
    assert ap._llama_agent_sizes({"hostname": "h1", "token": "t"}) == {}


def test_llama_agent_sizes_skips_unparseable_entries(monkeypatch):
    class _Resp:
        ok = True
        def json(self):
            return {"sizes": {"good": 1_048_576, "bad": "not-a-number"}}
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (_Resp(), [], None))
    assert ap._llama_agent_sizes({"hostname": "h1"}) == {"good": 1}


# ── _parse_lms_size_mb: string ('8.00 GB') or raw bytes ────────────────

@pytest.mark.parametrize("raw,expected", [
    ("8.00 GB", 8192),
    ("512 MB", 512),
    (4 * 1024 * 1024, 4),
    ("garbage", None),
    (None, None),
    ("", None),
    (-5, None),
])
def test_parse_lms_size_mb(raw, expected):
    assert ap._parse_lms_size_mb(raw) == expected


# ── _refresh_model_sizes: multi-agent max + provider-key format ────────

def test_refresh_model_sizes_takes_max_across_llama_agents(monkeypatch):
    agents = {
        A1: {"status": "approved", "capabilities": {"llama": True}, "hostname": "h1"},
        A2: {"status": "approved", "capabilities": {"llama": True}, "hostname": "h2"},
    }
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})

    def _fake_request(method, agent, path, **kw):
        class _Resp:
            ok = True
            def json(self_inner):
                mb = 4096 if agent["hostname"] == "h1" else 8192
                return {"sizes": {"m1": mb * 1024 * 1024}}
        return _Resp(), [], None
    monkeypatch.setattr(agent_registry, "agent_request", _fake_request)

    merged = ap._refresh_model_sizes()
    assert merged == {"llama:m1": 8192}


def test_refresh_model_sizes_skips_agents_without_llama_cap_or_not_approved(monkeypatch):
    agents = {
        A1: {"status": "approved", "capabilities": {"vllm": True}, "hostname": "h1"},
        A2: {"status": "pending", "capabilities": {"llama": True}, "hostname": "h2"},
    }
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})
    calls = []
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (calls.append(1), (None, [], "x"))[1])
    assert ap._refresh_model_sizes() == {}
    assert calls == []


def test_refresh_model_sizes_includes_lms_from_pushed_state(monkeypatch):
    import provider_state
    agents = {A1: {"status": "approved", "capabilities": {"lms": True}, "hostname": "h1"}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})
    provider_state.STORE.put("lms", A1, {"ps": [
        {"model": "qwen2.5-7b", "status": "LOADED", "size": "6.50 GB"}]})
    try:
        merged = ap._refresh_model_sizes()
    finally:
        provider_state.STORE.evict(A1)
    assert merged == {"lms:qwen2.5-7b": 6656}


# ── _prod_model_sizes: TTL cache, in-flight guard, stale-on-failure ────

def test_prod_model_sizes_no_refetch_within_ttl(monkeypatch):
    calls = []
    ap._sizes_cache["ts"] = ap.time.time()
    ap._sizes_cache["sizes"] = {"llama:m1": 4096}
    monkeypatch.setattr(ap, "_refresh_model_sizes", lambda: (calls.append(1), {})[1])
    assert ap._prod_model_sizes() == {"llama:m1": 4096}
    assert ap._prod_model_sizes() == {"llama:m1": 4096}
    assert calls == []


def test_prod_model_sizes_refetches_after_ttl_expires(monkeypatch):
    calls = []
    ap._sizes_cache["ts"] = ap.time.time() - ap._SIZES_CACHE_TTL_S - 1
    ap._sizes_cache["sizes"] = {"llama:old": 1}
    monkeypatch.setattr(ap, "_refresh_model_sizes",
                        lambda: (calls.append(1), {"llama:new": 2})[1])
    assert ap._prod_model_sizes() == {"llama:new": 2}
    assert len(calls) == 1
    # Cache is fresh now — a second call must not fan out again.
    assert ap._prod_model_sizes() == {"llama:new": 2}
    assert len(calls) == 1


def test_prod_model_sizes_inflight_guard_short_circuits(monkeypatch):
    ap._sizes_cache["ts"] = 0.0
    ap._sizes_cache["sizes"] = {"llama:stale": 9}
    ap._sizes_refreshing = True
    calls = []
    monkeypatch.setattr(ap, "_refresh_model_sizes", lambda: (calls.append(1), {})[1])
    assert ap._prod_model_sizes() == {"llama:stale": 9}
    assert calls == []
    assert ap._sizes_refreshing is True     # untouched by the short-circuiting caller


def test_prod_model_sizes_serves_stale_cache_on_refresh_failure(monkeypatch):
    ap._sizes_cache["ts"] = 0.0
    ap._sizes_cache["sizes"] = {"llama:old": 42}

    def _boom():
        raise RuntimeError("agent fleet unreachable")
    monkeypatch.setattr(ap, "_refresh_model_sizes", _boom)
    assert ap._prod_model_sizes() == {"llama:old": 42}
    assert ap._sizes_refreshing is False     # guard released so a later call can retry


def test_prod_model_sizes_recovers_after_failed_refresh(monkeypatch):
    ap._sizes_cache["ts"] = 0.0
    ap._sizes_cache["sizes"] = {}
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("boom")
        return {"llama:m1": 1234}
    monkeypatch.setattr(ap, "_refresh_model_sizes", _flaky)
    assert ap._prod_model_sizes() == {}
    ap._sizes_cache["ts"] = 0.0                # force another stale-triggered refresh
    assert ap._prod_model_sizes() == {"llama:m1": 1234}


# ── Planner proof: production-shaped sizes dep -> build_observed -> plan() ──

def test_build_observed_prod_shaped_sizes_unblocks_planner_load():
    """A model_sizes dep shaped like _prod_model_sizes()'s output
    ({'provider:model': int_mb}) must let plan() place an unloaded model."""
    def fake_agents():
        return {"agents": {A1: {"capabilities": {"llama": True},
                                "status": "approved"}}}
    def fake_liveness(agent):
        return "live"
    def fake_snapshot(prov, agent_id):
        return {"sample": {
            "llama": {"state": "awake", "model": None},
            "gpu": {"vram_total_bytes": 24 * 1024 ** 3, "vram_used_mb": 1000},
        }}
    def fake_saturation(prov, agent_id):
        return {"value": None}
    def fake_sizes():
        return {"llama:m1": 8000}    # production shape: provider:model -> int MB

    deps = {"agents": fake_agents, "liveness": fake_liveness,
            "provider_snapshot": fake_snapshot, "saturation": fake_saturation,
            "model_sizes": fake_sizes}
    observed = ap.build_observed(deps)
    assert observed["model_sizes_mb"] == {"llama:m1": 8000}

    desired = {"enabled": True, "hosts": {}, "entries": [
        {"model": "m1", "provider": "llama", "placement": "auto",
         "failover": "semi", "priority": 100, "min_replicas": 1, "max_replicas": 1}]}
    ledger = {"last_action_ts": {}, "placed_at": {}, "in_flight_migrations": 0,
              "backoff_until": {}}
    actions = pl.plan(desired, observed, ledger, now=1000.0)
    assert len(actions) == 1
    assert actions[0].kind == "load" and actions[0].model == "m1"
    assert actions[0].agent_id == A1


def test_build_observed_missing_size_still_blocks_placement():
    """Sanity control: without a size entry the same setup still refuses
    to place (VRAM-fit check fails closed) — proves the prior test's
    green result comes from the sizes dep, not something else."""
    def fake_agents():
        return {"agents": {A1: {"capabilities": {"llama": True},
                                "status": "approved"}}}
    def fake_liveness(agent):
        return "live"
    def fake_snapshot(prov, agent_id):
        return {"sample": {
            "llama": {"state": "awake", "model": None},
            "gpu": {"vram_total_bytes": 24 * 1024 ** 3, "vram_used_mb": 1000},
        }}
    def fake_saturation(prov, agent_id):
        return {"value": None}

    deps = {"agents": fake_agents, "liveness": fake_liveness,
            "provider_snapshot": fake_snapshot, "saturation": fake_saturation,
            "model_sizes": lambda: {}}
    observed = ap.build_observed(deps)
    desired = {"enabled": True, "hosts": {}, "entries": [
        {"model": "m1", "provider": "llama", "placement": "auto",
         "failover": "semi", "priority": 100, "min_replicas": 1, "max_replicas": 1}]}
    ledger = {"last_action_ts": {}, "placed_at": {}, "in_flight_migrations": 0,
              "backoff_until": {}}
    actions = pl.plan(desired, observed, ledger, now=1000.0)
    assert actions == []
