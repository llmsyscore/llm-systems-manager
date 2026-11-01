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
    mod._orig_bench_tool_path = mod._bench_tool_path
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


def test_malformed_rows_skipped_energy_stopped_once(llama, monkeypatch, tmp_path):
    mod, events, ledger = llama

    class _CountingEnergy(_FakeEnergy):
        stop_calls = 0
        def stop(self):
            type(self).stop_calls += 1
            return super().stop()

    monkeypatch.setattr(mod._bl, "PowerIntegrator", _CountingEnergy)
    rows = [
        {"n_prompt": "x", "n_gen": 128, "avg_ts": 40.0,
         "type_k": "q8_0", "type_v": "q8_0", "samples_ns": [1, 2, 3, 4, 5]},
        {"n_prompt": 0, "n_gen": 128, "avg_ts": 40.0,
         "type_k": "q8_0", "type_v": "q8_0", "samples_ns": 5},
    ]
    p = tmp_path / "llama-bench-malformed"
    body = "#!/usr/bin/env python3\nimport json\n" + "".join(
        f"print(json.dumps({json.dumps(r)}))\n" for r in rows)
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(mod, "_bench_tool_path", lambda tool: (str(p), True))
    mod._bench_run_one("m1", "llama-bench", [], dict(os.environ))
    done = [e for e in events if e["type"] == "model_done"][-1]
    assert done["ok"] is True
    # only the second row (samples_ns non-list -> 1 rep) counts; the first (bad n_prompt) is skipped
    assert done["tokens"] == 128
    assert _CountingEnergy.stop_calls == 1


def test_model_done_carries_conditions(llama, monkeypatch):
    mod, events, ledger = llama
    kw: list = []
    monkeypatch.setattr(mod._shared, "post_tool_run", lambda *a, **k: kw.append(k))
    monkeypatch.setattr(mod, "_conditions_facts", lambda mid, bench_cmd=None: {"model_id": mid, "server_cmd": " ".join(map(str, bench_cmd or []))})
    monkeypatch.setattr(mod.run_conditions, "samples", lambda: (None, None))
    mod._bench_run_one("m1", "llama-bench", [], dict(os.environ))
    cond = [e for e in events if e["type"] == "model_done"][-1]["conditions"]
    assert cond["captured_start"] and cond["captured_end"] and cond["model"]["id"] == "m1"
    assert kw[-1]["conditions"] is cond
    assert "llama-bench" in cond["backend"]["server_cmd"] and "-hf" in cond["backend"]["server_cmd"]


def _builds_env(mod, monkeypatch, tmp_path, names=("b1", "b2")):
    """Two listed builds; _bench_run_one is replaced by a recorder of (tool path, model)."""
    rows, paths = [], []
    for n in names:
        d = tmp_path / n / "bin"
        d.mkdir(parents=True)
        (d / "llama-server").write_text("")
        (d / "llama-bench").write_text("")
        p = os.path.realpath(d / "llama-server")
        paths.append(p)
        rows.append({"path": p, "ok": True, "name": n})
    monkeypatch.setitem(mod.llama_inventory._state, "last", {"ok": True, "builds": rows})
    monkeypatch.setattr(mod, "_perf_job_set", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_require_ctx", lambda: types.SimpleNamespace(
        config=types.SimpleNamespace(LLAMA_BIN=paths[0])))
    return paths


def test_offline_runs_each_build_in_order(llama, monkeypatch, tmp_path):
    mod, events, _ = llama
    paths = _builds_env(mod, monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(mod, "_bench_run_one", lambda mid, tool, sw, env, bin_path=None, store_id=None: calls.append((bin_path, mid)))
    mod._bench_run_all(["m1", "m2"], "llama-bench", [], builds=paths)
    assert calls == [(paths[0], "m1"), (paths[0], "m2"), (paths[1], "m1"), (paths[1], "m2")]
    starts = [e for e in events if e["type"] == "build_start"]
    assert [(s["build"], s["index"], s["total"]) for s in starts] == [("b1", 0, 2), ("b2", 1, 2)]
    assert [e for e in events if e["type"] == "done"][-1]["count"] == 4


def test_offline_tool_path_only_beside_build(llama, tmp_path):
    mod, _, _ = llama
    fn = mod._orig_bench_tool_path
    d = tmp_path / "x"
    d.mkdir()
    (d / "llama-server").write_text("")
    path, found = fn("llama-bench", str(d / "llama-server"))
    assert path == str(d / "llama-bench") and found is False


def test_offline_no_builds_uses_llama_bin(llama, monkeypatch, tmp_path):
    mod, events, _ = llama
    _builds_env(mod, monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(mod, "_bench_run_one", lambda *a, **k: calls.append((a, k)))
    mod._bench_run_all(["m1"], "llama-bench", [])
    assert len(calls) == 1 and calls[0][1] == {}
    assert not [e for e in events if e["type"] == "build_start"]
    assert [e for e in events if e["type"] == "done"][-1]["count"] == 1


def test_offline_unlisted_build_400(llama, monkeypatch, tmp_path):
    mod, _, _ = llama
    _builds_env(mod, monkeypatch, tmp_path)
    with pytest.raises(mod.HTTPException) as ei:
        mod._validate_builds([str(tmp_path / "nope" / "llama-server")])
    assert ei.value.status_code == 400 and "build not in the list" in ei.value.detail
    with pytest.raises(mod.HTTPException) as ei:
        mod._validate_builds("x")
    assert ei.value.detail == "builds must be a list"


def test_validate_builds_dedup_and_cap(llama, monkeypatch, tmp_path):
    mod, _, _ = llama
    paths = _builds_env(mod, monkeypatch, tmp_path)
    assert mod._validate_builds([paths[1], paths[0], paths[1]]) == [paths[1], paths[0]]
    assert mod._validate_builds([]) == []
    with pytest.raises(mod.HTTPException):
        mod._validate_builds([paths[0]] * 3 + [f"/x/{i}" for i in range(9)])


def test_offline_env_uses_build_dir(llama, monkeypatch, tmp_path):
    mod, _, _ = llama
    paths = _builds_env(mod, monkeypatch, tmp_path)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/old")
    env, cwd = mod._build_env(paths[1])
    d = os.path.dirname(paths[1])
    assert env["LD_LIBRARY_PATH"] == f"{d}:/old" and cwd == d


def test_offline_model_done_carries_build(llama, monkeypatch, tmp_path):
    mod, events, _ = llama
    paths = _builds_env(mod, monkeypatch, tmp_path)
    shutil_src = _fake_tool(tmp_path)
    monkeypatch.setattr(mod, "_bench_tool_path", lambda tool, bin_path=None: (str(shutil_src), True))
    seen = {}
    real_popen = mod.subprocess.Popen
    def _popen(cmd, **kw):
        seen.update(kw)
        return real_popen(cmd, **kw)
    monkeypatch.setattr(mod.subprocess, "Popen", _popen)
    mod._bench_run_all(["m1"], "llama-bench", [], builds=[paths[1]])
    done = [e for e in events if e["type"] == "model_done"][-1]
    assert done["build"] == "b2" and done["build_path"] == paths[1]
    assert seen["cwd"] == os.path.dirname(paths[1])
    assert seen["env"]["LD_LIBRARY_PATH"].startswith(os.path.dirname(paths[1]))


def test_offline_cancel_stops_between_builds(llama, monkeypatch, tmp_path):
    mod, events, _ = llama
    paths = _builds_env(mod, monkeypatch, tmp_path)
    calls = []
    def _run(mid, tool, sw, env, bin_path=None, store_id=None):
        calls.append(bin_path)
        mod._bench_cancel_event.set()
    monkeypatch.setattr(mod, "_bench_run_one", _run)
    mod._bench_run_all(["m1"], "llama-bench", [], builds=paths)
    assert calls == [paths[0]]
    assert [e for e in events if e["type"] == "done"][-1]["cancelled"] is True


def test_offline_builds_ledger_rows_distinct(llama, monkeypatch, tmp_path):
    mod, events, ledger = llama
    paths = _builds_env(mod, monkeypatch, tmp_path)
    tool = _fake_tool(tmp_path)
    monkeypatch.setattr(mod, "_bench_tool_path", lambda t, bin_path=None: (str(tool), True))
    mod._bench_replay.start_run("r1")
    mod._bench_run_all(["m1"], "llama-bench", [], builds=paths)
    assert [(a[3], a[4], a[6]["build_name"]) for a in ledger] == [("r1", "m1", "b1"), ("r1-2", "m1", "b2")]
    done = [e for e in events if e["type"] == "model_done"]
    assert [(d["run_id"], d["stored_run_id"], d["build"]) for d in done] == [("r1", "r1", "b1"), ("r1", "r1-2", "b2")]
    starts = [e for e in events if e["type"] == "model_start"]
    assert [s["build"] for s in starts] == ["b1", "b2"]
    assert all(e.get("build") for e in events if e["type"] == "result")


def test_offline_single_build_ledger_keeps_run_id(llama):
    mod, events, ledger = llama
    mod._bench_replay.start_run("r2")
    mod._bench_run_one("m1", "llama-bench", [], dict(os.environ))
    assert ledger[-1][3] == "r2" and "build_name" not in ledger[-1][6]
    assert "stored_run_id" not in [e for e in events if e["type"] == "model_done"][-1]
