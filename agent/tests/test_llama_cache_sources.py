"""#1419: hub, flat llama.cpp cache and models-dir rows in the model list."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_llama_unit_info as base  # noqa: E402

llama = base.llama


@pytest.fixture
def env(monkeypatch):
    cfg = SimpleNamespace(LLAMA_ENABLED=True, LLAMA_SYSTEMD_UNIT="", LLAMA_API_URL="http://x:1",
                          LLAMA_CONFIG_INI="", LLAMA_CACHE_DIR="", LLAMA_MODELS_DIR="", AGENT_USER="")
    llama.set_context(SimpleNamespace(config=cfg, check_bearer=lambda *_: None))
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "_mode_probe_memo", {"ts": 0.0, "url": None, "ok": False})
    monkeypatch.setattr(llama, "_cached_models_memo", {"ts": 0.0, "key": None, "rows": []})
    return cfg


def _hub(root, repo, names):
    d = root / f"models--{repo.replace('/', '--')}" / "snapshots" / "abc"
    d.mkdir(parents=True)
    for n in names:
        (d / n).write_bytes(b"xx")


def _side(p, url):
    p.with_name(p.name + ".json").write_text(json.dumps({"url": url, "etag": "e", "lastModified": "l"}))


def test_flat_layout_rows_from_filename_and_manifest(tmp_path):
    a = tmp_path / "unsloth_Qwen3-GGUF_Qwen3-Q8_0.gguf"
    a.write_bytes(b"abc")
    _side(a, "https://huggingface.co/unsloth/Qwen3-GGUF/resolve/main/Qwen3-Q8_0.gguf")
    (tmp_path / "unsloth_Qwen2.5_7B-Instruct-Q4_K_M.gguf").write_bytes(b"abcd")
    (tmp_path / "mmproj-F16.gguf").write_bytes(b"x")
    rows = {r["path"].rsplit("/", 1)[1]: r for r in llama._list_llama_cache_ggufs(tmp_path)}
    assert set(rows) == {a.name, "unsloth_Qwen2.5_7B-Instruct-Q4_K_M.gguf"}
    r = rows[a.name]
    assert (r["repo"], r["file"], r["size"], r["source"]) == ("unsloth/Qwen3-GGUF", "Qwen3-Q8_0.gguf", 3, "llama-cache")
    u = rows["unsloth_Qwen2.5_7B-Instruct-Q4_K_M.gguf"]
    assert (u["repo"], u["file"]) == ("", "unsloth_Qwen2.5_7B-Instruct-Q4_K_M.gguf")


def test_flat_without_sidecar_not_duplicated_against_hub(env, monkeypatch, tmp_path):
    hub, flat = tmp_path / "hub", tmp_path / "flat"
    flat.mkdir()
    _hub(hub, "a/b", ["b-Q4_K_M.gguf"])
    (flat / "b-Q4_K_M.gguf").write_bytes(b"x")
    (flat / "own_Qwen2.5_7B-Q4_K_M.gguf").write_bytes(b"x")
    _wire(env, monkeypatch, hub, flat)
    rows = llama.cached_models()
    assert sorted(r["model_id"] for r in rows) == ["a/b:Q4_K_M", "own_Qwen2.5_7B-Q4_K_M"]


def test_models_dir_rows_are_file_stems(env, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "tiny-Q4_0.gguf").write_bytes(b"x")
    rows = llama._list_models_dir_ggufs(tmp_path)
    assert [(r["repo"], r["file"], r["source"]) for r in rows] == [("", "tiny-Q4_0.gguf", "models-dir")]


def _wire(env, monkeypatch, hub, flat=None, mdir=None, block=None):
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: hub)
    monkeypatch.setattr(llama, "_llama_cache_root", lambda: flat)
    env.LLAMA_MODELS_DIR = str(mdir) if mdir else ""
    monkeypatch.setattr(llama, "llama_mode_block", lambda: block or {
        "mode": "router", "presets_writable": True, "managed": True, "models_dir": env.LLAMA_MODELS_DIR})


def test_cached_models_dedupes_hub_first(env, monkeypatch, tmp_path):
    hub, flat = tmp_path / "hub", tmp_path / "flat"
    flat.mkdir()
    _hub(hub, "a/b", ["b-Q4_K_M.gguf"])
    (flat / "a_b_b-Q4_K_M.gguf").write_bytes(b"x")
    _side(flat / "a_b_b-Q4_K_M.gguf", "https://huggingface.co/a/b/resolve/abc/b-Q4_K_M.gguf")
    _wire(env, monkeypatch, hub, flat)
    rows = llama.cached_models()
    assert len(rows) == 1 and rows[0]["source"] == "hf-hub" and "/snapshots/" in rows[0]["path"]
    assert rows[0]["model_id"] == "a/b:Q4_K_M" and rows[0]["deployable"] is True


def test_deployable_false_when_not_router(env, monkeypatch, tmp_path):
    _hub(tmp_path, "a/b", ["b-Q4_K_M.gguf"])
    _wire(env, monkeypatch, tmp_path, block={"mode": "single", "presets_writable": True, "managed": True})
    assert llama.cached_models()[0]["deployable"] is False


def test_models_dir_model_id_is_stem(env, monkeypatch, tmp_path):
    md = tmp_path / "md"
    md.mkdir()
    (md / "tiny-Q4_0.gguf").write_bytes(b"x")
    _wire(env, monkeypatch, tmp_path / "none", None, md)
    rows = llama.cached_models()
    assert rows[0]["model_id"] == "tiny-Q4_0"


def test_quant_upper():
    assert llama.quant_upper("a/b:q4_k_m") == "a/b:Q4_K_M"
    assert llama.quant_upper("plain") == "plain"
    assert llama.quant_upper("a:b:c") == "a:b:C"


def test_cached_merge_quant_case(env, monkeypatch, tmp_path):
    _hub(tmp_path, "a/b", ["b-q4_k_m.gguf", "b-Q8_0.gguf"])
    _wire(env, monkeypatch, tmp_path)
    monkeypatch.setattr(llama.requests, "get", lambda *a, **k: SimpleNamespace(
        json=lambda: {"data": [{"id": "a/b:q4_k_m"}, {"id": "other"}]}), raising=False)
    monkeypatch.setattr(llama, "_drop_ghost_presets", lambda l: l)
    out = llama.llama_models_endpoint()
    assert [m["id"] for m in out["data"]] == ["a/b:q4_k_m", "other"]
    assert [r["model_id"] for r in out["extra"]["cached"]] == ["a/b:Q8_0"]


def test_models_endpoint_extra_has_cached_and_mode(env, monkeypatch, tmp_path):
    _hub(tmp_path, "a/b", ["b-Q4_K_M.gguf"])
    _wire(env, monkeypatch, tmp_path)
    monkeypatch.setattr(llama.requests, "get", lambda *a, **k: SimpleNamespace(
        json=lambda: {"data": [{"id": "x"}, {"id": "y"}]}), raising=False)
    monkeypatch.setattr(llama, "_drop_ghost_presets", lambda l: l)
    out = llama.llama_models_endpoint()
    assert out["extra"]["mode"]["mode"] == "router"
    assert len(out["extra"]["cached"]) == 1 and out["extra"]["cached"][0]["deployable"] is True


def test_locate_quant_files_finds_flat_layout(env, monkeypatch, tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "a_b_b-Q4_K_M.gguf").write_bytes(b"x")
    _side(flat / "a_b_b-Q4_K_M.gguf", "https://huggingface.co/a/b/resolve/abc/b-Q4_K_M.gguf")
    _wire(env, monkeypatch, tmp_path / "nohub", flat)
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: SimpleNamespace(has_section=lambda s: False))
    files, err = llama._locate_quant_files("a/b:Q4_K_M")
    assert err is None and [p.name for p in files] == ["a_b_b-Q4_K_M.gguf"]


def test_cache_root_prefers_config_then_env_then_default(env, monkeypatch, tmp_path):
    cfgd, envd, home = tmp_path / "c", tmp_path / "e", tmp_path / "home"
    cfgd.mkdir(); envd.mkdir()
    (home / ".cache" / "llama.cpp").mkdir(parents=True)
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: {
        "environment": {"LLAMA_CACHE": str(envd)}, "unit_user": "svc"})
    monkeypatch.setattr(llama.pwd, "getpwnam", lambda u: SimpleNamespace(pw_dir=str(home)))
    env.LLAMA_CACHE_DIR = str(cfgd)
    assert llama._llama_cache_root() == cfgd
    env.LLAMA_CACHE_DIR = ""
    assert llama._llama_cache_root() == envd
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: {"environment": {}, "unit_user": "svc"})
    assert llama._llama_cache_root() == home / ".cache" / "llama.cpp"
    monkeypatch.setattr(llama.pwd, "getpwnam", lambda u: SimpleNamespace(pw_dir=str(tmp_path / "nohome")))
    assert llama._llama_cache_root() is None


def test_models_endpoint_extra_failure_keeps_listing(env, monkeypatch):
    monkeypatch.setattr(llama.requests, "get", lambda *a, **k: SimpleNamespace(
        json=lambda: {"data": [{"id": "x"}]}), raising=False)
    monkeypatch.setattr(llama, "_drop_ghost_presets", lambda l: l)

    def boom(*a, **k):
        raise OSError("boom")
    monkeypatch.setattr(llama, "llama_mode_block", boom)
    out = llama.llama_models_endpoint()
    assert out["data"] == [{"id": "x"}] and out["extra"] == {"cached": [], "mode": {}}


def test_delete_never_touches_models_dir_files(env, monkeypatch, tmp_path):
    md = tmp_path / "md"
    md.mkdir()
    f = md / "tiny-Q4_0.gguf"
    f.write_bytes(b"x")
    _wire(env, monkeypatch, tmp_path / "hub", None, md)
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: SimpleNamespace(has_section=lambda s: False))
    deleted, err = llama._delete_quant_from_hf_cache("tiny-Q4_0")
    assert deleted == [] and err and f.exists()


def test_cache_gguf_ok_without_detect_module(env, monkeypatch):
    monkeypatch.setattr(llama, "_detect_module", lambda: (_ for _ in ()).throw(ImportError("gone")))
    monkeypatch.setattr(llama, "_unit_show", lambda unit, force=False: llama._empty_unit_info())
    monkeypatch.setattr(llama, "_hf_cache_rows", lambda: [])
    monkeypatch.setattr(llama, "_llama_cache_root", lambda: None)
    monkeypatch.setattr(llama, "_models_endpoint_ok", lambda base: False)
    r = llama.llama_cache_gguf(None)
    assert r["ok"] is True


def _count_scans(monkeypatch):
    n = {"walk": 0}
    real = llama._list_models_dir_ggufs

    def counted(root):
        n["walk"] += 1
        return real(root)
    monkeypatch.setattr(llama, "_list_models_dir_ggufs", counted)
    return n


def test_cached_models_memoised_within_ttl(env, monkeypatch, tmp_path):
    md = tmp_path / "md"
    md.mkdir()
    (md / "tiny-Q4_0.gguf").write_bytes(b"x")
    _wire(env, monkeypatch, tmp_path / "none", None, md)
    n = _count_scans(monkeypatch)
    assert llama.cached_models()[0]["model_id"] == "tiny-Q4_0"
    (md / "more-Q8_0.gguf").write_bytes(b"x")
    assert len(llama.cached_models()) == 1 and n["walk"] == 1


def test_cached_models_invalidate_rescans(env, monkeypatch, tmp_path):
    md = tmp_path / "md"
    md.mkdir()
    (md / "tiny-Q4_0.gguf").write_bytes(b"x")
    _wire(env, monkeypatch, tmp_path / "none", None, md)
    n = _count_scans(monkeypatch)
    llama.cached_models()
    (md / "more-Q8_0.gguf").write_bytes(b"x")
    llama._cached_models_invalidate()
    assert len(llama.cached_models()) == 2 and n["walk"] == 2


def test_cached_models_rescans_after_ttl(env, monkeypatch, tmp_path):
    md = tmp_path / "md"
    md.mkdir()
    _wire(env, monkeypatch, tmp_path / "none", None, md)
    n = _count_scans(monkeypatch)
    llama.cached_models()
    llama._cached_models_memo["ts"] -= llama._CACHED_MODELS_TTL_S + 1
    llama.cached_models()
    assert n["walk"] == 2


def test_mode_probe_runs_once_for_two_blocks(env, monkeypatch):
    calls = []
    monkeypatch.setattr(llama, "_unit_show", lambda unit, force=False: llama._empty_unit_info())
    monkeypatch.setattr(llama, "_llama_cache_root", lambda: None)
    monkeypatch.setattr(llama, "_models_endpoint_ok", lambda base: calls.append(base) or True)
    assert llama.llama_mode_block()["mode"] == "router"
    assert llama.llama_mode_block()["mode"] == "router"
    assert len(calls) == 1
    llama._unit_info_invalidate()
    llama.llama_mode_block()
    assert len(calls) == 2


def test_delete_quant_invalidates_cached_models(env, monkeypatch, tmp_path):
    llama._cached_models_memo.update({"ts": 9e12, "key": ("k",), "rows": [{"model_id": "x"}]})
    f = tmp_path / "m.gguf"
    f.write_bytes(b"x")
    monkeypatch.setattr(llama, "_locate_quant_files", lambda model_id: ([f], None))
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: tmp_path)
    deleted, err = llama._delete_quant_from_hf_cache("a/b:Q4")
    assert err is None and deleted == [str(f)]
    assert llama._cached_models_memo["key"] is None


def test_models_endpoint_unreachable_still_carries_mode(env, monkeypatch):
    def down(*a, **k):
        raise OSError("refused")
    monkeypatch.setattr(llama.requests, "get", down, raising=False)
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {"mode": "router", "unit": "u.service"})
    monkeypatch.setattr(llama, "cached_models", lambda block=None: [{"model_id": "a/b:Q4"}])
    out = llama.llama_models_endpoint()
    body = json.loads(out.body)
    assert out.status_code == 503 and "refused" in body["detail"]
    assert body["extra"]["mode"]["unit"] == "u.service" and body["extra"]["cached"][0]["model_id"] == "a/b:Q4"


def test_models_endpoint_down_hides_rows_already_in_presets(env, monkeypatch, tmp_path):
    ini = tmp_path / "models.ini"
    ini.write_text("[a/b:Q8_0]\nmodel = /m/b.gguf\n")
    env.LLAMA_CONFIG_INI = str(ini)

    def down(*a, **k):
        raise OSError("refused")
    monkeypatch.setattr(llama.requests, "get", down, raising=False)
    monkeypatch.setattr(llama, "_unit_show", lambda unit, force=False: llama._empty_unit_info())
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {"mode": "router"})
    monkeypatch.setattr(llama, "cached_models", lambda block=None: [{"model_id": "a/b:Q8_0"}, {"model_id": "c/d:Q4"}])
    body = json.loads(llama.llama_models_endpoint().body)
    assert [r["model_id"] for r in body["extra"]["cached"]] == ["c/d:Q4"]


def test_delete_last_quant_removes_the_repo_folder(env, monkeypatch, tmp_path):
    hub = tmp_path / "hub"
    repo = hub / "models--a--b"
    snap = repo / "snapshots" / "h1"
    snap.mkdir(parents=True)
    (repo / "refs").mkdir(); (repo / "refs" / "main").write_text("h1")
    f = snap / "b-Q8_0.gguf"; f.write_bytes(b"x")
    (snap / "b-Q8_0.gguf.json").write_text("{}")
    monkeypatch.setattr(llama, "_locate_quant_files", lambda model_id: ([f], None))
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: hub)
    deleted, err = llama._delete_quant_from_hf_cache("a/b:Q8_0")
    assert err is None and not repo.exists() and str(repo) in deleted


def test_delete_one_quant_keeps_the_repo_with_other_quants(env, monkeypatch, tmp_path):
    hub = tmp_path / "hub"
    snap = hub / "models--a--b" / "snapshots" / "h1"
    snap.mkdir(parents=True)
    f = snap / "b-Q8_0.gguf"; f.write_bytes(b"x")
    g = snap / "b-Q4_K_M.gguf"; g.write_bytes(b"y")
    monkeypatch.setattr(llama, "_locate_quant_files", lambda model_id: ([f], None))
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: hub)
    deleted, err = llama._delete_quant_from_hf_cache("a/b:Q8_0")
    assert err is None and g.exists() and (hub / "models--a--b").exists()


def test_models_extra_hides_rows_whose_path_is_in_presets(env, monkeypatch, tmp_path):
    gguf = tmp_path / "b-Q8_0.gguf"
    gguf.write_bytes(b"xx")
    ini = tmp_path / "models.ini"
    ini.write_text(f"[add-to-config]\nmodel = {gguf}\n")
    env.LLAMA_CONFIG_INI = str(ini)
    monkeypatch.setattr(llama, "_unit_show", lambda unit, force=False: llama._empty_unit_info())
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {"mode": "router"})
    monkeypatch.setattr(llama, "cached_models", lambda block=None: [
        {"model_id": "a/b:Q8_0", "path": str(gguf)}, {"model_id": "c/d:Q4", "path": "/m/d.gguf"}])
    out = llama._models_extra(set())
    assert [r["model_id"] for r in out["cached"]] == ["c/d:Q4"]


def _bin(tmp_path, root_owned):
    d = tmp_path / "bin"
    d.mkdir()
    b = d / "llama-server"
    b.write_bytes(b"xx")
    if root_owned:
        d.chmod(0o555)
    return str(b)


def test_build_refused_for_a_packaged_binary(env, monkeypatch, tmp_path):
    env.LLAMA_BIN = ""
    monkeypatch.setitem(llama._detected, "binary", "/usr/bin/llama-server")
    monkeypatch.setattr(llama, "_binary_package", lambda p: "llama.cpp")
    why = llama._build_refusal()
    assert "llama.cpp package" in why and "apt upgrade" in why
    body = json.loads(llama.llama_build({}).body)
    assert body["reason"] == "binary-not-ours"


def test_build_refused_for_homebrew_and_unwritable_binaries(env, monkeypatch, tmp_path):
    env.LLAMA_BIN = ""
    monkeypatch.setattr(llama, "_binary_package", lambda p: "")
    monkeypatch.setitem(llama._detected, "binary", "/home/linuxbrew/.linuxbrew/bin/llama-server")
    assert "brew upgrade" in llama._build_refusal()
    b = _bin(tmp_path, True)
    monkeypatch.setitem(llama._detected, "binary", b)
    monkeypatch.setattr(llama.os, "access", lambda p, m: False)
    env.LLAMA_BUILD_METHOD = "release_binary"
    assert "cannot write" in llama._build_refusal()
    env.LLAMA_BIN = b
    assert "cannot write" in llama._build_refusal()
    env.LLAMA_BUILD_METHOD = "custom_script"
    assert llama._build_refusal() is None


def test_build_allowed_for_an_agent_writable_binary_or_no_binary(env, monkeypatch, tmp_path):
    env.LLAMA_BIN = ""
    env.LLAMA_BUILD_METHOD = "release_binary"
    monkeypatch.setattr(llama, "_binary_package", lambda p: "")
    monkeypatch.setitem(llama._detected, "binary", _bin(tmp_path, False))
    assert llama._build_refusal() is None
    monkeypatch.setitem(llama._detected, "binary", "")
    assert llama._build_refusal() is None


def test_binary_package_parses_dpkg_and_rpm(monkeypatch):
    calls = []
    def run(cmd, **k):
        calls.append(cmd[0])
        if cmd[0] == "dpkg":
            return SimpleNamespace(returncode=0, stdout="llama.cpp: /usr/bin/llama-server\n")
        return SimpleNamespace(returncode=0, stdout="llama-cpp-1.0-1.x86_64\n")
    monkeypatch.setattr(llama.subprocess, "run", run)
    monkeypatch.setattr(llama.shutil, "which", lambda n: "/usr/bin/" + n)
    assert llama._binary_package("/usr/bin/llama-server") == "llama.cpp"
    monkeypatch.setattr(llama.shutil, "which", lambda n: "/usr/bin/rpm" if n == "rpm" else None)
    assert llama._binary_package("/usr/bin/llama-server") == "llama-cpp-1.0-1.x86_64"
    monkeypatch.setattr(llama.subprocess, "run", lambda cmd, **k: SimpleNamespace(returncode=1, stdout=""))
    assert llama._binary_package("/opt/x/llama-server") == ""


def test_live_binary_comes_from_the_unit_when_settings_are_saved(env, monkeypatch):
    env.LLAMA_BIN = ""
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: {
        "exec_start": "/opt/llama.cpp/bin/llama-server --host 0.0.0.0 --port 9931"})
    assert llama._llama_live_binary() == "/opt/llama.cpp/bin/llama-server"
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: {"exec_start": ""})
    monkeypatch.setitem(llama._detected, "binary", "/x/llama-server")
    assert llama._llama_live_binary() == "/x/llama-server"
    env.LLAMA_BIN = "/cfg/llama-server"
    assert llama._llama_live_binary() == "/cfg/llama-server"
