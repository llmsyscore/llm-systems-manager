"""Shared metric-flatten logic.

Both the in-process adapter and the new HTTP `/metrics/ingest` endpoint
need to take a raw metric dict (the dict produced by an agent's
collection cycle — e.g. {"cpu_total": 42.5, "gpu": {"temp_c": 78}, ...})
and yield (source, metric_name, value, unit) tuples that the alarm
engine stores as MetricPoints.

This module centralises that logic so agents push raw dicts and the
flatten happens once on the alarm-engine side.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional


# Top-level keys that are not metric values — never flatten these.
# `host` is read separately by callers; remaining keys are non-numeric
# annotations that the flatten loop already skips, but listing them
# explicitly avoids accidentally treating them as data.
SKIP_TOP_LEVEL: frozenset[str] = frozenset({"ts", "platform", "interval"})

# Path tuple → (source, metric_name, unit, transform_fn). Used so renames
# done over time stay backwards-compatible with rules that target the old
# names (e.g. `ram/usage_percent` instead of `ram/percent`).
_BYTES_TO_MBPS: Callable[[float], float] = lambda v: v / 1_048_576

ALIAS_TABLE: dict[
    tuple[str, ...],
    tuple[str, str, str, Optional[Callable[[float], float]]],
] = {
    ("cpu_total",):                       ("cpu",   "usage_percent",       "%",    None),
    ("cpu_temp_c",):                      ("cpu",   "temp_c",              "°C",   None),
    ("ram", "percent"):                   ("ram",   "usage_percent",       "%",    None),
    ("gpu", "gpu_util_percent"):          ("gpu",   "utilization_percent", "%",    None),
    ("gpu", "vram_usage_percent"):        ("gpu",   "vram_usage_percent",  "%",    None),
    ("gpu", "temperature_c"):             ("gpu",   "temp_c",              "°C",   None),
    ("gpu", "power_watts"):               ("gpu",   "power_watts",         "W",    None),
    ("net", "bytes_sent_per_sec"):        ("net",   "sent_mbps",           "Mbps", _BYTES_TO_MBPS),
    ("net", "bytes_recv_per_sec"):        ("net",   "recv_mbps",           "Mbps", _BYTES_TO_MBPS),
    ("disk_io", "read_bytes_per_sec"):    ("disk",  "read_mbps",           "Mbps", _BYTES_TO_MBPS),
    ("disk_io", "write_bytes_per_sec"):   ("disk",  "write_mbps",          "Mbps", _BYTES_TO_MBPS),
    # llama-server metrics. Two were already aliased pre-rename; the rest
    # land here so they get proper units in the AE catalog and surface in
    # the metrics dropdown / rule editor with sensible labels.
    ("llama", "tokens_per_second"):           ("llama", "tokens_per_second",        "tps",    None),
    ("llama", "prompt_tokens_per_second"):    ("llama", "prompt_tokens_per_second", "tps",    None),
    ("llama", "total_tokens_generated"):      ("llama", "total_tokens_generated",   "tokens", None),
    ("llama", "total_tokens_prompted"):       ("llama", "total_tokens_prompted",    "tokens", None),
    ("llama", "requests_processing"):         ("llama", "requests_processing",      None,     None),
    ("llama", "requests_deferred"):           ("llama", "requests_deferred",        None,     None),
    ("llama", "active_slots"):                ("llama", "active_slots",             None,     None),
    ("llama", "n_decode_total"):              ("llama", "n_decode_total",           None,     None),
    ("llama", "n_busy_slots_per_decode"):     ("llama", "n_busy_slots_per_decode",  None,     None),
    ("llama", "n_tokens_max"):                ("llama", "n_tokens_max",             "tokens", None),
    ("llama", "kv_cache_usage_ratio"):        ("llama", "kv_cache_usage_ratio",     "ratio",  None),
    ("llama", "kv_cache_tokens"):             ("llama", "kv_cache_tokens",          "tokens", None),
    ("llama", "n_remain"):                    ("llama", "n_remain",                 "tokens", None),
    ("ups", "percent"):                       ("psu",   "load_percent",             "%",      None),
    # Agent-side InfluxDB disk usage probe (`du -sb /var/lib/influxdb`).
    # Source matches the AE's own influx_monitor loop so the same
    # InfluxDB Health card / alarm rules pick it up on split installs
    # where the AE has no /var/lib/influxdb to scan.
    ("influxdb", "bytes_on_disk"):            ("influxdb", "bytes_on_disk",         "bytes",  None),
    # Self-monitor probes — emitted by the agent's _meta_perf_loop when
    # MONITOR_MANAGER_ENABLED / MONITOR_ALARM_ENGINE_ENABLED is set on
    # that agent. The top-level key in the agent sample is
    # `manager_self_monitor`; we keep the same source here so the catalog
    # surfaces a single coherent group in the rule editor.
    ("manager_self_monitor", "manager_api_latency_ms"):    ("manager_self_monitor", "manager_api_latency_ms",    "ms",    None),
    ("manager_self_monitor", "manager_history_latency_ms"):("manager_self_monitor", "manager_history_latency_ms","ms",    None),
    ("manager_self_monitor", "ae_health_latency_ms"):      ("manager_self_monitor", "ae_health_latency_ms",      "ms",    None),
    ("manager_self_monitor", "ae_ingest_latency_ms"):      ("manager_self_monitor", "ae_ingest_latency_ms",      "ms",    None),
    ("manager_self_monitor", "ae_query_24h_latency_ms"):   ("manager_self_monitor", "ae_query_24h_latency_ms",   "ms",    None),
    ("manager_self_monitor", "rule_eval_cycle_ms"):        ("manager_self_monitor", "rule_eval_cycle_ms",        "ms",    None),
    ("manager_self_monitor", "influx_write_latency_ms"):   ("manager_self_monitor", "influx_write_latency_ms",   "ms",    None),
    ("manager_self_monitor", "influx_query_5m_latency_ms"):("manager_self_monitor", "influx_query_5m_latency_ms","ms",    None),
    ("manager_self_monitor", "influx_query_24h_latency_ms"):("manager_self_monitor","influx_query_24h_latency_ms","ms",   None),
}


def _slug(text: str) -> str:
    """Filesystem-friendly slug for use inside a metric name.

    /        → root
    /home    → home
    /mnt/x   → mnt_x
    """
    if not text:
        return ""
    s = str(text)
    if s == "/":
        return "root"
    s = s.strip("/").replace("/", "_").replace(" ", "_").replace(":", "")
    return s or "root"


# Keys we accept as the identifier when descending into a list-of-dicts
# (e.g. `disk: [{mountpoint: "/", percent: ...}, ...]`).
_LIST_ID_KEYS: tuple[str, ...] = ("mountpoint", "device", "name", "id", "path")


def flatten(
    metric: dict[str, Any],
    path: tuple[str, ...] = (),
) -> Iterable[tuple[tuple[str, ...], float]]:
    """Yield (path, value) for every finite numeric leaf in the metric dict.

    Strings, booleans, and None are skipped — alarm rules can only reason
    over scalar numeric values. Top-level keys in SKIP_TOP_LEVEL are skipped
    wholesale.

    Lists of dicts are descended into when each element carries an
    identifier-like key (mountpoint / device / name / id / path); the slug
    of that identifier becomes the next path segment so per-volume metrics
    stay distinguishable. Lists of scalars are skipped (no useful key).
    """
    for key, val in metric.items():
        if not path and key in SKIP_TOP_LEVEL:
            continue
        cur = path + (str(key),)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            f = float(val)
            if f != f or f in (float("inf"), float("-inf")):
                continue
            yield cur, f
        elif isinstance(val, dict):
            yield from flatten(val, cur)
        elif isinstance(val, list):
            for item in val:
                if not isinstance(item, dict):
                    continue
                ident = next(
                    (item[k] for k in _LIST_ID_KEYS if k in item and item[k]),
                    None,
                )
                if ident is None:
                    continue
                slug = _slug(ident)
                if not slug:
                    continue
                # Strip the identifier key out so we don't emit a useless
                # `disk/<mount>/mountpoint` series; keep the numeric leaves.
                inner = {k: v for k, v in item.items() if k not in _LIST_ID_KEYS}
                yield from flatten(inner, cur + (slug,))
        # strings, None are skipped intentionally


def resolve(
    path: tuple[str, ...], value: float
) -> tuple[str, str, float, Optional[str]]:
    """Convert a (path, value) into (source, metric_name, value, unit).

    Looks up the alias table for canonical names; otherwise falls back to
    using the first path segment as the source and the rest as the name.
    """
    alias = ALIAS_TABLE.get(path)
    if alias is not None:
        src, name, unit, transform = alias
        return src, name, transform(value) if transform else value, unit
    if len(path) == 1:
        return path[0], "value", value, None
    return path[0], "_".join(path[1:]), value, None


def metric_to_points(
    metric: dict[str, Any],
    hostname: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Flatten a raw metric dict into a list of MetricPoint-shaped dicts.

    Caller-provided `hostname` overrides any "host" key in the metric. If
    neither is set the points carry no hostname (alerts will fall back to
    rule.source_host or display the metric category as "Source").
    """
    host = hostname or metric.get("host") or metric.get("platform")
    ts = timestamp or metric.get("ts")
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for path, value in flatten(metric):
        source, metric_name, val, unit = resolve(path, value)
        point: dict[str, Any] = {
            "source": source,
            "metric_name": metric_name,
            "value": val,
        }
        if ts:
            point["timestamp"] = ts
        if unit:
            point["unit"] = unit
        if host:
            point["hostname"] = host
        seen[(source, metric_name)] = point  # last write wins on collisions
    return list(seen.values())
