"""#471: run_job execution + embed formatting against fake deps."""
from __future__ import annotations

import discord_bot as db

HOSTS = [
    {"hostname": "box", "providers": ["llama"], "online": True, "age_s": 4.0,
     "model": "qwen3", "busy": True, "watts": 402.0},
    {"hostname": "mac", "providers": ["lms"], "online": False, "age_s": 700.0,
     "model": None, "busy": False, "watts": None},
]

ALERTS = [
    {"alert_id": "a1", "rule_name": "GPU hot", "severity": "critical",
     "status": "active", "hostname": "box"},
    {"alert_id": "a2", "rule_name": "RAM high", "severity": "warning",
     "status": "acknowledged", "hostname": "mac"},
]


def _deps(**over):
    deps = {
        "fleet": lambda: list(HOSTS),
        "host": lambda name: HOSTS[0] if name == "box" else None,
        "models": lambda host: [
            {"model": "qwen3", "provider": "llama", "hostname": "box",
             "loaded": True},
            {"model": "phi4", "provider": "llama", "hostname": "box",
             "loaded": False}],
        "alarms": lambda n: ALERTS[:n],
        "ack": lambda aid: (aid == "a1", None if aid == "a1"
                            else "alert not found"),
        "close": lambda aid: (True, None),
        "load": lambda p, h, m: (True, None),
        "unload": lambda p, h, m: (False, "llama refused"),
    }
    deps.update(over)
    return deps


def test_fleet_embed_lists_hosts_sorted_with_status():
    out = db.run_job({"kind": "fleet"}, _deps())
    desc = out["embeds"][0]["description"]
    lines = desc.splitlines()
    assert "box" in lines[0] and "🟢" in lines[0]
    assert "402 W" in lines[0] and "⚡" in lines[0]
    assert "mac" in lines[1] and "🔴" in lines[1]
    assert "last seen 12m ago" in lines[1]


def test_fleet_empty_state():
    out = db.run_job({"kind": "fleet"}, _deps(fleet=lambda: []))
    assert "No approved agents" in out["embeds"][0]["description"]


def test_host_embed_and_unknown_host():
    detail = {"hostname": "box", "online": True, "cpu_pct": 22.4,
              "ram_pct": 61.0, "watts": 390.0,
              "provider_states": {"llama.cpp": "awake"}}
    out = db.run_job({"kind": "host", "name": "box"},
                     _deps(host=lambda n: detail))
    embed = out["embeds"][0]
    names = [f["name"] for f in embed["fields"]]
    assert names == ["CPU", "RAM", "Power", "llama.cpp"]
    assert embed["description"] == "online"
    out = db.run_job({"kind": "host", "name": "ghost"}, _deps())
    assert "Unknown host" in out["embeds"][0]["description"]


def test_models_marks_loaded_and_empty_case():
    out = db.run_job({"kind": "models", "host": None}, _deps())
    lines = out["content"].splitlines()
    assert lines[0].startswith("▶ `qwen3`")
    assert lines[1].startswith("`phi4`")
    out = db.run_job({"kind": "models", "host": "x"},
                     _deps(models=lambda h: []))
    assert "No models" in out["content"]


def test_models_list_capped():
    many = [{"model": f"m{i}", "provider": "llama", "hostname": "box",
             "loaded": False} for i in range(15)]
    out = db.run_job({"kind": "models", "host": None},
                     _deps(models=lambda h: many))
    lines = out["content"].splitlines()
    assert len(lines) == db.LIST_CAP + 1
    assert lines[-1] == "… +5 more"


def test_alarms_embed_and_footer():
    out = db.run_job({"kind": "alarms", "count": 5}, _deps())
    embed = out["embeds"][0]
    assert "GPU hot" in embed["description"]
    assert "`a1`" in embed["description"]
    assert embed["color"] == db.SEVERITY_COLOR["critical"]
    assert "/ack" in embed["footer"]["text"]


def test_alarms_empty():
    out = db.run_job({"kind": "alarms", "count": 5},
                     _deps(alarms=lambda n: []))
    assert "No recent alarms" in out["embeds"][0]["description"]


def test_ack_and_silence_results():
    assert "Acknowledged" in db.run_job(
        {"kind": "ack", "alert_id": "a1"}, _deps())["content"]
    assert "not found" in db.run_job(
        {"kind": "ack", "alert_id": "zz"}, _deps())["content"]
    out = db.run_job({"kind": "silence", "alert_id": "a1"}, _deps())
    assert "re-fires" in out["content"]


def test_execute_unwraps_and_reports_control_results():
    ok = db.run_job({"kind": "execute",
                     "job": {"kind": "load", "provider": "llama",
                             "host": None, "model": "m"}}, _deps())
    assert ok["content"] == "Loaded `m`."
    bad = db.run_job({"kind": "execute",
                      "job": {"kind": "unload", "provider": "llama",
                              "host": None, "model": "m"}}, _deps())
    assert "llama refused" in bad["content"]


def test_job_exception_becomes_generic_failure_text():
    def boom():
        raise RuntimeError("store exploded at /internal/path")
    out = db.run_job({"kind": "fleet"}, _deps(fleet=boom))
    # Details go to the log, never into the Discord-visible reply.
    assert out["content"] == "Failed — check the manager log for details."
    assert "exploded" not in out["content"]


def test_unknown_job_kind():
    assert "Unknown job" in db.run_job({"kind": "wat"}, _deps())["content"]


def test_age_formatting():
    assert db._fmt_age(None) == "never"
    assert db._fmt_age(45) == "45s"
    assert db._fmt_age(720) == "12m"
    assert db._fmt_age(7200) == "2.0h"
