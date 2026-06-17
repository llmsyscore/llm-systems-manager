# agent/tests/test_llama_install.py
from __future__ import annotations

import types
import pytest
from conftest import llama_install as li


def _cfg(**kw):
    base = dict(LLAMA_BIN="/usr/local/llama-server/llama-server",
                LLAMA_BUILD_DIR="", LLAMA_BUILD_OPTS={},
                LLAMA_SYSTEMD_UNIT="llama_server.service")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_custom_script_default_uses_legacy_path():
    plan = li.plan("custom_script", {}, _cfg())
    assert plan.method == "custom_script"
    assert plan.steps == [["sudo", "-n", li.LEGACY_SCRIPT]]
    assert plan.resolve_binary() == "/usr/local/llama-server/llama-server"


def test_custom_script_honors_script_path_opt():
    plan = li.plan("custom_script", {"script_path": "/opt/x/build.sh"}, _cfg())
    assert plan.steps == [["sudo", "-n", "/opt/x/build.sh"]]


def test_empty_method_falls_back_to_custom_script():
    plan = li.plan("", {}, _cfg())
    assert plan.method == "custom_script"


def test_unknown_method_raises_install_error():
    with pytest.raises(li.InstallError):
        li.plan("nixpkgs", {}, _cfg())


def test_source_clone_when_absent(tmp_path):
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    plan = li.plan("source", {"git_ref": "b1234", "backend": "vulkan"}, cfg)
    src = str(tmp_path / "src")
    build = str(tmp_path / "src" / "build")
    assert plan.method == "source"
    assert plan.steps[0] == ["git", "clone", "--depth", "1", "--branch", "b1234", "--", li.REPO_URL, src]
    assert ["cmake", "-S", src, "-B", build, "-DGGML_VULKAN=ON"] in plan.steps
    assert ["cmake", "--build", build, "--target", "llama-server", "-j"] in plan.steps
    assert plan.resolve_binary() == str(tmp_path / "src" / "build" / "bin" / "llama-server")
    assert all(s[0] != "sudo" for s in plan.steps)


def test_source_fetch_when_present(tmp_path):
    (tmp_path / "src").mkdir()
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    plan = li.plan("source", {}, cfg)
    src = str(tmp_path / "src")
    assert plan.steps[0] == ["git", "-C", src, "fetch", "--depth", "1", "origin", "--", "master"]
    assert plan.steps[1] == ["git", "-C", src, "checkout", "-f", "FETCH_HEAD"]


def test_source_rejects_flaglike_ref(tmp_path):
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    for bad in ("--upload-pack=evil", "-x", "a b", "foo;bar", "../etc", "a..b", "/abs", "trail/"):
        with pytest.raises(li.InstallError):
            li.plan("source", {"git_ref": bad}, cfg)


def test_source_rejects_unknown_backend(tmp_path):
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    with pytest.raises(li.InstallError):
        li.plan("source", {"backend": "cude"}, cfg)


def test_source_jobs_caps_parallelism(tmp_path):
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    plan = li.plan("source", {"jobs": 4}, cfg)
    build = str(tmp_path / "src" / "build")
    assert ["cmake", "--build", build, "--target", "llama-server", "-j", "4"] in plan.steps
