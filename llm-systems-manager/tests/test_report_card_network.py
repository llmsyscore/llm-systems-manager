"""#468: the real HTTP boundary — probe, stream post, download stream, and
the agent-request wrapper. These are stubbed out everywhere else."""
from __future__ import annotations

import json
import types

import pytest

import report_card as rc

AGENT = {"agent_id": "a" * 32, "token": "tok",
         "hostname": "llm-systems-agent-llama",
         "bind_url": "https://llm-systems-agent-llama:8082",
         "registered_from": "203.0.113.7"}


class _Resp:
    def __init__(self, ok=True, status=200, payload=None, lines=(), raise_exc=None):
        self.ok = ok
        self.status_code = status
        self._payload = payload
        self._lines = lines
        self._raise = raise_exc

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


def _fake_requests(monkeypatch, *, get=None, post=None):
    """Install a stand-in `requests` module for report_card's lazy imports."""
    import requests as real

    mod = types.SimpleNamespace(exceptions=real.exceptions,
                                get=get or (lambda *a, **k: _Resp()),
                                post=post or (lambda *a, **k: _Resp()))
    monkeypatch.setitem(__import__("sys").modules, "requests", mod)
    return mod


# ── _probe_agent ─────────────────────────────────────────────────────

def test_probe_hits_health_and_reports_reachable(monkeypatch):
    seen = {}

    def get(url, **kw):
        seen["url"] = url
        seen["kw"] = kw
        return _Resp(ok=True)

    _fake_requests(monkeypatch, get=get)
    assert rc._probe_agent("https://host:8082") is True
    assert seen["url"] == "https://host:8082/health"
    assert seen["kw"]["timeout"] == (3, 5)


def test_probe_is_false_on_connection_error(monkeypatch):
    import requests as real

    def boom(url, **kw):
        raise real.exceptions.ConnectionError("name resolution failed")

    _fake_requests(monkeypatch, get=boom)
    assert rc._probe_agent("https://unresolvable:8082") is False


def test_probe_is_false_on_timeout(monkeypatch):
    import requests as real

    def slow(url, **kw):
        raise real.exceptions.Timeout("timed out")

    _fake_requests(monkeypatch, get=slow)
    assert rc._probe_agent("https://host:8082") is False


def test_probe_is_false_on_non_ok_status(monkeypatch):
    _fake_requests(monkeypatch, get=lambda url, **kw: _Resp(ok=False, status=503))
    assert rc._probe_agent("https://host:8082") is False


def test_bench_base_url_uses_the_real_probe_to_skip_a_dead_host(monkeypatch):
    # End-to-end through the production probe: the hostname bind_url fails,
    # the registered_from candidate answers, and that one gets benched.
    import requests as real

    def get(url, **kw):
        if "llm-systems-agent-llama" in url:
            raise real.exceptions.ConnectionError("nope")
        return _Resp(ok=True)

    _fake_requests(monkeypatch, get=get)
    url, headers = rc.bench_base_url("llama", AGENT)
    assert "203.0.113.7" in url and url.endswith("/llama/openai")
    assert headers["Authorization"] == "Bearer tok"


# ── _openai_stream_post ──────────────────────────────────────────────

def test_stream_post_yields_decoded_sse_lines(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(kw)
        seen["url"] = url
        return _Resp(lines=["data: a", "", "data: b"])

    _fake_requests(monkeypatch, post=post)
    out = list(rc._openai_stream_post("http://x/chat/completions", {"m": 1},
                                      {"Authorization": "Bearer t"}))
    assert out == ["data: a", "data: b"]          # blank lines dropped
    assert seen["stream"] is True
    assert seen["headers"]["Authorization"] == "Bearer t"
    assert seen["json"] == {"m": 1}


def test_stream_post_passes_verify_only_when_given(monkeypatch):
    seen = []
    _fake_requests(monkeypatch, post=lambda url, **kw: (seen.append(kw), _Resp())[1])
    list(rc._openai_stream_post("http://x", {}))
    assert "verify" not in seen[0]
    list(rc._openai_stream_post("http://x", {}, verify="/ca.crt"))
    assert seen[1]["verify"] == "/ca.crt"


def test_stream_post_raises_on_http_error(monkeypatch):
    import requests as real
    err = real.exceptions.HTTPError("404 Not Found")
    _fake_requests(monkeypatch, post=lambda url, **kw: _Resp(raise_exc=err))
    with pytest.raises(real.exceptions.HTTPError):
        list(rc._openai_stream_post("http://x", {}))


# ── _agent_download_lines ────────────────────────────────────────────

def test_download_lines_falls_back_past_an_unreachable_url(monkeypatch):
    # Same failover shape as bench_base_url; a hostname bind that does not
    # resolve must not strand the download stream.
    import requests as real
    tried = []

    def get(url, **kw):
        tried.append(url)
        if "llm-systems-agent-llama" in url:
            raise real.exceptions.ConnectionError("nope")
        return _Resp(lines=['data: {"type": "line", "text": "5%"}',
                            'data: {"type": "done"}'])

    _fake_requests(monkeypatch, get=get)
    msgs = list(rc._agent_download_lines(AGENT))
    assert len(tried) == 2 and "203.0.113.7" in tried[1]
    assert [m["type"] for m in msgs] == ["line", "done"]


def test_download_lines_raises_when_no_candidate_answers(monkeypatch):
    import requests as real

    def get(url, **kw):
        raise real.exceptions.ConnectionError("nope")

    _fake_requests(monkeypatch, get=get)
    with pytest.raises(RuntimeError, match="no reachable"):
        list(rc._agent_download_lines(AGENT))


def test_download_lines_skips_malformed_json(monkeypatch):
    _fake_requests(monkeypatch, get=lambda url, **kw: _Resp(
        lines=["data: {bad json", 'data: {"type": "done"}', ": keepalive"]))
    msgs = list(rc._agent_download_lines(AGENT))
    assert [m["type"] for m in msgs] == ["done"]


# ── _agent_json ──────────────────────────────────────────────────────

def _stub_agent_request(monkeypatch, resp, err=None):
    import agent_registry
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda m, a, p, **kw: (resp, [], err))


def test_agent_json_returns_none_when_unreachable(monkeypatch):
    _stub_agent_request(monkeypatch, None, err="no callback URL")
    assert rc._agent_json(AGENT, "GET", "/llama/models") is None


def test_agent_json_returns_none_on_http_error(monkeypatch):
    _stub_agent_request(monkeypatch, _Resp(ok=False, status=500))
    assert rc._agent_json(AGENT, "GET", "/llama/models") is None


def test_agent_json_returns_none_on_unparseable_body(monkeypatch):
    _stub_agent_request(monkeypatch, _Resp(payload=ValueError("not json")))
    assert rc._agent_json(AGENT, "GET", "/llama/models") is None


def test_agent_json_returns_the_decoded_payload(monkeypatch):
    _stub_agent_request(monkeypatch, _Resp(payload={"data": [{"id": "m"}]}))
    assert rc._agent_json(AGENT, "GET", "/llama/models") == {"data": [{"id": "m"}]}


def test_agent_json_sends_the_machine_token(monkeypatch):
    import agent_registry
    seen = {}

    def fake(method, agent, path, **kw):
        seen.update(kw)
        return _Resp(payload={"ok": True}), [], None

    monkeypatch.setattr(agent_registry, "agent_request", fake)
    rc._agent_json(AGENT, "POST", "/llama/load", json={"model": "m"})
    assert seen["headers"]["Authorization"] == "Bearer tok"


# ── prod_deps wiring ─────────────────────────────────────────────────

def test_prod_deps_calls_the_expected_agent_paths(monkeypatch):
    calls = []

    def fake(agent, method, path, timeout=15, **kw):
        calls.append((method, path, kw.get("json")))
        if path.endswith("/models"):
            return {"data": [{"id": "Qwen/R:Q4_K_M"}]}
        return {"ok": True}

    monkeypatch.setattr(rc, "_agent_json", fake)
    deps = rc.prod_deps(AGENT)
    assert deps["loaded_models"]("llama", "x") == ["Qwen/R:Q4_K_M"]
    assert deps["vllm_current"]("x") == "Qwen/R:Q4_K_M"
    assert deps["load"]("llama", "x", {"model_id": "Qwen/R:Q4_K_M"}) is True
    paths = [(m, p) for m, p, _b in calls]
    assert ("GET", "/llama/models") in paths
    assert ("GET", "/vllm/models") in paths
    assert ("POST", "/llama/load") in paths
    body = next(b for m, p, b in calls if p == "/llama/load")
    assert body == {"model": "Qwen/R:Q4_K_M"}


def test_prod_deps_load_reports_false_on_ok_false(monkeypatch):
    monkeypatch.setattr(rc, "_agent_json",
                        lambda a, m, p, timeout=15, **k: {"ok": False,
                                                          "error": "OOM"})
    deps = rc.prod_deps(AGENT)
    assert deps["load"]("llama", "x", {"model_id": "m"}) is False


def test_prod_deps_handles_an_empty_model_list(monkeypatch):
    monkeypatch.setattr(rc, "_agent_json",
                        lambda a, m, p, timeout=15, **k: None)
    deps = rc.prod_deps(AGENT)
    assert deps["loaded_models"]("llama", "x") == []
    assert deps["vllm_current"]("x") is None
