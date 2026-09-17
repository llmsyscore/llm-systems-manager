"""Leftover finder for the model profile and alias stores (#1009).

Groups saved entries by why they no longer match the fleet; prunes only
smoke-test artefacts. Operator entries are surfaced, never auto-deleted.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("llm-systems-manager.stores")

SMOKE_PREFIX = "smoke-test"
FIRST_RUN_GRACE_S = 20.0
INTERVAL_S = 6 * 3600
INDEX_WAIT_S = 10.0


def by_agent(catalog: dict, serving: dict) -> "dict[str, set[str]]":
    """Agent id -> model ids it lists or serves, from the gateway index maps."""
    out: "dict[str, set[str]]" = {}
    for src in (catalog or {}, serving or {}):
        for key, aids in src.items():
            _prov, _, mid = str(key).partition(":")
            for aid in aids or ():
                out.setdefault(str(aid), set()).add(mid)
    return out


def classify(profiles: dict, aliases: dict, agents: dict, index: "Optional[tuple]") -> dict:
    """Sorts store entries into smoke, removed_agents, absent_models, absent_aliases and the unverified lists."""
    seen = by_agent(*index) if index else {}
    known = set().union(*seen.values()) if seen else set()
    smoke: list = []
    removed: list = []
    absent: list = []
    unverified_models: list = []
    unverified: "set[str]" = set()
    for aid, models in (profiles or {}).items():
        models = models if isinstance(models, dict) else {}
        real = {}
        for mid, entry in models.items():
            if str(mid).startswith(SMOKE_PREFIX):
                smoke.append({"agent": aid, "model": mid})
            else:
                real[mid] = entry
        if aid not in agents:
            if real:
                removed.append({"agent": aid, "models": sorted(real),
                                "profiles": sum(len((e or {}).get("profiles") or {}) for e in real.values())})
            continue
        host = str((agents.get(aid) or {}).get("hostname") or aid)
        for mid in sorted(real):
            row = {"agent": aid, "host": host, "model": mid, "profiles": len((real[mid] or {}).get("profiles") or {})}
            if aid not in seen:
                unverified.add(host)
                unverified_models.append(row)
            elif mid not in seen[aid]:
                absent.append(row)
    alias_rows = [{"model": mid, "alias": name} for mid, name in sorted((aliases or {}).items())]
    absent_aliases = [r for r in alias_rows if r["model"] not in known] if known else []
    return {"smoke": smoke, "removed_agents": removed, "absent_models": absent, "absent_aliases": absent_aliases,
            "unverified_models": unverified_models, "unverified_aliases": [] if known else alias_rows,
            "aliases_checked": bool(known), "unverified": sorted(unverified)}


class Reconciler:
    """Runs the classifier over the live stores and applies removals."""

    def __init__(self, store, load_aliases: Callable[[], dict], save_aliases: Callable[[dict], None],
                 load_agents: Callable[[], dict], index_snapshot: Callable[[], Optional[tuple]],
                 refresh_index: Optional[Callable[[], None]] = None, now: Callable[[], float] = time.time) -> None:
        self._store = store
        self._load_aliases = load_aliases
        self._save_aliases = save_aliases
        self._load_agents = load_agents
        self._index_snapshot = index_snapshot
        self._refresh_index = refresh_index
        self._now = now
        self._lock = threading.Lock()
        self.last: Optional[dict] = None

    def run(self, reason: str = "") -> dict:
        with self._lock:
            agents = (self._load_agents() or {}).get("agents") or {}
            groups = classify(self._store.snapshot(), self._load_aliases(), agents, self._index_snapshot())
            pruned = 0
            for item in groups.pop("smoke"):
                if self._store.drop_model(item["agent"], item["model"]):
                    pruned += 1
            if pruned:
                log.info("stores reconcile: pruned %d smoke-test profile entr%s", pruned, "y" if pruned == 1 else "ies")
            counts = {k: len(groups[k]) for k in ("removed_agents", "absent_models", "absent_aliases",
                                                   "unverified_models", "unverified_aliases")}
            self.last = {"at": self._now(), "reason": reason, "pruned_smoke": pruned, "counts": counts,
                         "total": sum(counts.values()), **groups}
            return dict(self.last)

    def on_model_deleted(self, agent_id: str, model_id: str) -> dict:
        """Cascade for a model the manager deleted: its profiles on that host, and its name when no other host lists it."""
        removed = {"profiles": False, "alias": False}
        with self._lock:
            removed["profiles"] = self._store.drop_model(agent_id, model_id)
            index = self._index_snapshot()
            elsewhere = any(model_id in ids for aid, ids in by_agent(*index).items() if aid != agent_id) if index else False
            aliases = dict(self._load_aliases() or {})
            if not elsewhere and aliases.pop(model_id, None) is not None:
                self._save_aliases(aliases)
                removed["alias"] = True
        if any(removed.values()):
            log.info("stores: model %s deleted on agent %s — dropped %s", model_id, agent_id[:8],
                     ", ".join(k for k, v in removed.items() if v))
        self.run_bg("model delete")
        return removed

    def on_agent_deleted(self, agent_id: str) -> bool:
        """Cascade for a deleted agent: every profile saved under its id."""
        with self._lock:
            dropped = self._store.drop_agent(agent_id)
        if dropped:
            log.info("stores: agent %s deleted — dropped its saved profiles", agent_id[:8])
        self.run_bg("agent delete")
        return dropped

    def run_bg(self, reason: str = "") -> None:
        threading.Thread(target=self._safe_run, args=(reason,), name="stores-reconcile", daemon=True).start()

    def _safe_run(self, reason: str) -> None:
        try:
            self.run(reason)
        except Exception:  # noqa: BLE001
            log.warning("stores reconcile failed", exc_info=True)

    def leftovers(self) -> dict:
        return dict(self.last) if self.last is not None else self.run("first read")

    def clean(self, sel: dict) -> dict:
        """Removes the selected entries: {"all": true} or {"agents", "models", "aliases"} lists."""
        sel = sel if isinstance(sel, dict) else {}
        if sel.get("all"):
            cur = self.leftovers()
            sel = {"agents": [r["agent"] for r in cur["removed_agents"]],
                   "models": [{"agent": r["agent"], "model": r["model"]}
                              for r in cur["absent_models"] + cur["unverified_models"]],
                   "aliases": [r["model"] for r in cur["absent_aliases"] + cur["unverified_aliases"]]}
        removed = {"agents": 0, "models": 0, "aliases": 0}
        with self._lock:
            for aid in sel.get("agents") or []:
                if isinstance(aid, str) and self._store.drop_agent(aid):
                    removed["agents"] += 1
            for item in sel.get("models") or []:
                if isinstance(item, dict) and self._store.drop_model(str(item.get("agent") or ""), str(item.get("model") or "")):
                    removed["models"] += 1
            wanted = [m for m in (sel.get("aliases") or []) if isinstance(m, str)]
            if wanted:
                aliases = dict(self._load_aliases() or {})
                for mid in wanted:
                    if aliases.pop(mid, None) is not None:
                        removed["aliases"] += 1
                if removed["aliases"]:
                    self._save_aliases(aliases)
        if any(removed.values()):
            log.info("stores reconcile: removed %s", removed)
        return {"removed": removed, **self.run("clean")}

    def start_thread(self, stop: Callable[[], bool]) -> None:
        def _loop() -> None:
            waited = 0.0
            while waited < FIRST_RUN_GRACE_S and not stop():
                time.sleep(0.5)
                waited += 0.5
            while not stop():
                if self._refresh_index is not None:
                    try:
                        self._refresh_index()
                    except Exception:  # noqa: BLE001
                        log.warning("stores reconcile: index refresh failed", exc_info=True)
                self._safe_run("scheduled")
                slept = 0.0
                while slept < INTERVAL_S and not stop():
                    time.sleep(1.0)
                    slept += 1.0
        threading.Thread(target=_loop, name="stores-reconcile", daemon=True).start()
