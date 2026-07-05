"""REST API routes for metrics ingestion and querying.

Endpoints:
    GET    /api/alarm/metrics                  - List all tracked metrics (summary)
    POST   /api/alarm/metrics                  - Ingest a single metric point
    POST   /api/alarm/metrics/batch            - Ingest pre-flattened MetricPoints
    POST   /api/alarm/metrics/ingest           - Ingest raw agent payloads (auto-flattens)
    GET    /api/alarm/metrics/{source}/{name}  - Get metric history
    GET    /api/alarm/metrics/{source}/{name}/summary - Get metric summary
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ...integration.metric_flatten import metric_to_points
from ...models.alarm_rule import TAG_VALUE_RE
from ...models.metrics import MetricBatchCreate, MetricPoint
from ...storage.repositories import MetricRepository
from ..auth import require_ingest_token
from config.unified_config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alarm/metrics", tags=["metrics"])

# FastAPI Query() bounds are evaluated at route-decorator time (module load),
# so we hoist them to module-level constants sourced from settings. Adjust
# via [alarm_engine.api_limits] in llm-systems.toml.
_API = settings.alarm_engine.api_limits
_RECENT_LIST_MAX  = _API.recent_alerts_limit_max
_EXPORT_SINCE_MAX = _API.export_since_minutes_max
_HIST_SINCE_MAX   = _API.history_since_minutes_max
_HIST_LIMIT_MAX   = _API.history_limit_max
_SUMMARY_WIN_MAX  = _API.summary_window_minutes_max

def _validate_tag(value: str, field: str) -> str:
    if not TAG_VALUE_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
    return value

# ── Dependency injection wiring ─────────────────────────────────
_metric_repo: Optional[MetricRepository] = None


def set_repository(repo: Optional[MetricRepository] = None) -> None:
    """Wire shared repository instance (called from alarm_engine.py at startup)."""
    global _metric_repo
    if repo is not None:
        _metric_repo = repo


def get_metric_repo() -> MetricRepository:
    """FastAPI dependency: return the shared MetricRepository."""
    if _metric_repo is None:
        raise RuntimeError("MetricRepository not initialized")
    return _metric_repo


# ── Routes ──────────────────────────────────────────────────────

# TTL cache for the catalog walk below. The full cache iteration scales with
# (n_metric_keys × points_per_key) and dominates p99 under concurrent load —
# bench measured p99 ≈ 3 s while the catalog itself only changes when a new
# (host, source, metric) tuple appears. A 30 s TTL is invisible to UI users
# and drops p99 to ~0 ms on cache hits.
_LIST_METRICS_TTL_S = 30.0
_list_metrics_cache: dict[tuple[Optional[str], int], tuple[float, list[dict]]] = {}
_list_metrics_cache_lock = threading.Lock()


def compute_metric_list(
    metric_repo: MetricRepository,
    source: Optional[str] = None,
    limit: int = 1000,
    hostname: Optional[str] = None,
) -> list[dict]:
    """Build the cascade-dropdown list (host × source × metric_name).

    Reads from the cached TTL snapshot when fresh; otherwise walks the cache
    once and updates the snapshot. Shared by the HTTP handler and the
    startup pre-warm task so they hit the same cache key shape. When hostname
    is set, only that host's rows are returned (kept in the cache key).
    """
    now = time.monotonic()
    cache_key = (source, limit, hostname)
    with _list_metrics_cache_lock:
        entry = _list_metrics_cache.get(cache_key)
        if entry is not None and (now - entry[0]) < _LIST_METRICS_TTL_S:
            return entry[1]

    cache = metric_repo.cache
    seen: dict[tuple[Optional[str], str, str], dict] = {}

    for key in cache.metric_keys:
        parts = key.split(":", 1)
        if len(parts) != 2:
            continue
        src, mname = parts
        if source and src != source:
            continue
        # We only need the latest point per (host, src, mname). Walk
        # the cached points from the tail and stop once each host has
        # been seen — avoids a full O(N×M) scan when most series are
        # single-host but their per-series buffer is large.
        all_pts = cache.get_metric_points(src, mname, limit=10000)
        if not all_pts:
            continue
        latest_by_host: dict[Optional[str], Any] = {}
        for p in reversed(all_pts):
            host = getattr(p, "hostname", None)
            if host in latest_by_host:
                continue
            latest_by_host[host] = p
        for host, latest in latest_by_host.items():
            if hostname and host != hostname:
                continue
            seen[(host, src, mname)] = {
                "source": src,
                "metric_name": mname,
                "hostname": host,
                "unit": latest.unit,
                "latest_value": latest.value,
                "latest_timestamp": latest.timestamp,
            }

    result = list(seen.values())
    # Sort by latest timestamp descending — convert to string to avoid
    # TypeError when mixing offset-naive and offset-aware datetime objects.
    result.sort(key=lambda x: str(x.get("latest_timestamp") or ""), reverse=True)
    sliced = result[:limit]
    with _list_metrics_cache_lock:
        _list_metrics_cache[cache_key] = (now, sliced)
    return sliced


@router.get("")
async def list_metrics(
    source: Optional[str] = None,
    limit: int = Query(1000, ge=1, le=_RECENT_LIST_MAX),
    hostname: Optional[str] = Query(None, description="Filter to a single host"),
    metric_repo: MetricRepository = Depends(get_metric_repo),
) -> list[dict]:
    """List all tracked metrics with their latest values, segregated by host.

    Returns one row per (hostname, source, metric_name) so the rule form can
    build a cascading Device → Source → Metric dropdown without rules getting
    cross-attributed when both hosts push the same source/metric pair.

    Offloaded to a worker thread: compute_metric_list walks every cached
    series and (on a cache miss) takes seconds. Running it directly inline
    inside this async handler would block the event loop and serialize
    every other AE request behind it.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, compute_metric_list, metric_repo, source, limit, hostname,
    )


@router.post("")
async def ingest_metric(
    point: MetricPoint,
    metric_repo: MetricRepository = Depends(get_metric_repo),
    _auth: None = Depends(require_ingest_token),
) -> dict:
    """Ingest a new metric point."""
    created = metric_repo.create(point)
    return created.to_dict()


@router.post("/batch")
async def ingest_metric_batch(
    batch: MetricBatchCreate,
    metric_repo: MetricRepository = Depends(get_metric_repo),
    _auth: None = Depends(require_ingest_token),
) -> dict:
    """Ingest a batch of pre-flattened metric points."""
    count = metric_repo.create_batch(batch.metrics)
    return {"ingested": count}


@router.post("/ingest")
async def ingest_raw_batch(
    payload: dict[str, Any] = Body(...),
    metric_repo: MetricRepository = Depends(get_metric_repo),
    _auth: None = Depends(require_ingest_token),
) -> dict:
    """Ingest a buffered batch of RAW agent metric dicts.

    This is the canonical endpoint for agents using BufferedMetricClient.
    The payload is `{ "host": "<hostname>", "samples": [<raw metric dict>, ...] }`
    where each sample is the original collection-cycle dict produced by the
    agent. The endpoint flattens each sample into MetricPoint records and
    bulk-inserts them.

    Decoupling flatten from the agent keeps the alias table (cpu_total →
    cpu/usage_percent etc.) in one place and lets agents stay schema-naive.
    """
    host = payload.get("host")
    samples = payload.get("samples") or []
    if not isinstance(samples, list):
        raise HTTPException(status_code=400, detail="`samples` must be a list")

    points: list[MetricPoint] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        # Caller's host overrides per-sample host so a buffered batch from
        # one agent always carries that agent's identity, even if a single
        # sample dict happened to contain a stale host key.
        for raw_pt in metric_to_points(sample, hostname=host):
            try:
                points.append(MetricPoint(**raw_pt))
            except (TypeError, ValueError) as e:
                logger.debug("skipping malformed point %s: %s", raw_pt, e)

    if points:
        metric_repo.create_batch(points)
    logger.info(
        "ingest /metrics/ingest: host=%s samples=%d points=%d",
        host or "—", len(samples), len(points),
    )
    return {"samples_received": len(samples), "points_written": len(points)}


@router.get("/export")
async def export_metrics(
    source: str = Query(..., description="Metric source (e.g. cpu, gpu)"),
    metric_name: str = Query(..., description="Metric name (e.g. usage_percent)"),
    since_minutes: int = Query(1440, ge=1, le=_EXPORT_SINCE_MAX),
    hostname: Optional[str] = Query(None),
    format: str = Query("csv"),
    metric_repo: MetricRepository = Depends(get_metric_repo),
):
    """Export historical points for a single source/metric (optionally per host).

    Defined BEFORE /{source}/{metric_name} — FastAPI matches the first
    compatible route, and a path like /export would otherwise be interpreted
    as source="export" and yield a 422 missing-metric_name error.
    """
    import csv, io
    from fastapi.responses import Response
    if format != "csv":
        raise HTTPException(status_code=400, detail=f"Unsupported format: {format}")

    _validate_tag(source, "source")
    _validate_tag(metric_name, "metric_name")

    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    try:
        points = metric_repo.get_points(
            source, metric_name, since=since, limit=10000, hostname=hostname,
        )
    except Exception:
        logger.exception("metrics export: get_points failed")
        points = []

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "hostname", "source", "metric_name", "value", "unit"])
    for p in points:
        ts = p.timestamp.isoformat() if p.timestamp else ""
        writer.writerow([
            ts, p.hostname or "", p.source, p.metric_name,
            p.value, p.unit or "",
        ])

    host_part = (hostname + "_") if hostname else ""
    fname = f"{host_part}{source}_{metric_name}.csv".replace("/", "_")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@router.get("/{source}/{metric_name}")
async def get_metric_history(
    source: str,
    metric_name: str,
    since_minutes: int = Query(60, ge=1, le=_HIST_SINCE_MAX, alias="since_minutes"),
    limit: int = Query(100000, ge=1, le=_HIST_LIMIT_MAX),
    hostname: Optional[str] = Query(None, description="Filter to a single host"),
    metric_repo: MetricRepository = Depends(get_metric_repo),
) -> list[dict]:
    """Get metric history for a specific metric, optionally scoped to a host."""
    _validate_tag(source, "source")
    _validate_tag(metric_name, "metric_name")
    # Use timezone-aware UTC so .timestamp() comparison against stored
    # tz-aware point timestamps is correct (now_utc() is naive and
    # .timestamp() on it would be interpreted as local time, off by the
    # local UTC offset and silently filtering out all in-cache points).
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    points = metric_repo.get_points(
        source, metric_name, since=since, limit=limit, hostname=hostname,
    )
    return [p.to_dict() for p in points]


@router.get("/{source}/{metric_name}/summary")
async def get_metric_summary(
    source: str,
    metric_name: str,
    window_minutes: int = Query(60, ge=1, le=_SUMMARY_WIN_MAX),
    metric_repo: MetricRepository = Depends(get_metric_repo),
) -> dict:
    """Get aggregated metric summary."""
    _validate_tag(source, "source")
    _validate_tag(metric_name, "metric_name")
    summary = metric_repo.get_summary(source, metric_name, window_minutes=window_minutes)

    if not summary:
        raise HTTPException(status_code=404, detail=f"No data for {source}/{metric_name}")

    return summary.to_dict()