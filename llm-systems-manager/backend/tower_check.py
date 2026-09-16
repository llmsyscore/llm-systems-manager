"""Tower model capability check (#1039): one canned tool probe per model, graded native / fenced / failed."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Optional

import tower
import tower_tools

log = logging.getLogger("llm-systems-manager.tower")

SMALL_B = 7.0
PROBE_TEXT = "Which hosts are online? Call hosts_overview."
PROBE_TOOL = "hosts_overview"
PROBE_MAX_TOKENS = 512
_SIZE = re.compile(r"(?<![^-_ .xX/])(\d{1,3}(?:\.\d)?)\s*[bB](?![A-Za-z0-9])")


def size_b(model_id: str) -> Optional[float]:
    """Parameter count in billions from the model id's largest <n>b token, else None."""
    hits = [float(m.group(1)) for m in _SIZE.finditer(str(model_id or ""))]
    return max(hits) if hits else None


def probe(complete_stream: Callable, cfg: Any, tools: list, model: dict, native: bool, timeout: int = 0) -> "tuple[bool, str]":
    """One completion of the Tower prompt with the canned question; True when it calls hosts_overview."""
    body = {"model": model["model"], "temperature": 0, "max_tokens": PROBE_MAX_TOKENS, "stream": True,
            "messages": [{"role": "system", "content": tower.system_prompt(cfg, tools, None, native)},
                         {"role": "user", "content": PROBE_TEXT}]}
    if native and tools:
        body["tools"] = [tower_tools.openai_schema(t) for t in tools]

    def cs(b, *, label):
        return complete_stream(b, label="tower-check", read_timeout=timeout) if timeout else complete_stream(b, label="tower-check")

    try:
        msg, _shown = tower._stream_reply(cs, body, lambda ev: None, lambda: False)
    except Exception as e:  # noqa: BLE001
        return False, f"error: {type(e).__name__}"
    call = tower.parse_tool_call(msg)
    if call is None:
        return False, "no call"
    return (call[0] == PROBE_TOOL), f"called {call[0]}"


class Checks:
    """Cached capability grades per (model id, tool mode); ensure() runs a missing check in the background."""

    def __init__(self, *, complete_stream: Callable, cfg: Callable[[], Any], registry_factory: Callable[[], dict],
                 server_args_of: Optional[Callable[[dict], Any]] = None, now: Callable[[], float] = time.time):
        self._cs, self._cfg, self._registry_factory = complete_stream, cfg, registry_factory
        self._server_args_of, self._now = server_args_of, now
        self._lock = threading.Lock()
        self._results: dict = {}
        self._running: set = set()

    def _key(self, model_id: str) -> tuple:
        return (str(model_id), str(getattr(self._cfg(), "tool_mode", "auto") or "auto"))

    def get(self, model_id: str) -> Optional[dict]:
        with self._lock:
            return self._results.get(self._key(model_id))

    def forget(self) -> None:
        with self._lock:
            self._results.clear()

    def ensure(self, model: dict) -> dict:
        """The cached result, or the pending stub after starting one background run for this model."""
        key = self._key(model["model"])
        with self._lock:
            hit = self._results.get(key)
            if hit is not None:
                return hit
            if key not in self._running:
                self._running.add(key)
                threading.Thread(target=self._run_bg, args=(model, key), name="tower-check", daemon=True).start()
        return {"model": model["model"], "grade": "pending"}

    def _run_bg(self, model: dict, key: tuple) -> None:
        try:
            self._run(model, key)
        except Exception as e:  # noqa: BLE001
            log.warning("tower check failed: %s: %s", type(e).__name__, e)
        finally:
            with self._lock:
                self._running.discard(key)

    def run(self, model: dict) -> dict:
        """Runs the probe now (native first when supported, then fenced) and caches the grade."""
        return self._run(model, self._key(model["model"]))

    def _run(self, model: dict, key: tuple) -> dict:
        """The probe body, cached under the caller's key."""
        cfg = self._cfg()
        tools = tower_tools.catalog(self._registry_factory(), cfg, "operator")
        timeout = int(getattr(cfg, "request_timeout_s", 0) or 0)
        args = self._server_args_of(model) if self._server_args_of else None
        grade, mode, detail = "failed", "fenced", ""
        modes = (["native"] if tower.native_supported(cfg, model.get("provider") or "llama", args) else []) + ["fenced"]
        if str(getattr(cfg, "tool_mode", "auto") or "auto") == "native":
            modes = ["native"]
        for m in modes:
            ok, detail = probe(self._cs, cfg, tools, model, native=(m == "native"), timeout=timeout)
            mode = m
            if ok:
                grade = m
                break
        size = size_b(model["model"])
        result = {"model": model["model"], "grade": grade, "mode": mode, "size_b": size,
                  "small": bool(size is not None and size < SMALL_B), "at": float(self._now()), "detail": detail}
        with self._lock:
            self._results[key] = result
        log.debug("tower check model=%s grade=%s mode=%s size_b=%s", model["model"], grade, mode, size)
        return result
