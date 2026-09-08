"""_bench_run_one: JSONL rows keep KV types, energy integrates Wh per 1k tokens."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_llama():
    from tests._vllm_load import load_vllm
    load_vllm()
    _stub("collectors")
    _stub("collectors.gpu", collect_gpu=lambda *a, **k: {})

    pkg = types.ModuleType("providers")
    pkg.__path__ = [str(_AGENT_ROOT / "providers")]
    sys.modules["providers"] = pkg
    for sub in ("llama_install", "llama_sse", "llama_upgrade"):
        sys.modules[f"providers.{sub}"] = types.ModuleType(f"providers.{sub}")

    spec = importlib.util.spec_from_file_location(
        "providers.llama", _AGENT_ROOT / "providers" / "llama.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["providers.llama"] = mod
    spec.loader.exec_module(mod)
    return mod


_ROWS = [
    {"n_prompt": 512, "n_gen": 0, "n_depth": 0, "n_batch": 2048, "n_ubatch": 512,
     "avg_ts": 1000.0, "type_k": "f16", "type_v": "f16", "samples_ns": [1, 2, 3, 4, 5]},
    {"n_prompt": 0, "n_gen": 128, "n_depth": 0, "n_batch": 2048, "n_ubatch": 512,
     "avg_ts": 40.0, "type_k": "q8_0", "type_v": "q8_0", "samples_ns": [1, 2, 3, 4, 5]},
]


def _fake_tool(tmp_path: Path) -> Path:
    p = tmp_path / "llama-bench"
    body = "#!/usr/bin/env python3\nimport json\n" + "".join(
        f"print(json.dumps({json.dumps(r)}))\n" for r in _ROWS)
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


class _FakeEnergy:
    started = False
    def __init__(self, fn, interval_s=2.0): self.fn = fn
    def start(self): _FakeEnergy.started = True
    def stop(self): return (0.5, "psu")


@pytest.fixture
def llama(monkeypatch, tmp_path):
    mod = _load_llama()
    events: list = []
    ledger: list = []
    monkeypatch.setattr(mod, "_bench_put", lambda ev: events.append(ev))
    monkeypatch.setattr(mod, "_bench_tool_path", lambda tool: (str(_fake_tool(tmp_path)), True))
    monkeypatch.setattr(mod, "_bench_get_hf_arg", lambda mid: "org/repo:Q4_K_M")
    monkeypatch.setattr(mod, "_require_ctx", lambda: types.SimpleNamespace(config=types.SimpleNamespace()))
    monkeypatch.setattr(mod._shared, "post_tool_run", lambda *a, **k: ledger.append(a))
    monkeypatch.setattr(mod._bl, "PowerIntegrator", _FakeEnergy)
    mod._bench_cancel_event.clear()
    return mod, events, ledger


def test_rows_carry_kv_types_and_model_done_has_energy(llama):
    mod, events, ledger = llama
    mod._bench_run_one("m1", "llama-bench", [{"flag": "-ctk", "value": "f16,q8_0"}], dict(os.environ))
    results = [e for e in events if e["type"] == "result"]
    assert [r["type_k"] for r in results] == ["f16", "q8_0"]
    assert [r["type_v"] for r in results] == ["f16", "q8_0"]
    done = [e for e in events if e["type"] == "model_done"][-1]
    assert done["ok"] is True
    assert done["results"][1]["type_k"] == "q8_0"
    # tokens = (512 + 128) * 5 reps
    assert done["tokens"] == 3200
    assert done["energy_wh"] == 0.5 and done["energy_source"] == "psu"
    assert done["wh_per_ktok"] == pytest.approx(0.5 / 3.2)
    assert _FakeEnergy.started is True
    summary = ledger[-1][-1]
    assert summary["wh_per_ktok"] == pytest.approx(0.5 / 3.2)
    assert summary["bench_tool"] == "llama-bench"


def test_no_power_reading_gives_null_energy(llama, monkeypatch):
    mod, events, ledger = llama
    class _NoRead(_FakeEnergy):
        def stop(self): return (None, None)
    monkeypatch.setattr(mod._bl, "PowerIntegrator", _NoRead)
    mod._bench_run_one("m1", "llama-bench", [], dict(os.environ))
    done = [e for e in events if e["type"] == "model_done"][-1]
    assert done["energy_wh"] is None and done["wh_per_ktok"] is None
    assert done["tokens"] == 3200


def test_rows_without_samples_count_one_rep(llama, monkeypatch, tmp_path):
    mod, events, ledger = llama
    row = {k: v for k, v in _ROWS[1].items() if k != "samples_ns"}
    p = tmp_path / "llama-bench-onerep"
    p.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps(" + json.dumps(row) + "))\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(mod, "_bench_tool_path", lambda tool: (str(p), True))
    mod._bench_run_one("m1", "llama-bench", [], dict(os.environ))
    done = [e for e in events if e["type"] == "model_done"][-1]
    assert done["tokens"] == 128
    assert done["results"][0]["type_k"] == "q8_0"
