"""#924 Tower tool registry: catalog gating, arg validation, result caps, read tools on fake deps."""
from __future__ import annotations

import json
import time
import types
from datetime import timedelta, timezone

import pytest

import tower_tools as tt


def _deps():
    return {
        "hosts": lambda provider=None, online=None, busy=None, sort_by=None: [
            {"hostname": "box", "providers": ["llama"], "online": True, "model": "qwen3", "watts": 211.0, "busy": True, "age_s": 3}],
        "host": lambda name, section="all": {"hostname": name, "online": True, "gpu_temp_c": 91.0, "provider_states": {"llama.cpp": "awake"}} if name == "box" else None,
        "host_history": lambda host, metric, window="24h": {"host": host, "metric": metric, "window": window, "points": 0},
        "models": lambda host=None, provider=None: [{"model": "qwen3", "provider": "llama", "hostname": "box", "loaded": True}],
        "alarms": lambda status="active", count=5, window=None, host=None, rule=None: [{"id": "a1", "rule": "GPU temp high", "severity": "critical", "host": "box"}][:count],
        "alarm_search": lambda a: {"alerts": [{"id": "a1", "rule": "GPU temp high", "severity": "critical", "host": "box"}][:a.get("count", 5)],
                                   "total": 1, "offset": 0, "next_offset": None},
        "alarm_history": lambda window="30d", group_by="rule", top=10, host=None, rule=None, a=None: {"window": window, "total": 1},
        "alert": lambda aid: {"id": aid, "rule": "GPU temp high"} if aid == "a1" else None,
        "energy": lambda window="today": {"window": window, "kwh": 1.84, "cost_usd": 0.28},
        "flow": lambda: {"hosts": [], "totals": {"gen_tps": 38.2}},
        "runs": lambda tool=None, count=10: [{"tool": "benchmark", "model_id": "qwen3", "ok": True}],
        "speed": lambda model: [{"hostname": "box", "gen_tps": 38.2}],
        "health": lambda: {"manager": {"ok": True}, "alarm_engine": {"ok": True}},
        "log_tail": lambda host, provider="llama", lines=40, a=None: [("line %d " % i) * 12 for i in range(lines)],
        "profiles": lambda host=None, model=None, profile=None: {"hosts": [{"host": host or "box", "models": [
            {"model": "qwen3", "active": "default", "profiles": ["default", "long-ctx"]}]}]},
        "config_get": lambda path: {"path": path, "value": "***" if path.endswith("token") else 30},
        "help": lambda topic: "Slot pressure means every llama-server slot is busy.",
        "audit": lambda window="24h", actor=None, action=None, count=20: [
            {"ts": "2026-09-12T00:00:00+00:00", "actor": actor or "alice", "action": action or "tower.action.approve",
             "label": "Approved a Tower action", "target": "a1", "outcome": "ok", "detail": {"tool": "wake_server"}}][:count],
        "load": lambda provider, host, model: (host == "box", None if host == "box" else "unknown host"),
        "unload": lambda provider, host, model: (True, None),
        "wake": lambda host: (True, None),
        "restart": lambda provider, host: (True, None),
        "ack": lambda aid: (aid == "a1", None if aid == "a1" else "alert not found"),
        "close": lambda aid: (True, None),
    }


def _cfg(**over):
    base = dict(capabilities="read", disabled_tools=[], off_topic="refuse")
    base.update(over)
    return types.SimpleNamespace(**base)


READ = {"hosts_overview", "host_detail", "host_history", "models", "model_profiles", "alarms", "alarm_history", "alert_detail",
        "energy_summary", "gateway_flow", "recent_runs", "bench_speed", "service_health", "log_tail", "config_get",
        "help", "audit_log"}
ACT = {"load_model", "unload_model", "wake_server", "restart_provider", "ack_alert", "close_alert"}


def test_registry_has_read_and_act_tools():
    reg = tt.build_registry(_deps())
    assert set(reg) == READ | ACT
    assert {t.name for t in reg.values() if t.kind == "act"} == ACT
    assert all(reg[n].tier == "operate" for n in ACT - {"restart_provider"})
    assert reg["restart_provider"].tier == "admin" and reg["restart_provider"].role == "admin"
    assert tt.ACT_TOOL_NAMES == tuple(n for n in tt.TOOL_NAMES if n in ACT)


def test_catalog_ladder_hides_act_tools_until_operate():
    reg = tt.build_registry(_deps())
    assert {t.name for t in tt.catalog(reg, _cfg(capabilities="read"), "admin")} & ACT == set()
    op = {t.name for t in tt.catalog(reg, _cfg(capabilities="operate"), "operator")}
    assert op & ACT == ACT - {"restart_provider"}
    assert "restart_provider" not in {t.name for t in tt.catalog(reg, _cfg(capabilities="admin"), "operator")}
    assert "restart_provider" in {t.name for t in tt.catalog(reg, _cfg(capabilities="admin"), "admin")}
    assert "wake_server" not in {t.name for t in tt.catalog(reg, _cfg(capabilities="admin", disabled_tools=["wake_server"]), "admin")}


def test_act_tools_return_ok_message_and_validate_targets():
    reg = tt.build_registry(_deps())
    res, ok = tt.run_tool(reg["load_model"], {"provider": "llama", "host": "box", "model": "qwen3"})
    assert ok and res == {"ok": True, "message": "done"}
    res, ok = tt.run_tool(reg["load_model"], {"provider": "llama", "host": "ghost", "model": "qwen3"})
    assert ok and res == {"ok": False, "message": "unknown host"}
    res, _ = tt.run_tool(reg["ack_alert"], {"alert_id": "zz"})
    assert res == {"ok": False, "message": "alert not found"}
    args, err = tt.validate_args(reg["restart_provider"], {"provider": "docker", "host": "box"})
    assert err == "provider must be one of llama, lms, vllm"
    args, err = tt.validate_args(reg["wake_server"], {})
    assert err == "host is required"


def test_act_tool_wraps_a_raising_dep_and_a_falsy_no_message_dep():
    deps = _deps()
    deps["wake"] = lambda host: (_ for _ in ()).throw(RuntimeError("boom"))
    deps["restart"] = lambda provider, host: (False, None)
    reg = tt.build_registry(deps)
    res, ok = tt.run_tool(reg["wake_server"], {"host": "box"})
    assert ok is True and res["ok"] is False and "message" in res
    res, ok = tt.run_tool(reg["restart_provider"], {"provider": "llama", "host": "box"})
    assert ok is True and res == {"ok": False, "message": "failed"}


def test_action_cards_say_what_where_and_what_not():
    reg = tt.build_registry(_deps())
    c = tt.action_card(reg["wake_server"], {"host": "box"})
    assert c["title"] == "Wake llama-server" and c["target"] == "box · llama.cpp"
    assert "leaves idle sleep" in c["does"] and c["not"].startswith("No model is loaded or unloaded")
    c = tt.action_card(reg["unload_model"], {"provider": "lms", "host": "mac", "model": "gemma-3-12b"})
    assert c["title"] == "Unload gemma-3-12b" and c["target"] == "mac · LM Studio"
    c = tt.action_card(reg["restart_provider"], {"provider": "vllm", "host": "box"})
    assert c["title"] == "Restart vLLM" and "in flight fail" in c["does"]
    c = tt.action_card(reg["close_alert"], {"alert_id": "a1"})
    assert c["title"] == "Close alert a1" and c["target"] == "alert a1"
    assert tt.action_card(reg["host_detail"], {"host": "box"}) == {}


def test_prompt_catalog_marks_act_tools():
    reg = tt.build_registry(_deps())
    text = tt.prompt_catalog(tt.catalog(reg, _cfg(capabilities="operate"), "operator"))
    assert "- wake_server (action, needs approval):" in text
    assert "- host_detail:" in text


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
    assert err is None and clean == {"host": "box", "provider": "llama", "lines": 20, "before": 0}
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
        "ack": lambda aid: (True, None), "close": lambda aid: (True, None),
    })

    alert_row = {"alert_id": "a1", "rule_name": "GPU temp high", "severity": "critical",
                 "status": "active", "source_host": "box", "message": "hot",
                 "metric_source": "system", "metric_name": "gpu_temperature_c", "current_value": 91.0, "threshold_value": 85.0,
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
                        speed_table=lambda *a: [], service_health=lambda: {}, gateway_entries=lambda: [], audit_rows=lambda *a, **k: [])

    assert deps["hosts"]() == [{"hostname": "box"}]
    assert deps["hosts"](provider="lms") == []

    rows = deps["alarms"]()
    assert rows[0] == {"id": "a1", "rule": "GPU temp high", "severity": "critical", "status": "active",
                       "host": "box", "message": "hot", "metric": "system/gpu_temperature_c", "value": 91.0, "threshold": 85.0,
                       "triggered_at": "t1", "last_seen": "t2", "acknowledged_at": None, "closed_at": None, "incident": None}

    row = deps["alert"]("a1")
    assert row["id"] == "a1" and row["host"] == "box"

    res = deps["config_get"]("manager.poll_interval")
    assert res["value"] is not None and "help" in res

    secret = deps["config_get"]("manager.discord.bot_token")
    assert secret == {"path": "manager.discord.bot_token", "value": "***", "secret": True}

    with pytest.raises(ValueError):
        deps["config_get"]("/etc/passwd")

    import agent_registry
    monkeypatch.setattr(agent_registry, "pinned_agent",
                        lambda prov, model: {"hostname": "Mac"} if (prov, model) == ("lms", "gemma-3-12b-it") else None)
    assert deps["pinned"]("lms", "mac", "gemma-3-12b-it") is True
    assert deps["pinned"]("lms", "box", "gemma-3-12b-it") is False and deps["pinned"]("lms", "mac", "other") is False
    monkeypatch.setattr(agent_registry, "pinned_agent", lambda *a: (_ for _ in ()).throw(OSError("registry")))
    assert deps["pinned"]("lms", "mac", "gemma-3-12b-it") is False


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
    st = tt.alarm_stats(rows, "rule", 10, "last 30d", tz=timezone.utc)
    assert st["total"] == 4 and st["groups"] == 2 and st["window"] == "last 30d"
    assert st["by"][0] == {"key": "GPU temp high", "count": 2, "critical": 2, "warning": 0, "info": 0}
    assert st["by"][1]["key"] == "RAM high" and st["by"][1]["count"] == 2 and st["by"][1]["warning"] == 1
    assert st["chart"].splitlines()[0].startswith("GPU temp high  ") and st["chart"].splitlines()[0].endswith(" 2")
    assert st["first"] == "2026-09-11 12:00" and st["last"] == "2026-09-12 12:00"
    days = tt.alarm_stats(rows, "day", 10, "", tz=timezone.utc)["by"]
    assert [(d["key"], d["count"]) for d in days] == [("2026-09-11", 1), ("2026-09-12", 3)]
    assert [h["key"] for h in tt.alarm_stats(rows, "host", 1, "", tz=timezone.utc)["by"]] == ["box"]
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
    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {"fleet": lambda: [], "host": lambda n: None,
                                                                "ack": lambda aid: (True, None), "close": lambda aid: (True, None)})
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
                        service_health=lambda: {}, gateway_entries=lambda: [], audit_rows=lambda *a, **k: [])
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
    assert {t.name for t in tt.catalog(reg, _cfg(), "operator")} == set(tt.TOOL_NAMES) - {"config_get", "audit_log"} - ACT


def test_audit_log_is_admin_only_and_filters():
    reg = tt.build_registry(_deps())
    assert "audit_log" not in {t.name for t in tt.catalog(reg, _cfg(), "operator")}
    assert "audit_log" in {t.name for t in tt.catalog(reg, _cfg(), "admin")}
    res, ok = tt.run_tool(reg["audit_log"], {"actor": "bob", "count": 1})
    assert ok and len(res) == 1 and res[0]["actor"] == "bob"
    _, err = tt.validate_args(reg["audit_log"], {"count": 500})
    assert err is not None and "count" in err


def test_load_and_unload_reject_vllm_but_restart_still_allows_it():
    reg = tt.build_registry(_deps())
    _, err = tt.validate_args(reg["load_model"], {"provider": "vllm", "host": "box", "model": "m"})
    assert err == "provider must be one of llama, lms"
    _, err = tt.validate_args(reg["unload_model"], {"provider": "vllm", "host": "box", "model": "m"})
    assert err == "provider must be one of llama, lms"
    _, err = tt.validate_args(reg["restart_provider"], {"provider": "vllm", "host": "box"})
    assert err is None


def test_models_rows_with_a_host_filter_ignores_models_loaded_elsewhere():
    loaded = {("llama", "qwen"): ["other-box"]}
    entries = [{"id": "qwen", "provider": "llama", "hosts": ["other-box"]}, {"id": "gemma", "provider": "lms", "hosts": ["mac"]}]
    assert [r["model"] for r in tt.models_rows(entries, loaded, host="mac")] == ["gemma"]
    assert tt.models_rows(entries, loaded, host="nowhere") == []


def test_model_profiles_tool_is_a_plain_read_tool():
    reg = tt.build_registry(_deps())
    t = reg["model_profiles"]
    assert (t.kind, t.tier, t.role) == ("read", "read", "operator")
    assert set(t.params["properties"]) == {"host", "model", "profile"} and t.params["required"] == []
    assert "model_profiles" in {x.name for x in tt.catalog(reg, _cfg(), "operator")}
    assert "use model_profiles" in reg["config_get"].description
    res, ok = tt.run_tool(t, {"host": "box"})
    assert ok and res["hosts"][0]["models"][0]["profiles"] == ["default", "long-ctx"]


def test_profiles_help_topic_names_the_llm_control_tab():
    assert tt.default_help("profiles").startswith("Model profiles are saved llama-server flag sets")
    assert "LLM Control tab" in tt.default_help("model profiles")
    assert "Admin" not in tt.default_help("profiles")


def _profile_deps(monkeypatch, tmp_path, agents=None):
    """prod_deps wired to a real ProfileStore in tmp_path and a fake agent registry."""
    import agent_registry
    import discord_bot
    import model_profiles

    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {
        "fleet": lambda: [], "host": lambda n: None,
        "ack": lambda aid: (True, None), "close": lambda aid: (True, None)})
    store = model_profiles.ProfileStore(tmp_path / "p.json")
    store.put_profile("a1", "Qwen3-14B", "default", {"ctx": 4096}, make_active=True)
    store.put_profile("a1", "Qwen3-14B", "long-ctx", {"ctx": 32768})
    store.put_profile("a2", "gemma-3-12b", "default", {"ctx": 8192}, make_active=True)
    store.put_profile("a2", "Qwen3-14B", "mac", {"ctx": 2048}, make_active=True)
    monkeypatch.setattr(model_profiles, "STORE", store)
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents if agents is not None else {
        "a1": {"status": "approved", "hostname": "Box"},
        "a2": {"status": "approved", "hostname": "mac"},
        "a3": {"status": "pending", "hostname": "ghost"}}})
    ctx = types.SimpleNamespace(alarm_engine_url=lambda: "http://ae.local", ae_session=None)
    return tt.prod_deps(ctx, db_path="unused", tools_runs=lambda *a, **k: [], speed_table=lambda *a: [],
                        service_health=lambda: {}, gateway_entries=lambda: [], audit_rows=lambda *a, **k: [])["profiles"]


def test_prod_deps_profiles_summarises_every_host_and_filters_by_host(monkeypatch, tmp_path):
    profiles = _profile_deps(monkeypatch, tmp_path)
    out = profiles()
    assert [h["host"] for h in out["hosts"]] == ["Box", "mac"]
    box = out["hosts"][0]["models"][0]
    assert box == {"model": "Qwen3-14B", "active": "default", "profiles": ["default", "long-ctx"]}
    only = profiles("box")
    assert [h["host"] for h in only["hosts"]] == ["Box"]
    assert profiles("nope") == {"error": "unknown host"}


def test_prod_deps_profiles_returns_the_active_or_named_values(monkeypatch, tmp_path):
    profiles = _profile_deps(monkeypatch, tmp_path)
    row = profiles("box", "Qwen3-14B")
    assert row == {"host": "Box", "model": "Qwen3-14B", "active": "default",
                   "profiles": ["default", "long-ctx"], "values": {"ctx": 4096}}
    assert profiles("box", "qwen3-14b")["values"] == {"ctx": 4096}
    assert profiles("box", "qwen3")["model"] == "Qwen3-14B"
    assert profiles("box", "Qwen3-14B", "long-ctx")["values"] == {"ctx": 32768}
    assert profiles("box", "Qwen3-14B", "nope") == {"error": "unknown profile"}
    assert profiles("box", "nothing-like-this") == {"error": "unknown model"}


def test_prod_deps_profiles_without_a_host_lists_one_row_per_host(monkeypatch, tmp_path):
    profiles = _profile_deps(monkeypatch, tmp_path)
    rows = profiles(None, "Qwen3-14B")["rows"]
    assert [(r["host"], r["active"], r["values"]) for r in rows] == [
        ("Box", "default", {"ctx": 4096}), ("mac", "mac", {"ctx": 2048})]
    assert profiles(None, "no-such-model") == {"rows": []}


def test_prod_deps_profiles_skips_unapproved_agents_and_a_missing_store(monkeypatch, tmp_path):
    import model_profiles
    profiles = _profile_deps(monkeypatch, tmp_path)
    assert "ghost" not in {h["host"] for h in profiles()["hosts"]}
    monkeypatch.setattr(model_profiles, "STORE", None)
    assert profiles() == {"error": "profile store not available"}


# ── #978: closed/acknowledged stamps, local time ─────────────────────

@pytest.fixture
def new_york(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_local_ts_rewrites_utc_stamps_in_the_local_zone_and_passes_junk_through(new_york):
    assert tt.local_ts("2026-09-12T12:00:00Z") == "2026-09-12T08:00:00-04:00"
    assert tt.local_ts("2026-01-12T12:00:00+00:00") == "2026-01-12T07:00:00-05:00"
    assert tt.local_ts("junk") == "junk" and tt.local_ts(None) is None and tt.local_ts("") == ""


def test_alarm_stats_days_and_first_last_follow_the_zone():
    # 2026-09-12T02:00Z is still 2026-09-11 in New York.
    rows = [{"triggered_at": "2026-09-12T02:00:00Z", "rule": "r", "severity": "warning"}]
    utc = tt.alarm_stats(rows, "day", 10, "", tz=timezone.utc)
    ny = tt.alarm_stats(rows, "day", 10, "", tz=timezone(timedelta(hours=-4), "EDT"))
    assert [d["key"] for d in utc["by"]] == ["2026-09-12"] and utc["first"] == "2026-09-12 02:00" and utc["timezone"] == "UTC"
    assert [d["key"] for d in ny["by"]] == ["2026-09-11"] and ny["first"] == "2026-09-11 22:00" and ny["timezone"] == "EDT"


def _ae_deps(monkeypatch, alert_rows, csv_text=""):
    import discord_bot
    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {"fleet": lambda: [], "host": lambda n: None,
                                                              "ack": lambda a: (True, None), "close": lambda a: (True, None)})

    class _Resp:
        def __init__(self, body, text=""):
            self.ok, self._body, self.text = True, body, text
        def raise_for_status(self): pass
        def json(self): return self._body

    class _Session:
        def get(self, url, timeout=10):
            if "export" in url:
                return _Resp(None, csv_text)
            if "alerts/?" in url:
                limit = int(url.split("limit=")[1].split("&")[0])
                rows = alert_rows if "include_closed" in url else [r for r in alert_rows if r.get("status") in ("active", "acknowledged")]
                return _Resp(rows[:limit])
            return _Resp(alert_rows[0])
    ctx = types.SimpleNamespace(alarm_engine_url=lambda: "http://ae.local", ae_session=_Session())
    return tt.prod_deps(ctx, db_path="unused", tools_runs=lambda *a, **k: [], speed_table=lambda *a: [],
                        service_health=lambda: {}, gateway_entries=lambda: [], audit_rows=lambda *a, **k: [])


def test_alert_rows_carry_closed_and_acknowledged_times_in_local_time(monkeypatch, new_york):
    row = {"alert_id": "a1", "rule_name": "GPU temp high", "severity": "critical", "status": "closed", "source_host": "box",
           "message": "hot", "created_at": "2026-09-12T12:00:00+00:00", "last_evaluated_at": "2026-09-12T12:30:00+00:00",
           "acknowledged_at": "2026-09-12T12:10:00+00:00", "closed_at": "2026-09-12T12:30:00+00:00"}
    deps = _ae_deps(monkeypatch, [row])
    got = deps["alarms"]("closed", 5)[0]
    assert got["triggered_at"] == "2026-09-12T08:00:00-04:00" and got["last_seen"] == "2026-09-12T08:30:00-04:00"
    assert got["acknowledged_at"] == "2026-09-12T08:10:00-04:00" and got["closed_at"] == "2026-09-12T08:30:00-04:00"
    csv = ("alert_id,rule_name,severity,status,source_host,message,created_at,acknowledged_at,closed_at\n"
           "a2,RAM high,warning,closed,box,full,2026-09-12T12:00:00+00:00,,2026-09-12T13:00:00+00:00\n")
    deps = _ae_deps(monkeypatch, [row] * 1000, csv)
    hist = deps["alarm_history"]("90d", "rule", 10)
    assert hist["total"] == 1 and hist["timezone"] == "EDT" and hist["by"][0]["key"] == "RAM high"


# ── #957: agent connect failures never show the agent URL ────────────

def test_scrub_agent_error_hides_urls_and_connect_failures():
    assert tt.scrub_agent_error("https://10.0.0.5:8443/llama/server/wake: request failed", "box") == "could not reach the agent on box"
    assert tt.scrub_agent_error("no callback URL recorded", "box") == "could not reach the agent on box"
    assert tt.scrub_agent_error(None, "box") == "could not reach the agent on box"
    assert tt.scrub_agent_error("model not found", "box") == "model not found"
    assert tt.scrub_agent_error("503", "box") == "503"


def _agent_deps(monkeypatch, agents, calls):
    import agent_registry
    import discord_bot
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})
    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {"fleet": lambda: [], "host": lambda n: None,
                                                              "ack": lambda a: (True, None), "close": lambda a: (True, None)})

    def fake_call(agent, method, path, **kw):
        calls.append((agent["hostname"], method, path))
        return False, "https://10.0.0.5:8443" + path + ": request failed"
    monkeypatch.setattr(discord_bot, "_agent_call", fake_call)
    ctx = types.SimpleNamespace(alarm_engine_url=lambda: "http://ae.local", ae_session=None)
    return tt.prod_deps(ctx, db_path="unused", tools_runs=lambda *a, **k: [], speed_table=lambda *a: [],
                        service_health=lambda: {}, gateway_entries=lambda: [], audit_rows=lambda *a, **k: [])


def test_act_deps_report_a_fixed_line_when_the_agent_cannot_be_reached(monkeypatch):
    import provider_state
    agents = {"A1": {"hostname": "box", "status": "approved", "capabilities": {"llama": True}}}
    calls: list = []
    deps = _agent_deps(monkeypatch, agents, calls)
    monkeypatch.setattr(provider_state, "STORE", provider_state._ProviderSampleStore())
    provider_state.STORE.put("llama", "A1", {"llama": {"state": "awake", "model": "qwen3"}})
    assert deps["wake"]("box") == (False, "could not reach the agent on box")
    assert deps["unload"]("llama", "box", "qwen3") == (False, "could not reach the agent on box")
    assert calls == [("box", "POST", "/llama/server/wake"), ("box", "POST", "/llama/unload")]


# ── #967: act tools and the models tool skip a stale or unknown llama sample ──

def test_wake_and_load_skip_a_stale_or_unknown_llama_sample(monkeypatch):
    import provider_state
    agents = {"A1": {"hostname": "box", "status": "approved", "capabilities": {"llama": True}}}
    calls: list = []
    deps = _agent_deps(monkeypatch, agents, calls)
    store = provider_state._ProviderSampleStore()
    monkeypatch.setattr(provider_state, "STORE", store)
    assert deps["wake"]("box") == (False, "skipped: the llama sample from box is stale; try again shortly")
    store.put("llama", "A1", {"llama": {"state": "unknown"}})
    assert deps["load"]("llama", "box", "qwen3") == (False, "skipped: the llama sample from box is unknown; try again shortly")
    store.put("llama", "A1", {"llama": {"residency": {"aggregate": "sleeping", "models": []}}})
    deps["wake"]("box")
    assert calls == [("box", "POST", "/llama/server/wake")]
    # unload never needs a fresh sample
    store.put("llama", "A1", {"llama": {"state": "unknown"}})
    deps["unload"]("llama", "box", "qwen3")
    assert calls[-1] == ("box", "POST", "/llama/unload")


def test_models_tool_flags_hosts_without_a_fresh_llama_sample(monkeypatch):
    import provider_state
    agents = {"A1": {"hostname": "box", "status": "approved", "capabilities": {"llama": True}},
              "A2": {"hostname": "mac", "status": "approved", "capabilities": {"lms": True}}}
    deps = _agent_deps(monkeypatch, agents, [])
    store = provider_state._ProviderSampleStore()
    monkeypatch.setattr(provider_state, "STORE", store)
    store.put("llama", "A1", {"llama": {"state": "awake", "model": "qwen3"}})
    store.put("lms", "A2", {"ps_ok": True, "ps": [{"model": "gemma", "status": "LOADED"}]})
    rows = deps["models"]()
    assert isinstance(rows, list) and {(r["model"], tuple(r["loaded_on"])) for r in rows} == {("qwen3", ("box",)), ("gemma", ("mac",))}
    store.put("llama", "A1", {"llama": {"state": "unknown"}})
    out = deps["models"]()
    assert out["note"] == "llama residency not counted for hosts without a fresh sample: box (unknown)"
    assert [r["model"] for r in out["models"]] == ["gemma"]


# ── #981: arbitrary ranges, paging, severity, search, status in history, two-level grouping, incidents ──

def test_parse_when_accepts_relative_dates_and_iso():
    now = tt.parse_ts("2026-09-14T12:00:00+00:00")
    assert tt.parse_when("2h", now) == now - 7200 and tt.parse_when("3 days ago", now) == now - 3 * 86400
    assert tt.parse_when("1w", now) == now - 7 * 86400 and tt.parse_when("30m", now) == now - 1800
    assert tt.parse_when("2026-09-12T10:00:00Z", now) == now - 50 * 3600
    assert tt.parse_when("junk") is None and tt.parse_when("") is None and tt.parse_when(None) is None
    assert tt.parse_when(5.0) == 5.0
    start, end, label, err = tt.time_range(None, "2h", "1h", now)
    assert (start, end, label, err) == (now - 7200, now - 3600, "since 2h until 1h", None)
    assert tt.time_range("7d", None, None, now) == (now - 7 * 86400, None, "last 7d", None)
    assert tt.time_range(None, "junk", None, now)[3].startswith("since is not a time")
    assert tt.time_range(None, None, "junk", now)[3].startswith("until is not a time")


def test_filter_alerts_by_severity_search_and_bounds():
    now = tt.parse_ts("2026-09-12T12:00:00+00:00")
    rows = [_row(1, sev="critical"), _row(2, sev="warning", age_s=3 * 86400), _row(3, host="mac", rule="RAM high", age_s=40 * 86400)]
    assert [r["id"] for r in tt.filter_alerts(rows, severity="WARNING", now=now)] == ["a2"]
    assert [r["id"] for r in tt.filter_alerts(rows, search="ram", now=now)] == ["a3"]
    assert [r["id"] for r in tt.filter_alerts(rows, search="MAC", now=now)] == ["a3"]
    assert [r["id"] for r in tt.filter_alerts(rows, start=now - 5 * 86400, end=now - 86400, now=now)] == ["a2"]
    assert [r["id"] for r in tt.filter_alerts(rows, end=now, now=now)] == ["a2", "a3"]    # until is exclusive


def test_incidents_fold_members_under_their_root():
    rows = [{"id": "r1", "rule": "GPU temp high", "host": "box", "severity": "warning", "status": "active",
             "triggered_at": "2026-09-12T12:00:00Z", "incident": "r1"},
            {"id": "m2", "rule": "Fan speed", "host": "box", "severity": "critical", "status": "closed",
             "triggered_at": "2026-09-12T12:05:00Z", "incident": "r1"},
            {"id": "s3", "rule": "RAM high", "host": "mac", "severity": "info", "status": "closed",
             "triggered_at": "2026-09-11T12:00:00Z", "incident": None}]
    inc = tt.incidents(rows)
    assert [i["incident"] for i in inc] == ["r1", "s3"]
    assert inc[0]["rule"] == "GPU temp high" and inc[0]["members"] == 2 and inc[0]["severity"] == "critical" and inc[0]["status"] == "active"
    assert [a["id"] for a in inc[0]["alerts"]] == ["r1", "m2"] and inc[1]["members"] == 1 and inc[1]["status"] == "closed"


def test_alarm_stats_second_dimension_hours_and_status():
    rows = [_row(1, status="active"), _row(2, host="mac", status="active"), _row(3, rule="RAM high", sev="warning", age_s=3600, status="active"), _row(4)]
    st = tt.alarm_stats(rows, "rule", 10, "", tz=timezone.utc, then_by="host")
    assert st["by"][0]["key"] == "GPU temp high" and st["by"][0]["by_host"] == [{"key": "box", "count": 2}, {"key": "mac", "count": 1}]
    hours = tt.alarm_stats(rows, "hour", 10, "", tz=timezone.utc)["by"]
    assert [(h["key"], h["count"]) for h in hours] == [("2026-09-12 11:00", 1), ("2026-09-12 12:00", 3)]
    hod = tt.alarm_stats(rows, "hour_of_day", 1, "", tz=timezone.utc)["by"]
    assert [(h["key"], h["count"]) for h in hod] == [("11:00", 1), ("12:00", 3)]
    assert [(g["key"], g["count"]) for g in tt.alarm_stats(rows, "status", 10, "", tz=timezone.utc)["by"]] == [("active", 3), ("closed", 1)]


def _ae_rows(n, **over):
    out = []
    for i in range(n):
        out.append({"alert_id": f"a{i}", "rule_name": "RAM high" if i % 3 else "GPU temp high", "severity": "warning" if i % 3 else "critical",
                    "status": "closed" if i % 2 else "active", "source_host": "mac" if i % 4 == 0 else "box", "message": f"value {i} above threshold",
                    "created_at": f"2026-09-{14 - (i // 10):02d}T12:00:00+00:00", "last_evaluated_at": "2026-09-14T12:30:00+00:00",
                    "incident_id": "a0" if i < 3 else None, **over})
    return out


def test_alarm_search_pages_filters_and_groups(monkeypatch):
    deps = _ae_deps(monkeypatch, _ae_rows(30))
    out = deps["alarm_search"]({"status": "all", "count": 10, "offset": 0})
    assert out["total"] == 30 and len(out["alerts"]) == 10 and out["next_offset"] == 10 and out["alerts"][0]["id"] == "a0"
    out = deps["alarm_search"]({"status": "all", "count": 10, "offset": 25})
    assert [a["id"] for a in out["alerts"]] == [f"a{i}" for i in range(25, 30)] and out["next_offset"] is None
    out = deps["alarm_search"]({"status": "all", "severity": "critical", "count": 100})
    assert out["total"] == 10 and all(a["severity"] == "critical" for a in out["alerts"])
    out = deps["alarm_search"]({"status": "closed", "search": "value 7", "count": 100})
    assert [a["id"] for a in out["alerts"]] == ["a7"]
    out = deps["alarm_search"]({"status": "all", "since": "2026-09-13T00:00:00Z", "until": "2026-09-14T00:00:00Z", "count": 100})
    assert out["total"] == 10 and out["range"] == "since 2026-09-13T00:00:00Z until 2026-09-14T00:00:00Z"
    assert deps["alarm_search"]({"status": "all", "since": "yesterday-ish"}) == {"error": "since is not a time (use 2h, 3d, 1w, a date or ISO-8601)"}
    inc = deps["alarm_search"]({"status": "all", "group": "incident", "count": 2})
    assert inc["total"] == 28 and inc["incidents"][0]["members"] == 3 and inc["next_offset"] == 2
    # the active default with no filters is the cheap direct read
    fast = deps["alarm_search"]({"status": "active", "count": 3})
    assert fast["total"] == 3 and fast["next_offset"] is None


def test_alarm_history_honours_status_severity_search_and_then_by(monkeypatch):
    deps = _ae_deps(monkeypatch, _ae_rows(30))
    hist = deps["alarm_history"]("90d", "rule", 10, None, None, {"status": "active", "then_by": "host"})
    assert hist["total"] == 15 and hist["by"][0]["key"] == "RAM high" and "by_host" in hist["by"][0]
    hist = deps["alarm_history"]("90d", "host", 10, None, None, {"severity": "critical", "search": "value"})
    assert hist["total"] == 10 and {g["key"] for g in hist["by"]} == {"box", "mac"}
    hist = deps["alarm_history"]("30d", "day", 10, None, None, {"since": "2026-09-13T00:00:00Z"})
    assert hist["total"] == 20 and hist["window"] == "since 2026-09-13T00:00:00Z"
    assert deps["alarm_history"]("30d", "day", 10, None, None, {"until": "nope"}) == {"error": "until is not a time (use 2h, 3d, 1w, a date or ISO-8601)"}


# ── #982: host_detail sections and several hosts; host_history trends ──

def test_host_section_slices_a_full_detail():
    d = {"hostname": "box", "online": True, "liveness": "live", "age_s": 3, "cpu_pct": 12.0, "ram_pct": 40.0, "gpu_pct": 5.0,
         "gpu_temp_c": 61.0, "watts": 210.0, "provider_states": {"llama.cpp": "awake"},
         "hardware": {"cpu": "Ryzen 7 9700X", "cores": 8, "ram_gb": 64.0, "gpu": {"name": "Navi 31", "vram_gb": 24.0}, "disks": [{"mount": "/"}], "os": "linux"},
         "live": {"cpu": {"pct": 12.0, "temp_c": 55.0}, "ram": {"pct": 40.0}, "swap": {"pct": 0}, "gpu": {"temperature_c": 61.0},
                  "disks": [{"mount": "/", "used_pct": 41.0}], "power": {"watts": 210.0, "source": "psu"}, "providers": {"llama": {"state": "awake"}}}}
    assert tt.host_section(d, "all") is d
    assert tt.host_section(d, "summary") == {k: d[k] for k in ("hostname", "online", "liveness", "age_s", "cpu_pct", "ram_pct", "gpu_pct", "gpu_temp_c", "watts", "provider_states")}
    assert tt.host_section(d, "cpu") == {"hostname": "box", "online": True, "cpu_model": "Ryzen 7 9700X", "cores": 8, "cpu": {"pct": 12.0, "temp_c": 55.0}}
    assert tt.host_section(d, "gpu") == {"hostname": "box", "online": True, "gpu_model": {"name": "Navi 31", "vram_gb": 24.0}, "gpu": {"temperature_c": 61.0}}
    assert tt.host_section(d, "power") == {"hostname": "box", "online": True, "watts": 210.0, "power": {"watts": 210.0, "source": "psu"}}
    assert tt.host_section(d, "providers") == {"hostname": "box", "online": True, "provider_states": {"llama.cpp": "awake"}, "providers": {"llama": {"state": "awake"}}}
    assert tt.host_section(d, "hardware") == {"hostname": "box", "online": True, "hardware": d["hardware"]}
    assert tt.host_section(d, "disks")["disks"] == [{"mount": "/", "used_pct": 41.0}]
    assert tt.host_names("all", ["box", "mac"]) == ["box", "mac"] and tt.host_names(" box, MAC ,box", []) == ["box", "MAC"] and tt.host_names("", ["box"]) == []


def test_prod_host_detail_handles_sections_and_several_hosts(monkeypatch):
    import agent_registry
    import discord_bot
    import energy
    agents = {"A1": {"hostname": "box", "status": "approved", "capabilities": {"llama": True}, "os": "linux"},
              "A2": {"hostname": "mac", "status": "approved", "capabilities": {"lms": True}, "os": "darwin"}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})
    monkeypatch.setattr(agent_registry, "agent_liveness", lambda a: "live")
    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {
        "fleet": lambda: [], "ack": lambda a: (True, None), "close": lambda a: (True, None),
        "host": lambda name: {"hostname": name, "online": True, "cpu_pct": 12.0, "gpu_temp_c": 61.0, "watts": 200.0,
                              "provider_states": {}} if name in ("box", "mac") else None})
    monkeypatch.setattr(energy, "store_view_from_provider_state", lambda: {})
    ctx = types.SimpleNamespace(alarm_engine_url=lambda: "http://ae.local", ae_session=None)
    deps = tt.prod_deps(ctx, db_path="unused", tools_runs=lambda *a, **k: [], speed_table=lambda *a: [],
                        service_health=lambda: {}, gateway_entries=lambda: [], audit_rows=lambda *a, **k: [])
    one = deps["host"]("box", "all")
    assert one["hostname"] == "box" and "hardware" in one and "live" in one
    assert deps["host"]("box", "summary") == {"hostname": "box", "online": True, "liveness": "live", "cpu_pct": 12.0, "gpu_temp_c": 61.0, "watts": 200.0, "provider_states": {}}
    both = deps["host"]("all", "all")
    assert [h["hostname"] for h in both["hosts"]] == ["box", "mac"] and "hardware" not in both["hosts"][0]
    mixed = deps["host"]("box,nope", "power")
    assert mixed["hosts"][0]["watts"] == 200.0 and mixed["hosts"][1] == {"hostname": "nope", "error": "unknown host"}
    assert deps["host"]("nope", "all") is None and deps["host"]("", "all") is None


def test_history_summary_stats_trend_and_sparkline(new_york):
    pts = [{"timestamp": f"2026-09-14T{h:02d}:00:00+00:00", "value": v, "hostname": "box"} for h, v in enumerate([10, 12, 11, 30, 55, 80])]
    out = tt.history_summary(pts, "gpu_temp_c", "6h", "box", "C")
    assert out["points"] == 6 and out["min"] == 10 and out["max"] == 80 and out["avg"] == 33.0 and out["latest"] == 80
    assert out["trend"] == "rising" and out["sparkline"] == "▁▁▁▃▅█" and out["unit"] == "C"
    assert out["first"] == "2026-09-13T20:00:00-04:00" and out["series"][-1] == {"t": "2026-09-14T01:00:00-04:00", "v": 80.0}
    flat = tt.history_summary([{"timestamp": "2026-09-14T00:00:00Z", "value": 5}, {"timestamp": "2026-09-14T01:00:00Z", "value": 5}], "cpu_pct", "1h", "box")
    assert flat["trend"] == "flat" and flat["sparkline"] == "▄▄"
    assert tt.history_summary([], "cpu_pct", "1h", "box") == {"host": "box", "metric": "cpu_pct", "unit": "", "window": "1h", "points": 0, "note": "no samples in this window"}
    assert tt.history_summary([{"timestamp": "junk", "value": 1}, {"timestamp": "2026-09-14T00:00:00Z", "value": "x"}], "cpu_pct", "1h", "box")["points"] == 0


def test_prod_host_history_reads_the_alarm_engine_series(monkeypatch):
    import agent_registry
    import discord_bot
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": {"A1": {"hostname": "box", "status": "approved"}}})
    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {"fleet": lambda: [], "host": lambda n: None,
                                                              "ack": lambda a: (True, None), "close": lambda a: (True, None)})
    urls = []

    class _Resp:
        ok = True
        def json(self):
            return [{"timestamp": "2026-09-14T00:00:00Z", "value": 40.0, "hostname": "box"}, {"timestamp": "2026-09-14T01:00:00Z", "value": 60.0, "hostname": "box"}]

    class _Session:
        def get(self, url, timeout=10):
            urls.append(url)
            return _Resp()
    ctx = types.SimpleNamespace(alarm_engine_url=lambda: "http://ae.local", ae_session=_Session())
    deps = tt.prod_deps(ctx, db_path="unused", tools_runs=lambda *a, **k: [], speed_table=lambda *a: [],
                        service_health=lambda: {}, gateway_entries=lambda: [], audit_rows=lambda *a, **k: [])
    out = deps["host_history"]("BOX", "psu_watts", "7d")
    assert urls == ["http://ae.local/api/alarm/metrics/system/liquidctl_psu_Total%20power%20output_value?since_minutes=10080&hostname=box&max_points=24&agg=mean"]
    assert out["host"] == "box" and out["unit"] == "W" and out["min"] == 40.0 and out["max"] == 60.0 and out["trend"] == "rising"
    assert deps["host_history"]("nope", "cpu_pct") == {"host": "nope", "error": "unknown host"}
    reg = tt.build_registry(deps)
    args, err = tt.validate_args(reg["host_history"], {"host": "box", "metric": "cpu_pct"})
    assert err is None and args == {"host": "box", "metric": "cpu_pct", "window": "24h"}
    assert tt.validate_args(reg["host_history"], {"host": "box", "metric": "load_avg"})[1] == "metric must be one of " + ", ".join(tt.HISTORY_METRICS)


def test_alarm_search_escalates_to_the_csv_only_for_a_deep_range_or_page(monkeypatch):
    csv = "alert_id,rule_name,severity,status,source_host,message,created_at,acknowledged_at,closed_at\n" + "".join(
        f"c{i},Old rule,warning,closed,box,old,2026-06-01T00:00:00+00:00,,2026-06-01T01:00:00+00:00\n" for i in range(1200))
    deps = _ae_deps(monkeypatch, _ae_rows(1000), csv)
    out = deps["alarm_search"]({"status": "closed", "count": 10})
    assert out["total"] == 500 and out["note"] == "capped at the newest 1000 alerts" and out["alerts"][0]["id"] == "a1"
    deep = deps["alarm_search"]({"status": "all", "count": 100, "offset": 1000})
    assert deep["note"] == "capped at the newest 10000 alerts" and deep["total"] == 1200 and deep["alerts"][0]["id"] == "c1000"
    old = deps["alarm_search"]({"status": "all", "since": "2026-05-01", "until": "2026-07-01", "count": 5})
    assert old["note"] == "capped at the newest 10000 alerts" and old["total"] == 1200
    recent = deps["alarm_search"]({"status": "all", "window": "24h", "count": 5})
    assert recent["note"] == "capped at the newest 1000 alerts"


# ── #983: hosts_overview filters, sort and resource metrics; host_history over several hosts ──

def test_overview_rows_filter_and_sort():
    rows = [{"hostname": "box", "providers": ["llama"], "online": True, "busy": True, "watts": 210.0, "cpu_pct": 12.0, "age_s": 3},
            {"hostname": "mac", "providers": ["lms"], "online": True, "busy": False, "watts": None, "cpu_pct": 40.0, "age_s": 8},
            {"hostname": "vm", "providers": ["llama", "vllm"], "online": False, "busy": False, "watts": 90.0, "age_s": 400}]
    names = lambda out: [r["hostname"] for r in out]  # noqa: E731
    assert names(tt.overview_rows(rows, provider="llama")) == ["box", "vm"]
    assert names(tt.overview_rows(rows, online=True, busy=False)) == ["mac"]
    assert names(tt.overview_rows(rows, sort_by="watts")) == ["box", "vm", "mac"]
    assert names(tt.overview_rows(rows, sort_by="cpu_pct")) == ["mac", "box", "vm"]
    assert names(tt.overview_rows(rows, sort_by="age_s")) == ["box", "mac", "vm"]
    assert names(tt.overview_rows(rows, sort_by="hostname")) == ["box", "mac", "vm"]
    assert names(tt.overview_rows(rows, sort_by="bogus")) == ["box", "mac", "vm"]


def test_prod_hosts_overview_adds_metrics_and_honours_the_tool_args(monkeypatch):
    import agent_registry
    import discord_bot
    agents = {"A1": {"hostname": "box", "status": "approved"}, "A2": {"hostname": "mac", "status": "approved"}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})
    monkeypatch.setattr(agent_registry, "agent_liveness", lambda a: "live" if a["hostname"] == "box" else "stale")
    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {
        "fleet": lambda: [{"hostname": "box", "providers": ["llama"], "online": True, "busy": True, "watts": 210.0, "age_s": 3},
                          {"hostname": "mac", "providers": ["lms"], "online": True, "busy": False, "watts": None, "age_s": 40}],
        "host": lambda name: {"hostname": name, "cpu_pct": 12.0 if name == "box" else 55.0, "ram_pct": 40.0, "gpu_pct": None, "gpu_temp_c": 61.0},
        "ack": lambda a: (True, None), "close": lambda a: (True, None)})
    ctx = types.SimpleNamespace(alarm_engine_url=lambda: "http://ae.local", ae_session=None)
    deps = tt.prod_deps(ctx, db_path="unused", tools_runs=lambda *a, **k: [], speed_table=lambda *a: [],
                        service_health=lambda: {}, gateway_entries=lambda: [], audit_rows=lambda *a, **k: [])
    rows = deps["hosts"]()
    assert rows[0] == {"hostname": "box", "providers": ["llama"], "online": True, "busy": True, "watts": 210.0, "age_s": 3,
                       "liveness": "live", "cpu_pct": 12.0, "ram_pct": 40.0, "gpu_temp_c": 61.0}
    assert rows[1]["online"] is False and rows[1]["liveness"] == "stale" and rows[1]["cpu_pct"] == 55.0
    assert [r["hostname"] for r in deps["hosts"](None, None, None, "cpu_pct")] == ["mac", "box"]
    assert [r["hostname"] for r in deps["hosts"]("llama")] == ["box"]
    reg = tt.build_registry(deps)
    args, err = tt.validate_args(reg["hosts_overview"], {"online": True, "sort_by": "watts"})
    assert err is None and args == {"online": True, "sort_by": "watts"}
    assert tt.validate_args(reg["hosts_overview"], {"busy": "yes"})[1] == "busy must be true or false"
    out, ok = tt.run_tool(reg["hosts_overview"], args)
    assert ok and [r["hostname"] for r in out] == ["box"]


def test_prod_host_history_summarises_several_hosts(monkeypatch):
    import agent_registry
    import discord_bot
    agents = {"A1": {"hostname": "box", "status": "approved"}, "A2": {"hostname": "mac", "status": "approved"}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})
    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {"fleet": lambda: [], "host": lambda n: None,
                                                              "ack": lambda a: (True, None), "close": lambda a: (True, None)})

    class _Resp:
        def __init__(self, host): self.ok, self._h = True, host
        def json(self):
            base = 40.0 if self._h == "box" else 70.0
            return [{"timestamp": "2026-09-14T00:00:00Z", "value": base, "hostname": self._h}, {"timestamp": "2026-09-14T01:00:00Z", "value": base + 10, "hostname": self._h}]

    class _Session:
        def get(self, url, timeout=10):
            return _Resp("box" if "hostname=box" in url else "mac")
    ctx = types.SimpleNamespace(alarm_engine_url=lambda: "http://ae.local", ae_session=_Session())
    deps = tt.prod_deps(ctx, db_path="unused", tools_runs=lambda *a, **k: [], speed_table=lambda *a: [],
                        service_health=lambda: {}, gateway_entries=lambda: [], audit_rows=lambda *a, **k: [])
    out = deps["host_history"]("all", "cpu_pct", "6h")
    assert out["metric"] == "cpu_pct" and out["unit"] == "%" and [h["host"] for h in out["hosts"]] == ["box", "mac"]
    assert out["hosts"][1]["max"] == 80.0 and "series" not in out["hosts"][1] and out["hosts"][0]["trend"] == "rising"
    mixed = deps["host_history"]("mac, nope", "cpu_pct")
    assert mixed["hosts"][1] == {"host": "nope", "error": "unknown host"}
    assert "series" in deps["host_history"]("box", "cpu_pct")


# ── #984: log_tail search, level, since, paging, more sources, several hosts ──

LOG = ["2026-09-14 20:00:00 [INFO] boot",
       "2026-09-14 20:05:00 [WARNING] slot pressure",
       "2026-09-14 20:10:00 [ERROR] load failed",
       "Traceback (most recent call last):",
       "  ValueError: bad gguf",
       "2026-09-14 20:20:00 [INFO] loaded qwen3",
       "2026-09-14 20:30:00 [INFO] request done",
       "2026-09-14 20:31:00 [INFO] ALERT CREATED rule=GPU hot severity=critical message=error budget ok"]


def test_filter_log_lines_search_level_since_and_paging(new_york):
    assert tt.filter_log_lines(LOG, count=2) == {"lines": LOG[-2:], "matched": 8, "older": 6, "newer": 0}
    assert tt.filter_log_lines(LOG, count=2, before=3)["lines"] == LOG[3:5]
    assert tt.filter_log_lines(LOG, level="error")["lines"] == LOG[2:5]          # severity=critical in an INFO line is not a level
    assert tt.filter_log_lines(LOG, level="warning")["lines"] == [LOG[1]]
    assert tt.filter_log_lines(LOG, search="QWEN")["lines"] == [LOG[5]]
    assert tt.filter_log_lines(["srv  error: model not found", "load: ok", "2026-09-14 20:00:00 ERROR boom", "  File x.py"], level="error")["lines"] == [
        "srv  error: model not found", "2026-09-14 20:00:00 ERROR boom", "  File x.py"]
    since = tt.parse_when("2026-09-14T20:10:00")            # naive = local (New York)
    out = tt.filter_log_lines(LOG, since=since)
    assert out["lines"] == LOG[2:] and out["matched"] == 6        # the traceback lines inherit 20:10
    assert tt.filter_log_lines(LOG, since=since, level="error", count=1, before=1) == {"lines": [LOG[3]], "matched": 3, "older": 1, "newer": 1}
    assert tt.filter_log_lines([], search="x") == {"lines": [], "matched": 0, "older": 0, "newer": 0}
    assert tt._line_ts("[2026-09-14T20:00:00.123Z] x") == tt.parse_ts("2026-09-14T20:00:00.123Z") and tt._line_ts("plain") is None


def test_prod_log_tail_routes_every_source(monkeypatch, tmp_path):
    import agent_registry
    import discord_bot
    agents = {"A1": {"hostname": "box", "status": "approved", "capabilities": {"llama": True, "vllm": True}},
              "A2": {"hostname": "vm", "status": "approved", "capabilities": {"vllm": True}},
              "A3": {"hostname": "mac", "status": "approved", "capabilities": {"lms": True}}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": agents})
    monkeypatch.setattr(discord_bot, "prod_deps", lambda ctx: {"fleet": lambda: [], "host": lambda n: None,
                                                              "ack": lambda a: (True, None), "close": lambda a: (True, None)})
    asked = []
    def fake_json(agent, path, timeout=15):
        asked.append((agent["hostname"], path))
        if agent["hostname"] == "vm":
            return None
        return {"ok": True, "lines": [f"{agent['hostname']} {path} {i}" for i in range(10)]}
    monkeypatch.setattr(discord_bot, "_agent_json", fake_json)
    (tmp_path / "llm-systems-manager.log").write_text("m1\nm2 error\nm3\n")

    class _Resp:
        def __init__(self, code, body=None):
            self.status_code, self.ok, self._body = code, code == 200, body or {}
        def json(self): return self._body

    ae = {"code": 200}
    class _Session:
        def get(self, url, timeout=10):
            assert url.endswith("/api/alarm/admin/log/tail")
            return _Resp(ae["code"], {"ok": True, "lines": ["ae one", "ae two error"]})
    ctx = types.SimpleNamespace(alarm_engine_url=lambda: "http://ae.local", ae_session=_Session(),
                                settings=types.SimpleNamespace(paths=types.SimpleNamespace(log_dir=str(tmp_path))))
    deps = tt.prod_deps(ctx, db_path="unused", tools_runs=lambda *a, **k: [], speed_table=lambda *a: [],
                        service_health=lambda: {}, gateway_entries=lambda: [], audit_rows=lambda *a, **k: [])
    out = deps["log_tail"]("box", "llama", 5)
    assert out == {"host": "box", "source": "llama", "lines": [f"box /llama/log/tail {i}" for i in range(5, 10)], "matched": 10, "older": 5, "newer": 0}
    assert deps["log_tail"]("box", "agent", 5, {"search": " 7"})["lines"] == ["box /agent/log/tail 7"]
    assert asked[-1] == ("box", "/agent/log/tail")
    assert deps["log_tail"]("mac", "lms", 5)["note"] == "LM Studio serves no log through the agent"
    assert deps["log_tail"]("box", "lms", 5)["note"] == "box does not serve LM Studio"
    # `all` for a provider log covers only the hosts that serve it; the agent log covers every host
    assert deps["log_tail"]("all", "llama", 10)["host"] == "box"
    assert [h["host"] for h in deps["log_tail"]("all", "agent", 15)["hosts"]] == ["box", "mac", "vm"]
    both = deps["log_tail"]("all", "vllm", 10)
    assert [h["host"] for h in both["hosts"]] == ["box", "vm"] and len(both["hosts"][0]["lines"]) == 5
    assert both["hosts"][1] == {"host": "vm", "source": "vllm", "lines": [], "matched": 0, "older": 0, "newer": 0, "note": "could not reach the agent on vm"}
    assert deps["log_tail"]("nope", "llama", 5) == {"host": "nope", "source": "llama", "lines": [], "matched": 0, "older": 0, "newer": 0, "note": "unknown host"}
    assert deps["log_tail"]("", "llama", 5) == {"error": "host is required for llama logs (a name, a comma-separated list, or all)"}
    assert deps["log_tail"](None, "manager", 5)["source"] == "manager"
    agents["A2"]["capabilities"] = {}
    assert deps["log_tail"]("all", "vllm", 10)["host"] == "box"
    agents["A1"]["capabilities"] = {}
    assert deps["log_tail"]("all", "vllm", 10) == {"source": "vllm", "hosts": [], "note": "no host serves vLLM"}
    assert deps["log_tail"]("-", "manager", 40, {"level": "error"}) == {"source": "manager", "lines": ["m2 error"], "matched": 1, "older": 0, "newer": 0}
    assert deps["log_tail"]("-", "alarm_engine", 40)["lines"] == ["ae one", "ae two error"]
    ae["code"] = 401
    assert deps["log_tail"]("-", "alarm_engine", 40)["note"] == "the alarm engine refused the management token"
    ae["code"] = 404
    assert deps["log_tail"]("-", "alarm_engine", 40)["note"] == "the alarm engine is too old to serve its log"
    assert deps["log_tail"]("box", "llama", 5, {"since": "soon"}) == {"error": "since is not a time (use 30m, 2h, 3d, a date or ISO-8601)"}
    reg = tt.build_registry(deps)
    args, err = tt.validate_args(reg["log_tail"], {"provider": "manager", "lines": 200, "level": "error", "before": 3})
    assert err is None and args == {"provider": "manager", "lines": 200, "level": "error", "before": 3}
    assert tt.validate_args(reg["log_tail"], {"host": "box", "lines": 500})[1] == "lines must be at most 200"
