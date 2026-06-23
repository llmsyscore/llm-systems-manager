"""llama.cpp router-mode /models/sse consumer.

Stdlib-only at import; requests is imported lazily in requests_sse_lines.
"""
from __future__ import annotations

import json
import logging
import shlex
import time
from typing import Any, Callable, Iterable, Iterator, NamedTuple, Optional

_DEFAULT_LOGGER = logging.getLogger("llm-systems-agent.llama_sse")

# Router (multi-model) flags vs single-model flags. -m/--model forces single.
_ROUTER_FLAGS = frozenset({
    "--models-max", "--models-preset", "--models-dir", "--models-autoload",
})
_SINGLE_MODEL_FLAGS = frozenset({"-m", "--model"})

# llama.cpp model_status value -> agent llama_state. None = don't force binary.
_SSE_STATUS_TO_STATE = {
    "loaded": "awake",
    "loading": "awake",
    "sleeping": "sleeping",
    "unloaded": None,
    "downloading": None,
    "failed": None,
}


class SseFrame(NamedTuple):
    event: str
    data: str


def parse_exec_flags(exec_line: str) -> list[str]:
    """Flag tokens (those starting with '-') from a shell ExecStart command."""
    try:
        parts = shlex.split(exec_line)
    except ValueError:
        return []
    return [p for p in parts if p.startswith("-")]


def router_mode_from_flags(flags: Iterable[str]) -> bool:
    """True when a router flag is present and no single-model flag is."""
    flag_set = set(flags)
    if flag_set & _SINGLE_MODEL_FLAGS:
        return False
    return bool(flag_set & _ROUTER_FLAGS)


def sse_status_to_state(status: Optional[str]) -> Optional[str]:
    """Map a model_status value to 'awake'/'sleeping', else None."""
    return _SSE_STATUS_TO_STATE.get((status or "").lower())


def select_wake_target(models: Iterable[Any],
                       requested: Optional[str] = None) -> Optional[str]:
    """Pick the /v1/models id to warm: requested (when listed), else the
    sleeping model, else the loaded one, else the first listed."""
    entries = [m for m in models if isinstance(m, dict) and m.get("id")]
    if requested and any(m["id"] == requested for m in entries):
        return requested
    for want in ("sleeping", "loaded"):
        for m in entries:
            st = m.get("status")
            if (st.get("value") if isinstance(st, dict) else None) == want:
                return m["id"]
    return entries[0]["id"] if entries else None


def _inner(data: dict[str, Any]) -> dict[str, Any]:
    """The real envelope nests fields under data['data']; fall back to top level."""
    nested = data.get("data")
    return nested if isinstance(nested, dict) else data


def status_value(data: dict[str, Any]) -> Optional[str]:
    """Extract a status value from data['data'].status (or top-level status/state)."""
    inner = _inner(data)
    st = inner.get("status")
    if isinstance(st, dict):
        st = st.get("value")
    return st or inner.get("state")


def progress_value(data: dict[str, Any]) -> Any:
    """First present download-progress field: progress, percent, or pct."""
    inner = _inner(data)
    for k in ("progress", "percent", "pct"):
        if inner.get(k) is not None:
            return inner[k]
    return None


def event_kind(sse_event: str, data: dict[str, Any]) -> str:
    """Best-effort event name: SSE event field, else a type/event key in data."""
    if sse_event and sse_event != "message":
        return sse_event
    return data.get("type") or data.get("event") or "message"


def parse_sse_stream(lines: Iterable[Optional[str]]) -> Iterator[SseFrame]:
    """Parse SSE framing; dispatch one SseFrame per data line.

    llama.cpp emits one complete JSON object per `data:` line, with or without
    blank-line terminators, so each data line is its own event.
    """
    event: Optional[str] = None
    for raw in lines:
        line = (raw or "").rstrip("\n").rstrip("\r")
        if line == "":
            event = None
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "event":
            event = value
        elif field == "data":
            yield SseFrame(event or "message", value)
            event = None


def requests_sse_lines(
    url: str,
    *,
    session: Any = None,
    headers: Optional[dict[str, str]] = None,
    connect_timeout: float = 5.0,
    read_timeout: float = 300.0,
) -> Iterator[str]:
    """Open a streaming GET eagerly; return an iterator of decoded SSE lines."""
    if session is None:
        import requests
        session = requests
    resp = session.get(
        url, stream=True, headers=headers or {},
        timeout=(connect_timeout, read_timeout),
    )
    try:
        resp.raise_for_status()
    except Exception:
        resp.close()
        raise

    def _iter() -> Iterator[str]:
        try:
            for line in resp.iter_lines(decode_unicode=True):
                yield line if line is not None else ""
        finally:
            resp.close()
    return _iter()


class LlamaSseListener:
    """Reconnecting consumer of llama.cpp /models/sse with backoff.

    Callbacks (connect/on_event/should_stop/sleep/on_disconnect) are injected.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], Iterable[Optional[str]]],
        on_event: Callable[[str, dict[str, Any]], None],
        should_stop: Callable[[], bool],
        sleep: Callable[[float], None] = time.sleep,
        on_disconnect: Optional[Callable[[], None]] = None,
        logger: Optional[logging.Logger] = None,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
        fail_warn_threshold: int = 5,
    ) -> None:
        self._connect = connect
        self._on_event = on_event
        self._should_stop = should_stop
        self._sleep = sleep
        self._on_disconnect = on_disconnect
        self._log = logger or _DEFAULT_LOGGER
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._fail_warn_threshold = fail_warn_threshold
        # Cross-thread status flag, read lock-free by the wiring.
        self.connected = False

    def run(self) -> None:
        # should_stop is checked only at connection boundaries.
        backoff = self._initial_backoff
        fail_streak = 0
        healthy = False
        while not self._should_stop():
            opened = False
            try:
                lines = self._connect()
                opened = True
                self.connected = True
                if not healthy:
                    healthy = True
                    self._log.info("llama /models/sse connected")
                for frame in parse_sse_stream(lines):
                    obj = self._parse_data(frame.data)
                    if obj is None:
                        continue
                    self._on_event(frame.event, obj)
            except Exception as e:
                if opened:
                    # Established stream ended (idle read-timeout / drop) — expected.
                    self._log.debug("llama /models/sse stream ended: %s", type(e).__name__)
                else:
                    fail_streak += 1
                    if fail_streak == self._fail_warn_threshold:
                        healthy = False
                        self._log.warning(
                            "llama /models/sse down for %d consecutive attempts: %s",
                            fail_streak, e)
                    else:
                        self._log.debug("llama /models/sse connect error: %s", e)
            finally:
                self.connected = False
                if opened and self._on_disconnect is not None:
                    try:
                        self._on_disconnect()
                    except Exception as e:
                        self._log.debug("llama /models/sse on_disconnect error: %s", e)
            if self._should_stop():
                break
            if opened:
                # Healthy connection ended; reconnect promptly without escalation.
                fail_streak = 0
                backoff = self._initial_backoff
                self._sleep(self._initial_backoff)
            else:
                self._sleep(backoff)
                backoff = min(self._max_backoff, backoff * 2)

    @staticmethod
    def _parse_data(data_str: str) -> Optional[dict[str, Any]]:
        if not data_str:
            return None
        try:
            obj = json.loads(data_str)
        except (ValueError, TypeError):
            return None
        return obj if isinstance(obj, dict) else None
