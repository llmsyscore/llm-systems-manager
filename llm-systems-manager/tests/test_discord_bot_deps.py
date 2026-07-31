"""#471: prod_deps against a seeded STORE + patched registry/AE."""
from __future__ import annotations


import pytest

import discord_bot as db
import provider_state

AID = "a" * 32
MAC = "b" * 32

LLAMA_SAMPLE = {
    "host": "box", "cpu_total": 21.5,
    "ram": {"percent": 40.0},
    "gpu": {"power_watts": 250.0, "gpu_util_percent": 90.0,
            "temperature_c": 71.0},
    "liquidctl": {},
    "llama": {"state": "awake", "model": "qwen3",
              "requests_processing": 1, "tokens_per_second": 42.0},
}

LMS_SAMPLE = {
    "system": {"host": "mac", "cpu_total": 9.0, "ram": {"percent": 70.0},
               "gpu": {}},
    "server": {"on": True},
    "ps": [{"identifier": "phi4", "status": "IDLE"}],
}

AGENTS = {
    AID: {"status": "approved", "hostname": "box",
          "capabilities": {"llama": True}, "token": "tok",
          "bind_url": "https://box:8082"},
    MAC: {"status": "approved", "hostname": "mac",
          "capabilities": {"lms": True}, "token": "tok2",
          "bind_url": "https://mac:8082"},
}


@pytest.fixture
def deps(monkeypatch):
    import agent_registry
    monkeypatch.setattr(agent_registry, "load_agents",
                        lambda: {"agents": {k: dict(v)
                                            for k, v in AGENTS.items()}})
    monkeypatch.setattr(agent_registry, "default_agent_id_for",
                        lambda prov: {"llama": AID, "lms": MAC}.get(prov))
    provider_state.STORE.put("llama", AID, dict(LLAMA_SAMPLE))
    provider_state.STORE.put("lms", MAC, dict(LMS_SAMPLE))
    yield db.prod_deps(_ctx())
    provider_state.STORE.evict(AID)
    provider_state.STORE.evict(MAC)


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class _AeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        return _Resp(200, [{"alert_id": "a1", "rule_name": "GPU hot",
                            "severity": "critical", "status": "active",
                            "hostname": "box"}])

    def post(self, url, timeout=None):
        self.calls.append(("POST", url))
        return _Resp(404 if "missing" in url else 200, {})


def _ctx():
    # Mirrors the real app_context contract: alarm_engine_url is a
    # Callable[[], str] getter, ae_session a direct session object.
    class _Ctx:
        pass

    c = _Ctx()
    c.alarm_engine_url = lambda: "https://ae:8081"
    c.ae_session = _AeSession()
    return c


def test_fleet_reads_seeded_store(deps):
    rows = {h["hostname"]: h for h in deps["fleet"]()}
    assert rows["box"]["online"] is True
    assert rows["box"]["model"] == "qwen3" and rows["box"]["busy"] is True
    assert rows["box"]["watts"] == 250.0
    assert rows["box"]["providers"] == ["llama"]
    assert rows["mac"]["providers"] == ["lms"]
    assert rows["mac"]["busy"] is False
    assert rows["mac"]["model"] == "phi4"


def test_host_detail_flat_and_nested_shapes(deps):
    box = deps["host"]("BOX")
    assert box["cpu_pct"] == 21.5 and box["watts"] == 250.0
    assert box["provider_states"] == {"llama.cpp": "awake"}
    mac = deps["host"]("mac")
    assert mac["cpu_pct"] == 9.0 and mac["watts"] is None
    assert mac["provider_states"] == {"LM Studio": "running"}
    assert deps["host"]("ghost") is None


def test_models_fans_out_via_agent_request(deps, monkeypatch):
    import agent_registry

    def fake_request(method, agent, path, **kw):
        if agent.get("hostname") == "box" and path == "/llama/models":
            return _Resp(200, {"data": [
                {"id": "qwen3", "status": {"value": "loaded"}},
                {"id": "phi4", "status": {"value": "unloaded"}}]}), [], None
        return None, [], "unreachable"

    monkeypatch.setattr(agent_registry, "agent_request", fake_request)
    rows = deps["models"](None)
    marks = {r["model"]: r["loaded"] for r in rows}
    assert marks == {"qwen3": True, "phi4": False}
    assert all(r["hostname"] == "box" for r in rows)


def test_load_resolves_agent_and_unwraps_ok_false(deps, monkeypatch):
    import agent_registry
    seen = {}

    def fake_request(method, agent, path, **kw):
        seen["call"] = (method, agent.get("hostname"), path,
                        (kw.get("json") or {}).get("model"))
        return _Resp(200, {"ok": False, "error": "model missing"}), [], None

    monkeypatch.setattr(agent_registry, "agent_request", fake_request)
    ok, err = deps["load"]("llama", None, "qwen3")
    assert ok is False and "model missing" in err
    assert seen["call"] == ("POST", "box", "/llama/load", "qwen3")
    ok, err = deps["load"]("llama", "ghost", "qwen3")
    assert ok is False and "no approved llama agent named ghost" in err


def test_alarms_and_alert_actions_hit_the_ae(deps):
    alerts = deps["alarms"](3)
    assert alerts[0]["alert_id"] == "a1"
    ok, _err = deps["ack"]("a1")
    assert ok is True
    ok, err = deps["ack"]("missing")
    assert ok is False and err == "alert not found"
    ok, err = deps["ack"]("")
    assert ok is False and "required" in err


def test_control_rejects_capability_mismatch_on_named_host(deps, monkeypatch):
    import agent_registry
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda *a, **k: (_Resp(200, {"ok": True}), [], None))
    # "box" only advertises llama; naming it for an lms load must fail closed.
    ok, err = deps["load"]("lms", "box", "phi4")
    assert ok is False and "no approved lms agent named box" in err
    ok, _err = deps["load"]("llama", "box", "qwen3")
    assert ok is True


def test_fleet_reads_provider_blocks_from_their_own_buckets(deps):
    # A fresher lms bucket on the same agent must not hide the llama model.
    provider_state.STORE.put("lms", AID, dict(LMS_SAMPLE))
    rows = {h["hostname"]: h for h in deps["fleet"]()}
    assert rows["box"]["model"] == "qwen3"
    assert rows["box"]["busy"] is True


def test_full_pipeline_route_then_run(deps):
    cfg = {"allowed_user_ids": ["111"], "allow_model_control": False}
    ix = {"type": 2, "member": {"user": {"id": "111"}},
          "data": {"name": "fleet", "options": []}}
    decision = db.route(ix, cfg, db.PendingActions())
    out = db.run_job(decision["job"], deps)
    desc = out["embeds"][0]["description"]
    assert "box" in desc and "mac" in desc
