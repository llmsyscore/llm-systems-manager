"""Energy & cost intelligence (#470): fleet energy accounting, measured
$/Mtok, idle/active split, and a monthly cloud-savings summary."""
from __future__ import annotations

import calendar
import logging
import sys
import threading as _threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("llm-systems-manager.energy")

# Accumulator cadence and sample-freshness bounds (seconds).
TICK_S = 10.0
FRESH_S = 90.0
MAX_GAP_S = 120.0

PROVIDERS = ("llama", "vllm", "lms")

# Cloud list-price defaults for the savings estimate ($ per Mtok).
# Config [manager.energy] overrides; the UI can override per request.
CLOUD_PRICE_IN_DEFAULT = 0.15
CLOUD_PRICE_OUT_DEFAULT = 0.60
CLOUD_PRICE_LABEL_DEFAULT = "budget cloud API tier (GPT-4o-mini class, 2026-07 list)"

POWER_SOURCE_LABEL = {"psu": "wall", "mac": "SoC", "gpu": "GPU"}


# ── Sample extraction (pure) ─────────────────────────────────────────
# llama STORE samples are flat; vllm/lms embed system under "system".


def _sys_block(sample: dict) -> dict:
    sysb = sample.get("system")
    return sysb if isinstance(sysb, dict) and sysb else sample


def extract_power(sample: dict) -> "tuple[float | None, str | None]":
    """(watts, source) for one sample: PSU wall > Apple SoC > GPU."""
    sysb = _sys_block(sample or {})
    psu = ((sysb.get("liquidctl") or {}).get("psu") or {})
    est = psu.get("Estimated input power")
    if isinstance(est, dict) and isinstance(est.get("value"), (int, float)):
        return float(est["value"]), "psu"
    mac = (sample or {}).get("mac_power") or sysb.get("mac_power") or {}
    v = mac.get("soc_total_w") if isinstance(mac, dict) else None
    if isinstance(v, (int, float)):
        return float(v), "mac"
    gpu = sysb.get("gpu") or {}
    v = gpu.get("power_watts")
    if isinstance(v, (int, float)):
        return float(v), "gpu"
    return None, None


def extract_busy(sample: dict) -> bool:
    """True when the sample shows inference activity on any provider block."""
    s = sample or {}
    for key, gauges in (("llama", ("requests_processing", "tokens_per_second")),
                        ("vllm", ("requests_running", "tokens_per_second"))):
        blk = s.get(key) or {}
        for g in gauges:
            v = blk.get(g)
            if isinstance(v, (int, float)) and v > 0:
                return True
    for row in s.get("ps") or []:
        if isinstance(row, dict) and (row.get("status") or "") not in (
                "IDLE", "STOPPED", ""):
            return True
    return False


def extract_counters(sample: dict) -> dict:
    """Cumulative token counters per provider block; {} without telemetry."""
    out: dict = {}
    for key in ("llama", "vllm"):
        blk = (sample or {}).get(key) or {}
        gen = blk.get("total_tokens_generated")
        prompt = blk.get("total_tokens_prompted")
        if isinstance(gen, (int, float)) or isinstance(prompt, (int, float)):
            out[key] = {
                "gen": int(gen) if isinstance(gen, (int, float)) else None,
                "prompt": int(prompt) if isinstance(prompt, (int, float)) else None,
            }
    return out


def counter_delta(last: "int | None", cur: "int | None") -> "tuple[int, int | None]":
    """(tokens_added, new_baseline). None freezes; a decrease is a restart."""
    if cur is None:
        return 0, last
    if last is None:
        return 0, cur
    if cur >= last:
        return cur - last, cur
    return cur, cur


def extract_hostname(sample: dict) -> "str | None":
    s = sample or {}
    host = s.get("host") or _sys_block(s).get("host")
    if host:
        return str(host)
    hw = s.get("hardware") or {}
    return str(hw["name"]) if isinstance(hw, dict) and hw.get("name") else None


# ── Storage ──────────────────────────────────────────────────────────
# One row per (hour, agent); counters accumulate via UPSERT.

_COLS = ("hour_ts, agent_id, hostname, observed_s, active_s, power_s, "
         "energy_wh, active_energy_wh, tokens_gen, tokens_prompt, "
         "power_source, samples")


def init_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_hourly (
            hour_ts INTEGER NOT NULL,
            agent_id TEXT NOT NULL,
            hostname TEXT,
            observed_s REAL NOT NULL DEFAULT 0,
            active_s REAL NOT NULL DEFAULT 0,
            power_s REAL NOT NULL DEFAULT 0,
            energy_wh REAL NOT NULL DEFAULT 0,
            active_energy_wh REAL NOT NULL DEFAULT 0,
            tokens_gen INTEGER NOT NULL DEFAULT 0,
            tokens_prompt INTEGER NOT NULL DEFAULT 0,
            power_source TEXT,
            samples INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (hour_ts, agent_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_energy_hourly_ts "
                 "ON energy_hourly(hour_ts)")
    conn.commit()


def upsert_increment(conn, inc: dict) -> None:
    conn.execute("""
        INSERT INTO energy_hourly (hour_ts, agent_id, hostname, observed_s,
            active_s, power_s, energy_wh, active_energy_wh, tokens_gen,
            tokens_prompt, power_source, samples)
        VALUES (:hour_ts, :agent_id, :hostname, :observed_s, :active_s,
                :power_s, :energy_wh, :active_energy_wh, :tokens_gen,
                :tokens_prompt, :power_source, 1)
        ON CONFLICT(hour_ts, agent_id) DO UPDATE SET
            hostname = COALESCE(excluded.hostname, hostname),
            observed_s = observed_s + excluded.observed_s,
            active_s = active_s + excluded.active_s,
            power_s = power_s + excluded.power_s,
            energy_wh = energy_wh + excluded.energy_wh,
            active_energy_wh = active_energy_wh + excluded.active_energy_wh,
            tokens_gen = tokens_gen + excluded.tokens_gen,
            tokens_prompt = tokens_prompt + excluded.tokens_prompt,
            power_source = COALESCE(excluded.power_source, power_source),
            samples = samples + 1
        """, inc)
    conn.commit()


def query_rows(conn, start_ts: int, end_ts: int) -> "list[dict]":
    cols = [c.strip() for c in _COLS.split(",")]
    rows = conn.execute(
        f"SELECT {_COLS} FROM energy_hourly WHERE hour_ts >= ? AND hour_ts < ?"
        " ORDER BY hour_ts ASC", (int(start_ts), int(end_ts))).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def first_ts(conn) -> "int | None":
    row = conn.execute("SELECT MIN(hour_ts) FROM energy_hourly").fetchone()
    return int(row[0]) if row and row[0] is not None else None


# ── Accumulator ──────────────────────────────────────────────────────


class Accumulator:
    """Turns periodic store_view() snapshots ({agent_id: {provider:
    (sample, last_seen)}}) into hourly increments, persisted via sink(inc).
    usage_view() optionally supplies gateway-observed cumulative token
    counters ({agent_id: {"gen": N, "prompt": N}}) as an extra source."""

    def __init__(self, store_view, sink, usage_view=None):
        self._store_view = store_view
        self._sink = sink
        self._usage_view = usage_view
        self._agents: dict = {}

    def tick(self, now: "float | None" = None) -> "list[dict]":
        now = _time.time() if now is None else now
        out: list = []
        try:
            view = self._store_view() or {}
        except Exception as e:
            log.warning("energy: store view failed: %s", e)
            return out
        for agent_id, buckets in view.items():
            try:
                inc = self._tick_agent(agent_id, buckets or {}, now)
            except Exception as e:
                log.warning("energy: tick failed for agent %s: %s",
                            str(agent_id)[:8], e)
                continue
            if inc:
                out.append(inc)
                try:
                    self._sink(inc)
                except Exception as e:
                    log.warning("energy: persist failed: %s", e)
        return out

    def _tick_agent(self, agent_id: str, buckets: dict,
                    now: float) -> "dict | None":
        fresh = {p: (s, ls) for p, (s, ls) in buckets.items()
                 if isinstance(s, dict) and (now - ls) <= FRESH_S}
        if not fresh:
            return None
        st = self._agents.setdefault(agent_id, {"last": None, "counters": {}})
        last = st["last"]
        st["last"] = now
        dt = 0.0 if last is None else max(0.0, min(now - last, MAX_GAP_S))

        # Power/hostname come from the freshest bucket that reports power;
        # busy/counters merge across fresh buckets + gateway usage_view.
        ordered = sorted(fresh.values(), key=lambda t: t[1], reverse=True)
        watts, source = None, None
        for sample, _ls in ordered:
            watts, source = extract_power(sample)
            if watts is not None:
                break
        hostname = next((extract_hostname(s) for s, _ in ordered
                         if extract_hostname(s)), None)
        busy = any(extract_busy(s) for s, _ in fresh.values())

        # Merge duplicate counters across buckets by max per key.
        merged: dict = {}
        for _prov, (sample, _ls) in fresh.items():
            for key, cur in extract_counters(sample).items():
                m = merged.setdefault(key, {"gen": None, "prompt": None})
                for f in ("gen", "prompt"):
                    v = cur[f]
                    if v is not None and (m[f] is None or v > m[f]):
                        m[f] = v
        if self._usage_view is not None:
            try:
                u = (self._usage_view() or {}).get(agent_id)
            except Exception as e:
                log.debug("energy: usage view failed: %s", e)
                u = None
            if isinstance(u, dict):
                merged["gateway"] = {"gen": u.get("gen"),
                                     "prompt": u.get("prompt")}

        tokens_gen = tokens_prompt = 0
        for key, cur in merged.items():
            cst = st["counters"].setdefault(key, {"gen": None, "prompt": None})
            d_gen, cst["gen"] = counter_delta(cst["gen"], cur["gen"])
            d_prompt, cst["prompt"] = counter_delta(cst["prompt"], cur["prompt"])
            tokens_gen += d_gen
            tokens_prompt += d_prompt

        if dt <= 0 and not tokens_gen and not tokens_prompt:
            return None
        wh = (watts * dt / 3600.0) if watts is not None else 0.0
        return {
            "hour_ts": int(now // 3600) * 3600,
            "agent_id": agent_id,
            "hostname": hostname,
            "observed_s": dt,
            "active_s": dt if busy else 0.0,
            "power_s": dt if watts is not None else 0.0,
            "energy_wh": wh,
            "active_energy_wh": wh if busy else 0.0,
            "tokens_gen": tokens_gen,
            "tokens_prompt": tokens_prompt,
            "power_source": source,
        }


# ── Summary math (pure) ──────────────────────────────────────────────


def _agg_zero() -> dict:
    return {"observed_s": 0.0, "active_s": 0.0, "power_s": 0.0,
            "energy_wh": 0.0, "active_energy_wh": 0.0,
            "tokens_gen": 0, "tokens_prompt": 0,
            "hostname": None, "power_source": None}


def _fold(agg: dict, row: dict) -> None:
    for k in ("observed_s", "active_s", "power_s", "energy_wh",
              "active_energy_wh"):
        agg[k] += float(row.get(k) or 0)
    for k in ("tokens_gen", "tokens_prompt"):
        agg[k] += int(row.get(k) or 0)
    agg["hostname"] = row.get("hostname") or agg["hostname"]
    agg["power_source"] = row.get("power_source") or agg["power_source"]


def _derive(agg: dict, window_s: float, price_kwh: float,
            cloud_in: float, cloud_out: float) -> dict:
    has_power = agg["power_s"] > 0
    kwh = agg["energy_wh"] / 1000.0 if has_power else None
    active_kwh = agg["active_energy_wh"] / 1000.0 if has_power else None
    idle_kwh = (kwh - active_kwh) if has_power else None
    cost = kwh * price_kwh if has_power else None
    active_cost = active_kwh * price_kwh if has_power else None
    gen, prompt = agg["tokens_gen"], agg["tokens_prompt"]
    cloud_cost = (prompt / 1e6) * cloud_in + (gen / 1e6) * cloud_out
    out = {
        "observed_s": round(agg["observed_s"], 1),
        "active_s": round(agg["active_s"], 1),
        "active_pct": (round(100.0 * agg["active_s"] / agg["observed_s"], 1)
                       if agg["observed_s"] > 0 else None),
        "coverage_pct": (round(100.0 * min(agg["observed_s"] / window_s, 1.0), 1)
                         if window_s > 0 else None),
        "power_coverage_pct": (round(100.0 * min(agg["power_s"]
                                                 / agg["observed_s"], 1.0), 1)
                               if agg["observed_s"] > 0 else None),
        "kwh": None if kwh is None else round(kwh, 3),
        "active_kwh": None if active_kwh is None else round(active_kwh, 3),
        "idle_kwh": None if idle_kwh is None else round(idle_kwh, 3),
        "avg_watts": (round(agg["energy_wh"] * 3600.0 / agg["power_s"], 1)
                      if has_power else None),
        "cost_usd": None if cost is None else round(cost, 2),
        "idle_cost_usd": (None if cost is None
                          else round(cost - active_cost, 2)),
        "tokens_gen": gen,
        "tokens_prompt": prompt,
        "usd_per_mtok": (round(cost / gen * 1e6, 4)
                         if cost is not None and gen > 0 else None),
        "usd_per_mtok_active": (round(active_cost / gen * 1e6, 4)
                                if active_cost is not None and gen > 0
                                else None),
        "cloud_cost_usd": round(cloud_cost, 2),
        "power_source": agg["power_source"],
        "has_power": has_power,
        "has_tokens": (gen + prompt) > 0,
    }
    return out


def summarize(rows: "list[dict]", window_s: float, price_kwh: float,
              cloud_in: float, cloud_out: float) -> dict:
    """Fleet totals + per-host breakdown + savings from hourly rows."""
    per_agent: dict = {}
    total = _agg_zero()
    for row in rows:
        agg = per_agent.setdefault(row["agent_id"], _agg_zero())
        _fold(agg, row)
        _fold(total, row)
    # Fleet coverage divides summed observed_s by window × host count, so a
    # partially-observed host lowers it instead of healthy hosts saturating it.
    totals = _derive(total, window_s * max(1, len(per_agent)), price_kwh,
                     cloud_in, cloud_out)
    hosts = []
    for aid, agg in per_agent.items():
        h = _derive(agg, window_s, price_kwh, cloud_in, cloud_out)
        h["agent_id"] = aid
        h["hostname"] = agg["hostname"]
        hosts.append(h)
    hosts.sort(key=lambda h: (h["kwh"] or 0.0), reverse=True)
    # Fleet $/Mtok covers matched hosts only: those reporting both
    # power and tokens.
    matched = [a for a in per_agent.values()
               if a["power_s"] > 0 and (a["tokens_gen"] + a["tokens_prompt"]) > 0]
    m_wh = sum(a["energy_wh"] for a in matched)
    m_active_wh = sum(a["active_energy_wh"] for a in matched)
    m_gen = sum(a["tokens_gen"] for a in matched)
    cov = (round(100.0 * m_wh / total["energy_wh"], 1)
           if total["energy_wh"] > 0 else None)
    totals["mtok_energy_coverage_pct"] = cov
    if matched and m_gen > 0:
        totals["usd_per_mtok"] = round(m_wh / 1000.0 * price_kwh
                                       / m_gen * 1e6, 4)
        totals["usd_per_mtok_active"] = round(m_active_wh / 1000.0 * price_kwh
                                              / m_gen * 1e6, 4)
    else:
        totals["usd_per_mtok"] = None
        totals["usd_per_mtok_active"] = None
    # Savings: cloud list price for served tokens vs the full local bill
    # (idle included); rendered only at >= 95% matched-energy coverage.
    local_cost = totals["cost_usd"]
    savings = None
    if (totals["has_tokens"] and local_cost is not None
            and cov is not None and cov >= 95.0):
        savings = round(totals["cloud_cost_usd"] - local_cost, 2)
    return {"totals": totals, "hosts": hosts,
            "savings_usd": savings}


def month_bounds(month: str, now: "float | None" = None) -> "tuple[int, int]":
    """(start, end) epoch for a UTC calendar month 'YYYY-MM'."""
    dt = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    start = int(dt.timestamp())
    days = calendar.monthrange(dt.year, dt.month)[1]
    return start, start + days * 86400


def current_month(now: "float | None" = None) -> str:
    now = _time.time() if now is None else now
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m")


# ── Config + routes ──────────────────────────────────────────────────

_conn_factory = None
_ACCUM: "Accumulator | None" = None

_HOURLY_MAX_H = 24 * 45


def _cfg_energy(ctx) -> dict:
    """Config with getattr guards — unified_config.py is deployment-local
    and may predate [manager.energy]."""
    manager = getattr(getattr(ctx, "settings", None), "manager", None)
    en = getattr(manager, "energy", None)
    price = getattr(en, "price_kwh", None)
    if price is None:
        price = getattr(getattr(manager, "reportcard", None), "price_kwh", None)
    try:
        price = float(price) if price is not None else 0.15
    except (TypeError, ValueError):
        price = 0.15

    def _num(name, default):
        try:
            v = getattr(en, name, None)
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    label = getattr(en, "cloud_price_label", None) or CLOUD_PRICE_LABEL_DEFAULT
    return {"price_kwh": price,
            "cloud_price_in_per_mtok": _num("cloud_price_in_per_mtok",
                                            CLOUD_PRICE_IN_DEFAULT),
            "cloud_price_out_per_mtok": _num("cloud_price_out_per_mtok",
                                             CLOUD_PRICE_OUT_DEFAULT),
            "cloud_price_label": str(label)}


def _float_arg(args, name: str, default: float) -> "float | None":
    """Query override; None signals a parse error."""
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _window_from_args(args, now: float) -> "tuple[int, int, str] | None":
    """(start, end, label) from ?days=N / ?month=YYYY-MM (default: current
    UTC month); None on unparseable input."""
    days_raw = (args.get("days") or "").strip()
    if days_raw:
        try:
            days = max(1, min(int(days_raw), 366))
        except ValueError:
            return None
        end = int(now // 3600 + 1) * 3600
        return end - days * 86400, end, f"last {days} days"
    month = (args.get("month") or "").strip() or current_month(now)
    try:
        start, end = month_bounds(month)
    except ValueError:
        return None
    return start, end, month


def store_view_from_provider_state() -> dict:
    """{agent_id: {provider: (sample, last_seen)}} across every provider."""
    import provider_state
    out: dict = {}
    for prov in PROVIDERS:
        for aid, wrap in (provider_state.STORE.all_for(prov) or {}).items():
            sample = (wrap or {}).get("sample")
            last_seen = float((wrap or {}).get("last_seen") or 0)
            if isinstance(sample, dict):
                out.setdefault(aid, {})[prov] = (sample, last_seen)
    return out


def register_routes(app, ctx=None, db_path: "str | None" = None) -> None:
    """Mount /api/energy/* on the manager app."""
    global _conn_factory
    import sqlite3
    from flask import jsonify, request as flask_request

    path = db_path or str(Path(getattr(ctx, "data_dir", ".")) / "metrics.db")
    tls = _threading.local()

    def conn_factory():
        conn = getattr(tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(path, timeout=30.0)
            conn.execute("PRAGMA busy_timeout=5000")
            tls.conn = conn
        return conn

    if _conn_factory is None:
        _conn_factory = conn_factory

    @app.route("/api/energy/summary")
    def energy_summary():
        args = flask_request.args
        cfg = _cfg_energy(ctx)
        price = _float_arg(args, "price_kwh", cfg["price_kwh"])
        cloud_in = _float_arg(args, "cloud_in", cfg["cloud_price_in_per_mtok"])
        cloud_out = _float_arg(args, "cloud_out",
                               cfg["cloud_price_out_per_mtok"])
        if price is None or cloud_in is None or cloud_out is None:
            return jsonify({"ok": False, "error": "invalid price override"}), 400
        now = _time.time()
        window = _window_from_args(args, now)
        if window is None:
            return jsonify({"ok": False,
                            "error": "invalid days / month (YYYY-MM)"}), 400
        start, end, label = window
        # Elapsed window only, so coverage isn't diluted by the future
        # hours of a month in progress.
        window_s = max(0.0, min(float(end), now) - start)
        conn = _conn_factory()
        rows = query_rows(conn, start, end)
        summary = summarize(rows, window_s, price, cloud_in, cloud_out)
        return jsonify({"ok": True,
                        "window": {"label": label, "start_ts": start,
                                   "end_ts": end,
                                   "elapsed_s": round(window_s, 0)},
                        "since_ts": first_ts(conn),
                        "config": cfg,
                        "price_kwh": price,
                        "cloud_in": cloud_in, "cloud_out": cloud_out,
                        **summary})

    @app.route("/api/energy/hourly")
    def energy_hourly():
        args = flask_request.args
        now = _time.time()
        # ?days=/?month= mirror the summary window; bare ?hours= (default
        # 168) keeps the trailing-window form.
        if (args.get("days") or "").strip() or (args.get("month") or "").strip():
            window = _window_from_args(args, now)
            if window is None:
                return jsonify({"ok": False,
                                "error": "invalid days / month (YYYY-MM)"}), 400
            start, end, label = window
            start = max(start, end - _HOURLY_MAX_H * 3600)
        else:
            try:
                hours = max(1, min(int(args.get("hours") or 168),
                                   _HOURLY_MAX_H))
            except ValueError:
                return jsonify({"ok": False, "error": "invalid hours"}), 400
            end = int(now // 3600 + 1) * 3600
            start = end - hours * 3600
            label = f"last {hours} hours"
        agent = (args.get("agent") or "").strip()
        rows = query_rows(_conn_factory(), start, end)
        if agent:
            rows = [r for r in rows if r["agent_id"] == agent]
        # Hour-bucket rollup across agents keeps the chart payload flat.
        by_hour: dict = {}
        for r in rows:
            b = by_hour.setdefault(r["hour_ts"], {
                "hour_ts": r["hour_ts"], "energy_wh": 0.0,
                "active_energy_wh": 0.0, "tokens_gen": 0,
                "tokens_prompt": 0, "observed_s": 0.0, "active_s": 0.0})
            for k in ("energy_wh", "active_energy_wh", "observed_s",
                      "active_s"):
                b[k] += float(r.get(k) or 0)
            for k in ("tokens_gen", "tokens_prompt"):
                b[k] += int(r.get(k) or 0)
        out = [dict(b, energy_wh=round(b["energy_wh"], 2),
                    active_energy_wh=round(b["active_energy_wh"], 2),
                    observed_s=round(b["observed_s"], 1),
                    active_s=round(b["active_s"], 1))
               for b in sorted(by_hour.values(), key=lambda b: b["hour_ts"])]
        return jsonify({"ok": True, "label": label,
                        "hours": int((end - start) // 3600),
                        "start_ts": start, "end_ts": end, "rows": out})


def start_thread(ctx=None) -> None:
    """Daemon accumulator ticking every TICK_S; exceptions logged, never
    raised. No-op under pytest (mirrors autopilot.start_thread)."""
    global _ACCUM
    if "pytest" in sys.modules:
        return
    if _conn_factory is None:
        log.warning("energy: register_routes must run before start_thread")
        return
    import gateway_usage
    _ACCUM = Accumulator(store_view_from_provider_state,
                         lambda inc: upsert_increment(_conn_factory(), inc),
                         usage_view=gateway_usage.counters)

    def _loop():
        while True:
            try:
                _ACCUM.tick()
            except Exception as e:
                log.warning("energy accumulator tick failed: %s", e)
            _time.sleep(TICK_S)

    _threading.Thread(target=_loop, name="energy-accumulator",
                      daemon=True).start()
