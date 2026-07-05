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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a: [])
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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a: [a1, a2])
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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a: [a1, a2])
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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a: [a1, a2])
    payloads = {"h1": {"data": [{"id": "m1"}, {"id": "m2"}]},
                "h2": {"data": [{"id": "m2"}, {"id": "m3"}]}}

    def fake_request(method, agent, path, **kw):
        return FakeResp(200, payloads[agent["hostname"]]), [], None

    monkeypatch.setattr(gateway.agent_registry, "agent_request", fake_request)
    r = _client().get("/api/gateway/v1/models")
    ids = [m["id"] for m in r.get_json()["data"]]
    assert ids == ["m1", "m2", "m3"]


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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a: [agent])
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
    monkeypatch.setattr(gateway, "_candidates", lambda m, a: [a1, a2])
    bad = FakeUpstream([], status=503, ctype="application/json")
    good = FakeUpstream([b"data: [DONE]\n\n"])
    monkeypatch.setattr(gateway, "_dial_stream",
                        lambda a, p, b: bad if a is a1 else good)
    r = _client().post("/api/gateway/v1/chat/completions", json={"stream": True})
    assert r.status_code == 200 and bad.closed


def test_stream_non_sse_error_relayed(monkeypatch):
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a: [agent])
    up = FakeUpstream([], status=400, ctype="application/json")
    up.content = b'{"error":{"message":"bad request"}}'
    monkeypatch.setattr(gateway, "_dial_stream", lambda a, p, b: up)
    r = _client().post("/api/gateway/v1/chat/completions", json={"stream": True})
    assert r.status_code == 400
