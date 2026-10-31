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
        monkeypatch.setattr(inv, "_last", sentinel)
        monkeypatch.setattr(inv, "_gen", inv._gen + 1)
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
