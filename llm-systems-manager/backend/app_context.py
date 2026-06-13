"""Shared application context for Tier 3 leaf modules.

Populated once during manager startup and passed into each module's
`register_*(app, ctx, ...)` entrypoint. Modules read from it via attribute
access (``ctx.settings``, ``ctx.alarm_engine_url()``) instead of threading
the same dozen deps through every long ``register_*`` / ``set_deps`` kwarg
list.

Why this exists: the M1 (auth) and M2 (agent_registry) extractions each
accept their own kwargs (13 and 22 respectively). About 9 of those repeat
across modules; without a shared carrier each subsequent extraction (M3
terminal, M4 proxies, M5 openclaw) would re-thread the same kwargs again,
and the cleanup later would be a 1500-line churn diff. This puts the shared
deps in one place now while the kwarg lists are still tractable.

Conventions:
  - Add to ``Context`` only when a dep is (or will be) read by 2+ modules.
    Truly module-specific deps stay as explicit kwargs on the relevant
    ``register_*`` so the signature documents what that module actually uses.
  - Callables for live-rebound state (alarm_engine_url flips http→https at
    startup, agent_admin_allow can be edited via the admin tab, manager_secret
    is read from disk on demand) are stored as ``Callable[..., ...]`` getters
    and consumers invoke ``ctx.x()`` at use time so the latest value propagates
    without re-registration.
  - ``ae_session`` is the documented exception: it's a process-singleton
    ``requests.Session`` created exactly once at startup, never rebuilt. Stored
    as a direct reference (no getter) and consumed via ``.get()`` / ``.post()``
    method calls. If a future change adds session rebuild (e.g. CA rotation),
    flip ``ae_session`` to a ``Callable[[], Session]`` getter at the same time.
  - The dataclass is built once in main *after* every dep it carries is
    defined; passing ``ctx`` into ``auth.register_auth`` / ``agent_registry``
    therefore implies both modules see the fully-populated namespace.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Context:
    """Shared cross-module deps. Frozen so a stray ``ctx.x = ...`` raises
    instead of silently shadowing a value other modules still read."""

    settings: Any
    data_dir: Path
    version: str
    require_admin: Callable[[], Any]
    admin_ip_allowed: Callable[[str], bool]
    agent_admin_allow: Callable[[], list]
    alarm_engine_url: Callable[[], str]
    manager_secret: Callable[[], bytes]
    ae_session: Any


__all__ = ["Context"]
