"""#1031 Forecast wiring: production readers for the checks, plus the in-process counters the sampler reads."""
from __future__ import annotations

import collections
import csv
import io
import json
import logging
import math
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from forecast_checks import slug

log = logging.getLogger("llm-systems-manager.forecast")

AE_SINCE_MAX_MIN = 43200
AE_POINTS_MAX = 1500
NAMES_LIMIT = 5000
NAMES_RETRY_LIMIT = 1000
ROW_LIMIT = 5000
GATEWAY_KEYS = ("requests", "errors", "timeouts", "failovers")
MAX_GATEWAY_MODELS = 200
MODEL_KEY_MAX = 200

# Logical model actions the memory check asks for, mapped onto the manager's real audit action names.
_AUDIT_ALIASES = {
    "model.load": ("llama.load", "lms.load", "model.load"),
    "model.unload": ("llama.unload", "lms.unload", "model.unload"),
    "autopilot.load": ("autopilot:load", "autopilot:scale_up"),
    "autopilot.unload": ("autopilot:unload", "autopilot:scale_down"),
}


# ── in-process counters ───────────────────────────────────────────────────
_counts: "collections.Counter" = collections.Counter()
_gateway: "dict[str, dict[str, float]]" = {}
_counts_lock = threading.Lock()


def count(key: str, n: int = 1) -> None:
    """Adds n to a cumulative process counter."""
    if not key:
        return
    with _counts_lock:
        _counts[key] += int(n)


def count_gateway(model: Any, kind: str, n: int = 1) -> None:
    """One gateway event for one model; unknown kinds and models past the cap are dropped. Never raises.
    Only a served request opens a model's row, so a name that resolved to nothing never takes a slot."""
    try:
        if kind not in GATEWAY_KEYS:
            return
        name = str(model if model not in (None, "") else "-")[:MODEL_KEY_MAX]
        with _counts_lock:
            row = _gateway.get(name)
            if row is None:
                if kind != "requests" or len(_gateway) >= MAX_GATEWAY_MODELS:
                    return
                row = _gateway[name] = {k: 0.0 for k in GATEWAY_KEYS}
            row[kind] += float(n)
    except Exception:  # noqa: BLE001 — a counter never breaks a request
        pass


def counter_value(name: str) -> float:
    with _counts_lock:
        return float(_counts.get(name, 0))


def gateway_counts() -> "dict[str, dict]":
    """{model: {requests, errors, timeouts, failovers}} cumulative since process start."""
    with _counts_lock:
        return {model: dict(row) for model, row in _gateway.items()}


class ErrorCounter(logging.Handler):
    """Counts log records at ERROR or above."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self._n_lock = threading.Lock()
        self.n = 0

    def emit(self, record) -> None:
        try:
            with self._n_lock:
                self.n += 1
        except Exception:  # noqa: BLE001 — a counter never raises back into logging
            pass

    def handleError(self, record) -> None:
        """Swallows handler errors so a count can never recurse into logging."""


# ── helpers ───────────────────────────────────────────────────────────────
def _epoch(value: Any) -> Optional[float]:
    """ISO-8601 (Z, offset or naive UTC) or a number as epoch seconds; None when unparsable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _guard(name: str, empty: Callable[[], Any]) -> Callable:
    """Wraps a reader so any failure logs the exception type at DEBUG and yields the empty value."""
    def deco(fn):
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — one reader never stops a run
                log.debug("forecast reader %s failed: %s", name, type(e).__name__)
                return empty()
        wrapped.__name__ = name
        return wrapped
    return deco


def _rows(connect: Callable, path: Any, sql: str, params: tuple = ()) -> list:
    conn = connect(str(path), timeout=10.0)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def agent_gaps(rows: Any, now: float) -> "list[dict]":
    """One {host, gap_s, skew_s} row per host (freshest wins) from agent registry rows; skew_s only when measured."""
    best: "dict[str, dict]" = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        host = r.get("host")
        seen = _epoch(r.get("last_seen"))
        if not host or seen is None:
            continue
        gap = max(0.0, float(now) - seen)
        if host not in best or gap < best[host]["gap_s"]:
            best[host] = {"host": host, "gap_s": gap}
            skew = _num(r.get("skew_s"))
            if skew is not None:
                best[host]["skew_s"] = skew
    return [best[h] for h in sorted(best)]


def db_bytes(paths, getsize: Callable[[str], int]) -> float:
    """Summed size of the given SQLite files; missing files count as zero."""
    total = 0.0
    for p in paths or ():
        try:
            total += float(getsize(str(p)))
        except OSError:
            continue
    return total


# ── readers ───────────────────────────────────────────────────────────────
def build_readers(*, ae_get: Callable[[str], Any], db_paths: dict, now: float,
                  hosts: Callable[[], list], host_samples: Callable[[], dict],
                  agent_rows: Callable[[], list], host_of_agent: Callable[[str], str],
                  catalog: Callable[[], list], latest_agent_version: Callable[[], Any],
                  price_kwh: Callable[[], Any], findings: Callable[[], list],
                  connect: Callable = sqlite3.connect) -> "dict[str, Callable]":
    """Every CheckData reader bound to live manager state; each one is individually guarded."""
    manager_db, audit_db, energy_db = db_paths["manager"], db_paths["audit"], db_paths["energy"]
    names_cache: "dict[tuple, list]" = {}
    alerts_cache: "list[list]" = []

    @_guard("series", list)
    def _series(source, name, host, start=None, end=None, points=720, agg="mean"):
        lo = float(start) if start is not None else now - 86400.0
        hi = float(end) if end is not None else now
        since = min(AE_SINCE_MAX_MIN, max(1, int(math.ceil((now - lo) / 60.0))))
        query = urllib.parse.urlencode({"since_minutes": since, "hostname": str(host or ""),
                                        "max_points": max(1, min(int(points or 720), AE_POINTS_MAX)),
                                        "agg": "max" if str(agg) == "max" else "mean"})
        r = ae_get("/api/alarm/metrics/{}/{}?{}".format(urllib.parse.quote(str(source), safe=""),
                                                        urllib.parse.quote(str(name), safe=""), query))
        if r is None or not r.ok:
            return []
        body = r.json()
        out = []
        for p in body if isinstance(body, list) else []:
            ts, value = _epoch((p or {}).get("timestamp")), _num((p or {}).get("value"))
            if ts is None or value is None or ts < lo or ts > hi:
                continue
            out.append((ts, value))
        out.sort()
        return out

    def _names_get(source, host, limit):
        query = urllib.parse.urlencode({"source": str(source), "hostname": str(host or ""), "limit": int(limit)})
        return ae_get(f"/api/alarm/metrics?{query}")

    @_guard("names", list)
    def _names(source, host):
        key = (str(source), str(host or ""))
        hit = names_cache.get(key)
        if hit is not None:
            return list(hit)
        r = _names_get(source, host, NAMES_LIMIT)
        # A listing refused for its size is asked again for fewer names.
        if r is not None and 400 <= int(getattr(r, "status_code", 0) or 0) < 500:
            r = _names_get(source, host, NAMES_RETRY_LIMIT)
        rows = r.json() if (r is not None and r.ok) else []
        out = sorted({str(x.get("metric_name")) for x in (rows if isinstance(rows, list) else [])
                      if isinstance(x, dict) and x.get("metric_name")})
        names_cache[key] = out
        return list(out)

    @_guard("hosts", list)
    def _hosts():
        return [str(h) for h in (hosts() or []) if h]

    @_guard("mounts", dict)
    def _mounts(host):
        block = (host_samples() or {}).get(str(host)) or {}
        out = {}
        for d in block.get("disk") or []:
            mount = (d or {}).get("mountpoint") if isinstance(d, dict) else None
            if mount:
                out[slug(mount)] = str(mount)
        return out

    @_guard("alerts", list)
    def _alerts(start, end):
        if not alerts_cache:
            r = ae_get("/api/alarm/alerts/export?format=csv")
            text = r.text if (r is not None and r.ok) else ""
            rows = []
            for a in csv.DictReader(io.StringIO(text)):
                created = _epoch(a.get("created_at"))
                if created is None:
                    continue
                metric = "/".join(p for p in (str(a.get("metric_source") or "").strip(),
                                              str(a.get("metric_name") or "").strip()) if p)
                rows.append({"id": a.get("alert_id"), "rule": a.get("rule_name"),
                             "host": a.get("source_host") or None, "severity": a.get("severity"),
                             "metric": metric or None,
                             "created": created, "closed": _epoch(a.get("closed_at"))})
            alerts_cache.append(rows)
        return [r for r in alerts_cache[0] if float(start) <= r["created"] <= float(end)]

    @_guard("energy", list)
    def _energy(start, end):
        sql = ("SELECT hour_ts, hostname, energy_wh, COALESCE(tokens_gen, 0) + COALESCE(tokens_prompt, 0), "
               "active_s, observed_s FROM energy_hourly WHERE hour_ts BETWEEN ? AND ? ORDER BY hour_ts")
        rows = _rows(connect, energy_db, sql, (int(start), int(end)))
        return [{"ts": float(r[0]), "host": r[1], "wh": _num(r[2]) or 0.0, "tokens": _num(r[3]) or 0.0,
                 "active_s": _num(r[4]) or 0.0, "observed_s": _num(r[5]) or 0.0} for r in rows]

    @_guard("bench_runs", list)
    def _bench_runs():
        sql = ("SELECT ts, model_id, agent_id, gen_tps, ppt_tps, accept_rate, baseline, ok "
               f"FROM bench_live_runs ORDER BY id DESC LIMIT {ROW_LIMIT}")
        out = []
        for r in _rows(connect, manager_db, sql):
            ts = _epoch(r[0])
            if ts is None:
                continue
            out.append({"ts": ts, "model": r[1], "host": host_of_agent(r[2]), "gen_tps": _num(r[3]),
                        "ppt_tps": _num(r[4]), "accept_rate": _num(r[5]),
                        "baseline": bool(r[6]), "ok": bool(r[7])})
        return out

    @_guard("report_cards", list)
    def _report_cards():
        """Stored report cards with their generation speed in tok/s."""
        sql = f"SELECT ts, agent_id, result FROM report_cards ORDER BY id DESC LIMIT {ROW_LIMIT}"
        out = []
        for r in _rows(connect, manager_db, sql):
            try:
                result = json.loads(r[2]) if r[2] else {}
            except ValueError:
                continue
            tok_s = _num((result or {}).get("gen_tps"))
            if tok_s is None or tok_s <= 0 or not (result or {}).get("model"):
                continue
            out.append({"ts": _epoch(r[0]) or 0.0, "host": host_of_agent(r[1]),
                        "model": result["model"], "tok_s": tok_s})
        return out

    @_guard("audit", list)
    def _audit(actions, start, end):
        wanted = []
        for a in actions or ():
            wanted.extend(_AUDIT_ALIASES.get(str(a), (str(a),)))
        if not wanted:
            return []
        marks = ", ".join("?" * len(wanted))
        sql = (f"SELECT ts, action, target, detail FROM audit_log WHERE ts >= ? AND ts < ? AND action IN ({marks}) "
               f"ORDER BY id DESC LIMIT {ROW_LIMIT}")
        out = []
        for r in _rows(connect, audit_db, sql, (_iso(start), _iso(end), *wanted)):
            try:
                detail = json.loads(r[3]) if r[3] else {}
            except ValueError:
                detail = {}
            detail = detail if isinstance(detail, dict) else {}
            host = detail.get("host") or detail.get("hostname")
            if not host and (detail.get("agent") or detail.get("agent_id")):
                host = host_of_agent(detail.get("agent") or detail.get("agent_id"))
            out.append({"ts": _epoch(r[0]) or 0.0, "action": r[1], "target": r[2], "host": host or None})
        return out

    @_guard("catalog", list)
    def _catalog():
        return list(catalog() or [])

    @_guard("host_mem", dict)
    def _host_mem():
        out = {}
        for host, block in (host_samples() or {}).items():
            ram = _num(((block or {}).get("ram") or {}).get("total_bytes")) or 0.0
            vram = _num(((block or {}).get("gpu") or {}).get("vram_total_bytes")) or 0.0
            out[str(host)] = {"ram_gb": ram / 1e9, "vram_gb": vram / 1e9}
        return out

    @_guard("agents", list)
    def _agents():
        return [{"host": r.get("host"), "version": r.get("version"), "last_seen": r.get("last_seen")}
                for r in (agent_rows() or []) if isinstance(r, dict) and r.get("host")]

    @_guard("latest_agent_version", str)
    def _latest():
        return str(latest_agent_version() or "")

    @_guard("price_kwh", float)
    def _price():
        return float(price_kwh() or 0.0)

    @_guard("findings", list)
    def _findings():
        return list(findings() or [])

    return {"series": _series, "names": _names, "hosts": _hosts, "mounts": _mounts, "alerts": _alerts,
            "energy": _energy, "bench_runs": _bench_runs, "report_cards": _report_cards, "audit": _audit,
            "catalog": _catalog, "host_mem": _host_mem, "agents": _agents,
            "latest_agent_version": _latest, "price_kwh": _price, "findings": _findings}


def tz_offset_s(now: Optional[float] = None) -> float:
    """Seconds east of UTC for the local zone right now (DST aware)."""
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts).astimezone().utcoffset().total_seconds()
