"""LM Studio's native API over the agent (#910): per-model capabilities, the reasoning setting a model
accepts, and an OpenAI-shaped view of a native chat stream."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, Iterator, Optional

import agent_registry

log = logging.getLogger("llm-systems-manager.gateway")

MODELS_PATH = "/lms/native/models"
CACHE_TTL_S = 300.0
NATIVE_LEVELS = ("off", "low", "medium", "high", "on")

_lock = threading.Lock()
_cache: "dict[str, tuple[float, Optional[list]]]" = {}   # agent_id -> (fetched at, models or None)
_candidates: Optional[Callable[[str], list]] = None      # set by wire(); the lms agents serving a model


def wire(candidates: Callable[[str], list]) -> None:
    global _candidates
    _candidates = candidates


def forget() -> None:
    with _lock:
        _cache.clear()


def _fetch(agent: dict) -> Optional[list]:
    r, _tried, _err = agent_registry.agent_request(
        "GET", agent, MODELS_PATH, headers={"Authorization": f"Bearer {agent.get('token') or ''}"}, timeout=(4, 8))
    if r is None or r.status_code != 200:
        return None
    try:
        rows = r.json().get("models")
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def models(agent: dict, now: Callable[[], float] = time.time) -> Optional[list]:
    """The agent's native model list, cached for CACHE_TTL_S; None when the agent or LM Studio has no native API."""
    aid = str(agent.get("agent_id") or "")
    with _lock:
        hit = _cache.get(aid)
        if hit is not None and now() - hit[0] < CACHE_TTL_S:
            return hit[1]
    rows = _fetch(agent)
    with _lock:
        _cache[aid] = (now(), rows)
    return rows


def match(rows: list, model_id: str) -> Optional[dict]:
    """The row whose key is the id, else the sole row whose key is the id plus a quant suffix."""
    exact = [m for m in rows if isinstance(m, dict) and m.get("key") == model_id]
    if exact:
        return exact[0]
    bare = [m for m in rows if isinstance(m, dict) and str(m.get("key") or "").split("@")[0] == model_id]
    return bare[0] if len(bare) == 1 else None


def model_entry(model_id: str) -> Optional[dict]:
    """The native entry for a model across the agents that serve it; None when no agent offers the native API."""
    if not model_id or _candidates is None:
        return None
    try:
        agents = _candidates(model_id)
    except Exception as e:  # noqa: BLE001 — a resolver failure just means no native entry
        log.debug("lms native candidates failed: %s", type(e).__name__)
        return None
    for agent in agents:
        rows = models(agent)
        if rows:
            hit = match(rows, model_id)
            if hit is not None:
                return hit
    return None


def available(model_id: str) -> bool:
    return model_entry(model_id) is not None


def thinking_options(model_id: str) -> Optional[list]:
    """The reasoning settings LM Studio accepts for the model, e.g. ["off", "on"]; None when unknown."""
    entry = model_entry(model_id)
    reasoning = ((entry or {}).get("capabilities") or {}).get("reasoning") or {}
    opts = reasoning.get("allowed_options") if isinstance(reasoning, dict) else None
    if not isinstance(opts, list) or not opts:
        return None
    return [str(o) for o in opts if str(o) in NATIVE_LEVELS] or None


def trim_effort(model_id: str, body: dict) -> dict:
    """Drops reasoning_effort from an OpenAI-path body when the LM Studio model only knows on/off and thinks by
    default: the server would fall back to "on" anyway and log a warning per call."""
    effort = body.get("reasoning_effort")
    if effort in (None, "none"):
        return body
    entry = model_entry(model_id)
    reasoning = ((entry or {}).get("capabilities") or {}).get("reasoning") or {}
    opts = reasoning.get("allowed_options") if isinstance(reasoning, dict) else None
    if isinstance(opts, list) and opts and effort not in opts and reasoning.get("default") == "on":
        return {k: v for k, v in body.items() if k != "reasoning_effort"}
    return body


def reasoning_setting(level: str, options: Optional[list]) -> Optional[str]:
    """The native `reasoning` value for a Tower thinking level: the level itself when the model takes it,
    otherwise on/off; None when the model lists no options."""
    level = str(level or "off").lower()
    if not options:
        return None
    if level in options:
        return level
    want = "off" if level == "off" else "on"
    return want if want in options else None


def as_chunks(events: Iterable[dict]) -> Iterator[dict]:
    """Native stream events as OpenAI-style chunks: reasoning.delta → reasoning_content, message.delta → content,
    chat.end → usage with the reasoning token count."""
    for ev in events:
        kind = ev.get("type") if isinstance(ev, dict) else None
        if kind == "reasoning.delta":
            yield {"choices": [{"delta": {"reasoning_content": str(ev.get("content") or "")}}]}
        elif kind == "message.delta":
            yield {"choices": [{"delta": {"content": str(ev.get("content") or "")}}]}
        elif kind == "chat.end":
            stats = (ev.get("result") or {}).get("stats") or {}
            usage = {"prompt_tokens": int(stats.get("input_tokens") or 0),
                     "completion_tokens": int(stats.get("total_output_tokens") or 0),
                     "completion_tokens_details": {"reasoning_tokens": int(stats.get("reasoning_output_tokens") or 0)}}
            yield {"choices": [{"delta": {}}], "usage": usage, "stats": stats}
