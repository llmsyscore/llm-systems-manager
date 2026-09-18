"""InfluxDB self-monitoring loop.

Periodically probes the local InfluxDB instance (liveness, query latency,
write success rate, on-disk bytes, series cardinality) and writes the
results back through the MetricRepository as `source=influxdb` so the
same alarm-rule machinery that watches CPU/GPU can alert on InfluxDB
itself. Without this the alarm engine is blind to its own backing store
and a silent InfluxDB degradation silently degrades all alerting.

The loop is best-effort: any single probe failure increments the
appropriate error counter and continues; the loop never raises out into
the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import requests

from ..models.metrics import MetricPoint
from .repositories import MetricRepository
from .influxdb_client import InfluxDBClient, _flux_str

logger = logging.getLogger(__name__)


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "localhost"


def _write_metric(repo: MetricRepository, name: str, value: float,
                  unit: Optional[str] = None, host: Optional[str] = None) -> None:
    try:
        repo.create(MetricPoint(
            source="influxdb",
            metric_name=name,
            value=float(value),
            unit=unit,
            timestamp=datetime.now(timezone.utc),
            hostname=host or _hostname(),
        ))
    except Exception as e:
        logger.debug("self-monitor write %s failed: %s", name, e)


def _write_probe(repo: MetricRepository, host: Optional[str] = None) -> bool:
    """Synchronous write probe. Returns True on success, False if it raised."""
    try:
        repo.create(MetricPoint(
            source="influxdb",
            metric_name="selfwrite_probe",
            value=1.0,
            unit=None,
            timestamp=datetime.now(timezone.utc),
            hostname=host or _hostname(),
        ), sync=True)
        return True
    except Exception as e:
        logger.warning("InfluxDB self-monitor write probe failed: %s", e)
        return False


def _ping(url: str) -> tuple[bool, float]:
    """Return (ok, latency_ms). latency_ms is -1 when unreachable."""
    from config.unified_config import settings as _settings
    t0 = time.perf_counter()
    try:
        r = requests.get(
            f"{url.rstrip('/')}/ping",
            timeout=_settings.alarm_engine.timeouts.influxdb_ping,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        return (r.status_code in (200, 204), latency_ms)
    except Exception:
        return (False, -1.0)


def _query_latency_ms(db: InfluxDBClient) -> float:
    """Issue a trivial Flux read and time it. Returns -1 on failure."""
    flux = f'''
        from(bucket: "{db.metrics_bucket}")
          |> range(start: -1m)
          |> filter(fn: (r) => r._measurement == "metrics")
          |> limit(n: 1)
    '''
    t0 = time.perf_counter()
    try:
        # Touch the iterator so the query actually executes.
        list(db._metrics_query.query(flux, org=db.org))
        return (time.perf_counter() - t0) * 1000
    except Exception as e:
        logger.debug("self-monitor query probe failed: %s", e)
        return -1.0


# Series cardinality moves slowly; the query is cached for this long.
_CARDINALITY_TTL_S = 900.0
_CARD_CACHE: dict[str, dict] = {}
# Buckets whose token was refused influxdb.cardinality(); they use the fallback query.
_cardinality_fallback: set[str] = set()
_cardinality_warned: set[str] = set()


def _cardinality_flux(bucket: str) -> str:
    """Series count from the storage index; no points are read."""
    return (f'import "influxdata/influxdb"\n'
            f'influxdb.cardinality(bucket: "{_flux_str(bucket)}", start: -24h)')


def _cardinality_fallback_flux(bucket: str) -> str:
    """Series count for tokens refused cardinality(): last() per series, then count."""
    return f'''
        from(bucket: "{_flux_str(bucket)}")
          |> range(start: -24h)
          |> filter(fn: (r) => r._measurement == "metrics" and r._field == "value")
          |> last()
          |> group()
          |> count()
    '''


def _is_unauthorized(e: Exception) -> bool:
    status = getattr(e, "status", None)
    return status in (401, 403) or "401" in str(e) or "Unauthorized" in str(e)


def _first_number(tables) -> int:
    for t in tables:
        for r in t.records:
            v = r.get_value()
            if isinstance(v, (int, float)):
                return int(v)
    return 0


def _cardinality(db: InfluxDBClient, bucket: str, query_api,
                 now: Optional[float] = None) -> Optional[int]:
    """Distinct series in `bucket` over 24h, cached for _CARDINALITY_TTL_S.
    Returns 0 when both queries are refused (401), None on any other failure."""
    now = time.monotonic() if now is None else now
    hit = _CARD_CACHE.get(bucket)
    if hit is not None and (now - hit["at"]) < _CARDINALITY_TTL_S:
        return hit["value"]
    t0 = time.perf_counter()
    value: Optional[int] = None
    try:
        if bucket not in _cardinality_fallback:
            try:
                value = _first_number(list(query_api.query(_cardinality_flux(bucket), org=db.org)))
            except Exception as e:
                if _is_unauthorized(e):
                    _cardinality_fallback.add(bucket)
                else:
                    logger.debug("influxdb.cardinality(%s) failed, using fallback: %s", bucket, e)
        if value is None:
            value = _first_number(list(query_api.query(_cardinality_fallback_flux(bucket), org=db.org)))
    except Exception as e:
        if not _is_unauthorized(e):
            logger.debug("cardinality(%s) failed: %s", bucket, e)
            return None
        if bucket not in _cardinality_warned:
            _cardinality_warned.add(bucket)
            logger.warning(
                "cardinality(%s) returned 401 — the token scoped to this "
                "bucket has no read permission. Card will report 0; grant "
                "read with: influx auth update --id <id> --read-bucket <id>",
                bucket,
            )
        value = 0
    _CARD_CACHE[bucket] = {"value": value, "at": now,
                           "query_ms": (time.perf_counter() - t0) * 1000}
    return value


_BYTES_CACHE: dict[str, float] = {"value": -1.0, "at": 0.0}
_BYTES_CACHE_TTL_S = 300.0   # 5 min — disk size changes slowly


def _bytes_on_disk() -> Optional[int]:
    """Sum bytes in the InfluxDB v2 data directory.

    The directory is typically mode 0750 owned by user influxdb so the
    alarm-engine user (e.g. llmsys) cannot read it directly. We first try
    `du` unprivileged; if that fails with permission denied, we retry via
    `sudo -n` (the operator can grant this with one sudoers line — see
    the README block at the top of this module). Returns None when both
    attempts fail.

    Cached for _BYTES_CACHE_TTL_S seconds — `du` walks the entire TSM
    directory tree (~hundreds of files) which is wasteful to repeat
    every 30 s (the monitor cadence) when disk-size changes minute-by-
    minute at most.
    """
    import time as _time
    now = _time.monotonic()
    if (_BYTES_CACHE["value"] >= 0
            and (now - _BYTES_CACHE["at"]) < _BYTES_CACHE_TTL_S):
        return int(_BYTES_CACHE["value"])

    candidates = [
        os.environ.get("INFLUXD_ENGINE_PATH"),
        "/var/lib/influxdb",
        "/var/lib/influxdb2",
    ]
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        for cmd in (["du", "-sb", path], ["sudo", "-n", "du", "-sb", path]):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0 and r.stdout:
                    bytes_val = int(r.stdout.split()[0])
                    _BYTES_CACHE["value"] = float(bytes_val)
                    _BYTES_CACHE["at"] = now
                    return bytes_val
            except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
                continue
    return None


async def run(
    db: InfluxDBClient,
    repo: MetricRepository,
    interval_s: int = 30,
    initial_delay_s: float = 30.0,
) -> None:
    """Background loop: probe + emit metrics every interval_s seconds; the first
    cycle waits initial_delay_s. Blocking probes run in the default executor."""
    url = db.url
    host = _hostname()
    loop = asyncio.get_running_loop()
    # Sliding window of recent probe outcomes.
    probe_window: deque[bool] = deque(maxlen=20)
    consecutive_write_errors = 0

    def _record_write(ok: bool) -> None:
        nonlocal consecutive_write_errors
        probe_window.append(ok)
        consecutive_write_errors = 0 if ok else consecutive_write_errors + 1
        if probe_window:
            _write_metric(repo, "write_ok_rate",
                          sum(probe_window) / len(probe_window), "ratio", host)
        _write_metric(repo, "write_errors_consecutive",
                      float(consecutive_write_errors), None, host)

    logger.info("InfluxDB self-monitor started (interval=%ss, first probe in %ss)",
                interval_s, initial_delay_s)
    if initial_delay_s > 0:
        await asyncio.sleep(initial_delay_s)

    while True:
        try:
            ok, ping_ms = _ping(url)
            _write_metric(repo, "ping_ms", ping_ms if ok else -1.0, "ms", host)
            _write_metric(repo, "up", 1.0 if ok else 0.0, None, host)
            if not ok:
                # Unreachable: writes are failing too — record it so the
                # write-health series don't freeze at a stale value.
                _record_write(False)
                await asyncio.sleep(interval_s)
                continue

            q_ms = _query_latency_ms(db)
            _write_metric(repo, "query_ms", q_ms, "ms", host)

            # Probe write runs in a thread so a slow/hanging write can't
            # block the event loop; sync path raises so outages are seen.
            t0 = time.perf_counter()
            write_ok = await loop.run_in_executor(None, _write_probe, repo, host)
            _record_write(write_ok)
            if write_ok:
                w_ms = (time.perf_counter() - t0) * 1000
                _write_metric(repo, "write_ms", w_ms, "ms", host)

            n = await loop.run_in_executor(
                None, _cardinality, db, db.metrics_bucket, db._metrics_query)
            if n is not None:
                _write_metric(repo, "cardinality_metrics", float(n), None, host)
                q = (_CARD_CACHE.get(db.metrics_bucket) or {}).get("query_ms")
                if q is not None:
                    _write_metric(repo, "cardinality_query_ms", q, "ms", host)

            disk = await loop.run_in_executor(None, _bytes_on_disk)
            if disk is not None:
                _write_metric(repo, "bytes_on_disk", float(disk), "bytes", host)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("InfluxDB self-monitor cycle failed")
        await asyncio.sleep(interval_s)
