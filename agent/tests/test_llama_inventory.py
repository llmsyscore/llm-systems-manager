# agent/tests/test_llama_inventory.py
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

from conftest import llama_install, llama_inventory as inv


def _exe(path: Path, out: str = "version: 6150 (a0f7016d)", rc: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{out}'\nexit {rc}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _cfg(**kw):
    base = {"LLAMA_BIN": "", "LLAMA_BUILD_SEARCH_DIRS": [], "LLAMA_BUILD_NOTES": {}}
    base.update(kw)
    return SimpleNamespace(**base)


def _scan(cfg, root, monkeypatch, which=None):
    monkeypatch.setattr(inv.shutil, "which", lambda _n: which)
    return inv.scan(cfg, llama_install.installed_build_id, str(root))


def test_finds_builds_under_root_and_marks_active(tmp_path, monkeypatch):
    root = tmp_path / "root"
    vk = _exe(root / "vulkan" / "build" / "bin" / "llama-server")
    (vk.parent / "libggml-vulkan.so").write_text("")
    rocm = _exe(root / "rocm" / "bin" / "llama-server", "version: 0.4.0 (build 7, commit df03399)")
    (rocm.parent / "libggml-hip.so").write_text("")
    res = _scan(_cfg(LLAMA_BIN=str(vk)), root, monkeypatch)
    rows = {r["path"]: r for r in res["builds"]}
    assert set(rows) == {os.path.realpath(vk), os.path.realpath(rocm)}
    assert res["builds"][0]["path"] == os.path.realpath(vk)
    a, b = rows[os.path.realpath(vk)], rows[os.path.realpath(rocm)]
    assert a["active"] and not b["active"]
    assert (a["backend"], a["build"], a["commit"], a["ok"]) == ("Vulkan", "6150", "a0f7016d", True)
    assert (b["backend"], b["build"], b["commit"]) == ("ROCm", "7", "df03399")
    assert inv.cached() is res


def test_skips_backup_and_hidden_dirs(tmp_path, monkeypatch):
    root = tmp_path / "root"
    live = _exe(root / "llama-server")
    _exe(root / ".upgrade.bak.20261031" / "llama-server")
    _exe(root / ".git" / "llama-server")
    res = _scan(_cfg(LLAMA_BIN=str(live)), root, monkeypatch)
    assert [r["path"] for r in res["builds"]] == [os.path.realpath(live)]


def test_dedupes_symlink_and_path_hit(tmp_path, monkeypatch):
    root = tmp_path / "root"
    real = _exe(root / "bin" / "llama-server")
    link = tmp_path / "link-server"
    link.symlink_to(real)
    res = _scan(_cfg(LLAMA_BIN=str(link)), root, monkeypatch, which=str(real))
    assert len(res["builds"]) == 1 and res["builds"][0]["active"]


def test_search_dirs_and_notes(tmp_path, monkeypatch):
    extra = _exe(tmp_path / "vendor" / "bin" / "llama-server")
    noted = _exe(tmp_path / "noted" / "llama-server")
    cfg = _cfg(LLAMA_BUILD_SEARCH_DIRS=str(tmp_path / "vendor"),
               LLAMA_BUILD_NOTES={str(noted.parent): "MTP build"})
    res = _scan(cfg, tmp_path / "none", monkeypatch)
    rows = {r["path"]: r for r in res["builds"]}
    assert set(rows) == {os.path.realpath(extra), os.path.realpath(noted)}
    assert rows[os.path.realpath(noted)]["note"] == "MTP build"
    assert rows[os.path.realpath(extra)]["note"] == ""


def test_missing_and_broken_binaries_report_errors(tmp_path, monkeypatch):
    root = tmp_path / "root"
    broken = _exe(root / "llama-server", "error while loading shared libraries", rc=127)
    gone = tmp_path / "gone" / "llama-server"
    res = _scan(_cfg(LLAMA_BIN=str(gone)), root, monkeypatch)
    rows = {r["path"]: r for r in res["builds"]}
    assert rows[str(gone)]["error"] == "not found" and not rows[str(gone)]["ok"]
    b = rows[os.path.realpath(broken)]
    assert not b["ok"] and "shared libraries" in b["error"]


def test_backend_from_marker_wins_over_libs(tmp_path):
    d = tmp_path / "b"; d.mkdir()
    (d / "libggml-vulkan.so").write_text("")
    assert inv.detect_backend(d) == "Vulkan"
    (d / inv.BUILD_MARKER).write_text(json.dumps({"backend": "rocm"}))
    assert inv.detect_backend(d) == "ROCm"
    e = tmp_path / "e"; e.mkdir()
    assert inv.detect_backend(e) == "Unknown"
    (e / "libggml-cpu.so").write_text("")
    assert inv.detect_backend(e) == "CPU"


def test_caps_build_count(tmp_path, monkeypatch):
    root = tmp_path / "root"
    for i in range(inv.MAX_BUILDS + 3):
        _exe(root / f"b{i:02d}" / "llama-server")
    res = _scan(_cfg(), root, monkeypatch)
    assert len(res["builds"]) == inv.MAX_BUILDS and res["truncated"]


def test_unreadable_dir_is_skipped(tmp_path, monkeypatch):
    root = tmp_path / "root"
    ok = _exe(root / "a" / "llama-server")
    locked = root / "locked"; locked.mkdir()
    _exe(locked / "llama-server")
    locked.chmod(0)
    try:
        res = _scan(_cfg(), root, monkeypatch)
    finally:
        locked.chmod(0o755)
    paths = [r["path"] for r in res["builds"]]
    assert os.path.realpath(ok) in paths
    if os.geteuid() != 0:
        assert len(paths) == 1


def test_waiting_caller_gets_running_scan_result(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(inv.shutil, "which", lambda _n: None)
    out = {}
    sentinel = {"ok": True, "builds": [], "scanned_at": 1, "truncated": False}
    inv._lock.acquire()
    try:
        t = threading.Thread(target=lambda: out.setdefault(
            "r", inv.scan(_cfg(), llama_install.installed_build_id, str(tmp_path))))
        t.start()
        t.join(0.2)
        monkeypatch.setitem(inv._state, "last", sentinel)
        monkeypatch.setitem(inv._state, "gen", inv._state["gen"] + 1)
    finally:
        inv._lock.release()
    t.join(5)
    assert out["r"] is sentinel


def test_previously_active_build_stays_listed(tmp_path, monkeypatch):
    a = _exe(tmp_path / "a" / "llama-server")
    b = _exe(tmp_path / "b" / "llama-server")
    seen = tmp_path / "seen.json"
    _scan_seen = lambda bin_: (monkeypatch.setattr(inv.shutil, "which", lambda _n: None),
                               inv.scan(_cfg(LLAMA_BIN=str(bin_)), llama_install.installed_build_id,
                                        str(tmp_path / "none"), seen))[1]
    _scan_seen(a)
    res = _scan_seen(b)
    rows = {r["path"]: r["active"] for r in res["builds"]}
    assert rows == {os.path.realpath(b): True, os.path.realpath(a): False}
    assert json.loads(seen.read_text()) == [os.path.realpath(b), os.path.realpath(a)]


def test_remember_drops_missing_and_survives_bad_file(tmp_path):
    seen = tmp_path / "seen.json"
    seen.write_text("not json")
    a = _exe(tmp_path / "a" / "llama-server")
    assert inv.remember(str(a), seen) == [os.path.realpath(a)]
    a.unlink()
    assert inv.remember("", seen) == []
    assert json.loads(seen.read_text()) == []


def _switch_calls(*, preflight=(True, None), apply_ok=(True, True), ready=((True, None), (True, None)),
                  persist_exc=None):
    calls = {"apply": [], "persist": [], "tail": 0}
    applies = iter(apply_ok)
    readies = iter(ready)

    def apply_exec(tokens):
        calls["apply"].append(list(tokens))
        return {"ok": next(applies)}

    def persist(t):
        calls["persist"].append(t)
        if persist_exc:
            raise persist_exc
        return "cfg.bak"

    def tail():
        calls["tail"] += 1
        return ["boom"]

    kw = dict(preflight=lambda _t: preflight, apply_exec=apply_exec,
              wait_ready=lambda: next(readies), persist=persist, log_tail=tail)
    return calls, kw


def test_switch_success_keeps_flags_and_persists():
    calls, kw = _switch_calls()
    res = inv.switch("/new/llama-server", ["/old/llama-server", "--port", "8080"], **kw)
    assert res["ok"] and res["active"] == "/new/llama-server" and res["previous"] == "/old/llama-server"
    assert calls["apply"] == [["/new/llama-server", "--port", "8080"]]
    assert calls["persist"] == ["/new/llama-server"] and res["config_backup"] == "cfg.bak"


def test_switch_preflight_failure_changes_nothing():
    calls, kw = _switch_calls(preflight=(False, "missing lib"))
    res = inv.switch("/new/llama-server", ["/old/llama-server"], **kw)
    assert not res["ok"] and res["stage"] == "check" and res["error"] == "missing lib"
    assert calls["apply"] == [] and calls["persist"] == []


def test_switch_not_ready_rolls_back():
    calls, kw = _switch_calls(ready=((False, "crashed"), (True, None)))
    res = inv.switch("/new/llama-server", ["/old/llama-server", "-m", "x"], **kw)
    assert not res["ok"] and res["rolled_back"] and res["restored"] and res["error"] == "crashed"
    assert calls["apply"] == [["/new/llama-server", "-m", "x"], ["/old/llama-server", "-m", "x"]]
    assert calls["persist"] == [] and res["log"] == ["boom"]


def test_switch_rollback_that_also_fails_is_reported():
    calls, kw = _switch_calls(ready=((False, "crashed"), (False, "no answer")))
    res = inv.switch("/new/llama-server", ["/old/llama-server"], **kw)
    assert res["rolled_back"] and not res["restored"] and res["restore_error"] == "no answer"


def test_switch_apply_failure_restores():
    calls, kw = _switch_calls(apply_ok=(False, True), ready=((True, None),))
    res = inv.switch("/new/llama-server", ["/old/llama-server"], **kw)
    assert not res["ok"] and res["restored"] and len(calls["apply"]) == 2


def test_switch_persist_failure_is_a_warning():
    _calls, kw = _switch_calls(persist_exc=OSError("read-only"))
    res = inv.switch("/new/llama-server", ["/old/llama-server"], **kw)
    assert res["ok"] and "read-only" in res["warning"]


def test_set_yaml_key_replaces_live_line_and_keeps_comments(tmp_path):
    cfg = tmp_path / "agent_config.yaml"
    cfg.write_text("# LLAMA_BIN: old-comment\nLLAMA_BIN: /a/llama-server\nOTHER: 1\n")
    backup = inv.set_yaml_key(cfg, "LLAMA_BIN", "/b/llama-server")
    assert cfg.read_text() == '# LLAMA_BIN: old-comment\nLLAMA_BIN: "/b/llama-server"\nOTHER: 1\n'
    assert Path(backup).read_text().startswith("# LLAMA_BIN")
    cfg2 = tmp_path / "c2.yaml"
    cfg2.write_text("OTHER: 1\n")
    inv.set_yaml_key(cfg2, "LLAMA_BIN", "/b/llama-server")
    assert cfg2.read_text().endswith('LLAMA_BIN: "/b/llama-server"\n')


def test_listed_build_matches_last_scan_only(tmp_path, monkeypatch):
    a = _exe(tmp_path / "root" / "a" / "llama-server")
    _scan(_cfg(), tmp_path / "root", monkeypatch)
    assert inv.listed_build(str(a))["path"] == os.path.realpath(a)
    assert inv.listed_build(str(tmp_path / "other" / "llama-server")) is None
    inv.forget_cache()
    assert inv.listed_build(str(a)) is None


def test_switch_rollback_still_waits_when_restore_restart_timed_out():
    calls, kw = _switch_calls(apply_ok=(True, False), ready=((False, "crashed"), (True, None)))
    res = inv.switch("/new/llama-server", ["/old/llama-server"], **kw)
    assert res["rolled_back"] and res["restored"] and res["restore_error"] is None


def test_build_that_needs_its_library_folder_is_not_ok(tmp_path, monkeypatch):
    root = tmp_path / "root"
    a = _exe(root / "a" / "llama-server")
    b = _exe(root / "b" / "llama-server")
    plain = lambda p: (False, "libllama.so: cannot open") if p == os.path.realpath(b) else (True, None)
    monkeypatch.setattr(inv.shutil, "which", lambda _n: None)
    res = inv.scan(_cfg(), llama_install.installed_build_id, str(root), plain_check=plain)
    rows = {r["path"]: r for r in res["builds"]}
    assert rows[os.path.realpath(a)]["ok"]
    rb = rows[os.path.realpath(b)]
    assert not rb["ok"] and rb["commit"] == "a0f7016d"
    assert rb["error"].startswith("runs only with its own folder on the library path")


def test_unit_run_context_reads_cwd_and_environment():
    unit = ('[Service]\nUser=llmsys\nWorkingDirectory=/usr/local/llama-server/llama.cpp/\n'
            'Environment="HSA_ENABLE_SDMA=0" "PATH=/usr/bin:/usr/local/bin"\n'
            'Environment=FOO=bar\nExecStart=/x/llama-server --port 8080\n')
    cwd, env = inv.unit_run_context(unit)
    assert cwd == "/usr/local/llama-server/llama.cpp/"
    assert env == {"HSA_ENABLE_SDMA": "0", "PATH": "/usr/bin:/usr/local/bin", "FOO": "bar"}
    assert inv.unit_run_context("WorkingDirectory=~\n") == (None, {})
    assert inv.unit_run_context("WorkingDirectory=-/opt/x\n")[0] == "/opt/x"
