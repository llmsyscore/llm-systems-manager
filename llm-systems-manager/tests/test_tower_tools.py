"""#924 Tower tool registry: catalog gating, arg validation, result caps, read tools on fake deps."""
from __future__ import annotations

import json
import types

import pytest

import tower_tools as tt


def _deps():
    return {
        "hosts": lambda: [{"hostname": "box", "providers": ["llama"], "online": True, "model": "qwen3", "watts": 211.0, "busy": True, "age_s": 3}],
        "host": lambda name: {"hostname": name, "online": True, "gpu_temp_c": 91.0, "provider_states": {"llama.cpp": "awake"}} if name == "box" else None,
        "models": lambda host=None, provider=None: [{"model": "qwen3", "provider": "llama", "hostname": "box", "loaded": True}],
        "alarms": lambda status="active", count=5, window=None, host=None, rule=None: [{"id": "a1", "rule": "GPU temp high", "severity": "critical", "host": "box"}][:count],
        "alarm_history": lambda window="30d", group_by="rule", top=10, host=None, rule=None: {"window": window, "total": 1},
        "alert": lambda aid: {"id": aid, "rule": "GPU temp high"} if aid == "a1" else None,
        "energy": lambda window="today": {"window": window, "kwh": 1.84, "cost_usd": 0.28},
        "flow": lambda: {"hosts": [], "totals": {"gen_tps": 38.2}},
        "runs": lambda tool=None, count=10: [{"tool": "benchmark", "model_id": "qwen3", "ok": True}],
        "speed": lambda model: [{"hostname": "box", "gen_tps": 38.2}],
        "health": lambda: {"manager": {"ok": True}, "alarm_engine": {"ok": True}},
        "log_tail": lambda host, provider="llama", lines=40: [("line %d " % i) * 12 for i in range(lines)],
        "config_get": lambda path: {"path": path, "value": "***" if path.endswith("token") else 30},
        "help": lambda topic: "Slot pressure means every llama-server slot is busy.",
    }


def _cfg(**over):
    base = dict(capabilities="read", disabled_tools=[], off_topic="refuse")
    base.update(over)
    return types.SimpleNamespace(**base)


def test_registry_has_only_read_tools_in_p1():
    reg = tt.build_registry(_deps())
    assert set(reg) == {"hosts_overview", "host_detail", "models", "alarms", "alarm_history", "alert_detail", "energy_summary",
                        "gateway_flow", "recent_runs", "bench_speed", "service_health", "log_tail", "config_get", "help"}
    assert all(t.kind == "read" and t.tier == "read" for t in reg.values())
    assert not any(w in n for n in reg for w in ("shell", "exec", "terminal", "file", "http", "config_set"))


def test_catalog_honours_disabled_tools_and_tier():
    reg = tt.build_registry(_deps())
    names = {t.name for t in tt.catalog(reg, _cfg(disabled_tools=["log_tail"]), "operator")}
    assert "log_tail" not in names and "hosts_overview" in names
    reg["fake_act"] = tt.Tool("fake_act", "x", {"type": "object", "properties": {}}, "act", "operate", lambda a: {"ok": True})
    assert "fake_act" not in {t.name for t in tt.catalog(reg, _cfg(), "admin")}
    assert "fake_act" in {t.name for t in tt.catalog(reg, _cfg(capabilities="operate"), "admin")}


def test_validate_args_drops_unknown_and_checks_types():
    reg = tt.build_registry(_deps())
    clean, err = tt.validate_args(reg["log_tail"], {"host": "box", "lines": 500, "rm": "-rf"})
    assert err is not None and "lines" in err
    clean, err = tt.validate_args(reg["log_tail"], {"host": "box", "lines": 20, "rm": "-rf"})
    assert err is None and clean == {"host": "box", "provider": "llama", "lines": 20}
    _, err = tt.validate_args(reg["host_detail"], {})
    assert err and "host" in err


def test_run_tool_caps_results_and_wraps_errors():
    reg = tt.build_registry(_deps())
    res, ok = tt.run_tool(reg["log_tail"], {"host": "box", "lines": 80})
    assert ok and res.get("truncated") is True and len(json.dumps(res, default=str)) <= tt.RESULT_CAP + 64
    deps = _deps(); deps["host"] = lambda name: 1 / 0
    reg = tt.build_registry(deps)
    res, ok = tt.run_tool(reg["host_detail"], {"host": "box"})
    assert ok is False and "error" in res and "ZeroDivision" not in res["error"]


def test_config_get_masks_secrets_and_refuses_unknown_paths():
    reg = tt.build_registry(_deps())
    res, ok = tt.run_tool(reg["config_get"], {"path": "manager.poll_interval"})
    assert ok and res["value"] == 30
    res, ok = tt.run_tool(reg["config_get"], {"path": "/etc/passwd"})
    assert ok is False


def test_openai_schema_and_prompt_catalog():
    reg = tt.build_registry(_deps())
    s = tt.openai_schema(reg["alarms"])
    assert s["type"] == "function" and s["function"]["name"] == "alarms"
    text = tt.prompt_catalog(list(reg.values()))
    assert "hosts_overview" in text and "```tool" in text


def test_summary_line():
    reg = tt.build_registry(_deps())
    assert tt.summary_line(reg["host_detail"], {"host": "box"}, {}, 84) == "read host detail · box · 84 ms"


def test_cap_result_recurses_into_nested_dicts():
    obj = {"a": {"big": list(range(2000))}, "b": 1}
    res = tt.cap_result(obj, 200)
    assert res.get("truncated") is True
    assert len(json.dumps(res, default=str)) <= 200 + 64


def test_cap_result_bounds_quote_dense_strings():
    res = tt.cap_result('a="b" ' * 2000, 500)
    assert res.get("truncated") is True
    assert len(json.dumps(res, default=str)) <= 500 + 64

    obj = {f"k{i}": 'a="b"\\c' * 300 for i in range(5)}
    res = tt.cap_result(obj, 500)
    assert res.get("truncated") is True
    assert len(json.dumps(res, default=str)) <= 500 + 64


def test_validate_args_rejects_non_finite_numbers():
    reg = tt.build_registry(_deps())
    _, err = tt.validate_args(reg["log_tail"], {"host": "box", "lines": float("nan")})
    assert err is not None and "lines" in err
    _, err = tt.validate_args(reg["log_tail"], {"host": "box", "lines": float("inf")})
    assert err is not None and "lines" in err


def test_prod_deps_wires_hosts_alarms_alert_and_config(monkeypatch):
    import discord_bot

    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {
        "fleet": lambda: [{"hostname": "box"}],
        "host": lambda name: {"hostname": name},
        "models": lambda host=None: [{"model": "qwen3", "provider": "llama"}],
    })

    alert_row = {"alert_id": "a1", "rule_name": "GPU temp high", "severity": "critical",
                 "status": "active", "source_host": "box", "message": "hot",
                 "created_at": "t1", "last_evaluated_at": "t2"}

    class _Resp:
        def __init__(self, body):
            self.ok = True
            self._body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    class _Session:
        def get(self, url, timeout=10):
            return _Resp([alert_row] if "alerts/?" in url else alert_row)

    ctx = types.SimpleNamespace(alarm_engine_url=lambda: "http://ae.local", ae_session=_Session())
    deps = tt.prod_deps(ctx, db_path="unused", tools_runs=lambda *a, **k: [],
                        speed_table=lambda *a: [], service_health=lambda: {}, gateway_entries=lambda: [])

    assert deps["hosts"]() == [{"hostname": "box"}]

    rows = deps["alarms"]()
    assert rows[0] == {"id": "a1", "rule": "GPU temp high", "severity": "critical", "status": "active",
                       "host": "box", "message": "hot", "triggered_at": "t1", "last_seen": "t2"}

    row = deps["alert"]("a1")
    assert row["id"] == "a1" and row["host"] == "box"

    res = deps["config_get"]("manager.poll_interval")
    assert res["value"] is not None and "help" in res

    secret = deps["config_get"]("manager.discord.bot_token")
    assert secret == {"path": "manager.discord.bot_token", "value": "***", "secret": True}

    with pytest.raises(ValueError):
        deps["config_get"]("/etc/passwd")


def test_tool_names_constant_matches_the_registry():
    assert tuple(tt.build_registry(_deps()).keys()) == tt.TOOL_NAMES


def test_models_rows_merge_every_host_and_put_loaded_first():
    agents = {"a1": {"status": "approved", "hostname": "mac", "capabilities": {"lms": True}},
              "a2": {"status": "approved", "hostname": "box", "capabilities": {"llama": True}},
              "a3": {"status": "disabled", "hostname": "old", "capabilities": {"lms": True}}}
    samples = {("lms", "a1"): {"ps": [{"model": "google/gemma-4-e4b", "status": "IDLE"}, {"model": "x/stopped", "status": "STOPPED"}]},
               ("llama", "a2"): {"llama": {"state": "awake", "model": "qwen3-14b"}},
               ("lms", "a3"): {"ps": [{"model": "ghost", "status": "IDLE"}]}}
    by_prov = {"lms": lambda s: [p["model"] for p in s.get("ps") or [] if p.get("status") != "STOPPED"],
               "llama": lambda s: [s["llama"]["model"]] if s.get("llama", {}).get("state") == "awake" else []}
    loaded = tt.loaded_models(agents, lambda prov, aid: samples.get((prov, aid)), by_prov)
    assert loaded == {("lms", "google/gemma-4-e4b"): ["mac"], ("llama", "qwen3-14b"): ["box"]}
    entries = [{"id": "qwen3-14b", "provider": "llama", "hosts": ["box"]},
               {"id": "google/gemma-4-e4b", "provider": "lms", "hosts": ["mac"]},
               {"id": "nvidia/nemotron", "provider": "lms", "hosts": ["mac"]}]
    rows = tt.models_rows(entries, loaded)
    assert [(r["model"], r["loaded"], r["loaded_on"]) for r in rows] == [
        ("qwen3-14b", True, ["box"]), ("google/gemma-4-e4b", True, ["mac"]), ("nvidia/nemotron", False, [])]
    assert [r["model"] for r in tt.models_rows(entries, loaded, provider="lms")] == ["google/gemma-4-e4b", "nvidia/nemotron"]
    assert [r["model"] for r in tt.models_rows(entries, tt.loaded_models(agents, lambda p, a: samples.get((p, a)), by_prov, host="box"), host="box")] == ["qwen3-14b"]


def _row(i, rule="GPU temp high", host="box", sev="critical", status="closed", age_s=0):
    import datetime as dt
    t = dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.timezone.utc) - dt.timedelta(seconds=age_s)
    return {"id": f"a{i}", "rule": rule, "severity": sev, "status": status, "host": host, "triggered_at": t.isoformat()}


def test_filter_alerts_by_status_window_host_and_rule():
    now = tt.parse_ts("2026-09-12T12:00:00+00:00")
    rows = [_row(1, status="active"), _row(2, age_s=3 * 86400), _row(3, host="mac", rule="RAM high", age_s=40 * 86400),
            {"id": "a4", "rule": "no time", "status": "closed", "triggered_at": None}]
    assert [r["id"] for r in tt.filter_alerts(rows, status="active", now=now)] == ["a1"]
    assert [r["id"] for r in tt.filter_alerts(rows, status="closed", now=now)] == ["a2", "a3", "a4"]
    assert [r["id"] for r in tt.filter_alerts(rows, window="7d", now=now)] == ["a1", "a2"]
    assert [r["id"] for r in tt.filter_alerts(rows, window="90d", host="MAC", now=now)] == ["a3"]
    assert [r["id"] for r in tt.filter_alerts(rows, rule="ram", now=now)] == ["a3"]
    assert tt.parse_ts("2026-09-12T12:00:00Z") == now and tt.parse_ts("junk") is None


def test_alarm_stats_groups_counts_and_draws_a_chart():
    rows = [_row(1), _row(2), _row(3, rule="RAM high", sev="warning"), _row(4, host="mac", rule="RAM high", age_s=86400)]
    st = tt.alarm_stats(rows, "rule", 10, "last 30d")
    assert st["total"] == 4 and st["groups"] == 2 and st["window"] == "last 30d"
    assert st["by"][0] == {"key": "GPU temp high", "count": 2, "critical": 2, "warning": 0, "info": 0}
    assert st["by"][1]["key"] == "RAM high" and st["by"][1]["count"] == 2 and st["by"][1]["warning"] == 1
    assert st["chart"].splitlines()[0].startswith("GPU temp high  ") and st["chart"].splitlines()[0].endswith(" 2")
    assert st["first"] == "2026-09-11 12:00" and st["last"] == "2026-09-12 12:00"
    days = tt.alarm_stats(rows, "day", 10, "")["by"]
    assert [(d["key"], d["count"]) for d in days] == [("2026-09-11", 1), ("2026-09-12", 3)]
    assert [h["key"] for h in tt.alarm_stats(rows, "host", 1, "")["by"]] == ["box"]
    assert tt.text_bars([("a", 4), ("bb", 0)], width=4) == "a   ████ 4\nbb   0"


def test_hardware_block_reads_static_facts_from_the_sample_and_agent():
    sample = {"system": {"cpu_name": "AMD Ryzen 7 9700X", "cpu_per_core": [1, 2, 3, 4], "ram": {"total_bytes": 64e9},
                         "gpu": {"name": "Navi 31", "vendor": "amd", "vram_total_bytes": 24e9, "temperature_c": 60},
                         "disk": [{"mountpoint": "/", "total_bytes": 1e12, "percent": 41.5}]}}
    hw = tt.hardware_block(sample, {"os": "linux", "role": "llama_host", "version": "v2026.09.11-5"})
    assert hw == {"cpu": "AMD Ryzen 7 9700X", "cores": 4, "ram_gb": 64.0,
                  "gpu": {"name": "Navi 31", "vendor": "amd", "vram_gb": 24.0},
                  "disks": [{"mount": "/", "total_gb": 1000.0, "used_pct": 41.5}],
                  "os": "linux", "role": "llama_host", "agent_version": "v2026.09.11-5"}
    assert tt.hardware_block({}, {"os": "darwin"}) == {"os": "darwin"}


def test_window_underfilled_when_the_oldest_row_is_inside_the_window():
    now = tt.parse_ts("2026-09-12T12:00:00+00:00")
    rows = [_row(1), _row(2, age_s=3 * 86400)]
    assert tt.window_underfilled(rows, "30d", now) is True
    assert tt.window_underfilled(rows, "24h", now) is False
    assert tt.window_underfilled(rows, None, now) is False and tt.window_underfilled([], "7d", now) is False


def test_alarm_history_falls_back_to_the_csv_export_when_1000_rows_do_not_cover_the_window(monkeypatch):
    import discord_bot
    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {"fleet": lambda: [], "host": lambda n: None})
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    fresh = [{"alert_id": f"a{i}", "rule_name": "Slow writes", "severity": "info", "status": "closed", "source_host": "box",
              "created_at": (now - dt.timedelta(minutes=i)).isoformat()} for i in range(1000)]
    csv_text = "alert_id,rule_name,source_host,severity,status,message,created_at,closed_at\n" + "\n".join(
        [f"{a['alert_id']},Slow writes,box,info,closed,m,{a['created_at']}," for a in fresh]
        + [f"c{i},Old rule,mac,critical,closed,m,{(now - dt.timedelta(days=20, minutes=i)).isoformat()}," for i in range(5)])
    calls = []

    class _Resp:
        def __init__(self, body, text=""): self.ok, self._body, self.text = True, body, text
        def raise_for_status(self): pass
        def json(self): return self._body

    class _Session:
        def get(self, url, timeout=10):
            calls.append(url)
            return _Resp(None, csv_text) if "export" in url else _Resp(fresh)

    ctx = types.SimpleNamespace(alarm_engine_url=lambda: "http://ae.local", ae_session=_Session())
    deps = tt.prod_deps(ctx, db_path="unused", tools_runs=lambda *a, **k: [], speed_table=lambda *a: [],
                        service_health=lambda: {}, gateway_entries=lambda: [])
    out = deps["alarm_history"]("30d", "rule", 10)
    assert any("export" in u for u in calls)
    assert out["total"] == 1005 and [g["key"] for g in out["by"]] == ["Slow writes", "Old rule"]
    assert out["note"] == "capped at the newest 10000 alerts"
    out24 = deps["alarm_history"]("24h", "rule", 10)
    assert out24["total"] == 1000 and out24["note"] == "capped at the newest 10000 alerts"
    out1h = deps["alarm_history"]("1h", "rule", 10) if "1h" in tt.WINDOWS else None
    assert out1h is None or out1h["total"] == 60


def test_with_liveness_overrides_online_from_the_heartbeat():
    row = {"hostname": "dev", "online": False, "watts": None}
    assert tt.with_liveness(row, {"hostname": "dev"}, lambda a: "live") == {"hostname": "dev", "online": True, "watts": None, "liveness": "live"}
    assert tt.with_liveness(row, {"hostname": "dev"}, lambda a: "stale")["online"] is False
    assert tt.with_liveness(row, None, lambda a: "live") is row


def test_live_block_covers_system_power_and_provider_runtime():
    sysb = {"cpu_total": 12.5, "cpu_temp_c": 61.0, "cpu_governor": None, "cpu_per_core": [1.0, 2.0],
            "ram": {"percent": 40.0, "used_bytes": 8e9, "available_bytes": 12e9, "total_bytes": 20e9},
            "swap": {"percent": 0.0, "used_bytes": 0, "total_bytes": 4e9},
            "disk": [{"mountpoint": "/", "percent": 55.0, "free_bytes": 100e9}],
            "disk_io": {"read_bytes_per_sec": 1.0}, "net": {"bytes_recv_per_s": 5, "bytes_recv_per_sec": 5, "bytes_sent_total": 9},
            "gpu": {"name": "Navi 31", "temperature_c": 70, "power_watts": 120.0, "vram_used_mb": 8000},
            "ups": {"on_battery": None, "percent": None}, "throughput_window": {"gen": {"avg": 80}}}
    sample = {"system": sysb, "llama": {"state": "awake", "model": "qwen", "total_slots": 2, "active_slots": 1, "chat_template": "x"}}
    buckets = {"llama": (sample, 1.0), "lms": ({"server": {"on": True}, "ps": [{"model": "gemma", "status": "IDLE"}]}, 1.0),
               "vllm": ({"vllm": {"state": "running", "model": "v", "kv_cache_usage_pct": 3}}, 1.0)}
    live = tt.live_block(sample, buckets)
    assert live["cpu"] == {"pct": 12.5, "temp_c": 61.0, "per_core_pct": [1.0, 2.0]}
    assert live["ram"] == {"pct": 40.0, "used_gb": 8.0, "available_gb": 12.0, "total_gb": 20.0}
    assert live["swap"] == {"pct": 0.0, "used_gb": 0.0, "total_gb": 4.0}
    assert live["disks"] == [{"mount": "/", "used_pct": 55.0, "free_gb": 100.0}]
    assert live["net"] == {"bytes_recv_per_sec": 5, "bytes_sent_total": 9}
    assert live["gpu"]["temperature_c"] == 70 and live["power"] == {"watts": 120.0, "source": "gpu"}
    assert "ups" not in live and live["throughput_window"] == {"gen": {"avg": 80}}
    assert live["providers"]["llama"] == {"state": "awake", "model": "qwen", "total_slots": 2, "active_slots": 1}
    assert live["providers"]["lms"] == {"server_on": True, "loaded": [{"model": "gemma", "status": "IDLE"}]}
    assert live["providers"]["vllm"] == {"state": "running", "model": "v", "kv_cache_usage_pct": 3}
    assert tt.live_block({}, {}) == {}


def test_config_get_is_admin_only_in_the_catalog():
    reg = tt.build_registry(_deps())
    assert reg["config_get"].role == "admin"
    assert "config_get" not in {t.name for t in tt.catalog(reg, _cfg(), "operator")}
    assert "config_get" in {t.name for t in tt.catalog(reg, _cfg(), "admin")}
    assert {t.name for t in tt.catalog(reg, _cfg(), "operator")} == set(tt.TOOL_NAMES) - {"config_get"}
