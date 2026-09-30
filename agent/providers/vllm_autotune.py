"""vLLM autotune (#894): request validation, stage engine and choice rules over the serve flags.
Pure stdlib; vllm_tools.py owns the unit file, the journal, restarts and routes."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from . import llama_autotune as _at
from . import lms_autotune as _lat

OBJECTIVES = ("fit", "speed", "balanced", "serve")
MODES = ("tune", "verify")
STAGES = ("context", "seqs", "kvdtype", "spec", "prefix", "verify")
MEASURED = ("seqs", "kvdtype", "spec", "prefix", "verify")
KV_DTYPES = ("auto", "fp8")
SPEC_TYPES = ("auto", "ngram", "draft", "none")
NGRAM_SPEC = {"method": "ngram", "num_speculative_tokens": 5, "prompt_lookup_max": 4}
SEQS_DEFAULT = {"fit": [1, 4, 8], "speed": [1, 4, 8], "balanced": [1, 4, 8], "serve": [8, 16, 32]}
MIN_CTX, MAX_CTX = 256, 262144
DEFAULT_MAX_NUM_SEQS = 256
# Serve flags the tuner may recommend, in the order the change table lists them.
CONFIG_KEYS = ("max_model_len", "max_num_seqs", "kv_cache_dtype", "speculative_config", "enable_prefix_caching")
INPUT_KEYS = ("gpu_memory_utilization",)
FLAGS = {"max_model_len": "--max-model-len", "max_num_seqs": "--max-num-seqs", "kv_cache_dtype": "--kv-cache-dtype",
         "speculative_config": "--speculative-config", "enable_prefix_caching": "--enable-prefix-caching",
         "gpu_memory_utilization": "--gpu-memory-utilization"}
_ALIASES = {"speculative_config": ("-sc",), "enable_prefix_caching": ("--no-enable-prefix-caching",)}
DIM_KEYS = {"seqs": ("max_num_seqs",), "kvdtype": ("kv_cache_dtype",), "spec": ("speculative_config",),
            "prefix": ("enable_prefix_caching",)}
BASELINE_KEYS = ("decode_tps", "prefill_tps", "agg_tps", "ctx", "concurrency")
LEGACY_KEYS = ("probe_len", "concurrency", "kv_fraction", "report_only")
GAIN_MIN_PCT = _at.GAIN_MIN_PCT
SPEC_MIN_GAIN_PCT = _at.SPEC_MIN_GAIN_PCT
STICK_LIMIT = _at.STICK_LIMIT
_MODEL_RE = re.compile(r"^[A-Za-z0-9./][A-Za-z0-9._@:/\-]{0,199}$")

DEFAULT_DIMS: dict = {
    "context": {"on": True, "probe_len": 4096, "concurrency": 1.0, "kv_fraction": 1.0},
    "seqs":    {"on": True, "candidates": []},
    "kvdtype": {"on": True, "candidates": ["auto", "fp8"]},
    "spec":    {"on": True, "types": ["auto"], "draft_model": "none"},
    "prefix":  {"on": True, "candidates": [True, False]},
}


class Cancelled(Exception):
    pass


def is_legacy(body: dict) -> bool:
    """The pre-#894 max-model-len body: no model_ids, one of the old probe keys."""
    return "model_ids" not in body and any(k in body for k in LEGACY_KEYS)


def legacy_body(body: dict, model_id: str) -> dict:
    """The old wizard's body as a context-only request for the served model."""
    ctx = {k: body[k] for k in ("probe_len", "concurrency", "kv_fraction") if k in body}
    return {"model_ids": [model_id], "objective": "fit", "budget_min": 120,
            "dims": {"context": ctx, "seqs": {"on": False}, "kvdtype": {"on": False},
                     "spec": {"on": False}, "prefix": {"on": False}},
            "apply": not bool(body.get("report_only")),
            "load_timeout_s": body.get("load_timeout_s", 600)}


def validate_request(body: dict) -> dict:
    ids = body.get("model_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    ids = [str(m).strip() for m in ids if str(m).strip()]
    if not ids:
        raise ValueError("model_ids required")
    bad = next((m for m in ids if not _MODEL_RE.match(m)), None)
    if bad is not None:
        raise ValueError(f"invalid model id: {bad}")
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
            if "probe_len" in g:
                d["probe_len"] = _at._int(g["probe_len"], "context.probe_len", MIN_CTX, MAX_CTX)
            if "concurrency" in g:
                d["concurrency"] = _at._float(g["concurrency"], "context.concurrency", 1.0, 64.0)
            if "kv_fraction" in g:
                d["kv_fraction"] = _at._float(g["kv_fraction"], "context.kv_fraction", 0.1, 1.0)
        elif name == "seqs":
            if "candidates" in g:
                d["candidates"] = sorted({_at._int(x, "seqs.candidates", 1, 1024) for x in _at._list(g["candidates"], "seqs.candidates")})
        elif name == "kvdtype":
            if "candidates" in g:
                c = [str(x) for x in _at._list(g["candidates"], "kvdtype.candidates")]
                if any(x not in KV_DTYPES for x in c):
                    raise ValueError("kvdtype.candidates must be from " + ", ".join(KV_DTYPES))
                d["candidates"] = list(dict.fromkeys(c))
        elif name == "prefix":
            if "candidates" in g:
                d["candidates"] = _lat._bool_list(g["candidates"], "prefix.candidates")
        elif name == "spec":
            if "types" in g:
                t = [str(x) for x in _at._list(g["types"], "spec.types")]
                if any(x not in SPEC_TYPES for x in t):
                    raise ValueError("spec.types must be from " + ", ".join(SPEC_TYPES))
                d["types"] = list(dict.fromkeys(t))
            if "draft_model" in g:
                dm = str(g["draft_model"] or "none").strip()
                if dm != "none" and not _MODEL_RE.match(dm):
                    raise ValueError("spec.draft_model must be none or a model id")
                d["draft_model"] = dm
    dims["context"]["on"] = True
    if not dims["seqs"]["candidates"]:
        dims["seqs"]["candidates"] = list(SEQS_DEFAULT[objective])
    out: dict = {"model_ids": ids, "objective": objective, "budget_min": budget, "dims": dims,
                 "apply": bool(body.get("apply")),
                 "load_timeout_s": _at._int(body.get("load_timeout_s", 600), "load_timeout_s", 60, 3600)}
    gmu = body.get("gpu_memory_utilization")
    out["gpu_memory_utilization"] = None if gmu is None else _at._float(gmu, "gpu_memory_utilization", 0.5, 0.95)
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
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dict):
        return json.dumps(v, sort_keys=True, separators=(",", ":"))
    return str(v)


def spec_type(cfg: Any) -> str:
    """none / ngram / draft / custom for a speculative_config value."""
    if not cfg:
        return "none"
    if not isinstance(cfg, dict):
        return "custom"
    method = str(cfg.get("method") or ("draft_model" if cfg.get("model") else ""))
    return {"ngram": "ngram", "draft_model": "draft"}.get(method, "custom")


def _split_flag(a: dict) -> tuple[str, Any]:
    flag, value = str(a.get("flag") or ""), a.get("value")
    if flag.startswith("--") and "=" in flag:
        flag, value = flag.split("=", 1)
    return flag, value


_SUFFIX = {"k": 1024, "m": 1024 * 1024}


def _int_arg(value: Any) -> Optional[int]:
    """Integer flag value; accepts vLLM's k/m suffixes; None when unparseable."""
    raw = str(value or "").replace(",", "").strip().lower()
    mult = _SUFFIX.get(raw[-1:], 1)
    try:
        return int(raw[:-1] if mult > 1 else raw) * mult
    except ValueError:
        return None


def config_from_args(args: list) -> dict:
    """{key: typed value} for the tuned and input keys present in svcconfig arg rows (both flag forms)."""
    out: dict = {}
    for a in args or []:
        flag, value = _split_flag(a)
        if flag == "--no-enable-prefix-caching":
            out["enable_prefix_caching"] = False
            continue
        if flag == "-sc":
            flag = "--speculative-config"
        key = next((k for k, f in FLAGS.items() if f == flag), None)
        if key is None:
            continue
        if key == "enable_prefix_caching":
            out[key] = True
        elif key == "speculative_config":
            try:
                out[key] = json.loads(str(value)) if value not in (None, "") else None
            except ValueError:
                out[key] = str(value)
        elif key in ("max_model_len", "max_num_seqs"):
            out[key] = _int_arg(value)
        elif key == "gpu_memory_utilization":
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                out[key] = None
        else:
            out[key] = None if value is None else str(value)
    return out


def args_with_config(args: list, config: dict) -> list:
    """Arg rows with every key in config rewritten (both flag forms and aliases); a None value drops the flag."""
    drop: set = set()
    for k in config:
        f = FLAGS.get(k)
        if f:
            drop.update((f,) + _ALIASES.get(k, ()))
    out = []
    for a in args or []:
        flag, _v = _split_flag(a)
        if flag in drop:
            continue
        out.append(dict(a))
    for k in CONFIG_KEYS + INPUT_KEYS:
        if k not in config or config[k] is None or config[k] == "" or config[k] == {}:
            continue
        v = config[k]
        if k == "enable_prefix_caching":
            out.append({"flag": "--enable-prefix-caching" if v else "--no-enable-prefix-caching", "value": None, "bool": True})
        else:
            out.append({"flag": FLAGS[k], "value": cfg_str(v), "bool": False})
    return out


def compute_recommended_max_len(kv_tokens: int, concurrency: float = 1.0,
                                kv_fraction: float = 1.0) -> int:
    """Largest --max-model-len fitting kv_tokens at the target concurrency,
    scaled by kv_fraction, floored to a multiple of 256 (min 256)."""
    conc = max(1.0, float(concurrency))
    frac = min(1.0, max(0.1, float(kv_fraction)))
    raw = int(int(kv_tokens) * frac / conc)
    return max(256, (raw // 256) * 256)


def estimate(stage: str, n: int, load_s: float, stick_s: float) -> int:
    n = max(1, int(n))
    per = {"context": load_s + stick_s, "verify": load_s + 60}
    return int(round(per.get(stage, n * (load_s + stick_s))))


def build_changes(current: dict, rec: dict, evidence: dict) -> list[dict]:
    rows = []
    keys = [k for k in CONFIG_KEYS if k in rec] + [k for k in rec if k not in CONFIG_KEYS]
    for k in keys:
        cur, new = cfg_str((current or {}).get(k)), cfg_str(rec[k])
        if cur == new:
            continue
        ev = (evidence or {}).get(k) or {}
        g = ev.get("gain_pct")
        rows.append({"key": k, "flag": FLAGS.get(k, k), "current": cur, "recommended": new, "value": rec[k],
                     "source": ev.get("source", "measured"), "evidence": ev.get("text", ""),
                     "selected": g is None or g >= GAIN_MIN_PCT})
    return rows


class _Run:
    """Stage engine for one served model. backend: current() → {loaded, served, config, max_ctx};
    load(config, measure) → {ok, error, rejected?, config, ctx, kv_tokens, load_s, stick}; drafts() → []."""

    def __init__(self, model_id, req, backend, put, cancelled, env, clock):
        self.model_id, self.req, self.backend = model_id, req, backend
        self.put, self.cancelled, self.env, self.clock = put, cancelled, env, clock
        self.dims, self.objective = req["dims"], req["objective"]
        self.mode = req.get("mode") or "tune"
        self.budget_s = int(req["budget_min"]) * 60
        self.runtime = bool(env.get("runtime"))
        self.base: dict = {}
        self.was_loaded = True
        self.max_ctx: Optional[int] = None
        self.rec: dict = {}
        self.evidence: dict = {}
        self.stages: list = []
        self.before: Optional[dict] = None
        self.after: Optional[dict] = None
        self.verify: Optional[dict] = None
        self.ctx_total: Optional[int] = None
        self.kv_tokens: Optional[int] = None
        self.load_s, self.stick_s = 60.0, 30.0
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
            cfg["max_model_len"] = int(self.ctx_total)
        if self.req.get("gpu_memory_utilization") is not None:
            cfg["gpu_memory_utilization"] = float(self.req["gpu_memory_utilization"])
        cfg.update(extra or {})
        return cfg

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
        dmax = res.get("derived_max")
        if dmax and self.ctx_total and int(self.ctx_total) > int(dmax):
            self.ctx_total = max(256, (int(dmax) // 256) * 256)
            self.rec["max_model_len"] = self.ctx_total
            self.ev("max_model_len", f"capped at the model's derived maximum {int(dmax):,}")
            self.emit("line", text=f"[autotune] max_model_len capped at the model's derived maximum {int(dmax):,}")
        if res.get("load_s"):
            self.load_s = float(res["load_s"])
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
        shown = cfg_str(value) if isinstance(value, bool) else value
        if res.get("rejected"):
            self.emit("candidate_rejected", stage=stage, value=shown, reason=res.get("error") or "the engine did not start")
        self.emit("candidate_result", stage=stage, value=shown,
                  ok=bool(res.get("ok")) and (not st or bool(st.get("ok"))),
                  ctx=res.get("ctx"), kv_tokens=res.get("kv_tokens"), decode_tps=st.get("decode_tps"),
                  prefill_tps=st.get("prefill_tps"), agg_tps=st.get("agg_tps"), accept=st.get("accept"),
                  error=res.get("error") or st.get("error"), **extra)

    def ev(self, key: str, text: str, gain: Optional[float] = None) -> None:
        self.evidence[key] = {"source": "measured", "text": text, "gain_pct": gain}

    def sweep(self, stage: str, key: str, cands: list, metric: str = "decode_tps", default: Any = None) -> None:
        """Measure one value per candidate and keep the fastest when it beats the current value."""
        if not self.on(stage) or not self.need_runtime(stage):
            return
        current = self.cur(key, default)
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
        choice, reason, gain = _lat.choose_best(results, metric, current)
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
        info = self.backend.current() or {}
        self.was_loaded = bool(info.get("loaded", True))
        self.base = {k: v for k, v in (info.get("config") or {}).items() if k in CONFIG_KEYS}
        self.max_ctx = info.get("max_ctx")
        served = info.get("served")
        if served and served != self.model_id:
            self.stop_reason = f"vLLM serves {served}, not {self.model_id}"
            self.skip("context", "not_served")
            return False
        probe = int(d["probe_len"])
        cur_len = self.base.get("max_model_len")
        mark = self.begin("context", [probe], estimate("context", 1, self.load_s, self.stick_s))
        self.check_cancel()
        self.emit("candidate_start", stage="context", value=probe)
        res, st = self.measure({"max_model_len": probe})
        self.result("context", probe, res)
        if not res.get("ok"):
            self.stop_reason = res.get("error") or "the server did not start at the probe length"
            self.end("context", mark, None, self.stop_reason)
            return False
        if res.get("kv_tokens") is None:
            self.stop_reason = "no KV-capacity line in the journal"
            self.end("context", mark, None, self.stop_reason)
            return False
        self.kv_tokens = int(res["kv_tokens"])
        self.emit("kv_capacity", tokens=self.kv_tokens)
        want = compute_recommended_max_len(self.kv_tokens, d["concurrency"], d["kv_fraction"])
        capped = bool(self.max_ctx) and want > int(self.max_ctx)
        if capped:
            want = int(self.max_ctx)
        self.emit("recommendation", max_model_len=want, kv_tokens=self.kv_tokens,
                  concurrency=d["concurrency"], kv_fraction=d["kv_fraction"])
        self.decode_now, self.prefill_now = st.get("decode_tps"), st.get("prefill_tps")
        self.before = {"decode_tps": st.get("decode_tps"), "prefill_tps": st.get("prefill_tps"),
                       "agg_tps": st.get("agg_tps"), "ctx": probe, "concurrency": 1}
        math_text = f"{self.kv_tokens:,} KV tokens ÷ {float(d['concurrency']):g} × {float(d['kv_fraction']):g} → {want:,}"
        if capped:
            math_text += " (capped at the model's maximum)"
        if self.objective != "fit" and cur_len and int(cur_len) <= want:
            self.ctx_total = int(cur_len)
            self.end("context", mark, int(cur_len), f"kept {int(cur_len):,} · fits ({math_text})")
            return True
        self.ctx_total = want
        if want != cur_len:
            self.rec["max_model_len"] = want
            self.ev("max_model_len", math_text)
        self.end("context", mark, want, math_text)
        return True

    def stage_seqs(self) -> None:
        if not self.on("seqs") or not self.need_runtime("seqs"):
            return
        cands = [int(c) for c in self.dims["seqs"]["candidates"]]
        est = estimate("seqs", len(cands), self.load_s, self.stick_s)
        if not self.within_budget("seqs", est):
            return
        mark = self.begin("seqs", cands, est)
        results = []
        for c in cands:
            self.check_cancel()
            self.emit("candidate_start", stage="seqs", value=c)
            res, st = self.measure({"max_num_seqs": c}, concurrency=c, limit=max(STICK_LIMIT, 2 * c))
            results.append({"value": c, "ok": bool(res.get("ok")) and bool(st.get("ok")),
                            "decode_tps": st.get("decode_tps"), "agg_tps": st.get("agg_tps")})
            self.result("seqs", c, res)
        choice, reason = _at.choose_slots(self.objective, results)
        if choice is None:
            self.end("seqs", mark, None, reason)
            return
        one = next((r for r in results if int(r["value"]) == 1 and r["ok"]), None)
        best = next((r for r in results if int(r["value"]) == int(choice)), None)
        if int(choice) != int(self.cur("max_num_seqs", DEFAULT_MAX_NUM_SEQS)):
            self.rec["max_num_seqs"] = int(choice)
            self.ev("max_num_seqs", reason, _at.gain_pct((best or {}).get("agg_tps"), (one or {}).get("agg_tps")))
        self.concurrency = int(choice)
        self.end("seqs", mark, choice, reason)

    def stage_kvdtype(self) -> None:
        self.sweep("kvdtype", "kv_cache_dtype", [str(c) for c in self.dims["kvdtype"]["candidates"]], default="auto")

    def spec_rows(self) -> list[dict]:
        d = self.dims["spec"]
        types = ["ngram", "draft"] if "auto" in d["types"] else list(d["types"])
        rows = [{"type": "none", "cfg": None}]
        for t in types:
            if t == "ngram":
                rows.append({"type": "ngram", "cfg": dict(NGRAM_SPEC)})
            elif t == "draft" and d["draft_model"] != "none":
                rows.append({"type": "draft", "cfg": {"method": "draft_model", "model": d["draft_model"],
                                                      "num_speculative_tokens": 5}})
        return rows

    def stage_spec(self) -> None:
        if not self.on("spec") or not self.need_runtime("spec"):
            return
        rows = self.spec_rows()
        if len(rows) < 2:
            self.skip("spec", "no_candidates")
            return
        est = estimate("spec", len(rows), self.load_s, self.stick_s)
        if not self.within_budget("spec", est):
            return
        mark = self.begin("spec", [r["type"] for r in rows], est)
        results = []
        for r in rows:
            self.check_cancel()
            self.emit("candidate_start", stage="spec", value=r["type"])
            res, st = self.measure({"speculative_config": r["cfg"]})
            results.append({"value": r["type"], "ok": bool(res.get("ok")) and bool(st.get("ok")),
                            "decode_tps": st.get("decode_tps"), "accept": st.get("accept"), "cfg": r["cfg"]})
            self.result("spec", r["type"], res)
        choice, reason, gain = _lat.choose_spec(results)
        best = next((r for r in results if r["value"] == choice), None)
        if best is None:
            self.end("spec", mark, None, reason)
            return
        if cfg_str(self.base.get("speculative_config")) != cfg_str(best["cfg"]):
            self.rec["speculative_config"] = best["cfg"]
            self.ev("speculative_config", reason, gain)
        self.end("spec", mark, choice, reason)

    def stage_prefix(self) -> None:
        self.sweep("prefix", "enable_prefix_caching", list(self.dims["prefix"]["candidates"]), default=True)

    def stage_verify(self) -> None:
        mark = self.begin("verify", ["recommended set"], estimate("verify", 1, self.load_s, self.stick_s))
        self.check_cancel()
        conc = self.concurrency
        limit = _at.verify_limit(self.decode_now, self.prefill_now, conc)
        self.emit("candidate_start", stage="verify", value=1)
        res, st = self.measure({}, concurrency=conc, limit=limit)
        self.result("verify", 1, res)
        ok = bool(res.get("ok")) and (not self.runtime or bool(st.get("ok")))
        if ok:
            self.after = {"decode_tps": st.get("decode_tps"), "prefill_tps": st.get("prefill_tps"),
                          "ctx": res.get("ctx") or self.ctx_total, "agg_tps": st.get("agg_tps"),
                          "concurrency": conc, "accept": st.get("accept")}
            self.verify = {"ok": True, "seconds": st.get("seconds") or 0, "dropped": [], "reason": None, "warning": None}
            self.end("verify", mark, "pass", f"concurrency {conc} · {limit} requests" if self.runtime else "restart only")
            return
        reason = res.get("error") or st.get("error") or "restart failed"
        self.after = {"ctx": self.ctx_total, "concurrency": 1}
        self.verify = {"ok": False, "seconds": 0, "dropped": [], "reason": reason, "warning": None}
        self.end("verify", mark, "fail", reason)

    # ── driver ──
    def run(self) -> dict:
        if self.mode == "verify":
            return self.run_verify_only()
        planned = [s for s in STAGES if s in ("context", "verify") or self.dims.get(s, {}).get("on")]
        self.emit("model_start", objective=self.objective, mode=self.mode, stages=planned, provider="vllm",
                  budget_min=self.req["budget_min"], probe_len=self.dims["context"]["probe_len"])
        ok = cancelled = False
        try:
            ok = self.stage_context()
            if ok:
                for fn in (self.stage_seqs, self.stage_kvdtype, self.stage_spec, self.stage_prefix, self.stage_verify):
                    self.check_cancel()
                    fn()
        except Cancelled:
            cancelled, ok = True, False
            self.stop_reason = "cancelled"
        except Exception as e:
            ok = False
            self.stop_reason = str(e)[:300]
        return self.done(ok, cancelled)

    def run_verify_only(self) -> dict:
        self.emit("model_start", objective=self.objective, mode="verify", stages=["verify"], provider="vllm",
                  budget_min=self.req["budget_min"], probe_len=self.dims["context"]["probe_len"])
        info = self.backend.current() or {}
        self.was_loaded = bool(info.get("loaded", True))
        self.base = {k: v for k, v in (info.get("config") or {}).items() if k in CONFIG_KEYS}
        self.ctx_total = int(self.base.get("max_model_len") or 0) or None
        base = dict(self.req.get("baseline") or {})
        try:
            self.concurrency = max(1, int(base.get("concurrency") or 1))
        except (TypeError, ValueError):
            self.concurrency = 1
        bt = self.req.get("baseline_tps")
        self.before = base or None
        self.decode_now = bt
        ok = cancelled = False
        try:
            self.stage_verify()
            ok = bool((self.verify or {}).get("ok"))
        except Cancelled:
            cancelled = True
            self.stop_reason = "cancelled"
        after = (self.after or {}).get("decode_tps")
        if ok and bt is not None and after is not None:
            self.regressed = float(after) < 0.85 * float(bt)
        return self.done(ok, cancelled)

    def done(self, ok: bool, cancelled: bool) -> dict:
        changes = build_changes(self.base, self.rec, self.evidence)
        if self.after is None and self.ctx_total:
            self.after = {"ctx": self.ctx_total, "concurrency": 1}
        apply = bool(self.req.get("apply"))
        doc = {"type": "model_done", "model_id": self.model_id, "run_id": self.env.get("run_id"), "provider": "vllm",
               "ok": bool(ok) and not cancelled, "cancelled": cancelled, "objective": self.objective,
               "mode": self.mode, "llama_build": None, "regressed": self.regressed,
               "facts": {}, "changes": changes, "before": self.before, "after": self.after,
               "base_config": self.base, "config": self.config(), "was_loaded": self.was_loaded,
               "guard": None, "verify": self.verify, "stages": self.stages, "kv_tokens": self.kv_tokens,
               "apply": apply, "applied": apply and bool(ok) and not cancelled and bool((self.verify or {}).get("ok")),
               "power_cap_w": None, "over_cap": None,
               "stop_reason": self.stop_reason, "elapsed_s": int(self.elapsed()), "loads": self.loads}
        self.put(doc)
        return doc


def run_model(model_id: str, req: dict, backend, put, cancelled, env: dict, clock=time.monotonic) -> dict:
    """Runs every stage for one model and returns the emitted model_done document."""
    return _Run(model_id, req, backend, put, cancelled, env, clock).run()


def ledger_summary(done: dict) -> dict:
    out = _at.ledger_summary(done)
    out.update(provider="vllm", kv_tokens=done.get("kv_tokens"), applied=done.get("applied"),
               max_model_len=(done.get("after") or {}).get("ctx"))
    return out
