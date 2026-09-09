"""Autotune v2 (#880): request validation, model facts, stage choice rules,
and the stage engine. Pure stdlib; llama.py owns processes and routes."""
from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

OBJECTIVES = ("fit", "speed", "balanced", "serve")
MODES = ("tune", "verify", "quality")
# config.ini keys the quality guard may override (what llama-perplexity accepts).
QUALITY_KEYS = frozenset({"cache-type-k", "ctk", "cache-type-v", "ctv", "threads", "t", "threads-batch", "tb",
                          "n-gpu-layers", "ngl", "n-cpu-moe", "ncmoe", "batch-size", "b", "ubatch-size", "ub",
                          "flash-attn", "fa", "no-mmap", "mlock"})
_OVERRIDE_VAL_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
KV_TYPES = ("f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1")
KV_LOSSY = ("q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1")
SPEC_TYPES = ("auto", "draft-mtp", "draft-dflash", "draft-simple", "ngram-simple")
SPEC_NEEDS_DRAFT = ("draft-dflash", "draft-simple")
STAGES = ("context", "kv", "moe", "threads", "spec", "slots", "sampling", "verify")
MEASURED = ("threads", "spec", "slots", "verify")
FALLBACK_ORDER = ("slots", "spec", "threads")
GAIN_MIN_PCT = 3.0
SPEC_MIN_GAIN_PCT = 5.0
THREADS_TIE_PCT = 3.0
KV_MIN_CTX_GAIN_PCT = 10.0
SLOTS_BALANCED_FLOOR = 0.70
MAX_CANDIDATES = 8

DEFAULT_DIMS: dict = {
    "context":  {"on": True, "target_mb": 1024, "tolerance_mb": 50, "custom_args": []},
    "kv":       {"on": True, "candidates": ["f16", "q8_0", "q4_0"], "guard_kl_max": 0.02},
    "moe":      {"on": True, "min": 0, "max": 16},
    "threads":  {"on": True, "candidates": []},
    "slots":    {"on": True, "candidates": [1, 2, 4, 8], "min_ctx_per_slot": 32768},
    "spec":     {"on": True, "types": ["auto"], "draft_model": "auto", "n_min": 0, "n_max": 16, "p_min": 0.75},
    "sampling": {"on": True, "overwrite": False},
}

# Keys the tuner owns or that never belong on a tuning spawn.
ALWAYS_DROP = ("ctx-size", "c", "fit", "fitt", "fit-target", "fit-ctx", "hf-repo", "hf-file",
               "port", "host", "models-max", "log-verbosity", "lv", "alias")
KEY_ALIASES = {"cache-type-k": ("ctk",), "cache-type-v": ("ctv",), "threads": ("t",),
               "threads-batch": ("tb",), "parallel": ("np",), "n-cpu-moe": ("ncmoe",),
               "model-draft": ("md",), "gpu-layers-draft": ("ngld",),
               "cache-type-k-draft": ("ctkd",), "cache-type-v-draft": ("ctvd",)}
ALIAS_TO_KEY = {a: k for k, aliases in KEY_ALIASES.items() for a in aliases}
# context.custom_args limits; these flags carry paths/identity the tuner owns.
CUSTOM_ARGS_MAX = 32
CUSTOM_ARG_MAX_LEN = 256
CUSTOM_ARGS_DENY = ("-m", "--model", "-md", "--model-draft", "--lora", "--lora-scaled", "--mmproj",
                    "--models-preset", "--hf-repo", "-hf", "--hf-file", "--port", "--host")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,199}$")
_FACT_RE = re.compile(r"print_info:\s*([A-Za-z0-9_.][A-Za-z0-9_. ]*?)\s*=\s*(.+?)\s*$")
_MTP_KV_RE = re.compile(r"nextn_predict_layers\s+\w+\s*=\s*(\d+)")
_KL_RE = re.compile(r"Mean\s+KLD\s*:\s*([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)")
_SIZE_TOKEN_RE = re.compile(r"[-_](\d+(?:\.\d+)?)[bB](?=[-_.]|$)")
_HELP_FLAG_RE = re.compile(r"^--?[A-Za-z0-9]")
BOOL_ON = ("on", "true")
BOOL_OFF = ("off", "false")


def _int(v: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(v, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if not lo <= n <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return n


def _float(v: Any, name: str, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(f) or not lo <= f <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return f


def _list(v: Any, name: str) -> list:
    if not isinstance(v, list) or not v or len(v) > MAX_CANDIDATES:
        raise ValueError(f"{name} must be a list of 1–{MAX_CANDIDATES} items")
    return v


def _draft_path(v: str, cache_root: Optional[Path]) -> str:
    """'auto' | 'none' | an existing .gguf under cache_root (resolved)."""
    if v in ("auto", "none"):
        return v
    p = Path(v)
    if not p.is_absolute() or p.suffix != ".gguf" or ".." in p.parts:
        raise ValueError("draft_model must be auto, none, or a .gguf inside the HF cache")
    if cache_root is not None:
        try:
            rp = p.resolve(strict=True)
            rp.relative_to(Path(cache_root).resolve())
        except (OSError, ValueError):
            raise ValueError("draft_model must be a .gguf inside the HF cache")
        return str(rp)
    return str(p)


def _custom_args(ca: list) -> list[str]:
    """Trimmed tokens, capped in count and length, with tuner-owned flags refused."""
    toks = [t.strip() for t in ca if t.strip()]
    if len(toks) > CUSTOM_ARGS_MAX:
        raise ValueError(f"context.custom_args must be at most {CUSTOM_ARGS_MAX} tokens")
    if any(len(t) > CUSTOM_ARG_MAX_LEN for t in toks):
        raise ValueError(f"context.custom_args tokens must be at most {CUSTOM_ARG_MAX_LEN} characters")
    bad = next((t for t in toks if t in CUSTOM_ARGS_DENY), None)
    if bad:
        raise ValueError(f"context.custom_args must not set {bad}")
    return toks


def validate_request(body: dict, *, cache_root: Optional[Path] = None, v1_args=None) -> dict:
    ids = body.get("model_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    ids = [str(m).strip() for m in ids if str(m).strip()]
    if not ids or any(not _MODEL_RE.match(m) for m in ids):
        raise ValueError("model_ids required")
    dims = {k: dict(v) for k, v in DEFAULT_DIMS.items()}
    if "objective" not in body:
        target = _int(body.get("target_mb"), "target_mb", 0, 1_000_000)
        tol = max(1, _int(body.get("tolerance_mb", 50), "tolerance_mb", -1_000_000, 1_000_000))
        params = body.get("optional_params") or {}
        if not isinstance(params, dict):
            raise ValueError("optional_params must be an object")
        for d in dims:
            dims[d]["on"] = d == "context"
        dims["context"].update({"target_mb": target, "tolerance_mb": tol,
                                "custom_args": list(v1_args(params)) if v1_args else []})
        return {"model_ids": ids, "objective": "fit", "budget_min": 600, "dims": dims, "mode": "tune"}
    objective = str(body.get("objective") or "")
    if objective not in OBJECTIVES:
        raise ValueError("objective must be one of " + ", ".join(OBJECTIVES))
    budget = _int(body.get("budget_min", 120), "budget_min", 5, 600)
    given = body.get("dims") or {}
    if not isinstance(given, dict):
        raise ValueError("dims must be an object")
    for name, d in dims.items():
        g = given.get(name) or {}
        if not isinstance(g, dict):
            raise ValueError(f"dims.{name} must be an object")
        if "on" in g:
            d["on"] = bool(g["on"])
        if name == "context":
            if "target_mb" in g:
                d["target_mb"] = _int(g["target_mb"], "context.target_mb", 0, 1_000_000)
            if "tolerance_mb" in g:
                d["tolerance_mb"] = max(1, _int(g["tolerance_mb"], "context.tolerance_mb", -1, 1_000_000))
            ca = g.get("custom_args", [])
            if not isinstance(ca, list) or any(not isinstance(t, str) for t in ca):
                raise ValueError("context.custom_args must be a list of strings")
            d["custom_args"] = _custom_args(ca)
        elif name == "kv":
            if "candidates" in g:
                c = [str(x) for x in _list(g["candidates"], "kv.candidates")]
                if any(x not in KV_TYPES for x in c):
                    raise ValueError("kv.candidates must be llama KV cache types")
                d["candidates"] = list(dict.fromkeys(c))
            if "guard_kl_max" in g:
                d["guard_kl_max"] = _float(g["guard_kl_max"], "kv.guard_kl_max", 0.0, 10.0)
        elif name == "moe":
            if "min" in g:
                d["min"] = _int(g["min"], "moe.min", 0, 512)
            if "max" in g:
                d["max"] = _int(g["max"], "moe.max", 0, 512)
            if d["min"] > d["max"]:
                raise ValueError("moe.min must not exceed moe.max")
        elif name == "threads":
            if "candidates" in g:
                d["candidates"] = sorted({_int(x, "threads.candidates", 1, 256) for x in _list(g["candidates"], "threads.candidates")})
        elif name == "slots":
            if "candidates" in g:
                d["candidates"] = sorted({_int(x, "slots.candidates", 1, 64) for x in _list(g["candidates"], "slots.candidates")})
            if "min_ctx_per_slot" in g:
                d["min_ctx_per_slot"] = _int(g["min_ctx_per_slot"], "slots.min_ctx_per_slot", 512, 4_194_304)
        elif name == "spec":
            if "types" in g:
                t = [str(x) for x in _list(g["types"], "spec.types")]
                if any(x not in SPEC_TYPES for x in t):
                    raise ValueError("spec.types must be from " + ", ".join(SPEC_TYPES))
                d["types"] = list(dict.fromkeys(t))
            if "draft_model" in g:
                d["draft_model"] = _draft_path(str(g["draft_model"] or "auto"), cache_root)
            if "n_min" in g:
                d["n_min"] = _int(g["n_min"], "spec.n_min", 0, 64)
            if "n_max" in g:
                d["n_max"] = _int(g["n_max"], "spec.n_max", 1, 64)
            if d["n_min"] >= d["n_max"]:
                raise ValueError("spec.n_min must be below spec.n_max")
            if "p_min" in g:
                d["p_min"] = _float(g["p_min"], "spec.p_min", 0.0, 1.0)
        elif name == "sampling":
            d["overwrite"] = bool(g.get("overwrite", False))
    out = {"model_ids": ids, "objective": objective, "budget_min": budget, "dims": dims}
    mode = str(body.get("mode") or "tune")
    if mode not in MODES:
        raise ValueError("mode must be one of " + ", ".join(MODES))
    out["mode"] = mode
    if mode == "verify":
        for name, d in dims.items():
            if name != "context":
                d["on"] = False
        bt = body.get("baseline_tps")
        out["baseline_tps"] = None if bt is None else _float(bt, "baseline_tps", 0.0, 1e6)
    elif mode == "quality":
        ov = body.get("overrides")
        if not isinstance(ov, dict) or not ov:
            raise ValueError("overrides required for quality mode")
        clean: dict = {}
        for k, v in ov.items():
            key = str(k).lstrip("-")
            if key not in QUALITY_KEYS:
                raise ValueError(f"override {key!r} is not a quality-guard key")
            if v is None:
                clean[key] = None
                continue
            sv = str(v).strip()
            if not _OVERRIDE_VAL_RE.match(sv):
                raise ValueError(f"override {key!r} has an invalid value")
            clean[key] = sv
        out["overrides"] = clean
        out["kl_max"] = _float(body.get("kl_max", dims["kv"]["guard_kl_max"]), "kl_max", 0.0, 10.0)
    return out


def parse_help_valued(text: str) -> set[str]:
    """Long option names (no dashes) that take a value, from llama-server --help output.
    A value hint follows the last flag by exactly one space; descriptions are padded or wrapped."""
    valued: set[str] = set()
    for line in (text or "").splitlines():
        toks = [(m.start(), m.end(), m.group()) for m in re.finditer(r"\S+", line)]
        if not toks or not _HELP_FLAG_RE.match(toks[0][2].rstrip(",")):
            continue
        names: list[str] = []
        i, end = 0, 0
        while i < len(toks):
            tok = toks[i][2]
            bare = tok.rstrip(",")
            if not _HELP_FLAG_RE.match(bare):
                break
            if bare.startswith("--"):
                names.append(bare.lstrip("-"))
            end = toks[i][1]
            i += 1
            if not tok.endswith(","):
                break
        if names and i < len(toks) and toks[i][0] - end == 1:
            valued.update(names)
    return valued


def _unquote(s: str) -> str:
    """Strip one pair of surrounding double quotes."""
    return s[1:-1] if len(s) >= 2 and s[0] == '"' and s[-1] == '"' else s


def section_args(section: dict, overrides: dict, valued: Optional[set] = None) -> list[str]:
    """llama-server argv for a config.ini section with tuner overrides applied.
    on/off values become `--key value` for options in `valued`, else a bare flag or nothing."""
    ov = {str(k).lstrip("-"): v for k, v in (overrides or {}).items()}
    drop = set(ALWAYS_DROP)
    for k in ov:
        for name in (k, ALIAS_TO_KEY.get(k)):
            if name:
                drop.add(name)
                drop.update(KEY_ALIASES.get(name, ()))
    merged: list[tuple[str, Any]] = [(k, v) for k, v in
                                     ((str(k).lstrip("-"), v) for k, v in (section or {}).items()) if k not in drop]
    merged += list(ov.items())
    out: list[str] = []
    for k, v in merged:
        sv = "" if v is None else _unquote(str(v).strip())
        if not k or sv == "":
            continue
        flag = ("-" if len(k) == 1 else "--") + k
        low = sv.lower()
        if low in BOOL_ON or low in BOOL_OFF:
            takes = valued is not None and (k in valued or ALIAS_TO_KEY.get(k, "") in valued)
            if takes:
                out += [flag, sv]
            elif low in BOOL_ON:
                out.append(flag)
            continue
        out += [flag, sv]
    return out


def _num(s: str) -> Optional[float]:
    m = re.match(r"^-?\d+(?:\.\d+)?", s.strip())
    return float(m.group(0)) if m else None


def parse_facts(lines: Iterable[str]) -> dict:
    """Model facts from a load log; lines may carry a timestamp/level prefix."""
    raw: dict[str, str] = {}
    mtp = 0
    for line in lines:
        m = _FACT_RE.search(line)
        if m:
            raw[m.group(1)] = m.group(2)
        kv = _MTP_KV_RE.search(line)
        if kv:
            mtp = max(mtp, int(kv.group(1)))
    def _i(key: str) -> Optional[int]:
        v = _num(raw.get(key, ""))
        return int(v) if v is not None else None
    for k, v in raw.items():
        lk = k.lower()
        if "nextn" in lk or "mtp" in lk:
            n = _num(v)
            if n:
                mtp = max(mtp, int(n))
    size = raw.get("model size") or raw.get("file size", "")
    return {"arch": raw.get("arch"), "name": raw.get("general.name"),
            "n_layer": _i("n_layer"), "n_expert": _i("n_expert"), "n_expert_used": _i("n_expert_used"),
            "n_ctx_train": _i("n_ctx_train"), "model_size_gib": _num(size),
            "model_params_b": _num(raw.get("model params", "")), "mtp_layers": mtp}


def parse_kl(text: str) -> Optional[float]:
    m = _KL_RE.search(text or "")
    return float(m.group(1)) if m else None


def physical_cores(cpuinfo: str, logical: int) -> dict:
    """Distinct (physical id, core id) pairs; falls back to half the logical count."""
    cores: set = set()
    phys = core = None
    for line in (cpuinfo or "").splitlines():
        k, _, v = line.partition(":")
        k = k.strip()
        if k == "physical id":
            phys = v.strip()
        elif k == "core id":
            core = v.strip()
            cores.add((phys, core))
    n = len(cores) if cores else max(1, int(logical) // 2)
    return {"physical": n, "logical": int(logical)}


def family_prefix(name: str) -> str:
    base = (name or "").split("/")[-1]
    m = _SIZE_TOKEN_RE.search(base)
    return (base[:m.start()] if m else base).lower()


def find_draft(drafts: list, target_repo: str, target_size: int) -> Optional[dict]:
    """Smallest cached GGUF of the same family at most 1/8 of the target's bytes."""
    fam = family_prefix(target_repo)
    cands = [d for d in (drafts or [])
             if family_prefix(d.get("repo", "")) == fam and "dflash" not in d.get("file", "").lower()
             and 0 < int(d.get("size") or 0) <= int(target_size or 0) / 8]
    return min(cands, key=lambda d: d["size"]) if cands else None


def find_dflash(drafts: list, target_repo: str) -> Optional[dict]:
    fam = family_prefix(target_repo)
    for d in drafts or []:
        if "dflash" in d.get("file", "").lower() and family_prefix(d.get("repo", "")) == fam:
            return d
    return None


def expand_spec_types(types: list, facts: dict, drafts: list, target_repo: str, target_size: int,
                      draft_model: str, current_type: Optional[str] = None,
                      current_draft: str = "") -> list[dict]:
    """Rows {type, draft} to try, in order; types with no usable draft are dropped."""
    if draft_model == "none":
        simple = None
    elif draft_model == "auto":
        d = find_draft(drafts, target_repo, target_size)
        simple = d["path"] if d else None
    else:
        simple = draft_model
    df = find_dflash(drafts, target_repo)
    avail = {"draft-mtp": None if (facts or {}).get("mtp_layers") else False,
             "draft-dflash": df["path"] if df else False,
             "draft-simple": simple if simple else False,
             "ngram-simple": None}
    order = ["draft-mtp", "draft-dflash", "draft-simple", "ngram-simple"] if "auto" in types \
        else [t for t in types if t in avail]
    if "auto" in types:
        order += [t for t in types if t != "auto" and t not in order]
    rows = []
    for t in order:
        a = avail.get(t, False)
        if a is False:
            continue
        rows.append({"type": t, "draft": a})
    cur = (current_type or "").strip().lower()
    if cur in SPEC_TYPES and cur not in ("auto", "none") and cur not in {r["type"] for r in rows}:
        cd = (current_draft or "").strip() or simple
        if cur not in SPEC_NEEDS_DRAFT or cd:
            rows.insert(0, {"type": cur, "draft": cd if cur in SPEC_NEEDS_DRAFT else None})
    return rows


def _ok(rows: list) -> list:
    return [r for r in rows if r.get("ok")]


def choose_kv(objective: str, results: list, min_ctx_per_slot: int) -> tuple[Optional[str], str]:
    """results rows: {value, ok, ctx, kl, guard_pass, lossy}; f16 row is the reference."""
    def passes(r):
        return r.get("ok") and (not r.get("lossy") or r.get("guard_pass") is True) and r.get("guard_pass") is not False
    f16 = next((r for r in results if r["value"] == "f16"), None)
    ok = [r for r in results if passes(r)]
    if not ok:
        return ("f16", "no candidate passed") if f16 else (None, "no candidate passed")
    order = {t: i for i, t in enumerate(("f32", "bf16", "f16", "q8_0", "q5_1", "q5_0", "iq4_nl", "q4_1", "q4_0"))}
    if objective == "fit":
        best = max(ok, key=lambda r: order.get(r["value"], -1))
        return best["value"], f"smallest passing type · ctx {best.get('ctx')}"
    q8 = next((r for r in ok if r["value"] == "q8_0"), None)
    if objective == "speed":
        if f16 and f16.get("ok") and int(f16.get("ctx") or 0) >= min_ctx_per_slot:
            return "f16", "fastest type reaches the ctx floor"
        if q8:
            return "q8_0", "f16 cannot reach the ctx floor"
        return ("f16", "f16 kept") if f16 else (ok[0]["value"], "first passing type")
    if q8 and f16 and f16.get("ctx"):
        gain = gain_pct(q8.get("ctx"), f16.get("ctx"))
        if gain is not None and gain >= KV_MIN_CTX_GAIN_PCT:
            return "q8_0", f"KL {q8.get('kl')} · ctx +{gain:.0f} % over f16"
        return "f16", "q8_0 gains under 10 % ctx"
    if q8 and not f16:
        return "q8_0", "q8_0 passes"
    return "f16", "f16 kept"


def choose_threads(results: list) -> tuple[Optional[int], str]:
    ok = _ok(results)
    if not ok:
        return None, "no candidate loaded"
    top = max(r["decode_tps"] or 0 for r in ok)
    tied = [r for r in ok if (r["decode_tps"] or 0) >= top * (1 - THREADS_TIE_PCT / 100)]
    best = min(tied, key=lambda r: int(r["value"]))
    return int(best["value"]), f"{best['decode_tps']:.1f} t/s" + (" · tie → fewer threads" if len(tied) > 1 else "")


def choose_spec(results: list) -> tuple[Optional[str], str]:
    none = next((r for r in results if r["value"] == "none" and r.get("ok")), None)
    base = (none or {}).get("decode_tps") or 0
    ok = [r for r in _ok(results) if r["value"] != "none"]
    if not ok:
        return "none", "no speculative type loaded"
    best = max(ok, key=lambda r: r["decode_tps"] or 0)
    g = gain_pct(best["decode_tps"], base)
    if g is not None and g >= SPEC_MIN_GAIN_PCT:
        acc = best.get("accept")
        return best["value"], f"+{g:.0f} % decode" + (f" · accept {acc * 100:.0f} %" if isinstance(acc, (int, float)) else "")
    return "none", "no type gained 5 % over none"


def window_plan(n_min: int, n_max: int) -> list[tuple[int, Optional[int]]]:
    """(n_min, n_max) pairs to try: n_max ∈ {4,8,12,16} within (n_min, n_max], then n_min ∈ {0,2}."""
    plan = [(n_min, m) for m in (4, 8, 12, 16) if n_min < m <= n_max]
    plan += [(m, None) for m in (0, 2) if m != n_min]
    return plan


def choose_slots(objective: str, results: list) -> tuple[Optional[int], str]:
    ok = _ok(results)
    if not ok:
        return None, "no candidate loaded"
    one = next((r for r in ok if int(r["value"]) == 1), ok[0])
    if objective in ("fit", "speed"):
        return 1, "single slot for this objective"
    if objective == "balanced":
        floor = (one["decode_tps"] or 0) * SLOTS_BALANCED_FLOOR
        keep = [r for r in ok if (r["decode_tps"] or 0) >= floor]
        best = max(keep, key=lambda r: int(r["value"]))
        return int(best["value"]), f"per-request decode ≥ 70 % of 1 slot · aggregate {best.get('agg_tps') or 0:.0f} t/s"
    best = max(ok, key=lambda r: r.get("agg_tps") or 0)
    return int(best["value"]), f"max aggregate {best.get('agg_tps') or 0:.0f} t/s"


def bisect_smallest(lo: int, hi: int, fits: Callable[[int], bool]) -> tuple[Optional[int], list]:
    """Smallest n in [lo, hi] with fits(n) true, assuming fits is monotonic."""
    tried: list = []
    if lo > hi:
        return None, tried
    if lo == hi:
        ok = bool(fits(lo))
        tried.append((lo, ok))
        return (lo if ok else None), tried
    best: Optional[int] = None
    a, b = lo, hi
    while a <= b:
        mid = (a + b) // 2
        ok = bool(fits(mid))
        tried.append((mid, ok))
        if ok:
            best = mid
            b = mid - 1
        else:
            a = mid + 1
    return best, tried


def budget_ok(elapsed_s: float, est_s: float, budget_s: float) -> bool:
    return (elapsed_s + est_s) <= budget_s


def estimate(stage: str, n_candidates: int, load_s: float, stick_s: float) -> int:
    """Seconds: loads per candidate × load time + stick time (+ KL pass for kv)."""
    n = max(1, int(n_candidates))
    per = {"context": 5 * load_s + 90, "kv": n * (4 * load_s + stick_s + 90),
           "moe": 5 * load_s + 2 * stick_s, "threads": n * (load_s + stick_s),
           "spec": n * (load_s + stick_s), "slots": n * (load_s + stick_s),
           "sampling": 5, "verify": load_s + 60}
    return int(round(per.get(stage, load_s)))


def gain_pct(after: Any, before: Any) -> Optional[float]:
    try:
        a, b = float(after), float(before)
    except (TypeError, ValueError):
        return None
    if not b or not math.isfinite(a) or not math.isfinite(b):
        return None
    return (a - b) / b * 100.0


_CHANGE_ORDER = ("ctx-size", "cache-type-k", "cache-type-v", "n-cpu-moe", "threads", "threads-batch",
                 "spec-type", "model-draft", "gpu-layers-draft", "cache-type-k-draft", "cache-type-v-draft",
                 "spec-draft-n-min", "spec-draft-n-max", "spec-draft-p-min", "parallel")


def build_changes(current: dict, rec: dict, evidence: dict) -> list[dict]:
    rows = []
    keys = [k for k in _CHANGE_ORDER if k in rec] + [k for k in rec if k not in _CHANGE_ORDER]
    for k in keys:
        cur = str((current or {}).get(k, "") or "")
        new = str(rec[k])
        if cur == new:
            continue
        ev = (evidence or {}).get(k) or {}
        g = ev.get("gain_pct")
        rows.append({"key": k, "current": cur, "recommended": new,
                     "source": ev.get("source", "measured"), "evidence": ev.get("text", ""),
                     "selected": g is None or g >= GAIN_MIN_PCT})
    return rows


def ledger_summary(done: dict) -> dict:
    after = done.get("after") or {}
    before = done.get("before") or {}
    stages = done.get("stages") or []
    guard = done.get("guard") or {}
    return {"objective": done.get("objective"), "mode": done.get("mode") or "tune",
            "llama_build": done.get("llama_build") or None,
            "ctx_size": after.get("ctx"), "free_mb": after.get("free_mb"),
            "decode_tps": after.get("decode_tps"),
            "gain_pct": gain_pct(after.get("decode_tps"), before.get("decode_tps")),
            "stages_done": sum(1 for s in stages if s.get("status") == "done"),
            "verify_ok": (done.get("verify") or {}).get("ok"), "wh_per_ktok": after.get("wh_per_ktok"),
            "n_expert": (done.get("facts") or {}).get("n_expert"),
            "kl": guard.get("kl"), "kl_pass": guard.get("pass"), "regressed": done.get("regressed")}


_KL_SAFE_VALUE = {"--cache-type-k", "--cache-type-v", "-ctk", "-ctv", "--threads", "-t", "--threads-batch", "-tb",
                  "--n-gpu-layers", "-ngl", "--n-cpu-moe", "-ncmoe", "--batch-size", "-b", "--ubatch-size", "-ub"}
_KL_SAFE_FLAG = {"--flash-attn", "-fa", "--no-mmap", "--mlock"}


def kl_args(args: list) -> list[str]:
    """Subset of a server argv that llama-perplexity accepts."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = str(args[i])
        if a in _KL_SAFE_VALUE and i + 1 < len(args):
            out += [a, str(args[i + 1])]
            i += 2
            continue
        if a in _KL_SAFE_FLAG:
            out.append(a)
        i += 1
    return out


def spawn_cmd(bin_path: str, hf_arg: str, port: int, fitt: Optional[int], ctx: Optional[int], extra: list) -> list[str]:
    """llama-server argv for one tuning load: -fitt convergence, an explicit ctx with fit
    off, or neither flag (baseline load of the current config)."""
    cmd = [str(bin_path), "--models-max", "1", "-lv", "4", "--host", "127.0.0.1", "--port", str(int(port))]
    if ctx is not None:
        cmd += ["--fit", "off", "-c", str(int(ctx))]
    elif fitt is not None:
        cmd += ["-fitt", str(int(fitt))]
    cmd += [str(t) for t in (extra or [])]
    cmd += ["-hf", hf_arg]
    return cmd


# ── stage engine ────────────────────────────────────────────────────

DIM_KEYS = {
    "slots": ("parallel",),
    "spec": ("spec-type", "model-draft", "gpu-layers-draft", "cache-type-k-draft", "cache-type-v-draft",
             "spec-draft-n-min", "spec-draft-n-max", "spec-draft-p-min"),
    "threads": ("threads", "threads-batch"),
}
# The measuring stick: single-turn 1k-token prompts, fixed output length.
STICK_BENCH = "throughput_1k"
STICK_ISL = 1024
STICK_OSL = 256
STICK_LIMIT = 4
VERIFY_SECONDS = 60
DEFAULT_PREFILL_TPS = 2000.0


class Cancelled(Exception):
    pass


def verify_limit(decode_tps: Any, prefill_tps: Any, concurrency: int) -> int:
    """Requests that keep the server busy ~60 s at the chosen concurrency (4–64), prefill included."""
    try:
        tps = float(decode_tps)
    except (TypeError, ValueError):
        return STICK_LIMIT
    if not tps or tps <= 0:
        return STICK_LIMIT
    try:
        pre = float(prefill_tps)
    except (TypeError, ValueError):
        pre = 0.0
    if pre <= 0:
        pre = DEFAULT_PREFILL_TPS
    per_req = STICK_ISL / pre + STICK_OSL / tps
    n = math.ceil(VERIFY_SECONDS / per_req) * max(1, int(concurrency))
    return max(STICK_LIMIT, min(64, n))


class _Run:
    def __init__(self, model_id, section, req, backend, put, cancelled, env, clock):
        self.model_id, self.section, self.req = model_id, dict(section or {}), req
        self.backend, self.put, self.cancelled, self.env, self.clock = backend, put, cancelled, env, clock
        self.dims, self.objective = req["dims"], req["objective"]
        self.mode = req.get("mode") or "tune"
        self.regressed: Optional[bool] = None
        self.budget_s = int(req["budget_min"]) * 60
        self.runtime, self.perplexity = bool(env.get("runtime")), bool(env.get("perplexity"))
        self.rec: dict = {}
        self.evidence: dict = {}
        self.stages: list = []
        self.facts: dict = {}
        self.before: Optional[dict] = None
        self.after: Optional[dict] = None
        self.guard: Optional[dict] = None
        self.verify: Optional[dict] = None
        self.ctx_total: Optional[int] = None
        self.fitt_best: Optional[int] = None
        self.free_mb: Optional[int] = None
        self.load_s, self.stick_s = 60.0, 30.0
        self.decode_now: Optional[float] = None
        self.prefill_now: Optional[float] = None
        self.concurrency = 1
        self.loads = 0
        self.budget_hit = False
        self.stop_reason: Optional[str] = None
        self.t0 = clock()

    # ── plumbing ──
    def elapsed(self) -> float:
        return self.clock() - self.t0

    def emit(self, typ: str, **kw) -> None:
        self.put({"type": typ, "model_id": self.model_id, **kw})

    def cur(self, key: str, default: str = "") -> str:
        v = self.section.get(key)
        return default if v in (None, "") else str(v)

    def args(self, extra: Optional[dict] = None) -> list[str]:
        ov = dict(self.rec)
        ov.update(extra or {})
        return section_args(self.section, ov, valued=self.env.get("valued")) \
            + list(self.dims["context"].get("custom_args") or [])

    def check_cancel(self) -> None:
        if self.cancelled():
            raise Cancelled()

    def begin(self, stage: str, candidates: list, est: float) -> dict:
        self.emit("stage_start", stage=stage, candidates=candidates, est_s=int(est))
        return {"t": self.elapsed(), "loads": self.loads}

    def end(self, stage: str, mark: dict, choice: Any, reason: str) -> None:
        sec = int(round(self.elapsed() - mark["t"]))
        loads = self.loads - mark["loads"]
        self.stages.append({"stage": stage, "status": "done", "seconds": sec, "loads": loads,
                            "choice": None if choice is None else str(choice)})
        self.emit("stage_done", stage=stage, choice=None if choice is None else str(choice),
                  reason=reason, seconds=sec, loads=loads)

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
        if self.budget_hit or not budget_ok(self.elapsed(), est, self.budget_s):
            self.budget_hit = True
            self.skip(stage, "budget")
            return False
        return True

    def note_facts(self, facts: Optional[dict]) -> None:
        if facts and not self.facts.get("n_layer") and facts.get("n_layer"):
            self.facts = dict(facts)
            self.emit("facts", **self.facts)

    def load(self, extra: dict, ctx: Optional[int], measure: Optional[dict]) -> dict:
        self.loads += 1
        res = self.backend.load(self.args(extra), ctx, measure) or {}
        if res.get("load_s"):
            self.load_s = float(res["load_s"])
        self.note_facts(res.get("facts"))
        return res

    def measure(self, extra: dict, ctx: Optional[int], concurrency: int = 1, limit: int = STICK_LIMIT) -> tuple[dict, dict]:
        m = {"concurrency": int(concurrency), "limit": int(limit)} if self.runtime else None
        res = self.load(extra, ctx, m)
        return res, (res.get("stick") or {})

    def result(self, stage: str, value: Any, res: dict, **extra) -> None:
        st = res.get("stick") or {}
        self.emit("candidate_result", stage=stage, value=value, ok=bool(res.get("ok")) and (not st or bool(st.get("ok"))),
                  ctx=res.get("ctx"), free_mb=res.get("free_mb"), decode_tps=st.get("decode_tps"),
                  agg_tps=st.get("agg_tps"), accept=st.get("accept"), kl=extra.get("kl"),
                  guard_pass=extra.get("guard_pass"), error=res.get("error") or st.get("error"))

    def current_window(self) -> Optional[tuple]:
        """(n_min, n_max) from the section's spec-draft window, when n_max parses."""
        try:
            n_max = int(self.cur("spec-draft-n-max"))
        except ValueError:
            return None
        try:
            n_min = int(self.cur("spec-draft-n-min") or 0)
        except ValueError:
            n_min = 0
        return (n_min, n_max) if 0 <= n_min < n_max else None

    def floor_target(self) -> int:
        if self.objective == "fit":
            return int(self.ctx_total or 0)
        return int(self.dims["slots"]["min_ctx_per_slot"])

    def ev(self, key: str, text: str, gain: Optional[float] = None, source: str = "measured") -> None:
        self.evidence[key] = {"source": source, "text": text, "gain_pct": gain}

    # ── stages ──
    def stage_context(self) -> bool:
        d = self.dims["context"]
        mark = self.begin("context", ["baseline", f"-fitt {d['target_mb']}"], estimate("context", 1, self.load_s, self.stick_s))
        self.emit("candidate_start", stage="context", value="baseline")
        try:
            cur_ctx = int(self.cur("ctx-size") or 0) or None
        except ValueError:
            cur_ctx = None
        base, st = self.measure({}, cur_ctx)
        self.result("context", "baseline", base)
        if base.get("ok"):
            self.before = {"decode_tps": st.get("decode_tps"), "prefill_tps": st.get("prefill_tps"),
                           "ctx": base.get("ctx"), "agg_tps": st.get("agg_tps"), "free_mb": base.get("free_mb")}
            self.decode_now = st.get("decode_tps")
            self.prefill_now = st.get("prefill_tps")
            if st.get("decode_tps"):
                self.stick_s = max(5.0, STICK_LIMIT * STICK_OSL / float(st["decode_tps"]) + 10.0)
        self.check_cancel()
        conv = self.backend.converge(self.args(), int(d["target_mb"]), int(d["tolerance_mb"]), None, 10) or {}
        self.loads += int(conv.get("iters") or 0)
        if conv.get("load_s"):
            self.load_s = float(conv["load_s"])
        self.note_facts(conv.get("facts"))
        if not conv.get("ok"):
            self.stages.append({"stage": "context", "status": "failed", "reason": conv.get("stop_reason"),
                                "seconds": int(round(self.elapsed() - mark["t"])),
                                "loads": self.loads - mark["loads"], "choice": None})
            self.emit("stage_done", stage="context", choice=None, reason=conv.get("stop_reason") or "failed",
                      seconds=int(self.elapsed() - mark["t"]), loads=self.loads - mark["loads"])
            self.stop_reason = conv.get("stop_reason") or "context stage failed"
            return False
        self.ctx_total = int(conv["ctx"])
        self.fitt_best = conv.get("fitt")
        self.free_mb = conv.get("free_mb")
        self.rec["ctx-size"] = str(self.ctx_total)
        self.ev("ctx-size", f"free {conv.get('free_mb')} MB after fit · {conv.get('iters')} loads")
        self.end("context", mark, self.ctx_total, conv.get("stop_reason") or "converged")
        return True

    def stage_kv(self) -> None:
        if not self.on("kv"):
            return
        d = self.dims["kv"]
        curk, curv = self.cur("cache-type-k", "f16"), self.cur("cache-type-v", "f16")
        cands = [c for c in d["candidates"] if not (c == curk and c == curv)]
        if not cands:
            self.skip("kv", "no_candidates")
            return
        est = estimate("kv", len(cands), self.load_s, self.stick_s)
        if not self.within_budget("kv", est):
            return
        mark = self.begin("kv", cands, est)
        cur_label = curk if curk == curv else f"{curk}/{curv}"
        results = [{"value": cur_label, "ok": True, "ctx": self.ctx_total, "kl": None, "guard_pass": None,
                    "lossy": curk in KV_LOSSY, "current": True}]
        base_written = False
        for c in cands:
            self.check_cancel()
            self.emit("candidate_start", stage="kv", value=c)
            ov = {"cache-type-k": c, "cache-type-v": c}
            kl = guard_pass = None
            err = None
            lossy = c in KV_LOSSY
            if lossy and not self.perplexity:
                err = "unguarded: llama-perplexity not installed"
            elif self.perplexity and c not in ("f16", "f32", "bf16"):
                if not base_written:
                    b = self.backend.kl(self.args({"cache-type-k": "f16", "cache-type-v": "f16"}), True) or {}
                    base_written = bool(b.get("ok"))
                    if not base_written:
                        err = f"KL base failed: {b.get('error') or 'unknown'}"
                if base_written:
                    k = self.backend.kl(self.args(ov), False) or {}
                    kl = k.get("kl")
                    guard_pass = bool(k.get("ok")) and kl is not None and kl <= float(d["guard_kl_max"])
                    if not k.get("ok"):
                        err = f"KL failed: {k.get('error') or 'unknown'}"
            # A rejected candidate is never re-converged; the guard already ruled it out.
            if not err and guard_pass is False:
                err = f"failed guard (KL {kl} > {d['guard_kl_max']})"
            if err:
                results.append({"value": c, "ok": False, "ctx": None, "kl": kl, "guard_pass": guard_pass,
                                "lossy": lossy, "error": err})
                self.emit("candidate_result", stage="kv", value=c, ok=False, ctx=None, free_mb=None, kl=kl,
                          guard_pass=guard_pass, error=err)
                continue
            conv = self.backend.converge(self.args(ov), int(self.dims["context"]["target_mb"]),
                                         int(self.dims["context"]["tolerance_mb"]), self.fitt_best, 4) or {}
            self.loads += int(conv.get("iters") or 0)
            row = {"value": c, "ok": bool(conv.get("ok")), "ctx": conv.get("ctx"), "kl": kl,
                   "guard_pass": guard_pass, "lossy": lossy, "free_mb": conv.get("free_mb")}
            results.append(row)
            self.emit("candidate_result", stage="kv", value=c, ok=row["ok"], ctx=row["ctx"], free_mb=row["free_mb"],
                      kl=kl, guard_pass=guard_pass, error=None if row["ok"] else (conv.get("stop_reason") or "did not fit"))
        choice, reason = choose_kv(self.objective, results, int(self.dims["slots"]["min_ctx_per_slot"]))
        chosen = next((r for r in results if r["value"] == choice), None)
        failed = [f"{r['value']} failed guard ({r['kl']})" for r in results if r.get("guard_pass") is False]
        if chosen and not chosen.get("current") and chosen.get("ok"):
            self.rec["cache-type-k"] = choice
            self.rec["cache-type-v"] = choice
            self.ctx_total = int(chosen["ctx"])
            self.free_mb = chosen.get("free_mb")
            self.rec["ctx-size"] = str(self.ctx_total)
            text = reason + (f" · KL {chosen['kl']}" if chosen.get("kl") is not None else "")
            self.ev("cache-type-k", " · ".join([text] + failed))
            self.ev("cache-type-v", "same pass as K")
            self.ev("ctx-size", f"free {self.free_mb} MB after re-fit with {choice} KV")
        rejected = [f"{r['value']} KL {r['kl']}" for r in results if r.get("guard_pass") is False]
        if chosen is not None and chosen.get("kl") is not None:
            self.guard = {"kl": chosen["kl"], "max": float(d["guard_kl_max"]),
                          "pass": bool(chosen.get("guard_pass")),
                          "text": f"{chosen['value']} KV vs f16 · "
                                  + ("pass" if chosen.get("guard_pass") else "guard failed")}
        else:
            # Chosen row carries no KL (f16 kept): the recommendation itself is unguarded-safe.
            self.guard = {"kl": None, "max": float(d["guard_kl_max"]), "pass": True,
                          "text": f"{(chosen or {}).get('value') or choice or cur_label} needs no guard"
                                  + (" · rejected " + ", ".join(rejected) if rejected else "")}
        self.end("kv", mark, choice, reason)

    def stage_moe(self) -> None:
        if not self.on("moe"):
            return
        if int(self.facts.get("n_expert") or 0) <= 1:
            self.skip("moe", "not_moe")
            return
        floor = self.floor_target()
        if self.ctx_total and self.ctx_total >= floor:
            self.skip("moe", "fits")
            return
        d = self.dims["moe"]
        est = estimate("moe", 1, self.load_s, self.stick_s)
        if not self.within_budget("moe", est):
            return
        mark = self.begin("moe", [f"{d['min']}…{d['max']}"], est)

        def fits(n: int) -> bool:
            self.check_cancel()
            self.emit("candidate_start", stage="moe", value=n)
            res = self.load({"n-cpu-moe": str(n)}, floor, None)
            self.result("moe", n, res)
            return bool(res.get("ok"))

        n, tried = bisect_smallest(max(1, int(d["min"])), int(d["max"]), fits)
        if n is None:
            self.end("moe", mark, None, f"no offload count fits {floor} ctx")
            return
        self.ctx_total = floor
        self.rec["ctx-size"] = str(floor)
        self.rec["n-cpu-moe"] = str(n)
        if self.runtime:
            for v in [n] + sorted(x for x, ok in tried if ok and x > n)[:1]:
                self.check_cancel()
                self.emit("candidate_start", stage="moe", value=v)
                res, st = self.measure({"n-cpu-moe": str(v)}, floor)
                self.result("moe", v, res)
                if v == n and res.get("ok"):
                    self.free_mb = res.get("free_mb")
                    self.decode_now = st.get("decode_tps")
                    self.prefill_now = st.get("prefill_tps") or self.prefill_now
        oom = [str(x) for x, ok in tried if not ok]
        self.ev("n-cpu-moe", f"fewest layers that fit {floor} ctx" + (f" · {', '.join(oom)} OOM" if oom else ""))
        self.ev("ctx-size", f"{floor} ctx reached with {n} expert layers on CPU")
        self.end("moe", mark, n, "smallest offload that fits")

    def stage_threads(self) -> None:
        if not self.on("threads") or not self.need_runtime("threads"):
            return
        d = self.dims["threads"]
        phys = int((self.env.get("cores") or {}).get("physical") or 0)
        cands = list(d["candidates"]) or sorted({max(1, phys // 2), max(1, phys * 3 // 4), max(1, phys)})
        est = estimate("threads", len(cands), self.load_s, self.stick_s)
        if not self.within_budget("threads", est):
            return
        mark = self.begin("threads", cands, est)
        results = []
        for t in cands:
            self.check_cancel()
            self.emit("candidate_start", stage="threads", value=t)
            res, st = self.measure({"threads": str(t)}, self.ctx_total)
            results.append({"value": t, "ok": bool(res.get("ok")) and bool(st.get("ok")),
                            "decode_tps": st.get("decode_tps"), "prefill_tps": st.get("prefill_tps")})
            self.result("threads", t, res)
        choice, reason = choose_threads(results)
        if choice is None:
            self.end("threads", mark, None, reason)
            return
        best = next(r for r in results if r["value"] == choice)
        cur_t = self.cur("threads")
        ref = next((r["decode_tps"] for r in results if str(r["value"]) == cur_t and r["ok"]), self.decode_now)
        if str(choice) != cur_t:
            self.rec["threads"] = str(choice)
            self.ev("threads", reason, gain_pct(best["decode_tps"], ref))
        self.decode_now = best["decode_tps"]
        self.prefill_now = best.get("prefill_tps") or self.prefill_now
        tb = phys if self.rec.get("n-cpu-moe") and phys else choice
        cur_tb = self.cur("threads-batch")
        if (self.rec.get("n-cpu-moe") and str(tb) != cur_tb) or (cur_tb and cur_tb != str(tb)):
            self.rec["threads-batch"] = str(tb)
            self.ev("threads-batch", "physical cores with MoE offload on" if self.rec.get("n-cpu-moe") else "matches threads")
        self.end("threads", mark, choice, reason)

    def stage_spec(self) -> None:
        if not self.on("spec") or not self.need_runtime("spec"):
            return
        d = self.dims["spec"]
        rows = expand_spec_types(d["types"], self.facts, self.env.get("drafts") or [],
                                 self.env.get("target_repo") or "", int(self.env.get("target_size") or 0),
                                 d["draft_model"], current_type=self.cur("spec-type"),
                                 current_draft=self.cur("model-draft"))
        if not rows:
            self.skip("spec", "no_candidates")
            return
        est = estimate("spec", len(rows) + 1 + len(window_plan(int(d["n_min"]), int(d["n_max"]))), self.load_s, self.stick_s)
        if not self.within_budget("spec", est):
            return
        mark = self.begin("spec", ["none"] + [r["type"] for r in rows], est)
        kvc = self.rec.get("cache-type-k") or self.cur("cache-type-k", "f16")

        def spec_ov(row: dict, n_min: int, n_max: int) -> dict:
            ov = {"spec-type": row["type"], "spec-draft-n-min": str(n_min), "spec-draft-n-max": str(n_max),
                  "spec-draft-p-min": str(d["p_min"])}
            if row.get("draft"):
                ov.update({"model-draft": row["draft"], "gpu-layers-draft": "99",
                           "cache-type-k-draft": kvc, "cache-type-v-draft": kvc})
            return ov

        results = []
        self.check_cancel()
        self.emit("candidate_start", stage="spec", value="none")
        res, st = self.measure({"spec-type": "none"}, self.ctx_total)
        results.append({"value": "none", "ok": bool(res.get("ok")) and bool(st.get("ok")),
                        "decode_tps": st.get("decode_tps"), "prefill_tps": st.get("prefill_tps"), "accept": None})
        self.result("spec", "none", res)
        n_max0 = min(int(d["n_max"]), 8)
        for row in rows:
            self.check_cancel()
            self.emit("candidate_start", stage="spec", value=row["type"])
            res, st = self.measure(spec_ov(row, int(d["n_min"]), n_max0), self.ctx_total)
            results.append({"value": row["type"], "ok": bool(res.get("ok")) and bool(st.get("ok")),
                            "decode_tps": st.get("decode_tps"), "prefill_tps": st.get("prefill_tps"),
                            "accept": st.get("accept")})
            self.result("spec", row["type"], res)
        choice, reason = choose_spec(results)
        none_tps = results[0]["decode_tps"]
        others = ", ".join(f"{r['value']} {gain_pct(r['decode_tps'], none_tps):+.0f} %" for r in results[1:]
                           if r["ok"] and gain_pct(r["decode_tps"], none_tps) is not None)
        cur_t = self.cur("spec-type").strip().lower()
        cur_row = next((r for r in results if r["value"] == cur_t and r["ok"]), None)
        if choice == "none":
            if cur_t not in ("", "none"):
                self.rec["spec-type"] = "none"
                self.ev("spec-type", f"{reason} · {others}",
                        gain_pct(none_tps, (cur_row or {}).get("decode_tps")))
            self.decode_now = none_tps or self.decode_now
            self.prefill_now = results[0].get("prefill_tps") or self.prefill_now
            self.end("spec", mark, "none", reason)
            return
        winrow = next(r for r in rows if r["type"] == choice)
        win = next(r for r in results if r["value"] == choice)
        probe = {"n_min": int(d["n_min"]), "n_max": n_max0, "decode_tps": win["decode_tps"],
                 "prefill_tps": win.get("prefill_tps"), "accept": win["accept"]}
        best = probe
        tried = {(probe["n_min"], probe["n_max"])}
        cur_win = self.current_window() if choice == cur_t else None
        if cur_win and cur_win not in tried:
            self.check_cancel()
            label = f"{choice} {cur_win[0]}–{cur_win[1]} (current)"
            self.emit("candidate_start", stage="spec", value=label)
            res, st = self.measure(spec_ov(winrow, cur_win[0], cur_win[1]), self.ctx_total)
            self.result("spec", label, res)
            tried.add(cur_win)
            if res.get("ok") and st.get("ok"):
                best = {"n_min": cur_win[0], "n_max": cur_win[1], "decode_tps": st.get("decode_tps"),
                        "prefill_tps": st.get("prefill_tps"), "accept": st.get("accept")}
                if (probe["decode_tps"] or 0) > (best["decode_tps"] or 0):
                    best = probe
        for n_min, n_max in window_plan(int(d["n_min"]), int(d["n_max"])):
            n_max = best["n_max"] if n_max is None else n_max
            if (n_min, n_max) in tried or n_min >= n_max:
                continue
            self.check_cancel()
            label = f"{choice} {n_min}–{n_max}"
            self.emit("candidate_start", stage="spec", value=label)
            res, st = self.measure(spec_ov(winrow, n_min, n_max), self.ctx_total)
            self.result("spec", label, res)
            tried.add((n_min, n_max))
            if res.get("ok") and st.get("ok") and (st.get("decode_tps") or 0) > (best["decode_tps"] or 0):
                best = {"n_min": n_min, "n_max": n_max, "decode_tps": st.get("decode_tps"),
                        "prefill_tps": st.get("prefill_tps"), "accept": st.get("accept")}
        window = f"window {best['n_min']}–{best['n_max']}"
        if cur_win and (best["n_min"], best["n_max"]) == cur_win:
            self.decode_now = best["decode_tps"] or self.decode_now
            self.prefill_now = best.get("prefill_tps") or self.prefill_now
            self.end("spec", mark, choice, f"already configured · no better window · {window}")
            return
        ov = spec_ov(winrow, best["n_min"], best["n_max"])
        if self.cur("spec-draft-p-min") == ov["spec-draft-p-min"]:
            del ov["spec-draft-p-min"]
        self.rec.update(ov)
        g = gain_pct(best["decode_tps"], none_tps)
        acc = best.get("accept")
        self.ev("spec-type", " · ".join(x for x in [
            reason, f"accept {acc * 100:.0f} %" if isinstance(acc, (int, float)) else "", others] if x), g)
        win_text = f"window {best['n_min']}–{best['n_max']} best of {len(window_plan(int(d['n_min']), int(d['n_max']))) + 1} tried"
        for k in ("spec-draft-n-min", "spec-draft-n-max"):
            self.ev(k, win_text)
        if "spec-draft-p-min" in ov:
            self.ev("spec-draft-p-min", "as configured in the tuning plan")
        if ov.get("model-draft"):
            self.ev("model-draft", f"{Path(ov['model-draft']).name} found in the HF cache")
            self.ev("gpu-layers-draft", "draft fully on GPU")
            self.ev("cache-type-k-draft", f"follows the {kvc} KV choice")
            self.ev("cache-type-v-draft", f"follows the {kvc} KV choice")
        self.decode_now = best["decode_tps"]
        self.prefill_now = best.get("prefill_tps") or self.prefill_now
        self.end("spec", mark, choice, f"{reason} · {window}")

    def stage_slots(self) -> None:
        if not self.on("slots"):
            return
        if self.cur("kv-unified").lower() == "true":
            self.skip("slots", "kv_unified")
            return
        if not self.need_runtime("slots"):
            return
        d = self.dims["slots"]
        cap = max(1, int(self.ctx_total or 0) // int(d["min_ctx_per_slot"]))
        cands = [c for c in d["candidates"] if int(c) <= cap] or [1]
        est = estimate("slots", len(cands), self.load_s, self.stick_s)
        if not self.within_budget("slots", est):
            return
        mark = self.begin("slots", cands, est)
        results = []
        for c in cands:
            self.check_cancel()
            self.emit("candidate_start", stage="slots", value=c)
            res, st = self.measure({"parallel": str(c)}, self.ctx_total, concurrency=int(c), limit=max(STICK_LIMIT, 2 * int(c)))
            results.append({"value": c, "ok": bool(res.get("ok")) and bool(st.get("ok")),
                            "decode_tps": st.get("decode_tps"), "agg_tps": st.get("agg_tps")})
            self.result("slots", c, res)
        choice, reason = choose_slots(self.objective, results)
        if choice is None:
            self.end("slots", mark, None, reason)
            return
        one = next((r for r in results if int(r["value"]) == 1 and r["ok"]), None)
        best = next(r for r in results if int(r["value"]) == choice)
        if str(choice) != self.cur("parallel", "1"):
            self.rec["parallel"] = str(choice)
            self.ev("parallel", reason + f" · {int(self.ctx_total or 0) // choice} ctx per slot",
                    gain_pct(best.get("agg_tps"), (one or {}).get("agg_tps")))
        self.concurrency = int(choice)
        self.end("slots", mark, choice, reason)

    def stage_sampling(self) -> None:
        if self.on("sampling"):
            self.skip("sampling", "manager")

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
            limit = verify_limit(self.decode_now, self.prefill_now, conc)
            self.emit("candidate_start", stage="verify", value=attempt)
            if self.runtime:
                self.backend.energy_start()
            res, st = self.measure({}, self.ctx_total, concurrency=conc, limit=limit)
            wh, src = self.backend.energy_stop() if self.runtime else (None, None)
            self.result("verify", attempt, res)
            free = res.get("free_mb")
            free_ok = free is None or int(free) >= target - tol
            # Compute buffers are live under load, so a small shortfall warns instead of failing.
            soft = not free_ok and free is not None and int(free) >= 0.5 * target
            ok = bool(res.get("ok")) and (not self.runtime or bool(st.get("ok"))) and (free_ok or soft)
            if ok:
                warning = None
                if soft:
                    warning = f"free VRAM {int(free)} MB is below the {target} ± {tol} MB target"
                    self.emit("line", text=f"[autotune] {warning}")
                tokens = int(st.get("completion_tokens") or 0)
                self.after = {"decode_tps": st.get("decode_tps"), "prefill_tps": st.get("prefill_tps"),
                              "ctx": self.ctx_total, "agg_tps": st.get("agg_tps"), "free_mb": free,
                              "concurrency": conc, "accept": st.get("accept"),
                              "wh_per_ktok": (wh / (tokens / 1000.0)) if wh is not None and tokens > 0 else None,
                              "energy_wh": wh, "energy_source": src}
                self.verify = {"ok": True, "seconds": st.get("seconds") or 0, "free_mb": free, "dropped": dropped,
                               "reason": None, "warning": warning}
                self.end("verify", mark, "pass", f"{conc} slot(s) · {limit} requests" if self.runtime else "load only")
                return
            reason = res.get("error") or st.get("error") or ("free VRAM below target" if not free_ok else "load failed")
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
        self.emit("model_start", objective=self.objective, mode=self.mode, stages=planned,
                  budget_min=self.req["budget_min"], target_mb=self.dims["context"]["target_mb"],
                  tolerance_mb=self.dims["context"]["tolerance_mb"])
        ok = False
        cancelled = False
        try:
            ok = self.stage_context()
            if ok:
                for fn in (self.stage_kv, self.stage_moe, self.stage_threads, self.stage_spec,
                           self.stage_slots, self.stage_sampling, self.stage_verify):
                    self.check_cancel()
                    fn()
        except Cancelled:
            cancelled = True
            ok = False
            self.stop_reason = "cancelled"
        return self.done(ok, cancelled)

    def run_verify_only(self) -> dict:
        """Verify stage against the current section; before = the ledger's stored decode t/s."""
        self.emit("model_start", objective=self.objective, mode="verify", stages=["verify"],
                  budget_min=self.req["budget_min"], target_mb=self.dims["context"]["target_mb"],
                  tolerance_mb=self.dims["context"]["tolerance_mb"])
        try:
            self.ctx_total = int(self.cur("ctx-size") or 0) or None
        except ValueError:
            self.ctx_total = None
        try:
            self.concurrency = max(1, int(self.cur("parallel") or self.cur("np") or 1))
        except ValueError:
            self.concurrency = 1
        base = self.req.get("baseline_tps")
        self.before = {"decode_tps": base} if base is not None else None
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
        changes = build_changes(self.section, self.rec, self.evidence)
        if self.after is None and self.ctx_total:
            self.after = {"ctx": self.ctx_total, "free_mb": self.free_mb, "concurrency": 1}
        doc = {"type": "model_done", "model_id": self.model_id, "run_id": self.env.get("run_id"),
               "ok": bool(ok) and not cancelled, "cancelled": cancelled, "objective": self.objective,
               "mode": self.mode, "llama_build": self.env.get("llama_build") or None, "regressed": self.regressed,
               "facts": self.facts, "changes": changes, "before": self.before, "after": self.after,
               "guard": self.guard, "verify": self.verify, "stages": self.stages,
               "stop_reason": self.stop_reason, "elapsed_s": int(self.elapsed()), "loads": self.loads}
        self.put(doc)
        return doc


def run_model(model_id: str, section: dict, req: dict, backend, put, cancelled, env: dict,
              clock=time.monotonic) -> dict:
    """Runs every stage for one model and returns the emitted model_done document."""
    return _Run(model_id, section, req, backend, put, cancelled, env, clock).run()


def run_quality(model_id: str, section: dict, req: dict, backend, put, cancelled, env: dict,
                clock=time.monotonic) -> dict:
    """Two llama-perplexity passes: f16 base on the section, then the section with overrides applied."""
    t0 = clock()
    ov, kl_max, valued = dict(req.get("overrides") or {}), float(req.get("kl_max", 0.02)), env.get("valued")
    base_args = section_args(section, {"cache-type-k": "f16", "cache-type-v": "f16"}, valued=valued)
    cand_args = section_args(section, ov, valued=valued)

    def emit(typ, **kw):
        put({"type": typ, "model_id": model_id, **kw})

    emit("model_start", objective="quality", mode="quality", stages=["quality"], budget_min=0,
         target_mb=0, tolerance_mb=0)
    emit("stage_start", stage="quality", candidates=["f16 base", "candidate"],
         est_s=estimate("kv", 1, 0.0, 0.0))
    guard = {"kl": None, "kl_max": kl_max, "pass": None, "error": None}
    ok = False
    stop = None
    try:
        if cancelled():
            raise Cancelled()
        emit("candidate_start", stage="quality", value="f16 base")
        b = backend.kl(base_args, True) or {}
        emit("candidate_result", stage="quality", value="f16 base", ok=bool(b.get("ok")), kl=None,
             guard_pass=None, error=None if b.get("ok") else b.get("error"))
        if not b.get("ok"):
            guard["error"] = f"KL base failed: {b.get('error') or 'unknown'}"
        else:
            if cancelled():
                raise Cancelled()
            emit("candidate_start", stage="quality", value="candidate")
            k = backend.kl(cand_args, False) or {}
            kl = k.get("kl")
            if not k.get("ok") or kl is None:
                guard["error"] = f"KL failed: {k.get('error') or 'unknown'}"
            else:
                guard["kl"] = kl
                guard["pass"] = float(kl) <= kl_max
                ok = True
            emit("candidate_result", stage="quality", value="candidate", ok=ok, kl=kl,
                 guard_pass=guard["pass"], error=guard["error"])
    except Cancelled:
        stop = "cancelled"
    reason = stop or guard["error"] or (f"KL {guard['kl']} ≤ {kl_max}" if guard["pass"] else f"KL {guard['kl']} > {kl_max}")
    choice = "pass" if guard["pass"] else "fail"
    emit("stage_done", stage="quality", choice=choice, reason=reason,
         seconds=int(clock() - t0), loads=0)
    changes = [{"key": k, "current": section.get(k), "recommended": v} for k, v in ov.items()]
    doc = {"type": "model_done", "model_id": model_id, "run_id": env.get("run_id"), "ok": ok and stop is None,
           "cancelled": stop == "cancelled", "objective": "quality", "mode": "quality",
           "llama_build": env.get("llama_build") or None, "facts": {}, "changes": changes,
           "before": None, "after": None, "guard": guard, "verify": None,
           "stages": [{"stage": "quality", "status": "done" if ok else "failed", "reason": reason,
                       "seconds": int(clock() - t0), "loads": 0, "choice": choice}],
           "base_args": base_args, "cand_args": cand_args,
           "stop_reason": stop, "elapsed_s": int(clock() - t0), "loads": 0}
    put(doc)
    return doc
