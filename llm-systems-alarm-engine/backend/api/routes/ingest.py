"""Generic external alarm ingestion endpoint.

POST /api/alarm/ingest accepts payloads from:
  - InfluxDB notification rules (key-sniffed by `_check_id`)
  - Grafana alerting webhooks (key-sniffed by `alerts` array + `status`)
  - Generic JSON or YAML (best-effort field mapping)

Each format is mapped to AlertCreate and pushed through alert_manager.process_alert().
A synthetic deterministic rule_id (UUID5) is derived per logical alert so the
existing rule-id-based deduplication works for external alerts (otherwise every
external alert would carry rule_id=None and collapse together).
"""

import json
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ...engine.alert_manager import AlertManager
from ...models.alert import AlertCreate
from ..auth import require_ingest_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alarm", tags=["ingest"])

# ── DI wiring ──────────────────────────────────────────────────
_alert_mgr: Optional[AlertManager] = None

# Stable namespace so the same logical alert always maps to the same rule_id.
_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def set_alert_manager(alert_mgr: AlertManager) -> None:
    global _alert_mgr
    _alert_mgr = alert_mgr


def get_alert_mgr() -> AlertManager:
    if _alert_mgr is None:
        raise RuntimeError("AlertManager not initialized")
    return _alert_mgr


# ── Helpers ────────────────────────────────────────────────────

_SEVERITY_MAP = {
    "ok": None, "okay": None, "resolved": None,
    "info": "info", "information": "info", "informational": "info", "low": "info",
    "warn": "warning", "warning": "warning", "medium": "warning", "moderate": "warning",
    "crit": "critical", "critical": "critical", "high": "critical",
    "error": "critical", "fatal": "critical", "emergency": "critical",
}


def _normalize_severity(raw: Any, default: str = "warning") -> Optional[str]:
    """Return canonical severity string, or None if the input means 'resolved/ok'."""
    if raw is None:
        return default
    key = str(raw).strip().lower()
    if key in _SEVERITY_MAP:
        return _SEVERITY_MAP[key]
    return default


def _stable_rule_id(*parts: Any) -> uuid.UUID:
    """Deterministic UUID for an external logical alert (so dedup works)."""
    return uuid.uuid5(_NAMESPACE, "|".join(str(p) for p in parts if p is not None))


# ── Format mappers ─────────────────────────────────────────────

def _map_influxdb(payload: dict) -> Optional[AlertCreate]:
    """Map InfluxDB check notification → AlertCreate.

    Returns None if level is 'ok' (resolved) — we don't create alerts for those.
    """
    level = payload.get("_level")
    severity = _normalize_severity(level, default="warning")
    if severity is None:
        logger.info("influxdb: skipping resolved/ok notification check=%s", payload.get("_check_name"))
        return None

    check_name = payload.get("_check_name") or payload.get("_check_id") or "InfluxDB Check"
    measurement = payload.get("_source_measurement") or "influxdb"
    message = payload.get("_message") or f"InfluxDB check '{check_name}' triggered"

    # First non-underscore-prefixed numeric field is the metric value.
    current_value = 0.0
    metric_name = check_name
    for k, v in payload.items():
        if k.startswith("_") or k in ("host",):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            current_value = float(v)
            metric_name = k
            break

    return AlertCreate(
        rule_id=_stable_rule_id("influxdb", payload.get("_check_id") or check_name, metric_name),
        rule_name=check_name,
        metric_source=str(measurement),
        metric_name=str(metric_name),
        current_value=current_value,
        threshold_value=0.0,
        severity=severity,
        message=str(message),
        source_host=payload.get("host") or payload.get("_source_host"),
    )


def _map_grafana(payload: dict) -> list[AlertCreate]:
    """Map Grafana alerting webhook → list[AlertCreate] (one per firing alert)."""
    out: list[AlertCreate] = []
    for entry in payload.get("alerts") or []:
        if not isinstance(entry, dict):
            continue
        if (entry.get("status") or "").lower() == "resolved":
            continue
        labels = entry.get("labels") or {}
        annotations = entry.get("annotations") or {}
        name = labels.get("alertname") or payload.get("ruleName") or "Grafana Alert"
        severity = _normalize_severity(labels.get("severity"), default="warning")
        if severity is None:
            continue
        message = (
            annotations.get("summary")
            or annotations.get("description")
            or annotations.get("message")
            or f"Grafana alert '{name}' is firing"
        )
        host = labels.get("instance") or labels.get("host") or labels.get("hostname")
        try:
            value = float(entry.get("value")) if entry.get("value") is not None else 0.0
        except (TypeError, ValueError):
            value = 0.0
        out.append(AlertCreate(
            rule_id=_stable_rule_id("grafana", name, host or ""),
            rule_name=name,
            metric_source="grafana",
            metric_name=labels.get("metric") or name,
            current_value=value,
            threshold_value=0.0,
            severity=severity,
            message=str(message),
            source_host=host,
        ))
    return out


def _first_present(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _map_generic(payload: dict) -> Optional[AlertCreate]:
    """Best-effort generic mapper. Returns None if nothing usable found."""
    name = _first_present(payload, "name", "rule_name", "alert_name", "alertname", "title")
    message = _first_present(payload, "message", "description", "summary", "text", "body")
    severity_raw = _first_present(payload, "severity", "level", "priority")
    severity = _normalize_severity(severity_raw, default="warning")
    if severity is None:
        return None
    metric_source = _first_present(payload, "source", "metric_source", "category") or "generic"
    metric_name = _first_present(payload, "metric", "metric_name") or name or "unknown"
    host = _first_present(payload, "host", "hostname", "source_host", "instance")

    raw_val = _first_present(payload, "value", "current_value", "metric_value")
    try:
        current_value = float(raw_val) if raw_val is not None else 0.0
    except (TypeError, ValueError):
        current_value = 0.0

    raw_threshold = _first_present(payload, "threshold", "threshold_value", "limit")
    try:
        threshold_value = float(raw_threshold) if raw_threshold is not None else 0.0
    except (TypeError, ValueError):
        threshold_value = 0.0

    if not name and not message:
        # Nothing useful at all — refuse rather than create a noise alert.
        return None

    if not message:
        message = f"External alert: {name}"
    if not name:
        name = str(metric_name)

    return AlertCreate(
        rule_id=_stable_rule_id("generic", metric_source, metric_name, host or ""),
        rule_name=str(name),
        metric_source=str(metric_source),
        metric_name=str(metric_name),
        current_value=current_value,
        threshold_value=threshold_value,
        severity=severity,
        message=str(message),
        source_host=str(host) if host else None,
    )


# ── Body parsing (JSON or YAML) ────────────────────────────────

async def _parse_body(request: Request) -> dict:
    """Parse request body as JSON, falling back to YAML if Content-Type indicates it."""
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty request body")
    ctype = (request.headers.get("content-type") or "").lower()

    if "yaml" in ctype:
        try:
            import yaml  # type: ignore
        except ImportError:
            raise HTTPException(status_code=415, detail="YAML payloads require PyYAML to be installed")
        try:
            data = yaml.safe_load(raw)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")
    else:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # Try YAML as a last resort — many generic webhook tools emit YAML-ish data.
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(raw)
            except Exception as ye:
                logger.warning("ingest payload neither JSON nor YAML: json_err=%s yaml_err=%s", e, ye)
                raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Top-level payload must be a JSON/YAML object")
    return data


# ── Route ──────────────────────────────────────────────────────

@router.post("/ingest")
async def ingest_external_alert(
    request: Request,
    alert_mgr: AlertManager = Depends(get_alert_mgr),
    _auth: None = Depends(require_ingest_token),
) -> dict:
    """Receive an external alarm and route it into the alarm engine.

    Auto-detects InfluxDB / Grafana / generic formats by inspecting the payload.
    """
    payload = await _parse_body(request)

    # ── Format detection ──
    if "_check_id" in payload or "_check_name" in payload:
        fmt = "influxdb"
        creates = [_map_influxdb(payload)]
    elif isinstance(payload.get("alerts"), list) and ("status" in payload or "receiver" in payload):
        fmt = "grafana"
        creates = _map_grafana(payload)
    else:
        fmt = "generic"
        creates = [_map_generic(payload)]

    # Drop None entries (e.g. resolved/ok skip)
    creates = [c for c in creates if c is not None]

    if not creates:
        logger.info("ingest: %s payload accepted but produced no alerts (resolved or unmappable)", fmt)
        # Truncate logged sample to avoid blowing up the log file on huge payloads.
        sample = json.dumps(payload)[:500]
        logger.debug("ingest: skipped payload sample: %s", sample)
        return {"status": "skipped", "format": fmt, "reason": "resolved or no usable fields"}

    created_ids: list[str] = []
    deduped = 0
    for ac in creates:
        try:
            alert = alert_mgr.process_alert(ac)
        except Exception as e:
            logger.exception("ingest: process_alert failed for %s payload", fmt)
            raise HTTPException(status_code=500, detail=f"Failed to process alert: {e}")
        if alert is None:
            deduped += 1
        else:
            created_ids.append(str(alert.alert_id))

    logger.info(
        "ingest: format=%s created=%d deduped=%d", fmt, len(created_ids), deduped,
    )
    return {
        "status": "ok",
        "format": fmt,
        "created": len(created_ids),
        "deduplicated": deduped,
        "alert_ids": created_ids,
    }
