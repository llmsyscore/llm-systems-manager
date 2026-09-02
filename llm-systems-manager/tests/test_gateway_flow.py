"""#797: gateway API-key labels, per-client attribution, /api/admin/gateway/flow
and the hot PUT toggle."""
from __future__ import annotations

import time
import types

import pytest

import auth
import gateway
import gateway_usage
import manager_mod as M
import settings_catalog as sc
import settings_toml_io as sio


# ── labelled api keys ────────────────────────────────────────────────

def _with_keys(monkeypatch, keys):
    gw = types.SimpleNamespace(enabled=True, api_keys=list(keys),
                               read_timeout_s=600.0, expose_proxied_to=True,
                               usage_probe=True)
    monkeypatch.setattr(auth._settings.manager, "gateway", gw, raising=False)
    return gw


def test_labeled_and_plain_keys_both_match(monkeypatch):
    _with_keys(monkeypatch, ["prod=sk-secret-one", "sk-plain-two"])
    assert auth.gateway_key_entries() == [("prod", "sk-secret-one"),
                                          ("key-2", "sk-plain-two")]
    assert auth._gateway_api_keys() == ["sk-secret-one", "sk-plain-two"]


def test_base64_padding_is_not_a_label(monkeypatch):
    _with_keys(monkeypatch, ["c2stcGFkZGVk=="])
    assert auth.gateway_key_entries() == [("key-1", "c2stcGFkZGVk==")]


def test_label_lookup_matches_the_presented_bearer(monkeypatch):
    _with_keys(monkeypatch, ["prod=sk-one", "sk-two"])
    assert auth.gateway_key_label("sk-one") == "prod"
    assert auth.gateway_key_label("sk-two") == "key-2"
    assert auth.gateway_key_label("nope") is None
    assert auth.gateway_key_label("") is None


def test_gate_accepts_a_labeled_key_by_its_secret(monkeypatch):
    _with_keys(monkeypatch, ["prod=sk-one"])
    M.app.config["TESTING"] = True
    with M.app.test_request_context(
            "/api/gateway/v1/models", headers={"Authorization": "Bearer sk-one"}):
        assert auth._gateway_key_ok() is True
    with M.app.test_request_context(
            "/api/gateway/v1/models",
            headers={"Authorization": "Bearer prod=sk-one"}):
        assert auth._gateway_key_ok() is False


# ── per-client attribution ───────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_clients():
    gateway_usage.reset_clients()
    yield
    gateway_usage.reset_clients()


def test_client_rows_track_requests_tokens_and_inflight():
    k = gateway_usage.client_begin("prod", "10.0.0.4", now=1000.0)
    gateway_usage.client_record(k, 120, 30)
    rows = gateway_usage.clients_snapshot(now=1001.0)
    assert rows == [{"label": "prod", "ip": "10.0.0.4", "req_per_min": 1,
                     "inflight": 1, "prompt_tokens": 120, "gen_tokens": 30,
                     "last_seen_s": 1.0, "state": "ok"}]
    gateway_usage.client_end(k, now=1002.0)
    assert gateway_usage.clients_snapshot(now=1002.0)[0]["inflight"] == 0


def test_client_goes_idle_after_ten_minutes():
    k = gateway_usage.client_begin("prod", "10.0.0.4", now=1000.0)
    gateway_usage.client_end(k, now=1000.0)
    assert gateway_usage.clients_snapshot(now=1300.0)[0]["state"] == "ok"
    assert gateway_usage.clients_snapshot(now=1700.0)[0]["state"] == "idle"


def test_stale_clients_fall_out_of_the_window():
    k = gateway_usage.client_begin("gone", "10.0.0.9", now=1000.0)
    gateway_usage.client_end(k, now=1000.0)
    assert gateway_usage.clients_snapshot(now=2000.0) == []


def test_double_close_never_goes_negative():
    k = gateway_usage.client_begin("prod", "1.1.1.1", now=1000.0)
    gateway_usage.client_end(k, now=1001.0)
    gateway_usage.client_end(k, now=1002.0)
    assert gateway_usage.clients_snapshot(now=1002.0)[0]["inflight"] == 0


def test_totals_report_p50_and_errors():
    gateway_usage.client_begin("prod", "1.1.1.1", now=1000.0)
    for ms in (10.0, 20.0, 300.0):
        gateway_usage.record_latency(ms, now=1000.0)
    gateway_usage.record_error(now=1000.0)
    t = gateway_usage.client_totals(now=1001.0)
    assert t["p50_ms"] == 20.0
    assert t["errors_15m"] == 1
    assert t["req_per_min"] == 1


def test_completion_request_attributes_to_the_key_label(monkeypatch):
    _with_keys(monkeypatch, ["prod=sk-one"])
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agent])

    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"choices":[{}],"usage":{"prompt_tokens":7,"completion_tokens":5}}'

    monkeypatch.setattr(gateway, "_forward_json", lambda *a, **k: (_R(), None))
    M.app.config["TESTING"] = True
    c = M.app.test_client()
    r = c.post("/api/gateway/lms/v1/chat/completions",
               json={"model": "m"}, headers={"Authorization": "Bearer sk-one"})
    assert r.status_code == 200
    rows = gateway_usage.clients_snapshot()
    assert [(x["label"], x["prompt_tokens"], x["gen_tokens"], x["inflight"])
            for x in rows] == [("prod", 7, 5, 0)]
    assert gateway_usage.client_totals()["p50_ms"] is not None


def test_dashboard_session_callers_get_the_session_label(monkeypatch):
    _with_keys(monkeypatch, [])
    agent = {"agent_id": "a" * 32, "hostname": "h1", "token": "t"}
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [agent])

    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b"{}"

    monkeypatch.setattr(gateway, "_forward_json", lambda *a, **k: (_R(), None))
    M.app.config["TESTING"] = True
    c = M.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    c.post("/api/gateway/lms/v1/chat/completions", json={"model": "m"})
    assert [x["label"] for x in gateway_usage.clients_snapshot()] == ["session"]


def test_exhausted_candidates_count_as_an_error(monkeypatch):
    _with_keys(monkeypatch, [])
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama", **kw: [])
    M.app.config["TESTING"] = True
    c = M.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    r = c.post("/api/gateway/lms/v1/chat/completions", json={"model": "m"})
    assert r.status_code == 404
    assert gateway_usage.client_totals()["errors_15m"] == 1


# ── /api/admin/gateway/flow ──────────────────────────────────────────

@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(M, "_require_admin", lambda: None)
    M.app.config["TESTING"] = True
    with M.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        yield c


def _stub_fleet(monkeypatch):
    monkeypatch.setattr(gateway, "_GATEWAY_PROVIDERS", ("llama", "lms"))
    monkeypatch.setattr(gateway, "_pool_agent_ids",
                        lambda p: {"llama": ["a" * 32], "lms": ["b" * 32]}[p])
    monkeypatch.setattr(gateway.agent_registry, "resolve_agent_by_id",
                        lambda aid, **k: {"agent_id": aid,
                                          "hostname": "h-" + aid[0]})
    samples = {
        ("llama", "a" * 32): {"llama": {"state": "awake", "model": "qwen3-8b",
                                        "tokens_per_second": 42.0,
                                        "prompt_tokens_per_second": 100.0},
                              "system": {"gpu": {"power_watts": 210.0}}},
        ("lms", "b" * 32): {"ps": [{"model": "phi-4", "status": "LOADED"}],
                            "system": {"gpu": {"power_watts": 90.0}}},
    }
    monkeypatch.setattr(gateway.provider_state.STORE, "get",
                        lambda p, aid: {"sample": samples.get((p, aid))})
    monkeypatch.setattr(gateway.gateway_usage, "last_rates",
                        lambda aid: {"gen_tps": 13.0, "prompt_tps": 4.0})


def test_flow_payload_shape(admin, monkeypatch):
    _stub_fleet(monkeypatch)
    _with_keys(monkeypatch, ["prod=sk-one", "sk-two"])
    k = gateway_usage.client_begin("prod", "10.0.0.4")
    gateway_usage.client_record(k, 100, 20)
    gateway_usage.record_latency(35.0)
    d = admin.get("/api/admin/gateway/flow").get_json()
    assert d["ok"] is True and d["enabled"] is True
    assert d["endpoint"] == "/api/gateway/v1"
    assert d["keys"] == 2 and d["usage_probe"] is True
    assert set(d["clients"][0]) == {"label", "ip", "req_per_min", "inflight",
                                    "prompt_tokens", "gen_tokens",
                                    "last_seen_s", "state"}
    hosts = {h["provider"]: h for h in d["hosts"]}
    assert hosts["llama"]["model"] == "qwen3-8b"
    assert hosts["llama"]["gen_tps"] == 42.0
    assert hosts["llama"]["state"] == "ok"
    # LM Studio publishes no tok/s of its own — the gateway's own rate is used.
    assert hosts["lms"]["model"] == "phi-4"
    assert hosts["lms"]["gen_tps"] == 13.0
    assert set(d["totals"]) == {"req_per_min", "prompt_tps", "gen_tps",
                                "p50_ms", "inflight", "errors_15m"}
    assert d["totals"]["gen_tps"] == 55.0
    assert d["totals"]["prompt_tps"] == 104.0
    assert d["totals"]["p50_ms"] == 35.0
    assert set(d["energy"]) == {"serving_w", "kwh_today", "cost_today",
                                "usd_per_mtok", "cloud_usd_per_mtok"}
    assert d["energy"]["serving_w"] == 300.0


def test_flow_lists_at_most_three_clients_most_recent_first(admin, monkeypatch):
    _stub_fleet(monkeypatch)
    base = time.time() - 10.0
    for i, label in enumerate(["old", "c", "b", "a"]):
        gateway_usage.client_end(
            gateway_usage.client_begin(label, "1.1.1.1", now=base + i),
            now=base + i)
    d = admin.get("/api/admin/gateway/flow").get_json()
    assert [c["label"] for c in d["clients"]] == ["a", "b", "c"]


def test_flow_requires_admin(monkeypatch):
    monkeypatch.setattr(M, "_require_admin",
                        lambda: (M.jsonify({"ok": False}), 403))
    M.app.config["TESTING"] = True
    with M.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        assert c.get("/api/admin/gateway/flow").status_code == 403


# ── PUT /api/admin/gateway ───────────────────────────────────────────

@pytest.fixture
def gw_client(monkeypatch, tmp_path):
    cfg = tmp_path / "llm-systems.toml"
    cfg.write_text("[manager]\nport = 5000\n\n[manager.gateway]\nenabled = true\n")
    monkeypatch.setattr(sio, "resolve_config_path", lambda: cfg)
    monkeypatch.setattr(sc, "_BOOT_FILE_VALUES", sc.file_catalog_values())
    monkeypatch.setattr(M, "_require_admin", lambda: None)
    live = types.SimpleNamespace(enabled=True, api_keys=[], read_timeout_s=600.0,
                                 expose_proxied_to=True, usage_probe=True)
    monkeypatch.setattr(M.settings.manager, "gateway", live, raising=False)
    M.app.config["TESTING"] = True
    with M.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        yield c, cfg, live


def test_put_writes_the_toml_and_flips_the_runtime(gw_client):
    c, cfg, live = gw_client
    assert gateway._gw_enabled() is True
    d = c.put("/api/admin/gateway", json={"enabled": False}).get_json()
    assert d == {"ok": True, "enabled": False}
    assert "enabled = false" in cfg.read_text()
    assert live.enabled is False
    assert gateway._gw_enabled() is False
    d = c.put("/api/admin/gateway", json={"enabled": True}).get_json()
    assert d == {"ok": True, "enabled": True}
    assert gateway._gw_enabled() is True


def test_put_rejects_a_non_boolean(gw_client):
    c, cfg, _live = gw_client
    before = cfg.read_text()
    r = c.put("/api/admin/gateway", json={"enabled": "yes"})
    assert r.status_code == 400
    assert cfg.read_text() == before


def test_gateway_enabled_is_hot_and_never_flags_a_restart():
    assert sc.is_hot("manager.gateway.enabled") is True
    assert sc.services_for(["manager.gateway.enabled"]) == set()
    assert "manager.gateway." in M._HOT_RELOADERS


def test_put_is_audited():
    action, _target, event = M._audit_match("PUT", "/api/admin/gateway")
    assert (action, event) == ("config.gateway", "config.settings")


def test_flow_energy_summarizes_todays_rows(admin, monkeypatch, tmp_path):
    import sqlite3
    import energy
    _stub_fleet(monkeypatch)
    conn = sqlite3.connect(str(tmp_path / "e.db"))
    energy.init_table(conn)
    hour = int(time.time() // 3600) * 3600
    energy.upsert_increment(conn, {
        "hour_ts": hour, "agent_id": "a" * 32, "hostname": "h-a",
        "observed_s": 3600.0, "active_s": 1800.0, "power_s": 3600.0,
        "energy_wh": 200.0, "active_energy_wh": 120.0,
        "tokens_gen": 1_000_000, "tokens_prompt": 500_000,
        "power_source": "gpu"})
    monkeypatch.setattr(energy, "_conn_factory", lambda: conn)
    e = admin.get("/api/admin/gateway/flow").get_json()["energy"]
    assert e["kwh_today"] == 0.2
    assert e["cost_today"] is not None and e["cost_today"] > 0
    assert e["usd_per_mtok"] is not None
    assert e["cloud_usd_per_mtok"] > 0


def test_flow_energy_degrades_without_the_energy_db(admin, monkeypatch):
    import energy
    _stub_fleet(monkeypatch)
    monkeypatch.setattr(energy, "_conn_factory", None)
    e = admin.get("/api/admin/gateway/flow").get_json()["energy"]
    assert e["kwh_today"] is None and e["cost_today"] is None
    assert e["serving_w"] == 300.0


def test_new_client_entry_prunes_stale_clients_without_a_snapshot_read():
    import gateway_usage as gu
    gu._clients.clear()
    k1 = gu.client_begin("a", "10.0.0.1", now=1000.0)
    gu.client_end(k1, now=1000.0)
    gu.client_begin("b", "10.0.0.2", now=1000.0 + gu.CLIENT_WINDOW_S + 5)
    assert k1 not in gu._clients
    gu._clients.clear()


def test_configured_keys_without_traffic_show_as_idle_clients(admin, monkeypatch):
    import gateway_usage as gu
    gu._clients.clear()
    monkeypatch.setattr(auth, "gateway_key_entries", lambda: [("ops-1", "s1"), ("dev", "s2")])
    d = admin.get("/api/admin/gateway/flow").get_json()
    idle = [c for c in d["clients"] if c["state"] == "idle"]
    assert [c["label"] for c in idle] == ["ops-1", "dev"]
    assert idle[0]["ip"] is None and idle[0]["last_seen_s"] is None


def test_hosts_report_provider_side_activity_without_gateway_traffic(admin, monkeypatch):
    _stub_fleet(monkeypatch)
    samples = {
        ("llama", "a" * 32): {"llama": {"state": "awake", "model": "qwen3-8b",
                                        "tokens_per_second": 0.0, "requests_processing": 2}},
        ("lms", "b" * 32): {"ps": [{"model": "phi-4", "status": "GENERATING"}]},
    }
    monkeypatch.setattr(gateway.provider_state.STORE, "get",
                        lambda p, aid: {"sample": samples.get((p, aid))})
    monkeypatch.setattr(gateway.gateway_usage, "last_rates", lambda aid: None)
    d = admin.get("/api/admin/gateway/flow").get_json()
    hosts = {h["provider"]: h for h in d["hosts"]}
    assert hosts["llama"]["inflight"] == 2 and hosts["llama"]["state"] == "ok"
    assert hosts["lms"]["inflight"] == 1 and hosts["lms"]["state"] == "ok"
    assert hosts["lms"]["model"] == "phi-4"
