# agent/tests/test_llama_install.py
from __future__ import annotations

import io
import os
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
    assert plan.tools == ("sudo",)


def test_empty_method_falls_back_to_custom_script():
    plan = li.plan("", {}, _cfg())
    assert plan.method == "custom_script"


def test_unknown_method_raises_install_error():
    with pytest.raises(li.InstallError):
        li.plan("nixpkgs", {}, _cfg())


def test_source_clone_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(li.os, "cpu_count", lambda: 6)
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    plan = li.plan("source", {"git_ref": "b1234", "backend": "vulkan"}, cfg)
    src = str(tmp_path / "src")
    build = str(tmp_path / "src" / "build")
    assert plan.method == "source"
    assert plan.steps[0] == ["git", "clone", "--depth", "1", "--branch", "b1234", "--", li.REPO_URL, src]
    assert ["cmake", "-S", src, "-B", build, "-DCMAKE_BUILD_TYPE=Release", "-DGGML_VULKAN=ON"] in plan.steps
    assert ["cmake", "--build", build, "--target", "llama-server", "-j", "6"] in plan.steps
    assert plan.resolve_binary() == str(tmp_path / "src" / "build" / "bin" / "llama-server")
    assert all(s[0] != "sudo" for s in plan.steps)
    assert plan.tools == ("git", "cmake")


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


def test_source_jobs_defaults_to_nproc(tmp_path, monkeypatch):
    monkeypatch.setattr(li.os, "cpu_count", lambda: 12)
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    build = str(tmp_path / "src" / "build")
    for opts in ({}, {"jobs": ""}, {"jobs": None}):
        plan = li.plan("source", opts, cfg)
        assert ["cmake", "--build", build, "--target", "llama-server", "-j", "12"] in plan.steps


def test_source_rejects_invalid_jobs(tmp_path):
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    for bad in ("x", "1.5", 0, -3):
        with pytest.raises(li.InstallError):
            li.plan("source", {"jobs": bad}, cfg)


def test_source_builds_existing_tool_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(li.os, "cpu_count", lambda: 4)
    install = tmp_path / "install"
    install.mkdir()
    for name in ("llama-server", "llama-bench", "llama-cli"):
        f = install / name
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    (install / "llama-server.log").write_text("log")   # dot => not a target
    (install / "libllama.so").write_text("lib")        # not a llama-* tool
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path / "bld"), LLAMA_BIN=str(install / "llama-server"))
    plan = li.plan("source", {}, cfg)
    build = str(tmp_path / "bld" / "src" / "build")
    assert ["cmake", "--build", build, "--target", "llama-server", "-j", "4"] in plan.steps
    sh_steps = [s for s in plan.steps if s[0] == "sh"]
    assert len(sh_steps) == 1
    cmd = sh_steps[0][2]
    assert "llama-bench" in cmd and "llama-cli" in cmd
    assert "llama-server.log" not in cmd and "libllama.so" not in cmd
    assert "|| echo" in cmd


def test_source_no_extra_step_when_only_server(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / "llama-server").write_text("x")
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path / "bld"), LLAMA_BIN=str(install / "llama-server"))
    plan = li.plan("source", {}, cfg)
    assert all(s[0] != "sh" for s in plan.steps)


def _configure_step(plan):
    return next(s for s in plan.steps if s[:1] == ["cmake"] and "-S" in s)


def test_source_sets_release_build_type(tmp_path):
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    for backend in ("cpu", "cuda", "vulkan", "metal"):
        plan = li.plan("source", {"backend": backend}, cfg)
        cfg_step = _configure_step(plan)
        assert "-DCMAKE_BUILD_TYPE=Release" in cfg_step


def test_source_non_rocm_env_empty(tmp_path):
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    for backend in ("cpu", "vulkan", "cuda", "metal"):
        plan = li.plan("source", {"backend": backend}, cfg)
        assert plan.env == {}


def test_source_rocm_populates_hip_env(tmp_path, monkeypatch):
    monkeypatch.setattr(li.shutil, "which",
                        lambda name: "/opt/rocm/bin/hipconfig" if name == "hipconfig" else None)

    def fake_run(cmd, **kw):
        out = "/opt/rocm/llvm/bin" if cmd[1] == "-l" else "/opt/rocm"
        return types.SimpleNamespace(stdout=out + "\n", returncode=0)

    monkeypatch.setattr(li.subprocess, "run", fake_run)
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    plan = li.plan("source", {"backend": "rocm"}, cfg)
    cfg_step = _configure_step(plan)
    assert "-DCMAKE_BUILD_TYPE=Release" in cfg_step and "-DGGML_HIP=ON" in cfg_step
    assert plan.env == {"HIPCXX": "/opt/rocm/llvm/bin/clang", "HIP_PATH": "/opt/rocm"}


def test_source_rocm_missing_hipconfig_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(li.shutil, "which", lambda name: None)
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    with pytest.raises(li.InstallError):
        li.plan("source", {"backend": "rocm"}, cfg)


def test_source_rocm_empty_hipconfig_output_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(li.shutil, "which", lambda name: "/opt/rocm/bin/hipconfig")
    monkeypatch.setattr(li.subprocess, "run",
                        lambda cmd, **kw: types.SimpleNamespace(stdout="  \n", returncode=0))
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    with pytest.raises(li.InstallError):
        li.plan("source", {"backend": "rocm"}, cfg)


def test_source_rocm_hipconfig_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(li.shutil, "which", lambda name: "/opt/rocm/bin/hipconfig")

    def boom(cmd, **kw):
        raise li.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(li.subprocess, "run", boom)
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    with pytest.raises(li.InstallError):
        li.plan("source", {"backend": "rocm"}, cfg)


def test_cleanup_source_removes_build_keeps_src(tmp_path):
    root = tmp_path / "bld"
    (root / "src" / "build" / "bin").mkdir(parents=True)
    (root / "src" / "CMakeLists.txt").write_text("x")
    live = tmp_path / "live"
    live.mkdir()
    (live / "llama-server").write_text("x")
    cfg = _cfg(LLAMA_BUILD_DIR=str(root), LLAMA_BIN=str(live / "llama-server"))
    li.cleanup_after_inplace(cfg, "source")
    assert not (root / "src" / "build").exists()
    assert (root / "src" / "CMakeLists.txt").exists()


def test_cleanup_release_removes_release_and_tarball(tmp_path):
    root = tmp_path / "bld"
    (root / "release" / "bin").mkdir(parents=True)
    (root / "release.download").write_text("tar")
    live = tmp_path / "live"
    live.mkdir()
    (live / "llama-server").write_text("x")
    cfg = _cfg(LLAMA_BUILD_DIR=str(root), LLAMA_BIN=str(live / "llama-server"))
    li.cleanup_after_inplace(cfg, "release_binary")
    assert not (root / "release").exists()
    assert not (root / "release.download").exists()


def test_cleanup_skips_dir_containing_llama_bin(tmp_path):
    root = tmp_path / "bld"
    server = root / "release" / "bin" / "llama-server"
    server.parent.mkdir(parents=True)
    server.write_text("x")
    cfg = _cfg(LLAMA_BUILD_DIR=str(root), LLAMA_BIN=str(server))
    li.cleanup_after_inplace(cfg, "release_binary")
    assert (root / "release").exists()
    assert server.exists()


# Task 4: release_binary handler

def test_release_binary_builds_download_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(li, "_resolve_release_asset",
                        lambda version, backend="cpu": "https://example.com/llama-bin-ubuntu-x64.zip")
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    plan = li.plan("release_binary", {"version": "latest"}, cfg)
    dest = str(tmp_path / "release")
    assert plan.method == "release_binary"
    assert plan.steps[0] == ["mkdir", "-p", dest]
    # curl downloads the resolved asset URL
    assert any(s[0] == "curl" and "https://example.com/llama-bin-ubuntu-x64.zip" in s for s in plan.steps)
    assert all(s[0] != "sudo" for s in plan.steps)
    # .zip asset unpacks with unzip and declares it as a required tool
    assert plan.steps[-1][0] == "unzip"
    assert plan.tools == ("curl", "unzip")


def test_release_binary_targz_uses_tar(tmp_path, monkeypatch):
    monkeypatch.setattr(li, "_resolve_release_asset",
                        lambda version, backend="cpu": "https://example.com/llama-bin-ubuntu-x64.tar.gz")
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    plan = li.plan("release_binary", {}, cfg)
    assert plan.steps[-1][0] == "tar"
    assert plan.tools == ("curl", "tar")
    assert all(s[0] != "sh" for s in plan.steps)


def test_release_binary_resolve_finds_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(li, "_resolve_release_asset", lambda version, backend="cpu": "https://x/y.zip")
    dest = tmp_path / "release" / "build" / "bin"
    dest.mkdir(parents=True)
    (dest / "llama-server").touch()
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    plan = li.plan("release_binary", {}, cfg)
    assert plan.resolve_binary() == str(dest / "llama-server")


def _assets(*names):
    return [{"name": n, "browser_download_url": f"https://example.com/{n}"} for n in names]


def test_select_asset_cpu_skips_accelerator_variants():
    # OpenVINO listed first must not win for a CPU backend
    assets = _assets("llama-b1-bin-ubuntu-openvino-2026.2-x64.tar.gz",
                     "llama-b1-bin-ubuntu-vulkan-x64.tar.gz",
                     "llama-b1-bin-ubuntu-x64.tar.gz")
    dl = li._select_asset(assets, ["ubuntu", "x64"], li._RELEASE_VARIANT["cpu"])
    assert dl == "https://example.com/llama-b1-bin-ubuntu-x64.tar.gz"


def test_select_asset_vulkan_requires_token():
    assets = _assets("llama-b1-bin-ubuntu-x64.tar.gz",
                     "llama-b1-bin-ubuntu-vulkan-x64.tar.gz")
    dl = li._select_asset(assets, ["ubuntu", "x64"], li._RELEASE_VARIANT["vulkan"])
    assert dl == "https://example.com/llama-b1-bin-ubuntu-vulkan-x64.tar.gz"


def test_select_asset_returns_none_when_no_match():
    assets = _assets("llama-b1-bin-ubuntu-openvino-x64.tar.gz")
    assert li._select_asset(assets, ["ubuntu", "x64"], li._RELEASE_VARIANT["cpu"]) is None


def test_release_binary_rejects_unknown_backend(tmp_path):
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    with pytest.raises(li.InstallError):
        li.plan("release_binary", {"backend": "openvino"}, cfg)


def test_release_binary_passes_backend_to_resolver(tmp_path, monkeypatch):
    seen = {}

    def fake_resolve(version, backend="cpu"):
        seen["backend"] = backend
        return "https://example.com/llama-b1-bin-ubuntu-vulkan-x64.tar.gz"

    monkeypatch.setattr(li, "_resolve_release_asset", fake_resolve)
    cfg = _cfg(LLAMA_BUILD_DIR=str(tmp_path))
    li.plan("release_binary", {"backend": "vulkan"}, cfg)
    assert seen["backend"] == "vulkan"


# Task 5: conda handler

def test_conda_uses_conda_when_present(monkeypatch):
    monkeypatch.setattr(li.shutil, "which",
                        lambda n: "/opt/conda/bin/conda" if n == "conda"
                        else ("/opt/conda/bin/llama-server" if n == "llama-server" else None))
    plan = li.plan("conda", {}, _cfg())
    assert plan.method == "conda"
    assert plan.steps == [["conda", "install", "-y", "-c", "conda-forge", "llama-cpp"]]
    assert plan.tools == ("conda",)
    assert plan.resolve_binary() == "/opt/conda/bin/llama-server"
    assert all(s[0] != "sudo" for s in plan.steps)


def test_conda_falls_back_to_mamba(monkeypatch):
    monkeypatch.setattr(li.shutil, "which",
                        lambda n: "/opt/conda/bin/mamba" if n == "mamba" else None)
    plan = li.plan("conda", {}, _cfg())
    assert plan.steps[0][0] == "mamba"


def test_conda_missing_raises(monkeypatch):
    monkeypatch.setattr(li.shutil, "which", lambda n: None)
    with pytest.raises(li.InstallError):
        li.plan("conda", {}, _cfg())


# Task 6: homebrew handler

def test_homebrew_install_when_absent(monkeypatch):
    monkeypatch.setattr(li.shutil, "which", lambda n: "/opt/homebrew/bin/brew" if n == "brew" else None)
    # brew list --formula llama.cpp -> not installed (rc=1)
    monkeypatch.setattr(li.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    plan = li.plan("homebrew", {}, _cfg())
    assert plan.method == "homebrew"
    assert plan.steps == [["/opt/homebrew/bin/brew", "install", "llama.cpp"]]
    assert plan.tools == ("/opt/homebrew/bin/brew",)
    assert all(s[0] != "sudo" for s in plan.steps)


def test_homebrew_upgrade_when_present(monkeypatch):
    monkeypatch.setattr(li.shutil, "which", lambda n: "/opt/homebrew/bin/brew" if n == "brew" else None)
    monkeypatch.setattr(li.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="/opt/homebrew\n", stderr=""))
    plan = li.plan("homebrew", {}, _cfg())
    assert plan.steps == [["/opt/homebrew/bin/brew", "upgrade", "llama.cpp"]]
    assert plan.resolve_binary() == "/opt/homebrew/bin/llama-server"


def test_homebrew_missing_raises(monkeypatch):
    monkeypatch.setattr(li.shutil, "which", lambda n: None)
    with pytest.raises(li.InstallError):
        li.plan("homebrew", {}, _cfg())


# Task 7: detect_method

def test_detect_homebrew_path():
    assert li.detect_method(_cfg(LLAMA_BIN="/opt/homebrew/bin/llama-server")) == "homebrew"


def test_detect_conda_path():
    assert li.detect_method(_cfg(LLAMA_BIN="/home/u/miniconda3/envs/llm/bin/llama-server")) == "conda"


def test_detect_release_binary(tmp_path):
    binp = str(tmp_path / "release" / "build" / "bin" / "llama-server")
    assert li.detect_method(_cfg(LLAMA_BIN=binp, LLAMA_BUILD_DIR=str(tmp_path))) == "release_binary"


def test_detect_custom_script_when_legacy_present(monkeypatch, tmp_path):
    monkeypatch.setattr(li.os.path, "exists", lambda p: p == li.LEGACY_SCRIPT)
    assert li.detect_method(_cfg(LLAMA_BIN="/usr/local/llama-server/llama-server",
                                 LLAMA_BUILD_DIR=str(tmp_path))) == "custom_script"


def test_detect_returns_none_when_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(li.os.path, "exists", lambda p: False)
    assert li.detect_method(_cfg(LLAMA_BIN="/random/bin/llama-server",
                                 LLAMA_BUILD_DIR=str(tmp_path))) is None


def test_detect_source_when_checkout_present(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "CMakeLists.txt").touch()
    assert li.detect_method(_cfg(LLAMA_BIN="/random/bin/llama-server",
                                 LLAMA_BUILD_DIR=str(tmp_path))) == "source"


# Task 8 (#88): missing_build_tools + run_install (shared install executor)

def test_missing_build_tools_present_and_absent():
    env = {"PATH": os.environ.get("PATH", "")}
    assert li.missing_build_tools(["sh"], env) == []
    assert li.missing_build_tools(["definitely-not-a-real-binary-xyz"], env) == \
        ["definitely-not-a-real-binary-xyz"]


def test_missing_build_tools_any_of_tuple():
    env = {"PATH": os.environ.get("PATH", "")}
    assert li.missing_build_tools([("definitely-not-a-real-binary-xyz", "sh")], env) == []
    assert li.missing_build_tools([("no-a-xyz", "no-b-xyz")], env) == ["no-a-xyz or no-b-xyz"]


def test_missing_build_tools_absolute_path(tmp_path):
    real = tmp_path / "real-tool"; real.touch(); real.chmod(0o755)
    ghost = tmp_path / "ghost-tool"
    env = {"PATH": ""}                                    # absolute paths ignore PATH
    assert li.missing_build_tools([str(real)], env) == []
    assert li.missing_build_tools([str(ghost)], env) == [str(ghost)]


class _FakeProc:
    def __init__(self, lines, rc):
        self.stdout = io.StringIO("".join(lines))
        self._rc = rc
        self.returncode = None

    def wait(self):
        self.returncode = self._rc


def _scripted_popen(scripted):
    state = {"i": 0, "steps": []}

    def popen(step, **kw):
        i = state["i"]; state["i"] += 1
        state["steps"].append(step)
        lines, rc = scripted[i] if i < len(scripted) else ([], 0)
        return _FakeProc(lines, rc)

    return popen, state


def _plan(steps, *, tools=(), resolve="/x/llama-server"):
    return li.InstallPlan(method="source", label="source", steps=steps,
                          cwd=None, env={}, tools=tools,
                          resolve_binary=lambda: resolve)


def test_run_install_streams_lines_and_resolves():
    popen, state = _scripted_popen([(["building...\n", "\x1b[32mdone\x1b[0m\n"], 0)])
    seen = []
    rc, resolved = li.run_install(_plan([["cmake"]]), emit=seen.append, popen=popen)
    assert rc == 0
    assert resolved == "/x/llama-server"
    assert seen == ["building...", "done"]          # ANSI stripped, blanks dropped
    assert state["steps"] == [["cmake"]]


def test_run_install_preflight_missing_tool_skips_steps():
    popen, state = _scripted_popen([([], 0)])
    seen = []
    rc, resolved = li.run_install(_plan([["nope"]], tools=("definitely-not-a-real-binary-xyz",)),
                                  emit=seen.append, popen=popen)
    assert rc == 127
    assert resolved is None
    assert state["i"] == 0                            # popen never called
    assert any("required command(s) not found" in s for s in seen)


def test_run_install_step_failure_breaks_and_skips_resolve():
    popen, state = _scripted_popen([(["boom\n"], 1), (["unreached\n"], 0)])
    seen = []
    rc, resolved = li.run_install(_plan([["a"], ["b"]]), emit=seen.append, popen=popen)
    assert rc == 1
    assert resolved is None                           # resolve skipped on failure
    assert state["i"] == 1                            # second step never ran


def test_run_install_resolve_exception_is_swallowed():
    popen, _ = _scripted_popen([([], 0)])

    def boom():
        raise RuntimeError("nope")

    plan = li.InstallPlan(method="source", label="source", steps=[["a"]],
                          cwd=None, env={}, tools=(), resolve_binary=boom)
    rc, resolved = li.run_install(plan, emit=lambda _s: None, popen=popen)
    assert rc == 0 and resolved is None


def test_strip_ansi_removes_color_and_clear_codes():
    assert li.strip_ansi("\x1b[32mdone\x1b[0m") == "done"
    assert li.strip_ansi("\x1b[2K\x1b[1Gprogress") == "progress"


def test_strip_ansi_removes_cursor_visibility_codes():
    # tqdm wraps progress in cursor hide/show escapes; must strip so JSON parses (#102)
    raw = '\x1b[?25l[{"file": "a.gguf", "size": "-"}]\x1b[?25h'
    assert li.strip_ansi(raw) == '[{"file": "a.gguf", "size": "-"}]'
