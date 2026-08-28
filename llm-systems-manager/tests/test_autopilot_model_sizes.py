"""#472/#474/#475: production model-size + gpu_layers discovery — fan-out,
merge, TTL cache, offload-aware observer fields."""
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
    ap._sizes_cache["layers"] = {}
    ap._sizes_cache["refreshing"] = False
    yield
    ap._sizes_cache["ts"] = 0.0
    ap._sizes_cache["sizes"] = {}
    ap._sizes_cache["layers"] = {}
    ap._sizes_cache["refreshing"] = False


# ── _llama_agent_sizes_and_layers: bytes -> MB + meta.gpu_layers ───────

def test_llama_agent_sizes_and_layers_converts_bytes_to_mb(monkeypatch):
    class _Resp:
        ok = True
        def json(self):
            return {"ok": True, "sizes": {"m1": 8 * 1024 * 1024 * 1024},
                    "meta": {"m1": {"gpu_layers": 0}}}
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (_Resp(), [], None))
    sizes, layers = ap._llama_agent_sizes_and_layers({"hostname": "h1", "token": "t"})
    assert sizes == {"m1": 8192}
    assert layers == {"m1": 0}


def test_llama_agent_sizes_and_layers_empty_on_no_response(monkeypatch):
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (None, [], "no callback URL recorded"))
    assert ap._llama_agent_sizes_and_layers({"hostname": "h1", "token": "t"}) == ({}, {})


def test_llama_agent_sizes_and_layers_empty_on_old_agent_404(monkeypatch):
    class _Resp:
        ok = False
        status_code = 404
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (_Resp(), [], None))
    assert ap._llama_agent_sizes_and_layers({"hostname": "h1", "token": "t"}) == ({}, {})


def test_llama_agent_sizes_skips_unparseable_size_entries(monkeypatch):
    class _Resp:
        ok = True
        def json(self):
            return {"sizes": {"good": 1_048_576, "bad": "not-a-number"}}
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (_Resp(), [], None))
    sizes, layers = ap._llama_agent_sizes_and_layers({"hostname": "h1"})
    assert sizes == {"good": 1}
    assert layers == {}


def test_llama_agent_sizes_and_layers_degrades_on_agent_missing_meta(monkeypatch):
    """Review fix #475: an OLD agent's response has no 'meta' key at all —
    must degrade to gpu_layers-unknown for every model, never raise."""
    class _Resp:
        ok = True
        def json(self):
            return {"ok": True, "sizes": {"m1": 4 * 1024 * 1024}}  # no "meta"
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (_Resp(), [], None))
    sizes, layers = ap._llama_agent_sizes_and_layers({"hostname": "h1"})
    assert sizes == {"m1": 4}
    assert layers == {}


def test_llama_agent_sizes_and_layers_null_gpu_layers_and_malformed_meta(monkeypatch):
    class _Resp:
        ok = True
        def json(self):
            return {"sizes": {"m1": 1_048_576, "m2": 1_048_576},
                    "meta": {"m1": {"gpu_layers": None}, "m2": "not-a-dict"}}
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (_Resp(), [], None))
    sizes, layers = ap._llama_agent_sizes_and_layers({"hostname": "h1"})
    assert sizes == {"m1": 1, "m2": 1}
    assert layers == {"m1": None}          # m2's malformed meta entry is skipped


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
            def json(self):
                mb = 4096 if agent["hostname"] == "h1" else 8192
                return {"sizes": {"m1": mb * 1024 * 1024}}
        return _Resp(), [], None
    monkeypatch.setattr(agent_registry, "agent_request", _fake_request)

    sizes, layers = ap._refresh_model_sizes()
    assert sizes == {"llama:m1": 8192}
    assert layers == {}


def test_refresh_model_sizes_gpu_layers_first_seen_wins_on_conflict(monkeypatch):
    """Documented tiebreak: gpu_layers is a categorical config value, not a
    quantity, so a later agent's differing report never overwrites the first."""
    agents = {
        A1: {"status": "approved", "capabilities": {"llama": True}, "hostname": "h1"},
        A2: {"status": "approved", "capabilities": {"llama": True}, "hostname": "h2"},
    }
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})

    def _fake_request(method, agent, path, **kw):
        gl = 0 if agent["hostname"] == "h1" else 32
        class _Resp:
            ok = True
            def json(self):
                return {"sizes": {"m1": 1_048_576},
                        "meta": {"m1": {"gpu_layers": gl}}}
        return _Resp(), [], None
    monkeypatch.setattr(agent_registry, "agent_request", _fake_request)

    # dict iteration order == insertion order: h1 (A1) is seen first.
    sizes, layers = ap._refresh_model_sizes()
    assert sizes == {"llama:m1": 1}
    assert layers == {"llama:m1": 0}


def test_refresh_model_sizes_skips_agents_without_llama_cap_or_not_approved(monkeypatch):
    agents = {
        A1: {"status": "approved", "capabilities": {"vllm": True}, "hostname": "h1"},
        A2: {"status": "pending", "capabilities": {"llama": True}, "hostname": "h2"},
    }
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})
    calls = []
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (calls.append(1), (None, [], "x"))[1])
    assert ap._refresh_model_sizes() == ({}, {})
    assert calls == []


def test_refresh_model_sizes_includes_lms_from_pushed_state(monkeypatch):
    import provider_state
    agents = {A1: {"status": "approved", "capabilities": {"lms": True}, "hostname": "h1"}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})
    provider_state.STORE.put("lms", A1, {"ps": [
        {"model": "qwen2.5-7b", "status": "LOADED", "size": "6.50 GB"}]})
    try:
        sizes, layers = ap._refresh_model_sizes()
    finally:
        provider_state.STORE.evict(A1)
    assert sizes == {"lms:qwen2.5-7b": 6656}
    assert layers == {}          # lms never contributes gpu_layers


# ── Review fix: a malformed lms 'ps' row must not raise (#472 follow-up) ──

class _EvilRow(dict):
    """A dict-shaped row whose .get() itself blows up — exercises the
    try/except, not just the isinstance(row, dict) guard."""
    def get(self, key, default=None):
        raise RuntimeError("row.get() boom")


def test_lms_agent_sizes_skips_malformed_rows_without_raising():
    import provider_state
    agents = {
        A1: {"status": "approved", "capabilities": {"lms": True}, "hostname": "h1"},
        A2: {"status": "approved", "capabilities": {"lms": True}, "hostname": "h2"},
    }
    provider_state.STORE.put("lms", A1, {"ps": [
        "not-a-dict", 42, None, _EvilRow({"model": "evil", "size": "1 GB"}),
        {"model": "good-model", "status": "LOADED", "size": "2.00 GB"},
    ]})
    provider_state.STORE.put("lms", A2, {"ps": [
        {"model": "other-model", "status": "LOADED", "size": "1.00 GB"}]})
    try:
        out = ap._lms_agent_sizes(agents)          # must not raise
    finally:
        provider_state.STORE.evict(A1)
        provider_state.STORE.evict(A2)
    assert out == {"good-model": 2048, "other-model": 1024}


def test_refresh_model_sizes_survives_malformed_lms_row_alongside_llama(monkeypatch):
    """Other providers' sizes are still returned when one lms row is garbage."""
    import provider_state
    agents = {
        A1: {"status": "approved", "capabilities": {"llama": True}, "hostname": "h1"},
        A2: {"status": "approved", "capabilities": {"lms": True}, "hostname": "h2"},
    }
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})

    class _Resp:
        ok = True
        def json(self):
            return {"sizes": {"m1": 4096 * 1024 * 1024}}
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (_Resp(), [], None))
    provider_state.STORE.put("lms", A2, {"ps": ["not-a-dict", None]})
    try:
        sizes, _layers = ap._refresh_model_sizes()   # must not raise
    finally:
        provider_state.STORE.evict(A2)
    assert sizes == {"llama:m1": 4096}


# ── _prod_model_sizes / _prod_model_gpu_layers: shared TTL cache ───────

def test_prod_model_sizes_no_refetch_within_ttl(monkeypatch):
    calls = []
    ap._sizes_cache["ts"] = ap.time.time()
    ap._sizes_cache["sizes"] = {"llama:m1": 4096}
    monkeypatch.setattr(ap, "_refresh_model_sizes", lambda: (calls.append(1), ({}, {}))[1])
    assert ap._prod_model_sizes() == {"llama:m1": 4096}
    assert ap._prod_model_sizes() == {"llama:m1": 4096}
    assert calls == []


def test_prod_model_sizes_refetches_after_ttl_expires(monkeypatch):
    calls = []
    ap._sizes_cache["ts"] = ap.time.time() - ap._SIZES_CACHE_TTL_S - 1
    ap._sizes_cache["sizes"] = {"llama:old": 1}
    monkeypatch.setattr(ap, "_refresh_model_sizes",
                        lambda: (calls.append(1), ({"llama:new": 2}, {}))[1])
    assert ap._prod_model_sizes() == {"llama:new": 2}
    assert len(calls) == 1
    # Cache is fresh now — a second call must not fan out again.
    assert ap._prod_model_sizes() == {"llama:new": 2}
    assert len(calls) == 1


def test_prod_model_gpu_layers_shares_cache_no_double_fanout(monkeypatch):
    """Calling sizes then gpu_layers in the same cycle triggers exactly one
    fan-out — gpu_layers never re-fetches what sizes already refreshed."""
    calls = []
    ap._sizes_cache["ts"] = 0.0

    def _fake_refresh():
        calls.append(1)
        return {"llama:m1": 4096}, {"llama:m1": 0}
    monkeypatch.setattr(ap, "_refresh_model_sizes", _fake_refresh)

    assert ap._prod_model_sizes() == {"llama:m1": 4096}
    assert ap._prod_model_gpu_layers() == {"llama:m1": 0}
    assert len(calls) == 1


def test_prod_model_gpu_layers_triggers_refresh_when_called_first(monkeypatch):
    """Order independence: gpu_layers can be the one that triggers the
    (only) fan-out if sizes hasn't been called yet this cycle."""
    calls = []
    ap._sizes_cache["ts"] = 0.0

    def _fake_refresh():
        calls.append(1)
        return {"llama:m1": 4096}, {"llama:m1": 32}
    monkeypatch.setattr(ap, "_refresh_model_sizes", _fake_refresh)

    assert ap._prod_model_gpu_layers() == {"llama:m1": 32}
    assert ap._prod_model_sizes() == {"llama:m1": 4096}
    assert len(calls) == 1


def test_prod_model_sizes_inflight_guard_short_circuits(monkeypatch):
    ap._sizes_cache["ts"] = 0.0
    ap._sizes_cache["sizes"] = {"llama:stale": 9}
    ap._sizes_cache["refreshing"] = True
    calls = []
    monkeypatch.setattr(ap, "_refresh_model_sizes", lambda: (calls.append(1), ({}, {}))[1])
    assert ap._prod_model_sizes() == {"llama:stale": 9}
    assert calls == []
    assert ap._sizes_cache["refreshing"] is True     # untouched by the short-circuiting caller


def test_prod_model_sizes_serves_stale_cache_on_refresh_failure(monkeypatch):
    ap._sizes_cache["ts"] = 0.0
    ap._sizes_cache["sizes"] = {"llama:old": 42}

    def _boom():
        raise RuntimeError("agent fleet unreachable")
    monkeypatch.setattr(ap, "_refresh_model_sizes", _boom)
    assert ap._prod_model_sizes() == {"llama:old": 42}
    assert ap._sizes_cache["refreshing"] is False     # guard released so a later call can retry


def test_prod_model_sizes_backs_off_after_failed_refresh_within_ttl(monkeypatch):
    """#472 review fix: any exception during a refresh (e.g. a malformed
    upstream body) must still advance ts, so a second call inside the TTL
    window serves stale data instead of re-running the full agent fan-out —
    reproduces the prior retry-storm (two calls -> two full fan-outs)."""
    ap._sizes_cache["ts"] = 0.0
    ap._sizes_cache["sizes"] = {"llama:old": 42}
    agents = {A1: {"status": "approved", "capabilities": {"llama": True}, "hostname": "h1"}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})

    calls = []

    def _fake_request(method, agent, path, **kw):
        calls.append(1)
        class _Resp:
            ok = True
            def json(self):
                return {"sizes": "not-a-dict"}      # malformed upstream body -> raises
        return _Resp(), [], None
    monkeypatch.setattr(agent_registry, "agent_request", _fake_request)

    assert ap._prod_model_sizes() == {"llama:old": 42}
    assert ap._prod_model_sizes() == {"llama:old": 42}   # no manual ts reset
    assert len(calls) == 1        # second call served stale, no re-fan-out


def test_prod_model_sizes_recovers_after_failed_refresh(monkeypatch):
    ap._sizes_cache["ts"] = 0.0
    ap._sizes_cache["sizes"] = {}
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("boom")
        return {"llama:m1": 1234}, {"llama:m1": None}
    monkeypatch.setattr(ap, "_refresh_model_sizes", _flaky)
    assert ap._prod_model_sizes() == {}
    ap._sizes_cache["ts"] = 0.0                # force another stale-triggered refresh
    assert ap._prod_model_sizes() == {"llama:m1": 1234}
    assert ap._prod_model_gpu_layers() == {"llama:m1": None}


# ── build_observed: ram_free_mb + model_gpu_layers observer fields ─────

def _base_deps(gpu_layers=None, ram=None, vram_used_mb=1000):
    def fake_agents():
        return {"agents": {A1: {"capabilities": {"llama": True},
                                "status": "approved"}}}
    def fake_liveness(agent):
        return "live"
    def fake_snapshot(prov, agent_id):
        sample = {
            "llama": {"state": "awake", "model": None},
            "gpu": {"vram_total_bytes": 24 * 1024 ** 3, "vram_used_mb": vram_used_mb},
        }
        if ram is not None:
            sample["ram"] = ram
        return {"sample": sample}
    def fake_saturation(prov, agent_id):
        return {"value": None}
    return {"agents": fake_agents, "liveness": fake_liveness,
            "provider_snapshot": fake_snapshot, "saturation": fake_saturation,
            "model_sizes": lambda: {"llama:m1": 8000},
            "model_gpu_layers": lambda: gpu_layers or {}}


def test_build_observed_ram_free_mb_from_pushed_ram_available_bytes():
    deps = _base_deps(ram={"total_bytes": 32 * 1024 ** 3,
                           "available_bytes": 16 * 1024 ** 3})
    observed = ap.build_observed(deps)
    assert observed["agents"][A1]["ram_free_mb"] == 16384


def test_build_observed_ram_free_mb_zero_when_absent():
    deps = _base_deps(ram=None)          # no "ram" key pushed at all
    observed = ap.build_observed(deps)
    assert observed["agents"][A1]["ram_free_mb"] == 0


def test_build_observed_exposes_model_gpu_layers():
    deps = _base_deps(gpu_layers={"llama:m1": 0})
    observed = ap.build_observed(deps)
    assert observed["model_gpu_layers"] == {"llama:m1": 0}


# ── Planner proof: production-shaped sizes dep -> build_observed -> plan() ──

def test_build_observed_prod_shaped_sizes_unblocks_planner_load():
    """A model_sizes dep shaped like _prod_model_sizes()'s output
    ({'provider:model': int_mb}) must let plan() place an unloaded model."""
    deps = _base_deps()
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
    deps = _base_deps()
    deps["model_sizes"] = lambda: {}
    observed = ap.build_observed(deps)
    desired = {"enabled": True, "hosts": {}, "entries": [
        {"model": "m1", "provider": "llama", "placement": "auto",
         "failover": "semi", "priority": 100, "min_replicas": 1, "max_replicas": 1}]}
    ledger = {"last_action_ts": {}, "placed_at": {}, "in_flight_migrations": 0,
              "backoff_until": {}}
    actions = pl.plan(desired, observed, ledger, now=1000.0)
    assert actions == []


# ── #474: entry size_mb overrides -> build_observed merge ──────────────

def test_build_observed_merges_size_overrides_over_discovered():
    deps = _base_deps()
    deps["size_overrides"] = lambda: {"vllm:big": 15000, "llama:m1": 9000}
    observed = ap.build_observed(deps)
    # New keys are added; an operator value beats the discovered 8000.
    assert observed["model_sizes_mb"] == {"llama:m1": 9000, "vllm:big": 15000}


def test_build_observed_without_size_overrides_dep_unchanged():
    observed = ap.build_observed(_base_deps())
    assert observed["model_sizes_mb"] == {"llama:m1": 8000}


def test_state_size_overrides_reads_entry_size_mb(monkeypatch):
    st = {"enabled": True, "hosts": {}, "entries": [
        {"model": "big", "provider": "vllm", "size_mb": 15000},
        {"model": "m1", "provider": "llama"},
        {"model": "weird", "provider": "lms", "size_mb": "not-int"}]}
    monkeypatch.setattr(ap, "get_state", lambda: st)
    assert ap._state_size_overrides() == {"vllm:big": 15000}


def test_size_override_unblocks_vllm_placement_end_to_end():
    """#474 proof: a vLLM entry (no discovery source) becomes placeable
    once its entry-declared size_mb flows through build_observed."""
    def fake_agents():
        return {"agents": {A1: {"capabilities": {"vllm": True},
                                "status": "approved"}}}
    deps = {"agents": fake_agents, "liveness": lambda a: "live",
            "provider_snapshot": lambda prov, aid: {"sample": {
                "system": {"gpu": {"vram_total_bytes": 24 * 1024 ** 3,
                                   "vram_used_mb": 1000}}}},
            "saturation": lambda prov, aid: {"value": None},
            "model_sizes": lambda: {},
            "model_gpu_layers": lambda: {},
            "size_overrides": lambda: {"vllm:qwen3-14b": 15000}}
    observed = ap.build_observed(deps)
    desired = {"enabled": True, "hosts": {}, "entries": [
        {"model": "qwen3-14b", "provider": "vllm", "placement": "auto",
         "failover": "semi", "priority": 100, "min_replicas": 1,
         "max_replicas": 1, "size_mb": 15000}]}
    ledger = {"last_action_ts": {}, "placed_at": {}, "in_flight_migrations": 0,
              "backoff_until": {}}
    actions = pl.plan(desired, observed, ledger, now=1000.0)
    assert len(actions) == 1
    assert actions[0].kind == "load" and actions[0].provider == "vllm"
    assert actions[0].auto is False          # vLLM never auto-executes

    # Control: same setup minus the override stays blocked.
    deps["size_overrides"] = lambda: {}
    blocked = pl.plan(desired, ap.build_observed(deps), ledger, now=1000.0)
    assert blocked == []


def test_build_observed_prod_shaped_ram_path_unblocks_cpu_served_model():
    """End-to-end offload-aware proof (#475): a llama model with gpu_layers=0
    and zero VRAM but ample pushed RAM still gets placed, via build_observed's
    real ram_free_mb + model_gpu_layers wiring — not a hand-built observed dict."""
    deps = _base_deps(gpu_layers={"llama:m1": 0}, vram_used_mb=24 * 1024,
                      ram={"total_bytes": 32 * 1024 ** 3,
                           "available_bytes": 16 * 1024 ** 3})
    observed = ap.build_observed(deps)
    assert observed["agents"][A1]["vram_free_mb"] == 0
    assert observed["agents"][A1]["ram_free_mb"] == 16384

    desired = {"enabled": True, "hosts": {}, "entries": [
        {"model": "m1", "provider": "llama", "placement": "auto",
         "failover": "semi", "priority": 100, "min_replicas": 1, "max_replicas": 1}]}
    ledger = {"last_action_ts": {}, "placed_at": {}, "in_flight_migrations": 0,
              "backoff_until": {}}
    actions = pl.plan(desired, observed, ledger, now=1000.0)
    assert len(actions) == 1
    assert actions[0].kind == "load" and actions[0].agent_id == A1


def test_build_observed_exposes_route_pins_from_global():
    deps = _base_deps()
    deps["agents"] = lambda: {"agents": {A1: {"capabilities": {"llama": True},
                                              "status": "approved"}},
                              "global": {"llama_model_pins": {"m1": A1}}}
    observed = ap.build_observed(deps)
    assert observed["route_pins"]["llama"] == {"m1": A1}
    assert observed["route_pins"].get("vllm", {}) == {}
