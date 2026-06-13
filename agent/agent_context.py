"""Shared frozen-dataclass deps handed to every provider via set_context()."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests


@dataclass(frozen=True)
class AgentContext:
    config: Any
    check_bearer: Callable[[Optional[str]], None]
    check_stream_auth: Callable[[Optional[str], Optional[str], str], None]
    post_session: requests.Session
    runtime_lock: threading.RLock
    state: dict[str, Any]
    now_iso: Callable[[], str]
    probe_http: Callable[..., "tuple[bool, str]"]
