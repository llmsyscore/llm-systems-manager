# agent/tests/test_autotune_v2_engine.py
"""#880: the stage engine against a fake backend — objectives, skips, budget, verify fallback, cancel."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def at():
    spec = importlib.util.spec_from_file_location(
        "llama_autotune", _AGENT_ROOT / "providers" / "llama_autotune.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["llama_autotune"] = mod
    spec.loader.exec_module(mod)
    return mod


def _val(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        return args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else "true"
    return default


class Fake:
    """Deterministic backend: ctx doubles on q8_0 KV, 12 threads is fastest, MTP gains 38 %,
    per-request decode falls with slots, offload n>=6 fits a 32k floor when ctx_fit is small."""
    def __init__(self, *, ctx_fit=32768, load_s=10.0, facts=None, verify_fail=None, oom_below_moe=None,
                 spec_gain=1.38, tick=1.0):
        self.ctx_fit, self.load_s, self.facts = ctx_fit, load_s, facts or {"n_expert": 0, "n_layer": 32, "mtp_layers": 0}
        self.verify_fail, self.oom_below_moe, self.spec_gain = verify_fail, oom_below_moe, spec_gain
        self.calls, self.energy, self.tick = [], [], tick
        self.verify_calls = 0

    def _ctx(self, args):
        return self.ctx_fit * (2 if _val(args, "--cache-type-k") == "q8_0" else 1)

    def converge(self, args, target, tol, start_fitt, max_iters):
        self.calls.append(("converge", list(args)))
        return {"ok": True, "ctx": self._ctx(args), "free_mb": 1012, "total_mb": 32768, "fitt": 1024,
                "converged": True, "iters": 3, "stop_reason": "converged", "facts": dict(self.facts),
                "load_s": self.load_s}

    def _decode(self, args, conc):
        t = int(_val(args, "--threads", "16"))
        base = {8: 100.0, 12: 102.5, 16: 101.0, 32: 96.0}.get(t, 100.0)
        st = _val(args, "--spec-type", "none")
        if st == "draft-mtp":
            base *= self.spec_gain
            nmax = int(_val(args, "--spec-draft-n-max", "8"))
            base *= {4: 0.97, 8: 1.0, 12: 1.05, 16: 1.02}.get(nmax, 1.0)
        elif st == "ngram-simple":
            base *= 1.02
        if _val(args, "--n-cpu-moe"):
            base *= 0.9
        per = base * {1: 1.0, 2: 0.9, 4: 0.72, 8: 0.5}.get(conc, 0.4)
        return per, per * conc

    def load(self, args, ctx, measure):
        self.calls.append(("load", list(args), ctx, measure))
        moe = _val(args, "--n-cpu-moe")
        if self.oom_below_moe is not None and ctx and ctx > self.ctx_fit and (moe is None or int(moe) < self.oom_below_moe):
            return {"ok": False, "oom": True, "error": "OOM at load", "ctx": None, "free_mb": None,
                    "total_mb": 32768, "facts": {}, "load_s": self.load_s, "stick": None}
        if measure and measure.get("limit", 4) > 16 and self.verify_fail:      # verify sizes limit ≥ 24; sweeps stay ≤ 16
            self.verify_calls += 1
            if self.verify_fail(args, self.verify_calls):
                return {"ok": False, "oom": True, "error": "OOM under load", "ctx": ctx, "free_mb": None,
                        "total_mb": 32768, "facts": {}, "load_s": self.load_s, "stick": None}
        stick = None
        if measure:
            per, agg = self._decode(args, measure["concurrency"])
            stick = {"ok": True, "decode_tps": per, "prefill_tps": 2300.0, "latency_s": 1.2, "agg_tps": agg,
                     "accept": 0.74 if _val(args, "--spec-type") not in (None, "none") else None,
                     "completion_tokens": 1024 * measure["concurrency"], "seconds": 20.0, "error": None}
        return {"ok": True, "oom": False, "error": None, "ctx": ctx or 32768, "free_mb": 1040,
                "total_mb": 32768, "facts": dict(self.facts), "load_s": self.load_s, "stick": stick}

    def kl(self, args, write_base):
        self.calls.append(("kl", list(args), write_base))
        c = _val(args, "--cache-type-k", "f16")
        return {"ok": True, "kl": {"f16": 0.0, "q8_0": 0.006, "q4_0": 0.031}.get(c, 0.01), "error": None}

    def energy_start(self):
        self.energy.append("start")

    def energy_stop(self):
        self.energy.append("stop")
        return 0.5, "psu"


class Clock:
    def __init__(self, step=1.0):
        self.t, self.step = 0.0, step
    def __call__(self):
        self.t += self.step
        return self.t


def _run(at, backend, body, section=None, *, env=None, cancelled=lambda: False, clock=None):
    req = at.validate_request(body)
    events = []
    e = {"run_id": "r1", "runtime": True, "perplexity": True, "drafts": [], "cores": {"physical": 16, "logical": 32},
         "target_repo": "unsloth/Qwen3-30B-A3B-GGUF", "target_size": 18_000_000_000, "llama_build": "b10850-abc"}
    e.update(env or {})
    done = at.run_model("org/m:Q4", section or {"hf-repo": "o/r", "ctx-size": "32768", "threads": "32"}, req,
                        backend, events.append, cancelled, e, clock=clock or Clock())
    return done, events


def _types(events):
    return [ev["type"] for ev in events]


def _stage(done, name):
    return next(s for s in done["stages"] if s["stage"] == name)


BALANCED = {"model_ids": ["org/m:Q4"], "objective": "balanced", "budget_min": 120,
            "dims": {"threads": {"on": True, "candidates": [8, 12, 16, 32]},
                     "slots": {"on": True, "candidates": [1, 2, 4, 8], "min_ctx_per_slot": 8192}}}


def test_v1_body_runs_context_and_verify_only(at):
    be = Fake()
    done, events = _run(at, be, {"model_ids": ["org/m:Q4"], "target_mb": 1024, "tolerance_mb": 50},
                        env={"runtime": False})
    assert done["type"] == "model_done" and done["ok"] and done["objective"] == "fit"
    assert [s["status"] for s in done["stages"]] == ["done", "skipped", "skipped", "skipped", "skipped", "skipped", "skipped", "done"]
    assert {s["reason"] for s in done["stages"] if s["status"] == "skipped"} == {"off"}
    assert done["changes"] == [] and done["after"]["ctx"] == 32768 and done["verify"]["ok"] is True
    assert _types(events)[0] == "model_start" and _types(events)[-1] == "model_done"
    assert be.calls[0][0] == "load" and be.calls[0][2] == 32768           # baseline: the current ctx-size, fit off
    assert be.calls[1][0] == "converge"


def test_balanced_full_run(at):
    be = Fake(facts={"n_expert": 128, "n_expert_used": 8, "n_layer": 48, "mtp_layers": 1})
    done, events = _run(at, be, BALANCED)
    assert done["ok"] and done["verify"]["ok"]
    st = {s["stage"]: s for s in done["stages"]}
    assert st["context"]["choice"] == "32768" and st["kv"]["choice"] == "q8_0"
    assert st["moe"]["status"] == "skipped" and st["moe"]["reason"] == "fits"
    assert st["threads"]["choice"] == "8"                                  # 8/12/16 tie → fewer
    assert st["spec"]["choice"] == "draft-mtp" and st["slots"]["choice"] == "4"
    assert st["sampling"] == {"stage": "sampling", "status": "skipped", "reason": "manager"}
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["ctx-size"]["recommended"] == "65536" and keys["ctx-size"]["current"] == "32768"
    assert keys["cache-type-k"]["recommended"] == "q8_0" and keys["cache-type-v"]["recommended"] == "q8_0"
    assert keys["threads"]["recommended"] == "8" and keys["threads"]["selected"] is True
    assert keys["spec-type"]["recommended"] == "draft-mtp" and keys["spec-type"]["selected"] is True
    assert keys["spec-draft-n-max"]["recommended"] == "12" and keys["parallel"]["recommended"] == "4"
    assert "model-draft" not in keys                                        # MTP needs no draft file
    assert all(c["source"] == "measured" for c in done["changes"])
    assert done["before"]["decode_tps"] == pytest.approx(96.0) and done["after"]["concurrency"] == 4
    assert done["after"]["wh_per_ktok"] == pytest.approx(0.5 / (4096 / 1000))
    assert done["guard"]["pass"] is True and done["guard"]["kl"] == 0.006
    assert done["facts"]["n_expert"] == 128
    assert "facts" in _types(events)
    starts = [ev["stage"] for ev in events if ev["type"] == "stage_start"]
    assert starts == ["context", "kv", "threads", "spec", "slots", "verify"]
    s = at.ledger_summary(done)
    assert s["objective"] == "balanced" and s["ctx_size"] == 65536 and s["verify_ok"] is True


def test_spawn_args_follow_the_help_valued_set(at):
    be = Fake()
    sec = {"hf-repo": "o/r", "ctx-size": "32768", "threads": "32", "flash-attn": "on", "check-tensors": "off"}
    _run(at, be, BALANCED, section=sec, env={"valued": {"flash-attn"}})
    for call in be.calls:
        args = call[1]
        assert args[args.index("--flash-attn"):args.index("--flash-attn") + 2] == ["--flash-attn", "on"]
        assert "--check-tensors" not in args


def _spec_candidates(events):
    return [ev["candidates"] for ev in events if ev["type"] == "stage_start" and ev["stage"] == "spec"]


def test_spec_stage_start_lists_the_none_reference_first(at):
    be = Fake(facts={"n_expert": 128, "n_expert_used": 8, "n_layer": 48, "mtp_layers": 1})
    _, events = _run(at, be, BALANCED)
    assert _spec_candidates(events) == [["none", "draft-mtp", "ngram-simple"]]


def test_spec_tries_the_configured_type_when_detection_missed_it(at):
    be = Fake(facts={"n_expert": 128, "n_expert_used": 8, "n_layer": 48, "mtp_layers": 0})
    sec = {"hf-repo": "o/r", "ctx-size": "32768", "threads": "32", "spec-type": "draft-mtp"}
    _, events = _run(at, be, BALANCED, section=sec)
    assert _spec_candidates(events) == [["none", "draft-mtp", "ngram-simple"]]


def _changes(done):
    return {c["key"]: c for c in done["changes"]}


def test_spec_keeps_a_configured_type_without_emitting_a_change(at):
    """draft-mtp already configured and still the winner: no spec-type row."""
    be = Fake(facts={"n_expert": 128, "n_expert_used": 8, "n_layer": 48, "mtp_layers": 1})
    sec = {"hf-repo": "o/r", "ctx-size": "32768", "threads": "32", "spec-type": "draft-mtp"}
    done, _ = _run(at, be, BALANCED, section=sec)
    assert _stage(done, "spec")["choice"] == "draft-mtp"
    assert "spec-type" not in _changes(done)


def test_spec_same_window_as_configured_emits_nothing(at):
    be = Fake(facts={"n_expert": 128, "n_expert_used": 8, "n_layer": 48, "mtp_layers": 1})
    sec = {"hf-repo": "o/r", "ctx-size": "32768", "threads": "32", "spec-type": "draft-mtp",
           "spec-draft-n-min": "0", "spec-draft-n-max": "12"}
    done, events = _run(at, be, BALANCED, section=sec)
    reason = next(ev["reason"] for ev in events if ev["type"] == "stage_done" and ev["stage"] == "spec")
    assert reason == "already configured · no better window · window 0–12"
    assert not [k for k in _changes(done) if k.startswith("spec-") or k == "model-draft"]


def test_spec_regression_against_the_configured_type_is_unselected(at):
    """none only beats the configured draft-mtp by 2 %: recommend it, but do not select it."""
    be = Fake(facts={"n_expert": 128, "n_expert_used": 8, "n_layer": 48, "mtp_layers": 1}, spec_gain=1 / 1.02)
    sec = {"hf-repo": "o/r", "ctx-size": "32768", "threads": "32", "spec-type": "draft-mtp"}
    done, _ = _run(at, be, BALANCED, section=sec)
    row = _changes(done)["spec-type"]
    assert row["current"] == "draft-mtp" and row["recommended"] == "none"
    assert row["selected"] is False


class FreeFake(Fake):
    """Load reports the free VRAM measured under load, which is lower than -fitt convergence saw."""
    def __init__(self, free_mb, **kw):
        super().__init__(**kw)
        self.free = free_mb

    def load(self, args, ctx, measure):
        r = super().load(args, ctx, measure)
        if r.get("ok"):
            r["free_mb"] = self.free
        return r


class CurWinFake(Fake):
    """The configured window (n-max 3) is the fastest one."""
    def _decode(self, args, conc):
        per, agg = super()._decode(args, conc)
        if _val(args, "--spec-type") == "draft-mtp" and _val(args, "--spec-draft-n-max") == "3":
            per, agg = per * 1.5, agg * 1.5
        return per, agg


def test_verify_warns_instead_of_failing_on_a_small_vram_shortfall(at):
    """Compute buffers are live under load; 925 of a 1024 ± 50 MB target must not burn the run."""
    done, events = _run(at, FreeFake(925, facts={"n_expert": 128, "n_expert_used": 8, "n_layer": 48,
                                                 "mtp_layers": 1}), BALANCED)
    assert done["verify"]["ok"] is True and done["verify"]["dropped"] == []
    assert done["verify"]["warning"] == "free VRAM 925 MB is below the 1024 ± 50 MB target"
    assert done["after"]["decode_tps"] and done["after"]["free_mb"] == 925
    assert any(ev["type"] == "line" and "free VRAM 925 MB" in ev["text"] for ev in events)
    assert {c["key"] for c in done["changes"]} >= {"spec-type", "threads"}    # nothing dropped


def test_verify_still_fails_when_vram_is_far_below_target(at):
    done, _ = _run(at, FreeFake(400, facts={"n_expert": 128, "n_expert_used": 8, "n_layer": 48,
                                            "mtp_layers": 1}), BALANCED)
    assert done["verify"]["ok"] is False and done["verify"]["reason"] == "free VRAM below target"
    assert done["verify"]["dropped"] == ["slots", "spec", "threads"] and done["verify"]["warning"] is None


def test_spec_measures_the_configured_window_first(at):
    be = CurWinFake(facts={"n_expert": 128, "n_expert_used": 8, "n_layer": 48, "mtp_layers": 1})
    sec = {"hf-repo": "o/r", "ctx-size": "32768", "threads": "32", "spec-type": "draft-mtp",
           "spec-draft-n-max": "3"}
    done, events = _run(at, be, BALANCED, section=sec)
    vals = [ev["value"] for ev in events if ev["type"] == "candidate_result" and ev["stage"] == "spec"]
    assert "draft-mtp 0–3 (current)" in vals
    assert vals.index("draft-mtp 0–3 (current)") < vals.index("draft-mtp 0–4")
    assert not [k for k in _changes(done) if k.startswith("spec-") or k == "model-draft"]


def test_spec_stage_done_reason_names_the_window(at):
    be = Fake(facts={"n_expert": 128, "n_expert_used": 8, "n_layer": 48, "mtp_layers": 1})
    _, events = _run(at, be, BALANCED)
    reason = next(ev["reason"] for ev in events if ev["type"] == "stage_done" and ev["stage"] == "spec")
    assert reason.endswith("· window 0–12") and reason.startswith("+38 % decode")


def test_speed_keeps_f16_and_one_slot(at):
    done, _ = _run(at, Fake(), dict(BALANCED, objective="speed"))
    st = {s["stage"]: s for s in done["stages"]}
    assert st["kv"]["choice"] == "f16" and st["slots"]["choice"] == "1"
    keys = {c["key"] for c in done["changes"]}
    assert "cache-type-k" not in keys and "parallel" not in keys


def test_serve_picks_max_aggregate(at):
    done, _ = _run(at, Fake(), dict(BALANCED, objective="serve"))
    assert _stage(done, "slots")["choice"] == "8"


def test_verify_measures_at_tuned_concurrency_when_slots_unchanged(at):
    done, _ = _run(at, Fake(), BALANCED, section={"hf-repo": "o/r", "ctx-size": "32768", "threads": "32", "parallel": "4"})
    assert _stage(done, "slots")["choice"] == "4"
    assert "parallel" not in {c["key"] for c in done["changes"]}
    assert done["after"]["concurrency"] == 4


def test_fit_objective_stops_after_context_when_other_dims_off(at):
    body = {"model_ids": ["org/m:Q4"], "objective": "fit",
            "dims": {d: {"on": False} for d in ("kv", "moe", "threads", "slots", "spec", "sampling")}}
    done, _ = _run(at, Fake(), body)
    assert [s["status"] for s in done["stages"]].count("done") == 2      # context + verify


def test_moe_bisects_when_ctx_is_below_the_floor(at):
    be = Fake(ctx_fit=16384, facts={"n_expert": 128, "n_layer": 48, "mtp_layers": 0}, oom_below_moe=6)
    body = dict(BALANCED, dims={"kv": {"on": False}, "spec": {"on": False}, "slots": {"on": False},
                                "threads": {"on": False}, "moe": {"on": True, "min": 0, "max": 16}})
    done, events = _run(at, be, body)
    st = _stage(done, "moe")
    assert st["status"] == "done" and st["choice"] == "6"
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["n-cpu-moe"]["recommended"] == "6" and "ctx-size" not in keys   # 32768 is already the configured ctx
    tried = [ev["value"] for ev in events if ev["type"] == "candidate_result" and ev["stage"] == "moe"]
    assert tried[:4] == [8, 4, 6, 5] and tried[4] == 6                   # bisect, then the curve at 6 and 8


def test_runtime_missing_skips_measured_dims_but_not_kv(at):
    done, _ = _run(at, Fake(), BALANCED, env={"runtime": False})
    st = {s["stage"]: s for s in done["stages"]}
    assert st["kv"]["status"] == "done"
    assert all(st[s]["reason"] == "runtime" for s in ("threads", "spec", "slots"))
    assert st["verify"]["status"] == "done" and done["after"].get("decode_tps") is None


def test_budget_skips_later_measured_stages(at):
    done, _ = _run(at, Fake(load_s=400.0), dict(BALANCED, budget_min=5))
    st = {s["stage"]: s for s in done["stages"]}
    assert st["context"]["status"] == "done"
    assert all(st[s]["reason"] == "budget" for s in ("kv", "threads", "spec", "slots"))
    assert st["verify"]["status"] == "done"


def test_verify_drops_slots_then_succeeds(at):
    be = Fake(verify_fail=lambda args, n: "--parallel" in args)
    done, _ = _run(at, be, BALANCED)
    assert done["verify"]["ok"] is True and done["verify"]["dropped"] == ["slots"]
    assert "parallel" not in {c["key"] for c in done["changes"]}
    assert done["after"]["concurrency"] == 1


def test_verify_fails_twice_leaves_context_and_kv_only(at):
    be = Fake(verify_fail=lambda args, n: True, facts={"n_expert": 0, "n_layer": 32, "mtp_layers": 1})
    done, _ = _run(at, be, BALANCED)
    assert done["ok"] is True and done["verify"]["ok"] is False
    assert done["verify"]["dropped"] == ["slots", "spec", "threads"]
    assert {c["key"] for c in done["changes"]} == {"ctx-size", "cache-type-k", "cache-type-v"}
    assert be.verify_calls == 2


def test_kv_lossy_candidate_is_unguarded_without_perplexity(at):
    body = dict(BALANCED, dims={"kv": {"on": True, "candidates": ["q8_0", "q4_0"]}, "spec": {"on": False},
                                "slots": {"on": False}, "threads": {"on": False}})
    done, events = _run(at, Fake(), body, env={"perplexity": False})
    q4 = next(ev for ev in events if ev["type"] == "candidate_result" and ev["stage"] == "kv" and ev["value"] == "q4_0")
    assert q4["ok"] is False and "unguarded" in q4["error"]
    assert _stage(done, "kv")["choice"] == "q8_0"


def test_kv_guard_failure_is_reported_not_chosen(at):
    body = {"model_ids": ["org/m:Q4"], "objective": "fit",
            "dims": {"kv": {"on": True, "candidates": ["q4_0"], "guard_kl_max": 0.02}, "spec": {"on": False},
                     "slots": {"on": False}, "threads": {"on": False}}}
    be = Fake()
    done, events = _run(at, be, body)
    q4 = next(ev for ev in events if ev["type"] == "candidate_result" and ev["stage"] == "kv")
    assert q4["kl"] == 0.031 and q4["guard_pass"] is False and q4["ok"] is False
    assert "failed guard" in q4["error"]
    # A rejected candidate must not be re-converged (4 wasted loads).
    assert not any(c[0] == "converge" and _val(c[1], "--cache-type-k") == "q4_0" for c in be.calls)
    assert "cache-type-k" not in {c["key"] for c in done["changes"]}
    # f16 is kept, so the recommendation itself needs no guard; the rejection is named in the text.
    assert done["guard"]["pass"] is True and done["guard"]["kl"] is None
    assert "q4_0 KL 0.031" in done["guard"]["text"]


def test_kv_guard_card_reports_the_chosen_row_when_q8_gain_is_small(at):
    class SmallKvGain(Fake):
        def _ctx(self, args):
            return int(self.ctx_fit * (1.05 if _val(args, "--cache-type-k") == "q8_0" else 1))

    body = {"model_ids": ["org/m:Q4"], "objective": "balanced",
            "dims": {"kv": {"on": True, "candidates": ["q8_0"]}, "spec": {"on": False},
                     "slots": {"on": False}, "threads": {"on": False}}}
    done, _ = _run(at, SmallKvGain(), body)
    assert _stage(done, "kv")["choice"] == "f16"
    assert "cache-type-k" not in {c["key"] for c in done["changes"]}
    assert done["guard"] == {"kl": None, "max": 0.02, "pass": True, "text": "f16 needs no guard"}


def test_slots_skipped_with_kv_unified(at):
    done, _ = _run(at, Fake(), BALANCED, section={"hf-repo": "o/r", "kv-unified": "true"})
    assert _stage(done, "slots")["reason"] == "kv_unified"


def test_spec_prefers_none_when_gain_is_small(at):
    be = Fake(spec_gain=1.03, facts={"n_expert": 0, "n_layer": 32, "mtp_layers": 1})
    done, _ = _run(at, be, BALANCED)
    assert _stage(done, "spec")["choice"] == "none"
    assert "spec-type" not in {c["key"] for c in done["changes"]}


def test_spec_with_a_current_spec_type_recommends_none_when_it_loses(at):
    be = Fake(spec_gain=1.03, facts={"n_expert": 0, "n_layer": 32, "mtp_layers": 1})
    done, _ = _run(at, be, BALANCED, section={"hf-repo": "o/r", "spec-type": "draft-mtp"})
    keys = {c["key"]: c for c in done["changes"]}
    assert keys["spec-type"]["recommended"] == "none"


def test_cancel_between_candidates(at):
    n = {"calls": 0}
    def cancelled():
        n["calls"] += 1
        return n["calls"] > 6
    done, events = _run(at, Fake(), BALANCED, cancelled=cancelled)
    assert done["ok"] is False and done["cancelled"] is True and done["stop_reason"] == "cancelled"
    assert _types(events)[-1] == "model_done"


def test_context_failure_ends_the_model(at):
    class Bad(Fake):
        def converge(self, *a, **k):
            return {"ok": False, "stop_reason": "iter_limit", "iters": 10, "load_s": 5.0}
    done, _ = _run(at, Bad(), BALANCED)
    assert done["ok"] is False and done["stop_reason"] == "iter_limit"
    assert [s["stage"] for s in done["stages"]] == ["context"]


def test_verify_limit(at):
    assert at.verify_limit(100.0, 2000.0, 1) == 20         # 1024/2000 + 256/100 = 3.07 s per request
    assert at.verify_limit(100.0, 2000.0, 4) == 64
    assert at.verify_limit(100.0, None, 1) == 20           # missing prefill assumed 2000 t/s
    assert at.verify_limit(100.0, 0, 1) == 20
    assert at.verify_limit(None, 2000.0, 1) == 4
    assert at.verify_limit(10000.0, 100000.0, 1) == 64


def test_verify_mode_runs_only_verify_against_current_section(at):
    fake = Fake()
    doc, events = _run(at, fake, {"model_ids": ["org/m:Q4"], "objective": "balanced", "mode": "verify",
                                  "baseline_tps": 120.0}, section={"ctx-size": "16384", "threads": "12", "parallel": "2"})
    assert [e["type"] for e in events][0] == "model_start"
    assert events[0]["stages"] == ["verify"]
    assert not any(c[0] == "converge" for c in fake.calls)
    assert doc["mode"] == "verify" and doc["ok"] is True
    assert doc["after"]["ctx"] == 16384 and doc["after"]["concurrency"] == 2
    assert doc["before"] == {"decode_tps": 120.0}


class BuildFake(Fake):
    def load(self, args, ctx, measure):
        res = super().load(args, ctx, measure)
        res["build"] = "b6300-434ddbb"
        return res


def test_build_printed_by_the_spawned_server_wins_over_the_cached_one(at):
    doc, _ = _run(at, BuildFake(), {"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "verify"},
                  env={"llama_build": ""})
    assert doc["llama_build"] == "b6300-434ddbb"


def test_verify_before_carries_the_full_baseline(at):
    doc, _ = _run(at, Fake(), {"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "verify",
                               "baseline": {"decode_tps": 120.0, "ctx": 16384, "free_mb": 900}},
                  section={"ctx-size": "16384"})
    assert doc["before"] == {"decode_tps": 120.0, "ctx": 16384.0, "free_mb": 900.0}
    assert doc["regressed"] is True
    assert doc["regressed"] is True            # Fake decodes ~92 t/s at 2 slots, 120 × 0.85 = 102
    assert doc["llama_build"] == "b10850-abc"
    assert doc["changes"] == []


def test_verify_measurement_is_sized_from_the_baseline_not_the_floor(at):
    fake = Fake()
    _run(at, fake, {"model_ids": ["org/m:Q4"], "objective": "balanced", "mode": "verify",
                    "baseline_tps": 120.0}, section={"ctx-size": "16384", "parallel": "2"})
    measure = next(c[3] for c in fake.calls if c[0] == "load" and c[3])
    assert measure["limit"] == at.verify_limit(120.0, None, 2) == 46      # floor would be 4


def test_verify_mode_reads_quoted_and_aliased_context_keys(at):
    doc, _ = _run(at, Fake(), {"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "verify"},
                  section={"c": '"16384"', "np": '"2"'})
    assert doc["after"]["ctx"] == 16384 and doc["after"]["concurrency"] == 2


def test_verify_mode_without_baseline_is_never_regressed(at):
    doc, _ = _run(at, Fake(), {"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "verify"})
    assert doc["ok"] is True and doc["regressed"] is None


def test_verify_mode_zero_baseline_is_computed_not_skipped(at):
    doc, _ = _run(at, Fake(), {"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "verify",
                               "baseline_tps": 0.0})
    assert doc["ok"] is True and doc["regressed"] is False


class KLFake(Fake):
    def __init__(self, kl=0.01, base_ok=True, stats=None):
        super().__init__(); self.kl_v, self.base_ok, self.kl_calls = kl, base_ok, []
        self.stats = stats
    def kl(self, args, write_base):
        self.kl_calls.append((list(args), write_base))
        if write_base:
            return {"ok": self.base_ok, "kl": None, "stats": None, "error": None if self.base_ok else "boom"}
        return {"ok": True, "kl": self.kl_v, "stats": self.stats, "error": None}


def test_quality_run_scores_overrides_against_f16_base(at):
    fake = KLFake(kl=0.015)
    events = []
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                               "overrides": {"cache-type-k": "q4_0", "cache-type-v": "q4_0"}, "kl_max": 0.02})
    doc = at.run_quality("org/m:Q4", {"ctx-size": "8192", "cache-type-k": "q8_0"}, req, fake, events.append,
                         lambda: False, {"run_id": "q1", "valued": set(), "llama_build": "b1"})
    base_args, cand_args = fake.kl_calls[0][0], fake.kl_calls[1][0]
    assert fake.kl_calls[0][1] is True and fake.kl_calls[1][1] is False
    assert base_args[base_args.index("--cache-type-k") + 1] == "f16"
    assert cand_args[cand_args.index("--cache-type-k") + 1] == "q4_0"
    assert "--ctx-size" not in base_args                      # tuner-owned keys never reach perplexity
    assert doc["type"] == "model_done" and doc["mode"] == "quality" and doc["ok"] is True
    assert doc["guard"] == {"kl": 0.015, "kl_max": 0.02, "pass": True, "error": None, "stats": None}
    assert doc["changes"] == [{"key": "cache-type-k", "current": "q8_0", "recommended": "q4_0"},
                              {"key": "cache-type-v", "current": None, "recommended": "q4_0"}]
    types = [e["type"] for e in events]
    assert types == ["model_start", "stage_start", "candidate_start", "candidate_result",
                     "candidate_start", "candidate_result", "stage_done", "model_done"]
    assert events[1]["stage"] == "quality" and events[1]["candidates"] == ["f16 base", "candidate"]
    assert doc["llama_build"] == "b1"


def test_quality_run_reads_current_from_either_key_spelling(at):
    fake = KLFake(kl=0.01)
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                               "overrides": {"ctv": "q4_0"}, "kl_max": 0.02})
    assert req["overrides"] == {"cache-type-v": "q4_0"}
    doc = at.run_quality("org/m:Q4", {"ctv": "f16"}, req, fake, lambda *_: None,
                         lambda: False, {"run_id": "q1", "valued": set(), "llama_build": "b1"})
    assert doc["changes"] == [{"key": "cache-type-v", "current": "f16", "recommended": "q4_0"}]
    cand = fake.kl_calls[1][0]
    assert cand.count("--cache-type-v") == 1 and "-ctv" not in cand


def test_quality_run_fails_when_base_fails_and_flags_kl_over_max(at):
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                               "overrides": {"threads": "8"}, "kl_max": 0.01})
    doc = at.run_quality("org/m:Q4", {}, req, KLFake(base_ok=False), lambda m: None, lambda: False, {"run_id": "q2"})
    assert doc["ok"] is False and doc["guard"]["error"] == "KL base failed: boom"
    doc = at.run_quality("org/m:Q4", {}, req, KLFake(kl=0.03), lambda m: None, lambda: False, {"run_id": "q3"})
    assert doc["ok"] is True and doc["guard"]["pass"] is False and doc["guard"]["kl"] == 0.03


def test_quality_guard_carries_the_backend_statistics(at):
    stats = {"kl": 0.0054, "same_top_p": 98.118, "rms_dp": 3.236, "p999_dp": 17.595, "max_dp": 23.904}
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                               "overrides": {"cache-type-k": "q4_0"}, "kl_max": 0.02})
    doc = at.run_quality("org/m:Q4", {}, req, KLFake(kl=0.0054, stats=stats), lambda m: None,
                         lambda: False, {"run_id": "q5"})
    assert doc["guard"]["stats"] == stats and doc["guard"]["pass"] is True
    # An older agent that reports no statistics still produces a usable guard.
    doc = at.run_quality("org/m:Q4", {}, req, KLFake(kl=0.0054), lambda m: None,
                         lambda: False, {"run_id": "q6"})
    assert doc["guard"]["stats"] is None and doc["guard"]["kl"] == 0.0054


def test_quality_run_honours_cancel(at):
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                               "overrides": {"threads": "8"}})
    doc = at.run_quality("org/m:Q4", {}, req, KLFake(), lambda m: None, lambda: True, {"run_id": "q4"})
    assert doc["ok"] is False and doc["cancelled"] is True and doc["stop_reason"] == "cancelled"
