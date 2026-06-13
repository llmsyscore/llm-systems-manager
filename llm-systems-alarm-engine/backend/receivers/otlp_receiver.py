"""OTLP/HTTP receiver for metrics, traces, and logs.

OpenClaw's diagnostics-otel plugin pushes OpenTelemetry telemetry here over
HTTP/protobuf. We decode the standard OTLP envelopes and convert every signal
into the same metric-record shape so the alarm engine's rule evaluator can
operate on traces and logs the same way it does on native metrics:

  /v1/metrics  → counters / gauges / histograms (sum + count) → InfluxDB
  /v1/traces   → one duration_ms metric per span (name = "<span>.duration_ms")
  /v1/logs     → one count=1 metric per log record (name = "<source>.log.count")

All OTEL attributes (resource + data-point + span/log) are preserved as tags
so dashboards can slice arbitrarily. Span status and log severity become tags
on the synthesized metric, which is what makes "alert on error rate" rules
possible without a separate logs storage layer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Status

from ..api.auth import require_ingest_token
from ..models.metrics import MetricPoint
from ..storage.cache import Cache
from ..storage.influxdb_client import InfluxDBClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["otlp"])

_cache: Optional[Cache] = None
_db: Optional[InfluxDBClient] = None

# Counters (cumulative since process start) for the heartbeat task
_otlp_metrics_seen = 0   # number of data points ingested
_otlp_traces_seen  = 0   # number of spans ingested
_otlp_logs_seen    = 0   # number of log records ingested
_otlp_metric_batches = 0
_otlp_trace_batches  = 0
_otlp_log_batches    = 0
_otlp_parse_errors   = 0
_otlp_write_errors   = 0
_hb_task = None


def configure(cache: Cache, db: Optional[InfluxDBClient]) -> None:
    """Wire the receiver to the alarm engine's cache and InfluxDB client.

    Called once during alarm engine startup. We hold module-level references
    instead of using FastAPI Depends because the receiver lives inside the
    same process as the engine — no need to round-trip through the DI graph.
    """
    global _cache, _db, _hb_task
    _cache = cache
    _db = db
    # Start a 60s heartbeat task once a running event loop is available.
    try:
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        if _hb_task is None or _hb_task.done():
            _hb_task = loop.create_task(_heartbeat_loop())
            logger.info("OTLP receiver heartbeat task started (60s interval)")
    except Exception as e:
        logger.warning("OTLP heartbeat task failed to start: %s", e, exc_info=True)


async def _heartbeat_loop() -> None:
    """Emit one log line per interval with delta counters since last tick.

    INFO when there's traffic, DEBUG when quiet so quiet days don't spam.
    """
    import asyncio as _asyncio
    from config.unified_config import settings as _settings
    last_m = last_t = last_l = 0
    last_pe = last_we = 0
    while True:
        try:
            await _asyncio.sleep(_settings.alarm_engine.intervals.otlp_heartbeat_s)
            dm = _otlp_metrics_seen - last_m
            dt = _otlp_traces_seen  - last_t
            dl = _otlp_logs_seen    - last_l
            dpe = _otlp_parse_errors - last_pe
            dwe = _otlp_write_errors - last_we
            last_m, last_t, last_l = _otlp_metrics_seen, _otlp_traces_seen, _otlp_logs_seen
            last_pe, last_we = _otlp_parse_errors, _otlp_write_errors
            line = (
                f"heartbeat otlp: metrics+{dm} traces+{dt} logs+{dl} "
                f"(total m={_otlp_metrics_seen} t={_otlp_traces_seen} l={_otlp_logs_seen} "
                f"batches m/t/l={_otlp_metric_batches}/{_otlp_trace_batches}/{_otlp_log_batches}) "
                f"parse_err+{dpe} write_err+{dwe}"
            )
            if dm or dt or dl or dpe or dwe:
                logger.info(line)
            else:
                logger.debug(line)
        except _asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("OTLP heartbeat tick failed: %s", e, exc_info=True)


def _attr_value(v: AnyValue) -> str:
    """Coerce a protobuf AnyValue to a string suitable for an InfluxDB tag.

    InfluxDB tags must be strings; numeric/bool attributes are stringified
    rather than dropped because they're often the dimension we want to slice
    on (model, status code, etc.).
    """
    kind = v.WhichOneof("value")
    if kind == "string_value":
        return v.string_value
    if kind == "bool_value":
        return "true" if v.bool_value else "false"
    if kind == "int_value":
        return str(v.int_value)
    if kind == "double_value":
        return str(v.double_value)
    if kind == "bytes_value":
        try:
            return v.bytes_value.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return ""


def _attrs_to_dict(attrs) -> dict[str, str]:
    return {kv.key: _attr_value(kv.value) for kv in attrs}


def _ts_from_nanos(nanos: int) -> datetime:
    if not nanos:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc)


def _flatten_metrics(req: ExportMetricsServiceRequest) -> list[dict[str, Any]]:
    """Walk ResourceMetrics → ScopeMetrics → Metric → DataPoint and produce
    one record per data point.

    Each record is shaped like the InfluxDB write payload the rest of the
    alarm engine uses (measurement/tags/fields/time) so we can hand it
    straight to db.write_metrics_batch().

    OTEL attributes from both the Resource and the data point land in tags.
    The data-point attributes win on key collision (more specific dimension).
    """
    records: list[dict[str, Any]] = []

    for rm in req.resource_metrics:
        resource_attrs = _attrs_to_dict(rm.resource.attributes)
        # service.name -> source so existing alarm rules can target the producer
        source = resource_attrs.pop("service.name", "openclaw-otel")
        hostname = resource_attrs.pop("host.name", None) or resource_attrs.pop(
            "host.hostname", None
        )

        for sm in rm.scope_metrics:
            scope_name = sm.scope.name if sm.HasField("scope") else ""

            for metric in sm.metrics:
                name = metric.name
                unit = metric.unit or ""

                kind = metric.WhichOneof("data")
                if kind in ("sum", "gauge"):
                    points = metric.sum.data_points if kind == "sum" else metric.gauge.data_points
                    for dp in points:
                        records.extend(
                            _emit_number_point(
                                dp, name, unit, source, hostname,
                                resource_attrs, scope_name,
                            )
                        )
                elif kind == "histogram":
                    for dp in metric.histogram.data_points:
                        records.extend(
                            _emit_histogram_point(
                                dp, name, unit, source, hostname,
                                resource_attrs, scope_name,
                            )
                        )
                # exponential_histogram and summary are skipped in Phase 1.

    return records


def _emit_number_point(
    dp,
    name: str,
    unit: str,
    source: str,
    hostname: Optional[str],
    resource_attrs: dict[str, str],
    scope_name: str,
) -> list[dict[str, Any]]:
    value = dp.as_double if dp.HasField("as_double") else float(dp.as_int)
    tags = _build_tags(source, name, unit, hostname, resource_attrs, dp.attributes, scope_name)
    return [_build_record(name, value, tags, _ts_from_nanos(dp.time_unix_nano))]


def _emit_histogram_point(
    dp,
    name: str,
    unit: str,
    source: str,
    hostname: Optional[str],
    resource_attrs: dict[str, str],
    scope_name: str,
) -> list[dict[str, Any]]:
    """Phase 1 emits only sum and count. Bucket counts (for percentile
    queries) are deferred to Phase 2 — they multiply record volume by the
    bucket count and we want to validate the basic flow first.
    """
    out: list[dict[str, Any]] = []
    ts = _ts_from_nanos(dp.time_unix_nano)
    base_tags = _build_tags(source, name, unit, hostname, resource_attrs, dp.attributes, scope_name)

    if dp.HasField("sum"):
        out.append(_build_record(f"{name}.sum", dp.sum, dict(base_tags), ts))
    out.append(_build_record(f"{name}.count", float(dp.count), dict(base_tags), ts))
    return out


def _build_tags(
    source: str,
    metric_name: str,
    unit: str,
    hostname: Optional[str],
    resource_attrs: dict[str, str],
    dp_attrs,
    scope_name: str,
) -> dict[str, str]:
    """Build the tag set for one InfluxDB record.

    The data-point-level OTEL attributes win on collision — they're more
    specific than the resource-level ones (e.g., per-call `model` vs
    process-level `service.version`).
    """
    tags: dict[str, str] = {
        "source": source,
        "metric_name": metric_name,
        "unit": unit,
    }
    if hostname:
        tags["hostname"] = hostname
    if scope_name:
        tags["scope"] = scope_name
    for k, v in resource_attrs.items():
        tags[_safe_tag(k)] = v
    for kv in dp_attrs:
        tags[_safe_tag(kv.key)] = _attr_value(kv.value)
    return tags


def _safe_tag(key: str) -> str:
    """InfluxDB allows dots in tag keys but our rule UI is friendlier with
    underscores; normalize so attribute names don't collide with reserved
    Flux tokens.
    """
    return key.replace(".", "_").replace(" ", "_")


def _build_record(metric_name: str, value: float, tags: dict[str, str], ts: datetime) -> dict[str, Any]:
    tags["metric_name"] = metric_name  # overwrite so histogram .sum/.count are distinct
    return {
        "measurement": "metrics",
        "tags": tags,
        "fields": {"value": float(value)},
        "time": int(ts.timestamp() * 1e9),
    }


def _seed_cache(records: list[dict[str, Any]]) -> None:
    """Push every record into the in-memory metric cache.

    Alarm rule evaluation reads from the cache for hot-path queries, so
    bypassing it would mean rules can't react to OTEL metrics until the next
    InfluxDB scan. The cache key is (source, metric_name) so points across
    different attribute combinations land in the same series; that's an
    accepted Phase 1 trade-off — for finer slicing, dashboards still query
    InfluxDB directly where the full tag set is preserved.
    """
    if _cache is None:
        return
    for r in records:
        try:
            tags = r["tags"]
            point = MetricPoint(
                source=tags.get("source", "openclaw-otel"),
                metric_name=tags.get("metric_name", "unknown"),
                value=r["fields"]["value"],
                unit=tags.get("unit") or None,
                timestamp=datetime.fromtimestamp(r["time"] / 1e9, tz=timezone.utc),
                hostname=tags.get("hostname"),
            )
            _cache.add_metric_point(point)
        except Exception as e:
            logger.debug(f"OTLP cache add skipped: {e}")


@router.post("/v1/metrics")
async def receive_metrics(request: Request, _auth: None = Depends(require_ingest_token)) -> Response:
    """OTLP/HTTP metrics endpoint.

    Spec: https://opentelemetry.io/docs/specs/otlp/#otlphttp
    Returns an empty ExportMetricsServiceResponse on success — partial-
    success reporting is intentionally omitted for Phase 1; we either
    accept the whole batch or fail the request.
    """
    body = await request.body()
    if not body:
        return Response(
            content=ExportMetricsServiceResponse().SerializeToString(),
            media_type="application/x-protobuf",
        )

    req = ExportMetricsServiceRequest()
    try:
        req.ParseFromString(body)
    except Exception as e:
        global _otlp_parse_errors
        _otlp_parse_errors += 1
        logger.warning(f"OTLP metrics parse error: {e}")
        return Response(status_code=400, content=b"protobuf parse error")

    try:
        records = _flatten_metrics(req)
    except Exception as e:
        logger.exception(f"OTLP metrics flatten error: {e}")
        return Response(status_code=400, content=b"flatten error")

    if records and _db is not None:
        try:
            _db.write_metrics_batch(records)
        except Exception as e:
            global _otlp_write_errors
            _otlp_write_errors += 1
            logger.exception(f"OTLP InfluxDB write failed: {e}")
            return Response(status_code=500, content=b"db write failed")

    _seed_cache(records)
    global _otlp_metrics_seen, _otlp_metric_batches
    _otlp_metrics_seen += len(records)
    _otlp_metric_batches += 1

    logger.debug(f"OTLP metrics ingested: {len(records)} points")
    return Response(
        content=ExportMetricsServiceResponse().SerializeToString(),
        media_type="application/x-protobuf",
    )


# ── Phase 2: traces ──────────────────────────────────────────────────────

def _flatten_spans(req: ExportTraceServiceRequest) -> list[dict[str, Any]]:
    """Walk ResourceSpans → ScopeSpans → Span and synthesize one record per
    span shaped as a duration_ms metric.

    The span name becomes "<name>.duration_ms" so each span kind shows up as
    its own series — `openclaw.run.duration_ms`, `openclaw.tool.execution.duration_ms`,
    `openclaw.model.call.duration_ms`, etc. — and rules can target a specific
    operation without parsing tags.
    """
    records: list[dict[str, Any]] = []

    for rs in req.resource_spans:
        resource_attrs = _attrs_to_dict(rs.resource.attributes)
        source = resource_attrs.pop("service.name", "openclaw-otel")
        hostname = resource_attrs.pop("host.name", None) or resource_attrs.pop(
            "host.hostname", None
        )

        for ss in rs.scope_spans:
            scope_name = ss.scope.name if ss.HasField("scope") else ""

            for span in ss.spans:
                start_ns = span.start_time_unix_nano
                end_ns = span.end_time_unix_nano
                if not start_ns or not end_ns or end_ns < start_ns:
                    # Malformed timing — skip rather than emit a bad metric.
                    continue
                duration_ms = (end_ns - start_ns) / 1e6

                metric_name = f"{span.name}.duration_ms"
                tags = _build_tags(
                    source, metric_name, "ms", hostname,
                    resource_attrs, span.attributes, scope_name,
                )
                # Status (UNSET/OK/ERROR) is what makes error-rate alarms
                # possible; we always emit it even when it's UNSET so rules
                # can filter consistently.
                tags["status"] = Status.StatusCode.Name(span.status.code).replace(
                    "STATUS_CODE_", ""
                ).lower()
                # span.kind too — e.g., CLIENT vs SERVER vs INTERNAL — useful
                # for slicing client-side latency from server-side latency on
                # the same metric.
                tags["span_kind"] = _span_kind_name(span.kind)

                records.append(_build_record(
                    metric_name, duration_ms, tags, _ts_from_nanos(end_ns),
                ))

    return records


def _span_kind_name(kind: int) -> str:
    """SpanKind protobuf enum → short string. Avoids importing the enum
    directly so the code is robust to proto-package layout changes.
    """
    return {
        0: "unspecified",
        1: "internal",
        2: "server",
        3: "client",
        4: "producer",
        5: "consumer",
    }.get(kind, f"unknown_{kind}")


@router.post("/v1/traces")
async def receive_traces(request: Request, _auth: None = Depends(require_ingest_token)) -> Response:
    """OTLP/HTTP traces endpoint.

    We don't persist trace structure (no parent/child reconstruction); each
    span is collapsed to a single `<name>.duration_ms` metric. Trace context
    (trace_id/span_id) is intentionally dropped — preserving it would require
    a separate trace store and isn't needed for alarm-rule evaluation.
    """
    body = await request.body()
    if not body:
        return Response(
            content=ExportTraceServiceResponse().SerializeToString(),
            media_type="application/x-protobuf",
        )

    req = ExportTraceServiceRequest()
    try:
        req.ParseFromString(body)
    except Exception as e:
        global _otlp_parse_errors
        _otlp_parse_errors += 1
        logger.warning(f"OTLP traces parse error: {e}")
        return Response(status_code=400, content=b"protobuf parse error")

    try:
        records = _flatten_spans(req)
    except Exception as e:
        logger.exception(f"OTLP traces flatten error: {e}")
        return Response(status_code=400, content=b"flatten error")

    if records and _db is not None:
        try:
            _db.write_metrics_batch(records)
        except Exception as e:
            global _otlp_write_errors
            _otlp_write_errors += 1
            logger.exception(f"OTLP traces InfluxDB write failed: {e}")
            return Response(status_code=500, content=b"db write failed")

    _seed_cache(records)
    global _otlp_traces_seen, _otlp_trace_batches
    _otlp_traces_seen += len(records)
    _otlp_trace_batches += 1

    logger.debug(f"OTLP traces ingested: {len(records)} spans")
    return Response(
        content=ExportTraceServiceResponse().SerializeToString(),
        media_type="application/x-protobuf",
    )


# ── Phase 3: logs ────────────────────────────────────────────────────────

def _severity_bucket(num: int) -> str:
    """Collapse OTEL's 24-level SeverityNumber into the 6 standard buckets so
    alarm rules don't have to enumerate every variant. OTEL uses 1–4=trace,
    5–8=debug, 9–12=info, 13–16=warn, 17–20=error, 21–24=fatal.
    """
    if num >= 21: return "fatal"
    if num >= 17: return "error"
    if num >= 13: return "warn"
    if num >= 9:  return "info"
    if num >= 5:  return "debug"
    if num >= 1:  return "trace"
    return "unspecified"


def _flatten_logs(req: ExportLogsServiceRequest) -> list[dict[str, Any]]:
    """Walk ResourceLogs → ScopeLogs → LogRecord and synthesize one record
    per log shaped as a count=1 metric.

    The metric name is "<source>.log.count" so a single rule like
    "warn rate > 10/min on openclaw-gateway" works without splitting metrics
    per severity. The severity ends up as a tag for filtering. Log bodies
    are NOT persisted — InfluxDB tags must be strings under ~64KB each and
    we don't want to bloat the metric series with unbounded text.
    """
    records: list[dict[str, Any]] = []

    for rl in req.resource_logs:
        resource_attrs = _attrs_to_dict(rl.resource.attributes)
        source = resource_attrs.pop("service.name", "openclaw-otel")
        hostname = resource_attrs.pop("host.name", None) or resource_attrs.pop(
            "host.hostname", None
        )

        for sl in rl.scope_logs:
            scope_name = sl.scope.name if sl.HasField("scope") else ""

            for rec in sl.log_records:
                ts_ns = rec.time_unix_nano or rec.observed_time_unix_nano
                metric_name = f"{source}.log.count"
                tags = _build_tags(
                    source, metric_name, "1", hostname,
                    resource_attrs, rec.attributes, scope_name,
                )
                tags["severity"] = _severity_bucket(rec.severity_number)
                if rec.severity_text:
                    tags["severity_text"] = rec.severity_text

                records.append(_build_record(
                    metric_name, 1.0, tags, _ts_from_nanos(ts_ns),
                ))

    return records


@router.post("/v1/logs")
async def receive_logs(request: Request, _auth: None = Depends(require_ingest_token)) -> Response:
    """OTLP/HTTP logs endpoint.

    Logs are converted to counters keyed by source+severity. This is the
    minimum useful translation for alarm rules; anyone needing the actual
    log body should send those to a logs store (Loki, Elasticsearch) instead.
    """
    body = await request.body()
    if not body:
        return Response(
            content=ExportLogsServiceResponse().SerializeToString(),
            media_type="application/x-protobuf",
        )

    req = ExportLogsServiceRequest()
    try:
        req.ParseFromString(body)
    except Exception as e:
        global _otlp_parse_errors
        _otlp_parse_errors += 1
        logger.warning(f"OTLP logs parse error: {e}")
        return Response(status_code=400, content=b"protobuf parse error")

    try:
        records = _flatten_logs(req)
    except Exception as e:
        logger.exception(f"OTLP logs flatten error: {e}")
        return Response(status_code=400, content=b"flatten error")

    if records and _db is not None:
        try:
            _db.write_metrics_batch(records)
        except Exception as e:
            global _otlp_write_errors
            _otlp_write_errors += 1
            logger.exception(f"OTLP logs InfluxDB write failed: {e}")
            return Response(status_code=500, content=b"db write failed")

    _seed_cache(records)
    global _otlp_logs_seen, _otlp_log_batches
    _otlp_logs_seen += len(records)
    _otlp_log_batches += 1

    logger.debug(f"OTLP logs ingested: {len(records)} records")
    return Response(
        content=ExportLogsServiceResponse().SerializeToString(),
        media_type="application/x-protobuf",
    )
