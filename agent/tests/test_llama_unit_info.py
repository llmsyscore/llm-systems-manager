"""#1419: llama unit facts, mode block and wrapper-driven service control."""
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
_FIX = Path(__file__).resolve().parent / "fixtures" / "detect_llama"
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


def _fx(name):
    return (_FIX / name).read_text()


class _Runner:
    def __init__(self, show="systemctl_show_llama.txt"):
        self.show = show
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[:2] == ["systemctl", "show"] and self.show:
            return subprocess.CompletedProcess(cmd, 0, stdout=_fx(self.show), stderr="")
        if cmd[:2] == ["systemctl", "status"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="active (running)", stderr="")
        if cmd[:1] == ["systemctl"]:
            raise FileNotFoundError("systemctl")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def env(monkeypatch):
    cfg = SimpleNamespace(LLAMA_ENABLED=True, LLAMA_SYSTEMD_UNIT="llama-server.service",
                          LLAMA_API_URL="http://localhost:8080", LLAMA_CONFIG_INI="",
                          LLAMA_CACHE_DIR="", LLAMA_MODELS_DIR="")
    llama.set_context(SimpleNamespace(config=cfg, check_bearer=lambda *_: None))
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "reconcile_now", lambda: None)
    monkeypatch.setattr(llama, "_mode_probe_memo", {"ts": 0.0, "url": None, "ok": False})
    monkeypatch.setattr(llama, "_cached_models_memo", {"ts": 0.0, "key": None, "rows": []})
    monkeypatch.setattr(llama.requests, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")),
                        raising=False)
    llama._unit_info_invalidate()
    yield cfg
    llama._unit_info_invalidate()


def _use(monkeypatch, runner):
    monkeypatch.setattr(llama.subprocess, "run", runner)
    llama._unit_info_invalidate()
    return runner


def test_unit_info_parses_show_output_and_drop_in_source(env, monkeypatch):
    _use(monkeypatch, _Runner())
    i = llama.llama_unit_info()
    assert i["unit"] == "llama-server.service"
    assert i["unit_path"] == "/usr/lib/systemd/system/llama-server.service"
    assert i["drop_ins"] == ["/etc/systemd/system/llama-server.service.d/50-llm-systems-agent.conf"]
    assert (i["unit_user"], i["load_state"], i["active_state"]) == ("llama", "loaded", "active")
    assert i["environment"] == {"FOO": "bar"}
    assert i["flags"] == ["--host", "0.0.0.0", "--port", "9931"]
    assert i["source"] == "drop-in"


def test_svc_file_path_uses_fragment_path(env, monkeypatch):
    _use(monkeypatch, _Runner())
    assert llama._llama_svc_file_path() == "/usr/lib/systemd/system/llama-server.service"


def test_no_systemctl_keeps_legacy_path(env, monkeypatch):
    _use(monkeypatch, _Runner(show=None))
    i = llama.llama_unit_info()
    assert i["source"] == "none" and i["unit"] == "" and i["flags"] == []
    assert llama._llama_svc_file_path() == "/etc/systemd/system/llama-server.service"


def test_router_mode_from_merged_exec_start_honours_drop_in(env, monkeypatch, tmp_path):
    frag = tmp_path / "u.service"
    frag.write_text("[Service]\nExecStart=/usr/bin/llama-server -m x.gguf\n")
    show = tmp_path / "show.txt"
    show.write_text(f"FragmentPath={frag}\nDropInPaths=/d/50.conf\nLoadState=loaded\nActiveState=active\n"
                    "ExecStart={ path=/x ; argv[]=/usr/bin/llama-server --models-preset p.ini ; ignore_errors=no }\n")
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=show.read_text(), stderr="")
    _use(monkeypatch, run)
    assert llama._llama_router_mode_from_unit_file() is True


def test_exec_tokens_follow_the_drop_in_not_the_fragment(env, monkeypatch, tmp_path):
    frag = tmp_path / "u.service"
    frag.write_text("[Service]\nExecStart=/usr/bin/llama-server -m x.gguf\n")
    show = (f"FragmentPath={frag}\nDropInPaths=/d/50.conf\nLoadState=loaded\nActiveState=active\n"
            "ExecStart={ path=/usr/bin/llama-server ; argv[]=/usr/bin/llama-server --models-preset p.ini ; ignore_errors=no }\n")
    _use(monkeypatch, lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=show, stderr=""))
    assert llama._llama_exec_tokens() == ["/usr/bin/llama-server", "--models-preset", "p.ini"]


def test_mode_block_single_env(env, monkeypatch):
    _use(monkeypatch, _Runner("systemctl_show_env_model.txt"))
    b = llama.llama_mode_block()
    assert (b["mode"], b["configured_by"]) == ("single", "environment")
    assert b["managed"] is True and b["settings_source"] == "default"
    assert set(b) == {"mode", "configured_by", "presets_file", "presets_writable", "models_dir", "unit",
                      "unit_path", "unit_user", "cache_dir", "managed", "settings_source",
                      "agent_user", "api_url", "helper_update", "root_update_cmd"}
    assert b["agent_user"] and isinstance(b["agent_user"], str)
    assert b["api_url"] == llama._llama_effective_api_url()


def test_mode_block_unmanaged_when_no_unit(env, monkeypatch):
    _use(monkeypatch, _Runner(show=None))
    b = llama.llama_mode_block()
    assert b["managed"] is False and b["unit"] == ""


def test_settings_source_saved_and_detected(env, monkeypatch):
    _use(monkeypatch, _Runner())
    env.LLAMA_SYSTEMD_UNIT, env.LLAMA_API_URL, env.LLAMA_CONFIG_INI = "x.service", "http://h:1", "/p.ini"
    assert llama.llama_mode_block()["settings_source"] == "saved"
    env.LLAMA_SYSTEMD_UNIT, env.LLAMA_API_URL, env.LLAMA_CONFIG_INI = "llama_server.service", "http://localhost:8080", ""
    monkeypatch.setattr(llama, "_detected", {"unit": "llama-server.service"})
    assert llama.llama_mode_block()["settings_source"] == "detected"


def test_effective_unit_falls_back_to_detected(env, monkeypatch):
    env.LLAMA_SYSTEMD_UNIT = "missing.service"
    monkeypatch.setattr(llama, "_detected", {"unit": "llama-server.service"})
    def run(cmd, **kw):
        out = "LoadState=not-found\n" if cmd[2] == "missing.service" else _fx("systemctl_show_llama.txt")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    _use(monkeypatch, run)
    assert llama._llama_effective_unit() == "llama-server.service"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_presets_writable_false_for_root_owned_dir(env, monkeypatch, tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    env.LLAMA_CONFIG_INI = str(d / "p.ini")
    d.chmod(0o555)
    try:
        _use(monkeypatch, _Runner(show=None))
        assert llama.llama_mode_block()["presets_writable"] is False
        d.chmod(0o755)
        llama._unit_info_invalidate()
        assert llama.llama_mode_block()["presets_writable"] is True
    finally:
        d.chmod(0o755)


def _wrapper(tmp_path, version):
    w = tmp_path / "wrap"
    w.write_text(f"#!/bin/bash\n# SVCCONFIG_WRAPPER_VERSION={version}\n")
    return str(w)


def test_systemctl_uses_wrapper_when_version_3(env, monkeypatch, tmp_path):
    _use(monkeypatch, _Runner())
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", _wrapper(tmp_path, 3))
    seen = []
    monkeypatch.setattr(llama._shared, "svcconfig_op",
                        lambda w, op, unit, timeout=30: seen.append((w, op, unit)) or {"ok": True, "error": None})
    assert llama._llama_systemctl("restart")["ok"] is True
    assert seen == [(llama._SVCCONFIG_WRAPPER, "restart", "llama-server.service")]


def test_systemctl_falls_back_to_sudoers_rule_when_wrapper_refuses(env, monkeypatch, tmp_path):
    r = _use(monkeypatch, _Runner())
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", _wrapper(tmp_path, 3))
    monkeypatch.setattr(llama._shared, "svcconfig_op",
                        lambda w, op, unit, timeout=30: {"ok": False, "error": "not a llama-server or vllm unit: x"})
    assert llama._llama_systemctl("restart")["ok"] is True
    assert ["sudo", "-n", "/usr/bin/systemctl", "restart", "llama-server.service"] in r.calls


def test_systemctl_reports_wrapper_refusal_when_sudoers_also_denies(env, monkeypatch, tmp_path):
    class R(_Runner):
        def __call__(self, cmd, **kw):
            if cmd[:2] == ["sudo", "-n"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="sudo: a password is required")
            return super().__call__(cmd, **kw)
    _use(monkeypatch, R())
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", _wrapper(tmp_path, 3))
    monkeypatch.setattr(llama._shared, "svcconfig_op",
                        lambda w, op, unit, timeout=30: {"ok": False, "error": "unit is not loaded: y"})
    res = llama._llama_systemctl("restart")
    assert res["ok"] is False and res["error"] == "unit is not loaded: y"


def test_systemctl_without_a_unit_says_so(env, monkeypatch, tmp_path):
    _use(monkeypatch, _Runner(show=None))
    monkeypatch.setattr(llama, "_llama_effective_unit", lambda: "")
    res = llama._llama_systemctl("start")
    assert res["ok"] is False and "No llama-server service" in res["error"]


def test_systemctl_falls_back_to_sudo_systemctl_on_old_wrapper(env, monkeypatch, tmp_path):
    r = _use(monkeypatch, _Runner())
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", _wrapper(tmp_path, 2))
    assert llama._llama_systemctl("stop")["ok"] is True
    assert ["sudo", "-n", "/usr/bin/systemctl", "stop", "llama-server.service"] in r.calls


def test_unit_info_cache_invalidated_by_restart(env, monkeypatch, tmp_path):
    r = _use(monkeypatch, _Runner())
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", _wrapper(tmp_path, 2))
    llama.llama_unit_info()
    llama.llama_unit_info()
    shows = lambda: sum(1 for c in r.calls if c[:2] == ["systemctl", "show"])
    n = shows()
    llama.llama_unit_info()
    assert shows() == n
    llama._llama_systemctl("restart")
    llama.llama_unit_info()
    assert shows() > n


def test_status_endpoint_includes_mode_and_survives_mode_failure(env, monkeypatch):
    _use(monkeypatch, _Runner())
    r = llama.llama_server_status_endpoint()
    assert r["ok"] is True and r["mode"]["unit"] == "llama-server.service"
    monkeypatch.setattr(llama, "llama_mode_block", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    r = llama.llama_server_status_endpoint()
    assert r["mode"] is None and "output" in r


def test_detect_module_frozen_branch(tmp_path, monkeypatch):
    inst = tmp_path / "install"
    inst.mkdir()
    (inst / "detect_llama.py").write_text("MARK = 'frozen-stub'\n")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "path", [str(inst.parent)])
    monkeypatch.delitem(sys.modules, "detect_llama", raising=False)
    try:
        mod = llama._detect_module()
        assert mod.MARK == "frozen-stub"
    finally:
        sys.modules.pop("detect_llama", None)


def test_svc_path_empty_when_systemctl_ran_but_unit_absent(env, monkeypatch):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="LoadState=not-found\n", stderr="")
    _use(monkeypatch, run)
    assert llama._llama_svc_file_path() == ""
    assert llama.llama_svcconfig_get()["error"] == "no systemd unit found for llama-server"
    assert llama.llama_svcconfig_post({"binary": "b", "args": []})["error"] == "no systemd unit found for llama-server"
    assert llama._llama_router_mode_from_unit_file() is False


def test_show_nonzero_rc_is_ignored(env, monkeypatch):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout=_fx("systemctl_show_llama.txt"), stderr="x")
    _use(monkeypatch, run)
    i = llama.llama_unit_info()
    assert i["source"] == "none" and i["unit"] == ""
    assert llama._llama_svc_file_path() == ""


def test_invalidate_mid_flight_skips_store(env, monkeypatch):
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        llama._unit_info_invalidate()
        return subprocess.CompletedProcess(cmd, 0, stdout=_fx("systemctl_show_llama.txt"), stderr="")
    _use(monkeypatch, run)
    llama._unit_show("llama-server.service")
    llama._unit_show("llama-server.service")
    assert len(calls) == 2


def test_models_dir_and_cache_dir_resolution_order(env, monkeypatch):
    _use(monkeypatch, _Runner())
    monkeypatch.setattr(llama, "_detected", {"models_dir": "/det"})
    assert llama.llama_mode_block()["models_dir"] == "/det"
    env.LLAMA_MODELS_DIR = "/cfg"
    assert llama.llama_mode_block()["models_dir"] == "/cfg"
    _use(monkeypatch, lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="LoadState=loaded\nFragmentPath=/u\nDropInPaths=\nEnvironment=LLAMA_CACHE=/envc\n"
        "ExecStart={ path=/x ; argv[]=/x --models-dir /flag ; ignore_errors=no }\n", stderr=""))
    b = llama.llama_mode_block()
    assert b["models_dir"] == "/flag" and b["cache_dir"] == "/envc"
    env.LLAMA_CACHE_DIR = "/cfgc"
    assert llama.llama_mode_block()["cache_dir"] == "/cfgc"


def _extract_probe(logger, mode_fn, rich_fn, state_fn):
    import re
    src = (_AGENT_ROOT / "llm-systems-agent.py").read_text()
    m = re.search(r"^def _probe_and_reconcile\(.*?(?=^def )", src, re.S | re.M)
    ns = {"Any": object, "CONFIG": SimpleNamespace(LMS_ENABLED=False, LLAMA_ENABLED=True,
                                                   VLLM_ENABLED=False, LLAMA_API_URL="http://x"),
          "collect_llama_for_metrics": rich_fn, "llama_get_state": state_fn,
          "llama_api_port": lambda u: 1, "llama_mode_block": mode_fn, "logger": logger,
          "_reconcile_residency": lambda s: s}
    exec("from __future__ import annotations\n" + m.group(0), ns)
    return ns["_probe_and_reconcile"]


@pytest.mark.parametrize("rich", [{"state": "up"}, None])
def test_probe_attaches_mode_in_both_branches_and_guards(rich):
    log = SimpleNamespace(debug=lambda *a: None)
    f = _extract_probe(log, lambda: {"mode": "single"}, lambda: dict(rich) if rich else None, lambda: "down")
    assert f()["llama"]["mode"] == {"mode": "single"}
    def boom():
        raise RuntimeError("x")
    f = _extract_probe(log, boom, lambda: dict(rich) if rich else None, lambda: "down")
    out = f()
    assert "mode" not in out["llama"] and "state" in out["llama"]


def _no_unit(env, monkeypatch):
    _use(monkeypatch, lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="LoadState=not-found\n", stderr=""))
    assert llama._llama_svc_file_path() == ""


def test_exec_tokens_empty_path_raises_value_error(env, monkeypatch):
    _no_unit(env, monkeypatch)
    with pytest.raises(ValueError, match="no systemd unit found"):
        llama._llama_exec_tokens()


def test_switch_preflight_empty_path_does_not_raise(env, monkeypatch):
    _no_unit(env, monkeypatch)
    seen = {}
    def run(cmd, **kw):
        if cmd[:1] == ["systemctl"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="LoadState=not-found\n", stderr="")
        seen["cwd"] = kw.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="version", stderr="")
    monkeypatch.setattr(llama.subprocess, "run", run)
    llama._unit_info_invalidate()
    llama._switch_preflight("/bin/echo")
    assert seen["cwd"] is None


def test_switch_apply_empty_path_returns_error(env, monkeypatch):
    _no_unit(env, monkeypatch)
    assert llama._switch_apply(["x"]) == {"ok": False, "error": "no systemd unit found for llama-server"}


class _SudoDenied(_Runner):
    def __call__(self, cmd, **kw):
        if cmd[:1] == ["sudo"]:
            self.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="sudo: a password is required")
        return super().__call__(cmd, **kw)


def test_sudo_denied_on_old_wrapper_names_the_root_update(env, monkeypatch, tmp_path):
    _use(monkeypatch, _SudoDenied())
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", _wrapper(tmp_path, 2))
    res = llama._llama_systemctl("restart")
    assert res["ok"] is False
    assert "one-time root update" in res["error"] and llama._ROOT_UPDATE_CMD in res["error"]


def test_helper_update_flag_when_unit_differs_from_baked(env, monkeypatch, tmp_path):
    _use(monkeypatch, _Runner())
    w = tmp_path / "wrap"
    w.write_text("#!/bin/bash\n# SVCCONFIG_WRAPPER_VERSION=2\nUNIT_PATH='/etc/systemd/system/llama_server.service'\n")
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", str(w))
    monkeypatch.setattr(llama, "_detected", {"unit": "llama-server.service"})
    assert llama._helper_needs_update() is True
    block = llama.llama_mode_block()
    assert block["helper_update"] is True and block["root_update_cmd"] == llama._ROOT_UPDATE_CMD
    llama._require_ctx().config.LLAMA_SYSTEMD_UNIT = "llama_server.service"
    monkeypatch.setattr(llama, "_detected", {"unit": "llama_server.service"})
    assert llama._helper_needs_update() is False
    llama._require_ctx().config.LLAMA_SYSTEMD_UNIT = "llama-server.service"
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", _wrapper(tmp_path, 3))
    monkeypatch.setattr(llama, "_detected", {"unit": "llama-server.service"})
    assert llama._helper_needs_update() is False


def test_environment_file_model_makes_mode_block_single(env, monkeypatch, tmp_path):
    ef = tmp_path / "llama.env"
    ef.write_text('# c\nLLAMA_ARG_MODEL="/m/a.gguf"\nLLAMA_CACHE=/srv/cache\n')
    show = tmp_path / "show.txt"
    show.write_text(f"Environment=FOO=bar\nEnvironmentFiles={ef} (ignore_errors=no) "
                    f"{tmp_path / 'missing.env'} (ignore_errors=yes)\n"
                    "ExecStart={ path=/usr/bin/llama-server ; argv[]=/usr/bin/llama-server ; ignore_errors=no }\n"
                    "LoadState=loaded\nActiveState=active\nFragmentPath=/u/llama-server.service\nDropInPaths=\n")
    _use(monkeypatch, lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=show.read_text(), stderr=""))
    assert llama.llama_unit_info()["environment"] == {
        "FOO": "bar", "LLAMA_ARG_MODEL": "/m/a.gguf", "LLAMA_CACHE": "/srv/cache"}
    b = llama.llama_mode_block()
    assert (b["mode"], b["configured_by"]) == ("single", "environment")


def _pkg_install(env, monkeypatch, tmp_path, marker, dpkg):
    env.AGENT_INSTALL_DIR = str(tmp_path / "inst")
    (tmp_path / "inst").mkdir()
    if marker:
        (tmp_path / "inst" / ".llmsys-package-state").write_text("x\n")
    lst = tmp_path / "llm-systems-agent.list"
    if dpkg:
        lst.write_text("/opt\n")
    monkeypatch.setattr(llama, "_DPKG_LIST", str(lst))


def test_root_update_cmd_deb(env, monkeypatch, tmp_path):
    _pkg_install(env, monkeypatch, tmp_path, marker=True, dpkg=True)
    assert llama._root_update_cmd() == "sudo dpkg-reconfigure llm-systems-agent"


def test_root_update_cmd_rpm(env, monkeypatch, tmp_path):
    _pkg_install(env, monkeypatch, tmp_path, marker=True, dpkg=False)
    monkeypatch.setattr(llama.shutil, "which", lambda n: "/usr/bin/dnf" if n == "dnf" else None)
    assert llama._root_update_cmd() == "sudo dnf reinstall llm-systems-agent"
    monkeypatch.setattr(llama.shutil, "which", lambda n: None)
    assert llama._root_update_cmd() == "sudo yum reinstall llm-systems-agent"


def test_root_update_cmd_script_install(env, monkeypatch, tmp_path):
    _pkg_install(env, monkeypatch, tmp_path, marker=False, dpkg=True)
    assert llama._root_update_cmd() == llama._ROOT_UPDATE_CMD


def test_root_update_cmd_feeds_mode_block_and_sudo_error(env, monkeypatch, tmp_path):
    _pkg_install(env, monkeypatch, tmp_path, marker=True, dpkg=True)
    _use(monkeypatch, _SudoDenied())
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", _wrapper(tmp_path, 2))
    assert llama.llama_mode_block()["root_update_cmd"] == "sudo dpkg-reconfigure llm-systems-agent"
    assert "sudo dpkg-reconfigure llm-systems-agent" in llama._llama_systemctl("restart")["error"]


def test_vendor_unit_without_drop_in_reports_drop_in_source(env, monkeypatch):
    show = _fx("systemctl_show_llama.txt").replace(
        "DropInPaths=/etc/systemd/system/llama-server.service.d/50-llm-systems-agent.conf", "DropInPaths=")
    _use(monkeypatch, _Runner(show=None))
    monkeypatch.setattr(llama.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=show if cmd[:2] == ["systemctl", "show"] else "", stderr=""))
    llama._unit_info_invalidate()
    i = llama.llama_unit_info(force=True)
    assert i["drop_ins"] == [] and i["source"] == "drop-in"
    show2 = show.replace("FragmentPath=/usr/lib/systemd/system/llama-server.service",
                         "FragmentPath=/etc/systemd/system/llama-server.service")
    monkeypatch.setattr(llama.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=show2 if cmd[:2] == ["systemctl", "show"] else "", stderr=""))
    llama._unit_info_invalidate()
    assert llama.llama_unit_info(force=True)["source"] == "unit"
    # an /etc unit with an operator drop-in is still edited in place by the helper
    show3 = show2.replace("DropInPaths=", "DropInPaths=/etc/systemd/system/llama-server.service.d/override.conf")
    monkeypatch.setattr(llama.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=show3 if cmd[:2] == ["systemctl", "show"] else "", stderr=""))
    llama._unit_info_invalidate()
    assert llama.llama_unit_info(force=True)["source"] == "unit"
