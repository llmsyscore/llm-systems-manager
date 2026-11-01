"""#1302: llama / LM Studio / vLLM run-conditions fact gatherers and the run-conditions routes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests._vllm_tools_load import load


@pytest.fixture(scope="module")
def mods():
    llama, _vllm, vllm_tools, restore = load()
    import sys
    yield llama, sys.modules["providers.lms_tools"], vllm_tools
    restore()


class _Ctx:
    def __init__(self):
        self.config = SimpleNamespace(LLAMA_API_URL="http://127.0.0.1:9931", LLAMA_SYSTEMD_UNIT="llama-server",
                                      LLAMA_ENABLED=True, AGENT_USER="", AGENT_OS="linux", LLAMA_CONFIG_INI="",
                                      LMS_API_URL="http://127.0.0.1:1234", LMS_CMD="/nonexistent/lms",
                                      VLLM_API_URL="http://127.0.0.1:8000", VLLM_SYSTEMD_UNIT="vllm.service")
        self.state = {"token": "", "agent_id": ""}

    def check_bearer(self, *a, **k):
        return None


@pytest.fixture
def ctx(mods, monkeypatch):
    llama, lms_tools, vllm_tools = mods
    c = _Ctx()
    monkeypatch.setattr(llama, "_require_ctx", lambda: c)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(lms_tools, "_ctx", lambda: c)
    monkeypatch.setattr(lms_tools._lms, "_lms_check_enabled", lambda: None)
    monkeypatch.setattr(vllm_tools, "_ctx", lambda: c)
    monkeypatch.setattr(vllm_tools._v, "_vllm_check_enabled", lambda: None)
    return c


@pytest.fixture
def llama(mods, ctx):
    return mods[0]


@pytest.fixture
def lms_tools(mods, ctx):
    return mods[1]


@pytest.fixture
def vllm_tools(mods, ctx):
    return mods[2]


def test_llama_facts_from_props_and_cmdline(llama, monkeypatch, tmp_path):
    f = tmp_path / "m-Q4_K_M.gguf"; f.write_bytes(b"g" * 100)
    props = {"model_path": str(f), "build_info": "b1234-abc",
             "default_generation_settings": {"n_ctx": 32768, "params": {"temperature": 0.8, "top_k": 40, "top_p": 0.95, "min_p": 0.05, "repeat_penalty": 1.0, "seed": -1}}}
    monkeypatch.setattr(llama, "_props_for", lambda mid: props, raising=False)
    monkeypatch.setattr(llama, "_llama_server_cmdline", lambda pid: "llama-server -m x -ngl 99")
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: 123)
    monkeypatch.setattr(llama, "_gpu_label", lambda: ("7900 XTX", "amdgpu 6.12.0"), raising=False)
    monkeypatch.setitem(llama._llama_loaded, "last", "org/m")
    facts = llama._conditions_facts("org/m")
    assert set(facts) == {"gpu_label", "driver", "backend", "backend_build", "install", "server_cmd", "model_id",
                          "model_path", "quantization", "context", "sampling"}
    assert facts["gpu_label"] == "7900 XTX" and facts["driver"] == "amdgpu 6.12.0" and facts["model_id"] == "org/m"
    assert facts["backend"] == "llama.cpp" and facts["backend_build"] == "b1234-abc"
    assert facts["server_cmd"] == "llama-server -m x -ngl 99" and facts["model_path"] == str(f)
    assert facts["quantization"] == "Q4_K_M" and facts["context"] == 32768
    assert facts["sampling"] == {"temperature": 0.8, "top_k": 40, "top_p": 0.95, "min_p": 0.05, "repeat_penalty": 1.0, "seed": -1}


def test_llama_facts_never_raise(llama, monkeypatch):
    monkeypatch.setattr(llama, "_props_for", lambda mid: (_ for _ in ()).throw(RuntimeError("down")), raising=False)
    monkeypatch.setattr(llama, "_llama_server_cmdline", lambda pid: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(llama, "_gpu_label", lambda: (_ for _ in ()).throw(RuntimeError("x")), raising=False)
    monkeypatch.setattr(llama, "_bench_live_server", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    facts = llama._conditions_facts("org/m")
    assert facts["backend"] == "llama.cpp" and facts["context"] is None and facts["model_path"] is None
    assert facts["server_cmd"] is None and facts["gpu_label"] is None and facts["quantization"] is None


def test_llama_props_probe_passes_autoload_0(llama, monkeypatch):
    seen = {}

    class _R:
        def json(self):
            return {"build_info": "b1"}

    def _get(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return _R()
    monkeypatch.setattr(llama.requests, "get", _get, raising=False)
    assert llama._props_for("org/m") == {"build_info": "b1"}
    assert seen["url"].endswith("/props") and seen["params"] == {"model": "org/m", "autoload": "0"}


_OTHER_PROPS = {"model_path": "/models/other-Q8_0.gguf", "build_info": "b1234-abc", "model_alias": "org/other",
                "default_generation_settings": {"n_ctx": 4096, "params": {"temperature": 0.1}}}


def test_llama_facts_ignore_props_of_another_loaded_model(llama, monkeypatch, tmp_path):
    snap = tmp_path / "models--org--m" / "snapshots" / "abc"; snap.mkdir(parents=True)
    (snap / "m-Q4_K_M.gguf").write_bytes(b"g")
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: tmp_path)
    monkeypatch.setattr(llama, "_props_for", lambda mid: dict(_OTHER_PROPS), raising=False)
    monkeypatch.setitem(llama._llama_loaded, "last", "org/other")
    calls = []
    monkeypatch.setattr(llama, "_bench_live_server", lambda: calls.append(1) or {"loaded_id": "org/other"})
    monkeypatch.setattr(llama, "_llama_server_cmdline", lambda pid: None)
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: None)
    monkeypatch.setattr(llama, "_gpu_label", lambda: (None, None), raising=False)
    facts = llama._conditions_facts("org/m")
    assert facts["model_path"] == str(snap / "m-Q4_K_M.gguf") and facts["quantization"] == "Q4_K_M"
    assert facts["context"] is None and facts["sampling"] is None and facts["backend_build"] == "b1234-abc"
    assert len(calls) == 1


def test_llama_facts_use_props_when_alias_matches(llama, monkeypatch):
    monkeypatch.setattr(llama, "_props_for", lambda mid: dict(_OTHER_PROPS), raising=False)
    monkeypatch.setitem(llama._llama_loaded, "last", None)
    monkeypatch.setattr(llama, "_bench_live_server", lambda: (_ for _ in ()).throw(AssertionError("no server call")))
    monkeypatch.setattr(llama, "_llama_server_cmdline", lambda pid: None)
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: None)
    monkeypatch.setattr(llama, "_gpu_label", lambda: (None, None), raising=False)
    facts = llama._conditions_facts("org/other")
    assert facts["model_path"] == "/models/other-Q8_0.gguf" and facts["quantization"] == "Q8_0"
    assert facts["context"] == 4096 and facts["sampling"] == {"temperature": 0.1}


def test_llama_offline_facts_come_from_the_bench_command(llama, monkeypatch, tmp_path):
    snap = tmp_path / "models--org--m" / "snapshots" / "abc"; snap.mkdir(parents=True)
    (snap / "m-IQ4_K_M.gguf").write_bytes(b"g")
    (snap / "m-Q4_K_M.gguf").write_bytes(b"g")
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: tmp_path)
    monkeypatch.setattr(llama, "_props_for", lambda mid: (_ for _ in ()).throw(AssertionError("no props call")), raising=False)
    monkeypatch.setattr(llama, "_gpu_label", lambda: ("7900 XTX", "amdgpu 6.12.0"), raising=False)
    monkeypatch.setattr(llama, "_llama_build_last", "b1234-abc")
    cmd = ["/opt/llama/llama-bench", "-ngl", "99", "-hf", "org/m:Q4_K_M", "-o", "jsonl"]
    facts = llama._conditions_facts("org/m", bench_cmd=cmd)
    assert facts["server_cmd"] == " ".join(cmd) and facts["backend_build"] == "b1234-abc"
    assert facts["model_path"] == str(snap / "m-Q4_K_M.gguf") and facts["quantization"] == "Q4_K_M"
    assert facts["context"] is None and facts["sampling"] is None and facts["gpu_label"] == "7900 XTX"
    assert llama._conditions_facts("org/other", bench_cmd=["llama-bench", "-hf", "org/other"])["model_path"] is None


@pytest.mark.parametrize("name,quant", [("m-q8_0.gguf", "Q8_0"), ("m.Q4_K_M-00001-of-00002.gguf", "Q4_K_M"),
                                        ("m-IQ4_XS.gguf", "IQ4_XS"), ("m-bf16.gguf", "BF16"), ("model.gguf", None)])
def test_llama_quant_from_file_name(llama, name, quant):
    assert llama._quant_from_path("/x/" + name) == quant


def test_lms_facts_from_native_row(lms_tools, monkeypatch, tmp_path):
    (tmp_path / "org" / "m").mkdir(parents=True); mf = tmp_path / "org" / "m" / "m-q8_0.gguf"; mf.write_bytes(b"x")
    row = {"key": "org/m", "path": "org/m/m-q8_0.gguf", "quantization": {"name": "Q8_0"},
           "loaded_instances": [{"id": "org/m", "config": {"context_length": 8192, "flash_attention": True, "temperature": 0.7}}]}
    monkeypatch.setattr(lms_tools, "_native_models", lambda: [row])
    monkeypatch.setattr(lms_tools, "_lms_models_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(lms_tools, "_lms_version", lambda: "0.3.20", raising=False)
    facts = lms_tools.conditions_facts("org/m")
    assert facts["backend"] == "LM Studio" and facts["backend_build"] == "lms 0.3.20"
    assert facts["server_cmd"] == "context_length=8192 flash_attention=True temperature=0.7"
    assert facts["model_path"] == str(mf) and facts["quantization"] == "Q8_0" and facts["context"] == 8192
    assert facts["sampling"] == {"temperature": 0.7}


def test_lms_facts_never_raise(lms_tools, monkeypatch):
    monkeypatch.setattr(lms_tools, "_native_models", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(lms_tools, "_lms_version", lambda: None, raising=False)
    facts = lms_tools.conditions_facts("org/m")
    assert facts["backend"] == "LM Studio" and facts["model_id"] == "org/m"
    assert facts["model_path"] is None and facts["backend_build"] is None and facts["server_cmd"] is None


def test_lms_version_parses_and_caches(lms_tools, monkeypatch, tmp_path):
    cmd = tmp_path / "lms"; cmd.write_text("")
    lms_tools._ctx().config.LMS_CMD = str(cmd)
    calls = []

    def _co(argv, **kw):
        calls.append(argv)
        return "lms - LM Studio CLI - v0.3.20\n"
    monkeypatch.setattr(lms_tools, "_LMS_VERSION_CACHE", {}, raising=False)
    monkeypatch.setattr(lms_tools.subprocess, "check_output", _co)
    assert lms_tools._lms_version() == "0.3.20" and lms_tools._lms_version() == "0.3.20"
    assert calls == [[str(cmd), "version"]]


def test_lms_version_caches_a_failure(lms_tools, monkeypatch, tmp_path):
    cmd = tmp_path / "lms"; cmd.write_text("")
    lms_tools._ctx().config.LMS_CMD = str(cmd)
    calls = []

    def _co(argv, **kw):
        calls.append(argv)
        raise lms_tools.subprocess.TimeoutExpired(argv, 3)
    monkeypatch.setattr(lms_tools, "_LMS_VERSION_CACHE", {}, raising=False)
    monkeypatch.setattr(lms_tools.subprocess, "check_output", _co)
    assert lms_tools._lms_version() is None and lms_tools._lms_version() is None
    assert len(calls) == 1


def test_vllm_facts_from_unit_and_snapshot(vllm_tools, monkeypatch, tmp_path):
    snap = tmp_path / "models--org--m" / "snapshots" / "abc"; snap.mkdir(parents=True)
    (snap / "config.json").write_text('{"torch_dtype": "bfloat16", "max_position_embeddings": 4096}')
    (snap / "model.safetensors").write_bytes(b"w" * 10)
    monkeypatch.setattr(vllm_tools, "_unit_cmdline", lambda: "vllm serve org/m --max-model-len 4096 --gpu-memory-utilization 0.9", raising=False)
    monkeypatch.setattr(vllm_tools, "_snapshot_dir", lambda mid: snap, raising=False)
    monkeypatch.setattr(vllm_tools, "_build", lambda: "0.11.0")
    monkeypatch.setattr(vllm_tools, "_max_model_len", lambda: 4096, raising=False)
    facts = vllm_tools.conditions_facts("org/m")
    assert facts["backend"] == "vLLM" and facts["backend_build"] == "vllm 0.11.0"
    assert facts["server_cmd"].startswith("vllm serve org/m") and facts["quantization"] == "bfloat16"
    assert facts["model_path"] == str(snap) and facts["context"] == 4096


def test_vllm_unit_cmdline_and_snapshot_dir(vllm_tools, monkeypatch, tmp_path):
    unit = tmp_path / "vllm.service"
    unit.write_text("[Service]\nExecStart=/opt/v/bin/vllm serve org/m --max-model-len 4096 --enforce-eager\n")
    monkeypatch.setattr(vllm_tools._v, "_vllm_svc_file_path", lambda: str(unit))
    assert vllm_tools._unit_cmdline() == "/opt/v/bin/vllm serve org/m --max-model-len 4096 --enforce-eager"
    repo = tmp_path / "models--org--m"; (repo / "snapshots" / "aaa").mkdir(parents=True)
    (repo / "snapshots" / "bbb").mkdir(); (repo / "refs").mkdir(); (repo / "refs" / "main").write_text("bbb")
    monkeypatch.setattr(vllm_tools._ll, "_hf_cache_root", lambda: tmp_path)
    assert vllm_tools._snapshot_dir("org/m") == repo / "snapshots" / "bbb"
    assert vllm_tools._snapshot_dir("org/missing") is None


def test_vllm_facts_never_raise(vllm_tools, monkeypatch):
    boom = lambda *a: (_ for _ in ()).throw(RuntimeError("x"))  # noqa: E731
    for name in ("_unit_cmdline", "_snapshot_dir", "_build", "_max_model_len", "_unit_args"):
        monkeypatch.setattr(vllm_tools, name, boom, raising=False)
    facts = vllm_tools.conditions_facts("org/m")
    assert facts["backend"] == "vLLM" and facts["model_path"] is None and facts["server_cmd"] is None


def test_run_conditions_route_and_404(llama, monkeypatch):
    monkeypatch.setattr(llama, "_conditions_facts", lambda mid: {"model_id": mid, "backend": "llama.cpp"})
    monkeypatch.setattr(llama, "_llama_model_known", lambda mid: mid == "org/m", raising=False)
    out = llama.llama_run_conditions(model="org/m", authorization="Bearer t")
    assert out["schema"] == 1 and out["model"]["id"] == "org/m" and out["captured_start"]
    with pytest.raises(Exception) as ei:
        llama.llama_run_conditions(model="nope", authorization="Bearer t")
    assert getattr(ei.value, "status_code", None) == 404


def test_llama_model_known_ini_cached_then_server(llama, monkeypatch):
    import configparser
    ini = configparser.ConfigParser(); ini.read_string("[org/a]\nm = x\n[*]\nngl = 99\n")
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: ini)
    monkeypatch.setitem(llama._llama_loaded, "last", "org/cached")
    probes = []
    monkeypatch.setattr(llama, "_bench_live_server", lambda: probes.append(1) or {"loaded_id": "org/b"})
    assert llama._llama_model_known("org/a") and llama._llama_model_known("org/cached") and not probes
    assert llama._llama_model_known("org/b") and len(probes) == 1
    assert not llama._llama_model_known("org/c") and not llama._llama_model_known("*")


def test_lms_route_and_404(lms_tools, monkeypatch):
    fetches = []
    monkeypatch.setattr(lms_tools, "_native_models",
                        lambda: fetches.append(1) or [{"key": "org/m", "loaded_instances": []}])
    monkeypatch.setattr(lms_tools, "_lms_version", lambda: None, raising=False)
    out = lms_tools.lms_run_conditions(model="org/m", authorization="Bearer t")
    assert out["schema"] == 1 and out["backend"]["name"] == "LM Studio" and len(fetches) == 1
    with pytest.raises(Exception) as ei:
        lms_tools.lms_run_conditions(model="nope", authorization="Bearer t")
    assert getattr(ei.value, "status_code", None) == 404


def test_vllm_route_and_404(vllm_tools, monkeypatch):
    monkeypatch.setattr(vllm_tools, "_served_ids", lambda: ["org/m"])
    monkeypatch.setattr(vllm_tools, "conditions_facts", lambda mid: {"model_id": mid, "backend": "vLLM"})
    out = vllm_tools.vllm_run_conditions(model="org/m", authorization="Bearer t")
    assert out["schema"] == 1 and out["backend"]["name"] == "vLLM"
    with pytest.raises(Exception) as ei:
        vllm_tools.vllm_run_conditions(model="nope", authorization="Bearer t")
    assert getattr(ei.value, "status_code", None) == 404


def test_route_tables(mods):
    llama, lms_tools, vllm_tools = mods
    assert ("GET", "/llama/run-conditions", llama.llama_run_conditions) in llama._ROUTES
    assert ("GET", "/lms/run-conditions", lms_tools.lms_run_conditions) in lms_tools._ROUTES
    assert ("GET", "/vllm/run-conditions", vllm_tools.vllm_run_conditions) in vllm_tools._ROUTES
