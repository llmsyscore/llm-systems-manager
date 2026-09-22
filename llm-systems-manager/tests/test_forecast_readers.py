"""#1031 Forecast production readers: row shaping, epoch conversion, guarded failure, counters."""
from __future__ import annotations

import json
import logging
import sqlite3
import time

import pytest

import forecast_wiring as fw


class _Resp:
    def __init__(self, body=None, text="", ok=True):
        self._body, self.text, self.ok = body, text, ok
        self.status_code = 200 if ok else 500

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture()
def dbs(tmp_path):
    paths = {k: tmp_path / f"{k}.db" for k in ("manager", "audit", "energy")}
    m = sqlite3.connect(str(paths["manager"]))
    m.executescript("""
        CREATE TABLE bench_live_runs (id INTEGER PRIMARY KEY, run_id TEXT, model_id TEXT, agent_id TEXT,
            ts TEXT, ok INTEGER, baseline INTEGER, gen_tps REAL, ppt_tps REAL, latency_s REAL,
            accept_rate REAL, wh_per_ktok REAL, config_json TEXT, result_json TEXT);
        CREATE TABLE report_cards (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, agent_id TEXT,
            provider TEXT, mode TEXT, preset_version TEXT, eligible INTEGER, result TEXT);
    """)
    m.commit()
    m.close()
    a = sqlite3.connect(str(paths["audit"]))
    a.executescript("""
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, role TEXT, ip TEXT,
            auth TEXT, method TEXT, path TEXT, action TEXT, target TEXT, status INTEGER, outcome TEXT,
            detail TEXT, event TEXT);
    """)
    a.commit()
    a.close()
    e = sqlite3.connect(str(paths["energy"]))
    e.executescript("""
        CREATE TABLE energy_hourly (hour_ts INTEGER, agent_id TEXT, hostname TEXT, observed_s REAL,
            active_s REAL, power_s REAL, energy_wh REAL, active_energy_wh REAL, tokens_gen INTEGER,
            tokens_prompt INTEGER, power_source TEXT, samples INTEGER);
    """)
    e.commit()
    e.close()
    return paths


def _build(dbs, **over):
    base = dict(ae_get=lambda path: None, db_paths=dbs, now=1_700_000_000.0,
                hosts=lambda: ["box-a", "box-b"], host_samples=lambda: {},
                agent_rows=lambda: [], host_of_agent=lambda aid: {"a1": "box-a"}.get(aid, ""),
                catalog=lambda: [], latest_agent_version=lambda: "v2026.01.01-1",
                price_kwh=lambda: 0.21, findings=lambda: [])
    base.update(over)
    return fw.build_readers(**base)


# ── series / names ──
def test_series_parses_iso_rows_and_clips_to_the_window(dbs):
    seen = {}

    def ae(path):
        seen["path"] = path
        return _Resp([{"timestamp": "2023-11-14T22:13:20+00:00", "value": 5},
                      {"timestamp": "2023-11-14T22:13:20Z", "value": 7},
                      {"timestamp": "1999-01-01T00:00:00Z", "value": 99},
                      {"timestamp": "nonsense", "value": 1},
                      {"timestamp": "2023-11-14T22:13:20Z", "value": "x"}])

    r = _build(dbs, ae_get=ae)
    out = r["series"]("system", "disk_root_percent", "box-a", start=1_699_999_000.0, end=1_700_000_000.0)
    assert out == [(1_700_000_000.0, 5.0), (1_700_000_000.0, 7.0)]
    assert "/api/alarm/metrics/system/disk_root_percent?" in seen["path"]
    assert "hostname=box-a" in seen["path"] and "agg=mean" in seen["path"]


def test_series_asks_the_alarm_engine_for_the_aggregate_the_check_wants(dbs):
    seen = []
    r = _build(dbs, ae_get=lambda path: seen.append(path) or _Resp([]))
    r["series"]("llama", "requests_processing", "box-a", agg="max")
    r["series"]("llama", "requests_processing", "box-a", agg="nonsense")
    assert "agg=max" in seen[0] and "agg=mean" in seen[1]


def test_names_retries_a_smaller_limit_on_a_refusal(dbs):
    seen = []
    refused = _Resp(ok=False)
    refused.status_code = 414

    def ae(path):
        seen.append(path)
        return refused if len(seen) == 1 else _Resp([{"metric_name": "a"}])

    assert _build(dbs, ae_get=ae)["names"]("forecast", "box-a") == ["a"]
    assert len(seen) == 2
    assert f"limit={fw.NAMES_LIMIT}" in seen[0] and f"limit={fw.NAMES_RETRY_LIMIT}" in seen[1]


def test_names_does_not_retry_a_server_error(dbs):
    seen = []
    names = _build(dbs, ae_get=lambda p: seen.append(p) or _Resp(ok=False))["names"]("forecast", "box-a")
    assert names == [] and len(seen) == 1


def test_series_returns_empty_when_the_alarm_engine_is_unreachable(dbs):
    def boom(path):
        raise OSError("down")

    assert _build(dbs, ae_get=boom)["series"]("system", "ram_percent", "box-a") == []
    assert _build(dbs, ae_get=lambda p: _Resp(ok=False))["series"]("system", "ram_percent", "box-a") == []


def test_names_dedupes_sorts_and_caches_per_source_host(dbs):
    calls = []

    def ae(path):
        calls.append(path)
        return _Resp([{"metric_name": "b"}, {"metric_name": "a"}, {"metric_name": "a"}, {}])

    r = _build(dbs, ae_get=ae)
    assert r["names"]("forecast", "box-a") == ["a", "b"]
    assert r["names"]("forecast", "box-a") == ["a", "b"]
    assert len(calls) == 1
    r["names"]("forecast", "box-b")
    assert len(calls) == 2


# ── hosts / mounts / host_mem ──
def test_hosts_mounts_and_host_mem_read_the_sample_block(dbs):
    samples = {"box-a": {"disk": [{"mountpoint": "/"}, {"mountpoint": "/mnt/models"}, {}],
                         "ram": {"total_bytes": 64e9}, "gpu": {"vram_total_bytes": 24e9}}}
    r = _build(dbs, host_samples=lambda: samples)
    assert r["hosts"] == r["hosts"]
    assert r["hosts"]() == ["box-a", "box-b"]
    assert r["mounts"]("box-a") == {"root": "/", "mnt_models": "/mnt/models"}
    assert r["mounts"]("box-b") == {}
    mem = r["host_mem"]()
    assert mem["box-a"]["ram_gb"] == pytest.approx(64.0)
    assert mem["box-a"]["vram_gb"] == pytest.approx(24.0)


def test_every_reader_fails_closed_to_its_empty_value(dbs, caplog):
    def boom():
        raise RuntimeError("nope")

    r = _build(dbs, hosts=boom, host_samples=boom, agent_rows=boom, catalog=boom,
               latest_agent_version=boom, price_kwh=boom, findings=boom)
    with caplog.at_level(logging.DEBUG, logger="llm-systems-manager.forecast"):
        assert r["hosts"]() == []
        assert r["mounts"]("box-a") == {}
        assert r["host_mem"]() == {}
        assert r["agents"]() == []
        assert r["catalog"]() == []
        assert r["latest_agent_version"]() == ""
        assert r["price_kwh"]() == 0.0
        assert r["findings"]() == []
    assert "RuntimeError" in caplog.text and "nope" not in caplog.text


# ── alerts ──
def test_alerts_parses_the_csv_export_into_epoch_rows(dbs):
    csv = ("alert_id,rule_name,source_host,metric_source,metric_name,current_value,threshold_value,"
           "severity,status,message,created_at,acknowledged_at,closed_at\n"
           "a1,disk full,box-a,system,disk,9,8,critical,closed,msg,"
           "2023-11-14T22:13:20+00:00,,2023-11-14T22:20:00+00:00\n"
           "a2,hot,box-b,system,temp,9,8,warning,active,msg,2023-11-14T22:13:20+00:00,,\n"
           "a3,old,box-b,system,temp,9,8,warning,closed,msg,1999-01-01T00:00:00+00:00,,\n"
           "a4,broken,box-b,system,temp,9,8,warning,active,msg,,,\n")
    r = _build(dbs, ae_get=lambda p: _Resp(text=csv))
    rows = r["alerts"](1_699_999_000.0, 1_700_000_100.0)
    assert [x["id"] for x in rows] == ["a1", "a2"]
    assert rows[0]["created"] == 1_700_000_000.0 and rows[0]["closed"] == 1_700_000_400.0
    assert rows[1]["closed"] is None
    assert rows[0]["rule"] == "disk full" and rows[0]["host"] == "box-a"
    assert rows[0]["metric"] == "system/disk" and rows[1]["metric"] == "system/temp"


def test_alerts_leave_the_metric_empty_when_the_export_has_none(dbs):
    csv = ("alert_id,rule_name,source_host,metric_source,metric_name,severity,status,created_at,closed_at\n"
           "a1,manual,box-a,,,warning,active,2023-11-14T22:13:20+00:00,\n")
    (row,) = _build(dbs, ae_get=lambda p: _Resp(text=csv))["alerts"](1_699_999_000.0, 1_700_000_100.0)
    assert row["metric"] is None and row["rule"] == "manual"


# ── sqlite readers ──
def test_energy_rows_sum_tokens_and_never_return_null(dbs):
    c = sqlite3.connect(str(dbs["energy"]))
    c.execute("INSERT INTO energy_hourly VALUES (1700000000,'a1','box-a',3600,60,60,120.5,10,7,3,'psu',1)")
    c.execute("INSERT INTO energy_hourly VALUES (1700003600,'a1','box-a',3600,0,0,90,0,NULL,NULL,'psu',1)")
    c.execute("INSERT INTO energy_hourly VALUES (1600000000,'a1','box-a',3600,0,0,1,0,0,0,'psu',1)")
    c.commit()
    c.close()
    rows = _build(dbs)["energy"](1_699_999_999, 1_700_004_000)
    assert [r["ts"] for r in rows] == [1_700_000_000.0, 1_700_003_600.0]
    assert rows[0]["tokens"] == 10.0 and rows[1]["tokens"] == 0.0
    assert rows[0]["wh"] == 120.5 and rows[0]["observed_s"] == 3600.0


def test_bench_runs_map_agent_to_host_and_iso_ts_to_epoch(dbs):
    c = sqlite3.connect(str(dbs["manager"]))
    c.execute("INSERT INTO bench_live_runs (run_id, model_id, agent_id, ts, ok, baseline, gen_tps, ppt_tps,"
              " accept_rate, config_json, result_json) VALUES"
              " ('r1','m1','a1','2023-11-14T22:13:20+00:00',1,1,40.5,900,0.7,'{}','{}')")
    c.execute("INSERT INTO bench_live_runs (run_id, model_id, agent_id, ts, ok, baseline, config_json, result_json)"
              " VALUES ('r2','m1','zz','not-a-date',0,0,'{}','{}')")
    c.commit()
    c.close()
    rows = _build(dbs)["bench_runs"]()
    assert len(rows) == 1
    assert rows[0] == {"ts": 1_700_000_000.0, "model": "m1", "host": "box-a", "gen_tps": 40.5,
                       "ppt_tps": 900.0, "accept_rate": 0.7, "baseline": True, "ok": True}


def test_report_cards_score_is_generation_tps_and_skips_rows_without_a_positive_one(dbs):
    c = sqlite3.connect(str(dbs["manager"]))
    c.execute("INSERT INTO report_cards (ts, agent_id, provider, mode, preset_version, eligible, result)"
              " VALUES (1700000000,'a1','llama','standard','1',1,?)",
              (json.dumps({"model": "m1", "gen_tps": 42.0}),))
    c.execute("INSERT INTO report_cards (ts, agent_id, provider, mode, preset_version, eligible, result)"
              " VALUES (1700000001,'a1','llama','standard','1',1,?)", (json.dumps({"model": "m2"}),))
    c.execute("INSERT INTO report_cards (ts, agent_id, provider, mode, preset_version, eligible, result)"
              " VALUES (1700000002,'a1','llama','standard','1',1,'not json')")
    c.execute("INSERT INTO report_cards (ts, agent_id, provider, mode, preset_version, eligible, result)"
              " VALUES (1700000003,'a1','llama','standard','1',1,?)",
              (json.dumps({"model": "m3", "gen_tps": 0}),))
    c.commit()
    c.close()
    rows = _build(dbs)["report_cards"]()
    assert rows == [{"ts": 1_700_000_000.0, "host": "box-a", "model": "m1", "tok_s": 42.0}]


def test_audit_expands_logical_actions_and_resolves_the_host_from_the_detail(dbs):
    c = sqlite3.connect(str(dbs["audit"]))
    rows = [("2023-11-14T22:13:20+00:00", "llama.load", "m1", json.dumps({"agent": "a1"})),
            ("2023-11-14T22:14:20+00:00", "autopilot:load", "m2", json.dumps({"host": "box-b"})),
            ("2023-11-14T22:15:20+00:00", "auth.login", "bob", None),
            ("1999-01-01T00:00:00+00:00", "llama.unload", "m3", "{}")]
    for ts, action, target, detail in rows:
        c.execute("INSERT INTO audit_log (ts, actor, role, ip, auth, method, path, action, target, status,"
                  " outcome, detail, event) VALUES (?, 'x','admin','','session','POST','/p',?,?,200,'ok',?,'e')",
                  (ts, action, target, detail))
    c.commit()
    c.close()
    out = _build(dbs)["audit"](("model.load", "model.unload", "autopilot.load"),
                               1_699_999_000.0, 1_700_000_200.0)
    assert sorted(r["action"] for r in out) == ["autopilot:load", "llama.load"]
    assert {r["action"]: r["host"] for r in out} == {"llama.load": "box-a", "autopilot:load": "box-b"}
    assert all(isinstance(r["ts"], float) for r in out)


def test_sqlite_readers_survive_a_missing_table(dbs, tmp_path):
    empty = {k: tmp_path / f"empty-{k}.db" for k in ("manager", "audit", "energy")}
    r = _build(dbs, db_paths=empty)
    assert r["energy"](0, 1) == [] and r["bench_runs"]() == [] and r["report_cards"]() == []
    assert r["audit"](("model.load",), 0, 1) == []


def test_agents_reader_keeps_only_host_rows(dbs):
    rows = [{"host": "box-a", "version": "v1", "last_seen": "2023-11-14T22:13:20+00:00"},
            {"version": "v1"}, "junk"]
    assert _build(dbs, agent_rows=lambda: rows)["agents"]() == [
        {"host": "box-a", "version": "v1", "last_seen": "2023-11-14T22:13:20+00:00"}]


# ── counters ──
@pytest.fixture(autouse=True)
def _clean_counters():
    fw._counts.clear()
    fw._gateway.clear()
    yield
    fw._counts.clear()
    fw._gateway.clear()


def test_counters_are_cumulative_and_gateway_rows_carry_every_key():
    fw.count("model_loads")
    fw.count("model_loads", 2)
    assert fw.counter_value("model_loads") == 3.0
    assert fw.counter_value("never_seen") == 0.0
    fw.count_gateway("m1", "requests", 5)
    fw.count_gateway("m1", "errors")
    assert fw.gateway_counts() == {"m1": {"requests": 5.0, "errors": 1.0, "timeouts": 0.0, "failovers": 0.0}}


def test_a_model_name_containing_the_old_delimiter_keeps_its_own_counts():
    fw.count_gateway("evil|model", "requests")
    fw.count_gateway("evil|model", "errors", 3)
    assert fw.gateway_counts() == {"evil|model": {"requests": 1.0, "errors": 3.0,
                                                  "timeouts": 0.0, "failovers": 0.0}}


def test_gateway_counts_are_a_copy_callers_cannot_corrupt():
    fw.count_gateway("m1", "requests")
    snap = fw.gateway_counts()
    snap["m1"]["requests"] = 99.0
    snap["m2"] = {}
    assert fw.gateway_counts() == {"m1": {"requests": 1.0, "errors": 0.0,
                                          "timeouts": 0.0, "failovers": 0.0}}


def test_only_a_request_opens_a_models_row():
    """A failure for a model nobody served is a name the gateway could not resolve; it gets no row."""
    fw.count_gateway("never-served", "errors")
    fw.count_gateway("never-served", "timeouts")
    fw.count_gateway("never-served", "failovers")
    assert fw.gateway_counts() == {}
    fw.count_gateway("real", "requests")
    fw.count_gateway("real", "errors", 2)
    assert fw.gateway_counts() == {"real": {"requests": 1.0, "errors": 2.0,
                                            "timeouts": 0.0, "failovers": 0.0}}


def test_a_failure_for_an_unseen_model_never_takes_one_of_the_slots():
    # one slot still free: the cap cannot be what keeps the name out
    for i in range(fw.MAX_GATEWAY_MODELS - 1):
        fw.count_gateway(f"m{i}", "requests")
    fw.count_gateway("unresolvable", "errors")
    assert len(fw.gateway_counts()) == fw.MAX_GATEWAY_MODELS - 1
    assert "unresolvable" not in fw.gateway_counts()
    fw.count_gateway(f"m{fw.MAX_GATEWAY_MODELS - 1}", "requests")
    fw.count_gateway("one-too-many", "errors")
    counts = fw.gateway_counts()
    assert len(counts) == fw.MAX_GATEWAY_MODELS and "one-too-many" not in counts


def test_gateway_model_cardinality_is_capped_and_known_models_keep_counting():
    for i in range(fw.MAX_GATEWAY_MODELS):
        fw.count_gateway(f"m{i}", "requests")
    fw.count_gateway("one-too-many", "requests")
    fw.count_gateway("m0", "requests", 4)
    counts = fw.gateway_counts()
    assert len(counts) == fw.MAX_GATEWAY_MODELS
    assert "one-too-many" not in counts
    assert counts["m0"]["requests"] == 5.0


def test_gateway_counter_ignores_unknown_kinds_and_odd_model_values():
    fw.count_gateway("m1", "bogus", 7)
    fw.count_gateway("m1", "requests")
    assert fw.gateway_counts()["m1"] == {"requests": 1.0, "errors": 0.0, "timeouts": 0.0, "failovers": 0.0}
    assert "bogus" not in fw.gateway_counts()["m1"]
    for odd in (None, "", 7, b"x", object()):
        fw.count_gateway(odd, "requests")
    assert fw.gateway_counts()["-"]["requests"] == 2.0
    assert fw.gateway_counts()["7"]["requests"] == 1.0


def test_gateway_model_key_is_truncated():
    fw.count_gateway("x" * 900, "requests")
    assert list(fw.gateway_counts()) == ["x" * fw.MODEL_KEY_MAX]


def test_error_counter_counts_only_error_and_above():
    h = fw.ErrorCounter()
    lg = logging.getLogger("llm-systems-manager.forecast-test")
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        lg.warning("no")
        lg.error("yes")
        lg.critical("yes")
    finally:
        lg.removeHandler(h)
    assert h.n == 2


def test_error_counter_is_thread_safe():
    import threading as _t
    h = fw.ErrorCounter()
    rec = logging.LogRecord("x", logging.ERROR, "f", 1, "m", None, None)

    def hammer():
        for _ in range(500):
            h.emit(rec)

    ts = [_t.Thread(target=hammer) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert h.n == 3000


# ── sampler helpers ──
def test_agent_gaps_dedupes_by_host_and_keeps_the_freshest():
    now = 1_700_000_000.0
    rows = [{"host": "box-a", "last_seen": now - 30}, {"host": "box-a", "last_seen": now - 5},
            {"host": "box-b", "last_seen": "2023-11-14T22:13:00+00:00"},
            {"host": "box-c"}, {"last_seen": now}]
    out = fw.agent_gaps(rows, now)
    assert [r["host"] for r in out] == ["box-a", "box-b"]
    assert out[0]["gap_s"] == 5.0 and out[1]["gap_s"] == 20.0
    assert "skew_s" not in out[0] and "skew_s" not in out[1]


def test_agent_gaps_carries_the_freshest_clock_skew():
    """#1091: skew_s rides along from the freshest row only, and only when it is a finite number."""
    now = 1_700_000_000.0
    rows = [{"host": "box-a", "last_seen": now - 30, "skew_s": 40.0},
            {"host": "box-a", "last_seen": now - 5, "skew_s": -2.5},
            {"host": "box-b", "last_seen": now - 5, "skew_s": None},
            {"host": "box-c", "last_seen": now - 5, "skew_s": "nan"},
            {"host": "box-d", "last_seen": now - 5, "skew_s": "3"}]
    out = {r["host"]: r for r in fw.agent_gaps(rows, now)}
    assert out["box-a"]["skew_s"] == -2.5
    assert "skew_s" not in out["box-b"] and "skew_s" not in out["box-c"]
    assert out["box-d"]["skew_s"] == 3.0


def test_db_bytes_sums_and_skips_missing(tmp_path):
    p = tmp_path / "a.db"
    p.write_bytes(b"x" * 10)
    assert fw.db_bytes([p, tmp_path / "gone.db"], lambda s: __import__("os").path.getsize(s)) == 10.0


def test_tz_offset_is_a_number():
    assert isinstance(fw.tz_offset_s(time.time()), float)
