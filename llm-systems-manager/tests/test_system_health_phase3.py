"""#797/#764: system-health phase-3 additions — manager version/connections,
AE + Influx rate passthrough, flow block, agent_update, ae_restart."""
from __future__ import annotations

import pytest

import manager_mod as M


class _AeResp:
    ok = True
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


_AE_HEALTH = {
    "status": "ok",
    "version": "v2026.09.02-1",
    "uptime_s": 4242.0,
    "ingest_points_per_s": 42.5,
    "influx_writes_per_s": 41.0,
    "active_alerts": 3,
    "components": {
        "influxdb": "connected",
        "influxdb_version": "2.7.5",
        "influxdb_ping_ms": 1.4,
        "rule_eval_last_cycle_ms": 12.5,
        "tls": {"enabled": False, "active": False, "error": None},
    },
}


@pytest.fixture
def admin(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "_require_admin", lambda: None)
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.test:8081")
    monkeypatch.setattr(M._ae_session, "get", lambda *a, **k: _AeResp(_AE_HEALTH))
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: {"agents": {}, "global": {}})
    monkeypatch.setattr(M, "_custom_manager_tls_files", lambda quiet=True: None)
    monkeypatch.setattr(M, "DATA_DIR", tmp_path)
    M._AGENT_PUSH_RATE.reset()
    M._HISTORY_REQ_RATE.reset()
    M.app.config["TESTING"] = True
    with M.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        yield c


def _health(c):
    r = c.get("/api/admin/system-health")
    assert r.status_code == 200
    return r.get_json()


def test_manager_block_has_version_connections_and_subscriptions(admin, monkeypatch):
    monkeypatch.setattr(M.stream_health, "snapshot", lambda: {
        "browser_connections": 4, "agent_connections": 9,
        "worker_threads": 40, "worker_threads_busy": 7})
    monkeypatch.setattr(M.companion, "subscription_count", lambda: 2)
    mgr = _health(admin)["manager"]
    assert mgr["version"] == M.__version__
    assert mgr["connections"] == {"browsers": 4, "agents": 9,
                                  "worker_threads": 40, "worker_threads_busy": 7}
    assert mgr["push_subscriptions"] == 2
    # Pre-existing fields survive.
    assert "streams" in mgr and "uptime_s" in mgr


def test_connections_default_to_zero_when_unavailable(admin, monkeypatch):
    monkeypatch.setattr(M.stream_health, "snapshot", lambda: {})
    assert _health(admin)["manager"]["connections"] == {
        "browsers": 0, "agents": 0, "worker_threads": 0, "worker_threads_busy": 0}


def test_ae_and_influx_rates_pass_through(admin):
    svc = {s["name"]: s for s in _health(admin)["services"]}
    ae = svc["alarm_engine"]
    assert ae["rule_eval_ms"] == 12.5
    assert ae["ingest_points_per_s"] == 42.5
    assert ae["active_alerts"] == 3
    assert svc["influxdb"]["writes_per_s"] == 41.0
    assert svc["influxdb"]["state"] == "connected"


def test_flow_block_combines_local_counters_and_ae(admin):
    M._AGENT_PUSH_RATE.add(120)
    M._HISTORY_REQ_RATE.add(60)
    flow = _health(admin)["flow"]
    assert flow["agent_pushes_per_s"] == pytest.approx(2.0)
    assert flow["history_req_per_s"] == pytest.approx(1.0)
    assert flow["ae_ingest_points_per_s"] == 42.5
    assert flow["influx_writes_per_s"] == 41.0


def test_flow_ae_rates_are_null_when_the_engine_is_unreachable(admin, monkeypatch):
    def _boom(*a, **k):
        raise OSError("no route")

    monkeypatch.setattr(M._ae_session, "get", _boom)
    flow = _health(admin)["flow"]
    assert flow["ae_ingest_points_per_s"] is None
    assert flow["influx_writes_per_s"] is None
    assert flow["agent_pushes_per_s"] == 0.0


def test_agent_update_counts_only_outdated_approved_agents(admin, monkeypatch):
    monkeypatch.setattr(M, "_latest_agent_version", lambda: "v2026.09.02-2")
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: {"global": {}, "agents": {
        "a" * 32: {"agent_id": "a" * 32, "hostname": "old1", "status": "approved",
                   "version": "v2026.08.01-1"},
        "b" * 32: {"agent_id": "b" * 32, "hostname": "current", "status": "approved",
                   "version": "v2026.09.02-2"},
        "c" * 32: {"agent_id": "c" * 32, "hostname": "pending", "status": "pending",
                   "version": "v2026.01.01-1"},
    }})
    up = _health(admin)["agent_update"]
    assert up["latest"] == "v2026.09.02-2"
    assert up["outdated"] == 1
    assert up["hostnames"] == ["old1"]


def test_ae_restart_is_always_available(admin, monkeypatch):
    monkeypatch.setattr(M, "install_topology", lambda: {
        "ae_local_disk": True, "ae_local_unit": True,
        "ae_local_url": True, "split": False})
    d = _health(admin)
    assert d["ae_restart"] == {"available": True, "via": "systemctl"}
    assert d["ae_local"] is True

    monkeypatch.setattr(M, "install_topology", lambda: {
        "ae_local_disk": False, "ae_local_unit": False,
        "ae_local_url": False, "split": True})
    d = _health(admin)
    assert d["ae_restart"] == {"available": True, "via": "self-restart"}
    assert d["ae_local"] is False
    # Compatibility fields stay.
    assert "containerized" in d


def test_push_endpoints_feed_the_rate_counter(admin, monkeypatch):
    agent = {"agent_id": "a" * 32, "hostname": "h1", "status": "approved",
             "capabilities": {"llama": True, "lms": True}}
    monkeypatch.setattr(M.agent_registry, "bearer_from_request", lambda: "t")
    monkeypatch.setattr(M.agent_registry, "agent_by_token", lambda t: agent)
    monkeypatch.setattr(M, "_broadcast_llama_state_if_changed", lambda *a, **k: None)
    monkeypatch.setattr(M, "set_lms_active", lambda *a, **k: None)
    assert M._AGENT_PUSH_RATE.total() == 0
    admin.post("/api/remote/host-metrics", json={"llama": {"state": "awake"}})
    admin.post("/api/remote/lmstudio", json={"ps": []})
    admin.post("/api/remote/provider-state",
               json={"provider": "llama", "sample": {"llama": {}}})
    assert M._AGENT_PUSH_RATE.total() == 3


def test_history_rows_count_one_ae_request_per_series(monkeypatch):
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.test:8081")
    monkeypatch.setattr(M, "_fetch_history_series",
                        lambda *a, **k: (a[3], []))
    M._HISTORY_REQ_RATE.reset()
    M._build_history_rows(5, 10)
    assert M._HISTORY_REQ_RATE.total() > 0


def test_manager_block_has_ws_relay_status(admin):
    M._ws_relay_state["status"] = "connected"
    assert _health(admin)["manager"]["ws_relay"] == "connected"
    M._ws_relay_state["status"] = "off"
    assert _health(admin)["manager"]["ws_relay"] == "off"


def test_alarm_engine_service_reports_last_ok_at_and_consecutive_failures(admin):
    M._ae_health_state.update({"consecutive_failures": 0, "last_ok_at": None})
    svc = {s["name"]: s for s in _health(admin)["services"]}["alarm_engine"]
    assert svc["consecutive_failures"] == 0
    assert svc["last_ok_at"] is not None  # a successful probe just stamped it


def test_alarm_engine_last_ok_at_survives_a_transient_failure(admin, monkeypatch):
    M._ae_health_state.update({"consecutive_failures": 0, "last_ok_at": "2026-09-01T00:00:00+00:00"})

    def _boom(*a, **k):
        raise OSError("no route")

    monkeypatch.setattr(M._ae_session, "get", _boom)
    svc = {s["name"]: s for s in _health(admin)["services"]}["alarm_engine"]
    assert svc["last_ok_at"] == "2026-09-01T00:00:00+00:00"  # not cleared on failure
    assert svc["consecutive_failures"] == 1


def test_alarm_engine_no_url_configured_reports_health_state_fields(admin, monkeypatch):
    M._ae_health_state.update({"consecutive_failures": 2, "last_ok_at": None})
    monkeypatch.setattr(M, "_alarm_engine_url", "")
    svc = {s["name"]: s for s in _health(admin)["services"]}["alarm_engine"]
    assert svc["last_ok_at"] is None
    assert svc["consecutive_failures"] == 2


def test_split_install_ae_restart_uses_the_self_restart_api(monkeypatch):
    posts = {}

    class _Resp:
        ok = True
        status_code = 200
        text = ""

    monkeypatch.setattr(M, "_CONTAINERIZED", False)
    monkeypatch.setattr(M, "_BREW_KEG", False)
    monkeypatch.setattr(M, "install_topology", lambda: {
        "ae_local_disk": False, "ae_local_unit": False,
        "ae_local_url": False, "split": True})
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.test:8081")
    monkeypatch.setattr(M._ae_session, "post",
                        lambda url, **k: (posts.update(url=url) or _Resp()))
    with M.app.test_request_context():
        resp = M._admin_service_restart_impl("alarm_engine")
    body = resp[0] if isinstance(resp, tuple) else resp
    assert body.get_json()["ok"] is True
    assert posts["url"].endswith("/api/alarm/admin/self-restart")


# --- restart pending (#816) ---


def test_health_reports_restart_pending(admin, monkeypatch, tmp_path):
    import settings_catalog
    import settings_toml_io as sio
    cfg = tmp_path / "llm-systems.toml"
    cfg.write_text("[manager]\nport = 5000\n")
    monkeypatch.setattr(sio, "resolve_config_path", lambda: cfg)
    monkeypatch.setattr(settings_catalog, "_BOOT_FILE_VALUES",
                        settings_catalog.file_catalog_values())
    M._SETTINGS_RESTART_PENDING.clear()
    assert _health(admin)["restart_pending"] == []
    cfg.write_text("[manager]\nport = 5001\n")
    assert _health(admin)["restart_pending"] == ["manager"]


# ── #828: alarm-engine auth posture ───────────────────────────────────────

def _ae_with_auth(monkeypatch, auth, detail, bearer="tok"):
    payload = dict(_AE_HEALTH)
    if auth is not None:
        payload["auth"] = auth
    payload["components"] = dict(_AE_HEALTH["components"])
    if detail is not None:
        payload["components"]["auth"] = detail
    monkeypatch.setattr(M._ae_session, "get", lambda *a, **k: _AeResp(payload))
    hdrs = dict(M._ae_session.headers)
    hdrs.pop("Authorization", None)
    if bearer:
        hdrs["Authorization"] = f"Bearer {bearer}"
    monkeypatch.setattr(M._ae_session, "headers", hdrs)


def _ae_svc(h):
    return next(s for s in h["services"] if s["name"] == "alarm_engine")


def _auth_warnings(h):
    return [w for w in h["warnings"] if w.startswith("alarm engine auth")]


def test_open_engine_on_network_warns_with_remedy(admin, monkeypatch):
    _ae_with_auth(monkeypatch, "open",
                  {"management": "open", "ingest": "open", "loopback_only": False, "open_on_network": True})
    h = _health(admin)
    svc = _ae_svc(h)
    assert svc["auth"] == "open"
    assert svc["auth_detail"]["open_on_network"] is True
    assert svc["bearer_configured"] is True
    (w,) = _auth_warnings(h)
    assert "auth open" in w and "Admin → Settings → Alarm engine" in w and "both hosts" in w


def test_open_engine_on_loopback_does_not_warn(admin, monkeypatch):
    _ae_with_auth(monkeypatch, "open",
                  {"management": "open", "ingest": "open", "loopback_only": True, "open_on_network": False})
    h = _health(admin)
    assert _ae_svc(h)["auth"] == "open"
    assert _auth_warnings(h) == []


def test_enforced_engine_without_manager_bearer_warns(admin, monkeypatch):
    _ae_with_auth(monkeypatch, "enforced",
                  {"management": "management_token", "ingest": "enforced",
                   "loopback_only": False, "open_on_network": False, "bearer_ok": None}, bearer="")
    h = _health(admin)
    assert _ae_svc(h)["bearer_configured"] is False
    (w,) = _auth_warnings(h)
    assert "sends no token" in w and "[alarm_engine].management_token" in w
    assert "Admin → Settings → Alarm engine" in w


def test_missing_bearer_remedy_names_ingest_token_when_that_is_the_gate(admin, monkeypatch):
    _ae_with_auth(monkeypatch, "enforced",
                  {"management": "ingest_token", "ingest": "enforced",
                   "loopback_only": False, "open_on_network": False, "bearer_ok": None}, bearer="")
    (w,) = _auth_warnings(_health(admin))
    assert "[alarm_engine].ingest_token" in w


def test_rejected_bearer_warns_of_a_mismatch(admin, monkeypatch):
    _ae_with_auth(monkeypatch, "enforced",
                  {"management": "management_token", "ingest": "enforced",
                   "loopback_only": False, "open_on_network": False, "bearer_ok": False})
    h = _health(admin)
    svc = _ae_svc(h)
    assert svc["bearer_configured"] is True and svc["auth_detail"]["bearer_ok"] is False
    (w,) = _auth_warnings(h)
    assert "rejects the manager's token" in w and "differ" in w and "Admin → Settings → Alarm engine" in w


def test_enforced_engine_with_bearer_is_quiet(admin, monkeypatch):
    _ae_with_auth(monkeypatch, "enforced",
                  {"management": "management_token", "ingest": "enforced",
                   "loopback_only": False, "open_on_network": False, "bearer_ok": True})
    h = _health(admin)
    assert _ae_svc(h)["bearer_configured"] is True
    assert _auth_warnings(h) == []


def test_older_engine_without_auth_field_is_quiet(admin, monkeypatch):
    _ae_with_auth(monkeypatch, None, None, bearer="")
    h = _health(admin)
    svc = _ae_svc(h)
    assert svc["auth"] is None and svc["auth_detail"] is None
    assert _auth_warnings(h) == []
