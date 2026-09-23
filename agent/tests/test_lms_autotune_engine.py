"""#916: LM Studio autotune engine — validation, context bisection, measured sweeps, spec/slots choices, verify."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def lat():
    sys.path.insert(0, str(_AGENT_ROOT))
    pkg = types.ModuleType("providers")
    pkg.__path__ = [str(_AGENT_ROOT / "providers")]
    sys.modules.setdefault("providers", pkg)
    import importlib.util
    for name in ("llama_autotune", "lms_autotune"):
        spec = importlib.util.spec_from_file_location(f"providers.{name}", _AGENT_ROOT / "providers" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"providers.{name}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["providers.lms_autotune"]


BASE = {"context_length": 32768, "flash_attention": True, "eval_batch_size": 2048, "parallel": 1,
        "offload_kv_cache_to_gpu": True, "speculative_draft_mtp": False, "speculative_draft_simple": False,
        "speculative_draft_model": "", "speculative_draft_max_tokens": 3, "speculative_draft_min_tokens": 0,
        "speculative_draft_min_continue_probability": 0}


class FakeBackend:
    """Bigger contexts eat memory; MTP speeds decode; flash is neutral; batch 4096 prefills faster."""

    def __init__(self, loaded=True, base=None, fail_ctx_above=None):
        self.loaded, self.base = loaded, dict(base or BASE)
        self.loads: list = []
        self.fail_ctx_above = fail_ctx_above

    def current(self):
        return {"loaded": self.loaded, "config": dict(self.base) if self.loaded else {}, "max_ctx": 262144,
                "size_bytes": 9_000_000_000, "free_mb": 20000, "missing": False}

    def drafts(self):
        return [{"key": "qwen3.5-9b@q6_k", "type": "llm", "size_bytes": 9_000_000_000},
                {"key": "qwen3.5-0.8b", "type": "llm", "size_bytes": 800_000_000},
                {"key": "gemma-2b", "type": "llm", "size_bytes": 2_000_000_000}]

    def load(self, config, measure):
        self.loads.append((dict(config), dict(measure) if measure else None))
        eff = dict(BASE, **config)
        ctx = int(eff.get("context_length") or 4096)
        if self.fail_ctx_above and ctx > self.fail_ctx_above:
            return {"ok": False, "error": "Insufficient memory"}
        free = 24000 - ctx // 8
        out = {"ok": True, "error": None, "config": eff, "ctx": ctx, "free_mb": free, "load_s": 5.0, "stick": None}
        if measure:
            conc = int(measure.get("concurrency") or 1)
            decode = 50.0
            if eff.get("speculative_draft_mtp"):
                decode = 62.0 if int(eff.get("speculative_draft_max_tokens") or 3) <= 8 else 58.0
            elif eff.get("speculative_draft_simple"):
                decode = 52.0
            decode = decode / (conc ** 0.4)
            prefill = 3000.0 if int(eff.get("eval_batch_size") or 0) >= 4096 else 2000.0
            out["stick"] = {"ok": True, "decode_tps": decode, "prefill_tps": prefill, "agg_tps": decode * conc * 0.9,
                            "accept": 0.8 if eff.get("speculative_draft_mtp") else None, "seconds": 20.0,
                            "completion_tokens": 1000, "error": None}
        return out


def _run(lat, body, backend=None, cancelled=lambda: False):
    req = lat.validate_request(body)
    events: list = []
    backend = backend or FakeBackend()
    done = lat.run_model("qwen3.5-9b@q6_k", req, backend, events.append, cancelled, {"run_id": "r1", "runtime": True})
    return req, done, events, backend


# ── validation ──

def test_validate_defaults_and_bounds(lat):
    req = lat.validate_request({"model_ids": ["qwen3.5-9b@q6_k"]})
    assert req["objective"] == "balanced" and req["mode"] == "tune" and req["dims"]["slots"]["candidates"] == [1, 2, 4, 8]
    with pytest.raises(ValueError):
        lat.validate_request({"model_ids": []})
    with pytest.raises(ValueError):
        lat.validate_request({"model_ids": ["m"], "objective": "quiet"})
    with pytest.raises(ValueError):
        lat.validate_request({"model_ids": ["m"], "dims": {"flash": {"candidates": ["yes"]}}})
    with pytest.raises(ValueError):
        lat.validate_request({"model_ids": ["m"], "dims": {"spec": {"n_min": 8, "n_max": 4}}})
    req = lat.validate_request({"model_ids": ["m"], "mode": "verify", "baseline": {"decode_tps": 40, "ctx": 8192}})
    assert req["baseline_tps"] == 40.0 and all(not d["on"] for k, d in req["dims"].items() if k != "context")


def test_cfg_str_and_typed_round_trip(lat):
    assert lat.cfg_str(True) == "true" and lat.cfg_str(2048) == "2048" and lat.cfg_str(0.75) == "0.75" and lat.cfg_str(None) == ""
    assert lat.cfg_typed("true") is True and lat.cfg_typed("2048") == 2048 and lat.cfg_typed("0.75") == 0.75
    assert lat.cfg_typed("qwen3.5-0.8b") == "qwen3.5-0.8b"


def test_ctx_candidates_and_bisect(lat):
    cands = lat.ctx_candidates(4096, 100000, 12000)
    assert cands[0] == 4096 and cands[-1] == 100000 and 12000 in cands and 131072 not in cands
    best, tried = lat.bisect_largest([1, 2, 3, 4, 5, 6], lambda c: c <= 4)
    assert best == 4 and len(tried) <= 3


def test_find_draft_same_family_and_small(lat):
    rows = FakeBackend().drafts()
    assert lat.find_draft(rows, "qwen3.5-9b@q6_k", 9_000_000_000)["key"] == "qwen3.5-0.8b"
    assert lat.find_draft(rows, "gemma-2b", 2_000_000_000) is None


# ── stages ──

def test_balanced_tune_recommends_mtp_and_keeps_context(lat):
    req, done, events, be = _run(lat, {"model_ids": ["qwen3.5-9b@q6_k"], "objective": "balanced"})
    start = next(e for e in events if e["type"] == "model_start")
    assert start["provider"] == "lms" and start["stages"] == ["context", "flash", "batch", "kvgpu", "spec", "slots", "verify"]
    assert done["ok"] and done["provider"] == "lms" and done["before"]["decode_tps"] == 50.0
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["speculative_draft_mtp"]["value"] is True and keys["speculative_draft_mtp"]["recommended"] == "true"
    assert "context_length" not in keys                     # balanced keeps the loaded context
    assert "flash_attention" not in keys                    # no gain → no change
    assert keys["eval_batch_size"]["value"] == 4096 if "eval_batch_size" in keys else True
    assert done["verify"]["ok"] and done["after"]["agg_tps"] > done["before"]["agg_tps"]
    assert done["after"]["concurrency"] == 2                # balanced: per-request decode ≥ 70 % of one slot
    # every load carried the model's base config merged with the recommendation so far
    assert all("context_length" in cfg for cfg, _m in be.loads)
    stages = {s["stage"]: s for s in done["stages"]}
    assert stages["spec"]["choice"] == "mtp" and stages["verify"]["choice"] == "pass"


def test_batch_stage_uses_prefill_metric(lat):
    req, done, events, be = _run(lat, {"model_ids": ["m-9b"], "objective": "speed",
                                       "dims": {"batch": {"candidates": [512, 2048, 4096]}, "spec": {"on": False},
                                                "slots": {"on": False}, "flash": {"on": False}, "kvgpu": {"on": False}}})
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["eval_batch_size"]["value"] == 4096 and "+50 %" in keys["eval_batch_size"]["evidence"]
    skipped = {e["stage"]: e["reason"] for e in events if e["type"] == "stage_skipped"}
    assert skipped == {"flash": "off", "kvgpu": "off", "spec": "off", "slots": "off"}


def test_fit_bisects_context_to_the_memory_target(lat):
    body = {"model_ids": ["m-9b"], "objective": "fit",
            "dims": {"context": {"target_mb": 8000, "tolerance_mb": 200, "min_ctx": 4096},
                     "flash": {"on": False}, "batch": {"on": False}, "kvgpu": {"on": False},
                     "spec": {"on": False}, "slots": {"on": False}}}
    req, done, events, be = _run(lat, body)
    # free = 24000 - ctx/8 ≥ 7800 → ctx ≤ 129600 → largest ladder step is 65536... plus max_ctx 262144 fails
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["context_length"]["value"] == 65536 and done["after"]["ctx"] == 65536
    ctx_results = [e for e in events if e["type"] == "candidate_result" and e["stage"] == "context"]
    assert ctx_results[0]["value"] == "current" and any(e.get("fits") is False for e in ctx_results)


def test_fit_keeps_current_when_nothing_larger_fits(lat):
    body = {"model_ids": ["m-9b"], "objective": "fit",
            "dims": {"context": {"target_mb": 19000, "tolerance_mb": 100, "min_ctx": 4096},
                     "flash": {"on": False}, "batch": {"on": False}, "kvgpu": {"on": False},
                     "spec": {"on": False}, "slots": {"on": False}}}
    req, done, events, be = _run(lat, body)
    # free ≥ 18900 → ctx ≤ 40800: current 32768 fits, 65536 does not
    assert "context_length" not in {c["key"] for c in done["changes"]} and done["after"]["ctx"] == 32768


def test_other_objectives_search_context_only_when_the_loaded_one_misses_the_target(lat):
    dims = {"flash": {"on": False}, "batch": {"on": False}, "kvgpu": {"on": False}, "spec": {"on": False}, "slots": {"on": False}}
    # loaded 32768 leaves 19904 MB free: a 2048 MB target is met → verified, one load, no change
    req, done, events, be = _run(lat, {"model_ids": ["m-9b"], "objective": "balanced", "dims": dims})
    stage = next(s for s in done["stages"] if s["stage"] == "context")
    assert "context_length" not in {c["key"] for c in done["changes"]} and stage["choice"] == "32768"
    assert len([e for e in events if e["type"] == "candidate_result" and e["stage"] == "context"]) == 1
    assert next(e for e in events if e["type"] == "stage_done" and e["stage"] == "context")["reason"].startswith("kept as loaded")
    # a 19950 ± 10 MB target is missed → the same bisect as Fit picks the largest length that meets it
    dims["context"] = {"target_mb": 19950, "tolerance_mb": 10, "min_ctx": 4096}
    req, done, events, be = _run(lat, {"model_ids": ["m-9b"], "objective": "balanced", "dims": dims})
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["context_length"]["value"] == 16384 and done["after"]["ctx"] == 16384
    assert any(e["type"] == "line" and "under the 19940 MB target" in e["text"] for e in events)


def test_fit_probes_carry_a_traffic_margin(lat):
    # free = 24000 - ctx/8: 131072 leaves 7616, 65536 leaves 15808. Floor 7300 → 131072 fits on the plain reading
    # (7616) but not with the 512 MB traffic margin (7104); the current 32768 (19904) is judged without the margin.
    body = {"model_ids": ["m-9b"], "objective": "fit",
            "dims": {"context": {"target_mb": 7400, "tolerance_mb": 100, "min_ctx": 4096},
                     "flash": {"on": False}, "batch": {"on": False}, "kvgpu": {"on": False},
                     "spec": {"on": False}, "slots": {"on": False}}}
    req, done, events, be = _run(lat, body)
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["context_length"]["value"] == 65536 and "512 MB traffic margin" in keys["context_length"]["evidence"]
    r131 = next(e for e in events if e["type"] == "candidate_result" and e["stage"] == "context" and e["value"] == 131072)
    assert r131["ok"] and r131["fits"] is False and r131["margin_mb"] == 512


def test_serve_picks_max_aggregate_slots(lat):
    req, done, events, be = _run(lat, {"model_ids": ["m-9b"], "objective": "serve",
                                       "dims": {"slots": {"candidates": [1, 2, 4], "min_ctx_per_slot": 4096},
                                                "spec": {"on": False}, "flash": {"on": False}, "batch": {"on": False}, "kvgpu": {"on": False}}})
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["parallel"]["value"] == 4 and done["after"]["concurrency"] == 4
    verify_load = be.loads[-1]
    assert verify_load[0]["parallel"] == 4 and verify_load[1]["concurrency"] == 4


def test_slots_capped_by_min_ctx_per_slot(lat):
    req, done, events, be = _run(lat, {"model_ids": ["m-9b"], "objective": "serve",
                                       "dims": {"slots": {"candidates": [1, 2, 4, 8], "min_ctx_per_slot": 16384},
                                                "spec": {"on": False}, "flash": {"on": False}, "batch": {"on": False}, "kvgpu": {"on": False}}})
    st = next(e for e in events if e["type"] == "stage_start" and e["stage"] == "slots")
    assert st["candidates"] == [1, 2]


def test_spec_draft_model_when_mtp_off(lat):
    req, done, events, be = _run(lat, {"model_ids": ["qwen3.5-9b@q6_k"], "objective": "speed",
                                       "dims": {"spec": {"types": ["draft"], "n_max": 4}, "flash": {"on": False},
                                                "batch": {"on": False}, "kvgpu": {"on": False}, "slots": {"on": False}}})
    keys = {c["key"]: c for c in done["changes"]}
    # draft-simple measured 52 vs 50 plain: below the 5 % floor → none recommended
    assert "speculative_draft_simple" not in keys
    assert any(cfg.get("speculative_draft_model") == "qwen3.5-0.8b" for cfg, _m in be.loads)


def test_spec_window_sweep_prefers_shorter_draft(lat):
    req, done, events, be = _run(lat, {"model_ids": ["m-9b"], "objective": "speed",
                                       "dims": {"spec": {"types": ["mtp"], "n_min": 0, "n_max": 16}, "flash": {"on": False},
                                                "batch": {"on": False}, "kvgpu": {"on": False}, "slots": {"on": False}}})
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["speculative_draft_mtp"]["value"] is True
    assert keys["speculative_draft_max_tokens"]["value"] in (4, 8)


def test_no_runtime_skips_measured_stages_but_fit_still_runs(lat):
    req = lat.validate_request({"model_ids": ["m-9b"], "objective": "fit",
                                "dims": {"context": {"target_mb": 8000, "tolerance_mb": 200}}})
    events: list = []
    be = FakeBackend()
    done = lat.run_model("m-9b", req, be, events.append, lambda: False, {"run_id": "r2", "runtime": False})
    skipped = {e["stage"]: e["reason"] for e in events if e["type"] == "stage_skipped"}
    assert set(skipped) == {"flash", "batch", "kvgpu", "spec", "slots"} and all(v == "runtime" for v in skipped.values())
    assert done["ok"] and {c["key"] for c in done["changes"]} == {"context_length"}
    assert done["verify"]["ok"] and all(m is None for _c, m in be.loads)


def test_verify_drops_slots_then_spec_when_the_set_fails(lat):
    class Flaky(FakeBackend):
        def load(self, config, measure):
            if measure and config.get("parallel", 1) > 1 and config.get("speculative_draft_mtp") and measure.get("limit", 0) > 8:
                return {"ok": False, "error": "Insufficient memory"}
            return super().load(config, measure)
    req, done, events, be = _run(lat, {"model_ids": ["m-9b"], "objective": "serve",
                                       "dims": {"slots": {"candidates": [1, 2], "min_ctx_per_slot": 4096},
                                                "flash": {"on": False}, "batch": {"on": False}, "kvgpu": {"on": False}}}, backend=Flaky())
    assert done["verify"]["ok"] and done["verify"]["dropped"] == ["slots"]
    assert "parallel" not in {c["key"] for c in done["changes"]}


def test_first_load_failure_stops_the_run(lat):
    class Dead(FakeBackend):
        def load(self, config, measure):
            return {"ok": False, "error": "Model not found"}
    req, done, events, be = _run(lat, {"model_ids": ["m-9b"]}, backend=Dead())
    assert not done["ok"] and done["stop_reason"] == "Model not found" and done["changes"] == []


def test_cancel_mid_run(lat):
    flag = {"n": 0}

    def cancelled():
        flag["n"] += 1
        return flag["n"] > 3
    req, done, events, be = _run(lat, {"model_ids": ["m-9b"]}, cancelled=cancelled)
    assert done["cancelled"] and not done["ok"] and done["stop_reason"] == "cancelled"


def test_verify_mode_regression_against_recorded_baseline(lat):
    req = lat.validate_request({"model_ids": ["m-9b"], "mode": "verify", "baseline_tps": 80.0,
                                "baseline": {"decode_tps": 80.0, "ctx": 32768}})
    events: list = []
    done = lat.run_model("m-9b", req, FakeBackend(), events.append, lambda: False, {"run_id": "r3", "runtime": True})
    assert done["mode"] == "verify" and done["ok"] and done["regressed"] is True
    assert done["before"]["decode_tps"] == 80.0 and done["after"]["decode_tps"] == 50.0
    assert [e["stage"] for e in events if e["type"] == "stage_start"] == ["verify"]


def test_unloaded_model_tunes_from_lm_studio_defaults(lat):
    be = FakeBackend(loaded=False)
    req, done, events, _ = _run(lat, {"model_ids": ["m-9b"], "objective": "speed",
                                      "dims": {"flash": {"on": False}, "batch": {"on": False}, "kvgpu": {"on": False},
                                               "spec": {"on": False}, "slots": {"on": False}}}, backend=be)
    assert done["ok"] and done["was_loaded"] is False and be.loads[0][0] == {}
    assert done["base_config"]["context_length"] == 32768


def test_ledger_summary_marks_provider(lat):
    req, done, events, be = _run(lat, {"model_ids": ["m-9b"], "objective": "speed",
                                       "dims": {"flash": {"on": False}, "batch": {"on": False}, "kvgpu": {"on": False},
                                                "spec": {"on": False}, "slots": {"on": False}}})
    s = lat.ledger_summary(done)
    assert s["provider"] == "lms" and s["llama_build"] is None and s["ctx_size"] == 32768 and s["verify_ok"] is True
