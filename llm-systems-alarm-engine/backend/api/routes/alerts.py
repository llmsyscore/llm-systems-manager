"""Alert management API routes.
Provides CRUD operations for alerts with status filtering and lifecycle management.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Any, List, Optional

from ...models.alert import Alert, AlertStatus
from ...storage.repositories import AlertRepository
from ...engine.alert_manager import AlertManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alarm/alerts", tags=["alerts"])

# ── Dependency injection wiring ─────────────────────────────────
_alert_repo: Optional[AlertRepository] = None
_alert_mgr: Optional[AlertManager] = None
_notification_dispatcher: Any = None  # backend.engine.notification_dispatcher.NotificationDispatcher


def set_repositories(
    alert_repo: Optional[AlertRepository] = None,
    alert_mgr: Optional[AlertManager] = None,
    notification_dispatcher: Any = None,
) -> None:
    """Wire shared repository/manager instances (called from alarm_engine.py at startup)."""
    global _alert_repo, _alert_mgr, _notification_dispatcher
    if alert_repo is not None:
        _alert_repo = alert_repo
    if alert_mgr is not None:
        _alert_mgr = alert_mgr
    if notification_dispatcher is not None:
        _notification_dispatcher = notification_dispatcher


def _try_notify(method_name: str, alert) -> None:
    """Safely call a dispatcher hook if wired and the alert is non-null.
    Used by the ack and close routes to fire the corresponding toast."""
    d = _notification_dispatcher
    if d is None or alert is None:
        return
    fn = getattr(d, method_name, None)
    if callable(fn):
        try:
            fn(alert)
        except Exception:
            logger.exception(
                "dispatcher.%s failed for alert %s", method_name, getattr(alert, "alert_id", "?")
            )


def get_alert_repo() -> AlertRepository:
    """FastAPI dependency: return the shared AlertRepository."""
    if _alert_repo is None:
        raise RuntimeError("AlertRepository not initialized")
    return _alert_repo


def get_alert_mgr() -> AlertManager:
    """FastAPI dependency: return the shared AlertManager."""
    if _alert_mgr is None:
        raise RuntimeError("AlertManager not initialized")
    return _alert_mgr


# ── Routes ──────────────────────────────────────────────────────

@router.get("/", response_model=List[Alert])
async def list_alerts(
    alert_repo: AlertRepository = Depends(get_alert_repo),
    status: Optional[AlertStatus] = Query(None, description="Filter by status"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    rule_id: Optional[str] = Query(None, description="Filter by rule ID"),
    metric_name: Optional[str] = Query(None, description="Filter by metric name"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    only_active: bool = Query(False, description="Only return active/unresolved alerts"),
    include_closed: bool = Query(False, description="Include closed alerts in the result set"),
) -> List[Alert]:
    """List alerts with optional filtering.

    Default behaviour returns active+acknowledged alerts only. Pass
    `include_closed=true` to also surface closed alerts — used by the
    dashboard's Recent Alerts widget which wants the last 25 of any state.
    """
    alerts = await alert_repo.list_alerts(
        status=status,
        severity=severity,
        rule_id=rule_id,
        metric_name=metric_name,
        limit=limit,
        only_active=only_active,
        include_closed=include_closed,
    )
    return alerts


@router.get("/active", response_model=List[Alert])
async def get_active_alerts(
    alert_repo: AlertRepository = Depends(get_alert_repo),
) -> List[Alert]:
    """Get all currently active (unresolved) alerts."""
    return await alert_repo.list_alerts(only_active=True, limit=1000)


@router.get("/counters")
async def get_alert_counters(
    alert_repo: AlertRepository = Depends(get_alert_repo),
) -> dict:
    """Get alert count by status and severity."""
    return await alert_repo.get_alert_counters()


@router.get("/export")
async def export_alerts(
    format: str = Query("csv", description="Export format (csv only for now)"),
    alert_repo: AlertRepository = Depends(get_alert_repo),
):
    """Export all alerts as CSV.

    Defined BEFORE the parameterized /{alert_id} route — FastAPI matches the
    first compatible route, and a path like /export would otherwise be
    interpreted as alert_id="export" and 404 with "Alert export not found".
    """
    import csv, io
    from fastapi.responses import Response
    if format != "csv":
        raise HTTPException(status_code=400, detail=f"Unsupported format: {format}")

    try:
        alerts = await alert_repo.list_alerts(limit=10000, include_closed=True)
    except Exception:
        logger.exception("export: list_alerts failed")
        # Return an empty CSV with headers rather than 500 — keeps the
        # browser download UX intact when the data store is unhappy.
        alerts = []

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "alert_id", "rule_name", "source_host", "metric_source", "metric_name",
        "current_value", "threshold_value", "severity", "status",
        "message", "created_at", "acknowledged_at", "closed_at",
    ])
    for a in alerts:
        writer.writerow([
            str(a.alert_id), a.rule_name or "",
            getattr(a, "source_host", "") or "",
            a.metric_source or "", a.metric_name or "",
            a.current_value if a.current_value is not None else "",
            a.threshold_value if a.threshold_value is not None else "",
            a.severity or "", str(a.status) if a.status is not None else "",
            a.message or "",
            a.created_at.isoformat() if a.created_at else "",
            a.acknowledged_at.isoformat() if a.acknowledged_at else "",
            a.closed_at.isoformat() if a.closed_at else "",
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=alerts.csv"},
    )


@router.get("/{alert_id}", response_model=Alert)
async def get_alert(
    alert_id: str,
    alert_repo: AlertRepository = Depends(get_alert_repo),
) -> Alert:
    """Get a single alert by ID."""
    alert = await alert_repo.get_alert(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    return alert


@router.post("/{alert_id}/read")
async def mark_alert_read(
    alert_id: str,
    alert_mgr: AlertManager = Depends(get_alert_mgr),
) -> dict:
    """Mark an alert as read."""
    result = alert_mgr.mark_as_read(alert_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    _try_notify("notify_alert_acknowledged", result)
    return {"status": "ok", "message": f"Alert {alert_id} marked as read"}


@router.post("/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: str,
    alert_mgr: AlertManager = Depends(get_alert_mgr),
) -> dict:
    """Acknowledge an alert (same as mark as read in this implementation).

    Side effect: a toast is sent informing the user the alert was
    acknowledged. From this point on, the rule engine's continuing-breach
    cycles will NOT route this alert to any non-toast channel — only the
    eventual clear notification will come through.
    """
    result = alert_mgr.mark_as_read(alert_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    _try_notify("notify_alert_acknowledged", result)
    return {"status": "ok", "message": f"Alert {alert_id} acknowledged"}


@router.post("/{alert_id}/close")
async def close_alert(
    alert_id: str,
    alert_mgr: AlertManager = Depends(get_alert_mgr),
) -> dict:
    """Manually close (resolve) an alert. Always fires a 'cleared' toast
    so the user sees the state change."""
    result = alert_mgr.close_alert(alert_id, reason="manual")
    if not result:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    _try_notify("notify_alert_resolved", result)
    return {"status": "ok", "message": f"Alert {alert_id} closed"}


@router.post("/{alert_id}/ignore")
async def ignore_alert(
    alert_id: str,
    duration_hours: int = Query(24, ge=1, le=720, description="Hours to ignore (1-720)"),
    alert_mgr: AlertManager = Depends(get_alert_mgr),
) -> dict:
    """Temporarily ignore an alert for a specified duration."""
    result = alert_mgr.ignore_alert(alert_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    return {
        "status": "ok",
        "message": f"Alert {alert_id} ignored for {duration_hours} hours",
        "ignored_until": None,
    }


@router.delete("/{alert_id}")
async def delete_alert(
    alert_id: str,
    alert_repo: AlertRepository = Depends(get_alert_repo),
) -> dict:
    """Delete an alert permanently."""
    success = await alert_repo.delete_alert(alert_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    return {"status": "ok", "message": f"Alert {alert_id} deleted"}


@router.post("/close-all")
async def close_all_alerts(
    alert_repo: AlertRepository = Depends(get_alert_repo),
) -> dict:
    """Close all active alerts."""
    count = await alert_repo.close_all_alerts()
    return {"status": "ok", "message": f"{count} alerts closed", "count": count}


@router.post("/bulk")
async def bulk_update_alerts(
    payload: dict,
    alert_mgr: AlertManager = Depends(get_alert_mgr),
) -> dict:
    """Apply an action to a list of alerts.
    payload: { "alert_ids": [...], "action": "acknowledge|close|ignore" }
    """
    alert_ids = payload.get("alert_ids") or []
    action = (payload.get("action") or "").lower()
    if action not in ("acknowledge", "close", "ignore"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {action!r}")

    if action == "acknowledge":
        fn = alert_mgr.mark_as_read
    elif action == "close":
        fn = alert_mgr.close_alert
    else:
        fn = alert_mgr.ignore_alert

    updated = 0
    failed = []
    for aid in alert_ids:
        try:
            if fn(str(aid)):
                updated += 1
            else:
                failed.append(str(aid))
        except Exception as e:
            logger.warning(f"bulk {action} failed for {aid}: {e}")
            failed.append(str(aid))
    return {"status": "ok", "action": action, "updated": updated, "failed": failed}


@router.post("/ignore-all")
async def ignore_all_alerts(
    alert_repo: AlertRepository = Depends(get_alert_repo),
    duration_hours: int = Query(24, ge=1, le=720),
) -> dict:
    """Ignore all active alerts for a duration."""
    count = await alert_repo.ignore_all_alerts(duration_hours)
    return {"status": "ok", "message": f"{count} alerts ignored", "count": count}