"""Live benchmark (#879): speed-bench against the running llama-server.
Pure helpers + the per-level loop; llama.py owns the routes and globals."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

BENCHES = ("qualitative", "throughput_1k", "throughput_2k", "throughput_8k",
           "throughput_16k", "throughput_32k")
SCRIPT_COMMIT = "67672dc5b76f8bc17785a19d3dc6d1463fc2902c"
SCRIPT_URL = ("https://raw.githubusercontent.com/ggml-org/llama.cpp/"
              f"{SCRIPT_COMMIT}/tools/server/bench/speed-bench/speed_bench.py")
SCRIPT_SHA256 = "9eacf67452c2b9a829da61c8b077bc5ece9219482e1e2734ad4a9340bdb03911"
REQUIREMENTS = ("datasets", "requests", "tqdm")
_CAT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
_PROG_RE = re.compile(r"%\|.*?\|\s*(\d+)/(\d+)\s*\[")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,199}$")
_ELAPSED_RE = re.compile(r"^Summary \(elapsed=(\d+(?:\.\d+)?)s\)")


def bench_dir(install_dir: str) -> Path:
    return Path(install_dir) / "data" / "bench"


def runtime_python(install_dir: str, override: str) -> tuple[Optional[str], str]:
    """(python, source): the config override, else the bench venv, else missing."""
    if (override or "").strip():
        return override.strip(), "override"
    py = bench_dir(install_dir) / "venv" / "bin" / "python"
    if py.is_file():
        return str(py), "venv"
    return None, "missing"


def _sha_ok(p: Path) -> bool:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest() == SCRIPT_SHA256
    except OSError:
        return False


def script_path(install_dir: str, agent_root: Path) -> tuple[Optional[Path], str]:
    """The pinned speed_bench.py: vendored beside the sources, else the bench dir copy."""
    for cand in (Path(agent_root) / "bench" / "speed_bench.py",
                 bench_dir(install_dir) / "speed_bench.py"):
        if cand.is_file():
            if _sha_ok(cand):
                return cand, "ok"
            return None, f"{cand.name} does not match the pinned upstream hash"
    return None, "speed_bench.py not installed"


def _int(v: Any, name: str) -> int:
    """Rejects bool (a subtype of int) and anything int() can't parse."""
    if isinstance(v, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")


def validate_run_request(body: dict) -> dict:
    model_id = str(body.get("model_id") or "").strip()
    if not model_id or not _MODEL_RE.match(model_id):
        raise ValueError("model_id required")
    bench = str(body.get("bench") or "qualitative").strip()
    if bench not in BENCHES:
        raise ValueError("unknown bench")
    cats = body.get("categories", "all")
    if cats != "all":
        if not isinstance(cats, list) or not cats or len(cats) > 32:
            raise ValueError("categories must be 'all' or a non-empty list")
        cats = [str(c).strip() for c in cats]
        if any(not _CAT_RE.match(c) for c in cats):
            raise ValueError("invalid category name")
    osl = _int(body.get("osl", 1024), "osl")
    if not 16 <= osl <= 8192:
        raise ValueError("osl out of range")
    limit = _int(body.get("limit", 8), "limit")
    if not 1 <= limit <= 64:
        raise ValueError("limit out of range")
    conc = body.get("concurrency", [1])
    if not isinstance(conc, list) or not conc or len(conc) > 8:
        raise ValueError("concurrency must be 1-8 levels")
    conc = [_int(c, "concurrency") for c in conc]
    if any(not 1 <= c <= 64 for c in conc):
        raise ValueError("concurrency level out of range")
    timeout_s = _int(body.get("timeout_s", 600), "timeout_s")
    if not 30 <= timeout_s <= 3600:
        raise ValueError("timeout_s out of range")
    extra = body.get("extra_inputs", {"temperature": 0})
    if not isinstance(extra, dict):
        raise ValueError("extra_inputs must be an object")
    if len(json.dumps(extra)) > 2048:
        raise ValueError("extra_inputs too large")
    base = body.get("baseline_run_id")
    base = str(base).strip()[:64] if base else None
    return {"model_id": model_id, "bench": bench, "categories": cats, "osl": osl,
            "limit": limit, "concurrency": conc, "timeout_s": timeout_s,
            "extra_inputs": extra, "baseline_run_id": base}


def build_cmd(python: str, script: str, server_url: str, req: dict, level: int, out_path: str) -> list[str]:
    cats = "all" if req["categories"] == "all" else ",".join(req["categories"])
    return [python, script, "--url", server_url, "--model", req["model_id"],
            "--bench", req["bench"], "--category", cats,
            "--osl", str(req["osl"]), "--limit", str(req["limit"]),
            "--concurrency", str(level), "--timeout", str(req["timeout_s"]),
            "--extra-inputs", json.dumps(req["extra_inputs"]), "--output", out_path]


def parse_progress(frame: str) -> Optional[tuple[int, int]]:
    m = _PROG_RE.search(frame or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _int0(v: Any, default: int) -> int:
    """int(v), falling back to default when v is missing or non-numeric."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def level_summary(payload: dict, wall_s: float) -> dict:
    rows = [r for r in (payload.get("summary") or []) if isinstance(r, dict)]
    overall = next((r for r in rows if r.get("category") == "overall"), {})
    rows = [r for r in rows if r.get("category") != "overall"]
    results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
    ok = [r for r in results if r.get("ok")]
    tokens = sum(int(r.get("completion_tokens") or 0) for r in ok)
    agg = (tokens / wall_s) if wall_s > 0 and tokens > 0 else None
    return {"rows": rows, "all": {
        "requests": _int0(overall.get("requests") or len(ok), len(ok)),
        "failed": _int0(overall.get("failed") or (len(results) - len(ok)), len(results) - len(ok)),
        "prompt_tps": _num(overall.get("avg_prompt_t_s")),
        "pred_tps": _num(overall.get("avg_pred_t_s")),
        "latency_s": _num(overall.get("avg_latency")),
        "accept_rate": _num(overall.get("accept_rate")),
        "agg_pred_tps": agg, "completion_tokens": tokens}}


class PowerIntegrator:
    """Samples watts on a thread and integrates to Wh; None when nothing reads."""

    def __init__(self, watts_fn: Callable[[], tuple[Optional[float], Optional[str]]], interval_s: float = 2.0):
        self._fn = watts_fn
        self._dt = interval_s
        self._stop = threading.Event()
        self._wh = 0.0
        self._n = 0
        self._src: Optional[str] = None
        self._t: Optional[threading.Thread] = None

    def _loop(self) -> None:
        last_t = time.monotonic()
        last_w: Optional[float] = None
        while not self._stop.wait(self._dt):
            now = time.monotonic()
            try:
                w, src = self._fn()
            except Exception:
                w, src = None, None
            if w is not None:
                if last_w is not None:
                    self._wh += (last_w + w) / 2.0 * (now - last_t) / 3600.0
                self._n += 1
                self._src = src or self._src
                last_w = w
                last_t = now

    def start(self) -> None:
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self) -> tuple[Optional[float], Optional[str]]:
        self._stop.set()
        if self._t:
            self._t.join(timeout=5)
        return (self._wh, self._src) if self._n >= 2 else (None, None)


def marker_path(install_dir: str) -> Path:
    return bench_dir(install_dir) / "datasets.json"


def read_marker(install_dir: str) -> dict:
    try:
        data = json.loads(marker_path(install_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_marker(install_dir: str, bench: str, categories: list[str]) -> None:
    """Merges categories into the bench's known set; never shrinks it."""
    data = read_marker(install_dir)
    known = (data.get(bench) or {}).get("categories") or []
    data[bench] = {"categories": sorted(set(known) | set(categories)), "ts": int(time.time())}
    p = marker_path(install_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)


def run_level_subprocess(cmd: list[str], env: dict, put: Callable[[dict], None], model_id: str,
                         level: int, cancel: "threading.Event", track: Callable[[subprocess.Popen], None],
                         untrack: Callable[[], None]) -> tuple[int, bool, Optional[float]]:
    """Runs one level; stdout → line events, stderr tqdm frames → throttled progress.
    Third value is the script's own elapsed seconds (excludes dataset load), else None."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, bufsize=1,
                            close_fds=True, env=env, start_new_session=True)
    track(proc)
    last_prog = [0.0]

    def _drain_stderr() -> None:
        buf = ""
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                break
            if ch in ("\r", "\n"):
                frame, buf = buf, ""
                pr = parse_progress(frame)
                if pr:
                    now = time.monotonic()
                    if now - last_prog[0] >= 1.0 or pr[0] == pr[1]:
                        last_prog[0] = now
                        put({"type": "progress", "model_id": model_id, "level": level,
                             "done": pr[0], "total": pr[1]})
                elif frame.strip():
                    put({"type": "line", "model_id": model_id, "text": frame.strip()})
            else:
                buf += ch
        if buf.strip():
            put({"type": "line", "model_id": model_id, "text": buf.strip()})

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()
    elapsed: Optional[float] = None
    for raw in iter(proc.stdout.readline, ""):
        if cancel.is_set():
            break
        line = raw.rstrip("\n")
        if line.strip():
            m = _ELAPSED_RE.match(line.strip())
            if m:
                elapsed = float(m.group(1))
            put({"type": "line", "model_id": model_id, "text": line})
    proc.wait()
    t.join(timeout=5)
    untrack()
    return proc.returncode, cancel.is_set(), elapsed
