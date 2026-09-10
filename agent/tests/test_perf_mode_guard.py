"""Perf-mode guard (#888): config-honouring switch around jobs that need llama-server down."""
import sys
import types

import pytest

from tests.test_autotune_v2_routes import _load_llama


@pytest.fixture(scope="module")
def llama():
    # Reuse the already-stubbed module when a sibling suite loaded it first.
    return sys.modules.get("providers.llama") or _load_llama()


class _Ctx:
    def __init__(self, enabled=True, awake="turbo", sleep="eco"):
        self.config = types.SimpleNamespace(
            PERF_CONTROLLER_ENABLED=enabled,
            PERF_TARGET_AWAKE=awake, PERF_TARGET_SLEEP=sleep,
            LLAMA_BIN="", LLAMA_ENABLED=True, AGENT_INSTALL_DIR="/tmp")
        self.state = {"token": "", "agent_id": ""}

    def check_bearer(self, *a, **k):
        return None


def _wire(llama, monkeypatch, ctx, runs):
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama.subprocess, "run",
                        lambda argv, **kw: runs.append(argv) or types.SimpleNamespace(
                            returncode=0, stdout="", stderr=""))


def test_disabled_controller_is_a_no_op_with_a_reason(llama, monkeypatch):
    runs, events = [], []
    _wire(llama, monkeypatch, _Ctx(enabled=False), runs)
    llama._perf_mode_set("awake", events.append)
    assert runs == []
    ev = events[0]
    assert ev["type"] == "perf_mode" and ev["enabled"] is False and ev["skipped"] is True
    assert ev["ok"] is False and "PERF_CONTROLLER_ENABLED" in ev["error"]


def test_uses_the_configured_unit_names(llama, monkeypatch):
    runs, events = [], []
    _wire(llama, monkeypatch, _Ctx(awake="turbo", sleep="eco"), runs)
    llama._perf_mode_set("awake", events.append)
    llama._perf_mode_set("sleep", events.append)
    assert runs == [["sudo", "-n", "systemctl", "reload-or-restart", "turbo"],
                    ["sudo", "-n", "systemctl", "reload-or-restart", "eco"]]
    assert [e["mode"] for e in events] == ["turbo", "eco"]
    assert all(e["ok"] and e["enabled"] and not e["skipped"] for e in events)


def test_governor_is_read_not_assumed(llama, monkeypatch):
    """A failed switch must not make the event claim the target mode took effect."""
    runs, events = [], []
    ctx = _Ctx()
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama.subprocess, "run",
                        lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="denied"))
    llama._perf_mode_set("awake", events.append)
    assert events[0]["ok"] is False and events[0]["rc"] == 1
    assert events[0]["error"] == "denied"
    assert events[0]["governor"] == "powersave"
    assert runs == []


def test_offline_benchmark_sets_and_resets(llama, monkeypatch):
    runs, events = [], []
    _wire(llama, monkeypatch, _Ctx(), runs)
    monkeypatch.setattr(llama, "_bench_put", events.append)
    monkeypatch.setattr(llama, "_bench_run_one", lambda *a, **k: None)
    llama._bench_run_all(["org/m:Q4"], "llama-bench", [])
    assert [r[-1] for r in runs] == ["turbo", "eco"]
    assert [e["phase"] for e in events if e["type"] == "perf_mode"] == ["awake", "sleep"]


def test_offline_benchmark_resets_when_the_job_raises(llama, monkeypatch):
    runs, events = [], []
    _wire(llama, monkeypatch, _Ctx(), runs)
    monkeypatch.setattr(llama, "_bench_put", events.append)

    def _boom(*a, **k):
        raise RuntimeError("bench blew up")
    monkeypatch.setattr(llama, "_bench_run_one", _boom)
    llama._bench_run_all(["org/m:Q4"], "llama-bench", [])
    assert [r[-1] for r in runs] == ["turbo", "eco"]
    assert any(e["type"] == "done" and not e["ok"] for e in events)


def test_autotune_sets_and_resets_when_the_run_raises(llama, monkeypatch):
    runs, events = [], []
    _wire(llama, monkeypatch, _Ctx(), runs)
    monkeypatch.setattr(llama, "_autotune_put", events.append)
    monkeypatch.setattr(llama, "_bench_live_runtime", lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    llama._autotune_run_all({"model_ids": ["org/m:Q4"], "mode": "tune"})
    assert [r[-1] for r in runs] == ["turbo", "eco"]
    assert [e["phase"] for e in events if e["type"] == "perf_mode"] == ["awake", "sleep"]


def test_quality_guard_sets_and_resets(llama, monkeypatch, tmp_path):
    """The quality guard shares the autotune runner, so it inherits the same guard."""
    runs, events = [], []
    ctx = _Ctx()
    ctx.config.AGENT_INSTALL_DIR = str(tmp_path)
    ctx.config.LLAMA_CONFIG_INI = str(tmp_path / "config.ini")
    _wire(llama, monkeypatch, ctx, runs)
    monkeypatch.setattr(llama, "_autotune_put", events.append)
    monkeypatch.setattr(llama, "_bench_live_runtime", lambda: {"python": "", "script": ""})
    monkeypatch.setattr(llama, "_llama_catalog_sweep", lambda: ({"org/m:Q4": 1}, {}))
    monkeypatch.setattr(llama, "_list_cache_ggufs", lambda root: [])
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: tmp_path)
    monkeypatch.setattr(llama, "_llama_help_valued", lambda: {"ctx-size"})
    monkeypatch.setattr(llama, "_autotune_perplexity_bin", lambda: None)
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: __import__("configparser").ConfigParser())
    monkeypatch.setattr(llama, "_bench_get_hf_arg", lambda mid: "org/m:Q4")
    monkeypatch.setattr(llama._at, "run_quality", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(llama._at, "ledger_summary", lambda done: {})
    monkeypatch.setattr(llama._shared, "post_tool_run", lambda *a, **k: None)
    llama._autotune_run_all({"model_ids": ["org/m:Q4"], "mode": "quality"})
    assert [r[-1] for r in runs] == ["turbo", "eco"]
    assert [e["phase"] for e in events if e["type"] == "perf_mode"] == ["awake", "sleep"]
    assert any(e["type"] == "done" and e["ok"] for e in events)


# ── the manual Performance/Powersave button (#887) ───────────────────

def test_manual_switch_uses_the_configured_units(llama, monkeypatch):
    runs = []
    _wire(llama, monkeypatch, _Ctx(awake="turbo", sleep="eco"), runs)
    assert llama.llama_bench_perf_mode({"mode": "performance"})["unit"] == "turbo"
    assert llama.llama_bench_perf_mode({"mode": "powersave"})["unit"] == "eco"
    assert runs == [["sudo", "-n", "systemctl", "reload-or-restart", "turbo"],
                    ["sudo", "-n", "systemctl", "reload-or-restart", "eco"]]


def test_manual_switch_is_refused_when_the_controller_is_off(llama, monkeypatch):
    runs = []
    _wire(llama, monkeypatch, _Ctx(enabled=False), runs)
    out = llama.llama_bench_perf_mode({"mode": "performance"})
    assert out["ok"] is False and "PERF_CONTROLLER_ENABLED" in out["error"]
    assert runs == []


def test_manual_switch_still_validates_the_mode(llama, monkeypatch):
    runs = []
    _wire(llama, monkeypatch, _Ctx(), runs)
    assert llama.llama_bench_perf_mode({"mode": "turbo"})["ok"] is False
    assert runs == []


# ── startup reset after a killed run (#887) ──────────────────────────

def _controller_start(llama, monkeypatch, ctx, unit_active, switched, written=None):
    """Runs the controller with restart_pending set, so it exits after startup."""
    import asyncio
    ctx.state["restart_pending"] = True
    ctx.config.AGENT_OS = "linux"
    ctx.config.LLAMA_LOG_FILE = "/nonexistent/llama.log"
    ctx.config.LLAMA_STATE_FILE = "/nonexistent/llama.state"
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_unit_active", lambda: unit_active)
    monkeypatch.setattr(llama, "llama_write_state_file",
                        lambda state: written.append(state) if written is not None else None)

    async def _switch(unit):
        switched.append(unit)
    monkeypatch.setattr(llama, "_perf_switch", _switch)
    asyncio.run(llama.perf_controller_loop())


def test_startup_resets_to_the_sleep_unit_when_llama_server_is_down(llama, monkeypatch):
    switched = []
    _controller_start(llama, monkeypatch, _Ctx(), False, switched)
    assert switched == ["eco"]


def test_startup_reset_also_records_sleeping_in_the_state_file(llama, monkeypatch):
    switched, written = [], []
    _controller_start(llama, monkeypatch, _Ctx(), False, switched, written)
    assert written[0] == "sleeping"


def test_startup_leaves_a_running_server_in_its_current_mode(llama, monkeypatch):
    switched = []
    _controller_start(llama, monkeypatch, _Ctx(), True, switched)
    assert switched == []
