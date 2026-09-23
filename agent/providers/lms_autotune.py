"""LM Studio autotune (#916): request validation, stage engine and choice rules over the
native load-config keys. Pure stdlib; lms_tools.py owns HTTP, processes and routes."""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from . import llama_autotune as _at

OBJECTIVES = ("fit", "speed", "balanced", "serve")
MODES = ("tune", "verify")
STAGES = ("context", "flash", "batch", "kvgpu", "spec", "slots", "verify")
MEASURED = ("flash", "batch", "kvgpu", "spec", "slots", "verify")
FALLBACK_ORDER = ("slots", "spec", "batch")
SPEC_TYPES = ("auto", "mtp", "draft", "none")
BATCH_SIZES = (64, 128, 256, 512, 1024, 2048, 4096)
CTX_LADDER = (2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576)
MIN_CTX, MAX_CTX = 512, 4_194_304
# Native load keys the tuner may recommend, in the order the change table lists them.
CONFIG_KEYS = ("context_length", "flash_attention", "eval_batch_size", "offload_kv_cache_to_gpu",
               "speculative_draft_mtp", "speculative_draft_simple", "speculative_draft_model",
               "speculative_draft_min_tokens", "speculative_draft_max_tokens",
               "speculative_draft_min_continue_probability", "parallel")
SPEC_KEYS = CONFIG_KEYS[4:10]
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:/\-]{0,199}$")
DIM_KEYS = {"slots": ("parallel",), "spec": SPEC_KEYS, "batch": ("eval_batch_size",),
            "flash": ("flash_attention",), "kvgpu": ("offload_kv_cache_to_gpu",)}
BASELINE_KEYS = ("decode_tps", "prefill_tps", "agg_tps", "ctx", "free_mb")
GAIN_MIN_PCT = _at.GAIN_MIN_PCT
SPEC_MIN_GAIN_PCT = _at.SPEC_MIN_GAIN_PCT
STICK_LIMIT = _at.STICK_LIMIT
# A context probe is read under one warm-up request; serving traffic costs about this much more.
TRAFFIC_MARGIN_MB = 512

DEFAULT_DIMS: dict = {
    "context":  {"on": True, "target_mb": 2048, "tolerance_mb": 256, "min_ctx": 4096},
    "flash":    {"on": True, "candidates": [True, False]},
    "batch":    {"on": True, "candidates": [512, 1024, 2048]},
    "kvgpu":    {"on": True, "candidates": [True, False]},
    "spec":     {"on": True, "types": ["auto"], "draft_model": "auto", "n_min": 0, "n_max": 16, "p_min": 0.75},
    "slots":    {"on": True, "candidates": [1, 2, 4, 8], "min_ctx_per_slot": 8192},
}


class Cancelled(Exception):
    pass


def _bool_list(v: Any, name: str) -> list:
    if not isinstance(v, list) or not v or len(v) > 2 or any(not isinstance(x, bool) for x in v):
        raise ValueError(f"{name} must be a list of booleans")
    return list(dict.fromkeys(v))


def validate_request(body: dict) -> dict:
    ids = body.get("model_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    ids = [str(m).strip() for m in ids if str(m).strip()]
    if not ids or any(not _MODEL_RE.match(m) for m in ids):
        raise ValueError("model_ids required")
    objective = str(body.get("objective") or "balanced")
    if objective not in OBJECTIVES:
        raise ValueError("objective must be one of " + ", ".join(OBJECTIVES))
    budget = _at._int(body.get("budget_min", 120), "budget_min", 5, 600)
    given = body.get("dims") or {}
    if not isinstance(given, dict):
        raise ValueError("dims must be an object")
    dims = {k: dict(v) for k, v in DEFAULT_DIMS.items()}
    for name, d in dims.items():
        g = given.get(name) or {}
        if not isinstance(g, dict):
            raise ValueError(f"dims.{name} must be an object")
        if "on" in g:
            d["on"] = bool(g["on"])
        if name == "context":
            if "target_mb" in g:
                d["target_mb"] = _at._int(g["target_mb"], "context.target_mb", 0, 1_000_000)
            if "tolerance_mb" in g:
                d["tolerance_mb"] = max(1, _at._int(g["tolerance_mb"], "context.tolerance_mb", -1, 1_000_000))
            if "min_ctx" in g:
                d["min_ctx"] = _at._int(g["min_ctx"], "context.min_ctx", MIN_CTX, MAX_CTX)
        elif name in ("flash", "kvgpu"):
            if "candidates" in g:
                d["candidates"] = _bool_list(g["candidates"], f"{name}.candidates")
        elif name == "batch":
            if "candidates" in g:
                c = sorted({_at._int(x, "batch.candidates", 1, 65536) for x in _at._list(g["candidates"], "batch.candidates")})
                d["candidates"] = c
        elif name == "slots":
            if "candidates" in g:
                d["candidates"] = sorted({_at._int(x, "slots.candidates", 1, 64) for x in _at._list(g["candidates"], "slots.candidates")})
            if "min_ctx_per_slot" in g:
                d["min_ctx_per_slot"] = _at._int(g["min_ctx_per_slot"], "slots.min_ctx_per_slot", 512, MAX_CTX)
        elif name == "spec":
            if "types" in g:
                t = [str(x) for x in _at._list(g["types"], "spec.types")]
                if any(x not in SPEC_TYPES for x in t):
                    raise ValueError("spec.types must be from " + ", ".join(SPEC_TYPES))
                d["types"] = list(dict.fromkeys(t))
            if "draft_model" in g:
                dm = str(g["draft_model"] or "auto").strip()
                if dm not in ("auto", "none") and not _MODEL_RE.match(dm):
                    raise ValueError("spec.draft_model must be auto, none, or a model key")
                d["draft_model"] = dm
            if "n_min" in g:
                d["n_min"] = _at._int(g["n_min"], "spec.n_min", 0, 64)
            if "n_max" in g:
                d["n_max"] = _at._int(g["n_max"], "spec.n_max", 1, 64)
            if d["n_min"] >= d["n_max"]:
                raise ValueError("spec.n_min must be below spec.n_max")
            if "p_min" in g:
                d["p_min"] = _at._float(g["p_min"], "spec.p_min", 0.0, 1.0)
    out: dict = {"model_ids": ids, "objective": objective, "budget_min": budget, "dims": dims}
    mode = str(body.get("mode") or "tune")
    if mode not in MODES:
        raise ValueError("mode must be one of " + ", ".join(MODES))
    out["mode"] = mode
    if mode == "verify":
        for name, d in dims.items():
            if name != "context":
                d["on"] = False
        raw = body.get("baseline")
        base: dict = {}
        if isinstance(raw, dict):
            for k in BASELINE_KEYS:
                if raw.get(k) is not None:
                    base[k] = _at._float(raw[k], "baseline." + k, 0.0, 1e9)
        bt = body.get("baseline_tps")
        if bt is None:
            bt = base.get("decode_tps")
        out["baseline_tps"] = None if bt is None else _at._float(bt, "baseline_tps", 0.0, 1e6)
        if out["baseline_tps"] is not None:
            base["decode_tps"] = out["baseline_tps"]
        out["baseline"] = base
    return out


def cfg_str(v: Any) -> str:
    """Config value as the change table shows it: booleans lower-case, numbers plain."""
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def cfg_typed(v: Any) -> Any:
    """Inverse of cfg_str for a value the frontend sends back."""
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    s = str(v).strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def ctx_candidates(min_ctx: int, max_ctx: Optional[int], current: Optional[int]) -> list[int]:
    """Ladder of context lengths to bisect over: powers of two within [min_ctx, max_ctx] plus the current one."""
    hi = int(max_ctx or CTX_LADDER[-1])
    vals = {c for c in CTX_LADDER if min_ctx <= c <= hi}
    if current and min_ctx <= int(current) <= hi:
        vals.add(int(current))
    if hi >= min_ctx:
        vals.add(hi)
    return sorted(vals)


def bisect_largest(cands: list, fits: Callable[[int], bool]) -> tuple[Optional[int], list]:
    """Largest candidate that fits, assuming fits(c) is monotonic (true below some size)."""
    tried: list = []
    lo, hi, best = 0, len(cands) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        ok = bool(fits(cands[mid]))
        tried.append((cands[mid], ok))
        if ok:
            best = cands[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return best, tried


def find_draft(models: list, target_key: str, target_size: int) -> Optional[dict]:
    """Smallest downloaded LLM of the same family at most 1/4 of the target's bytes."""
    fam = _at.family_prefix(target_key.split("@")[0])
    cands = [m for m in (models or []) if isinstance(m, dict) and m.get("key") and m.get("key") != target_key
             and (m.get("type") or "llm") == "llm"
             and _at.family_prefix(str(m.get("key")).split("@")[0]) == fam
             and 0 < int(m.get("size_bytes") or 0) <= int(target_size or 0) / 4]
    return min(cands, key=lambda m: int(m.get("size_bytes") or 0)) if cands else None


def choose_best(results: list, metric: str, current: Any, min_gain: float = GAIN_MIN_PCT) -> tuple[Any, str, Optional[float]]:
    """Fastest candidate on `metric`, kept only when it beats the current value by min_gain; else the current one."""
    ok = [r for r in results if r.get("ok") and (r.get(metric) or 0) > 0]
    if not ok:
        return None, "no candidate loaded", None
    best = max(ok, key=lambda r: r[metric] or 0)
    cur = next((r for r in ok if r["value"] == current), None)
    if cur is None:
        return best["value"], f"{best[metric]:.1f} t/s", None
    g = _at.gain_pct(best[metric], cur[metric])
    if best["value"] != current and g is not None and g >= min_gain:
        return best["value"], f"+{g:.0f} % over {cfg_str(current)}", g
    return current, f"no candidate gained {min_gain:.0f} % over {cfg_str(current)}", None


def choose_spec(results: list) -> tuple[Any, str, Optional[float]]:
    none = next((r for r in results if r["value"] == "none" and r.get("ok")), None)
    base = (none or {}).get("decode_tps") or 0
    ok = [r for r in results if r.get("ok") and r["value"] != "none" and (r.get("decode_tps") or 0) > 0]
    if not ok:
        return "none", "no speculative setup loaded", None
    best = max(ok, key=lambda r: r["decode_tps"] or 0)
    g = _at.gain_pct(best["decode_tps"], base)
    if base and g is not None and g >= SPEC_MIN_GAIN_PCT:
        acc = best.get("accept")
        return best["value"], f"+{g:.0f} % decode" + (f" · accept {acc * 100:.0f} %" if isinstance(acc, (int, float)) else ""), g
    if not base:
        return best["value"], f"{best['decode_tps']:.1f} t/s · plain decode did not load", None
    return "none", f"no setup gained {SPEC_MIN_GAIN_PCT:.0f} % over plain decode", None


def estimate(stage: str, n: int, load_s: float, stick_s: float) -> int:
    n = max(1, int(n))
    per = {"context": 4 * load_s + stick_s, "flash": n * (load_s + stick_s), "batch": n * (load_s + stick_s),
           "kvgpu": n * (load_s + stick_s), "spec": n * (load_s + stick_s), "slots": n * (load_s + stick_s),
           "verify": load_s + 60}
    return int(round(per.get(stage, load_s)))


def build_changes(current: dict, rec: dict, evidence: dict) -> list[dict]:
    rows = []
    keys = [k for k in CONFIG_KEYS if k in rec] + [k for k in rec if k not in CONFIG_KEYS]
    for k in keys:
        cur, new = cfg_str((current or {}).get(k)), cfg_str(rec[k])
        if cur == new:
            continue
        ev = (evidence or {}).get(k) or {}
        g = ev.get("gain_pct")
        rows.append({"key": k, "current": cur, "recommended": new, "value": rec[k],
                     "source": ev.get("source", "measured"), "evidence": ev.get("text", ""),
                     "selected": g is None or g >= GAIN_MIN_PCT})
    return rows


class _Run:
    """Stage engine for one model. backend: current() → {loaded, config, max_ctx, size_bytes, free_mb};
    load(config, measure) → {ok, error, config, ctx, free_mb, load_s, stick}; drafts() → native model rows."""

    def __init__(self, model_id, req, backend, put, cancelled, env, clock):
        self.model_id, self.req, self.backend = model_id, req, backend
        self.put, self.cancelled, self.env, self.clock = put, cancelled, env, clock
        self.dims, self.objective = req["dims"], req["objective"]
        self.mode = req.get("mode") or "tune"
        self.budget_s = int(req["budget_min"]) * 60
        self.runtime = bool(env.get("runtime"))
        self.base: dict = {}          # config the model was loaded with before the run
        self.was_loaded = False
        self.max_ctx: Optional[int] = None
        self.size_bytes = 0
        self.rec: dict = {}
        self.evidence: dict = {}
        self.stages: list = []
        self.before: Optional[dict] = None
        self.after: Optional[dict] = None
        self.verify: Optional[dict] = None
        self.ctx_total: Optional[int] = None
        self.free_mb: Optional[int] = None
        self.load_s, self.stick_s = 30.0, 30.0
        self.decode_now: Optional[float] = None
        self.prefill_now: Optional[float] = None
        self.concurrency = 1
        self.loads = 0
        self.budget_hit = False
        self.stop_reason: Optional[str] = None
        self.regressed: Optional[bool] = None
        self.t0 = clock()

    # ── plumbing ──
    def elapsed(self) -> float:
        return self.clock() - self.t0

    def emit(self, typ: str, **kw) -> None:
        self.put({"type": typ, "model_id": self.model_id, **kw})

    def cur(self, key: str, default: Any = None) -> Any:
        v = self.rec.get(key, self.base.get(key))
        return default if v is None else v

    def config(self, extra: Optional[dict] = None) -> dict:
        cfg = {k: v for k, v in self.base.items() if k in CONFIG_KEYS}
        cfg.update(self.rec)
        if self.ctx_total:
            cfg["context_length"] = int(self.ctx_total)
        cfg.update(extra or {})
        return {k: v for k, v in cfg.items() if v is not None and v != ""}

    def check_cancel(self) -> None:
        if self.cancelled():
            raise Cancelled()

    def begin(self, stage: str, candidates: list, est: float) -> dict:
        self.emit("stage_start", stage=stage, candidates=[cfg_str(c) if isinstance(c, bool) else c for c in candidates],
                  est_s=int(est))
        return {"t": self.elapsed(), "loads": self.loads}

    def end(self, stage: str, mark: dict, choice: Any, reason: str, warning: Optional[str] = None) -> None:
        sec = int(round(self.elapsed() - mark["t"]))
        loads = self.loads - mark["loads"]
        extra = {"warning": warning} if warning else {}
        text = None if choice is None else cfg_str(choice)
        self.stages.append({"stage": stage, "status": "done", "seconds": sec, "loads": loads, "choice": text, **extra})
        self.emit("stage_done", stage=stage, choice=text, reason=reason, seconds=sec, loads=loads, **extra)

    def skip(self, stage: str, reason: str) -> None:
        self.stages.append({"stage": stage, "status": "skipped", "reason": reason})
        self.emit("stage_skipped", stage=stage, reason=reason)

    def on(self, stage: str) -> bool:
        if self.dims.get(stage, {}).get("on"):
            return True
        self.skip(stage, "off")
        return False

    def need_runtime(self, stage: str) -> bool:
        if self.runtime:
            return True
        self.skip(stage, "runtime")
        return False

    def within_budget(self, stage: str, est: float) -> bool:
        if self.budget_hit or not _at.budget_ok(self.elapsed(), est, self.budget_s):
            self.budget_hit = True
            self.skip(stage, "budget")
            return False
        return True

    def load(self, extra: dict, measure: Optional[dict]) -> dict:
        self.loads += 1
        res = self.backend.load(self.config(extra), measure) or {}
        if res.get("load_s"):
            self.load_s = float(res["load_s"])
        if res.get("free_mb") is not None:
            self.free_mb = res["free_mb"]
        return res

    def measure(self, extra: dict, concurrency: int = 1, limit: int = STICK_LIMIT) -> tuple[dict, dict]:
        m = {"concurrency": int(concurrency), "limit": int(limit)} if self.runtime else None
        res = self.load(extra, m)
        st = res.get("stick") or {}
        if st.get("seconds"):
            self.stick_s = float(st["seconds"])
        return res, st

    def result(self, stage: str, value: Any, res: dict, **extra) -> None:
        st = res.get("stick") or {}
        self.emit("candidate_result", stage=stage, value=cfg_str(value) if isinstance(value, bool) else value,
                  ok=bool(res.get("ok")) and (not st or bool(st.get("ok"))),
                  ctx=res.get("ctx"), free_mb=res.get("free_mb"), decode_tps=st.get("decode_tps"),
                  prefill_tps=st.get("prefill_tps"), agg_tps=st.get("agg_tps"), accept=st.get("accept"),
                  error=res.get("error") or st.get("error"), **extra)

    def ev(self, key: str, text: str, gain: Optional[float] = None) -> None:
        self.evidence[key] = {"source": "measured", "text": text, "gain_pct": gain}

    def sweep(self, stage: str, key: str, cands: list, metric: str = "decode_tps") -> None:
        """Measure one value per candidate and keep the fastest when it beats the current value."""
        if not self.on(stage) or not self.need_runtime(stage):
            return
        current = self.cur(key)
        cands = list(dict.fromkeys(cands))
        est = estimate(stage, len(cands), self.load_s, self.stick_s)
        if not self.within_budget(stage, est):
            return
        mark = self.begin(stage, cands, est)
        results = []
        for c in cands:
            self.check_cancel()
            self.emit("candidate_start", stage=stage, value=cfg_str(c) if isinstance(c, bool) else c)
            res, st = self.measure({key: c})
            results.append({"value": c, "ok": bool(res.get("ok")) and bool(st.get("ok")),
                            "decode_tps": st.get("decode_tps"), "prefill_tps": st.get("prefill_tps"),
                            "agg_tps": st.get("agg_tps")})
            self.result(stage, c, res)
        choice, reason, gain = choose_best(results, metric, current)
        if choice is None:
            self.end(stage, mark, None, reason)
            return
        if choice != current:
            self.rec[key] = choice
            self.ev(key, reason, gain)
        self.end(stage, mark, choice, reason)

    # ── stages ──
    def stage_context(self) -> bool:
        d = self.dims["context"]
        target, tol = int(d["target_mb"]), int(d["tolerance_mb"])
        info = self.backend.current() or {}
        self.was_loaded = bool(info.get("loaded"))
        self.base = {k: v for k, v in (info.get("config") or {}).items() if k in CONFIG_KEYS}
        self.max_ctx = info.get("max_ctx")
        self.size_bytes = int(info.get("size_bytes") or 0)
        cur_ctx = self.base.get("context_length")
        cands = ctx_candidates(int(d["min_ctx"]), self.max_ctx, cur_ctx)
        n_fit = len(cands).bit_length() if self.objective == "fit" else 0
        mark = self.begin("context", (["current"] + cands) if n_fit else ["current"],
                          estimate("context", n_fit + 1, self.load_s, self.stick_s))
        self.check_cancel()
        self.emit("candidate_start", stage="context", value="current")
        res, st = self.measure({})
        self.result("context", "current", res)
        if not res.get("ok"):
            self.stop_reason = res.get("error") or "the model did not load with its current settings"
            self.end("context", mark, None, self.stop_reason)
            return False
        cur_ctx = int(res.get("ctx") or cur_ctx or 0) or None
        self.base = {k: v for k, v in (res.get("config") or self.base).items() if k in CONFIG_KEYS}
        self.ctx_total = cur_ctx
        self.decode_now, self.prefill_now = st.get("decode_tps"), st.get("prefill_tps")
        self.before = {"decode_tps": st.get("decode_tps"), "prefill_tps": st.get("prefill_tps"),
                       "agg_tps": st.get("agg_tps"), "ctx": cur_ctx, "free_mb": res.get("free_mb"), "concurrency": 1}
        cur_free = res.get("free_mb")
        cur_fits = cur_free is None or int(cur_free) >= target - tol
        # Outside Fit the loaded context is only verified; the search runs when it misses the target.
        if self.objective != "fit" and cur_fits:
            self.end("context", mark, cur_ctx, "kept as loaded" + (f" · {int(cur_free)} MB free" if cur_free is not None else ""))
            return True
        if self.objective != "fit":
            self.emit("line", text=f"[autotune] loaded context leaves {int(cur_free)} MB free, under the {target - tol} MB target; searching")

        def fits(ctx: int) -> bool:
            self.check_cancel()
            self.emit("candidate_start", stage="context", value=ctx)
            r = self.load({"context_length": ctx}, None)
            free = r.get("free_mb")
            ok = bool(r.get("ok")) and (free is None or int(free) - TRAFFIC_MARGIN_MB >= target - tol)
            self.result("context", ctx, r, fits=ok, margin_mb=TRAFFIC_MARGIN_MB)
            return ok

        search = [c for c in cands if c != cur_ctx]
        if not search:
            self.end("context", mark, cur_ctx, "already the only context length available")
            return True
        best, tried = bisect_largest(search, fits)
        if best is None and cur_fits:
            best = cur_ctx
        if best is None:
            best = min(search)
            self.emit("line", text=f"[autotune] no context length left {target} MB free; keeping the smallest, {best}")
        if cur_fits and cur_ctx and (best is None or cur_ctx > best):
            best = cur_ctx
        self.ctx_total = best
        if best != cur_ctx:
            self.rec["context_length"] = int(best)
            self.ev("context_length", f"largest context that leaves ≥ {target - tol} MB free with a {TRAFFIC_MARGIN_MB} MB traffic margin")
        self.end("context", mark, best, f"{len(tried)} loads · ≥ {target - tol} MB free · probes carry a {TRAFFIC_MARGIN_MB} MB traffic margin")
        return True

    def stage_flash(self) -> None:
        self.sweep("flash", "flash_attention", list(self.dims["flash"]["candidates"]))

    def stage_batch(self) -> None:
        self.sweep("batch", "eval_batch_size", [int(c) for c in self.dims["batch"]["candidates"]], metric="prefill_tps")

    def stage_kvgpu(self) -> None:
        self.sweep("kvgpu", "offload_kv_cache_to_gpu", list(self.dims["kvgpu"]["candidates"]))

    def spec_rows(self) -> list[dict]:
        d = self.dims["spec"]
        types = list(d["types"])
        draft = None
        if d["draft_model"] != "none":
            if d["draft_model"] == "auto":
                hit = find_draft(self.backend.drafts() or [], self.model_id, self.size_bytes)
                draft = hit["key"] if hit else None
            else:
                draft = d["draft_model"]
        order = ["mtp", "draft"] if "auto" in types else [t for t in types if t in ("mtp", "draft")]
        if "auto" in types:
            order += [t for t in types if t not in ("auto", "none") and t not in order]
        rows = [{"type": "none", "cfg": {"speculative_draft_mtp": False, "speculative_draft_simple": False,
                                         "speculative_draft_model": ""}}]
        for t in order:
            if t == "mtp":
                rows.append({"type": "mtp", "cfg": {"speculative_draft_mtp": True, "speculative_draft_simple": False,
                                                    "speculative_draft_model": ""}})
            elif t == "draft" and draft:
                rows.append({"type": "draft", "cfg": {"speculative_draft_mtp": False, "speculative_draft_simple": True,
                                                      "speculative_draft_model": draft}})
        return rows

    def stage_spec(self) -> None:
        if not self.on("spec") or not self.need_runtime("spec"):
            return
        d = self.dims["spec"]
        rows = self.spec_rows()
        if len(rows) < 2:
            self.skip("spec", "no_candidates")
            return
        windows = [m for m in (4, 8, 16) if int(d["n_min"]) < m <= int(d["n_max"])]
        est = estimate("spec", len(rows) + max(0, len(windows) - 1), self.load_s, self.stick_s)
        if not self.within_budget("spec", est):
            return
        mark = self.begin("spec", [r["type"] for r in rows], est)
        win = {"speculative_draft_min_tokens": int(d["n_min"]), "speculative_draft_max_tokens": int(d["n_max"]),
               "speculative_draft_min_continue_probability": float(d["p_min"])}
        results = []
        for r in rows:
            self.check_cancel()
            self.emit("candidate_start", stage="spec", value=r["type"])
            extra = dict(r["cfg"])
            if r["type"] != "none":
                extra.update(win)
            res, st = self.measure(extra)
            results.append({"value": r["type"], "ok": bool(res.get("ok")) and bool(st.get("ok")),
                            "decode_tps": st.get("decode_tps"), "accept": st.get("accept"), "cfg": extra})
            self.result("spec", r["type"], res)
        choice, reason, gain = choose_spec(results)
        best = next((r for r in results if r["value"] == choice), None)
        if best is None:
            self.end("spec", mark, None, reason)
            return
        chosen = dict(best["cfg"])
        if choice != "none" and len(windows) > 1:
            # Draft window: shorter maximum draft lengths, keep the fastest.
            wres = [{"value": int(d["n_max"]), "ok": True, "decode_tps": best["decode_tps"]}]
            for m in windows:
                if m == int(d["n_max"]):
                    continue
                self.check_cancel()
                self.emit("candidate_start", stage="spec", value=f"{choice} · window {m}")
                extra = dict(chosen, speculative_draft_max_tokens=m)
                res, st = self.measure(extra)
                wres.append({"value": m, "ok": bool(res.get("ok")) and bool(st.get("ok")), "decode_tps": st.get("decode_tps")})
                self.result("spec", f"{choice} · window {m}", res)
            wchoice, wreason, _g = choose_best(wres, "decode_tps", int(d["n_max"]))
            if wchoice is not None:
                chosen["speculative_draft_max_tokens"] = int(wchoice)
                reason += f" · window {wchoice} ({wreason})"
        for k, v in chosen.items():
            if cfg_str(self.base.get(k)) != cfg_str(v):
                self.rec[k] = v
                self.ev(k, reason, gain)
        self.end("spec", mark, choice, reason)

    def stage_slots(self) -> None:
        if not self.on("slots") or not self.need_runtime("slots"):
            return
        d = self.dims["slots"]
        cap = max(1, int(self.ctx_total or 0) // int(d["min_ctx_per_slot"]))
        cands = [int(c) for c in d["candidates"] if int(c) <= cap] or [1]
        est = estimate("slots", len(cands), self.load_s, self.stick_s)
        if not self.within_budget("slots", est):
            return
        mark = self.begin("slots", cands, est)
        results = []
        for c in cands:
            self.check_cancel()
            self.emit("candidate_start", stage="slots", value=c)
            res, st = self.measure({"parallel": c}, concurrency=c, limit=max(STICK_LIMIT, 2 * c))
            results.append({"value": c, "ok": bool(res.get("ok")) and bool(st.get("ok")),
                            "decode_tps": st.get("decode_tps"), "agg_tps": st.get("agg_tps")})
            self.result("slots", c, res)
        choice, reason = _at.choose_slots(self.objective, results)
        if choice is None:
            self.end("slots", mark, None, reason)
            return
        one = next((r for r in results if int(r["value"]) == 1 and r["ok"]), None)
        best = next(r for r in results if int(r["value"]) == choice)
        if cfg_str(choice) != cfg_str(self.cur("parallel", 1)):
            self.rec["parallel"] = int(choice)
            self.ev("parallel", reason + f" · {int(self.ctx_total or 0) // choice} ctx per slot",
                    _at.gain_pct(best.get("agg_tps"), (one or {}).get("agg_tps")))
        self.concurrency = int(choice)
        self.end("slots", mark, choice, reason)

    def drop_dim(self, dim: str) -> bool:
        had = False
        for k in DIM_KEYS.get(dim, ()):
            if k in self.rec:
                had = True
                self.rec.pop(k, None)
                self.evidence.pop(k, None)
        if dim == "slots":
            self.concurrency = 1
        return had

    def stage_verify(self) -> None:
        mark = self.begin("verify", ["recommended set"], estimate("verify", 1, self.load_s, self.stick_s))
        target, tol = int(self.dims["context"]["target_mb"]), int(self.dims["context"]["tolerance_mb"])
        if self.mode == "verify":
            target = 0
        dropped: list = []
        for attempt in (1, 2):
            self.check_cancel()
            conc = self.concurrency
            limit = _at.verify_limit(self.decode_now, self.prefill_now, conc)
            self.emit("candidate_start", stage="verify", value=attempt)
            res, st = self.measure({}, concurrency=conc, limit=limit)
            self.result("verify", attempt, res)
            free = res.get("free_mb")
            free_ok = free is None or int(free) >= target - tol
            soft = not free_ok and free is not None and int(free) >= 0.5 * target
            ok = bool(res.get("ok")) and (not self.runtime or bool(st.get("ok"))) and (free_ok or soft)
            if ok:
                warning = None
                if soft:
                    warning = f"free memory {int(free)} MB is below the {target} ± {tol} MB target"
                    self.emit("line", text=f"[autotune] {warning}")
                self.after = {"decode_tps": st.get("decode_tps"), "prefill_tps": st.get("prefill_tps"),
                              "ctx": res.get("ctx") or self.ctx_total, "agg_tps": st.get("agg_tps"), "free_mb": free,
                              "concurrency": conc, "accept": st.get("accept")}
                self.verify = {"ok": True, "seconds": st.get("seconds") or 0, "free_mb": free, "dropped": dropped,
                               "reason": None, "warning": warning}
                self.end("verify", mark, "pass", f"{conc} slot(s) · {limit} requests" if self.runtime else "load only")
                return
            reason = res.get("error") or st.get("error") or ("free memory below target" if not free_ok else "load failed")
            if attempt == 1:
                nxt = next((s for s in FALLBACK_ORDER if self.drop_dim(s)), None)
                if nxt:
                    dropped.append(nxt)
                    self.emit("line", text=f"[autotune] verify failed ({reason}) — dropping {nxt} and retrying")
                    continue
            for s in FALLBACK_ORDER:
                if self.drop_dim(s):
                    dropped.append(s)
            self.after = {"ctx": self.ctx_total, "free_mb": free, "concurrency": 1}
            self.verify = {"ok": False, "seconds": 0, "free_mb": free, "dropped": dropped, "reason": reason,
                           "warning": None}
            self.end("verify", mark, "fail", reason)
            return

    # ── driver ──
    def run(self) -> dict:
        if self.mode == "verify":
            return self.run_verify_only()
        planned = [s for s in STAGES if s in ("context", "verify") or self.dims.get(s, {}).get("on")]
        self.emit("model_start", objective=self.objective, mode=self.mode, stages=planned, provider="lms",
                  budget_min=self.req["budget_min"], target_mb=self.dims["context"]["target_mb"],
                  tolerance_mb=self.dims["context"]["tolerance_mb"])
        ok = cancelled = False
        try:
            ok = self.stage_context()
            if ok:
                for fn in (self.stage_flash, self.stage_batch, self.stage_kvgpu, self.stage_spec,
                           self.stage_slots, self.stage_verify):
                    self.check_cancel()
                    fn()
        except Cancelled:
            cancelled, ok = True, False
            self.stop_reason = "cancelled"
        return self.done(ok, cancelled)

    def run_verify_only(self) -> dict:
        self.emit("model_start", objective=self.objective, mode="verify", stages=["verify"], provider="lms",
                  budget_min=self.req["budget_min"], target_mb=self.dims["context"]["target_mb"],
                  tolerance_mb=self.dims["context"]["tolerance_mb"])
        info = self.backend.current() or {}
        self.was_loaded = bool(info.get("loaded"))
        self.base = {k: v for k, v in (info.get("config") or {}).items() if k in CONFIG_KEYS}
        self.ctx_total = int(self.base.get("context_length") or 0) or None
        try:
            self.concurrency = max(1, int(self.base.get("parallel") or 1))
        except (TypeError, ValueError):
            self.concurrency = 1
        base = self.req.get("baseline_tps")
        self.before = dict(self.req.get("baseline") or {}) or None
        self.decode_now = base
        ok = cancelled = False
        try:
            self.stage_verify()
            ok = bool((self.verify or {}).get("ok"))
        except Cancelled:
            cancelled = True
            self.stop_reason = "cancelled"
        after = (self.after or {}).get("decode_tps")
        if ok and base is not None and after is not None:
            self.regressed = float(after) < 0.85 * float(base)
        return self.done(ok, cancelled)

    def done(self, ok: bool, cancelled: bool) -> dict:
        changes = build_changes(self.base, self.rec, self.evidence)
        if self.after is None and self.ctx_total:
            self.after = {"ctx": self.ctx_total, "free_mb": self.free_mb, "concurrency": 1}
        doc = {"type": "model_done", "model_id": self.model_id, "run_id": self.env.get("run_id"), "provider": "lms",
               "ok": bool(ok) and not cancelled, "cancelled": cancelled, "objective": self.objective,
               "mode": self.mode, "llama_build": None, "regressed": self.regressed,
               "facts": {}, "changes": changes, "before": self.before, "after": self.after,
               "base_config": self.base, "config": self.config(), "was_loaded": self.was_loaded,
               "guard": None, "verify": self.verify, "stages": self.stages,
               "power_cap_w": None, "over_cap": None,
               "stop_reason": self.stop_reason, "elapsed_s": int(self.elapsed()), "loads": self.loads}
        self.put(doc)
        return doc


def run_model(model_id: str, req: dict, backend, put, cancelled, env: dict, clock=time.monotonic) -> dict:
    """Runs every stage for one model and returns the emitted model_done document."""
    return _Run(model_id, req, backend, put, cancelled, env, clock).run()


def ledger_summary(done: dict) -> dict:
    out = _at.ledger_summary(done)
    out["provider"] = "lms"
    return out
