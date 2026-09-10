"""Overnight fleet autotune batch (#891): one queue of (host, model) tunes walked
in order on a manager thread, applied when they verify, summarised once."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("llm-systems-manager.autotune_batch")

OBJECTIVES = ("fit", "speed", "balanced", "serve", "quiet")
MAX_ITEMS = 32
BUDGET_RANGE = (5, 1440)
POWER_CAP_RANGE = (20.0, 5000.0)
START_AHEAD_MAX_S = 86400.0
START_BEHIND_MAX_S = 60.0
ITEM_MIN_BUDGET = 5
ITEM_CAP_S = 7200.0
POLL_S = 1.0
BATCH_RETENTION = 50
DIMS_MAX_BYTES = 8192
CANCELLED = "cancelled"

_ITEM_KEYS = ("agent_id", "hostname", "model_id", "status", "run_id", "run_ts", "started", "finished",
              "gain_pct", "ctx", "wh_per_ktok", "verify_ok", "applied", "note")


# ── validation ──

def validate_body(body: dict, host_ids: set, now: float) -> dict:
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"items: at most {MAX_ITEMS}")
    out_items, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("items: each entry must be an object")
        aid = str(it.get("agent_id") or "")
        mid = str(it.get("model_id") or "").strip()
        if aid not in host_ids:
            raise ValueError("items: agent_id must be an approved llama host")
        if not mid or len(mid) > 200:
            raise ValueError("items: model_id required (1–200 chars)")
        if (aid, mid) in seen:
            raise ValueError("items: duplicate host · model")
        seen.add((aid, mid))
        out_items.append({"agent_id": aid, "model_id": mid})
    objective = str(body.get("objective") or "balanced")
    if objective not in OBJECTIVES:
        raise ValueError("objective must be one of " + ", ".join(OBJECTIVES))
    dims = body.get("dims")
    if not isinstance(dims, dict) or len(json.dumps(dims)) > DIMS_MAX_BYTES:
        raise ValueError("dims must be an object")
    try:
        budget = int(body.get("budget_min"))
    except (TypeError, ValueError):
        raise ValueError("budget_min must be an integer")
    if not BUDGET_RANGE[0] <= budget <= BUDGET_RANGE[1]:
        raise ValueError(f"budget_min must be {BUDGET_RANGE[0]}–{BUDGET_RANGE[1]}")
    raw_start = body.get("start_at")
    if raw_start in (None, 0, ""):
        start_at = float(now)
    else:
        try:
            start_at = float(raw_start)
        except (TypeError, ValueError):
            raise ValueError("start_at must be a unix timestamp")
        if not math.isfinite(start_at) or start_at < now - START_BEHIND_MAX_S or start_at > now + START_AHEAD_MAX_S:
            raise ValueError("start_at must be within the next 24 h")
        start_at = max(start_at, float(now))
    cap = None
    if objective == "quiet":
        try:
            cap = float(body.get("power_cap_w"))
        except (TypeError, ValueError):
            raise ValueError("power_cap_w is required for quiet")
        if not math.isfinite(cap) or not POWER_CAP_RANGE[0] <= cap <= POWER_CAP_RANGE[1]:
            raise ValueError(f"power_cap_w must be {POWER_CAP_RANGE[0]:.0f}–{POWER_CAP_RANGE[1]:.0f}")
    return {"items": out_items, "objective": objective, "dims": dims, "budget_min": budget,
            "start_at": start_at, "restart": bool(body.get("restart", True)), "power_cap_w": cap}


def new_batch(req: dict, hostnames: dict, now: float) -> dict:
    items = [{"agent_id": it["agent_id"], "hostname": hostnames.get(it["agent_id"]) or it["agent_id"][:8],
              "model_id": it["model_id"], "status": "queued", "run_id": None, "run_ts": None, "started": None,
              "finished": None, "gain_pct": None, "ctx": None, "wh_per_ktok": None, "verify_ok": None,
              "applied": False, "note": None} for it in req["items"]]
    return {"id": uuid.uuid4().hex[:12], "created": float(now), "start_at": float(req["start_at"]),
            "started": None, "finished": None, "status": "queued", "objective": req["objective"],
            "dims": req["dims"], "budget_min": int(req["budget_min"]), "restart": bool(req["restart"]),
            "power_cap_w": req["power_cap_w"], "items": items, "current": None, "summary": None,
            "error": None, "restarted": [], "restart_errors": {}}


# ── pure helpers ──

def _gain(doc: dict) -> Optional[float]:
    after = ((doc.get("after") or {}).get("decode_tps"))
    before = ((doc.get("before") or {}).get("decode_tps"))
    try:
        a, b = float(after), float(before)
    except (TypeError, ValueError):
        return None
    if not b or not math.isfinite(a) or not math.isfinite(b):
        return None
    return (a - b) / b * 100.0


def should_apply(doc: dict) -> tuple[bool, str]:
    if not (doc.get("verify") or {}).get("ok"):
        return False, "verify did not pass"
    g = _gain(doc)
    if g is not None and g < 0:
        return False, "slower than the live config"
    if not any(c.get("selected") for c in (doc.get("changes") or []) if isinstance(c, dict)):
        return False, "no change recommended"
    return True, ""


def item_from_done(item: dict, doc: dict) -> None:
    after = doc.get("after") or {}
    item["run_id"] = doc.get("run_id") or item.get("run_id")
    item["run_ts"] = datetime.now(timezone.utc).isoformat()
    item["gain_pct"] = _gain(doc)
    item["ctx"] = after.get("ctx")
    item["wh_per_ktok"] = after.get("wh_per_ktok")
    item["verify_ok"] = (doc.get("verify") or {}).get("ok")


def _gain_text(g) -> str:
    if g is None:
        return "n/a"
    r = int(round(g))
    return f"+{r} %" if r >= 0 else f"−{abs(r)} %"


def _ctx_text(ctx) -> str:
    try:
        c = int(ctx)
    except (TypeError, ValueError):
        return "ctx ?"
    return f"ctx {c // 1024}k" if c >= 1024 else f"ctx {c}"


def summary_of(batch: dict) -> dict:
    items = batch.get("items") or []
    counts = {"tuned": 0, "applied": 0, "skipped": 0, "failed": 0, "cancelled": 0}
    lines = []
    for it in items:
        head = f"{it.get('hostname')} · {it.get('model_id')}"
        st = it.get("status")
        if st == "done":
            counts["tuned"] += 1
            counts["applied"] += 1 if it.get("applied") else 0
            tail = "applied" if it.get("applied") else f"not applied · {it.get('note') or 'no reason recorded'}"
            lines.append(f"{head} · {_gain_text(it.get('gain_pct'))} · {_ctx_text(it.get('ctx'))} · {tail}")
        elif st in ("skipped", "failed", "cancelled"):
            counts[st] += 1
            note = it.get("note")
            lines.append(f"{head} · {st}" + (f" · {note}" if note else ""))
        else:
            lines.append(f"{head} · {st}")
    if batch.get("restarted"):
        lines.append("restarted: " + ", ".join(batch["restarted"]))
    for host, err in (batch.get("restart_errors") or {}).items():
        lines.append(f"restart failed on {host}: {err}")
    title = f"Overnight autotune: {counts['tuned']} tuned, {counts['applied']} applied, {counts['skipped']} skipped"
    return {**counts, "title": title, "body": "\n".join(lines)}


def alert_payload(batch: dict) -> dict:
    s = batch.get("summary") or summary_of(batch)
    return {"name": s["title"], "source": "autotune", "metric": "autotune/batch", "severity": "info",
            "value": s["applied"], "threshold": 0, "message": s["body"]}


# ── store ──

def init_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autotune_batches (
            id           TEXT PRIMARY KEY,
            created      REAL NOT NULL,
            start_at     REAL NOT NULL,
            status       TEXT NOT NULL,
            objective    TEXT,
            dims_json    TEXT,
            budget_min   INTEGER,
            items_json   TEXT,
            summary_json TEXT,
            error        TEXT,
            extra_json   TEXT
        )""")
    conn.commit()


_EXTRA_KEYS = ("started", "finished", "restart", "power_cap_w", "current", "restarted", "restart_errors")


class Store:
    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]):
        self._conn = conn_factory
        self._lock = threading.Lock()

    def save(self, b: dict) -> None:
        extra = {k: b.get(k) for k in _EXTRA_KEYS}
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO autotune_batches "
                "(id, created, start_at, status, objective, dims_json, budget_min, items_json, summary_json, error, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (b["id"], b["created"], b["start_at"], b["status"], b.get("objective"), json.dumps(b.get("dims") or {}),
                 b.get("budget_min"), json.dumps(b.get("items") or []),
                 json.dumps(b["summary"]) if b.get("summary") is not None else None, b.get("error"), json.dumps(extra)))
            conn.execute("DELETE FROM autotune_batches WHERE id NOT IN "
                         "(SELECT id FROM autotune_batches ORDER BY created DESC LIMIT ?)", (BATCH_RETENTION,))
            conn.commit()

    @staticmethod
    def _row(r) -> dict:
        extra = json.loads(r[10]) if r[10] else {}
        return {"id": r[0], "created": r[1], "start_at": r[2], "status": r[3], "objective": r[4],
                "dims": json.loads(r[5]) if r[5] else {}, "budget_min": r[6],
                "items": json.loads(r[7]) if r[7] else [], "summary": json.loads(r[8]) if r[8] else None,
                "error": r[9], "started": extra.get("started"), "finished": extra.get("finished"),
                "restart": bool(extra.get("restart", True)), "power_cap_w": extra.get("power_cap_w"),
                "current": extra.get("current"), "restarted": extra.get("restarted") or [],
                "restart_errors": extra.get("restart_errors") or {}}

    _SEL = ("SELECT id, created, start_at, status, objective, dims_json, budget_min, items_json, "
            "summary_json, error, extra_json FROM autotune_batches ")

    def get(self, batch_id: str) -> Optional[dict]:
        with self._lock:
            r = self._conn().execute(self._SEL + "WHERE id = ?", (batch_id,)).fetchone()
        return self._row(r) if r else None

    def recent(self, limit: int = 10) -> list:
        with self._lock:
            rows = self._conn().execute(self._SEL + "ORDER BY created DESC LIMIT ?", (int(limit),)).fetchall()
        return [self._row(r) for r in rows]

    def active(self) -> Optional[dict]:
        with self._lock:
            r = self._conn().execute(self._SEL + "WHERE status IN ('queued', 'running') "
                                     "ORDER BY created DESC LIMIT 1").fetchone()
        return self._row(r) if r else None

    def recover(self, now: float) -> list:
        """Fail batches left running by a restart; return queued ones to re-arm."""
        rearm = []
        for b in self.recent(BATCH_RETENTION):
            if b["status"] == "running":
                for it in b["items"]:
                    if it["status"] in ("queued", "running"):
                        it.update({"status": "failed", "note": "manager restarted", "finished": now})
                b.update({"status": "failed", "error": "manager restarted mid-batch", "finished": now,
                          "summary": summary_of(b)})
                self.save(b)
            elif b["status"] == "queued":
                rearm.append(b)
        return rearm


# ── runner ──

@dataclass
class Deps:
    hosts: Callable[[], list]                                   # [{agent_id, hostname, online}]
    busy_agents: Callable[[], set]                              # manager-side tool activity
    preflight: Callable[[str], Optional[dict]]                  # GET /llama/autotune/preflight, None when unreachable
    run_on_agent: Callable[[str, dict], tuple]                  # (ok, run_id | error)
    stream_on_agent: Callable[[str], Iterator[dict]]            # decoded /llama/autotune/stream events
    cancel_on_agent: Callable[[str], bool]
    stop_server: Callable[[str], tuple]                         # (ok, error)
    restart_server: Callable[[str], tuple]                      # (ok, error)
    read_config: Callable[[str], Optional[dict]]                # whole config.ini document
    write_config: Callable[[str, dict], tuple]                  # (ok, error)
    active_profile: Callable[[str, str], Optional[str]]
    save_profile: Callable[[str, str, str, dict, bool], Any]
    alert: Callable[[dict], bool]
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    shutting_down: Callable[[], bool] = lambda: False


class Runner:
    def __init__(self, store: Store, deps: Deps):
        self.store, self.deps = store, deps
        self._lock = threading.Lock()
        self._cancel: set = set()
        self._current: dict = {}     # batch_id -> agent_id of the running item

    def start(self, batch: dict) -> Optional[threading.Thread]:
        import sys
        if "pytest" in sys.modules:
            return None
        t = threading.Thread(target=self.run, args=(batch["id"],), name="autotune-batch", daemon=True)
        t.start()
        return t

    def cancel(self, batch_id: str) -> Optional[str]:
        """Flag the batch; a queued batch ends at once, a running one after its item."""
        b = self.store.get(batch_id)
        if not b or b["status"] not in ("queued", "running"):
            return None
        with self._lock:
            self._cancel.add(batch_id)
            aid = self._current.get(batch_id)
        if b["status"] == "queued":
            b.update({"status": "cancelled", "finished": self.deps.now()})
            for it in b["items"]:
                it["status"] = "cancelled"
            b["summary"] = summary_of(b)
            self.store.save(b)
            return "cancelled"
        if aid:
            try:
                self.deps.cancel_on_agent(aid)
            except Exception as e:  # noqa: BLE001
                log.warning("autotune batch: cancel on agent failed: %s", e)
        return "running"

    def _cancelled(self, batch_id: str) -> bool:
        with self._lock:
            return batch_id in self._cancel

    def run(self, batch_id: str) -> None:
        d = self.deps
        b = self.store.get(batch_id)
        if not b or b["status"] != "queued":
            return
        try:
            if not self._wait(b):
                return
            b.update({"status": "running", "started": d.now()})
            self.store.save(b)
            stopped: list = []
            for i, it in enumerate(b["items"]):
                if self._cancelled(batch_id):
                    it.update({"status": "cancelled", "finished": d.now()})
                    continue
                if not self._item(b, i, stopped):
                    break
            self._finish_hosts(b, stopped)
            b.update({"status": "cancelled" if self._cancelled(batch_id) else "done", "finished": d.now(),
                      "current": None})
            for it in b["items"]:
                if it["status"] == "queued":
                    it.update({"status": "cancelled", "finished": d.now()})
            b["summary"] = summary_of(b)
            self.store.save(b)
            try:
                d.alert(alert_payload(b))
            except Exception as e:  # noqa: BLE001
                log.warning("autotune batch: summary alert failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("autotune batch %s failed: %s", batch_id, e)
            b.update({"status": "failed", "error": str(e)[:300], "finished": d.now(), "current": None})
            b["summary"] = summary_of(b)
            self.store.save(b)
        finally:
            with self._lock:
                self._cancel.discard(batch_id)
                self._current.pop(batch_id, None)

    def _wait(self, b: dict) -> bool:
        d = self.deps
        while d.now() < b["start_at"]:
            if d.shutting_down():
                return False
            if self._cancelled(b["id"]):
                b.update({"status": "cancelled", "finished": d.now()})
                for it in b["items"]:
                    it["status"] = "cancelled"
                b["summary"] = summary_of(b)
                self.store.save(b)
                return False
            d.sleep(min(POLL_S, max(0.0, b["start_at"] - d.now())))
        return not self._cancelled(b["id"]) or self._cancel_now(b)

    def _cancel_now(self, b: dict) -> bool:
        b.update({"status": "cancelled", "finished": self.deps.now()})
        for it in b["items"]:
            it["status"] = "cancelled"
        b["summary"] = summary_of(b)
        self.store.save(b)
        return False

    def _budget(self, b: dict) -> Optional[int]:
        queued = sum(1 for it in b["items"] if it["status"] in ("queued", "running"))
        elapsed_min = (self.deps.now() - float(b["started"] or self.deps.now())) / 60.0
        remaining = b["budget_min"] - elapsed_min
        if remaining < ITEM_MIN_BUDGET:
            return None
        return max(ITEM_MIN_BUDGET, int(remaining // max(1, queued)))

    def _precheck(self, it: dict, stopped: list) -> Optional[str]:
        d = self.deps
        aid = it["agent_id"]
        host = next((h for h in d.hosts() if h.get("agent_id") == aid), None)
        if not host or not host.get("online"):
            return "host offline"
        if aid in (d.busy_agents() or set()):
            return "host busy"
        pre = d.preflight(aid)
        if not isinstance(pre, dict) or not pre.get("ok", True):
            return "host unreachable"
        if pre.get("busy"):
            return "host busy"
        if it["model_id"] not in (pre.get("sizes") or {}):
            return "model not configured"
        if pre.get("unit_active") and aid not in stopped:
            ok, err = d.stop_server(aid)
            if not ok:
                return f"could not stop llama-server: {err}"
            stopped.append(aid)
        return None

    def _item(self, b: dict, i: int, stopped: list) -> bool:
        """Run one item; False when the batch should stop walking."""
        d = self.deps
        it = b["items"][i]
        it.update({"status": "running", "started": d.now()})
        b["current"] = i
        self.store.save(b)
        budget = self._budget(b)
        if budget is None:
            it.update({"status": "skipped", "note": "out of budget", "finished": d.now()})
            self.store.save(b)
            return True
        reason = self._precheck(it, stopped)
        if reason:
            it.update({"status": "skipped" if not reason.startswith("could not") else "failed",
                       "note": reason, "finished": d.now()})
            self.store.save(b)
            return True
        body = {"model_ids": [it["model_id"]], "objective": b["objective"], "budget_min": budget, "dims": b["dims"]}
        if b.get("power_cap_w") is not None:
            body["power_cap_w"] = b["power_cap_w"]
        ok, val = d.run_on_agent(it["agent_id"], body)
        if not ok:
            it.update({"status": "failed", "note": str(val)[:300], "finished": d.now()})
            self.store.save(b)
            return True
        it["run_id"] = str(val)
        with self._lock:
            self._current[b["id"]] = it["agent_id"]
        self.store.save(b)
        doc, err = self._follow(b, it)
        with self._lock:
            self._current.pop(b["id"], None)
        if err == CANCELLED:
            it.update({"status": "cancelled", "note": "cancelled", "finished": d.now()})
            self.store.save(b)
            return False
        if doc is None:
            it.update({"status": "failed", "note": err or "run ended without a result", "finished": d.now()})
            self.store.save(b)
            return True
        item_from_done(it, doc)
        if not doc.get("ok"):
            it.update({"status": "failed", "note": str(doc.get("stop_reason") or "run failed")[:300], "finished": d.now()})
            self.store.save(b)
            return True
        apply_ok, why = should_apply(doc)
        if apply_ok:
            aerr = self._apply(b, it, doc)
            it["applied"] = aerr is None
            it["note"] = aerr or f"applied {sum(1 for c in doc['changes'] if c.get('selected'))} change" + \
                ("" if sum(1 for c in doc["changes"] if c.get("selected")) == 1 else "s")
        else:
            it["applied"] = False
            it["note"] = why
        it.update({"status": "done", "finished": d.now()})
        self.store.save(b)
        return True

    def _follow(self, b: dict, it: dict) -> tuple:
        d = self.deps
        deadline = d.now() + ITEM_CAP_S
        doc, err = None, None
        try:
            for ev in d.stream_on_agent(it["agent_id"]):
                if self._cancelled(b["id"]):
                    d.cancel_on_agent(it["agent_id"])
                    return None, CANCELLED
                if d.now() > deadline:
                    d.cancel_on_agent(it["agent_id"])
                    return None, "timed out after 2 h"
                if not isinstance(ev, dict):
                    continue
                t = ev.get("type")
                if t == "model_done" and ev.get("model_id") == it["model_id"]:
                    doc = ev
                elif t == "done":
                    if doc is None and ev.get("error"):
                        err = str(ev["error"])[:300]
                    break
        except Exception as e:  # noqa: BLE001
            return None, f"stream failed: {str(e)[:200]}"
        return doc, err

    def _apply(self, b: dict, it: dict, doc: dict) -> Optional[str]:
        d = self.deps
        aid, mid = it["agent_id"], it["model_id"]
        cfg = d.read_config(aid)
        if not isinstance(cfg, dict) or not isinstance(cfg.get(mid), dict):
            return "config unavailable"
        section = dict(cfg[mid])
        stamp = datetime.fromtimestamp(d.now(), timezone.utc).strftime("%Y-%m-%d")
        try:
            d.save_profile(aid, mid, f"before batch {stamp}", section, False)
        except Exception as e:  # noqa: BLE001
            return f"before-profile save failed: {str(e)[:120]}"
        for c in doc.get("changes") or []:
            if isinstance(c, dict) and c.get("selected") and c.get("key"):
                section[str(c["key"])] = str(c.get("recommended", ""))
        out = {k: v for k, v in cfg.items() if k != "__DEFAULTS__"}
        out[mid] = section
        ok, err = d.write_config(aid, out)
        if not ok:
            return f"config write failed: {err}"
        try:
            ap = d.active_profile(aid, mid)
            if ap:
                d.save_profile(aid, mid, ap, section, True)
        except Exception as e:  # noqa: BLE001
            log.warning("autotune batch: active profile sync failed: %s", e)
        return None

    def _finish_hosts(self, b: dict, stopped: list) -> None:
        """Restart every host the batch stopped, plus applied hosts when the toggle is on."""
        d = self.deps
        want = list(stopped)
        if b.get("restart"):
            for it in b["items"]:
                if it.get("applied") and it["agent_id"] not in want:
                    want.append(it["agent_id"])
        names = {it["agent_id"]: it["hostname"] for it in b["items"]}
        for aid in want:
            try:
                ok, err = d.restart_server(aid)
            except Exception as e:  # noqa: BLE001
                ok, err = False, str(e)[:120]
            if ok:
                b["restarted"].append(names.get(aid) or aid[:8])
            else:
                b["restart_errors"][names.get(aid) or aid[:8]] = str(err or "unknown error")[:200]
