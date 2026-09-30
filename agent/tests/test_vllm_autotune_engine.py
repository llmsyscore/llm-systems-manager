"""#894: vLLM autotune engine — validation, legacy mapping, arg transforms, stage order per objective,
context probe math, rejected candidates, budget stop, verify, cancel, ledger."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def vat():
    sys.path.insert(0, str(_AGENT_ROOT))
    pkg = types.ModuleType("providers")
    pkg.__path__ = [str(_AGENT_ROOT / "providers")]
    sys.modules.setdefault("providers", pkg)
    import importlib.util
    for name in ("llama_autotune", "lms_autotune", "vllm_autotune"):
        spec = importlib.util.spec_from_file_location(f"providers.{name}", _AGENT_ROOT / "providers" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"providers.{name}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["providers.vllm_autotune"]


MODEL = "org/model-8b"
BASE = {"max_model_len": 8192, "max_num_seqs": 4, "gpu_memory_utilization": 0.9}
ARGS = [{"flag": "--host", "value": "127.0.0.1", "bool": False},
        {"flag": "--max-model-len", "value": "8192", "bool": False},
        {"flag": "--max-num-seqs=4", "value": None, "bool": True},
        {"flag": "--gpu-memory-utilization", "value": "0.9", "bool": False}]


class FakeBackend:
    """KV capacity 230,528 tokens; fp8 fails to start when cpu=True; ngram speeds decode; prefix off is neutral."""

    def __init__(self, served=MODEL, base=None, cpu=False, kv_tokens=230528):
        self.served, self.base, self.cpu, self.kv_tokens = served, dict(base or BASE), cpu, kv_tokens
        self.loads: list = []

    def current(self):
        return {"loaded": True, "served": self.served, "config": dict(self.base), "max_ctx": None}

    def drafts(self):
        return []

    def load(self, config, measure):
        self.loads.append((dict(config), dict(measure) if measure else None))
        eff = dict(self.base, **config)
        if self.cpu and eff.get("kv_cache_dtype") == "fp8":
            return {"ok": False, "rejected": True, "error": "ValueError: fp8 KV cache is not supported on CPU"}
        out = {"ok": True, "error": None, "config": eff, "ctx": eff.get("max_model_len"),
               "kv_tokens": self.kv_tokens, "load_s": 20.0, "stick": None}
        if measure:
            conc = int(measure.get("concurrency") or 1)
            decode = 40.0
            spec = eff.get("speculative_config") or {}
            if isinstance(spec, dict) and spec.get("method") == "ngram":
                decode *= 1.2
            if eff.get("kv_cache_dtype") == "fp8":
                decode *= 1.075
            decode = decode / (conc ** 0.4)
            out["stick"] = {"ok": True, "decode_tps": decode, "prefill_tps": 900.0, "agg_tps": decode * conc * 0.9,
                            "accept": 0.7 if spec else None, "seconds": 20.0, "completion_tokens": 800, "error": None}
        return out


def _run(vat, body, backend=None, cancelled=lambda: False, runtime=True):
    req = vat.validate_request(body)
    events: list = []
    backend = backend or FakeBackend()
    done = vat.run_model(MODEL, req, backend, events.append, cancelled, {"run_id": "r1", "runtime": runtime})
    return req, done, events, backend


# ── validation + legacy ──

def test_validate_defaults_and_bounds(vat):
    req = vat.validate_request({"model_ids": [MODEL]})
    assert req["objective"] == "balanced" and req["mode"] == "tune" and req["apply"] is False
    assert req["dims"]["seqs"]["candidates"] == [1, 4, 8] and req["dims"]["kvdtype"]["candidates"] == ["auto", "fp8"]
    assert req["dims"]["context"] == {"on": True, "probe_len": 4096, "concurrency": 1.0, "kv_fraction": 1.0}
    assert req["load_timeout_s"] == 600 and req["gpu_memory_utilization"] is None
    assert vat.validate_request({"model_ids": [MODEL], "objective": "serve"})["dims"]["seqs"]["candidates"] == [8, 16, 32]
    assert vat.validate_request({"model_ids": [MODEL], "gpu_memory_utilization": 0.8})["gpu_memory_utilization"] == 0.8
    for bad in ({"model_ids": []}, {"model_ids": [MODEL], "objective": "quiet"},
                {"model_ids": [MODEL], "dims": {"kvdtype": {"candidates": ["int4"]}}},
                {"model_ids": [MODEL], "dims": {"context": {"probe_len": 100}}},
                {"model_ids": [MODEL], "dims": {"spec": {"types": ["mtp"]}}},
                {"model_ids": [MODEL], "gpu_memory_utilization": 0.3},
                {"model_ids": [MODEL], "dims": {"prefix": {"candidates": ["on"]}}}):
        with pytest.raises(ValueError):
            vat.validate_request(bad)
    for mid in ("/srv/models/foo", "./m"):
        assert vat.validate_request({"model_ids": [mid]})["model_ids"] == [mid]
    with pytest.raises(ValueError, match="invalid model id"):
        vat.validate_request({"model_ids": ["bad id"]})
    req = vat.validate_request({"model_ids": [MODEL], "mode": "verify", "baseline": {"decode_tps": 40, "ctx": 8192, "concurrency": 4}})
    assert req["baseline_tps"] == 40.0 and req["baseline"]["concurrency"] == 4.0
    assert all(not d["on"] for k, d in req["dims"].items() if k != "context")


def test_legacy_body_maps_to_context_only(vat):
    old = {"probe_len": 2048, "concurrency": 2.0, "kv_fraction": 0.5, "report_only": False, "load_timeout_s": 300}
    assert vat.is_legacy(old) and not vat.is_legacy({"model_ids": [MODEL], "probe_len": 1}) and not vat.is_legacy({})
    req = vat.validate_request(vat.legacy_body(old, MODEL))
    assert req["model_ids"] == [MODEL] and req["objective"] == "fit" and req["apply"] is True
    assert req["dims"]["context"] == {"on": True, "probe_len": 2048, "concurrency": 2.0, "kv_fraction": 0.5}
    assert all(not req["dims"][k]["on"] for k in ("seqs", "kvdtype", "spec", "prefix"))
    assert req["load_timeout_s"] == 300
    assert vat.validate_request(vat.legacy_body({"report_only": True}, MODEL))["apply"] is False


# ── arg transforms ──

def test_config_from_args_reads_both_flag_forms(vat):
    cfg = vat.config_from_args(ARGS + [{"flag": "--speculative-config", "value": '{"method":"ngram","num_speculative_tokens":5}', "bool": False},
                                       {"flag": "--no-enable-prefix-caching", "value": None, "bool": True},
                                       {"flag": "--kv-cache-dtype", "value": "fp8", "bool": False}])
    assert cfg == {"max_model_len": 8192, "max_num_seqs": 4, "gpu_memory_utilization": 0.9,
                   "speculative_config": {"method": "ngram", "num_speculative_tokens": 5},
                   "enable_prefix_caching": False, "kv_cache_dtype": "fp8"}
    assert vat.config_from_args([{"flag": "-sc", "value": "{bad", "bool": False}]) == {"speculative_config": "{bad"}
    assert vat.config_from_args([{"flag": "--enable-prefix-caching", "value": None, "bool": True}]) == {"enable_prefix_caching": True}
    assert vat.config_from_args([]) == {}
    assert vat.config_from_args([{"flag": "--max-model-len", "value": "8k", "bool": False}]) == {"max_model_len": 8192}
    assert vat.config_from_args([{"flag": "--max-num-seqs", "value": "lots", "bool": False}]) == {"max_num_seqs": None}


def test_backend_exception_still_ends_the_model(vat):
    class Boom(FakeBackend):
        def load(self, config, measure):
            raise RuntimeError("journal exploded")
    _req, done, events, _be = _run(vat, {"model_ids": [MODEL]}, backend=Boom())
    assert done["ok"] is False and done["stop_reason"] == "journal exploded" and events[-1]["type"] == "model_done"


def test_args_with_config_replaces_and_drops(vat):
    out = vat.args_with_config(ARGS, {"max_model_len": 4096, "max_num_seqs": 8, "enable_prefix_caching": False,
                                      "speculative_config": vat.NGRAM_SPEC, "kv_cache_dtype": "auto"})
    flags = [a["flag"] for a in out]
    assert flags[:1] == ["--host"] and "--max-num-seqs=4" not in flags
    assert {"flag": "--max-model-len", "value": "4096", "bool": False} in out
    assert {"flag": "--max-num-seqs", "value": "8", "bool": False} in out
    assert {"flag": "--no-enable-prefix-caching", "value": None, "bool": True} in out
    assert {"flag": "--kv-cache-dtype", "value": "auto", "bool": False} in out
    sc = next(a for a in out if a["flag"] == "--speculative-config")
    assert sc["value"] == '{"method":"ngram","num_speculative_tokens":5,"prompt_lookup_max":4}'
    assert ARGS[1]["value"] == "8192"
    # None drops the flag; a gpu_memory_utilization input is written like a tuned key.
    out = vat.args_with_config(out, {"speculative_config": None, "enable_prefix_caching": None, "gpu_memory_utilization": 0.85})
    flags = [a["flag"] for a in out]
    assert "--speculative-config" not in flags and "--no-enable-prefix-caching" not in flags
    assert {"flag": "--gpu-memory-utilization", "value": "0.85", "bool": False} in out and flags.count("--gpu-memory-utilization") == 1


def test_compute_recommended_max_len(vat):
    assert vat.compute_recommended_max_len(230528) == 230400
    assert vat.compute_recommended_max_len(230528, concurrency=2.0) == 115200
    assert vat.compute_recommended_max_len(10000, kv_fraction=0.5) == 4864
    assert vat.compute_recommended_max_len(100) == 256
    assert vat.compute_recommended_max_len(10000, concurrency=0.0) == vat.compute_recommended_max_len(10000, concurrency=1.0)


def test_cfg_str_and_spec_type(vat):
    assert vat.cfg_str(True) == "true" and vat.cfg_str(None) == "" and vat.cfg_str(8) == "8"
    assert vat.cfg_str({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert vat.spec_type(None) == "none" and vat.spec_type(vat.NGRAM_SPEC) == "ngram"
    assert vat.spec_type({"model": "org/draft"}) == "draft" and vat.spec_type("{bad") == "custom"


# ── stages ──

def test_balanced_tune_stage_order_and_recommendation(vat):
    req, done, events, be = _run(vat, {"model_ids": [MODEL]})
    ms = next(e for e in events if e["type"] == "model_start")
    assert ms["provider"] == "vllm" and ms["stages"] == ["context", "seqs", "kvdtype", "spec", "prefix", "verify"]
    assert [e["stage"] for e in events if e["type"] == "stage_start"] == ["context", "seqs", "kvdtype", "spec", "prefix", "verify"]
    assert done["ok"] and done["provider"] == "vllm" and done["kv_tokens"] == 230528
    keys = {c["key"]: c for c in done["changes"]}
    # 8192 fits 230,400 so balanced keeps it; fp8 gains 7.5 %; ngram gains 20 %; prefix stays; seqs → 1 (balanced keeps ≥ 70 % of single-request decode).
    assert "max_model_len" not in keys
    assert keys["speculative_config"]["value"] == vat.NGRAM_SPEC and keys["speculative_config"]["flag"] == "--speculative-config"
    assert keys["kv_cache_dtype"]["recommended"] == "fp8"
    assert "enable_prefix_caching" not in keys
    assert keys["max_num_seqs"]["recommended"] == "1"
    assert done["verify"]["ok"] and done["after"]["decode_tps"] > 0 and done["before"]["ctx"] == 4096
    assert be.loads[0][0]["max_model_len"] == 4096 and be.loads[-1][0]["speculative_config"] == vat.NGRAM_SPEC
    assert done["applied"] is False and done["apply"] is False


def test_fit_recommends_the_computed_context(vat):
    _req, done, events, _be = _run(vat, {"model_ids": [MODEL], "objective": "fit",
                                         "dims": {"context": {"concurrency": 2.0}, "seqs": {"on": False}, "kvdtype": {"on": False},
                                                  "spec": {"on": False}, "prefix": {"on": False}}})
    rec = next(e for e in events if e["type"] == "recommendation")
    assert rec["max_model_len"] == 115200 and rec["kv_tokens"] == 230528 and rec["concurrency"] == 2.0
    assert next(e for e in events if e["type"] == "kv_capacity")["tokens"] == 230528
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["max_model_len"]["recommended"] == "115200" and keys["max_model_len"]["current"] == "8192"
    assert [e["stage"] for e in events if e["type"] == "stage_skipped"] == ["seqs", "kvdtype", "spec", "prefix"]
    assert done["after"]["ctx"] == 115200


_CTX_ONLY = {"seqs": {"on": False}, "kvdtype": {"on": False}, "spec": {"on": False}, "prefix": {"on": False}}


def test_fit_caps_the_context_at_the_model_maximum(vat):
    class Capped(FakeBackend):
        def current(self):
            return dict(super().current(), max_ctx=40960)
    _req, done, events, _be = _run(vat, {"model_ids": [MODEL], "objective": "fit", "dims": _CTX_ONLY}, backend=Capped())
    assert next(e for e in events if e["type"] == "recommendation")["max_model_len"] == 40960
    ctx_done = next(e for e in events if e["type"] == "stage_done" and e["stage"] == "context")
    assert ctx_done["choice"] == "40960" and "capped at the model's maximum" in ctx_done["reason"]
    assert {c["key"]: c for c in done["changes"]}["max_model_len"]["recommended"] == "40960"
    assert done["after"]["ctx"] == 40960


def test_derived_max_rejection_caps_later_restarts(vat):
    class Derived(FakeBackend):
        rejected = False

        def load(self, config, measure):
            if config.get("kv_cache_dtype") == "auto" and not self.rejected:
                self.rejected = True
                self.loads.append((dict(config), dict(measure) if measure else None))
                return {"ok": False, "rejected": True, "error": "ValueError: greater than the derived max_model_len (40960)",
                        "derived_max": 40960}
            return super().load(config, measure)
    dims = dict(_CTX_ONLY, kvdtype={"on": True})
    _req, done, events, be = _run(vat, {"model_ids": [MODEL], "objective": "fit", "dims": dims}, backend=Derived())
    rejected_at = next(i for i, ld in enumerate(be.loads) if ld[0].get("kv_cache_dtype") == "auto")
    assert be.loads[rejected_at][0]["max_model_len"] == 230400
    assert all(ld[0]["max_model_len"] == 40960 for ld in be.loads[rejected_at + 1:]) and len(be.loads) > rejected_at + 1
    assert done["after"]["ctx"] == 40960 and done["config"]["max_model_len"] == 40960
    assert any(e["type"] == "line" and "derived maximum 40,960" in e["text"] for e in events)


def test_other_objectives_shrink_a_context_that_does_not_fit(vat):
    be = FakeBackend(base={"max_model_len": 500000, "max_num_seqs": 4})
    _req, done, _events, _be = _run(vat, {"model_ids": [MODEL], "objective": "speed", "dims": {"seqs": {"on": False}, "kvdtype": {"on": False}, "spec": {"on": False}, "prefix": {"on": False}}}, backend=be)
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["max_model_len"]["recommended"] == "230400"


def test_serve_picks_max_aggregate_seqs(vat):
    _req, done, _events, _be = _run(vat, {"model_ids": [MODEL], "objective": "serve", "dims": {"kvdtype": {"on": False}, "spec": {"on": False}, "prefix": {"on": False}}})
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["max_num_seqs"]["recommended"] == "32" and done["after"]["concurrency"] == 32


def test_speed_keeps_a_single_sequence(vat):
    _req, done, _events, _be = _run(vat, {"model_ids": [MODEL], "objective": "speed", "dims": {"kvdtype": {"on": False}, "spec": {"on": False}, "prefix": {"on": False}}})
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["max_num_seqs"]["recommended"] == "1" and done["after"]["concurrency"] == 1


def test_rejected_kvdtype_candidate_is_not_fatal(vat):
    be = FakeBackend(cpu=True)
    _req, done, events, _be = _run(vat, {"model_ids": [MODEL], "dims": {"seqs": {"on": False}, "spec": {"on": False}, "prefix": {"on": False}}}, backend=be)
    rej = [e for e in events if e["type"] == "candidate_rejected"]
    assert rej == [{"type": "candidate_rejected", "model_id": MODEL, "stage": "kvdtype", "value": "fp8",
                    "reason": "ValueError: fp8 KV cache is not supported on CPU"}]
    res = [e for e in events if e["type"] == "candidate_result" and e["stage"] == "kvdtype"]
    assert [(r["value"], r["ok"]) for r in res] == [("auto", True), ("fp8", False)]
    assert done["ok"] and "kv_cache_dtype" not in {c["key"] for c in done["changes"]}
    assert next(e for e in events if e["type"] == "stage_done" and e["stage"] == "kvdtype")["choice"] == "auto"


def test_spec_draft_model_row_only_when_named(vat):
    _req, _done, events, be = _run(vat, {"model_ids": [MODEL], "dims": {"seqs": {"on": False}, "kvdtype": {"on": False}, "prefix": {"on": False},
                                                                         "spec": {"types": ["auto"], "draft_model": "org/draft-1b"}}})
    st = next(e for e in events if e["type"] == "stage_start" and e["stage"] == "spec")
    assert st["candidates"] == ["none", "ngram", "draft"]
    draft = next(ld[0]["speculative_config"] for ld in be.loads if isinstance(ld[0].get("speculative_config"), dict) and ld[0]["speculative_config"].get("model"))
    assert draft == {"method": "draft_model", "model": "org/draft-1b", "num_speculative_tokens": 5}
    _req, _done, events, _be = _run(vat, {"model_ids": [MODEL], "dims": {"seqs": {"on": False}, "kvdtype": {"on": False}, "prefix": {"on": False}, "spec": {"types": ["draft"]}}})
    assert next(e for e in events if e["type"] == "stage_skipped" and e["stage"] == "spec")["reason"] == "no_candidates"


def test_no_runtime_skips_measured_stages_but_context_still_probes(vat):
    _req, done, events, be = _run(vat, {"model_ids": [MODEL], "objective": "fit"}, runtime=False)
    assert [e["stage"] for e in events if e["type"] == "stage_skipped"] == ["seqs", "kvdtype", "spec", "prefix"]
    assert done["ok"] and done["verify"]["ok"] and len(be.loads) == 2 and be.loads[0][1] is None


def test_budget_stops_later_stages(vat):
    clock = iter([0, 0, 0, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000])
    req = vat.validate_request({"model_ids": [MODEL], "budget_min": 5})
    events: list = []
    done = vat.run_model(MODEL, req, FakeBackend(), events.append, lambda: False, {"run_id": "r1", "runtime": True}, clock=lambda: next(clock, 3000))
    skipped = {e["stage"]: e["reason"] for e in events if e["type"] == "stage_skipped"}
    assert skipped.get("seqs") == "budget" and skipped.get("prefix") == "budget"
    assert done["ok"] and any(e["type"] == "stage_start" and e["stage"] == "verify" for e in events)


def test_probe_failure_stops_the_run(vat):
    class Dead(FakeBackend):
        def load(self, config, measure):
            return {"ok": False, "rejected": True, "error": "EngineCore failed to start"}
    _req, done, events, _be = _run(vat, {"model_ids": [MODEL]}, backend=Dead())
    assert done["ok"] is False and done["stop_reason"] == "EngineCore failed to start"
    assert [e["stage"] for e in events if e["type"] == "stage_start"] == ["context"]


def test_wrong_model_ends_without_a_load(vat):
    be = FakeBackend(served="org/other")
    _req, done, _events, _be = _run(vat, {"model_ids": [MODEL]}, backend=be)
    assert done["ok"] is False and done["stop_reason"] == f"vLLM serves org/other, not {MODEL}" and be.loads == []


def test_cancel_mid_run(vat):
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 4
    _req, done, events, _be = _run(vat, {"model_ids": [MODEL]}, cancelled=cancelled)
    assert done["cancelled"] and not done["ok"] and done["stop_reason"] == "cancelled"
    assert not any(e["type"] == "stage_start" and e["stage"] == "verify" for e in events)


def test_verify_failure_is_reported_not_retried(vat):
    class Flaky(FakeBackend):
        def load(self, config, measure):
            out = super().load(config, measure)
            if measure and int(measure.get("limit") or 0) > 4:
                out["stick"] = {"ok": False, "error": "3 samples failed"}
            return out
    _req, done, _events, be = _run(vat, {"model_ids": [MODEL], "objective": "speed", "dims": {"seqs": {"on": False}, "kvdtype": {"on": False}, "spec": {"on": False}, "prefix": {"on": False}}}, backend=Flaky())
    assert done["ok"] and done["verify"] == {"ok": False, "seconds": 0, "dropped": [], "reason": "3 samples failed", "warning": None}
    assert sum(1 for ld in be.loads if ld[1] and int(ld[1]["limit"]) > 4) == 1


def test_apply_marks_applied_only_when_verify_passes(vat):
    _req, done, _events, _be = _run(vat, {"model_ids": [MODEL], "apply": True, "dims": {"seqs": {"on": False}, "kvdtype": {"on": False}, "spec": {"on": False}, "prefix": {"on": False}}})
    assert done["apply"] is True and done["applied"] is True
    summary = vat.ledger_summary(done)
    assert summary["provider"] == "vllm" and summary["applied"] is True and summary["kv_tokens"] == 230528 and summary["max_model_len"] == 8192


def test_verify_mode_regression_against_recorded_baseline(vat):
    _req, done, events, be = _run(vat, {"model_ids": [MODEL], "mode": "verify", "baseline": {"decode_tps": 80, "ctx": 8192, "concurrency": 4}})
    assert done["mode"] == "verify" and done["regressed"] is True and be.loads[0][1]["concurrency"] == 4
    assert next(e for e in events if e["type"] == "model_start")["stages"] == ["verify"]
    assert done["changes"] == []


def test_gpu_memory_utilization_input_is_written_never_swept(vat):
    _req, done, _events, be = _run(vat, {"model_ids": [MODEL], "gpu_memory_utilization": 0.8, "dims": {"seqs": {"on": False}, "kvdtype": {"on": False}, "spec": {"on": False}, "prefix": {"on": False}}})
    assert all(ld[0].get("gpu_memory_utilization") == 0.8 for ld in be.loads)
    assert "gpu_memory_utilization" not in {c["key"] for c in done["changes"]}


# ── #1143: context probe retries at the engine's estimate; a derived-max rejection retries the candidate ──

def test_context_probe_retries_once_at_the_estimated_maximum(vat):
    class Estimating(FakeBackend):
        def load(self, config, measure):
            if config.get("max_model_len") == 230400:
                self.loads.append((dict(config), dict(measure) if measure else None))
                return {"ok": False, "rejected": True, "est_max_len": 56736,
                        "error": "engine estimates the maximum model length at 56,736"}
            return super().load(config, measure)
    dims = dict(_CTX_ONLY, context={"probe_len": 230400, "concurrency": 1, "kv_fraction": 0.9})
    _req, done, events, be = _run(vat, {"model_ids": [MODEL], "objective": "fit", "dims": dims}, backend=Estimating())
    assert [ld[0]["max_model_len"] for ld in be.loads[:2]] == [230400, 56576]
    assert done["ok"] is True and done["before"]["ctx"] == 56576
    assert any(e["type"] == "line" and "56,736" in e["text"] and "retry" in e["text"] for e in events)
    assert [e["value"] for e in events if e["type"] == "candidate_start" and e["stage"] == "context"] == [230400, 56576]


def test_context_probe_gives_up_after_one_estimate_retry(vat):
    class Always(FakeBackend):
        def load(self, config, measure):
            self.loads.append((dict(config), dict(measure) if measure else None))
            return {"ok": False, "rejected": True, "est_max_len": 2048, "error": "engine estimates the maximum model length at 2,048"}
    _req, done, _events, be = _run(vat, {"model_ids": [MODEL]}, backend=Always())
    assert done["ok"] is False and len(be.loads) == 2 and "2,048" in done["stop_reason"]
    assert [ld[0]["max_model_len"] for ld in be.loads] == [4096, 2048]


def test_derived_max_rejection_retries_the_same_candidate_at_the_cap(vat):
    class Derived(FakeBackend):
        rejected = False

        def load(self, config, measure):
            if config.get("kv_cache_dtype") == "auto" and not self.rejected:
                self.rejected = True
                self.loads.append((dict(config), dict(measure) if measure else None))
                return {"ok": False, "rejected": True, "error": "ValueError: greater than the derived max_model_len (40960)",
                        "derived_max": 40960}
            return super().load(config, measure)
    dims = dict(_CTX_ONLY, kvdtype={"on": True})
    _req, _done, events, be = _run(vat, {"model_ids": [MODEL], "objective": "fit", "dims": dims}, backend=Derived())
    i = next(i for i, ld in enumerate(be.loads) if ld[0].get("kv_cache_dtype") == "auto")
    assert be.loads[i + 1][0]["kv_cache_dtype"] == "auto" and be.loads[i + 1][0]["max_model_len"] == 40960
    auto = [e for e in events if e["type"] == "candidate_result" and e["stage"] == "kvdtype" and e["value"] == "auto"]
    assert len(auto) == 1 and auto[0]["ok"] is True
    assert not any(e["type"] == "candidate_rejected" and e["value"] == "auto" for e in events)
