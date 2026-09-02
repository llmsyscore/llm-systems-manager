"""#214: gateway routing, failover, error shape, models merge."""
import json
import types

from flask import Flask

import gateway


class FakeResp:
    def __init__(self, status=200, payload=None, ctype="application/json"):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode()
        self.headers = {"content-type": ctype}

    def json(self):
        return self._payload


def _client():
    app = Flask(__name__)
    gateway.register_routes(app, types.SimpleNamespace())
    return app.test_client()


def test_no_candidates_returns_nonretryable_no_backend(monkeypatch):
    # #650: zero candidates is a config state — 404/no_backend, not a
    # retryable 503.
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [])
    r = _client().post("/api/gateway/v1/chat/completions", json={"model": "x"})
    assert r.status_code == 404
    err = r.get_json()["error"]
    assert err["code"] == 404 and err["type"] == "no_backend"


def test_all_candidates_failed_stays_retryable_503(monkeypatch):
    # #650: candidates existed but all failed — transient, stays 503.
    a1 = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [a1])
    monkeypatch.setattr(gateway, "_forward_json", lambda agent, p, b: (None, "refused"))
    r = _client().post("/api/gateway/v1/chat/completions", json={"model": "x"})
    assert r.status_code == 503
    assert r.get_json()["error"]["type"] == "unavailable"


def test_invalid_body_400():
    r = _client().post("/api/gateway/v1/chat/completions",
                       data="notjson", content_type="application/json")
    assert r.status_code == 400


def test_disabled_503(monkeypatch):
    monkeypatch.setattr(gateway, "_gw_enabled", lambda: False)
    r = _client().post("/api/gateway/v1/chat/completions", json={})
    assert r.status_code == 503


def test_failover_to_second_agent(monkeypatch):
    a1 = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    a2 = {"agent_id": "b" * 32, "hostname": "h2", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [a1, a2])
    calls = []

    def fake_forward(agent, path, body):
        calls.append(agent["hostname"])
        return (None, "refused") if agent is a1 else (FakeResp(200, {"id": "c1"}), None)

    monkeypatch.setattr(gateway, "_forward_json", fake_forward)
    r = _client().post("/api/gateway/v1/chat/completions", json={"model": "m"})
    assert r.status_code == 200 and calls == ["h1", "h2"]
    assert r.headers["X-Proxied-To"].endswith("@h2")


def test_502_from_agent_fails_over(monkeypatch):
    a1 = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    a2 = {"agent_id": "b" * 32, "hostname": "h2", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [a1, a2])
    monkeypatch.setattr(gateway, "_forward_json", lambda agent, p, b:
                        (FakeResp(502), None) if agent is a1 else (FakeResp(200, {"ok": 1}), None))
    r = _client().post("/api/gateway/v1/completions", json={})
    assert r.status_code == 200


def test_candidates_order_and_dedupe(monkeypatch):
    prim = {"agent_id": "p1", "hostname": "hp"}
    pool_b = {"agent_id": "p2", "hostname": "h2"}
    monkeypatch.setattr(gateway.proxies, "_resolve_target",
                        lambda pk, m, a, allow_pool=True: (prim, None))
    monkeypatch.setattr(gateway.agent_registry, "load_agents",
                        lambda: {"global": {"llama_pool": ["p1", "p2"]}})
    monkeypatch.setattr(gateway.agent_registry, "default_agent_id_for", lambda p: "p2")
    monkeypatch.setattr(gateway.agent_registry, "resolve_agent_by_id",
                        lambda aid, capability=None: {"p1": prim, "p2": pool_b}[aid])
    monkeypatch.setattr(gateway.agent_registry, "agent_liveness", lambda a: "live")
    assert [a["agent_id"] for a in gateway._candidates("m", None)] == ["p1", "p2"]


def test_models_merge_dedupe(monkeypatch):
    _reset_model_index()
    a1 = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    a2 = {"agent_id": "b" * 32, "hostname": "h2", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [a1, a2])
    payloads = {"h1": {"data": [{"id": "m1"}, {"id": "m2"}]},
                "h2": {"data": [{"id": "m2"}, {"id": "m3"}]}}

    def fake_request(method, agent, path, **kw):
        return FakeResp(200, payloads[agent["hostname"]]), [], None

    monkeypatch.setattr(gateway.agent_registry, "agent_request", fake_request)
    r = _client().get("/api/gateway/v1/models")
    ids = [m["id"] for m in r.get_json()["data"]]
    assert ids == ["m1", "m2", "m3"]


def test_models_merge_across_all_providers(monkeypatch):
    """#493: the main /v1/models merges every gateway provider's pool."""
    _reset_model_index()
    agents = {p: {"agent_id": p[0] * 32, "hostname": f"h-{p}", "token": "t"}
              for p in gateway._GATEWAY_PROVIDERS}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agents[p]])
    payloads = {"h-llama": {"data": [{"id": "l1"}]},
                "h-lms": {"data": [{"id": "s1"}]},
                "h-vllm": {"data": [{"id": "v1"}]}}

    def fake_request(method, agent, path, **kw):
        return FakeResp(200, payloads[agent["hostname"]]), [], None

    monkeypatch.setattr(gateway.agent_registry, "agent_request", fake_request)
    assert set(gateway._GATEWAY_PROVIDERS) == {"llama", "lms", "vllm"}
    r = _client().get("/api/gateway/v1/models")
    data = r.get_json()["data"]
    assert {m["id"] for m in data} == {"l1", "s1", "v1"}
    assert {m["provider"] for m in data} == {"llama", "lms", "vllm"}


def test_provider_scoped_models_route_stays_scoped(monkeypatch):
    agents = {p: {"agent_id": p[0] * 32, "hostname": f"h-{p}", "token": "t"}
              for p in gateway._GATEWAY_PROVIDERS}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agents[p]])
    payloads = {"h-llama": {"data": [{"id": "l1"}]},
                "h-lms": {"data": [{"id": "s1"}]},
                "h-vllm": {"data": [{"id": "v1"}]}}

    def fake_request(method, agent, path, **kw):
        return FakeResp(200, payloads[agent["hostname"]]), [], None

    monkeypatch.setattr(gateway.agent_registry, "agent_request", fake_request)
    r = _client().get("/api/gateway/vllm/v1/models")
    assert [m["id"] for m in r.get_json()["data"]] == ["v1"]


def _reset_model_index():
    gateway._model_index["ts"] = 0.0
    gateway._model_index["map"] = {}
    gateway._model_index["entries"] = None


def test_completion_routes_to_owning_provider(monkeypatch):
    """#493: a chat request for an lms-owned model uses the lms passthrough."""
    _reset_model_index()
    monkeypatch.setattr(gateway.agent_registry, "pinned_agent", lambda p, m: None)
    monkeypatch.setattr(gateway, "_refresh_model_index", lambda: {"s1": "lms"})
    seen_providers, seen_paths = [], []

    def fake_candidates(m, a, p="llama"):
        seen_providers.append(p)
        return [{"agent_id": "a" * 32, "hostname": "h1", "token": "t"}]

    def fake_forward(agent, path, body):
        seen_paths.append(path)
        return FakeResp(200, {"ok": 1}), None

    monkeypatch.setattr(gateway, "_candidates", fake_candidates)
    monkeypatch.setattr(gateway, "_forward_json", fake_forward)
    r = _client().post("/api/gateway/v1/chat/completions", json={"model": "s1"})
    assert r.status_code == 200
    assert seen_providers == ["lms"]
    assert seen_paths == ["/lms/openai/chat/completions"]


def test_completion_unknown_model_falls_back_to_llama(monkeypatch):
    _reset_model_index()
    monkeypatch.setattr(gateway.agent_registry, "pinned_agent", lambda p, m: None)
    monkeypatch.setattr(gateway, "_refresh_model_index", lambda: {})
    seen = []

    def fake_candidates(m, a, p="llama"):
        seen.append(p)
        return [{"agent_id": "a" * 32, "hostname": "h1", "token": "t"}]

    monkeypatch.setattr(gateway, "_candidates", fake_candidates)
    monkeypatch.setattr(gateway, "_forward_json",
                        lambda agent, p, b: (FakeResp(200, {"ok": 1}), None))
    r = _client().post("/api/gateway/v1/chat/completions", json={"model": "nope"})
    assert r.status_code == 200 and seen == ["llama"]


def test_completion_pin_selects_provider(monkeypatch):
    _reset_model_index()
    monkeypatch.setattr(
        gateway.agent_registry, "pinned_agent",
        lambda p, m: {"agent_id": "x"} if (p, m) == ("lms", "pinned-model") else None)
    seen = []

    def fake_candidates(m, a, p="llama"):
        seen.append(p)
        return [{"agent_id": "a" * 32, "hostname": "h1", "token": "t"}]

    monkeypatch.setattr(gateway, "_candidates", fake_candidates)
    monkeypatch.setattr(gateway, "_forward_json",
                        lambda agent, p, b: (FakeResp(200, {"ok": 1}), None))
    r = _client().post("/api/gateway/v1/chat/completions",
                       json={"model": "pinned-model"})
    assert r.status_code == 200 and seen == ["lms"]


def test_model_index_cache_hit_skips_refresh(monkeypatch):
    _reset_model_index()
    monkeypatch.setattr(gateway.agent_registry, "pinned_agent", lambda p, m: None)
    gateway._model_index["ts"] = gateway.time.time()
    gateway._model_index["map"] = {"v1": "vllm"}
    monkeypatch.setattr(gateway, "_refresh_model_index",
                        lambda: (_ for _ in ()).throw(AssertionError("refresh called")))
    assert gateway._provider_for_model("v1") == "vllm"


def test_lms_gateway_routes_registered():
    c = _client()
    r = c.post("/api/gateway/lms/v1/chat/completions", data="notjson",
               content_type="application/json")
    assert r.status_code == 400


class FakeUpstream:
    def __init__(self, chunks, status=200, ctype="text/event-stream"):
        self.status_code = status
        self.headers = {"content-type": ctype}
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        self.closed = True


def test_stream_pipes_chunks(monkeypatch):
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agent])
    up = FakeUpstream([b'data: {"c":1}\n\n', b"data: [DONE]\n\n"])
    monkeypatch.setattr(gateway, "_dial_stream", lambda a, p, b: up)
    r = _client().post("/api/gateway/v1/chat/completions", json={"stream": True})
    assert r.status_code == 200
    assert r.content_type.startswith("text/event-stream")
    body = r.get_data()
    assert b'data: {"c":1}' in body and b"[DONE]" in body


def test_stream_503_upstream_fails_over(monkeypatch):
    a1 = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    a2 = {"agent_id": "b" * 32, "hostname": "h2", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [a1, a2])
    bad = FakeUpstream([], status=503, ctype="application/json")
    good = FakeUpstream([b"data: [DONE]\n\n"])
    monkeypatch.setattr(gateway, "_dial_stream",
                        lambda a, p, b: bad if a is a1 else good)
    r = _client().post("/api/gateway/v1/chat/completions", json={"stream": True})
    assert r.status_code == 200 and bad.closed


def test_stream_non_sse_error_relayed(monkeypatch):
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agent])
    up = FakeUpstream([], status=400, ctype="application/json")
    up.content = b'{"error":{"message":"bad request"}}'
    monkeypatch.setattr(gateway, "_dial_stream", lambda a, p, b: up)
    r = _client().post("/api/gateway/v1/chat/completions", json={"stream": True})
    assert r.status_code == 400


# ── #496: usage probe injection for usage-counted stream requests ────

def test_with_usage_probe_injects_when_absent():
    body = {"model": "m", "stream": True}
    out, injected = gateway._with_usage_probe(body)
    assert injected is True
    assert out["stream_options"] == {"include_usage": True}
    assert "stream_options" not in body


def test_with_usage_probe_respects_client_stream_options():
    body = {"model": "m", "stream": True,
            "stream_options": {"include_usage": False}}
    out, injected = gateway._with_usage_probe(body)
    assert injected is False and out is body


def test_candidates_prefer_agents_serving_model(monkeypatch):
    # p1 is the RR-resolved primary but only p2 serves model "m" per the
    # index: live ordering puts p2 first, p1 as failover.
    prim = {"agent_id": "p1", "hostname": "hp"}
    pool_b = {"agent_id": "p2", "hostname": "h2"}
    monkeypatch.setattr(gateway.proxies, "_resolve_target",
                        lambda pk, m, a, allow_pool=True: (prim, None))
    monkeypatch.setattr(gateway.agent_registry, "load_agents",
                        lambda: {"global": {"llama_pool": ["p1", "p2"]}})
    monkeypatch.setattr(gateway.agent_registry, "default_agent_id_for", lambda p: "p2")
    monkeypatch.setattr(gateway.agent_registry, "resolve_agent_by_id",
                        lambda aid, capability=None: {"p1": prim, "p2": pool_b}[aid])
    monkeypatch.setattr(gateway.agent_registry, "agent_liveness", lambda a: "live")
    monkeypatch.setattr(gateway.agent_registry, "pinned_agent", lambda p, m: None)
    monkeypatch.setattr(gateway, "_model_index",
                        {"ts": 0.0, "map": {}, "refreshing": False,
                         "serving": {"llama:m": ["p2"]}})
    assert [a["agent_id"] for a in gateway._candidates("m", None)] == ["p2", "p1"]
    # Unknown model: original order preserved.
    assert [a["agent_id"] for a in gateway._candidates("other", None)] == ["p1", "p2"]
    # Explicit ?agent= pick keeps first place despite the serving index.
    assert [a["agent_id"] for a in gateway._candidates("m", "p1")] == ["p1", "p2"]
    # A model pin resolving to the primary keeps first place too.
    monkeypatch.setattr(gateway.agent_registry, "pinned_agent",
                        lambda p, m: prim if m == "m" else None)
    assert [a["agent_id"] for a in gateway._candidates("m", None)] == ["p1", "p2"]


def test_models_merge_populates_serving_index(monkeypatch):
    a1 = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    a2 = {"agent_id": "b" * 32, "hostname": "h2", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [a1, a2])
    monkeypatch.setattr(gateway, "_GATEWAY_PROVIDERS", ("llama",))
    monkeypatch.setattr(gateway, "_model_index",
                        {"ts": 0.0, "map": {}, "serving": {}, "refreshing": False})
    # m3 is catalog-only on h2 (llama-router status "unloaded"): listed in
    # /v1/models output but excluded from the serving index.
    payloads = {"h1": {"data": [{"id": "m1"}, {"id": "m2"}]},
                "h2": {"data": [{"id": "m2"},
                                {"id": "m3", "status": {"value": "unloaded"}}]}}

    def fake_request(method, agent, path, **kw):
        return FakeResp(200, payloads[agent["hostname"]]), [], None

    monkeypatch.setattr(gateway.agent_registry, "agent_request", fake_request)
    r = _client().get("/api/gateway/v1/models")
    assert r.status_code == 200
    assert [m["id"] for m in r.get_json()["data"]] == ["m1", "m2", "m3"]
    assert gateway._model_index["serving"] == {
        "llama:m1": [a1["agent_id"]],
        "llama:m2": [a1["agent_id"], a2["agent_id"]]}


# ── #628/#630/#631/#632: gateway hardening ───────────────────────────

def test_stream_pool_exhausted_503_retry_after_and_log(monkeypatch, caplog):
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agent])
    up = FakeUpstream([b"data: [DONE]\n\n"])
    monkeypatch.setattr(gateway, "_dial_stream", lambda a, p, b: up)
    monkeypatch.setattr(gateway.stream_pool.POOL, "try_acquire", lambda: False)
    with caplog.at_level("WARNING", logger="llm-systems-manager.gateway"):
        r = _client().post("/api/gateway/v1/chat/completions",
                           json={"stream": True})
    assert r.status_code == 503 and up.closed
    assert r.headers["Retry-After"] == str(gateway._POOL_RETRY_AFTER_S)
    assert any("stream pool at capacity" in m for m in caplog.messages)


def test_500_relayed_verbatim_without_failover(monkeypatch):
    a1 = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    a2 = {"agent_id": "b" * 32, "hostname": "h2", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [a1, a2])
    calls = []

    def fake_forward(agent, path, body):
        calls.append(agent["hostname"])
        return FakeResp(500, {"error": {"message": "engine crash"}}), None

    monkeypatch.setattr(gateway, "_forward_json", fake_forward)
    r = _client().post("/api/gateway/v1/completions", json={})
    assert r.status_code == 500 and calls == ["h1"]


def test_proxied_to_header_suppressed_when_disabled(monkeypatch):
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agent])
    monkeypatch.setattr(gateway, "_forward_json",
                        lambda a, p, b: (FakeResp(200, {"ok": 1}), None))
    monkeypatch.setattr(gateway, "_gw_cfg",
                        lambda: types.SimpleNamespace(expose_proxied_to=False))
    r = _client().post("/api/gateway/v1/chat/completions", json={"model": "m"})
    assert r.status_code == 200 and "X-Proxied-To" not in r.headers


def test_stream_proxied_to_header_suppressed_when_disabled(monkeypatch):
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agent])
    up = FakeUpstream([b"data: [DONE]\n\n"])
    monkeypatch.setattr(gateway, "_dial_stream", lambda a, p, b: up)
    monkeypatch.setattr(gateway, "_gw_cfg",
                        lambda: types.SimpleNamespace(expose_proxied_to=False))
    r = _client().post("/api/gateway/v1/chat/completions", json={"stream": True})
    assert r.status_code == 200 and "X-Proxied-To" not in r.headers


def test_candidates_pool_read_failure_warns(monkeypatch, caplog):
    monkeypatch.setitem(gateway._pool_read_warn, "ts", 0.0)
    monkeypatch.setattr(gateway.proxies, "_resolve_target",
                        lambda pk, m, a, allow_pool=True: (None, None))

    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(gateway.agent_registry, "load_agents", boom)
    monkeypatch.setattr(gateway.agent_registry, "default_agent_id_for",
                        lambda p: None)
    with caplog.at_level("WARNING", logger="llm-systems-manager.gateway"):
        assert gateway._candidates("m", None) == []
    assert any("pool id read failed" in m and "disk gone" in m
               for m in caplog.messages)


def test_pool_read_warn_is_rate_limited(monkeypatch, caplog):
    monkeypatch.setitem(gateway._pool_read_warn, "ts", 0.0)
    with caplog.at_level("WARNING", logger="llm-systems-manager.gateway"):
        gateway._warn_pool_read_failed(OSError("first"))
        gateway._warn_pool_read_failed(OSError("second"))
    msgs = [m for m in caplog.messages if "pool id read failed" in m]
    assert len(msgs) == 1 and "first" in msgs[0]


# ── #625/#627/#629: gateway routing bug fixes ────────────────────────

def test_models_fanout_does_not_advance_pool_rr(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway.proxies, "_resolve_target",
                        lambda *a, **k: calls.append(a) or (None, None))
    monkeypatch.setattr(gateway.agent_registry, "load_agents",
                        lambda: {"global": {}})
    monkeypatch.setattr(gateway.agent_registry, "default_agent_id_for",
                        lambda p: None)
    monkeypatch.setattr(gateway, "_model_index",
                        {"ts": 0.0, "map": {}, "serving": {}, "refreshing": False})
    r = _client().get("/api/gateway/v1/models")
    assert r.status_code == 200 and calls == []


def test_candidates_no_rr_still_lists_pool_and_default(monkeypatch):
    p1 = {"agent_id": "p1", "hostname": "h1"}
    p2 = {"agent_id": "p2", "hostname": "h2"}

    def _no_resolve(*a, **k):
        raise AssertionError("resolve_target must not run with advance_rr=False")

    monkeypatch.setattr(gateway.proxies, "_resolve_target", _no_resolve)
    monkeypatch.setattr(gateway.agent_registry, "load_agents",
                        lambda: {"global": {"llama_pool": ["p1"]}})
    monkeypatch.setattr(gateway.agent_registry, "default_agent_id_for",
                        lambda p: "p2")
    monkeypatch.setattr(gateway.agent_registry, "resolve_agent_by_id",
                        lambda aid, capability=None: {"p1": p1, "p2": p2}[aid])
    monkeypatch.setattr(gateway.agent_registry, "agent_liveness", lambda a: "live")
    out = gateway._candidates(None, None, advance_rr=False)
    assert [a["agent_id"] for a in out] == ["p1", "p2"]


def test_stale_built_index_miss_serves_llama_and_kicks_async(monkeypatch):
    monkeypatch.setattr(gateway.agent_registry, "pinned_agent", lambda p, m: None)
    monkeypatch.setattr(gateway, "_model_index",
                        {"ts": 1.0, "map": {"other": "lms"},
                         "serving": {}, "refreshing": False})
    kicked = []
    monkeypatch.setattr(gateway, "_refresh_model_index_async",
                        lambda: kicked.append(1))

    def _no_sync():
        raise AssertionError("sync refresh must not run on a stale-built index")

    monkeypatch.setattr(gateway, "_refresh_model_index", _no_sync)
    assert gateway._provider_for_model("mystery") == "llama"
    assert kicked == [1]


def test_never_built_index_miss_refreshes_sync(monkeypatch):
    monkeypatch.setattr(gateway.agent_registry, "pinned_agent", lambda p, m: None)
    monkeypatch.setattr(gateway, "_model_index",
                        {"ts": 0.0, "map": {}, "serving": {}, "refreshing": False})
    monkeypatch.setattr(gateway, "_refresh_model_index", lambda: {"m1": "lms"})
    assert gateway._provider_for_model("m1") == "lms"


def test_prewarm_model_index_kicks_async(monkeypatch):
    kicked = []
    monkeypatch.setattr(gateway, "_refresh_model_index_async",
                        lambda: kicked.append(1))
    gateway.prewarm_model_index()
    assert kicked == [1]


def test_usage_not_recorded_for_non_completion_2xx(monkeypatch):
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates",
                        lambda m, a, p="llama", **kw: [agent])
    body = {"proxied": True,
            "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
    monkeypatch.setattr(gateway, "_forward_json",
                        lambda a, p, b: (FakeResp(200, body), None))
    recorded = []
    monkeypatch.setattr(gateway.gateway_usage, "record",
                        lambda aid, p, g: recorded.append((aid, p, g)))
    r = _client().post("/api/gateway/lms/v1/chat/completions", json={"model": "m"})
    assert r.status_code == 200 and recorded == []


def test_usage_recorded_for_completion_body(monkeypatch):
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates",
                        lambda m, a, p="llama", **kw: [agent])
    body = {"choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
    monkeypatch.setattr(gateway, "_forward_json",
                        lambda a, p, b: (FakeResp(200, body), None))
    recorded = []
    monkeypatch.setattr(gateway.gateway_usage, "record",
                        lambda aid, p, g: recorded.append((aid, p, g)))
    r = _client().post("/api/gateway/lms/v1/chat/completions", json={"model": "m"})
    assert r.status_code == 200 and recorded == [(agent["agent_id"], 5, 7)]


# ── #648/#649/#652: audit follow-ups ─────────────────────────────────

def test_candidates_no_rr_with_model_id_skips_pool(monkeypatch):
    # #652: advance_rr=False with a model_id resolves pin/picker but must
    # not allow the pool-RR pick.
    seen = {}

    def fake_resolve(pk, m, a, allow_pool=True):
        seen["allow_pool"] = allow_pool
        return None, None

    monkeypatch.setattr(gateway.proxies, "_resolve_target", fake_resolve)
    monkeypatch.setattr(gateway.agent_registry, "load_agents",
                        lambda: {"global": {}})
    monkeypatch.setattr(gateway.agent_registry, "default_agent_id_for",
                        lambda p: None)
    gateway._candidates("m", None, advance_rr=False)
    assert seen["allow_pool"] is False
    gateway._candidates("m", None, advance_rr=True)
    assert seen["allow_pool"] is True


def test_models_merged_served_from_fresh_cache(monkeypatch):
    # #648: the merged /v1/models path serves the fresh index instead of
    # re-fanning out to every agent.
    _reset_model_index()
    a1 = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [a1])
    monkeypatch.setattr(gateway, "_GATEWAY_PROVIDERS", ("llama",))
    calls = []

    def fake_request(method, agent, path, **kw):
        calls.append(path)
        return FakeResp(200, {"data": [{"id": "m1"}]}), [], None

    monkeypatch.setattr(gateway.agent_registry, "agent_request", fake_request)
    c = _client()
    r1 = c.get("/api/gateway/v1/models")
    n_first = len(calls)
    r2 = c.get("/api/gateway/v1/models")
    assert r1.get_json() == r2.get_json()
    assert [m["id"] for m in r2.get_json()["data"]] == ["m1"]
    assert n_first > 0 and len(calls) == n_first
    _reset_model_index()


def test_usage_probe_config_off_skips_injection(monkeypatch):
    # #649: gateway.usage_probe=false must leave the stream body untouched.
    import types as _types
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agent])
    monkeypatch.setattr(gateway, "_gw_cfg",
                        lambda: _types.SimpleNamespace(usage_probe=False))
    seen = {}

    def fake_stream(a, path, body, errors, provider="llama", strip_usage=False,
                    **kw):
        seen["body"], seen["strip_usage"] = body, strip_usage
        return gateway.Response("ok")

    monkeypatch.setattr(gateway, "_stream_from", fake_stream)
    r = _client().post("/api/gateway/lms/v1/chat/completions",
                       json={"model": "m", "stream": True})
    assert r.status_code == 200
    assert "stream_options" not in seen["body"] and seen["strip_usage"] is False


def test_stream_400_after_injection_logs_hint(monkeypatch, caplog):
    # #649: a 400 relayed after include_usage injection names the backend.
    import logging
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agent])
    up = FakeUpstream([], status=400, ctype="application/json")
    up.content = b'{"error":{"message":"unknown field stream_options"}}'
    monkeypatch.setattr(gateway, "_dial_stream", lambda a, p, b: up)
    with caplog.at_level(logging.WARNING, logger=gateway.log.name):
        r = _client().post("/api/gateway/lms/v1/chat/completions",
                           json={"model": "m", "stream": True})
    assert r.status_code == 400
    assert any("include_usage injection" in m for m in caplog.messages)
