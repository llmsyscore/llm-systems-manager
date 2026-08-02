"""GPU Report Card (#468): standardized cross-provider bench, storage, routes."""
from __future__ import annotations

import json
import logging
import queue as _queue
import statistics
import threading as _threading
import time as _time
import uuid as _uuid
from pathlib import Path

log = logging.getLogger("llm-systems-manager.report_card")

# ── Reference preset ─────────────────────────────────────────────────
# Changing any pinned value requires a new PRESET_VERSION.

PRESET_VERSION = "preset_v2"
GEN_TOKENS = 128
REPS = 10

# 4-bit class on every provider. The 7B GGUF ships sharded; `file` names
# shard 1 and llama.cpp pulls the rest.
REFERENCE_MODELS = [
    {"key": "small", "label": "Qwen2.5-1.5B-Instruct (4-bit)",
     "approx_gb": 1.1, "sources": {
        "llama": {"repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
                  "quant": "Q4_K_M",
                  "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
                  "patterns": ["qwen2.5-1.5b-instruct-q4_k_m.gguf"],
                  "model_id": "Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M",
                  "revision": "91cad51170dc346986eccefdc2dd33a9da36ead9"},
        "lms":   {"repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
                  "quant": "Q4_K_M",
                  "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
                  "patterns": ["qwen2.5-1.5b-instruct-q4_k_m.gguf"],
                  "model_id": "Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M",
                  "revision": "91cad51170dc346986eccefdc2dd33a9da36ead9"},
        "vllm":  {"repo": "Qwen/Qwen2.5-1.5B-Instruct-AWQ",
                  "quant": "", "file": "",
                  "model_id": "Qwen/Qwen2.5-1.5B-Instruct-AWQ",
                  "revision": "3ecffa0ceb27851800f45519bab9c457a04405e1"}}},
    {"key": "mid", "label": "Qwen2.5-7B-Instruct (4-bit)",
     "approx_gb": 4.7, "sources": {
        "llama": {"repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                  "quant": "Q4_K_M",
                  "file": "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
                  "patterns": ["qwen2.5-7b-instruct-q4_k_m-*.gguf"],
                  "model_id": "Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
                  "revision": "bb5d59e06d9551d752d08b292a50eb208b07ab1f"},
        "lms":   {"repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                  "quant": "Q4_K_M",
                  "file": "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
                  "patterns": ["qwen2.5-7b-instruct-q4_k_m-*.gguf"],
                  "model_id": "Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
                  "revision": "bb5d59e06d9551d752d08b292a50eb208b07ab1f"},
        "vllm":  {"repo": "Qwen/Qwen2.5-7B-Instruct-AWQ",
                  "quant": "", "file": "",
                  "model_id": "Qwen/Qwen2.5-7B-Instruct-AWQ",
                  "revision": "b25037543e9394b818fdfca67ab2a00ecc7dd641"}}},
]


def preset_source(model_key: str, provider: str) -> "dict | None":
    for m in REFERENCE_MODELS:
        if m["key"] == model_key:
            return (m["sources"] or {}).get(provider)
    return None


# ~512-token fixed prompt corpus, versioned with PRESET_VERSION.
PROMPT_CORPUS = (
    "You are a meticulous archivist describing the history of computing. "
    "Write a detailed, factual account of the development of operating "
    "systems from the 1960s to the present day. Begin with mainframe batch "
    "processing, where operators queued punched-card decks and a resident "
    "monitor handed the processor from one job to the next without human "
    "intervention. Explain how the economics of expensive hardware and cheap "
    "waiting made better utilization the central design goal of the era. "
    "Describe the arrival of time-sharing, the Compatible Time-Sharing System "
    "at MIT, and the ambitious Multics project that followed it, noting which "
    "of its ideas survived and which proved too costly to build. Cover the "
    "birth of UNIX at Bell Labs, the decision to rewrite it in C, and the way "
    "portable source code let a single system spread across incompatible "
    "machines. Trace the divergence into Berkeley and System V lineages, the "
    "standardization effort that produced POSIX, and the licensing disputes "
    "that shaped which implementations universities and vendors could adopt. "
    "Turn next to the personal computer, the small single-user monitors that "
    "preceded it, and the gradual reintroduction of protected memory, "
    "preemptive scheduling, and virtual memory into desktop systems that had "
    "shipped without them. Discuss the microkernel argument, what its "
    "advocates promised, what the measured costs turned out to be, and how "
    "hybrid designs settled the question in practice. Describe the rise of "
    "free and open source kernels, the collaborative development model that "
    "sustained them, and their eventual dominance in server and embedded "
    "deployments. Explain virtualization, from early mainframe hypervisors "
    "through hardware-assisted extensions, and how it changed capacity "
    "planning. Then treat security: privilege separation, mandatory access "
    "control, sandboxing, and the mitigations added in response to "
    "speculative execution attacks. Throughout, "
    "identify the recurring tension between abstraction and control, and "
    "conclude with contemporary containerized and cloud-native designs."
)

# ── Timing / energy / aggregation math ───────────────────────────────


def rep_metrics(rep: dict) -> dict:
    """Add prefill_tps + gen_tps to one repetition's raw timings;
    gen_tps spans the n-1 decode gaps after the first token."""
    out = dict(rep)
    out["prefill_tps"] = (rep["prompt_tokens"] / rep["ttft_s"]
                          if rep.get("ttft_s") else 0.0)
    out["gen_tps"] = ((rep["gen_tokens"] - 1) / rep["gen_duration_s"]
                      if rep.get("gen_duration_s") and rep["gen_tokens"] > 1
                      else 0.0)
    return out


def run_metrics(reps: "list[dict]") -> dict:
    """Median TTFT / prefill / generation throughput across repetitions."""
    ms = [rep_metrics(r) for r in reps]
    if not ms:
        return {"ttft_s": 0.0, "prefill_tps": 0.0, "gen_tps": 0.0}
    return {k: statistics.median(m[k] for m in ms)
            for k in ("ttft_s", "prefill_tps", "gen_tps")}


def energy_metrics(avg_watts, gen_tps: float, price_kwh: float) -> dict:
    """Tokens/joule + $/Mtok from generation throughput; None without power."""
    if not avg_watts or avg_watts <= 0 or not gen_tps or gen_tps <= 0:
        return {"tokens_per_joule": None, "usd_per_mtok": None,
                "avg_watts": avg_watts}
    return {"tokens_per_joule": gen_tps / avg_watts,
            "usd_per_mtok": (avg_watts / 1000.0 * price_kwh)
                            / (gen_tps * 3600.0) * 1e6,
            "avg_watts": avg_watts}


def aggregate_gpus(gpus: "list[dict]") -> dict:
    """Roll per-GPU entries into one card row: summed VRAM/watts, one label."""
    if not gpus:
        return {"gpu_config": None, "vram_total_mb": None,
                "vram_used_mb": None, "power_w": None}
    names = [g.get("name") or "?" for g in gpus]
    uniq = set(names)
    if len(uniq) == 1:
        label = f"{len(gpus)}× {names[0]}" if len(gpus) > 1 else names[0]
    else:
        label = " + ".join(sorted(uniq))
    powers = [g.get("power_w") for g in gpus if g.get("power_w") is not None]
    return {"gpu_config": label,
            "vram_total_mb": sum(g.get("vram_total_mb") or 0 for g in gpus),
            "vram_used_mb": sum(g.get("vram_used_mb") or 0 for g in gpus),
            "power_w": sum(powers) if powers else None}


# ── Bench driver ─────────────────────────────────────────────────────
# Streams one identical completion per provider OpenAI-compatible endpoint.


def bench_stream(base_url: str, model: str, http_post,
                 now=_time.monotonic) -> dict:
    """Drive one streamed completion; returns raw timings for one repetition."""
    t0 = now()
    ttft = None
    tokens = 0
    usage = None
    last = t0
    payload = {"model": model, "stream": True, "max_tokens": GEN_TOKENS,
               "temperature": 0,
               "stream_options": {"include_usage": True},
               "messages": [{"role": "user", "content": PROMPT_CORPUS}]}
    url = base_url.rstrip("/") + "/chat/completions"
    for line in http_post(url, payload):
        if not line or not line.startswith("data: "):
            continue
        body = line[6:].strip()
        if body == "[DONE]":
            continue
        try:
            chunk = json.loads(body)
        except ValueError:
            continue
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
        # Reasoning models stream their budget as reasoning_content first;
        # those are generated tokens too (and may be all there are).
        if (delta.get("content") or delta.get("reasoning_content")
                or delta.get("reasoning")):
            tokens += 1
            last = now()
            if ttft is None:
                ttft = last - t0
    if not tokens or ttft is None:
        raise RuntimeError("provider streamed no tokens")
    # Server-reported counts win; chunk count and chars//4 are fallbacks.
    usage = usage or {}
    return {"ttft_s": ttft,
            "prompt_tokens": usage.get("prompt_tokens")
                             or len(PROMPT_CORPUS) // 4,
            "gen_tokens": usage.get("completion_tokens") or tokens,
            "gen_duration_s": max(last - t0 - ttft, 0.0)}


def run_bench(base_url: str, model: str, http_post, now=_time.monotonic,
              progress_cb=None) -> dict:
    """One discarded warmup plus REPS measured repetitions, median-reduced."""
    emit = progress_cb or (lambda ev: None)
    emit({"phase": "warmup"})
    bench_stream(base_url, model, http_post, now=now)
    reps = []
    for i in range(REPS):
        emit({"phase": "rep", "n": i + 1, "of": REPS})
        reps.append(bench_stream(base_url, model, http_post, now=now))
    out = run_metrics(reps)
    out["reps"] = reps
    return out


def _openai_stream_post(url: str, payload: dict, headers=None, timeout=300,
                        verify=None, session=None):
    """Production http_post: streamed POST yielding decoded SSE lines;
    reuses `session`'s pooled connection when one is passed."""
    import requests
    kwargs = {"json": payload, "headers": headers or {}, "stream": True,
              "timeout": timeout}
    if verify is not None:
        kwargs["verify"] = verify
    r = (session or requests).post(url, **kwargs)
    if not r.ok:
        # Surface the provider's own message (e.g. LMS load guardrails)
        # instead of a bare status code with an internal URL.
        try:
            msg = ((r.json() or {}).get("error") or {}).get("message")
        except ValueError:
            msg = None
        detail = (msg or r.text or "").strip()[:300]
        raise RuntimeError(
            f"provider rejected the request (HTTP {r.status_code})"
            + (f": {detail}" if detail else ""))
    for raw in r.iter_lines(decode_unicode=True):
        if raw:
            yield raw


# ── Provider endpoints + power sampling ──────────────────────────────

PROVIDERS = ("llama", "vllm", "lms")
LMS_OPENAI_PORT = 1235


def _probe_agent(base: str) -> bool:
    """True when the agent answers /health at this base URL."""
    import requests
    import agent_registry
    url = f"{base}/health"
    try:
        return requests.get(url, timeout=(3, 5),
                            **agent_registry.agent_tls_kwargs(url)).ok
    except requests.exceptions.RequestException:
        return False


def bench_base_url(provider: str, agent: dict, probe=None) -> "tuple[str, dict]":
    """OpenAI-compatible base URL + auth headers for one provider/agent.
    llama/vllm use the agent passthrough; LM Studio is dialed directly."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    if provider == "lms":
        import proxies
        host = proxies._host_from_agent(agent)
        if not host:
            raise ValueError("no reachable host for LM Studio")
        return f"http://{host}:{LMS_OPENAI_PORT}/v1", {}
    import agent_registry
    urls = agent_registry.agent_callback_urls(agent)
    if not urls:
        raise ValueError("no callback URL recorded for agent")
    # Probes candidates in order; benches the first that answers.
    check = probe or _probe_agent
    base = next((u for u in urls if check(u)), None)
    if base is None:
        raise ValueError("agent is not reachable on any callback URL")
    return f"{base}/{provider}/openai", \
           {"Authorization": f"Bearer {agent.get('token') or ''}"}


def _has_telemetry(sample) -> bool:
    """True when a sample yields host watts or a GPU block, in either shape."""
    import energy
    if not isinstance(sample, dict) or not sample:
        return False
    watts, _src = energy.extract_power(sample)
    return watts is not None or bool(energy._sys_block(sample).get("gpu"))


def _agent_sample(agent_id: str, provider: "str | None" = None) -> dict:
    """Latest sample carrying host telemetry, preferring the run's own bucket.
    llama pushes the host fields flat; vllm/lms nest them under `system`."""
    import provider_state
    order = ([provider] if provider else []) + [p for p in PROVIDERS
                                                if p != provider]
    for prov in order:
        sample = (provider_state.STORE.get(prov, agent_id) or {}).get("sample")
        if _has_telemetry(sample):
            return sample
    return {}


def _snapshot_power(agent_id: str, provider: "str | None" = None) -> dict:
    """Normalize the live telemetry sample to {watts, source, gpus[]}.
    Watts follow energy.extract_power's PSU wall > Apple SoC > GPU order."""
    import energy
    sample = _agent_sample(agent_id, provider) or {}
    watts, source = energy.extract_power(sample)
    gpus = []
    gpu = energy._sys_block(sample).get("gpu") or {}
    if gpu:
        total_bytes = gpu.get("vram_total_bytes")
        gpus.append({
            "name": gpu.get("name"),
            "power_w": gpu.get("power_watts"),
            "vram_used_mb": gpu.get("vram_used_mb"),
            "vram_total_mb": (round(total_bytes / 1_048_576)
                              if total_bytes else None)})
    return {"watts": watts, "source": source, "gpus": gpus}


# Preference order when one window sees more than one power source.
_SOURCE_ORDER = ("psu", "mac", "gpu")


class PowerSampler:
    """Samples power during a bench window; PSU wall watts preferred.
    Single-use: start() once, stop() once. stop() is idempotent."""

    def __init__(self, sample_fn, interval_s: float = 2.0):
        self._fn = sample_fn
        self._interval = interval_s
        self._by_source: "dict[str, list[float]]" = {}
        self._last_gpus: list = []
        self._timer = None
        self._stopped = False
        self._result = None
        self._ticks = 0
        self._failures = 0
        self._lock = _threading.Lock()

    def _tick(self):
        try:
            snap = self._fn() or {}
        except Exception:
            with self._lock:
                self._ticks += 1
                self._failures += 1
            log.debug("report card: power sample failed", exc_info=True)
            return
        gpus = snap.get("gpus") or []
        watts, source = snap.get("watts"), snap.get("source")
        with self._lock:
            self._ticks += 1
            if self._stopped:
                return
            if gpus:
                self._last_gpus = gpus
            if source and isinstance(watts, (int, float)):
                self._by_source.setdefault(source, []).append(float(watts))

    def start(self):
        def loop():
            with self._lock:
                if self._stopped:
                    return
            self._tick()
            with self._lock:
                if self._stopped:
                    return
                self._timer = _threading.Timer(self._interval, loop)
                self._timer.daemon = True
                self._timer.start()
        loop()

    def reset(self):
        """Discard samples taken so far (idle/warmup); keep the GPU list."""
        with self._lock:
            self._by_source = {}

    def stop(self) -> dict:
        with self._lock:
            if self._result is not None:
                return self._result
            timer = self._timer
        if timer:
            timer.cancel()
        # Final sample so a window shorter than the interval still measures.
        self._tick()
        with self._lock:
            self._stopped = True
            if self._ticks and self._failures == self._ticks:
                log.warning("report card: every power sample failed (%d ticks)"
                            " — energy fields will be blank", self._ticks)
            self._result = {"avg_watts": None, "source": None,
                            "gpus": self._last_gpus}
            for src in _SOURCE_ORDER:
                vals = self._by_source.get(src)
                if vals:
                    self._result = {"avg_watts": sum(vals) / len(vals),
                                    "source": src, "gpus": self._last_gpus}
                    break
            return self._result


# ── Model readiness ──────────────────────────────────────────────────
# vLLM is benched as served; the manager never loads or restarts it.


def _norm(s: str) -> str:
    return str(s or "").strip().lower().replace("_", "-").replace(".", "-")


def _preset_stems(src: dict) -> "set[str]":
    """Normalized model-name stems LM Studio may list the preset under:
    the GGUF file stem cut at the quant token, and the repo tail minus -GGUF."""
    stems = set()
    quant = _norm(src.get("quant") or "")
    fname = src.get("file") or ""
    if fname and quant:
        fstem = _norm(fname[:-5] if fname.lower().endswith(".gguf") else fname)
        cut = fstem.find("-" + quant)
        if cut > 0:
            stems.add(fstem[:cut])
    repo = src.get("repo") or ""
    tail = _norm(repo.rsplit("/", 1)[-1]) if repo else ""
    if tail.endswith("-gguf"):
        stems.add(tail[:-5])
    return stems


def _model_matches(candidate: str, src: dict) -> bool:
    """True when a provider's model id refers to the preset source.
    Providers register GGUFs as "<repo>:<QUANT>", so match on repo + quant."""
    cand = str(candidate or "").strip()
    if not cand or not src:
        return False
    want_id = src.get("model_id") or ""
    if want_id and cand.lower() == want_id.lower():
        return True
    repo = src.get("repo") or ""
    quant = src.get("quant") or ""
    cand_repo, _, cand_quant = cand.partition(":")
    if repo and _norm(cand_repo) == _norm(repo):
        # A pinned quant must be stated and agree; unlabelled is not a match.
        return not quant or _norm(cand_quant) == _norm(quant)
    # Fall back to the GGUF filename for providers that list files directly.
    fname = src.get("file") or ""
    if fname and _norm(cand).endswith(_norm(fname)):
        return True
    # LM Studio virtual keys: "<stem>" or "<owner>_<model>@<quant>" (#489).
    # A bare key carries no quant; LMS only adds @quant on multi-quant hosts.
    base, _, at_quant = cand.partition("@")
    if quant and at_quant and _norm(at_quant) != _norm(quant):
        return False
    stems = _preset_stems(src)
    owner = _norm(repo.split("/", 1)[0]) if "/" in repo else ""
    for nb in {_norm(base), _norm(base.rsplit("/", 1)[-1])}:
        if nb in stems:
            return True
        if owner and nb.startswith(owner + "-") and nb[len(owner) + 1:] in stems:
            return True
    return False


def ensure_ready(provider: str, agent_id: str, model_key: str,
                 deps: dict) -> dict:
    """Resolve the model to bench; loads it for llama/lms, gates vLLM."""
    src = preset_source(model_key, provider)
    if not src:
        return {"status": "unavailable", "model": None,
                "reference": None, "is_reference": False,
                "error": f"no preset for {model_key}/{provider}"}
    if provider == "vllm":
        current = deps["vllm_current"](agent_id)
        if not current:
            return {"status": "unavailable", "model": None,
                    "reference": src["repo"], "is_reference": False,
                    "error": "vLLM is not serving a model"}
        if _model_matches(current, src):
            return {"status": "ready", "model": current,
                    "reference": src["model_id"], "is_reference": True}
        return {"status": "needs_confirm", "model": current,
                "reference": src["model_id"],
                "is_reference": False, "source": src}
    target = src["model_id"]
    base = {"reference": target, "is_reference": True, "source": src}
    # Registered ids come from the provider; a downloaded-but-unregistered
    # model is absent here and needs the download+register path.
    known = deps["loaded_models"](provider, agent_id) or []
    match = next((m for m in known if _model_matches(m, src)), None)
    if match:
        if deps["load"](provider, agent_id, {**src, "model_id": match}):
            return {"status": "ready", "model": match, **base}
        return {"status": "load_failed", "model": match, **base,
                "error": f"{provider} refused to load {match}"}
    return {"status": "needs_download", "model": target, **base}


def _agent_json(agent: dict, method: str, path: str, timeout=15, **kw):
    """Call an agent endpoint with its machine token; None on any failure."""
    import agent_registry
    r, _tried, err = agent_registry.agent_request(
        method, agent, path,
        headers={"Authorization": f"Bearer {agent.get('token') or ''}"},
        timeout=timeout, **kw)
    if r is None or not r.ok:
        log.warning("report card: %s %s failed on agent %s (%s)", method, path,
                    (agent.get("agent_id") or "?")[:8],
                    err or getattr(r, "status_code", "?"))
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _agent_call(agent: dict, method: str, path: str, timeout=15,
                **kw) -> "tuple[bool, str | None]":
    """Agent call as (ok, error). Agents report expected failures as HTTP 200
    with {"ok": false, "error": ...}, so truthiness is not a success test."""
    res = _agent_json(agent, method, path, timeout=timeout, **kw)
    if res is None:
        return False, f"{path} did not respond"
    if isinstance(res, dict) and res.get("ok") is False:
        err = res.get("error")
        if not err and isinstance(res.get("response"), dict):
            resp = res["response"]
            err = resp.get("error") or resp.get("detail")
        return False, str(err or f"{path} reported failure")
    return True, None


def _model_ids(payload) -> "list[str]":
    """Model ids out of an OpenAI /v1/models-shaped payload."""
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [str(m.get("id")) for m in data
            if isinstance(m, dict) and m.get("id")]


def register_llama_model(agent: dict, src: dict) -> "tuple[bool, str | None]":
    """Add the preset as a config.ini section so llama-server can serve it."""
    cfg = _agent_json(agent, "GET", "/llama/config", timeout=20)
    if not isinstance(cfg, dict):
        return False, "could not read the llama.cpp config"
    sections = {k: v for k, v in cfg.items() if isinstance(v, dict)}
    model_id = src["model_id"]
    if model_id not in sections:
        # hf-repo is derived from the section name; only the file pattern
        # and a modest context need persisting.
        sections[model_id] = {"hf-file": src.get("file") or "", "ctx-size": "4096"}
    return _agent_call(agent, "POST", "/llama/config", timeout=30,
                       json=sections)


def wait_for_model(agent: dict, provider: str, src: dict,
                   timeout_s: float = 180.0, should_cancel=None,
                   now=_time.monotonic, sleep=_time.sleep) -> "str | None":
    """Poll the provider's model list until the preset appears."""
    deadline = now() + timeout_s
    while now() < deadline:
        if should_cancel and should_cancel():
            return None
        ids = _model_ids(_agent_json(agent, "GET", f"/{provider}/models",
                                     timeout=10))
        match = next((m for m in ids if _model_matches(m, src)), None)
        if match:
            return match
        sleep(3.0)
    return None


def provision_model(agent: dict, provider: str, src: dict, emit,
                    should_cancel=None) -> dict:
    """Download the preset, register it, restart llama.cpp, then load it.
    Only runs after the operator confirms; llama.cpp restart is the cost."""
    emit({"phase": "download", "repo": src["repo"], "quant": src.get("quant")})
    # Patterns are exact lowercase filenames, not case-folded globs.
    body = {"repo": src["repo"], "patterns": list(src.get("patterns") or [])}
    if provider == "lms":
        lms_body = {"model": f"https://huggingface.co/{src['repo']}"}
        if src.get("quant"):
            lms_body["quantization"] = src["quant"]
        ok, err = _agent_call(agent, "POST", "/lms/download", timeout=60,
                              json=lms_body)
    else:
        ok, err = _agent_call(agent, "POST", "/llama/download", timeout=60,
                              json=body)
    if not ok:
        return {"status": "error",
                "error": err or "download could not be started"}
    emit({"phase": "downloading"})
    if provider == "llama":
        err = _follow_download(agent, emit, should_cancel=should_cancel)
        if err is _CANCELLED:
            return {"status": "cancelled"}
        if err:
            return {"status": "error", "error": err}
        emit({"phase": "register", "model": src["model_id"]})
        ok, err = register_llama_model(agent, src)
        if not ok:
            return {"status": "error",
                    "error": err or "could not register the model"}
        emit({"phase": "restart"})
        ok, err = _agent_call(agent, "POST", "/llama/server/restart",
                              timeout=120)
        if not ok:
            return {"status": "error",
                    "error": err or "llama.cpp restart failed"}
    emit({"phase": "waiting"})
    wait_s = 1800.0 if provider == "lms" else 180.0
    match = wait_for_model(agent, provider, src, timeout_s=wait_s,
                           should_cancel=should_cancel)
    if not match:
        if should_cancel and should_cancel():
            return {"status": "cancelled"}
        return {"status": "error",
                "error": "model did not appear after provisioning"}
    emit({"phase": "load", "model": match})
    ok, err = _agent_call(agent, "POST", f"/{provider}/load", timeout=180,
                          json={"model": match})
    if not ok:
        return {"status": "error", "error": err or f"failed to load {match}"}
    return {"status": "ready", "model": match}


# Sentinel distinguishing an operator cancel from a download error.
_CANCELLED = object()


def _follow_download(agent: dict, emit, should_cancel=None,
                     stream_lines=None) -> "str | None":
    """Relay the agent's download SSE until done; returns an error or None."""
    lines = stream_lines or _agent_download_lines
    try:
        for msg in lines(agent):
            if should_cancel and should_cancel():
                _agent_json(agent, "POST", "/llama/download/cancel", timeout=15)
                return _CANCELLED
            kind = msg.get("type")
            if kind == "line" and msg.get("text"):
                emit({"phase": "download_progress", "text": msg["text"][:160]})
            elif kind == "done":
                if msg.get("error") or msg.get("ok") is False:
                    return str(msg.get("error") or "download failed")[:200]
                return None
    except Exception as e:
        return f"download stream failed: {str(e)[:120]}"
    return "download stream ended without completing"


def _agent_download_lines(agent: dict):
    """Yield decoded JSON messages from the agent's download SSE."""
    import agent_registry
    import requests
    token = agent.get("token") or ""
    for base in agent_registry.agent_callback_urls(agent):
        url = f"{base}/llama/download/stream"
        try:
            r = requests.get(url, stream=True, timeout=(5, 120),
                             headers={"Authorization": f"Bearer {token}"},
                             **agent_registry.agent_tls_kwargs(url))
            r.raise_for_status()
        except requests.exceptions.RequestException:
            continue
        for raw in r.iter_lines(decode_unicode=True):
            if raw and raw.startswith("data: "):
                try:
                    yield json.loads(raw[6:])
                except ValueError:
                    continue
        return
    raise RuntimeError("no reachable agent stream")


def prod_deps(agent: dict) -> dict:
    """Readiness callables bound to one agent's live endpoints."""
    def loaded_models(provider: str, _agent_id: str) -> "list[str]":
        return _model_ids(_agent_json(agent, "GET", f"/{provider}/models"))

    def load(provider: str, _agent_id: str, src: dict) -> bool:
        ok, _err = _agent_call(agent, "POST", f"/{provider}/load", timeout=180,
                               json={"model": src["model_id"]})
        return ok

    def vllm_current(_agent_id: str) -> "str | None":
        ids = _model_ids(_agent_json(agent, "GET", "/vllm/models"))
        return ids[0] if ids else None

    return {"loaded_models": loaded_models, "load": load,
            "vllm_current": vllm_current}


# ── Storage ──────────────────────────────────────────────────────────
# One row per completed run; no pruning.

_COLS = "ts, agent_id, provider, mode, preset_version, eligible, result"


def init_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            agent_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            mode TEXT NOT NULL,
            preset_version TEXT NOT NULL,
            eligible INTEGER NOT NULL,
            result TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report_cards_lookup "
                 "ON report_cards(agent_id, provider, ts)")
    conn.commit()


def insert_card(conn, card: dict) -> int:
    cur = conn.execute(
        "INSERT INTO report_cards (ts, agent_id, provider, mode, preset_version,"
        " eligible, result) VALUES (?,?,?,?,?,?,?)",
        (int(card["ts"]), card["agent_id"], card["provider"], card["mode"],
         card["preset_version"], 1 if card["eligible"] else 0,
         json.dumps(card["result"])))
    conn.commit()
    return cur.lastrowid


def _row_to_card(row) -> dict:
    cols = [c.strip() for c in _COLS.split(",")]
    d = dict(zip(cols, row))
    d["eligible"] = bool(d["eligible"])
    d["result"] = json.loads(d["result"])
    return d


def latest_card(conn, agent_id: str, provider: str) -> "dict | None":
    row = conn.execute(
        f"SELECT {_COLS} FROM report_cards WHERE agent_id=? AND provider=?"
        " ORDER BY ts DESC, id DESC LIMIT 1", (agent_id, provider)).fetchone()
    return _row_to_card(row) if row else None


def history(conn, agent_id: str, provider: str, model: str) -> "list[dict]":
    rows = conn.execute(
        f"SELECT {_COLS} FROM report_cards WHERE agent_id=? AND provider=?"
        " ORDER BY ts ASC, id ASC", (agent_id, provider)).fetchall()
    return [c for c in map(_row_to_card, rows)
            if c["result"].get("model") == model]


# ── Routes ───────────────────────────────────────────────────────────
# Auth is the manager's global before_request gate; no per-route decorator.

MODES = ("standard", "custom")
_JOBS: "dict[str, dict]" = {}
_JOBS_LOCK = _threading.Lock()
_JOB_RETENTION = 32
_STREAM_MAX_S = 1800.0
_STREAM_TICK_S = 1.0

_conn_factory = None
_price_kwh_fn = None


def _price_kwh() -> float:
    if _price_kwh_fn:
        return _price_kwh_fn()
    return 0.15


def _agent_for(agent_id: str) -> "dict | None":
    import agent_registry
    return agent_registry.resolve_agent_by_id(agent_id)


_PUBLIC_CARD_FIELDS = ("ts", "provider", "mode", "preset_version",
                       "eligible", "result")


def _public_card(card: dict) -> dict:
    """Allowlisted card fields for the UI and leaderboard submission."""
    return {k: card[k] for k in _PUBLIC_CARD_FIELDS if k in card}


def _new_job() -> str:
    job_id = _uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"queue": _queue.Queue(), "done": False,
                         "cancel": _threading.Event()}
        if len(_JOBS) > _JOB_RETENTION:
            for stale in [k for k, v in list(_JOBS.items()) if v["done"]][:-8]:
                _JOBS.pop(stale, None)
    return job_id


class _Cancelled(Exception):
    """Raised in the worker when the operator cancels a run."""


def _run_job(job_id: str, req: dict) -> None:
    job = _JOBS[job_id]
    q, cancel = job["queue"], job["cancel"]
    started = _time.monotonic()

    def emit(ev):
        q.put({"event": "progress", "elapsed_s": round(_time.monotonic() - started, 1),
               **ev})

    def check():
        if cancel.is_set():
            raise _Cancelled()

    try:
        agent = _agent_for(req["agent"])
        if not agent:
            raise RuntimeError("agent not found or not approved")
        provider, mode = req["provider"], req["mode"]
        check()
        provisioned = False
        if mode == "custom":
            model, is_reference = req["model"], False
            emit({"phase": "ready", "status": "custom", "model": model})
        else:
            emit({"phase": "resolving"})
            ready = ensure_ready(provider, req["agent"], req["model_key"],
                                 prod_deps(agent))
            emit({"phase": "ready", "status": ready["status"],
                  "model": ready.get("model")})
            check()
            if ready["status"] == "unavailable":
                raise RuntimeError(ready.get("error") or "provider unavailable")
            if ready["status"] == "needs_download":
                if not req.get("confirm_download"):
                    raise RuntimeError("reference model is not installed")
                prov = provision_model(agent, provider, ready["source"], emit,
                                       should_cancel=cancel.is_set)
                if prov["status"] == "cancelled":
                    raise _Cancelled()
                if prov["status"] != "ready":
                    raise RuntimeError(prov.get("error") or "provisioning failed")
                ready = {**ready, "status": "ready", "model": prov["model"]}
                provisioned = True
            elif ready["status"] == "needs_confirm" and not req.get("confirm_vllm"):
                raise RuntimeError(ready.get("error")
                                   or "vLLM run needs confirmation")
            elif ready["status"] not in ("ready", "needs_confirm"):
                raise RuntimeError(ready.get("error")
                                   or f"model not ready: {ready['status']}")
            model = ready["model"]
            is_reference = bool(ready.get("is_reference"))
        check()
        base, headers = bench_base_url(provider, agent)
        sampler = PowerSampler(lambda: _snapshot_power(req["agent"], provider))
        sampler.start()

        def _progress(ev):
            # Warmup is discarded from timings; discard its power samples too.
            if ev.get("phase") == "rep" and ev.get("n") == 1:
                sampler.reset()
            check()
            emit(ev)

        import requests as _requests
        sess = _requests.Session()
        try:
            bench = run_bench(
                base, model,
                lambda u, p: _openai_stream_post(u, p, headers, session=sess,
                                                 **_tls_kwargs(u)),
                progress_cb=_progress)
        finally:
            sess.close()
            power = sampler.stop()
            # The bench loaded the model; free the host's RAM/VRAM again.
            if mode == "standard" and provider in ("llama", "lms"):
                emit({"phase": "unload", "model": model})
                ok_u, err_u = _agent_call(agent, "POST", f"/{provider}/unload",
                                          timeout=60, json={"model": model})
                if not ok_u:
                    log.warning("report card: unload of %s failed: %s",
                                model, err_u)
        agg = aggregate_gpus(power["gpus"])
        energy = energy_metrics(power["avg_watts"], bench["gen_tps"],
                                req["price_kwh"])
        result = {k: v for k, v in bench.items() if k != "reps"}
        result.update({"model": model, **energy, **agg,
                       "power_source": power["source"]})
        card = {"ts": int(_time.time()), "agent_id": req["agent"],
                "provider": provider, "mode": mode,
                "preset_version": PRESET_VERSION,
                "eligible": mode == "standard" and is_reference,
                "result": result}
        insert_card(_conn_factory(), card)
        q.put({"event": "done", "card": _public_card(card),
               "cleanup": {"downloaded": provisioned,
                           "deletable": provisioned
                           and provider in ("llama", "lms"),
                           "model_key": req.get("model_key")}})
    except _Cancelled:
        log.info("report card run cancelled")
        q.put({"event": "cancelled"})
    except Exception as e:
        log.warning("report card run failed: %s", e)
        q.put({"event": "error", "error": str(e)[:200]})
    finally:
        job["done"] = True


def _tls_kwargs(url: str) -> dict:
    import agent_registry
    return agent_registry.agent_tls_kwargs(url)


def register_routes(app, ctx=None, db_path: "str | None" = None) -> None:
    """Mount /api/reportcard/* on the manager app."""
    global _conn_factory, _price_kwh_fn
    import sqlite3
    from flask import jsonify, request as flask_request, stream_with_context

    path = db_path or str(Path(getattr(ctx, "data_dir", ".")) / "metrics.db")
    tls = _threading.local()

    # Per-thread connection, mirroring the manager's get_db(); bench workers
    # each get their own rather than opening one per call.
    def conn_factory():
        conn = getattr(tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(path, timeout=30.0)
            conn.execute("PRAGMA busy_timeout=5000")
            tls.conn = conn
        return conn

    if _conn_factory is None:
        _conn_factory = conn_factory

    def price_from_ctx() -> float:
        # unified_config.py is deployment-local; missing block falls back.
        manager = getattr(getattr(ctx, "settings", None), "manager", None)
        cfg = getattr(manager, "reportcard", None)
        try:
            return float(getattr(cfg, "price_kwh", 0.15) or 0.15)
        except (TypeError, ValueError):
            return 0.15

    if ctx is not None:
        _price_kwh_fn = price_from_ctx

    @app.route("/api/reportcard/preset")
    def reportcard_preset():
        return jsonify({"preset_version": PRESET_VERSION,
                        "gen_tokens": GEN_TOKENS, "reps": REPS,
                        "providers": list(PROVIDERS),
                        "price_kwh": _price_kwh(),
                        "models": [{"key": m["key"], "label": m["label"]}
                                   for m in REFERENCE_MODELS]})

    @app.route("/api/reportcard/run", methods=["POST"])
    def reportcard_run():
        body = flask_request.get_json(silent=True) or {}
        agent_id = (body.get("agent") or "").strip()
        provider = (body.get("provider") or "").strip()
        mode = (body.get("mode") or "standard").strip()
        if not agent_id:
            return jsonify({"ok": False, "error": "agent required"}), 400
        if provider not in PROVIDERS:
            return jsonify({"ok": False,
                            "error": f"unknown provider: {provider}"}), 400
        if mode not in MODES:
            return jsonify({"ok": False, "error": f"unknown mode: {mode}"}), 400
        model = (body.get("model") or "").strip()
        if mode == "custom" and not model:
            return jsonify({"ok": False,
                            "error": "model required in custom mode"}), 400
        model_key = (body.get("model_key") or "small").strip()
        if mode == "standard" and not preset_source(model_key, provider):
            return jsonify({"ok": False,
                            "error": f"unknown model_key: {model_key}"}), 400
        # Falls back to config only when price_kwh is absent, not when 0.
        raw_price = body.get("price_kwh")
        try:
            price = _price_kwh() if raw_price is None else float(raw_price)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid price_kwh"}), 400
        # needs_confirm requires confirm_vllm; such runs are never eligible.
        needs_precheck = (mode == "standard"
                          and not body.get("confirm_vllm")
                          and not body.get("confirm_download"))
        if needs_precheck:
            agent = _agent_for(agent_id)
            if not agent:
                return jsonify({"ok": False, "error": "agent not found"}), 404
            ready = ensure_ready(provider, agent_id, model_key, prod_deps(agent))
            if ready["status"] == "needs_confirm":
                return jsonify({"ok": True, "status": "needs_confirm",
                                "model": ready["model"],
                                "reference": ready["reference"]})
            if ready["status"] == "needs_download":
                src = ready.get("source") or {}
                entry = next((m for m in REFERENCE_MODELS
                              if m["key"] == model_key), {})
                return jsonify({"ok": True, "status": "needs_download",
                                "model": ready["model"],
                                "repo": src.get("repo"),
                                "quant": src.get("quant"),
                                "approx_gb": entry.get("approx_gb"),
                                "restarts": provider == "llama"})
            if ready["status"] == "unavailable":
                return jsonify({"ok": False,
                                "error": ready.get("error") or "provider unavailable"}), 409
        job_id = _new_job()
        req = {"agent": agent_id, "provider": provider, "mode": mode,
               "model": model, "model_key": model_key, "price_kwh": price,
               "confirm_vllm": bool(body.get("confirm_vllm")),
               "confirm_download": bool(body.get("confirm_download"))}
        _threading.Thread(target=_run_job, args=(job_id, req),
                          name=f"reportcard-{job_id[:8]}", daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id})

    @app.route("/api/reportcard/models")
    def reportcard_models():
        agent_id = flask_request.args.get("agent") or ""
        provider = flask_request.args.get("provider") or ""
        if not agent_id or provider not in PROVIDERS:
            return jsonify({"ok": False,
                            "error": "agent and provider required"}), 400
        agent = _agent_for(agent_id)
        if not agent:
            return jsonify({"ok": False, "error": "agent not found"}), 404
        ids = _model_ids(_agent_json(agent, "GET", f"/{provider}/models"))
        return jsonify({"ok": True, "models": ids})

    @app.route("/api/reportcard/delete-model", methods=["POST"])
    def reportcard_delete_model():
        body = flask_request.get_json(silent=True) or {}
        agent_id = (body.get("agent") or "").strip()
        provider = (body.get("provider") or "").strip()
        model_key = (body.get("model_key") or "").strip()
        if provider not in ("llama", "lms"):
            return jsonify({"ok": False,
                            "error": "deletion is only supported for llama.cpp"
                                     " and LM Studio"}), 400
        src = preset_source(model_key, provider)
        if not agent_id or not src:
            return jsonify({"ok": False,
                            "error": "agent and a known model_key required"}), 400
        agent = _agent_for(agent_id)
        if not agent:
            return jsonify({"ok": False, "error": "agent not found"}), 404
        if provider == "lms":
            # LMS lists downloads under its own virtual key; resolve it live.
            ids = _model_ids(_agent_json(agent, "GET", "/lms/models"))
            match = next((m for m in ids if _model_matches(m, src)), None)
            if not match:
                return jsonify({"ok": False,
                                "error": "model not found on the host"}), 404
            res = _agent_json(agent, "POST", "/lms/delete", timeout=60,
                              json={"model": match})
        else:
            res = _agent_json(agent, "DELETE",
                              f"/llama/config/{src['model_id']}?delete_cache=true",
                              timeout=60)
        if not isinstance(res, dict) or res.get("ok") is not True:
            err = res.get("error") if isinstance(res, dict) else None
            return jsonify({"ok": False,
                            "error": err or "agent did not respond"}), 502
        return jsonify({"ok": True,
                        "deleted_files": len(res.get("deleted_files") or []),
                        "cache_error": res.get("cache_error")})

    @app.route("/api/reportcard/cancel/<job_id>", methods=["POST"])
    def reportcard_cancel(job_id):
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        job["cancel"].set()
        return jsonify({"ok": True, "cancelling": True})

    @app.route("/api/reportcard/stream/<job_id>")
    def reportcard_stream(job_id):
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "unknown job"}), 404

        def generate():
            started = _time.monotonic()
            while True:
                if _time.monotonic() - started > _STREAM_MAX_S:
                    yield "data: " + json.dumps(
                        {"event": "error", "error": "run timed out"}) + "\n\n"
                    return
                try:
                    ev = job["queue"].get(timeout=_STREAM_TICK_S)
                except _queue.Empty:
                    if job["done"] and job["queue"].empty():
                        return
                    yield ": keepalive\n\n"
                    continue
                yield "data: " + json.dumps(ev) + "\n\n"
                if ev.get("event") in ("done", "error", "cancelled"):
                    return

        resp = app.response_class(stream_with_context(generate()),
                                  mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.route("/api/reportcard/latest")
    def reportcard_latest():
        agent_id = flask_request.args.get("agent") or ""
        provider = flask_request.args.get("provider") or ""
        if not agent_id or provider not in PROVIDERS:
            return jsonify({"ok": False,
                            "error": "agent and provider required"}), 400
        card = latest_card(_conn_factory(), agent_id, provider)
        return jsonify({"ok": True,
                        "card": _public_card(card) if card else None})

    @app.route("/api/reportcard/history")
    def reportcard_history():
        agent_id = flask_request.args.get("agent") or ""
        provider = flask_request.args.get("provider") or ""
        model = flask_request.args.get("model") or ""
        if not agent_id or provider not in PROVIDERS or not model:
            return jsonify({"ok": False,
                            "error": "agent, provider and model required"}), 400
        cards = history(_conn_factory(), agent_id, provider, model)
        return jsonify({"ok": True, "cards": [_public_card(c) for c in cards]})
