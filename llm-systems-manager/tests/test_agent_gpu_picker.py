"""Admin GPU picker: heartbeat card list and the gpu-device proxy (#1387)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent_registry as ar
import manager_mod
from manager_mod import app

_CARD = {"id": "0000:03:00.0", "vendor": "amd", "name": "Navi 31", "vram_bytes": 24 << 30, "monitored": True}


def test_heartbeat_gpu_fields_keep_valid_cards():
    body = {"gpu_cards": [_CARD, {"id": "0000:7c:00.0", "vendor": "amd"}], "gpu_device": "0000:03:00.0"}
    out = ar._hb_gpu_fields(body)
    assert out["gpu_device"] == "0000:03:00.0"
    assert out["gpu_cards"] == [
        _CARD,
        {"id": "0000:7c:00.0", "vendor": "amd", "name": None, "vram_bytes": None, "monitored": False},
    ]


@pytest.mark.parametrize("raw", [None, "x", 5, {"id": "a"}])
def test_heartbeat_gpu_fields_from_old_or_bad_agents_are_empty(raw):
    assert ar._hb_gpu_fields({"gpu_cards": raw}) == {"gpu_cards": [], "gpu_device": None}


def test_heartbeat_gpu_fields_drop_bad_entries_and_cap_the_list():
    junk = [None, "x", {"id": 7}, {"id": ""}, {"id": "a", "vram_bytes": True, "monitored": "yes"}]
    cards = ar._hb_gpu_fields({"gpu_cards": junk + [{"id": str(i)} for i in range(40)]})["gpu_cards"]
    assert cards[0] == {"id": "a", "vendor": None, "name": None, "vram_bytes": None, "monitored": False}
    assert len(cards) == 12


def _route(monkeypatch, reply, body=None):
    seen = {}
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    monkeypatch.setattr(manager_mod.agent_registry, "load_agents", lambda: {
        "agents": {"a1": {"agent_id": "a1", "bind_url": "https://x:8082", "token": "t"}}})

    def fake(method, agent, path, **kw):
        seen.update(method=method, path=path, json=kw.get("json"))
        return reply, ["https://x:8082"], None
    monkeypatch.setattr(manager_mod.agent_registry, "agent_request", fake)
    with app.test_request_context("/api/agents/a1/gpu-device", method="POST", json=body or {}):
        resp = manager_mod.agents_gpu_device("a1")
    resp, status = resp if isinstance(resp, tuple) else (resp, resp.status_code)
    return resp.get_json(), status, seen


def test_gpu_device_route_relays_the_choice(monkeypatch):
    reply = SimpleNamespace(status_code=200, ok=True, json=lambda: {"ok": True, "device": "0000:03:00.0"})
    payload, status, seen = _route(monkeypatch, reply, {"device": "0000:03:00.0", "extra": 1})
    assert status == 200 and payload["ok"] is True
    assert seen == {"method": "POST", "path": "/agent/gpu-device", "json": {"device": "0000:03:00.0"}}


def test_gpu_device_route_sends_automatic_for_a_non_string(monkeypatch):
    reply = SimpleNamespace(status_code=200, ok=True, json=lambda: {"ok": True, "device": ""})
    _, _, seen = _route(monkeypatch, reply, {"device": 5})
    assert seen["json"] == {"device": ""}


def test_gpu_device_route_explains_an_old_agent(monkeypatch):
    reply = SimpleNamespace(status_code=404, ok=False, json=lambda: {"detail": "Not Found"})
    payload, status, _ = _route(monkeypatch, reply, {"device": ""})
    assert status == 409 and "update this agent" in payload["error"]


def test_gpu_device_route_is_audited():
    hits = [r for r in manager_mod._AUDIT_ROUTES if r[1].match("/api/agents/a1/gpu-device")]
    assert [(r[0], r[2]) for r in hits] == [("POST", "agent.gpu-device")]
    assert "agent.gpu-device" in manager_mod._AUDIT_LABELS
