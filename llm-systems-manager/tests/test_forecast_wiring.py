"""#1031: the manager wires Forecast — store, job kind, routes, Tower dep, audit, alerts, sampler, threads."""
from __future__ import annotations

import re
import types
from pathlib import Path

import forecast
import forecast_checks
import settings_catalog as sc
from config.unified_config import ManagerConfig, ManagerForecast

SRC = (Path(__file__).resolve().parents[1] / "backend" / "llm-systems-manager.py").read_text()


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


# ── construction ──
def test_forecast_service_store_and_job_kind_are_wired():
    import manager_mod as M
    assert isinstance(M._forecast, forecast.Forecast)
    assert isinstance(M._forecast_store, forecast.Store)
    kind = M._jobs_service.kind(forecast.KIND)
    assert kind is not None and kind.title == forecast.TITLE


def test_routes_are_registered():
    import manager_mod as M
    rules = {str(r) for r in M.app.url_map.iter_rules()}
    assert "/api/forecast" in rules
    assert "/api/forecast/run" in rules
    assert "/api/forecast/<fid>/dismiss" in rules


def test_tower_dep_is_the_forecast_view():
    import manager_mod as M
    assert callable(M._tower_deps["forecast"])
    assert M._tower_deps["forecast"] == M._forecast.tower_view
    view = M._tower_deps["forecast"]()
    assert set(view) >= {"enabled", "findings", "checks"}


def test_tower_pass_gets_the_live_config_and_a_live_tz_offset():
    import manager_mod as M
    assert M._forecast_tower is not None
    assert M._forecast._tz is M._forecast_tz_offset_s
    assert isinstance(M._forecast_tz_offset_s(), float)
    assert M._forecast_host_alias("") == ""


# ── data factory ──
def test_data_factory_returns_checkdata_and_survives_an_unreachable_alarm_engine(monkeypatch):
    import manager_mod as M
    monkeypatch.setattr(M, "_alarm_engine_url", "")
    d = M._forecast_data(1_700_000_000.0, 14 * 86400.0)
    assert isinstance(d, forecast_checks.CheckData)
    assert isinstance(d.hosts(), list)
    assert d.series("system", "ram_percent", "nowhere") == []
    assert d.names("forecast", "nowhere") == []
    assert d.window("system", "ram_percent", "nowhere") == []
    assert d.mounts("nowhere") == {}
    assert isinstance(d.price_kwh(), float)
    assert isinstance(d.findings(), list)


def test_data_factory_never_raises_when_a_reader_source_is_broken(monkeypatch):
    import manager_mod as M

    def boom(*a, **kw):
        raise RuntimeError("registry down")

    monkeypatch.setattr(M, "_forecast_hosts", boom)
    monkeypatch.setattr(M, "_forecast_agent_rows", boom)
    d = M._forecast_data(1_700_000_000.0, 86400.0)
    assert d.hosts() == [] and d.agents() == []


# ── alarm engine helpers ──
def test_ingest_helpers_share_one_post(monkeypatch):
    import manager_mod as M
    calls = []
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.invalid")

    def fake_post(url, **kw):
        calls.append(url)
        return _Resp(200, {"alert_ids": ["alert-7"]})

    monkeypatch.setattr(M._ae_session, "post", fake_post)
    assert M._ae_ingest_alert({"name": "x"}) is True
    assert M._ae_ingest_alert_id({"name": "x"}) == "alert-7"
    assert len(calls) == 2 and calls[0].endswith("/api/alarm/ingest")


def test_ingest_alert_id_is_none_without_an_id(monkeypatch):
    import manager_mod as M
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.invalid")
    monkeypatch.setattr(M._ae_session, "post", lambda url, **kw: _Resp(200, {"alert_ids": []}))
    assert M._ae_ingest_alert_id({"name": "x"}) is None
    monkeypatch.setattr(M._ae_session, "post", lambda url, **kw: _Resp(500, None))
    assert M._ae_ingest_alert_id({"name": "x"}) is None
    assert M._ae_ingest_alert({"name": "x"}) is False


def test_alert_close_accepts_200_and_409_and_never_raises(monkeypatch):
    import manager_mod as M
    import requests
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.invalid")
    seen = []

    def post(url, **kw):
        seen.append(url)
        return _Resp(409)

    monkeypatch.setattr(M._ae_session, "post", post)
    assert M._ae_alert_close("a 1/b") is True
    assert seen[0].endswith("/api/alarm/alerts/a%201%2Fb/close")
    monkeypatch.setattr(M._ae_session, "post", lambda url, **kw: _Resp(200))
    assert M._ae_alert_close("a1") is True
    monkeypatch.setattr(M._ae_session, "post", lambda url, **kw: _Resp(404))
    assert M._ae_alert_close("a1") is False

    def boom(url, **kw):
        raise requests.RequestException("down")

    monkeypatch.setattr(M._ae_session, "post", boom)
    assert M._ae_alert_close("a1") is False
    monkeypatch.setattr(M, "_alarm_engine_url", "")
    assert M._ae_alert_close("a1") is False


def test_forecast_uses_the_id_returning_post_and_the_close_helper():
    import manager_mod as M
    assert M._forecast._post is M._ae_ingest_alert_id
    assert M._forecast._close is M._ae_alert_close
    assert M._forecast._audit is M._forecast_audit_auto


# ── effort signals ──
def test_model_signals_returns_the_four_keys_and_is_guarded(monkeypatch):
    import manager_mod as M
    assert M._forecast._signals is M._forecast_model_signals
    want = {"score_pct", "size_b", "tool_grade", "tok_s"}
    assert set(M._forecast_model_signals("")) == want
    got = M._forecast_model_signals("qwen3-32b-instruct")
    assert set(got) == want and got["size_b"] == 32.0

    # the bench fallback takes the slowest host, since the pinned model may be served from it
    monkeypatch.setattr(M.bench_live, "speed_table",
                        lambda db, m: [{"agent_id": "a", "gen_tps": 48.0}, {"agent_id": "b", "gen_tps": 11.5}])
    assert M._forecast_model_signals("qwen3-9b")["tok_s"] == 11.5

    def boom(*a, **kw):
        raise RuntimeError("store down")

    monkeypatch.setattr(M._tower_evals.store, "latest_for", boom)
    monkeypatch.setattr(M._tower_checks, "get", boom)
    monkeypatch.setattr(M.bench_live, "speed_table", boom)
    monkeypatch.setattr(M.tower_check, "size_b", boom)
    assert M._forecast_model_signals("qwen3-9b") == dict.fromkeys(want)


# ── audit ──
def test_audit_catalog_carries_both_forecast_events():
    import manager_mod as M
    group = next(g for g in M.AUDIT_EVENT_GROUPS if g["key"] == "forecast")
    keys = {e["key"]: e for e in group["events"]}
    assert keys["forecast.run"]["default_on"] is True
    assert keys["forecast.dismiss"]["default_on"] is True
    assert M._AUDIT_EVENT_GROUP["forecast.run"] == "forecast"
    assert M._audit_event_for("forecast.run") == "forecast.run"
    assert M._audit_event_for("forecast.dismiss") == "forecast.dismiss"
    assert M._audit_label("forecast.run") == "Forecast run"
    assert M._audit_label("forecast.dismiss") == "Dismissed a forecast finding"


def test_audit_auto_writes_a_row(monkeypatch):
    import manager_mod as M
    rows = []
    monkeypatch.setattr(M, "_audit_record", lambda entry: rows.append(entry))
    M._forecast_audit_auto({"actor": "forecast", "action": "forecast.run", "target": "code",
                            "ok": True, "detail": {"found": 0}})
    assert len(rows) == 1
    assert rows[0][7] == "forecast.run" and rows[0][-1] == "forecast.run"
    monkeypatch.setitem(M._AUDIT_CFG, "disabled", {"forecast.run"})
    M._forecast_audit_auto({"action": "forecast.run"})
    assert len(rows) == 1


# ── hot reload ──
def test_hot_reload_copies_file_values(monkeypatch):
    import manager_mod as M
    snap = ManagerConfig()
    snap.forecast.every = "6h"
    snap.forecast.window_days = 21
    snap.forecast.alert_min_severity = "critical"
    monkeypatch.setattr(sc, "_snapshot", lambda: types.SimpleNamespace(manager=snap))
    M._forecast_reload_config()
    live = M.settings.manager.forecast
    assert (live.every, live.window_days, live.alert_min_severity) == ("6h", 21, "critical")
    for k in M._FORECAST_KEYS:
        setattr(live, k, getattr(ManagerForecast(), k))


def test_forecast_keys_cover_every_field_and_the_reloader_is_registered():
    import manager_mod as M
    assert set(M._FORECAST_KEYS) == set(ManagerForecast.model_fields)
    assert M._HOT_RELOADERS["manager.forecast."] is M._forecast_reload_config


def test_reload_reschedules_without_waiting_for_the_ticker(monkeypatch):
    import manager_mod as M
    hits = []
    monkeypatch.setattr(M._forecast, "ensure_schedule", lambda: hits.append(1))
    monkeypatch.setattr(sc, "_snapshot", lambda: types.SimpleNamespace(manager=ManagerConfig()))
    M._forecast_reload_config()
    assert hits == [1]


# ── sampler + threads ──
def test_sampler_reads_every_series_the_module_expects():
    import manager_mod as M
    import forecast_sampler
    keys = set(M._forecast_sampler._read)
    assert set(forecast_sampler.GAUGES) <= keys
    assert set(forecast_sampler.COUNTERS) <= keys
    assert {"gateway", "agents"} <= keys
    assert M._forecast_sampler.hostname == M._HOSTNAME


def test_sampler_tick_is_a_no_op_while_forecast_is_off():
    import manager_mod as M
    assert bool(M.settings.manager.forecast.enabled) is False
    assert M._forecast_sampler.tick() == 0


def test_sampler_readers_all_return_something_usable():
    import manager_mod as M
    read = M._forecast_sampler._read
    for name in ("db_manager_bytes", "db_audit_bytes", "db_energy_bytes", "db_wal_bytes"):
        assert isinstance(read[name](), float)
    assert isinstance(read["agents_stale"](), float)
    assert isinstance(read["log_errors"](), float)
    assert isinstance(read["gateway"](), dict)
    assert isinstance(read["agents"](), list)
    assert read["backup_age_s"]() is None or isinstance(read["backup_age_s"](), float)


def test_threads_start_next_to_the_jobs_dispatcher():
    assert "forecast_sampler.start_thread(_forecast_sampler, lambda: _shutting_down)" in SRC
    assert "_start_forecast_ticker()" in SRC
    m = re.search(r"def _start_forecast_ticker\(\):(.*?)\n\n\n", SRC, re.S)
    assert m and '"pytest" in sys.modules' in m.group(1) and "daemon=True" in m.group(1)
    assert "_forecast.ensure_schedule()" in m.group(1)


def test_counter_hooks_sit_on_the_load_paths():
    import manager_mod as M
    assert SRC.count('forecast_wiring.count("model_loads")') == 2
    assert SRC.count('forecast_wiring.count("model_unloads")') == 2
    assert SRC.count('forecast_wiring.count("model_wakes")') == 1
    gw = (Path(__file__).resolve().parents[1] / "backend" / "gateway.py").read_text()
    for kind in ("requests", "errors", "timeouts", "failovers"):
        assert f'forecast_wiring.count_gateway(model_id, "{kind}")' in gw
    ap = (Path(__file__).resolve().parents[1] / "backend" / "autopilot.py").read_text()
    assert 'forecast_wiring.count("model_loads")' in ap
    assert M._forecast_log_errors in M.logging.getLogger("llm-systems-manager").handlers


def test_version_bumped():
    m = re.search(r'^__version__ = "v(\d{4})\.(\d{2})\.(\d{2})-(\d+)"', SRC, re.M)
    assert m and tuple(int(x) for x in m.groups()) >= (2026, 9, 18, 6)
