"""#1302: run conditions recorded beside every benchmark result — pure helpers."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]


def _stub(name: str, **attrs) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


@pytest.fixture(scope="module")
def rc():
    if "collectors" not in sys.modules:
        pkg = types.ModuleType("collectors")
        pkg.__path__ = [str(_AGENT_ROOT / "collectors")]
        sys.modules["collectors"] = pkg
    _stub("collectors.gpu", collect_gpu=lambda: {})
    _stub("collectors.system", collect_system_metrics=lambda: {}, read_cpu_name=lambda: None)
    spec = importlib.util.spec_from_file_location(
        "run_conditions", _AGENT_ROOT / "providers" / "run_conditions.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_conditions"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_file_identity_small_file_and_shards(rc, tmp_path):
    f = tmp_path / "m-00001-of-00002.gguf"; f.write_bytes(b"x" * 1000)
    (tmp_path / "m-00002-of-00002.gguf").write_bytes(b"y")
    ident = rc.file_identity(f)
    assert ident["file"] == "m-00001-of-00002.gguf" and ident["size_bytes"] == 1000 and ident["shards"] == 2
    assert ident["hash"].startswith("ht-sha256:") and len(ident["hash"]) == len("ht-sha256:") + 12
    assert rc.file_identity(tmp_path / "missing.gguf") is None
    assert rc.file_identity(tmp_path) is None


def test_file_identity_changes_with_content_and_size(rc, tmp_path):
    a = tmp_path / "a.gguf"; a.write_bytes(b"a" * (9 << 20))
    h1 = rc.file_identity(a)["hash"]
    a.write_bytes(b"a" * (9 << 20) + b"b")
    assert rc.file_identity(a)["hash"] != h1


def test_host_facts_shape(rc, tmp_path, monkeypatch):
    rc._reset_cache()
    monkeypatch.setattr(rc, "_OS_RELEASE", tmp_path / "os-release")
    (tmp_path / "os-release").write_text('NAME="Debian"\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    monkeypatch.setattr(rc, "_cpu_name", lambda: "AMD Ryzen 7 9700X")
    monkeypatch.setattr(rc, "_cpu_counts", lambda: (8, 16))
    monkeypatch.setattr(rc, "_ram_total", lambda: 64 << 30)
    monkeypatch.setattr(rc.platform, "release", lambda: "6.12.0-amd64")
    monkeypatch.setattr(rc.sys, "platform", "linux")
    assert rc.host_facts() == {"cpu": "AMD Ryzen 7 9700X", "cpu_cores": 8, "cpu_threads": 16, "ram_bytes": 64 << 30,
                               "os": {"name": "Debian GNU/Linux 13 (trixie)", "kernel": "6.12.0-amd64"}}


def test_snapshot_maps_amd_nvidia_and_system(rc):
    amd = {"temperature_c": 41.0, "temperature_junction_c": 55.0, "sclk_mhz": 2100, "mclk_mhz": 1250,
           "power_watts": 48.2, "power_cap_watts": 300.0, "performance_level": "auto"}
    sysm = {"cpu_governor": "performance", "ram": {"available_bytes": 51200000000}}
    assert rc.snapshot_from(amd, sysm) == {"temp_c": 41, "junction_c": 55, "sclk_mhz": 2100, "mclk_mhz": 1250, "power_w": 48,
                                           "power_cap_w": 300, "performance_level": "auto", "cpu_governor": "performance",
                                           "ram_available_bytes": 51200000000}
    nv = {"temperature_c": 60.0, "temperature_junction_c": None, "sclk_mhz": None, "mclk_mhz": None,
          "power_watts": 120.0, "power_cap_watts": 450.0, "performance_level": None}
    assert rc.snapshot_from(nv, None)["junction_c"] is None and rc.snapshot_from(nv, None)["power_cap_w"] == 450
    assert rc.snapshot_from(None, None) is None
    assert rc.snapshot_from({"gpus": [amd, amd], **amd}, None)["cards"] == 2


def test_start_finish_attach_shape(rc, monkeypatch):
    monkeypatch.setattr(rc, "host_facts", lambda: {"cpu": "c", "cpu_cores": 1, "cpu_threads": 2, "ram_bytes": 1, "os": {"name": "o", "kernel": "k"}})
    facts = {"gpu_label": "7900 XTX", "driver": "amdgpu 6.12.0", "backend": "llama.cpp", "backend_build": "b1234-abc",
             "server_cmd": "llama-server -m x.gguf", "model_id": "org/m", "model_path": None, "quantization": "Q4_K_M",
             "context": 32768, "sampling": {"temperature": 0.8}}
    c = rc.start(facts, {"temperature_c": 40.0}, None, now=lambda: "2026-10-18T20:00:00Z")
    assert c["schema"] == 1 and c["captured_start"] == "2026-10-18T20:00:00Z" and "captured_end" not in c
    assert c["hardware"] == {"gpu": "7900 XTX", "gpu_count": 1, "driver": "amdgpu 6.12.0", "cpu": "c", "cpu_cores": 1, "cpu_threads": 2, "ram_bytes": 1}
    assert c["backend"] == {"name": "llama.cpp", "build": "b1234-abc", "server_cmd": "llama-server -m x.gguf"}
    assert c["model"] == {"id": "org/m", "file": None, "size_bytes": None, "hash": None, "quantization": "Q4_K_M", "shards": None}
    assert c["snapshot"]["start"]["temp_c"] == 40 and "end" not in c["snapshot"]
    monkeypatch.setattr(rc, "samples", lambda: ({"temperature_c": 70.0}, None))
    doc = rc.attach({"conditions": c}, {"type": "model_done"})
    assert doc["conditions"]["snapshot"]["end"]["temp_c"] == 70 and doc["conditions"]["captured_end"]
    assert rc.attach({}, {"type": "model_done"}) == {"type": "model_done"}


def test_start_tolerates_missing_facts(rc):
    c = rc.start({}, None, None)
    assert c["hardware"]["gpu"] is None and c["model"]["id"] is None and c["snapshot"] == {"start": None}


def test_compact_caps_and_drops_in_order(rc):
    c = rc.start({"server_cmd": "x" * 5000, "sampling": {"temperature": 1}}, None, None)
    c = rc.finish(c, None, None)
    c["snapshot"] = {"start": {"temp_c": 1}, "end": {"temp_c": 2}}
    out = rc.compact(c)
    assert len(out["backend"]["server_cmd"]) == rc.SERVER_CMD_MAX
    big = dict(c, sampling={"k%d" % i: "v" * 100 for i in range(80)})
    out = rc.compact(big)
    assert "sampling" not in out and len(json.dumps(out)) <= rc.COND_MAX_BYTES
    assert rc.compact(None) is None


def test_dir_identity_snapshot(rc, tmp_path):
    snap = tmp_path / "abc"; snap.mkdir()
    (snap / "config.json").write_text('{"torch_dtype": "bfloat16"}')
    (snap / "model-00001-of-00002.safetensors").write_bytes(b"w" * 10)
    (snap / "model-00002-of-00002.safetensors").write_bytes(b"w" * 5)
    (snap / "tokenizer.json").write_bytes(b"t" * 100)
    ident = rc.dir_identity(snap)
    assert ident["file"] == "abc" and ident["size_bytes"] == 15 and ident["shards"] == 2
    assert ident["hash"].startswith("ht-sha256:") and len(ident["hash"]) == len("ht-sha256:") + 12
    assert rc.dir_identity(tmp_path / "missing") is None
    assert rc.dir_identity(snap / "config.json") is None
    (snap / "config.json").unlink()
    assert rc.dir_identity(snap) is None


def test_start_falls_back_to_dir_identity(rc, tmp_path):
    snap = tmp_path / "abc"; snap.mkdir()
    (snap / "config.json").write_text("{}")
    (snap / "pytorch_model.bin").write_bytes(b"b" * 7)
    cond = rc.start({"model_id": "org/m", "model_path": str(snap)}, now=lambda: "t")
    assert cond["model"]["file"] == "abc" and cond["model"]["size_bytes"] == 7 and cond["model"]["shards"] == 1


def test_gpu_label_from(rc):
    assert rc.gpu_label_from({"name": "RX 7900 XTX", "driver": "amdgpu 6.12"}) == ("RX 7900 XTX", "amdgpu 6.12")
    two = {"name": "RTX 4090", "driver": "550", "gpus": [{"name": "RTX 4090"}, {"name": "RTX 4090"}]}
    assert rc.gpu_label_from(two) == ("2× RTX 4090", "550")
    mixed = {"name": "RTX 4090", "gpus": [{"name": "RTX 4090"}, {"name": "RTX 3090"}]}
    assert rc.gpu_label_from(mixed) == ("RTX 4090 + RTX 3090", None)
    assert rc.gpu_label_from(None) == (None, None) and rc.gpu_label_from({}) == (None, None)


def test_dir_identity_hf_cache_symlinks(rc, tmp_path):
    repo = tmp_path / "models--org--m"; blobs = repo / "blobs"; snap = repo / "snapshots" / "rev1"
    blobs.mkdir(parents=True); snap.mkdir(parents=True)
    (blobs / "aaa").write_bytes(b"w" * 40); (blobs / "bbb").write_bytes(b"w" * 2)
    (blobs / "ccc").write_text('{"torch_dtype": "float16"}')
    (snap / "model-00001-of-00002.safetensors").symlink_to("../../blobs/aaa")
    (snap / "model-00002-of-00002.safetensors").symlink_to("../../blobs/bbb")
    (snap / "config.json").symlink_to("../../blobs/ccc")
    (snap / "extra.bin").symlink_to("../../blobs/missing")
    ident = rc.dir_identity(snap)
    assert ident["file"] == "rev1" and ident["size_bytes"] == 42 and ident["shards"] == 2
    assert ident["hash"].startswith("ht-sha256:") and len(ident["hash"]) == len("ht-sha256:") + 12


_SHARED_PY = _AGENT_ROOT / "providers" / "_shared.py"


@pytest.fixture
def shared(rc, monkeypatch):
    import logging
    import re
    if "providers" not in sys.modules:
        pkg = types.ModuleType("providers")
        pkg.__path__ = [str(_AGENT_ROOT / "providers")]
        monkeypatch.setitem(sys.modules, "providers", pkg)
    monkeypatch.setitem(sys.modules, "providers.run_conditions", rc)
    monkeypatch.delattr(sys.modules["providers"], "run_conditions", raising=False)
    m = re.search(r"^def post_tool_run\(.*?(?=^\S|\Z)", _SHARED_PY.read_text(), re.S | re.M)
    ns: dict = {"log": logging.getLogger("test"), "__package__": "providers"}
    exec(compile(m.group(0), "_shared", "exec"), ns)
    return types.SimpleNamespace(post_tool_run=ns["post_tool_run"])


@pytest.fixture
def fake_ctx():
    class _Session:
        def __init__(self):
            self.calls: list = []

        def post(self, url, json=None, timeout=None, headers=None):
            self.calls.append({"url": url, "json": json, "timeout": timeout, "headers": headers})

    return types.SimpleNamespace(config=types.SimpleNamespace(MANAGER_URL="http://m"),
                                 state={"token": "t", "agent_id": "a1"}, post_session=_Session())


def test_post_tool_run_sends_compacted_conditions(shared, fake_ctx):
    shared.post_tool_run(fake_ctx, "benchmark", "llama", "r1", "org/m", True, {"gen_tps": 1.0},
                         conditions={"schema": 1, "backend": {"server_cmd": "x" * 3000}})
    body = fake_ctx.post_session.calls[-1]["json"]
    assert len(body["conditions"]["backend"]["server_cmd"]) == 2000
    shared.post_tool_run(fake_ctx, "benchmark", "llama", "r1", "org/m", True, {"gen_tps": 1.0})
    assert "conditions" not in fake_ctx.post_session.calls[-1]["json"]


def _raising_collectors(monkeypatch):
    def boom():
        raise AssertionError("collector called")
    monkeypatch.setitem(sys.modules, "collectors.gpu", types.SimpleNamespace(collect_gpu=boom))
    monkeypatch.setitem(sys.modules, "collectors.system", types.SimpleNamespace(
        collect_system_metrics=boom, read_cpu_governor=lambda: "performance"))


def test_samples_read_the_registered_last_sample(rc, monkeypatch):
    _raising_collectors(monkeypatch)
    gpu = {"temperature_c": 61.0, "sclk_mhz": 2400}
    system = {"cpu_governor": "schedutil", "ram": {"available_bytes": 123}, "gpu": gpu}
    monkeypatch.setattr(rc, "_SAMPLE_SOURCE", None)
    rc.set_sample_source(lambda: {"ts": "t", "system": system})
    assert rc.samples() == (gpu, system)
    snap = rc.snapshot_from(*rc.samples())
    assert snap["temp_c"] == 61 and snap["cpu_governor"] == "schedutil" and snap["ram_available_bytes"] == 123


def test_samples_without_source_use_a_cheap_system_sample(rc, monkeypatch):
    _raising_collectors(monkeypatch)
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(available=456)))
    monkeypatch.setattr(rc, "_SAMPLE_SOURCE", None)
    assert rc.samples() == (None, {"cpu_governor": "performance", "ram": {"available_bytes": 456}})
    rc.set_sample_source(lambda: None)
    assert rc.samples() == (None, {"cpu_governor": "performance", "ram": {"available_bytes": 456}})


def test_redact_cmd_hides_secret_flag_values_and_kv_tokens(rc):
    cmd = "llama-server -m x.gguf --api-key s3cret --hf-token=tok -hft t2 --ssl-key-file /k.pem --port 8080 HF_TOKEN=abc MY_PASSWORD=p"
    assert rc.redact_cmd(cmd) == ("llama-server -m x.gguf --api-key *** --hf-token=*** -hft *** --ssl-key-file *** "
                                  "--port 8080 HF_TOKEN=*** MY_PASSWORD=***")
    assert rc.redact_cmd("vllm serve org/m --max-model-len 4096") == "vllm serve org/m --max-model-len 4096"
    assert rc.redact_cmd(None) is None
    c = rc.start({"server_cmd": "llama-server --api-key abc"}, None, None)
    assert c["backend"]["server_cmd"] == "llama-server --api-key ***"


def test_redact_args_redacts_argv_elements_with_spaces_and_quoted_flags(rc):
    argv = ["srv", "--api-key", "a b c", "--alias", "my model", "'--api-key=x'", "--url", "https://user:pw@host/p", "--ssl-key-file=/k.pem"]
    assert rc.redact_args(argv) == ["srv", "--api-key", "***", "--alias", "my model", "***", "--url", "https://***@host/p", "--ssl-key-file=***"]
    assert rc.redact_args(None) == [] and rc.redact_args([1, "x"]) == ["1", "x"]


def test_redact_cmd_splits_on_whitespace_only(rc):
    assert rc.redact_cmd("srv  --port   8080 --api-key=x --url https://u:p@h/x") == "srv --port 8080 --api-key=*** --url https://***@h/x"
    assert rc.redact_cmd("srv '--api-key' tok") == "srv *** ***"
