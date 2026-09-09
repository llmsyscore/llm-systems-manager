# llm-systems-manager/tests/test_fleet_wiring.py
"""Final-review fix: _fleet_run_on_agent/_fleet_cancel_on_agent wiring (#884, #885)."""
from __future__ import annotations

import manager_mod

AID = "a" * 32


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


def _agent():
    return {"agent_id": AID, "token": "t", "hostname": "alpha"}


def test_ok_reply_without_run_id_is_treated_as_a_failure(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "resolve_agent_by_id", lambda aid, capability=None: _agent())
    monkeypatch.setattr(manager_mod.agent_registry, "agent_request",
                        lambda *a, **k: (_Resp({"ok": True}), None, None))
    started = []
    monkeypatch.setattr(manager_mod.tool_activity, "note_start", lambda *a, **k: started.append(a))
    ok, val = manager_mod._fleet_run_on_agent(AID, {"model_id": "m"})
    assert ok is False
    assert val == "agent returned no run id"
    assert started == []


def test_ok_reply_with_run_id_succeeds_and_notes_start(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "resolve_agent_by_id", lambda aid, capability=None: _agent())
    monkeypatch.setattr(manager_mod.agent_registry, "agent_request",
                        lambda *a, **k: (_Resp({"ok": True, "run_id": "x"}), None, None))
    started = []
    monkeypatch.setattr(manager_mod.tool_activity, "note_start", lambda *a, **k: started.append(a))
    ok, val = manager_mod._fleet_run_on_agent(AID, {"model_id": "m"})
    assert (ok, val) == (True, "x")
    assert started == [(AID, "llama", "benchmark")]


def test_unreachable_agent_reply_is_a_failure(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "resolve_agent_by_id", lambda aid, capability=None: _agent())
    monkeypatch.setattr(manager_mod.agent_registry, "agent_request",
                        lambda *a, **k: (None, None, "connection refused"))
    ok, val = manager_mod._fleet_run_on_agent(AID, {"model_id": "m"})
    assert ok is False
    assert val == "connection refused"


def test_cancel_resolves_the_agent_with_the_same_capability_as_run(monkeypatch):
    seen = []

    def _resolve(aid, capability=None):
        seen.append(capability)
        return _agent()

    monkeypatch.setattr(manager_mod.agent_registry, "resolve_agent_by_id", _resolve)
    monkeypatch.setattr(manager_mod.agent_registry, "agent_request",
                        lambda *a, **k: (_Resp({"ok": True}), None, None))

    manager_mod._fleet_run_on_agent(AID, {"model_id": "m"})
    manager_mod._fleet_cancel_on_agent(AID)
    assert len(seen) == 2 and seen[0] == seen[1]


def test_cancel_returns_false_for_an_unknown_agent(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "resolve_agent_by_id", lambda aid, capability=None: None)
    assert manager_mod._fleet_cancel_on_agent(AID) is False


def test_ae_ingest_alert_posts_generic_payload(monkeypatch):
    sent = {}

    class _R:
        ok = True
        status_code = 200

    def post(url, json=None, headers=None, timeout=None):
        sent.update({"url": url, "json": json, "headers": headers})
        return _R()

    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "https://ae.example:8443")
    monkeypatch.setattr(manager_mod._ae_session, "post", post)
    monkeypatch.setattr(manager_mod.settings.alarm_engine, "ingest_token", "tok", raising=False)
    assert manager_mod._ae_ingest_alert({"name": "Benchmark regression", "severity": "warning"}) is True
    assert sent["url"] == "https://ae.example:8443/api/alarm/ingest"
    assert sent["json"]["name"] == "Benchmark regression"
    assert sent["headers"]["Authorization"] == "Bearer tok"


def test_ae_ingest_alert_without_url(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "")
    assert manager_mod._ae_ingest_alert({"name": "x"}) is False


def test_llama_build_of_reads_store(monkeypatch):
    aid = "z" * 32
    monkeypatch.setattr(manager_mod.provider_state.STORE, "get",
                        lambda p, a: {"sample": {"llama": {"build": "b1-abc"}}} if (p, a) == ("llama", aid) else None)
    assert manager_mod._llama_build_of(aid) == "b1-abc"
    assert manager_mod._llama_build_of("nope") == ""


def test_bench_baseline_cfg_defaults(monkeypatch):
    monkeypatch.setattr(manager_mod.settings.manager, "bench_baselines", None, raising=False)
    cfg = manager_mod._bench_baseline_cfg()
    assert cfg == {"enabled": False, "nightly_at": "03:00", "on_build_change": True, "regression_pct": 15.0}


def test_nightly_at_validator():
    assert manager_mod._validate_nightly_at("") is None
    assert manager_mod._validate_nightly_at("03:00") is None
    assert manager_mod._validate_nightly_at("25:00") == "use HH:MM (24-hour) or leave blank"
    assert "manager.bench_baselines." in manager_mod._HOT_RELOADERS
    assert manager_mod._SETTINGS_VALIDATORS["manager.bench_baselines.nightly_at"] is manager_mod._validate_nightly_at
