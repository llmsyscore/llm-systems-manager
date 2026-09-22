"""OTLP/HTTP receiver for metrics, traces, and logs.

OpenClaw's diagnostics-otel plugin pushes OpenTelemetry telemetry here over
HTTP/protobuf. We decode the standard OTLP envelopes and convert every signal
into the same metric-record shape so the alarm engine's rule evaluator can
operate on traces and logs the same way it does on native metrics:

  /v1/metrics  → counters / gauges / histograms (sum + count) → InfluxDB
  /v1/traces   → one duration_ms metric per span (name = "<span>.duration_ms")
  /v1/logs     → one count=1 metric per log record (name = "<source>.log.count")

OTEL attributes (resource + data-point + span/log) become tags under the
policy in `_classify()`: identifier / network / free-text keys are dropped,
numeric attributes become fields, and the remaining string tags are capped
per key and per point so the InfluxDB series count stays bounded (#1080).
Span status and log severity become tags on the synthesized metric, which is
what makes "alert on error rate" rules possible without a separate logs
storage layer.
"""

from __future__ import annotations

import functools
import logging
import math
import re
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
_otlp_tags_dropped   = 0   # attribute tags removed by key policy or per-point cap
_otlp_tags_capped    = 0   # tag values replaced with "other" past the per-key cap
_otlp_attr_fields    = 0   # numeric attributes stored as fields instead of tags
_hb_task = None

# ── Tag policy (#1080) ───────────────────────────────────────────────────
# Keys are matched on a normalized form: dots/camelCase → snake_case words.
_IDENT_WORDS = (
    "id|ids|uuid|guid|hash|token|key|secret|password|credential|session|"
    "trace|span|frame|pid|tid|instance|correlation|"
    "addr|address|port|peer|endpoint|ip|url|uri|path|host|remote|local|"
    "user_agent|message|messages|body|content|value|command|args|arguments|"
    "prompt|text|description|stack|stacktrace|exception|parents|definitions"
)
_MEASURE_WORDS = (
    "ms|s|sec|secs|bytes|chars|count|tokens|ratio|budget|lineno|line|"
    "blocks|images|elapsed|duration|latency|time|timestamp|ts"
)
# Compound identifier names written without a separator (sessionid, apikey).
_IDENT_SUFFIXES = (
    "uuid|guid|token|secret|password|credential|apikey|sessionid|userid|"
    "traceid|spanid|requestid|instanceid|clientid|deviceid|eventid|frameid"
)
_IDENT_RE = re.compile(rf"(^|_)({_IDENT_WORDS})(_|$)|({_IDENT_SUFFIXES})$")
_MEASURE_RE = re.compile(rf"(^|_)({_MEASURE_WORDS})$")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# Bounded dimensions the identifier pattern would otherwise catch.
_BUILTIN_ALLOW = frozenset({
    "gen_ai_token_type", "gen_ai_request_model", "gen_ai_response_model",
    "gen_ai_operation_name", "gen_ai_provider_name", "gen_ai_system",
    "gen_ai_tool_name", "error_type", "http_request_method",
    "http_response_status_code", "http_route", "rpc_method", "rpc_service",
    "service_version", "service_namespace", "deployment_environment",
    "os_type", "host_arch",
    "openclaw_security_policy_id", "openclaw_security_control_id",
})
# Fixed tag keys the receiver sets itself; attributes may not overwrite them.
_RESERVED_TAGS = frozenset({"source", "metric_name", "unit", "hostname", "scope",
                            "status", "span_kind", "severity", "severity_text"})
_MAX_TRACKED_KEYS = 1024
_CAPPED_VALUE = "other"
# Per-key distinct values seen this process; drives the "other" substitution.
_seen_values: dict[str, set[str]] = {}


def reset_policy_state() -> None:
    """Forget seen tag values and zero the policy counters (tests)."""
    global _otlp_tags_dropped, _otlp_tags_capped, _otlp_attr_fields
    _seen_values.clear()
    _otlp_tags_dropped = _otlp_tags_capped = _otlp_attr_fields = 0


def _normalize_key(key: str) -> str:
    return _CAMEL_RE.sub("_", key).replace(".", "_").replace(" ", "_").replace("-", "_").lower()


def _policy():
    from config.unified_config import settings as _settings
    return _settings.alarm_engine.otlp


def _classify(key: str, cfg) -> str:
    """Return "allow", "deny", "measure" or "tag" for one attribute key."""
    return _classify_cached(key, tuple(cfg.tag_allow), tuple(cfg.tag_deny))


@functools.lru_cache(maxsize=4096)
def _classify_cached(key: str, allow: tuple, deny: tuple) -> str:
    nk = _normalize_key(key)
    tk = _safe_tag(key)
    if tk in _RESERVED_TAGS or key in deny or tk in deny:
        return "deny"
    if key in allow or tk in allow or nk in _BUILTIN_ALLOW:
        return "allow"
    if _MEASURE_RE.search(nk):
        return "measure"
    if _IDENT_RE.search(nk):
        return "deny"
    return "tag"


def _cap_value(key: str, value: str, cap: int) -> str:
    """Return `value`, or "other" once `key` has `cap` distinct values."""
    global _otlp_tags_capped
    seen = _seen_values.get(key)
    if seen is None:
        if len(_seen_values) >= _MAX_TRACKED_KEYS:
            _otlp_tags_capped += 1
            return _CAPPED_VALUE
        seen = _seen_values[key] = set()
    if value in seen:
        return value
    if len(seen) < cap:
        seen.add(value)
        return value
    _otlp_tags_capped += 1
    return _CAPPED_VALUE


def _apply_attr(key: str, v: AnyValue, cfg, tags: dict[str, str], fields: dict[str, float],
                allowed: dict[str, str]) -> None:
    """Route one attribute into `allowed`/`tags` (string dims) or `fields` (numbers)."""
    global _otlp_tags_dropped, _otlp_attr_fields
    kind = v.WhichOneof("value")
    cls = _classify(key, cfg)
    tk = _safe_tag(key)
    if cls == "deny":
        _otlp_tags_dropped += 1
        return
    numeric = kind in ("int_value", "double_value")
    if cls == "measure" or (numeric and cls != "allow"):
        num = v.int_value if kind == "int_value" else v.double_value if kind == "double_value" else None
        if num is None and kind == "string_value":
            try:
                num = float(v.string_value)
            except ValueError:
                num = None
        if num is None or not math.isfinite(num):
            _otlp_tags_dropped += 1
            return
        tags.pop(tk, None)
        allowed.pop(tk, None)
        fields[tk] = float(num)
        _otlp_attr_fields += 1
        return
    val = _attr_value(v)
    fields.pop(tk, None)
    if cls == "allow":
        allowed[tk] = val
    elif kind == "bool_value":
        tags[tk] = val
    else:
        tags[tk] = _cap_value(tk, val, cfg.tag_value_cap)


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
    last_td = last_tc = last_af = 0
    while True:
        try:
            await _asyncio.sleep(_settings.alarm_engine.intervals.otlp_heartbeat_s)
            dm = _otlp_metrics_seen - last_m
            dt = _otlp_traces_seen  - last_t
            dl = _otlp_logs_seen    - last_l
            dpe = _otlp_parse_errors - last_pe
            dwe = _otlp_write_errors - last_we
            dtd = _otlp_tags_dropped - last_td
            dtc = _otlp_tags_capped - last_tc
            daf = _otlp_attr_fields - last_af
            last_m, last_t, last_l = _otlp_metrics_seen, _otlp_traces_seen, _otlp_logs_seen
            last_pe, last_we = _otlp_parse_errors, _otlp_write_errors
            last_td, last_tc, last_af = _otlp_tags_dropped, _otlp_tags_capped, _otlp_attr_fields
            line = (
                f"heartbeat otlp: metrics+{dm} traces+{dt} logs+{dl} "
                f"(total m={_otlp_metrics_seen} t={_otlp_traces_seen} l={_otlp_logs_seen} "
                f"batches m/t/l={_otlp_metric_batches}/{_otlp_trace_batches}/{_otlp_log_batches}) "
                f"parse_err+{dpe} write_err+{dwe} "
                f"tags dropped+{dtd} capped+{dtc} fields+{daf} keys={len(_seen_values)}"
            )
            if dm or dt or dl or dpe or dwe:
                logger.info(line)
                _write_self_metrics(dtd, dtc, daf)
            else:
                logger.debug(line)
        except _asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("OTLP heartbeat tick failed: %s", e, exc_info=True)


def _write_self_metrics(dropped: int, capped: int, fields: int) -> None:
    """Write the per-tick tag-policy counters as `otlp-receiver` metric points."""
    if _db is None:
        return
    import socket
    try:
        host = socket.gethostname()
    except Exception:
        host = "localhost"
    ts = datetime.now(timezone.utc)
    records = [
        _build_record(name, float(val), {"source": "otlp-receiver", "unit": "1", "hostname": host}, ts)
        for name, val in (
            ("otlp.tags_dropped", dropped),
            ("otlp.tags_capped", capped),
            ("otlp.attr_fields", fields),
            ("otlp.tag_keys_tracked", len(_seen_values)),
        )
    ]
    try:
        _db.write_metrics_batch(records)
        _seed_cache(records)
    except Exception as e:
        logger.debug("OTLP self-metric write failed: %s", e)


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


_RESOURCE_KEYS = ("service.name", "host.name", "host.hostname")


def _split_resource(attrs) -> tuple[str, Optional[str], list]:
    """Pull source + hostname out of the resource attributes; return the rest."""
    found = {kv.key: _attr_value(kv.value) for kv in attrs if kv.key in _RESOURCE_KEYS}
    rest = [kv for kv in attrs if kv.key not in _RESOURCE_KEYS]
    source = found.get("service.name") or "openclaw-otel"
    hostname = found.get("host.name") or found.get("host.hostname") or None
    return source, hostname, rest


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
        # service.name -> source so existing alarm rules can target the producer
        source, hostname, resource_attrs = _split_resource(rm.resource.attributes)

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
    resource_attrs,
    scope_name: str,
) -> list[dict[str, Any]]:
    value = dp.as_double if dp.HasField("as_double") else float(dp.as_int)
    tags, fields = _build_tags(source, name, unit, hostname, resource_attrs, dp.attributes, scope_name)
    return [_build_record(name, value, tags, _ts_from_nanos(dp.time_unix_nano), fields)]


def _emit_histogram_point(
    dp,
    name: str,
    unit: str,
    source: str,
    hostname: Optional[str],
    resource_attrs,
    scope_name: str,
) -> list[dict[str, Any]]:
    """Phase 1 emits only sum and count. Bucket counts (for percentile
    queries) are deferred to Phase 2 — they multiply record volume by the
    bucket count and we want to validate the basic flow first.
    """
    out: list[dict[str, Any]] = []
    ts = _ts_from_nanos(dp.time_unix_nano)
    base_tags, fields = _build_tags(source, name, unit, hostname, resource_attrs, dp.attributes, scope_name)

    if dp.HasField("sum"):
        out.append(_build_record(f"{name}.sum", dp.sum, dict(base_tags), ts, dict(fields)))
    out.append(_build_record(f"{name}.count", float(dp.count), dict(base_tags), ts, dict(fields)))
    return out


def _build_tags(
    source: str,
    metric_name: str,
    unit: str,
    hostname: Optional[str],
    resource_attrs,
    dp_attrs,
    scope_name: str,
) -> tuple[dict[str, str], dict[str, float]]:
    """Build the tag set and attribute fields for one InfluxDB record.

    Resource then data-point attributes pass through the tag policy; the
    data-point ones win on key collision. Allowlisted keys survive the
    per-point `max_tags` cap ahead of the rest.
    """
    global _otlp_tags_dropped
    cfg = _policy()
    tags: dict[str, str] = {
        "source": source,
        "metric_name": metric_name,
        "unit": unit,
    }
    if hostname:
        tags["hostname"] = hostname
    if scope_name:
        tags["scope"] = scope_name
    allowed: dict[str, str] = {}
    attr_tags: dict[str, str] = {}
    fields: dict[str, float] = {}
    for kv in resource_attrs:
        _apply_attr(kv.key, kv.value, cfg, attr_tags, fields, allowed)
    for kv in dp_attrs:
        _apply_attr(kv.key, kv.value, cfg, attr_tags, fields, allowed)
    room = max(0, cfg.max_tags - len(allowed))
    if len(attr_tags) > room:
        _otlp_tags_dropped += len(attr_tags) - room
        attr_tags = dict(list(attr_tags.items())[:room])
    tags.update(allowed)
    tags.update(attr_tags)
    return tags, fields


def _safe_tag(key: str) -> str:
    """InfluxDB allows dots in tag keys but our rule UI is friendlier with
    underscores; normalize so attribute names don't collide with reserved
    Flux tokens.
    """
    return key.replace(".", "_").replace(" ", "_")


def _build_record(metric_name: str, value: float, tags: dict[str, str], ts: datetime,
                  fields: Optional[dict[str, float]] = None) -> dict[str, Any]:
    tags["metric_name"] = metric_name  # overwrite so histogram .sum/.count are distinct
    rec_fields = {k: v for k, v in (fields or {}).items() if k != "value"}
    rec_fields["value"] = float(value)
    return {
        "measurement": "metrics",
        "tags": tags,
        "fields": rec_fields,
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
        source, hostname, resource_attrs = _split_resource(rs.resource.attributes)

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
                tags, fields = _build_tags(
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
                    metric_name, duration_ms, tags, _ts_from_nanos(end_ns), fields,
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
        source, hostname, resource_attrs = _split_resource(rl.resource.attributes)

        for sl in rl.scope_logs:
            scope_name = sl.scope.name if sl.HasField("scope") else ""

            for rec in sl.log_records:
                ts_ns = rec.time_unix_nano or rec.observed_time_unix_nano
                metric_name = f"{source}.log.count"
                tags, fields = _build_tags(
                    source, metric_name, "1", hostname,
                    resource_attrs, rec.attributes, scope_name,
                )
                tags["severity"] = _severity_bucket(rec.severity_number)
                if rec.severity_text:
                    tags["severity_text"] = rec.severity_text

                records.append(_build_record(
                    metric_name, 1.0, tags, _ts_from_nanos(ts_ns), fields,
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
