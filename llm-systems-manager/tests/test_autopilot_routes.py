"""#472: route contract over a stubbed reconciler."""
from __future__ import annotations
import pytest
import autopilot as ap

@pytest.fixture
def client(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    store = {"state": {"enabled": False, "entries": [], "hosts": {}}}
    monkeypatch.setattr(ap, "get_state", lambda: store["state"])
    monkeypatch.setattr(ap, "set_state",
                        lambda s: store.update(state=s))
    r = ap.Reconciler(get_state=lambda: store["state"],
                      build_observed=lambda: {"agents": {},
                                              "model_sizes_mb": {},
                                              "sat_history": {}},
                      executor=lambda a: True)
    monkeypatch.setattr(ap, "RECONCILER", r)
    ap.register_routes(app, ctx=None, auth=lambda f: f)
    return app.test_client()

def test_get_returns_state_and_proposals(client):
    j = client.get("/api/autopilot").get_json()
    assert j["state"]["enabled"] is False and j["proposals"] == []

def test_put_validates_and_persists(client):
    r = client.put("/api/autopilot", json={"enabled": True, "entries": [
        {"model": "m1", "provider": "llama"}], "hosts": {}})
    assert r.status_code == 200
    assert client.get("/api/autopilot").get_json()["state"]["enabled"] is True

def test_put_rejects_bad_state_with_message(client):
    r = client.put("/api/autopilot", json={"enabled": True, "entries": [
        {"model": "", "provider": "llama"}], "hosts": {}})
    assert r.status_code == 400 and "model" in r.get_json()["error"]

def test_put_rejects_structured_placement_400_not_500(client):
    r = client.put("/api/autopilot", json={"enabled": True, "entries": [
        {"model": "m1", "provider": "llama", "placement": ["x"]}], "hosts": {}})
    assert r.status_code == 400 and "placement" in r.get_json()["error"]

def test_put_rejects_non_numeric_min_replicas_400_not_500(client):
    r = client.put("/api/autopilot", json={"enabled": True, "entries": [
        {"model": "m1", "provider": "llama", "min_replicas": ["x"]}], "hosts": {}})
    assert r.status_code == 400 and "min_replicas" in r.get_json()["error"]

def test_apply_unknown_proposal_404(client):
    assert client.post("/api/autopilot/proposals/nope/apply").status_code == 404

def test_tick_endpoint_returns_plan(client):
    j = client.post("/api/autopilot/tick").get_json()
    assert "actions" in j and "proposals" in j

def test_get_reports_entry_status_for_unplaceable_entry(client):
    r = client.put("/api/autopilot", json={"enabled": True, "entries": [
        {"model": "m1", "provider": "llama", "min_replicas": 1}], "hosts": {}})
    assert r.status_code == 200
    j = client.get("/api/autopilot").get_json()
    assert j["entry_status"] == {"m1/llama": {"placed": 0, "want": 1,
                                              "blocked": "no live agent supports this provider"}}
