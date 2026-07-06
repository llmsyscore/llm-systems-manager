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
from .influxdb_client import InfluxDBClient

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


_cardinality_warned: set[str] = set()


def _cardinality(db: InfluxDBClient, bucket: str, query_api) -> Optional[int]:
    """Count distinct series in `bucket` over the last 24h.

    schema.cardinality() needs admin/task scope, which our bucket-scoped
    tokens lack (InfluxDB returns 401). Instead, walk every series with
    last() then collapse into one table and count rows — that works with
    any token that has read access to the bucket and is still cheap
    because last() returns one point per series, not all points.

    Returns:
      n        — actual count (including 0 for an empty bucket, since
                 Flux's count() emits no rows on empty input rather than
                 a row with value 0).
      0        — degraded path: query failed with 401 because the
                 bucket-scoped token doesn't have read permission on its
                 own bucket. Better to surface 0 than leave the card
                 showing a dash; we log a one-time warning so the
                 operator can fix it via `influx auth update`.
      None     — any other query failure (transport, syntax, …) so
                 callers can distinguish a transient blip from a
                 permanent config issue.
    """
    # `|> map(... _value: 1.0)` rewrites every record's _value to a uniform
    # float before last()/count() runs. Without this, buckets that store
    # both string fields (e.g. alerts.message) and numeric fields (e.g.
    # alerts.current_value) trigger:
    #   "schema collision detected: column _value is both float and string"
    # at count() because Flux refuses to merge tables with mismatched
    # column types. The rewrite is cheap (per-record), preserves series
    # identity (tag set + _measurement + _field), and makes the query
    # bucket-shape-agnostic.
    flux = f'''
        from(bucket: "{bucket}")
          |> range(start: -24h)
          |> map(fn: (r) => ({{r with _value: 1.0}}))
          |> last()
          |> group()
          |> count()
    '''
    try:
        tables = list(query_api.query(flux, org=db.org))
        for t in tables:
            for r in t.records:
                v = r.get_value()
                if isinstance(v, (int, float)):
                    return int(v)
        return 0
    except Exception as e:
        # influxdb_client raises ApiException with .status on HTTP errors;
        # fall back to string-match for older client versions.
        status = getattr(e, "status", None)
        is_unauth = status == 401 or "401" in str(e) or "Unauthorized" in str(e)
        if is_unauth:
            if bucket not in _cardinality_warned:
                _cardinality_warned.add(bucket)
                logger.warning(
                    "cardinality(%s) returned 401 — the token scoped to this "
                    "bucket has no read permission. Card will report 0; grant "
                    "read with: influx auth update --id <id> --read-bucket <id>",
                    bucket,
                )
            return 0
        logger.debug("cardinality(%s) failed: %s", bucket, e)
        return None


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
    """Background loop: probe + emit metrics every interval_s seconds.

    The first cycle runs 3 synchronous cardinality Flux queries plus a
    write probe and a disk-size scan — together that's 10-20 s of work on
    the asyncio event loop. Delaying the first cycle (`initial_delay_s`)
    keeps the event loop free during the AE startup window so concurrent
    HTTP requests (notably the manager's history-ring warm-up fan-out)
    can be served immediately instead of stalling.
    """
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

            n = _cardinality(db, db.metrics_bucket, db._metrics_query)
            if n is not None:
                _write_metric(repo, "cardinality_metrics", float(n), None, host)

            disk = _bytes_on_disk()
            if disk is not None:
                _write_metric(repo, "bytes_on_disk", float(disk), "bytes", host)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("InfluxDB self-monitor cycle failed")
        await asyncio.sleep(interval_s)
