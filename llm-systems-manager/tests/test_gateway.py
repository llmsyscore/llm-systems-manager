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


def test_no_candidates_returns_openai_shaped_503(monkeypatch):
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [])
    r = _client().post("/api/gateway/v1/chat/completions", json={"model": "x"})
    assert r.status_code == 503
    err = r.get_json()["error"]
    assert err["code"] == 503 and err["type"] == "unavailable"


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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [a1, a2])
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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [a1, a2])
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
    a1 = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    a2 = {"agent_id": "b" * 32, "hostname": "h2", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [a1, a2])
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
    agents = {p: {"agent_id": p[0] * 32, "hostname": f"h-{p}", "token": "t"}
              for p in gateway._GATEWAY_PROVIDERS}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [agents[p]])
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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [agents[p]])
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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [agent])
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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [a1, a2])
    bad = FakeUpstream([], status=503, ctype="application/json")
    good = FakeUpstream([b"data: [DONE]\n\n"])
    monkeypatch.setattr(gateway, "_dial_stream",
                        lambda a, p, b: bad if a is a1 else good)
    r = _client().post("/api/gateway/v1/chat/completions", json={"stream": True})
    assert r.status_code == 200 and bad.closed


def test_stream_non_sse_error_relayed(monkeypatch):
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [agent])
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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [a1, a2])
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
