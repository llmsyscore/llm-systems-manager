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
# Per-agent accumulator state is dropped after this long without a fresh
# sample; counter baselines re-seed if the agent returns (#621).
AGENT_EVICT_S = 24 * 3600.0
PRUNE_INTERVAL_S = 3600.0

PROVIDERS = ("llama", "vllm", "lms")
# Live buckets also carry the system-only push (#1041); energy accounting reads PROVIDERS only.
LIVE_PROVIDERS = PROVIDERS + ("system",)

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


# LMS ps status substrings that count as inference activity; transitional
# states (LOADING/UNLOADING/DOWNLOADING) are not busy (#619).
LMS_BUSY_MARKERS = ("PROMPT", "STREAM", "GENERAT", "PREDICT", "QUEUE")


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
        st = (row.get("status") or "") if isinstance(row, dict) else ""
        if any(m in st for m in LMS_BUSY_MARKERS):
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


NO_MODEL = "(no model)"


def _llama_models(sample: dict) -> "list[str]":
    """Resident llama models: residency block first, legacy state+model pair second."""
    llama = (sample or {}).get("llama") or {}
    res = llama.get("residency")
    if isinstance(res, dict) and res.get("aggregate"):
        return [str(m["model_id"]) for m in res.get("models") or []
                if isinstance(m, dict) and m.get("model_id") and m.get("provider") == "llama"
                and m.get("status") in ("loaded", "sleeping", "loading")]
    if llama.get("state") not in ("awake", "sleeping"):
        return []
    raw = llama.get("model")
    if not isinstance(raw, str) or raw.endswith(" (unloaded)"):
        return []
    m = raw.replace(" (sleeping)", "").strip()
    return [m] if m else []


def extract_models(sample: dict) -> dict:
    """{provider: [model ids]} resident in one sample; STOPPED LMS rows are unloaded."""
    s = sample or {}
    out: dict = {}
    llama = _llama_models(s)
    if llama:
        out["llama"] = llama
    v = s.get("vllm") or {}
    if v.get("state") == "running" and v.get("model"):
        out["vllm"] = [str(v["model"])]
    lms = [str(p.get("model")) for p in (s.get("ps") or []) if isinstance(p, dict)
           and p.get("model") and str(p.get("status") or "").upper() != "STOPPED"]
    if lms:
        out["lms"] = lms
    return out


def model_key(model_id) -> str:
    """Case-folded id without an LM Studio @quant suffix, for matching only."""
    return str(model_id or "").split("@", 1)[0].strip().lower()


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
    # Per-model split of each host row (#991); sums per (hour, agent) equal the host row.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_model_hourly (
            hour_ts INTEGER NOT NULL,
            agent_id TEXT NOT NULL,
            model TEXT NOT NULL,
            hostname TEXT,
            resident_s REAL NOT NULL DEFAULT 0,
            observed_s REAL NOT NULL DEFAULT 0,
            active_s REAL NOT NULL DEFAULT 0,
            power_s REAL NOT NULL DEFAULT 0,
            energy_wh REAL NOT NULL DEFAULT 0,
            active_energy_wh REAL NOT NULL DEFAULT 0,
            tokens_gen INTEGER NOT NULL DEFAULT 0,
            tokens_prompt INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (hour_ts, agent_id, model)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_energy_model_hourly_ts "
                 "ON energy_model_hourly(hour_ts)")
    conn.commit()


_UPSERT_SQL = """
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
        """


_MODEL_COLS = ("hour_ts, agent_id, model, hostname, resident_s, observed_s, active_s, power_s, "
               "energy_wh, active_energy_wh, tokens_gen, tokens_prompt")

_MODEL_UPSERT_SQL = """
        INSERT INTO energy_model_hourly (hour_ts, agent_id, model, hostname,
            resident_s, observed_s, active_s, power_s, energy_wh, active_energy_wh,
            tokens_gen, tokens_prompt)
        VALUES (:hour_ts, :agent_id, :model, :hostname, :resident_s, :observed_s, :active_s,
                :power_s, :energy_wh, :active_energy_wh, :tokens_gen, :tokens_prompt)
        ON CONFLICT(hour_ts, agent_id, model) DO UPDATE SET
            hostname = COALESCE(excluded.hostname, hostname),
            resident_s = resident_s + excluded.resident_s,
            observed_s = observed_s + excluded.observed_s,
            active_s = active_s + excluded.active_s,
            power_s = power_s + excluded.power_s,
            energy_wh = energy_wh + excluded.energy_wh,
            active_energy_wh = active_energy_wh + excluded.active_energy_wh,
            tokens_gen = tokens_gen + excluded.tokens_gen,
            tokens_prompt = tokens_prompt + excluded.tokens_prompt
        """


def _write_increment(conn, inc: dict) -> None:
    conn.execute(_UPSERT_SQL, inc)
    for m in inc.get("models") or []:
        conn.execute(_MODEL_UPSERT_SQL, {
            "hour_ts": inc["hour_ts"], "agent_id": inc["agent_id"],
            "hostname": inc.get("hostname"), **m})


def upsert_increment(conn, inc: dict) -> None:
    _write_increment(conn, inc)
    conn.commit()


def upsert_increments(conn, incs: "list[dict]") -> None:
    """All of one tick's rows under a single commit (#621)."""
    for inc in incs:
        _write_increment(conn, inc)
    conn.commit()


def prune(conn, retention_days: float, now: "float | None" = None) -> int:
    """Delete hourly rows older than retention_days; returns host rows deleted (#620)."""
    now = _time.time() if now is None else now
    cutoff = int(now - retention_days * 86400)
    cur = conn.execute("DELETE FROM energy_hourly WHERE hour_ts < ?", (cutoff,))
    conn.execute("DELETE FROM energy_model_hourly WHERE hour_ts < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def query_rows(conn, start_ts: int, end_ts: int) -> "list[dict]":
    cols = [c.strip() for c in _COLS.split(",")]
    rows = conn.execute(
        f"SELECT {_COLS} FROM energy_hourly WHERE hour_ts >= ? AND hour_ts < ?"
        " ORDER BY hour_ts ASC", (int(start_ts), int(end_ts))).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def query_model_rows(conn, start_ts: int, end_ts: int) -> "list[dict]":
    cols = [c.strip() for c in _MODEL_COLS.split(",")]
    rows = conn.execute(
        f"SELECT {_MODEL_COLS} FROM energy_model_hourly WHERE hour_ts >= ? AND hour_ts < ?"
        " ORDER BY hour_ts ASC", (int(start_ts), int(end_ts))).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def first_ts(conn) -> "int | None":
    row = conn.execute("SELECT MIN(hour_ts) FROM energy_hourly").fetchone()
    return int(row[0]) if row and row[0] is not None else None



HOST_PEAK_DAYS = 30


ACTIVE_MIN_S = 60.0


def host_peak(rows: "list[dict]", agent_id: str) -> dict:
    """Highest hourly average watts and highest under-load watts for one agent."""
    peak, hours, since = None, 0, None
    active_peak, active_hours = None, 0
    for r in rows:
        if r.get("agent_id") != agent_id or float(r.get("power_s") or 0) <= 0:
            continue
        w = float(r.get("energy_wh") or 0) / (float(r["power_s"]) / 3600.0)
        hours += 1
        peak = w if peak is None or w > peak else peak
        since = int(r["hour_ts"]) if since is None or int(r["hour_ts"]) < since else since
        active_s = float(r.get("active_s") or 0)
        if active_s >= ACTIVE_MIN_S:
            aw = float(r.get("active_energy_wh") or 0) / (active_s / 3600.0)
            active_hours += 1
            active_peak = aw if active_peak is None or aw > active_peak else active_peak
    return {"peak_w": round(peak, 1) if peak is not None else None, "hours": hours, "since": since,
            "peak_active_w": round(active_peak, 1) if active_peak is not None else None,
            "active_hours": active_hours}


# ── Accumulator ──────────────────────────────────────────────────────


class Accumulator:
    """Turns periodic store_view() snapshots ({agent_id: {provider:
    (sample, last_seen)}}) into hourly increments, persisted via one
    sink(incs) call per tick. usage_view() optionally supplies gateway
    cumulative token counters ({agent_id: {"gen": N, "prompt": N}})."""

    def __init__(self, store_view, sink, usage_view=None, usage_models_view=None):
        self._store_view = store_view
        self._sink = sink
        self._usage_view = usage_view
        # {agent_id: {model: {"gen": N, "prompt": N}}}; when given it also supplies the gateway host total.
        self._usage_models_view = usage_models_view
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
        if out:
            # One sink call per tick: the DB sink commits all rows at once (#621).
            try:
                self._sink(out)
            except Exception as e:
                log.warning("energy: persist failed: %s", e)
        self._evict(now)
        return out

    def _evict(self, now: float) -> None:
        """Drop per-agent state with no fresh sample for AGENT_EVICT_S (#621)."""
        stale = [aid for aid, st in self._agents.items()
                 if (now - (st.get("last") or 0)) > AGENT_EVICT_S]
        for aid in stale:
            del self._agents[aid]

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
        # Gateway counters per model (#991) replace the per-agent total when present.
        gw_models = self._gateway_models(agent_id)
        if gw_models is not None:
            for model, cur in gw_models.items():
                merged[f"gateway:{model}"] = {"gen": cur.get("gen"), "prompt": cur.get("prompt")}
        elif self._usage_view is not None:
            try:
                u = (self._usage_view() or {}).get(agent_id)
            except Exception as e:
                log.debug("energy: usage view failed: %s", e)
                u = None
            if isinstance(u, dict):
                merged["gateway"] = {"gen": u.get("gen"),
                                     "prompt": u.get("prompt")}

        tokens_gen = tokens_prompt = 0
        deltas: dict = {}
        for key, cur in merged.items():
            cst = st["counters"].setdefault(key, {"gen": None, "prompt": None})
            d_gen, cst["gen"] = counter_delta(cst["gen"], cur["gen"])
            d_prompt, cst["prompt"] = counter_delta(cst["prompt"], cur["prompt"])
            deltas[key] = (d_gen, d_prompt)
            tokens_gen += d_gen
            tokens_prompt += d_prompt

        if dt <= 0 and not tokens_gen and not tokens_prompt:
            return None
        wh = (watts * dt / 3600.0) if watts is not None else 0.0
        inc = {
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
        resident: dict = {}
        for sample, _ls in ordered:
            for prov, models in extract_models(sample).items():
                resident.setdefault(prov, [])
                resident[prov] += [m for m in models if m not in resident[prov]]
        inc["models"] = split_models(inc, resident, deltas)
        return inc

    def _gateway_models(self, agent_id: str) -> "dict | None":
        if self._usage_models_view is None:
            return None
        try:
            u = (self._usage_models_view() or {}).get(agent_id)
        except Exception as e:
            log.debug("energy: usage models view failed: %s", e)
            return None
        return u if isinstance(u, dict) else {}


def _match_model(model_id: str, resident: "list[str]") -> str:
    key = model_key(model_id)
    return next((m for m in resident if model_key(m) == key), model_id)


def split_models(inc: dict, resident: dict, deltas: dict) -> "list[dict]":
    """Per-model share of one host increment: token deltas go to the block's
    resident model(s); time and energy follow tokens, or split evenly when idle.
    resident_s is the whole tick for every model that was resident or served."""
    tokens: dict = {}

    def _add(model, gen, prompt):
        t = tokens.setdefault(model, [0, 0])
        t[0] += gen
        t[1] += prompt

    lms = resident.get("lms") or []
    for key, (d_gen, d_prompt) in deltas.items():
        if not d_gen and not d_prompt:
            continue
        if key.startswith("gateway:") and key[8:]:
            targets = [_match_model(key[8:], lms)]
        elif key in ("gateway", "gateway:"):
            targets = lms or [NO_MODEL]
        else:
            targets = resident.get(key) or [NO_MODEL]
        n = len(targets)
        for i, m in enumerate(targets):
            # Integer split; the first target takes the remainder.
            g, p = d_gen // n, d_prompt // n
            if i == 0:
                g, p = g + d_gen % n, p + d_prompt % n
            _add(m, g, p)
    models = [m for ms in resident.values() for m in ms]
    for m in tokens:
        if m not in models:
            models.append(m)
    if not models:
        models = [NO_MODEL]
    total = sum(g + p for g, p in tokens.values())
    if total > 0:
        weights = {m: (sum(tokens.get(m, (0, 0))) / total) for m in models}
    else:
        weights = {m: 1.0 / len(models) for m in models}
    out = []
    for m in models:
        w = weights[m]
        g, p = tokens.get(m, (0, 0))
        out.append({"model": m,
                    "resident_s": inc["observed_s"],
                    "observed_s": inc["observed_s"] * w,
                    "active_s": inc["active_s"] * w,
                    "power_s": inc["power_s"] * w,
                    "energy_wh": inc["energy_wh"] * w,
                    "active_energy_wh": inc["active_energy_wh"] * w,
                    "tokens_gen": g, "tokens_prompt": p})
    return out


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
        # usd_per_mtok divides by generated tokens only; cloud_cost_usd
        # prices prompt+gen at their separate rates (#618).
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
    # Savings: cloud list price for matched hosts' tokens vs the full local
    # bill — a token-only host with no power sample can't inflate it (#617).
    local_cost = totals["cost_usd"]
    savings = None
    if (totals["has_tokens"] and local_cost is not None
            and cov is not None and cov >= 95.0):
        m_prompt = sum(a["tokens_prompt"] for a in matched)
        m_cloud = (m_prompt / 1e6) * cloud_in + (m_gen / 1e6) * cloud_out
        savings = round(m_cloud - local_cost, 2)
    return {"totals": totals, "hosts": hosts,
            "savings_usd": savings}


def summarize_models(rows: "list[dict]", window_s: float, price_kwh: float,
                     cloud_in: float, cloud_out: float) -> "list[dict]":
    """Per-model rollup of energy_model_hourly rows (#991), largest energy first."""
    per_model: dict = {}
    hosts: dict = {}
    resident: dict = {}
    total_wh = 0.0
    for row in rows:
        m = str(row.get("model") or NO_MODEL)
        _fold(per_model.setdefault(m, _agg_zero()), row)
        resident[m] = resident.get(m, 0.0) + float(row.get("resident_s") or 0)
        total_wh += float(row.get("energy_wh") or 0)
        name = row.get("hostname") or str(row.get("agent_id") or "")[:8]
        if name and name not in hosts.setdefault(m, []):
            hosts[m].append(name)
    out = []
    for m, agg in per_model.items():
        d = _derive(agg, window_s, price_kwh, cloud_in, cloud_out)
        d.pop("power_source", None)
        d.pop("coverage_pct", None)
        d.pop("power_coverage_pct", None)
        # Whole-tick residency over the elapsed window per host, like fleet coverage.
        span = window_s * max(1, len(hosts.get(m, [])))
        d["resident_pct"] = (round(100.0 * min(resident[m] / span, 1.0), 1)
                             if span > 0 else None)
        d["model"] = m
        d["hosts"] = hosts.get(m, [])
        d["energy_share_pct"] = (round(100.0 * agg["energy_wh"] / total_wh, 1)
                                 if total_wh > 0 else None)
        out.append(d)
    out.sort(key=lambda d: (d["kwh"] or 0.0, d["tokens_gen"]), reverse=True)
    return out


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


def _retention_days(ctx) -> "float | None":
    """energy.retention_days from config; None/0/invalid = keep forever."""
    en_cfg = getattr(getattr(getattr(ctx, "settings", None), "manager", None),
                     "energy", None)
    try:
        v = getattr(en_cfg, "retention_days", None)
        v = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    return v if v and v > 0 else None


def _float_arg(args, name: str, default: float) -> "float | None":
    """Query override; None signals a parse error."""
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _tz_offset_hours(args) -> "int | None":
    """?tz_offset_min=N (minutes east of UTC) snapped to whole hours, or None.
    Hour buckets are UTC-aligned, so half-hour zones resolve to the nearest."""
    raw = (args.get("tz_offset_min") or "").strip()
    if not raw:
        return None
    try:
        minutes = int(raw)
    except ValueError:
        return None
    if not -900 <= minutes <= 900:
        return None
    return int(round(minutes / 60.0))


def _window_from_args(args, now: float) -> "tuple[int, int, str] | None":
    """(start, end, label) from ?days=N / ?month=YYYY-MM (default: current
    UTC month); None on unparseable input. With ?tz_offset_min the days
    window ends at the caller's local midnight instead of the next UTC hour."""
    days_raw = (args.get("days") or "").strip()
    if days_raw:
        try:
            days = max(1, min(int(days_raw), 366))
        except ValueError:
            return None
        tz_h = _tz_offset_hours(args)
        if tz_h is None:
            end = int(now // 3600 + 1) * 3600
            return end - days * 86400, end, f"last {days} days"
        off = tz_h * 3600
        end = ((int(now) + off) // 86400 + 1) * 86400 - off
        label = "today (local)" if days == 1 else f"last {days} local days"
        return end - days * 86400, end, label
    ytd_raw = (args.get("ytd") or "").strip()
    if ytd_raw.lower() in ("1", "true", "yes"):
        off = (_tz_offset_hours(args) or 0) * 3600
        year = _time.gmtime(int(now) + off).tm_year
        start = calendar.timegm((year, 1, 1, 0, 0, 0)) - off
        return start, int(now // 3600 + 1) * 3600, f"YTD {year}"
    start_raw = (args.get("start") or "").strip()
    end_raw = (args.get("end") or "").strip()
    if start_raw or end_raw:
        try:
            s = _time.strptime(start_raw, "%Y-%m-%d")
            e = _time.strptime(end_raw, "%Y-%m-%d")
        except ValueError:
            return None
        off = (_tz_offset_hours(args) or 0) * 3600
        start = calendar.timegm(s[:6]) - off
        # The end date is inclusive: the window closes at its next midnight.
        end = calendar.timegm(e[:6]) + 86400 - off
        if end <= start or end - start > 366 * 86400:
            return None
        return start, end, f"{start_raw} → {end_raw}"
    month = (args.get("month") or "").strip() or current_month(now)
    try:
        start, end = month_bounds(month)
    except ValueError:
        return None
    return start, end, month


def store_view_from_provider_state(names: tuple = LIVE_PROVIDERS) -> dict:
    """{agent_id: {provider: (sample, last_seen)}} across the named providers (default: every live bucket)."""
    import provider_state
    out: dict = {}
    for prov in names:
        for aid, wrap in (provider_state.STORE.all_for(prov) or {}).items():
            sample = (wrap or {}).get("sample")
            last_seen = float((wrap or {}).get("last_seen") or 0)
            if isinstance(sample, dict):
                out.setdefault(aid, {})[prov] = (sample, last_seen)
    return out


def register_routes(app, ctx=None, db_path: "str | None" = None, primary_agent=None) -> None:
    """Mount /api/energy/* on the manager app."""
    global _conn_factory
    import sqlite3
    from flask import jsonify, request as flask_request

    path = db_path or str(Path(getattr(ctx, "data_dir", ".")) / "energy.db")
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
                            "error": "invalid window parameters"}), 400
        start, end, label = window
        # Elapsed window only, so coverage isn't diluted by the future
        # hours of a month in progress.
        window_s = max(0.0, min(float(end), now) - start)
        conn = _conn_factory()
        rows = query_rows(conn, start, end)
        summary = summarize(rows, window_s, price, cloud_in, cloud_out)
        summary["models"] = summarize_models(query_model_rows(conn, start, end),
                                             window_s, price, cloud_in, cloud_out)
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
        # ?days=/?month=/?ytd=/?start+end mirror the summary window; bare
        # ?hours= (default 168) keeps the trailing-window form.
        truncated = False
        if any((args.get(k) or "").strip()
               for k in ("days", "month", "ytd", "start", "end")):
            window = _window_from_args(args, now)
            if window is None:
                return jsonify({"ok": False,
                                "error": "invalid window parameters"}), 400
            start, end, label = window
            floor = end - _HOURLY_MAX_H * 3600
            truncated = start < floor
            start = max(start, floor)
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
        return jsonify({"ok": True, "label": label, "truncated": truncated,
                        "cap_days": _HOURLY_MAX_H // 24,
                        "hours": int((end - start) // 3600),
                        "start_ts": start, "end_ts": end, "rows": out})

    @app.route("/api/energy/host-peak")
    def energy_host_peak():
        agent = (flask_request.args.get("agent_id") or "").strip()
        if not agent and primary_agent is not None:
            agent = str((primary_agent() or {}).get("agent_id") or "")
        if not agent:
            return jsonify({"ok": False, "error": "agent_id required"}), 400
        now = _time.time()
        end = int(now // 3600 + 1) * 3600
        start = end - HOST_PEAK_DAYS * 86400
        rows = query_rows(_conn_factory(), start, end)
        return jsonify({"ok": True, "agent_id": agent, "days": HOST_PEAK_DAYS, **host_peak(rows, agent)})


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
    _ACCUM = Accumulator(lambda: store_view_from_provider_state(PROVIDERS),
                         lambda incs: upsert_increments(_conn_factory(), incs),
                         usage_view=gateway_usage.counters,
                         usage_models_view=gateway_usage.model_counters)

    def _loop():
        last_prune = 0.0
        while True:
            try:
                _ACCUM.tick()
            except Exception as e:
                log.warning("energy accumulator tick failed: %s", e)
            try:
                days = _retention_days(ctx)
                if days and (_time.time() - last_prune) >= PRUNE_INTERVAL_S:
                    last_prune = _time.time()
                    n = prune(_conn_factory(), days)
                    if n:
                        log.info("energy: pruned %d hourly rows older than "
                                 "%g days", n, days)
            except Exception as e:
                log.warning("energy: prune failed: %s", e)
            _time.sleep(TICK_S)

    _threading.Thread(target=_loop, name="energy-accumulator",
                      daemon=True).start()
