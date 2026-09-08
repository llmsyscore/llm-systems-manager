"""#879: speed-bench live runner — pure helpers (no agent deps)."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bl():
    spec = importlib.util.spec_from_file_location(
        "llama_bench_live", _AGENT_ROOT / "providers" / "llama_bench_live.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["llama_bench_live"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_validate_defaults_and_limits(bl):
    req = bl.validate_run_request({"model_id": "org/m:Q4", "bench": "qualitative"})
    assert req == {"model_id": "org/m:Q4", "bench": "qualitative", "categories": "all",
                   "osl": 1024, "limit": 8, "concurrency": [1], "timeout_s": 600,
                   "extra_inputs": {"temperature": 0}, "baseline_run_id": None}
    req = bl.validate_run_request({"model_id": "m", "bench": "throughput_8k", "categories": ["a", "b"],
                                   "osl": 256, "limit": 4, "concurrency": [1, 2, 4], "timeout_s": 30,
                                   "extra_inputs": {"top_p": 0.9}, "baseline_run_id": "abc"})
    assert req["categories"] == ["a", "b"] and req["concurrency"] == [1, 2, 4]
    assert req["baseline_run_id"] == "abc" and req["extra_inputs"] == {"top_p": 0.9}


@pytest.mark.parametrize("body", [
    {"bench": "qualitative"},
    {"model_id": "m", "bench": "nope"},
    {"model_id": "m", "bench": "qualitative", "osl": 8},
    {"model_id": "m", "bench": "qualitative", "limit": 0},
    {"model_id": "m", "bench": "qualitative", "concurrency": []},
    {"model_id": "m", "bench": "qualitative", "concurrency": [1, 2, 3, 4, 5, 6, 7, 8, 9]},
    {"model_id": "m", "bench": "qualitative", "concurrency": [65]},
    {"model_id": "m", "bench": "qualitative", "timeout_s": 5},
    {"model_id": "m", "bench": "qualitative", "extra_inputs": [1]},
    {"model_id": "m", "bench": "qualitative", "extra_inputs": {"x": "y" * 3000}},
    {"model_id": "m", "bench": "qualitative", "categories": ["ok", "bad;name"]},
    {"model_id": "m", "bench": "qualitative", "limit": True},
    {"model_id": "m", "bench": "qualitative", "concurrency": [True]},
    {"model_id": "m", "bench": "qualitative", "osl": "abc"},
])
def test_validate_rejects(bl, body):
    with pytest.raises(ValueError):
        bl.validate_run_request(body)


def test_build_cmd(bl):
    req = bl.validate_run_request({"model_id": "org/m:Q4", "bench": "qualitative",
                                   "categories": ["coding", "math"], "osl": 512, "limit": 6,
                                   "concurrency": [1, 4], "timeout_s": 120,
                                   "extra_inputs": {"temperature": 0.2}})
    cmd = bl.build_cmd("/v/bin/python", "/s/speed_bench.py", "http://127.0.0.1:9931", req, 4, "/r/level-4.json")
    assert cmd == ["/v/bin/python", "/s/speed_bench.py", "--url", "http://127.0.0.1:9931",
                   "--model", "org/m:Q4", "--bench", "qualitative", "--category", "coding,math",
                   "--osl", "512", "--limit", "6", "--concurrency", "4", "--timeout", "120",
                   "--extra-inputs", '{"temperature": 0.2}', "--output", "/r/level-4.json"]


def test_parse_progress(bl):
    assert bl.parse_progress("speed_bench:  31%|███       | 15/48 [00:40<01:28,  2.7s/sample]") == (15, 48)
    assert bl.parse_progress("speed_bench: loaded 48 samples from bench=qualitative category=all") is None
    assert bl.parse_progress("") is None
    assert bl.parse_progress("Retrying request 3/10 [after error]") is None


def test_level_summary_maths(bl):
    payload = {
        "summary": [
            {"category": "coding", "requests": 2, "turns": 2, "failed": 0, "avg_prompt_t_s": 2000.0,
             "avg_pred_t_s": 100.0, "avg_latency": 4.0, "draft_n": 100, "accepted": 70, "accept_rate": 0.7},
            {"category": "math", "requests": 1, "turns": 1, "failed": 1, "avg_prompt_t_s": 1000.0,
             "avg_pred_t_s": 40.0, "avg_latency": 2.0, "draft_n": 0, "accepted": 0, "accept_rate": None},
            {"category": "overall", "requests": 3, "turns": 3, "failed": 1, "avg_prompt_t_s": 1666.67,
             "avg_pred_t_s": 80.0, "avg_latency": 3.33, "draft_n": 100, "accepted": 70, "accept_rate": 0.7},
        ],
        "results": [
            {"ok": True, "completion_tokens": 300, "latency_s": 3.0},
            {"ok": True, "completion_tokens": 300, "latency_s": 5.0},
            {"ok": True, "completion_tokens": 100, "latency_s": 2.0},
            {"ok": False, "completion_tokens": 0, "latency_s": 0.0},
        ],
    }
    out = bl.level_summary(payload, wall_s=7.0)
    assert [r["category"] for r in out["rows"]] == ["coding", "math"]
    a = out["all"]
    assert a["requests"] == 3 and a["failed"] == 1 and a["completion_tokens"] == 700
    assert a["pred_tps"] == 80.0 and a["prompt_tps"] == pytest.approx(1666.67)
    assert a["latency_s"] == pytest.approx(3.33) and a["accept_rate"] == 0.7
    assert a["agg_pred_tps"] == 100.0


def test_level_summary_handles_missing(bl):
    out = bl.level_summary({"summary": [], "results": []}, wall_s=0.0)
    assert out["rows"] == [] and out["all"]["agg_pred_tps"] is None and out["all"]["accept_rate"] is None


def test_power_integrator(bl):
    readings = iter([(100.0, "psu"), (300.0, "psu"), (None, None)])
    p = bl.PowerIntegrator(lambda: next(readings, (None, None)), interval_s=0.01)
    p.start()
    time.sleep(0.05)
    wh, src = p.stop()
    assert src == "psu" and wh is not None and wh > 0


def test_power_integrator_no_readings(bl):
    p = bl.PowerIntegrator(lambda: (None, None), interval_s=0.01)
    p.start(); time.sleep(0.03)
    assert p.stop() == (None, None)


def test_runtime_and_script_resolution(bl, tmp_path):
    inst = tmp_path / "inst"
    assert bl.runtime_python(str(inst), "/opt/py") == ("/opt/py", "override")
    assert bl.runtime_python(str(inst), "") == (None, "missing")
    py = bl.bench_dir(str(inst)) / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True); py.write_text("")
    assert bl.runtime_python(str(inst), "") == (str(py), "venv")
    src_root = tmp_path / "src"; (src_root / "bench").mkdir(parents=True)
    assert bl.script_path(str(inst), src_root)[0] is None
    (src_root / "bench" / "speed_bench.py").write_text("print('x')\n")
    assert bl.script_path(str(inst), src_root)[0] is None  # wrong hash
    good = bl.bench_dir(str(inst)) / "speed_bench.py"
    good.write_bytes(b"")  # sha of empty differs too
    assert bl.script_path(str(inst), src_root)[0] is None
    shutil.copy(_AGENT_ROOT / "bench" / "speed_bench.py", src_root / "bench" / "speed_bench.py")
    assert bl.script_path(str(inst), src_root)[1] == "ok"


def test_marker_roundtrip(bl, tmp_path):
    assert bl.read_marker(str(tmp_path)) == {}
    bl.write_marker(str(tmp_path), "qualitative", ["coding", "math"])
    m = bl.read_marker(str(tmp_path))
    assert m["qualitative"]["categories"] == ["coding", "math"] and m["qualitative"]["ts"] > 0
    bl.write_marker(str(tmp_path), "qualitative", ["qa"])
    assert bl.read_marker(str(tmp_path))["qualitative"]["categories"] == ["coding", "math", "qa"]


def test_run_level_subprocess_reports_script_elapsed(bl):
    prog = ("import sys; print('speed_bench: loaded 2 samples'); "
            "sys.stderr.write('speed_bench:  50%|#####     | 1/2 [00:01<00:01,  1.0s/sample]\\r'); "
            "print('Summary (elapsed=1.25s)')")
    cmd = [sys.executable, "-c", prog]
    events = []
    out = bl.run_level_subprocess(cmd, dict(os.environ), events.append, "org/m:Q4", 1,
                                  threading.Event(), lambda p: None, lambda: None)
    assert out == (0, False, 1.25)
    prog = [e for e in events if e["type"] == "progress"]
    assert len(prog) == 1 and prog[0]["done"] == 1 and prog[0]["total"] == 2
    assert any(e["type"] == "line" and "loaded 2 samples" in e["text"] for e in events)


def test_level_summary_tolerates_non_numeric_counts(bl):
    payload = {"summary": [{"category": "overall", "requests": "n/a", "failed": None,
                            "avg_pred_t_s": 10.0}],
               "results": [{"ok": True, "completion_tokens": 100}, {"ok": False}]}
    all_ = bl.level_summary(payload, 10.0)["all"]
    assert all_["requests"] == 1 and all_["failed"] == 1 and all_["pred_tps"] == 10.0
