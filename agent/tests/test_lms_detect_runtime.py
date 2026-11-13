"""#1422: LM Studio run-time detection, auto-enable and the lms.settings block."""
from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _AGENT_ROOT / "agent_config.yaml.example"
_BLOCK_KEYS = {"settings_source", "api_url", "lms_cmd", "cli_found", "running", "process_user", "agent_user"}


def _stub_if_absent(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return sys.modules[name]


class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _load_lms():
    _stub_if_absent("requests", Session=lambda: None)
    _stub_if_absent("fastapi", Header=lambda *a, **k: None, HTTPException=_HTTPException,
                    Query=lambda *a, **k: None, Request=object)
    pkg = _stub_if_absent("lms_rt_pkg")
    pkg.__path__ = [str(_AGENT_ROOT / "providers")]
    shared = _stub_if_absent("lms_rt_pkg._shared", openai_forward=None)
    shared.err_kind = lambda e: "error"
    pkg._shared = shared
    spec = importlib.util.spec_from_file_location("lms_rt_pkg.lms", _AGENT_ROOT / "providers" / "lms.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lms = _load_lms()


def det(**kw):
    d = dict(found=True, running=True, pid=4242, process_user="ana", port=1240,
             api_url="http://127.0.0.1:1240", lms_cmd="/home/ana/.lmstudio/bin/lms", lms_user="ana", candidates=[4242])
    d.update(kw)
    return SimpleNamespace(**d)


class FakeDetector:
    """Returns one fixed Detection and records each call's arguments."""

    def __init__(self, result):
        self.result = result
        self.calls: list = []

    def detect(self, port_hint=None, cmd_hint="", agent_user="", **kw):
        self.calls.append({"port_hint": port_hint, "cmd_hint": cmd_hint, "agent_user": agent_user})
        return self.result


@pytest.fixture
def env(tmp_path, monkeypatch):
    f = tmp_path / "agent_config.yaml"
    shutil.copy(_EXAMPLE, f)
    cfg = SimpleNamespace(LMS_ENABLED=True, LMS_API_URL="http://localhost:1234", LMS_CMD=lms._default_lms_cmd(),
                          AGENT_USER="llmsys", AGENT_ROLE="lms_host", LLAMA_ENABLED=False,
                          _auto_role=False, _loaded_from=str(f))
    lms.set_context(SimpleNamespace(config=cfg, check_bearer=lambda *_: None))
    monkeypatch.setattr(lms, "_detected", {})
    monkeypatch.setattr(lms, "_last_det", {"running": False, "process_user": "", "lms_user": ""})
    monkeypatch.setattr(lms, "_saved", False)
    monkeypatch.setattr(lms, "_redetect_last", None)
    monkeypatch.setattr(lms, "_auto_enabled", False)
    monkeypatch.setattr(lms, "_api_ok", None)
    monkeypatch.setattr(lms, "_server_on", None)
    fake = FakeDetector(det())
    monkeypatch.setattr(lms, "_detect_module", lambda: fake)
    return SimpleNamespace(cfg=cfg, file=f, fake=fake)


def test_startup_detect_applies_in_memory_and_marks_detected(env):
    before = env.file.read_text()
    lms.lms_startup_detect()
    assert env.cfg.LMS_API_URL == "http://127.0.0.1:1240"
    assert env.cfg.LMS_CMD == "/home/ana/.lmstudio/bin/lms"
    assert lms.lms_settings_block()["settings_source"] == "detected"
    assert env.file.read_text() == before


def test_startup_detect_never_overrides_saved(env):
    env.cfg.LMS_API_URL = "http://lms-box:1300"
    env.cfg.LMS_CMD = "/opt/lms/bin/lms"
    lms.lms_startup_detect()
    assert env.cfg.LMS_API_URL == "http://lms-box:1300"
    assert env.cfg.LMS_CMD == "/opt/lms/bin/lms"
    assert lms._detected == {}
    assert lms.lms_settings_block()["settings_source"] == "saved"


def test_owner_read_on_saved_host_without_changing_config(env, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(lms.time, "monotonic", lambda: clock[0])
    env.cfg.LMS_API_URL = "http://lms-box:1300"
    env.cfg.LMS_CMD = "/opt/lms/bin/lms"
    lms.lms_startup_detect()
    assert env.fake.calls == [{"port_hint": 1300, "cmd_hint": "/opt/lms/bin/lms", "agent_user": "llmsys"}]
    b = lms.lms_settings_block()
    assert (b["process_user"], b["agent_user"]) == ("ana", "llmsys")
    assert b["settings_source"] == "saved" and lms._detected == {}
    assert lms._last_det["lms_user"] == "ana"
    monkeypatch.setattr(lms, "_api_ok", True)
    env.fake.result = det(process_user="bob")
    clock[0] = 1301.0
    lms.lms_redetect_tick()
    assert len(env.fake.calls) == 2
    assert lms.lms_settings_block()["process_user"] == "bob"
    assert (env.cfg.LMS_API_URL, env.cfg.LMS_CMD) == ("http://lms-box:1300", "/opt/lms/bin/lms")
    assert lms.lms_settings_block()["settings_source"] == "saved"


def test_default_moved_still_finds_1235(env):
    env.fake.result = det(port=1235, api_url="http://127.0.0.1:1235")
    lms.lms_startup_detect()
    assert env.fake.calls[0]["port_hint"] == 1234
    assert env.cfg.LMS_API_URL == "http://127.0.0.1:1235"


def test_redetect_runs_only_every_300s(env, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(lms.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(lms, "_api_ok", False)
    lms.lms_startup_detect()
    assert len(env.fake.calls) == 1
    clock[0] = 1100.0
    lms.lms_redetect_tick()
    assert len(env.fake.calls) == 1
    clock[0] = 1301.0
    lms.lms_redetect_tick()
    assert len(env.fake.calls) == 2
    monkeypatch.setattr(lms, "_api_ok", True)
    clock[0] = 1700.0
    env.fake.result = det(port=1250, api_url="http://127.0.0.1:1250")
    lms.lms_redetect_tick()
    assert len(env.fake.calls) == 3
    assert env.cfg.LMS_API_URL == "http://127.0.0.1:1240"
    clock[0] = 1800.0
    lms.lms_redetect_tick()
    assert len(env.fake.calls) == 3


def test_auto_enable_turns_on_lms_and_role(env):
    env.cfg.LMS_ENABLED, env.cfg._auto_role, env.cfg.AGENT_ROLE = False, True, "system_only"
    assert lms.lms_auto_enable_tick() is True
    assert env.cfg.LMS_ENABLED is True
    assert env.cfg.AGENT_ROLE == "lms_host"
    assert env.cfg.LMS_API_URL == "http://127.0.0.1:1240"
    assert lms.lms_settings_block()["settings_source"] == "detected"


def test_auto_enabled_at_defaults_offers_save(env):
    env.cfg.LMS_ENABLED, env.cfg._auto_role, env.cfg.AGENT_ROLE = False, True, "system_only"
    env.fake.result = det(port=1234, api_url="http://127.0.0.1:1234", lms_cmd=env.cfg.LMS_CMD)
    assert lms.lms_auto_enable_tick() is True
    assert lms._detected == {}
    assert lms.lms_settings_block()["settings_source"] == "detected"


def test_auto_enable_with_llama_sets_mixed(env):
    env.cfg.LMS_ENABLED, env.cfg._auto_role = False, True
    env.cfg.LLAMA_ENABLED, env.cfg.AGENT_ROLE = True, "llama_host"
    assert lms.lms_auto_enable_tick() is True
    assert env.cfg.AGENT_ROLE == "mixed"


def test_auto_enable_with_vllm_sets_mixed(env):
    env.cfg.LMS_ENABLED, env.cfg._auto_role, env.cfg.AGENT_ROLE = False, True, "vllm_host"
    assert lms.lms_auto_enable_tick() is True
    assert env.cfg.AGENT_ROLE == "mixed"


def test_default_lms_cmd_fixed_at_context(env, monkeypatch):
    monkeypatch.setattr(lms, "_default_cmd", lms._default_cmd)
    before = lms._default_lms_cmd()
    assert env.cfg.LMS_CMD == before
    monkeypatch.setattr(lms.shutil, "which", lambda name, *a, **kw: "/new/place/bin/lms")
    assert lms._default_lms_cmd() == before
    assert lms.lms_settings_block()["settings_source"] == "default"
    lms.set_context(SimpleNamespace(config=env.cfg, check_bearer=lambda *_: None))
    assert lms._default_lms_cmd() == "/new/place/bin/lms"


def test_auto_enable_ignored_when_role_not_auto(env):
    env.cfg.LMS_ENABLED, env.cfg._auto_role, env.cfg.AGENT_ROLE = False, False, "system_only"
    assert lms.lms_auto_enable_tick() is False
    assert env.cfg.LMS_ENABLED is False
    assert env.cfg.AGENT_ROLE == "system_only"
    assert env.fake.calls == []


def test_auto_enable_skips_when_nothing_found(env):
    env.cfg.LMS_ENABLED, env.cfg._auto_role, env.cfg.AGENT_ROLE = False, True, "system_only"
    env.fake.result = det(found=False, running=False, pid=None, process_user="", port=None, api_url="", lms_cmd="")
    assert lms.lms_auto_enable_tick() is False
    assert env.cfg.LMS_ENABLED is False


def test_settings_block_other_user(env, tmp_path, monkeypatch):
    monkeypatch.setattr(lms, "_server_on", True)
    cmd = tmp_path / "lms"
    cmd.write_text("")
    env.fake.result = det(lms_cmd=str(cmd))
    lms.lms_startup_detect()
    b = lms.lms_settings_block()
    assert set(b) == _BLOCK_KEYS
    assert b["process_user"] == "ana" and b["agent_user"] == "llmsys"
    assert b["running"] is True and b["cli_found"] is True
    assert b["api_url"] == "http://127.0.0.1:1240" and b["lms_cmd"] == str(cmd)


def test_running_false_when_daemon_up_but_server_off(env, monkeypatch):
    status = {"on": False, "port": None, "raw": ""}
    monkeypatch.setattr(lms, "lms_get_status", lambda: dict(status))
    monkeypatch.setattr(lms, "lms_get_models", lambda: [])
    monkeypatch.setattr(lms, "_lms_read_ps", lambda: ([], None))
    monkeypatch.setattr(lms, "_residency_inputs", {"models": [], "server": "unknown"})
    monkeypatch.setattr(lms, "_api_ok", False)
    lms.lms_startup_detect()
    assert lms._last_det["running"] is True
    lms.lms_sample_block()
    b = lms.lms_settings_block()
    assert b["running"] is False and b["process_user"] == "ana"
    status["on"] = True
    lms.lms_sample_block()
    assert lms.lms_settings_block()["running"] is True


def test_settings_block_default_when_nothing_detected(env):
    env.fake.result = det(found=False, running=False, pid=None, process_user="", port=None, api_url="", lms_cmd="")
    lms.lms_startup_detect()
    b = lms.lms_settings_block()
    assert b["settings_source"] == "default" and b["running"] is False


def test_detector_error_is_contained(env, monkeypatch):
    def boom():
        raise ImportError("no detector")
    monkeypatch.setattr(lms, "_detect_module", boom)
    lms.lms_startup_detect()
    assert env.cfg.LMS_API_URL == "http://localhost:1234"


def test_capabilities_reflect_runtime_enable(env):
    src = (_AGENT_ROOT / "llm-systems-agent.py").read_text()
    body = re.search(r"^def _capabilities\(.*?(?=^\S)", src, re.MULTILINE | re.DOTALL).group(0)
    env.cfg.LMS_ENABLED, env.cfg._auto_role, env.cfg.AGENT_ROLE = False, True, "system_only"
    for k in ("VLLM_ENABLED", "OPENCLAW_ENABLED", "IMGGEN_ENABLED", "PERF_CONTROLLER_ENABLED",
              "MONITOR_MANAGER_ENABLED", "MONITOR_ALARM_ENGINE_ENABLED"):
        setattr(env.cfg, k, False)
    ns = {"CONFIG": env.cfg}
    exec(compile("from __future__ import annotations\n" + body, "caps", "exec"), ns)
    assert ns["_capabilities"]()["lms"] is False
    lms.lms_auto_enable_tick()
    assert ns["_capabilities"]()["lms"] is True


def test_settings_save_route_registered():
    assert ("POST", "/lms/settings/save", lms.lms_settings_save) in lms._ROUTES
