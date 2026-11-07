"""#1419: switch a single-model llama-server to multi-model mode."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_AGENT_ROOT), str(_AGENT_ROOT / "install")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _module_available(name):
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules.setdefault(name, mod)


if not _module_available("requests"):
    _stub("requests", get=None, Session=object)
if not _module_available("fastapi"):
    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            super().__init__(detail)
            self.status_code = status_code
    _stub("fastapi", Header=lambda *a, **k: None, HTTPException=_HTTPException,
          Query=lambda *a, **k: None, Request=object)
    _stub("fastapi.responses", Response=object, StreamingResponse=object)
    _stub("starlette")
    _stub("starlette.concurrency", run_in_threadpool=lambda f, *a, **k: f(*a, **k))
if not _module_available("psutil"):
    _c = types.ModuleType("collectors")
    _c.__path__ = []
    sys.modules.setdefault("collectors", _c)
    _stub("collectors.gpu", collect_gpu=lambda: {})

_pkg = types.ModuleType("providers")
_pkg.__path__ = [str(_AGENT_ROOT / "providers")]
sys.modules.setdefault("providers", _pkg)
_spec = importlib.util.spec_from_file_location("providers.llama", _AGENT_ROOT / "providers" / "llama.py")
llama = importlib.util.module_from_spec(_spec)
sys.modules["providers.llama"] = llama
_spec.loader.exec_module(llama)
_REAL_MODE_BLOCK = llama.llama_mode_block


import json


def _body(resp):
    return json.loads(resp.body)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(llama, "_mode_probe_memo", {"ts": 0.0, "url": None, "ok": False})
    monkeypatch.setattr(llama, "_cached_models_memo", {"ts": 0.0, "key": None, "rows": []})
    c = SimpleNamespace(LLAMA_ENABLED=True, LLAMA_SYSTEMD_UNIT="llama-server.service",
                        LLAMA_API_URL="http://localhost:9931", LLAMA_CONFIG_INI=str(tmp_path / "p" / "models.ini"),
                        LLAMA_CACHE_DIR="", LLAMA_MODELS_DIR="", AGENT_USER="")
    (tmp_path / "p").mkdir()
    llama.set_context(SimpleNamespace(config=c, check_bearer=lambda *_: None))
    return c


def _block(**kw):
    b = {"mode": "single", "configured_by": "flags", "presets_file": "", "presets_writable": False,
         "models_dir": "", "unit": "llama-server.service", "unit_path": "/u/llama-server.service",
         "unit_user": "llama", "cache_dir": "", "managed": True, "settings_source": "default"}
    b.update(kw)
    return b


@pytest.fixture
def sw(cfg, monkeypatch):
    st = SimpleNamespace(applies=[], ops=[], apply_res={"ok": True}, polls=[{"data": []}, {"data": [{"id": "a"}]}],
                         block=_block(), gets=0)
    info = {"unit": "llama-server.service", "unit_path": "/u/llama-server.service", "source": "drop-in",
            "exec_start": "/usr/bin/llama-server -m /m/a.gguf --port 9931 --host 127.0.0.1"}
    monkeypatch.setattr(llama, "llama_mode_block", lambda: st.block)
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: info)
    monkeypatch.setattr(llama, "_unit_info_invalidate", lambda: None)
    monkeypatch.setattr(llama, "cached_models", lambda mode=None: [])
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "_detect_matched", set())
    monkeypatch.setattr(llama.time, "sleep", lambda s: None)
    monkeypatch.setattr(llama._shared, "wrapper_version", lambda w: 3)

    def apply(wrapper, path, tokens, hint, **kw):
        st.applies.append((path, tokens, kw))
        return st.apply_res
    monkeypatch.setattr(llama._shared, "svcconfig_apply", apply)
    monkeypatch.setattr(llama._shared, "svcconfig_op",
                        lambda w, op, unit, timeout=30: st.ops.append((op, unit)) or {"ok": True})

    def get(url, timeout=0, **kw):
        i = min(st.gets, len(st.polls) - 1)
        st.gets += 1
        return SimpleNamespace(ok=True, status_code=200, json=lambda: st.polls[i])
    monkeypatch.setattr(llama.requests, "get", get, raising=False)
    return st


def test_build_router_exec_drops_m_and_adds_preset(cfg):
    binary, args, sec = llama.build_router_exec(
        "/usr/bin/llama-server -m /m/a.gguf --port 9931 --host 127.0.0.1", "/p/models.ini")
    flags = [a["flag"] for a in args]
    assert binary == "/usr/bin/llama-server"
    assert "-m" not in flags and "--models-preset" in flags and "--metrics" in flags
    assert {"flag": "--models-preset", "value": "/p/models.ini", "bool": False} in args
    assert {"flag": "--port", "value": "9931", "bool": False} in args
    assert sec == {"name": "a", "values": {"model": "/m/a.gguf"}}


def test_build_router_exec_drops_equals_form_and_keeps_metrics(cfg):
    _b, args, _s = llama.build_router_exec(
        "/b/llama-server --model=/m/a.gguf --hf-file x.gguf --metrics -ngl 99", "/p.ini")
    flags = [a["flag"] for a in args]
    assert "--model" not in flags and "--hf-file" not in flags
    assert flags.count("--metrics") == 1 and "-ngl" in flags


def test_build_router_exec_hf_in_cache_uses_path(cfg, monkeypatch):
    monkeypatch.setattr(llama, "cached_models", lambda mode=None: [
        {"model_id": "org/m-GGUF:Q4_K_M", "path": "/c/m.gguf", "repo": "org/m-GGUF", "file": "m.gguf"}])
    _b, args, sec = llama.build_router_exec("/b/llama-server -hf org/m-GGUF:q4_k_m", "/p.ini")
    assert sec == {"name": "org/m-GGUF:Q4_K_M", "values": {"model": "/c/m.gguf"}}
    assert "-hf" not in [a["flag"] for a in args]


def test_build_router_exec_hf_not_cached_keeps_bare_section(cfg):
    _b, _a, sec = llama.build_router_exec("/b/llama-server -hf org/m-GGUF:q4_k_m", "/p.ini")
    assert sec == {"name": "org/m-GGUF:Q4_K_M", "values": {}}


def test_build_router_exec_mu(cfg):
    _b, _a, sec = llama.build_router_exec("/b/llama-server -mu https://x/y.gguf", "/p.ini")
    assert sec == {"name": "y", "values": {"model-url": "https://x/y.gguf"}}


def test_build_router_exec_no_model_flag(cfg):
    assert llama.build_router_exec("/b/llama-server --port 1", "/p.ini")[2] is None


def test_mode_switch_refuses_environment_configured(sw):
    sw.block = _block(configured_by="environment")
    r = llama.llama_mode_post({"to": "router"})
    assert r.status_code == 409 and _body(r)["reason"] == "environment" and _body(r)["detail"]
    assert not sw.applies


def test_mode_switch_refuses_unmanaged(sw):
    sw.block = _block(managed=False)
    r = llama.llama_mode_post({"to": "router"})
    assert r.status_code == 409 and _body(r)["reason"] == "unmanaged"


def test_mode_switch_refuses_already_router(sw):
    sw.block = _block(mode="router", presets_file="/p/x.ini")
    r = llama.llama_mode_post({"to": "router"})
    assert r.status_code == 409 and _body(r)["reason"] == "already-router"


def test_mode_switch_refuses_unwritable_presets(sw, cfg, tmp_path):
    d = tmp_path / "p"
    d.chmod(0o555)
    try:
        if os.access(d, os.W_OK):
            pytest.skip("running as a user that ignores directory modes")
        r = llama.llama_mode_post({"to": "router"})
    finally:
        d.chmod(0o755)
    assert r.status_code == 409 and _body(r)["reason"] == "presets-not-writable"
    assert not sw.applies


def test_mode_switch_writes_section_applies_and_polls(sw, cfg, tmp_path):
    ini = Path(cfg.LLAMA_CONFIG_INI)
    ini.write_text("[keep]\nmodel = /m/keep.gguf\n")
    r = llama.llama_mode_post({"to": "router"})
    assert r["ok"] is True and r["presets_file"] == str(ini)
    assert r["unit"] == "llama-server.service" and r["source"] == "drop-in"
    text = ini.read_text()
    assert "[a]" in text and "model = /m/a.gguf" in text and "[keep]" in text
    path, tokens, kw = sw.applies[0]
    assert path == "/u/llama-server.service" and "--models-preset" in tokens and "-m" not in tokens
    assert kw["unit"] == "llama-server.service" and kw["restart_unit"] == "llama-server.service"
    assert kw["restart_timeout"] == 60
    assert sw.gets == 2 and not sw.ops


def test_mode_switch_restores_on_restart_failure(sw):
    sw.apply_res = {"ok": False, "error": "boom"}
    r = llama.llama_mode_post({"to": "router"})
    assert r.status_code == 500 and _body(r)["restored"] is True and _body(r)["error"]
    assert sw.ops == [("restore", "llama-server.service"), ("restart", "llama-server.service")]


def test_mode_switch_restores_on_poll_timeout(sw):
    sw.polls = [{"data": []}]
    r = llama.llama_mode_post({"to": "router"})
    assert r.status_code == 500 and _body(r)["restored"] is True
    assert sw.ops[0][0] == "restore" and sw.gets <= 40


def test_mode_switch_records_blank_presets_path(sw, cfg):
    path = cfg.LLAMA_CONFIG_INI
    cfg.LLAMA_CONFIG_INI = ""
    cfg.AGENT_USER = "tester"
    home = Path(path).parent
    llama.pwd = SimpleNamespace(getpwnam=lambda u: SimpleNamespace(pw_dir=str(home)))
    try:
        r = llama.llama_mode_post({"to": "router"})
    finally:
        import pwd as _pwd
        llama.pwd = _pwd
    want = str(home / ".config/llama.cpp/models.ini")
    assert r["presets_file"] == want and cfg.LLAMA_CONFIG_INI == want
    assert "presets_file" not in llama._detected and "LLAMA_CONFIG_INI" in llama._detect_matched


def test_poll_and_models_use_detected_api_url(sw, cfg, monkeypatch):
    cfg.LLAMA_API_URL = "http://localhost:8080"
    llama._detected["api_url"] = "http://127.0.0.1:9931"
    urls = []
    monkeypatch.setattr(llama.requests, "get", lambda url, timeout=0, **kw: urls.append(url) or SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"data": [{"id": "a"}]}), raising=False)
    assert llama.llama_mode_post({"to": "router"})["ok"] is True
    llama.llama_models_endpoint()
    assert urls and all(u.startswith("http://127.0.0.1:9931/v1/models") for u in urls)


def test_rollback_reports_failed_restore(sw, monkeypatch):
    sw.apply_res = {"ok": False, "error": "boom"}
    ops = []
    monkeypatch.setattr(llama._shared, "svcconfig_op", lambda w, op, unit, timeout=30: ops.append(op) or (
        {"ok": False, "error": "no backup"} if op == "restore" else {"ok": True}))
    r = llama.llama_mode_post({"to": "router"})
    b = _body(r)
    assert b["restored"] is False and b["restore_error"] == "no backup" and ops == ["restore"]


def test_mode_switch_refuses_unreadable_presets(sw, cfg):
    Path(cfg.LLAMA_CONFIG_INI).write_text("no section header\n")
    r = llama.llama_mode_post({"to": "router"})
    assert r.status_code == 409 and _body(r)["reason"] == "presets-unreadable" and not sw.applies


def test_mode_switch_leaves_file_when_section_exists(sw, cfg):
    ini = Path(cfg.LLAMA_CONFIG_INI)
    ini.write_text("# note\n[a]\nmodel = /x.gguf\n")
    assert llama.llama_mode_post({"to": "router"})["ok"] is True
    assert ini.read_text() == "# note\n[a]\nmodel = /x.gguf\n"


def test_mode_switch_wrapper_outdated(sw, monkeypatch):
    monkeypatch.setattr(llama._shared, "wrapper_version", lambda w: 2)
    monkeypatch.setattr(llama, "_root_update_cmd", lambda: "sudo do-the-update")
    r = llama.llama_mode_post({"to": "router"})
    assert r.status_code == 409 and _body(r)["reason"] == "wrapper-outdated" and not sw.applies
    assert _body(r)["root_update_cmd"] == "sudo do-the-update" and "sudo do-the-update" in _body(r)["detail"]


def test_router_without_presets_adds_preset_file(sw, cfg):
    sw.block = _block(mode="router")
    sw.polls = [{"data": []}]
    ini = Path(cfg.LLAMA_CONFIG_INI)
    r = llama.llama_mode_post({"to": "router"})
    assert r["ok"] is True and ini.exists() and ini.read_text() == llama._PRESETS_HEADER
    _p, tokens, _kw = sw.applies[0]
    assert "--models-preset" in tokens and "-m" in tokens


def test_router_with_presets_still_refused(sw):
    sw.block = _block(mode="router", presets_file="/p/x.ini")
    assert _body(llama.llama_mode_post({"to": "router"}))["reason"] == "already-router"


def test_build_router_exec_drops_hf_and_mmproj_flags(cfg):
    _b, args, _s = llama.build_router_exec(
        "/b/llama-server -hff a.gguf -hfv b -hfd x/y --hf-file-v c --hf-repo-draft d --mmproj p --mmproj-url u -ngl 9",
        "/p.ini")
    assert [a["flag"] for a in args if a["flag"] != "--models-preset" and a["flag"] != "--metrics"] == ["-ngl"]


def test_mode_switch_persists_presets_key(sw, cfg, tmp_path):
    f = tmp_path / "agent_config.yaml"
    f.write_text('LLAMA_ENABLED: true\n')
    cfg._loaded_from = str(f)
    path = cfg.LLAMA_CONFIG_INI
    cfg.LLAMA_CONFIG_INI = ""
    cfg.AGENT_USER = "tester"
    home = Path(path).parent
    llama.pwd = SimpleNamespace(getpwnam=lambda u: SimpleNamespace(pw_dir=str(home)))
    try:
        assert llama.llama_mode_post({"to": "router"})["ok"] is True
    finally:
        import pwd as _pwd
        llama.pwd = _pwd
    assert f'LLAMA_CONFIG_INI: "{home}/.config/llama.cpp/models.ini"' in f.read_text()


def _flags_info(flags):
    return {"flags": flags, "environment": {}, "unit": "u.service", "unit_path": "/u", "unit_user": "",
            "load_state": "loaded"}


def test_effective_presets_path_prefers_parsed_and_warns_once(cfg, monkeypatch, caplog):
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: _flags_info(["--models-preset", "/real.ini"]))
    monkeypatch.setitem(llama._presets_warned, "mismatch", False)
    with caplog.at_level("WARNING"):
        assert llama._llama_effective_presets_path() == "/real.ini"
        assert llama._llama_effective_presets_path() == "/real.ini"
    assert sum("differs" in r.getMessage() for r in caplog.records) == 1


def test_config_post_writes_detected_presets_file(cfg, monkeypatch, tmp_path):
    cfg.LLAMA_CONFIG_INI = ""
    target = tmp_path / "p" / "det.ini"
    monkeypatch.setattr(llama, "_detected", {"presets_file": str(target)})
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: _flags_info([]))
    assert llama.llama_config_post({"m": {"model": "/x.gguf"}})["ok"] is True
    assert "[m]" in target.read_text()


def test_mode_block_unknown_mode_with_answering_server_is_router(cfg, monkeypatch):
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: _flags_info([]))
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "_llama_cache_root", lambda: None)
    monkeypatch.setattr(llama.requests, "get", lambda url, timeout=0, **kw: SimpleNamespace(ok=True), raising=False)
    b = llama.llama_mode_block()
    assert b["mode"] == "router" and b["unit_user"] == "root"


@pytest.mark.parametrize("tok", ["${LLAMA_PRESETS}", "presets.ini"])
def test_unexpanded_or_relative_presets_flag_is_ignored(cfg, monkeypatch, tok):
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: _flags_info(["--models-preset", tok]))
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "_llama_cache_root", lambda: None)
    assert llama._llama_effective_presets_path() == cfg.LLAMA_CONFIG_INI
    assert llama.llama_mode_block()["presets_file"] == cfg.LLAMA_CONFIG_INI
    cfg.LLAMA_CONFIG_INI = ""
    assert llama._llama_effective_presets_path() == ""
    assert llama.llama_mode_block()["presets_file"] == ""


def test_rollback_with_nothing_to_restore_skips_restart(sw, monkeypatch):
    sw.apply_res = {"ok": False, "error": "llm-svcconfig-apply: binary is not a regular file: /x"}
    ops = []
    monkeypatch.setattr(llama._shared, "svcconfig_op", lambda w, op, unit, timeout=30: ops.append(op) or (
        {"ok": False, "error": "llm-svcconfig-apply: nothing to restore for llama-server.service"}
        if op == "restore" else {"ok": True}))
    r = llama.llama_mode_post({"to": "router"})
    b = _body(r)
    assert r.status_code == 500 and ops == ["restore"]
    assert b["changed"] is False and b["restored"] is False and b["detail"] == "nothing was changed"


def test_rollback_removes_created_section(sw, cfg):
    ini = Path(cfg.LLAMA_CONFIG_INI)
    ini.write_text("[keep]\nmodel = /m/keep.gguf\n")
    sw.apply_res = {"ok": False, "error": "boom"}
    llama.llama_mode_post({"to": "router"})
    cp = llama._read_presets(str(ini))
    assert cp.sections() == ["keep"]


def test_rollback_keeps_preexisting_section(sw, cfg):
    ini = Path(cfg.LLAMA_CONFIG_INI)
    ini.write_text("[a]\nmodel = /x.gguf\n")
    sw.apply_res = {"ok": False, "error": "boom"}
    llama.llama_mode_post({"to": "router"})
    assert ini.read_text() == "[a]\nmodel = /x.gguf\n"


def test_successful_switch_leaves_settings_saved(sw, cfg, monkeypatch):
    path = cfg.LLAMA_CONFIG_INI
    cfg.LLAMA_CONFIG_INI = ""
    cfg.AGENT_USER = "tester"
    home = Path(path).parent
    llama.pwd = SimpleNamespace(getpwnam=lambda u: SimpleNamespace(pw_dir=str(home)))
    try:
        assert llama.llama_mode_post({"to": "router"})["ok"] is True
    finally:
        import pwd as _pwd
        llama.pwd = _pwd
    monkeypatch.setattr(llama, "llama_mode_block", _REAL_MODE_BLOCK)
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: _flags_info(["--models-preset", "${X}"]))
    monkeypatch.setattr(llama, "_llama_cache_root", lambda: None)
    b = llama.llama_mode_block()
    assert b["settings_source"] == "saved" and b["presets_file"] == cfg.LLAMA_CONFIG_INI


def test_switch_apply_uses_detected_unit_and_header(cfg, monkeypatch):
    cfg.LLAMA_SYSTEMD_UNIT = "llama_server.service"
    seen = {}
    monkeypatch.setattr(llama, "_detected", {"unit": "llama-server.service"})
    monkeypatch.setattr(llama, "_unit_show", lambda unit, force=False: {"load_state": ""})
    monkeypatch.setattr(llama, "_llama_svc_file_path", lambda: "/u/llama-server.service")
    monkeypatch.setattr(llama, "reconcile_now", lambda: None)
    monkeypatch.setattr(llama._shared, "wrapper_version", lambda w: 3)
    monkeypatch.setattr(llama._shared, "svcconfig_apply",
                        lambda w, path, tokens, sudoers_hint="", **kw: seen.update(kw) or {"ok": True})
    assert llama._switch_apply(["/b/llama-server", "--port", "1"])["ok"] is True
    assert seen["restart_unit"] == "llama-server.service" and seen["unit"] == "llama-server.service"


@pytest.mark.parametrize("detected", [False, True])
def test_add_presets_file_end_to_end(cfg, monkeypatch, tmp_path, detected):
    etc = tmp_path / "etc-llama"
    etc.mkdir()
    yaml_f = tmp_path / "agent_config.yaml"
    yaml_f.write_text("LLAMA_ENABLED: true\n")
    cfg._loaded_from = str(yaml_f)
    cfg.LLAMA_CONFIG_INI = ""
    st = {"argv": "/usr/bin/llama-server --host 127.0.0.1 --port 9931"}

    def run(cmd, **kw):
        if list(cmd[:2]) == ["systemctl", "show"]:
            out = (f"ExecStart={{ path=/usr/bin/llama-server ; argv[]={st['argv']} ; ignore_errors=no }}\n"
                   "User=\nLoadState=loaded\nActiveState=active\n"
                   "FragmentPath=/usr/lib/systemd/system/llama-server.service\n"
                   "DropInPaths=/etc/systemd/system/llama-server.service.d/50-llm-systems-agent.conf\n")
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    def apply(wrapper, path, tokens, hint, **kw):
        import shlex
        st["argv"] = shlex.join(tokens)
        return {"ok": True}
    monkeypatch.setattr(llama, "_SYSTEM_PRESETS_DIR", str(etc))
    monkeypatch.setattr(llama.subprocess, "run", run)
    det = {}
    if detected:
        cfg.LLAMA_SYSTEMD_UNIT, cfg.LLAMA_API_URL = "llama_server.service", "http://localhost:8080"
        det = {"unit": "llama-server.service", "api_url": "http://127.0.0.1:9931", "mode": "router",
               "configured_by": "", "managed": True, "unit_path": "/usr/lib/systemd/system/llama-server.service"}
    monkeypatch.setattr(llama, "_detected", det)
    monkeypatch.setattr(llama, "_detect_matched", set())
    monkeypatch.setattr(llama, "_unit_info_cache", {})
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: tmp_path / "nohub")
    monkeypatch.setattr(llama, "_llama_cache_root", lambda: None)
    monkeypatch.setattr(llama, "reconcile_now", lambda: None)
    monkeypatch.setattr(llama.time, "sleep", lambda s: None)
    monkeypatch.setattr(llama._shared, "wrapper_version", lambda w: 3)
    monkeypatch.setattr(llama._shared, "svcconfig_apply", apply)
    monkeypatch.setattr(llama._shared, "svcconfig_op", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(llama.requests, "get", lambda url, timeout=0, **kw: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"data": []}), raising=False)

    assert llama.llama_mode_block()["mode"] == "router"
    assert llama.llama_mode_block()["presets_file"] == ""
    r = llama.llama_mode_post({"to": "router"})
    want = str(etc / "models.ini")
    assert r["ok"] is True and r["presets_file"] == want
    assert "--models-preset" in st["argv"] and want in st["argv"]
    text = Path(want).read_text()
    assert text.strip() == "# llama-server models (managed from the LLM Systems Manager dashboard)"
    block = llama.llama_mode_block()
    assert block["presets_file"] == want and block["mode"] == "router"
    assert llama._llama_effective_presets_path() == want
    out = llama.llama_models_endpoint()
    assert out["extra"]["mode"]["presets_file"] == want
    assert cfg.LLAMA_CONFIG_INI == want
    assert f'LLAMA_CONFIG_INI: "{want}"' in yaml_f.read_text()


def test_presets_choice_prefers_writable_system_folder(cfg, monkeypatch, tmp_path):
    etc = tmp_path / "etc-llama"
    etc.mkdir()
    cfg.LLAMA_CONFIG_INI = ""
    cfg.AGENT_USER = "tester"
    home = tmp_path / "home"
    monkeypatch.setattr(llama, "_SYSTEM_PRESETS_DIR", str(etc))
    monkeypatch.setattr(llama, "pwd", SimpleNamespace(getpwnam=lambda u: SimpleNamespace(pw_dir=str(home))))
    assert llama._presets_path_choice() == str(etc / "models.ini")
    monkeypatch.setattr(llama, "_SYSTEM_PRESETS_DIR", str(tmp_path / "absent"))
    assert llama._presets_path_choice() == str(home / ".config/llama.cpp/models.ini")


def test_unmanaged_wins_over_already_router(sw):
    sw.block = _block(managed=False, mode="router", presets_file="/p.ini")
    r = llama.llama_mode_post({"to": "router"})
    assert r.status_code == 409 and _body(r)["reason"] == "unmanaged"
