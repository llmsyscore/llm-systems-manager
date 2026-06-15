"""
================================================================================
alarm_engine.py  —  LLM Systems Alarm Engine  (version: see __version__ below)
================================================================================
Main FastAPI application entry point for the Alarm Engine.

Combines all components:
- REST API routes (alerts, rules, metrics, notifications)
- WebSocket endpoint for live updates
- Background task for periodic rule evaluation
- InfluxDB persistence sync
- In-memory cache with automatic warm-reload
"""

# Defer annotation evaluation on every Python version (matches the manager
# and agent). Pre-3.14 evaluates module-level annotations at import time,
# so a missing `from typing import Any/Optional` would crash on Debian 13.
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Bootstrap — make the shared config schema/loader importable.
# Uvicorn launches this module from llm-systems-alarm-engine/ as CWD, so
# Python doesn't put the repo root on sys.path. Add it so the top-level
# `config` package resolves the same way the manager does.
# ---------------------------------------------------------------------------
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from config.unified_config import settings, CONFIG_PATH  # noqa: E402
from ._best_effort import best_effort
from .api.websocket import WebSocketConnectionManager, init_manager
from .api.routes import alerts, ingest, metrics, notifications, rules
from .receivers import otlp_receiver
from .storage.repositories import (
    AlertRepository,
    MetricRepository,
    NotificationRepository,
    RuleRepository,
)
from .storage.ae_alarms_db import AeAlarmsDB
from .storage.ae_settings_db import AeSettingsDB
from .storage.cache import Cache
from .storage.influxdb_client import InfluxDBClient

# ---------------------------------------------------------------------------
# Version — single source of truth. Update this string only; the startup
# banner and FastAPI OpenAPI metadata both read from it. Bump the suffix
# (-1, -2, …) for same-day iterations; roll the date for a new day's first
# change.
# ---------------------------------------------------------------------------
__version__ = "v2026.06.15-2"
from .storage import influx_monitor as _influx_monitor
from .models.alarm_rule import (
    AlarmRuleCreate,
    RuleSpecificConfig,
    RuleType,
    Severity,
    ThresholdConfig,
)
from .engine.rule_engine import RuleEngine
from .engine.alert_manager import AlertManager
from .engine.notification_dispatcher import NotificationDispatcher

# ---------------------------------------------------------------------------
# Logging configuration — sourced from settings.paths + settings.logging.
# Per-service override: settings.alarm_engine.log_level wins over [logging].level.
# ---------------------------------------------------------------------------
LOG_DIR   = settings.paths.log_dir
LOG_FILE  = os.path.join(LOG_DIR, "llm-systems-alarm-engine.log")

os.makedirs(LOG_DIR, exist_ok=True)

_log_formatter = logging.Formatter(
    settings.logging.format,
    datefmt=settings.logging.datefmt,
)

_log_level = getattr(logging, settings.alarm_engine.log_level.upper(), logging.INFO)

logger = logging.getLogger("llm-systems-alarm-engine")
logger.setLevel(_log_level)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE,
    maxBytes=settings.paths.log_max_bytes,
    backupCount=settings.paths.log_backup_count,
    encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)

# Attach handlers to BOTH the named logger (for `logger.info` calls inside this
# module) AND the root logger so every submodule's `logging.getLogger(__name__)`
# propagates into the same file.
_root = logging.getLogger()
_root.setLevel(_log_level)
# Avoid duplicate handlers on reload
if not any(isinstance(h, logging.StreamHandler) and getattr(h, "_lsm_marker", False) for h in _root.handlers):
    _console_handler._lsm_marker = True
    _root.addHandler(_console_handler)
try:
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) and getattr(h, "_lsm_marker", False) for h in _root.handlers):
        _file_handler._lsm_marker = True
        _root.addHandler(_file_handler)
except PermissionError:
    # If we can't write to /var/log, log silently — don't crash the app
    pass

# Silence noisy third-party debug chatter that would otherwise reach the file
# now that we've attached handlers at the root.
for _noisy in ("urllib3", "asyncio", "uvicorn.access", "Rx"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ── Global dependency injection targets ─────────────────────────

rule_repo: Optional[RuleRepository] = None
alert_repo: Optional[AlertRepository] = None
metric_repo: Optional[MetricRepository] = None
notification_repo: Optional[NotificationRepository] = None
cache: Optional[Cache] = None
db_client: Optional[InfluxDBClient] = None
ae_settings_db: Optional[AeSettingsDB] = None
ae_alarms_db: Optional[AeAlarmsDB] = None
rule_engine: Optional[RuleEngine] = None
alert_manager: Optional[AlertManager] = None
notification_dispatcher: Optional[NotificationDispatcher] = None
ws_manager: Optional[WebSocketConnectionManager] = None

# Most recent rule-eval cycle wall time in milliseconds, written by
# `_rule_evaluation_loop` and read by /health so the agent's self-monitor
# probe can include it as a metric without scraping logs. 0.0 until the
# first cycle completes.
_rule_eval_last_cycle_ms: float = 0.0
# TLS serving state, set by main() at launch and exposed via /health.
#   enabled: [alarm_engine].tls_enabled  ·  active: actually serving HTTPS
#   error:   set when enabled but the cert/key couldn't be loaded
_tls_status: dict = {"enabled": False, "active": False, "error": None}
_startup_ts: float = 0.0  # wall-clock seconds at startup; drives the shutdown banner's uptime


# Seed thresholds come from [alarm_engine.default_rules] in the TOML.
# Applied only at first boot when no rules exist; editable later via the UI.
_dr = settings.alarm_engine.default_rules
# Source/name pairs use the names the agent's metric_flatten layer actually
# emits — paths nested under `system` fall through the alias table and land
# as (source="system", name="<parent>_<field>"). Verified against InfluxDB
# series; rules using the older aliased names (cpu.temp_c, gpu.temp_c,
# ram.usage_percent) never matched any incoming points.
_DEFAULT_RULES: list[dict] = [
    {
        "name": "CPU temperature warning",
        "description": f"CPU package temperature exceeds {_dr.cpu_temp_critical}°C",
        "metric_source": "system",
        "metric_name": "cpu_temp_c",
        "severity": Severity.WARNING,
        "threshold": _dr.cpu_temp_critical,
        "unit": "°C",
    },
    {
        "name": "CPU usage warning",
        "description": f"CPU usage above {_dr.cpu_usage_critical}%",
        "metric_source": "system",
        "metric_name": "cpu_total",
        "severity": Severity.WARNING,
        "threshold": _dr.cpu_usage_critical,
        "unit": "%",
    },
    {
        "name": "GPU temperature critical",
        "description": f"GPU temperature exceeds {_dr.gpu_temp_critical}°C",
        "metric_source": "system",
        "metric_name": "gpu_temperature_c",
        "severity": Severity.CRITICAL,
        "threshold": _dr.gpu_temp_critical,
        "unit": "°C",
    },
    {
        "name": "GPU VRAM warning",
        "description": f"GPU VRAM usage above {_dr.gpu_vram_critical}%",
        "metric_source": "system",
        "metric_name": "gpu_vram_usage_percent",
        "severity": Severity.WARNING,
        "threshold": _dr.gpu_vram_critical,
        "unit": "%",
    },
    {
        "name": "RAM usage warning",
        "description": f"System RAM usage above {_dr.ram_usage_critical}%",
        "metric_source": "system",
        "metric_name": "ram_percent",
        "severity": Severity.WARNING,
        "threshold": _dr.ram_usage_critical,
        "unit": "%",
    },
]


def _seed_default_rules(repo: RuleRepository) -> None:
    """Idempotently seed a baseline set of threshold rules if none exist.

    raise_on_error=True is critical: without it, an InfluxDB outage that
    causes query_rules() to silently return [] would make us re-seed the 5
    defaults and overshadow any custom rules already in the bucket
    (Phase-3 migration loss mode). With it, an InfluxDB failure aborts
    the seed entirely — the loop will re-evaluate on the next startup.
    """
    try:
        existing = repo.get_all(raise_on_error=True)
    except Exception as e:
        logger.warning(f"Skipping default-rule seed; InfluxDB query failed: {e}")
        return
    existing_names = {r.name for r in existing}
    to_seed = [s for s in _DEFAULT_RULES if s["name"] not in existing_names]
    if not to_seed:
        logger.info(f"Skipping default-rule seed; all {len(_DEFAULT_RULES)} default rules already exist")
        return
    for spec in to_seed:
        try:
            create = AlarmRuleCreate(
                name=spec["name"],
                description=spec["description"],
                metric_source=spec["metric_source"],
                metric_name=spec["metric_name"],
                rule_type=RuleType.THRESHOLD_ABOVE,
                config=RuleSpecificConfig(
                    threshold=ThresholdConfig(
                        value=spec["threshold"],
                        upper=spec["threshold"],
                        unit=spec.get("unit"),
                    )
                ),
                severity=spec["severity"],
                enabled=True,
            )
            repo.create(create)
        except Exception as e:
            logger.warning(f"Failed to seed rule {spec['name']}: {e}")
    logger.info(f"Seeded {len(to_seed)} default alarm rules")


async def _on_startup() -> None:
    """Initialize all components and start background tasks."""
    global cache, rule_repo, alert_repo, metric_repo, notification_repo
    global rule_engine, alert_manager, notification_dispatcher, ws_manager
    global db_client, ae_settings_db, ae_alarms_db, _startup_ts

    _startup_ts = time.time()

    # ── Startup banner (mirrors the manager's style) ──────────────
    _ae = settings.alarm_engine
    _ix = settings.influxdb
    _smtp = settings.notifications.smtp
    smtp_host = (_smtp.server or "").strip() or "(not set)"
    smtp_user = (_smtp.user or "").strip() or "(not set)"
    logger.info("=" * 60)
    logger.info(f"LLM Systems Alarm Engine {__version__} starting")
    logger.info(f"  Config:         {CONFIG_PATH or '(none — using built-in defaults)'}")
    logger.info(f"  Listening on:   http://{_ae.host}:{_ae.port}")
    logger.info(f"  Manager URL:    {_ae.manager_url}")
    logger.info(f"  CORS origins:   {_ae.cors_origins}")
    logger.info(f"  InfluxDB:       http://{_ix.host}:{_ix.port}  org={_ix.org}")
    logger.info(f"    buckets:      metrics={_ix.metrics_bucket} "
                f"rollup={_ix.metrics_rollup_bucket}")
    logger.info("  SQLite:         rules/channels/configs/deliveries → ae_notif_rules.db")
    logger.info("                  alerts + history → ae_alarms.db")
    logger.info(f"  Eval interval:  {_ae.evaluation_interval}s")
    logger.info(f"  SMTP:           {smtp_host}:{_smtp.port}  user={smtp_user}")
    logger.info("=" * 60)

    # 1. Initialize cache
    cache = Cache()

    # 1b. Open the SQLite settings store (rules / channels / notification
    # configs / deliveries). Self-creates on first run; idempotent schema
    # bootstrap.
    ae_settings_db = AeSettingsDB.open(
        Path(__file__).resolve().parent.parent / "data" / "ae_notif_rules.db"
    )

    # 1c. Open the SQLite alarms store (alerts + alert history). Same
    # directory; separate file so wiping alarm history doesn't touch
    # rules/channels/configs.
    ae_alarms_db = AeAlarmsDB.open(
        Path(__file__).resolve().parent.parent / "data" / "ae_alarms.db"
    )

    # 2. Initialize InfluxDB client (optional — engine still works in cache-only mode).
    try:
        _ds = settings.alarm_engine.history.downsampling
        db_client = InfluxDBClient(
            url=f"http://{settings.influxdb.host}:{settings.influxdb.port}",
            org=settings.influxdb.org,
            metrics_bucket=settings.influxdb.metrics_bucket,
            metrics_rollup_bucket=settings.influxdb.metrics_rollup_bucket,
            metrics_token=settings.influxdb.tokens.metrics,
            metrics_rollup_token=settings.influxdb.tokens.metrics_rollup,
            admin_token=settings.influxdb.tokens.admin,
            rollup_enabled=_ds.rollup_enabled,
            rollup_measurement=_ds.rollup_measurement,
            rollup_every=_ds.rollup_every,
        )
        logger.info(
            f"InfluxDB client connected: {settings.influxdb.host}:{settings.influxdb.port} "
            f"org={settings.influxdb.org} "
            f"metrics={settings.influxdb.metrics_bucket} "
            f"rollup_bucket={settings.influxdb.metrics_rollup_bucket} "
            f"rollup={'on:'+_ds.rollup_measurement+'@'+_ds.rollup_every if _ds.rollup_enabled else 'off'}"
        )
        # Best-effort: create the server-side rollup task. Logged but
        # non-fatal — the read path falls back to raw scans if missing.
        try:
            db_client.ensure_rollup_task()
        except Exception as e:
            logger.warning(f"ensure_rollup_task failed (non-fatal): {e}")
    except Exception as e:
        logger.warning(f"InfluxDB client unavailable, running in cache-only mode: {e}")
        db_client = None

    # 3. Initialize repositories. Transactional state (rules / channels /
    # configs / deliveries / alerts) lives in SQLite; metrics + rollup stay
    # in InfluxDB via db_client.
    rule_repo = RuleRepository(cache=cache, settings_db=ae_settings_db)
    alert_repo = AlertRepository(alarms_db=ae_alarms_db)
    metric_repo = MetricRepository(cache=cache, db=db_client)
    notification_repo = NotificationRepository(cache=cache, settings_db=ae_settings_db)

    # 3b. Seed default alarm rules on first startup (idempotent)
    _seed_default_rules(rule_repo)

    # 3c. Wire the OTLP receiver to the cache + InfluxDB client so OpenClaw
    # telemetry pushed to /v1/metrics flows through the same path as native
    # alarm-engine metrics.
    otlp_receiver.configure(cache=cache, db=db_client)

    # 3d-pre. Full-history cache warm-up. Pulls the last
    # cache.metric_ttl_seconds (default 1 h) of points into the in-memory
    # cache so the AE answers "last hour" history requests with real data
    # immediately after restart instead of growing one sample at a time
    # from live ingestion. The catalog warm-up below still runs so any
    # series not present in the recent window (idle hosts, sparse metrics)
    # still surface in the dropdown — but it only seeds one point/series.
    if db_client is not None:
        try:
            from datetime import datetime as _dt_w, timezone as _tz_w
            from .models.metrics import MetricPoint as _MetricPoint_w
            warm_minutes = max(1, int(getattr(cache, "_metric_ttl", 3600) / 60))
            t0 = time.perf_counter()
            entries = db_client.warm_metric_history(minutes=warm_minutes)
            staged: list = []
            for e in entries:
                src = e.get("source") or ""
                mname = e.get("metric_name") or ""
                if not src or not mname:
                    continue
                ts_raw = e.get("timestamp") or ""
                try:
                    ts = _dt_w.fromisoformat(ts_raw) if ts_raw else _dt_w.now(_tz_w.utc)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_tz_w.utc)
                    staged.append(_MetricPoint_w(
                        source=src,
                        metric_name=mname,
                        value=float(e.get("value") or 0.0),
                        unit=e.get("unit") or None,
                        timestamp=ts,
                        hostname=e.get("hostname"),
                    ))
                except Exception:
                    continue
            # Single bulk insertion under one lock acquisition — cheaper
            # than ~16K individual add_metric_point() calls at startup.
            cache.add_metric_points(staged)
            inj = len(staged)
            logger.info(
                "Metric history warm-up: %d points (~%d series) loaded in %.2fs (window=%dmin)",
                inj, len({(e.get("source"), e.get("metric_name"), e.get("hostname")) for e in entries}),
                time.perf_counter() - t0, warm_minutes,
            )
        except Exception as e:
            logger.warning(f"Metric history warm-up failed: {e}")

    # 3d. Warm-reload the metric catalog from InfluxDB so every (source,
    # metric_name) pair seen in the last 7 days is visible in the dropdown
    # immediately after restart — not just metrics received since boot.
    if db_client is not None:
        try:
            from datetime import datetime as _dt, timezone as _tz
            from .models.metrics import MetricPoint as _MetricPoint
            catalog = db_client.query_metric_catalog(days=settings.alarm_engine.caches.metric_catalog_warmup_days)
            staged_cat: list = []
            for entry in catalog:
                src = entry.get("source", "")
                mname = entry.get("metric_name", "")
                if not src or not mname:
                    continue
                try:
                    ts_raw = entry.get("timestamp", "")
                    ts = (
                        _dt.fromisoformat(ts_raw)
                        if ts_raw
                        else _dt.now(_tz.utc)
                    )
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_tz.utc)
                    staged_cat.append(_MetricPoint(
                        source=src,
                        metric_name=mname,
                        value=float(entry.get("value") or 0.0),
                        unit=entry.get("unit") or None,
                        timestamp=ts,
                        hostname=entry.get("hostname"),
                    ))
                except Exception as e:
                    logger.debug(f"Catalog warm-reload skip {src}/{mname}: {e}")
            cache.add_metric_points(staged_cat)
            logger.info(f"Metric catalog warm-reload: {len(staged_cat)} series seeded from InfluxDB")
        except Exception as e:
            logger.warning(f"Metric catalog warm-reload failed: {e}")

    # 3. Initialize engine components (order matters: deps first)
    notification_dispatcher = NotificationDispatcher(notification_repository=notification_repo)
    alert_manager = AlertManager(
        alert_repository=alert_repo,
        rule_repository=rule_repo,
    )
    rule_engine = RuleEngine(
        rule_repository=rule_repo,
        alert_repository=alert_repo,
        alert_manager=alert_manager,
        notification_dispatcher=notification_dispatcher,
        metric_repo=metric_repo,
    )

    # 4. WebSocket manager
    ws_manager = init_manager()
    await ws_manager.start()

    # 4b. Wire ws_manager into notification_dispatcher so toast channel
    #     notifications reach the browser on any tab.
    async def _dispatch_ws_notification(msg: str) -> None:
        try:
            d = json.loads(msg)
            inner = d.get("data", d)
            # Merge top-level action into payload so the frontend ws.on('notification') handler
            # can identify the message type (e.g. action="toast") without it being stripped.
            if isinstance(inner, dict) and "action" in d and "action" not in inner:
                inner = {"action": d["action"], **inner}
            await ws_manager.broadcast("notification", inner)
        except Exception:
            logger.warning("ws notification dispatch failed", exc_info=True)
    notification_dispatcher.websocket_send = _dispatch_ws_notification

    # 5. Wire API routes to shared instances
    alerts.set_repositories(
        alert_repo=alert_repo,
        alert_mgr=alert_manager,
        notification_dispatcher=notification_dispatcher,
    )
    ingest.set_alert_manager(alert_manager)
    metrics.set_repository(metric_repo)
    notifications.set_repository(notification_repo)
    notifications.set_ws_send(_dispatch_ws_notification)
    rules.set_dependencies({"rule_repository": rule_repo, "alert_manager": alert_manager})

    # 6. Start background rule evaluation task. evaluate_all() blocks the
    # event loop briefly per cycle (cache hits ~5 ms, but cold paths can
    # be hundreds of ms). It naturally awaits asyncio.sleep before the
    # first cycle (eval_interval = 15 s default), so we don't need to
    # delay it further.
    asyncio.create_task(_rule_evaluation_loop())

    # 6a. Keep the metric-list cascade cache continuously warm. Walking
    # the MetricCache to build (host × source × metric_name) rows takes
    # seconds at scale; without a refresher the request after a 30 s TTL
    # expiry pays the scan in the foreground.
    async def _periodic_refresh(label: str, fn, interval_s: float, *args) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                t = time.perf_counter()
                result = await loop.run_in_executor(None, fn, *args)
                count = len(result) if hasattr(result, "__len__") else 0
                logger.info(
                    "%s warm-up: %d record(s) in %.2fs",
                    label, count, time.perf_counter() - t,
                )
            except Exception as e:
                logger.warning(f"{label} warm-up failed (non-fatal): {e}")
            await asyncio.sleep(interval_s)

    # /api/alarm/metrics: 30 s TTL → refresh every 20 s.
    if metric_repo is not None:
        from .api.routes.metrics import compute_metric_list
        asyncio.create_task(_periodic_refresh(
            "Metric-list", compute_metric_list, 20.0, metric_repo, None, 1000,
        ))


    # 6b. InfluxDB self-monitor — writes ping/query/cardinality/bytes
    # metrics back into InfluxDB so the alarm engine can alert on its
    # own backing store. Skipped when db_client failed to initialise
    # (cache-only mode) since there is nowhere to write. The first cycle
    # runs 3 cardinality Flux queries + a write probe + bytes-on-disk
    # walk — ~10-20 s of synchronous work — so it's delayed 30 s past
    # startup to keep the event loop free for the manager's warm-up
    # fan-out (which only has ~45 s of retry runway).
    if db_client is not None and metric_repo is not None:
        asyncio.create_task(_influx_monitor.run(
            db_client, metric_repo,
            interval_s=settings.alarm_engine.intervals.influxdb_monitor_s,
            initial_delay_s=30.0,
        ))


def _fmt_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m {s}s"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _log_shutdown_banner() -> None:
    """Final summary line — uptime, store counts, active alerts. Best-effort:
    every lookup is wrapped because a partially-initialized engine (early
    startup failure) shouldn't drown the real error in stat-lookup tracebacks."""
    uptime = _fmt_uptime(time.time() - _startup_ts) if _startup_ts else "?"
    rules_n = channels_n = configs_n = deliveries_n = 0
    if ae_settings_db is not None:
        with best_effort("shutdown banner: count rules"):
            rules_n = len(ae_settings_db.query_rules())
        with best_effort("shutdown banner: count channels"):
            channels_n = len(ae_settings_db.query_channels())
        with best_effort("shutdown banner: count configs"):
            configs_n = len(ae_settings_db.query_configs())
        with best_effort("shutdown banner: count deliveries"):
            deliveries_n = len(ae_settings_db.query_deliveries(limit=100000))

    counts = {"total": 0, "by_status": {}, "by_severity": {}}
    active_rows: list[dict] = []
    if ae_alarms_db is not None:
        with best_effort("shutdown banner: count alerts"):
            counts = ae_alarms_db.count_by_status_and_severity()
        with best_effort("shutdown banner: query active alerts"):
            active_rows = ae_alarms_db.query_active(limit=20)

    logger.info("=" * 60)
    logger.info(f"LLM Systems Alarm Engine {__version__} shutting down")
    logger.info(f"  Uptime:         {uptime}")
    logger.info(f"  Last eval:      {_rule_eval_last_cycle_ms:.1f} ms / cycle")
    logger.info(f"  Storage:        rules={rules_n} channels={channels_n} "
                f"configs={configs_n} deliveries={deliveries_n}")
    logger.info(f"  Alerts:         total={counts.get('total', 0)} "
                f"by_status={dict(counts.get('by_status', {}))} "
                f"by_severity={dict(counts.get('by_severity', {}))}")
    if active_rows:
        logger.info(f"  Active alerts ({len(active_rows)}):")
        for a in active_rows:
            scope = a.get("source_host") or "—"
            logger.info(
                f"    • [{a.get('severity', '?'):<8}] {a.get('rule_name') or a.get('rule_id') or '?'}"
                f" — {scope}:{a.get('metric_source')}/{a.get('metric_name')}"
                f" (value={a.get('current_value')}, threshold={a.get('threshold_value')},"
                f" status={a.get('status')}, since={a.get('created_at')})"
            )
    else:
        logger.info("  Active alerts:  none")
    logger.info("=" * 60)


async def _on_shutdown() -> None:
    """Clean up resources + emit shutdown banner."""
    global ws_manager, db_client, ae_settings_db, ae_alarms_db
    try:
        _log_shutdown_banner()
    except Exception as e:
        logger.warning(f"shutdown banner failed: {e}")
    if ws_manager:
        await ws_manager.stop()
    if db_client:
        try:
            db_client.close()
        except Exception as e:
            logger.warning(f"InfluxDB client close failed: {e}")
    if ae_settings_db:
        try:
            ae_settings_db.close()
        except Exception as e:
            logger.warning(f"AeSettingsDB close failed: {e}")
    if ae_alarms_db:
        try:
            ae_alarms_db.close()
        except Exception as e:
            logger.warning(f"AeAlarmsDB close failed: {e}")
    logger.info("Alarm Engine shut down")


async def _rule_evaluation_loop() -> None:
    """Background task that periodically evaluates all rules against metrics."""
    global rule_engine, metric_repo, _rule_eval_last_cycle_ms
    import time as _time
    last_hb = 0.0
    cycles = 0
    last_cycle_ms = 0.0
    while True:
        try:
            await asyncio.sleep(settings.alarm_engine.evaluation_interval)
            if rule_engine and metric_repo:
                t0 = _time.perf_counter()
                await rule_engine.evaluate_all()
                last_cycle_ms = (_time.perf_counter() - t0) * 1000
                _rule_eval_last_cycle_ms = last_cycle_ms
                cycles += 1
            now = _time.time()
            if now - last_hb >= 60.0:
                try:
                    all_rules = rule_repo.get_all() if rule_repo else []
                    n_rules = len(all_rules)
                    n_enabled = sum(1 for r in all_rules if getattr(r, "enabled", False))
                except Exception as e:
                    logger.debug("heartbeat rule-count probe failed: %s", e)
                    n_rules = n_enabled = -1
                logger.info(
                    "heartbeat rule_engine: rules=%d enabled=%d cycles=%d last_cycle_ms=%.1f interval=%ss",
                    n_rules, n_enabled, cycles, last_cycle_ms,
                    settings.alarm_engine.evaluation_interval,
                )
                # Sweep stale per-series buffers so memory does not grow
                # unbounded when sources drop off.
                if cache is not None:
                    try:
                        cache.sweep_metric_points()
                    except Exception as e:
                        logger.warning("metric-cache sweep failed: %s", e)
                last_hb = now
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Rule evaluation error: %s", e, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for startup/shutdown."""
    await _on_startup()
    yield
    await _on_shutdown()


# ── Frontend path ───────────────────────────────────────────────

_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


# ── App factory ─────────────────────────────────────────────────

app = FastAPI(
    title="LLM Systems Alarm Engine",
    description=(
        "Core alarm engine providing thresholding, anomaly detection, "
        "predictive analysis, and notification management with InfluxDB persistence."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.alarm_engine.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Register API routers ────────────────────────────────────────

app.include_router(alerts.router)
app.include_router(metrics.router)
app.include_router(notifications.router)
app.include_router(rules.router)
app.include_router(ingest.router)
app.include_router(otlp_receiver.router)


# ── Frontend static serving ─────────────────────────────────────

app.mount("/js",  StaticFiles(directory=os.path.join(_FRONTEND_DIR, "js")),  name="js")
app.mount("/css", StaticFiles(directory=os.path.join(_FRONTEND_DIR, "css")), name="css")


@app.get("/")
async def serve_index() -> FileResponse:
    """Serve the alarm engine dashboard."""
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))


# ── Health endpoint ─────────────────────────────────────────────

@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint.

    Real readiness probe — actually pings InfluxDB rather than just
    checking that a host was configured. Returns 200 in all branches so
    that load balancers / uptime checkers can distinguish "alarm engine
    process up but InfluxDB unreachable" via the JSON body rather than
    HTTP code.
    """
    import requests as _r
    influx_status = "not configured"
    influx_latency_ms: float | None = None
    influx_version: str | None = None
    if settings.influxdb.host:
        url = f"http://{settings.influxdb.host}:{settings.influxdb.port}/ping"
        try:
            t0 = time.perf_counter()
            resp = _r.get(url, timeout=settings.alarm_engine.timeouts.influxdb_ping)
            influx_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            influx_status = "connected" if resp.status_code in (200, 204) else f"http_{resp.status_code}"
            influx_version = resp.headers.get("X-Influxdb-Version") or None
        except Exception as e:
            influx_status = f"unreachable: {type(e).__name__}"
    return {
        "status": "ok",
        "version": __version__,
        "components": {
            "cache": "active" if cache else "inactive",
            "influxdb": influx_status,
            "influxdb_ping_ms": influx_latency_ms,
            "influxdb_version": influx_version,
            # Exposed so agent self-monitor probes can record rule-eval
            # cycle health without parsing the journal. 0.0 = no cycle
            # has completed yet since startup.
            "rule_eval_last_cycle_ms": _rule_eval_last_cycle_ms,
            # TLS serving state — surfaced so the manager's admin tab can show
            # whether the ingest surface is encrypted (and flag a misconfig
            # where tls_enabled is on but the cert wasn't found).
            "tls": _tls_status,
        },
    }


# ── SQLite stats endpoint ───────────────────────────────────────
# Backs the Database Performance card's SQLite section. Cached across
# tabs so N polling browsers collapse to one scan.

_SQLITE_STATS_CACHE: dict = {"at": 0.0, "payload": {}}
_SQLITE_STATS_TTL_S = 10.0


def _scalar(conn, sql: str):
    try:
        r = conn.execute(sql).fetchone()
        return r[0] if r else None
    except Exception:
        return None


def _sqlite_common_stats(path: str, conn) -> dict:
    out: dict = {"path": path}
    try:
        out["size_bytes"] = int(os.stat(path).st_size)
    except Exception:
        out["size_bytes"] = None
    for suffix, key in (("-wal", "wal_size_bytes"), ("-shm", "shm_size_bytes")):
        try:
            out[key] = int(os.stat(path + suffix).st_size)
        except Exception:
            out[key] = None

    out["page_size"] = _scalar(conn, "PRAGMA page_size")
    out["page_count"] = _scalar(conn, "PRAGMA page_count")
    out["journal_mode"] = _scalar(conn, "PRAGMA journal_mode")
    out["cache_size"] = _scalar(conn, "PRAGMA cache_size")

    t0 = time.perf_counter()
    _scalar(conn, "SELECT 1")
    out["query_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    return out


def _collect_sqlite_stats() -> dict:
    payload: dict = {}
    if ae_alarms_db is not None:
        with ae_alarms_db._lock:
            stats = _sqlite_common_stats(str(ae_alarms_db.path), ae_alarms_db._conn)
            stats["alerts"] = _scalar(ae_alarms_db._conn, "SELECT COUNT(*) FROM alerts")
        payload["alarms_db"] = stats
    if ae_settings_db is not None:
        with ae_settings_db._lock:
            c = ae_settings_db._conn
            stats = _sqlite_common_stats(str(ae_settings_db.path), c)
            stats["rules"]      = _scalar(c, "SELECT COUNT(*) FROM rules")
            stats["channels"]   = _scalar(c, "SELECT COUNT(*) FROM channels")
            stats["configs"]    = _scalar(c, "SELECT COUNT(*) FROM configs")
            stats["deliveries"] = _scalar(c, "SELECT COUNT(*) FROM deliveries")
        payload["settings_db"] = stats
    # Don't expose absolute filesystem paths to the browser (security #124);
    # the dashboard card only uses sizes/counts. Keep the filename for labeling.
    for v in payload.values():
        if isinstance(v, dict) and v.get("path"):
            v["db"] = os.path.basename(v.pop("path"))
    return payload


# ── Admin backup / restore ──────────────────────────────────────────
# Manager proxies /api/alarm/admin/* under admin-IP gating, so these
# endpoints assume the request has already been authorized upstream.
# Ships ae_notif_rules.db (rules / channels / configs / deliveries)
# and ae_alarms.db (alerts + history) as an LSMENC archive.

from . import _archive as _ae_archive
from datetime import datetime, timezone
from fastapi import UploadFile, File, Form, Body, HTTPException

_AE_EXPORT_DBS = ["data/ae_notif_rules.db", "data/ae_alarms.db"]

# The unified config is shipped alongside the AE DBs so a split-install
# import can carry [notifications.smtp], [influxdb], etc. onto the AE box —
# otherwise the freshly-imported alarm engine has no SMTP server, tokens,
# etc. and notification sends fail (e.g. "[Errno -5] No address associated
# with hostname"). Stored in the archive under this arcname; written back
# to the resolved config path, which lives at the repo root — NOT under the
# AE dir — so the apply path special-cases its destination.
_AE_EXPORT_CONFIG = "config/llm-systems.toml"


def _ae_data_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ae_config_path() -> Path:
    """Resolve the unified config path the same order unified_config uses:
    $LLM_SYSTEMS_CONFIG, then /opt/llm-systems-manager/config/llm-systems.toml,
    then <repo>/config/llm-systems.toml. Returns the first that exists; when
    none do, returns the preferred write target (env path if set, else the
    canonical /opt path)."""
    candidates: list[Path] = []
    env = os.environ.get("LLM_SYSTEMS_CONFIG")
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/opt/llm-systems-manager/config/llm-systems.toml"))
    candidates.append(_ae_data_root().parent / "config" / "llm-systems.toml")
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


# Environment keys the operator can edit before an AE import overwrites
# config/llm-systems.toml. Mirrors the manager's _TOPOLOGY_OVERRIDES but
# centred on what the AE actually needs on a split box: InfluxDB topology +
# tokens, the AE bind, the manager URL, and the notification SMTP block.
# (label, [(section, key), ...]) — first match wins on read; every listed
# section/key is patched on write.
_AE_TOPOLOGY_OVERRIDES = {
    "manager_url":       ("Manager URL", [("alarm_engine", "manager_url")]),
    "alarm_engine_host": ("Alarm engine bind host", [("alarm_engine", "host")]),
    "alarm_engine_port": ("Alarm engine bind port", [("alarm_engine", "port")]),
    "influxdb_host":     ("InfluxDB host", [("influxdb", "host")]),
    "influxdb_port":     ("InfluxDB port", [("influxdb", "port")]),
    "influxdb_org":      ("InfluxDB org", [("influxdb", "org")]),
    "influxdb_token_metrics":        ("InfluxDB token: metrics bucket",
                                      [("influxdb.tokens", "metrics")]),
    "influxdb_token_metrics_rollup": ("InfluxDB token: metrics_rollup bucket",
                                      [("influxdb.tokens", "metrics_rollup")]),
    "influxdb_token_admin":          ("InfluxDB token: admin (optional)",
                                      [("influxdb.tokens", "admin")]),
    "smtp_server":   ("SMTP server", [("notifications.smtp", "server")]),
    "smtp_port":     ("SMTP port", [("notifications.smtp", "port")]),
    "smtp_user":     ("SMTP user", [("notifications.smtp", "user")]),
    "smtp_password": ("SMTP password", [("notifications.smtp", "password")]),
}


def _ae_extract_toml_topology(toml_bytes: bytes) -> tuple[dict, str | None]:
    """Returns (topology_values, parse_error). Mirrors the manager helper so
    the import preview can show the operator the captured values."""
    import tomllib
    try:
        cfg = tomllib.loads(toml_bytes.decode("utf-8"))
    except Exception as e:
        return {}, str(e)
    out: dict = {}
    for ovr_key, (_label, paths) in _AE_TOPOLOGY_OVERRIDES.items():
        for section, key in paths:
            node = cfg
            for part in section.split("."):
                node = (node or {}).get(part) or {}
            if isinstance(node, dict) and key in node:
                out[ovr_key] = node[key]
                break
    return out, None


def _ae_patch_toml_lines(toml_text: str, overrides: dict) -> tuple[str, list[str]]:
    """Line-based TOML patcher (mirrors the manager helper): rewrite just the
    value of each overridden `key = <value>` inside its section, preserving
    indentation and trailing comments. Returns (patched_text, applied_keys)."""
    if not overrides:
        return toml_text, []
    targets: dict[tuple[str, str], object] = {}
    for ovr_key, value in overrides.items():
        if ovr_key not in _AE_TOPOLOGY_OVERRIDES:
            continue
        if value is None or value == "":
            continue
        _label, paths = _AE_TOPOLOGY_OVERRIDES[ovr_key]
        for section, key in paths:
            targets[(section, key)] = value
    if not targets:
        return toml_text, []
    import re
    line_re = re.compile(
        r'^(?P<indent>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)(?P<sp>\s*=\s*)'
        r'(?P<val>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s#]+)'
        r'(?P<tail>.*)$'
    )
    section_re = re.compile(r'^\s*\[(?P<name>[^\]]+)\]\s*(?:#.*)?$')
    current = ""
    applied: set[tuple[str, str]] = set()
    out_lines: list[str] = []
    for line in toml_text.splitlines(keepends=True):
        stripped = line.rstrip("\n").rstrip("\r")
        sm = section_re.match(stripped)
        if sm:
            current = sm.group("name").strip()
            out_lines.append(line)
            continue
        m = line_re.match(stripped)
        if m:
            key = m.group("key")
            tgt = targets.get((current, key))
            if tgt is not None:
                applied.add((current, key))
                if isinstance(tgt, bool):
                    val_repr = "true" if tgt else "false"
                elif isinstance(tgt, int):
                    val_repr = str(tgt)
                else:
                    s = str(tgt).replace("\\", "\\\\").replace('"', '\\"')
                    val_repr = f'"{s}"'
                eol = line[len(stripped):]
                out_lines.append(f'{m.group("indent")}{m.group("key")}{m.group("sp")}{val_repr}{m.group("tail")}{eol}')
                continue
        out_lines.append(line)
    return "".join(out_lines), [f"{s}.{k}" for s, k in sorted(applied)]


# Imported rules carry the SOURCE system's host/agent names in
# rules.source_host and configs.source_hosts_json. On a different box those
# names never match incoming metrics, so every host-scoped rule silently
# stops firing. The import flow scans these for the operator and offers a
# before→after remap applied to the DB bytes before they're written.
_AE_RULES_DB = "data/ae_notif_rules.db"


def _ae_scan_hosts(db_bytes: bytes) -> list[dict]:
    """Distinct source-host references in the rules DB, with usage counts:
    [{host, rules, configs}, ...]. Drives the import preview's remap UI."""
    import json as _json
    import sqlite3
    import tempfile
    counts: dict[str, dict] = {}

    def _bump(host: str, field: str, n: int = 1) -> None:
        if not host:
            return
        counts.setdefault(host, {"host": host, "rules": 0, "configs": 0})[field] += n

    fd, path = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(db_bytes)
        con = sqlite3.connect(path)
        try:
            try:
                for host, n in con.execute(
                    "SELECT source_host, COUNT(*) FROM rules "
                    "WHERE source_host IS NOT NULL AND source_host != '' "
                    "GROUP BY source_host"):
                    _bump(host, "rules", n)
            except sqlite3.OperationalError:
                pass  # foreign/old DB without a rules table — nothing to scan
            try:
                rows = con.execute("SELECT source_hosts_json FROM configs").fetchall()
            except sqlite3.OperationalError:
                rows = []  # no configs table
            for (raw,) in rows:
                try:
                    hosts = _json.loads(raw or "[]")
                except Exception:
                    hosts = []
                for h in hosts:
                    _bump(h, "configs")
        finally:
            con.close()
    finally:
        os.unlink(path)
    return sorted(counts.values(), key=lambda x: x["host"])


def _ae_remap_hosts(db_bytes: bytes, mapping: dict) -> tuple[bytes, dict]:
    """Rewrite rules.source_host and configs.source_hosts_json per {old: new}.
    Returns (new_db_bytes, {"rules": n, "configs": n}). Empty/identity maps are
    no-ops that return the original bytes unchanged."""
    mapping = {k: v for k, v in mapping.items() if v and v != k}
    if not mapping:
        return db_bytes, {"rules": 0, "configs": 0}
    import json as _json
    import sqlite3
    import tempfile
    applied = {"rules": 0, "configs": 0}
    fd, path = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(db_bytes)
        con = sqlite3.connect(path)
        try:
            try:
                for old, new in mapping.items():
                    cur = con.execute(
                        "UPDATE rules SET source_host = ? WHERE source_host = ?",
                        (new, old))
                    applied["rules"] += cur.rowcount
            except sqlite3.OperationalError:
                pass  # no rules table — nothing to remap there
            try:
                rows = con.execute(
                    "SELECT config_id, source_hosts_json FROM configs").fetchall()
            except sqlite3.OperationalError:
                rows = []  # no configs table
            for cid, raw in rows:
                try:
                    hosts = _json.loads(raw or "[]")
                except Exception:
                    continue
                new_hosts = [mapping.get(h, h) for h in hosts]
                if new_hosts != hosts:
                    con.execute(
                        "UPDATE configs SET source_hosts_json = ? WHERE config_id = ?",
                        (_json.dumps(new_hosts), cid))
                    applied["configs"] += 1
            con.commit()
        finally:
            con.close()
        with open(path, "rb") as f:
            return f.read(), applied
    finally:
        os.unlink(path)


def _build_ae_archive() -> dict[str, bytes]:
    root = _ae_data_root()
    files: dict[str, bytes] = {}
    for rel in _AE_EXPORT_DBS:
        p = root / rel
        if p.is_file():
            files[rel] = _ae_archive.sqlite_snapshot(str(p))
    cfg = _ae_config_path()
    if cfg.is_file():
        files[_AE_EXPORT_CONFIG] = cfg.read_bytes()
    return files


def _ae_manifest(files: dict[str, bytes]) -> bytes:
    import socket as _s
    manifest = {
        "component": "alarm_engine",
        "ae_version": __version__,
        "hostname": _s.gethostname(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files),
        "files": sorted([
            {"name": n, "size": len(d)} for n, d in files.items()
        ], key=lambda x: x["name"]),
    }
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


@app.post("/api/alarm/admin/export")
async def ae_admin_export(body: dict = Body(default_factory=dict)):
    """Build the alarm engine backup archive. Body: {"password": "<>"}.
    Empty password = unencrypted; frontend warns explicitly."""
    password = (body or {}).get("password") or ""
    files = _build_ae_archive()
    files["manifest.json"] = _ae_manifest(files)
    try:
        tgz = _ae_archive.pack_tar(files)
        blob = _ae_archive.encrypt(tgz, password if password else None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ModuleNotFoundError as e:
        # The cryptography package only landed in requirements.txt
        # alongside this feature; older AE venvs need a pip install.
        raise HTTPException(
            status_code=503,
            detail=(f"backend dependency missing ({e.name}). Refresh the "
                    f"alarm engine venv: sudo -u llmsys "
                    f"/opt/llm-systems-manager/llm-systems-alarm-engine/venv/bin/pip "
                    f"install -r /opt/llm-systems-manager/llm-systems-alarm-engine/"
                    f"requirements.txt  &&  sudo systemctl restart "
                    f"llm-systems-alarm-engine"))
    import socket as _s
    fname = (f"lsm-ae-{_s.gethostname()}-"
             f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.lsmenc")
    logger.warning("AE export issued (%d files, %d bytes, encrypted=%s)",
                   len(files), len(blob), bool(password))
    from fastapi.responses import Response
    return Response(
        content=blob,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Lsmenc-Encrypted": "1" if password else "0",
        },
    )


from typing import NamedTuple

class _DecodedAE(NamedTuple):
    files: dict[str, bytes]
    manifest: dict
    encrypted: bool


def _ae_decode_upload(blob: bytes, password: str) -> _DecodedAE:
    enc = _ae_archive.sniff_encrypted(blob)
    if enc is None:
        raise HTTPException(status_code=400,
                            detail="not an LSMENC archive (bad magic)")
    try:
        payload = _ae_archive.decrypt(blob, password)
        files = _ae_archive.unpack_tar(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ModuleNotFoundError as e:
        raise HTTPException(
            status_code=503,
            detail=(f"backend dependency missing ({e.name}). Refresh the "
                    f"alarm engine venv (see export endpoint for the command)."))
    manifest = {}
    raw = files.get("manifest.json")
    if raw:
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            pass
    if manifest.get("component") and manifest["component"] != "alarm_engine":
        raise HTTPException(
            status_code=400,
            detail=f"archive is for '{manifest['component']}', not 'alarm_engine'")
    return _DecodedAE(files=files, manifest=manifest, encrypted=enc)


@app.post("/api/alarm/admin/import/preview")
async def ae_admin_import_preview(file: UploadFile = File(...),
                                  password: str = Form("")):
    blob = await file.read()
    decoded = _ae_decode_upload(blob, password)
    entries = sorted([
        {"name": n, "size": len(d)}
        for n, d in decoded.files.items() if n != "manifest.json"
    ], key=lambda x: x["name"])
    topology: dict = {}
    topology_error = None
    toml_bytes = decoded.files.get(_AE_EXPORT_CONFIG)
    if toml_bytes:
        topology, topology_error = _ae_extract_toml_topology(toml_bytes)
    topology_schema = [
        {"key": k, "label": label}
        for k, (label, _paths) in _AE_TOPOLOGY_OVERRIDES.items()
    ]
    host_remap: list = []
    rules_db = decoded.files.get(_AE_RULES_DB)
    if rules_db:
        try:
            host_remap = _ae_scan_hosts(rules_db)
        except Exception as e:
            logger.warning("AE import host scan failed: %s", e)
    return {"ok": True, "encrypted": decoded.encrypted,
            "manifest": decoded.manifest, "entries": entries,
            "topology": topology, "topology_schema": topology_schema,
            "topology_error": topology_error, "host_remap": host_remap}


@app.post("/api/alarm/admin/import/apply")
async def ae_admin_import_apply(file: UploadFile = File(...),
                                password: str = Form(""),
                                topology_overrides: str = Form(""),
                                host_remap: str = Form("")):
    blob = await file.read()
    files = _ae_decode_upload(blob, password).files
    # Topology overrides: a JSON dict {ovr_key: new_value}, patched into the
    # config TOML bytes before anything is written (mirrors the manager).
    try:
        overrides = json.loads(topology_overrides) if topology_overrides else {}
        if not isinstance(overrides, dict):
            raise ValueError("topology_overrides must be a JSON object")
    except Exception as e:
        raise HTTPException(status_code=400,
                            detail=f"bad topology_overrides: {e}")
    patched_keys: list[str] = []
    if overrides and files.get(_AE_EXPORT_CONFIG):
        try:
            old_text = files[_AE_EXPORT_CONFIG].decode("utf-8")
            new_text, patched_keys = _ae_patch_toml_lines(old_text, overrides)
            files[_AE_EXPORT_CONFIG] = new_text.encode("utf-8")
        except Exception as e:
            raise HTTPException(status_code=400,
                                detail=f"TOML patch failed: {e}")
    # Host remap: a JSON dict {old_host: new_host}, rewritten into the rules
    # DB bytes so imported rules/configs target this system's host names.
    try:
        remap = json.loads(host_remap) if host_remap else {}
        if not isinstance(remap, dict):
            raise ValueError("host_remap must be a JSON object")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad host_remap: {e}")
    host_remap_applied: dict = {"rules": 0, "configs": 0}
    if remap and files.get(_AE_RULES_DB):
        try:
            files[_AE_RULES_DB], host_remap_applied = _ae_remap_hosts(
                files[_AE_RULES_DB], remap)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"host remap failed: {e}")
    root = _ae_data_root()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    allow = set(_AE_EXPORT_DBS) | {_AE_EXPORT_CONFIG}
    written: list[str] = []
    backups: list[str] = []
    for arc_name, data in files.items():
        if arc_name == "manifest.json":
            continue
        if arc_name not in allow:
            logger.warning("ignoring unexpected AE archive entry: %s", arc_name)
            continue
        # The DBs live under the AE dir; the config TOML lives at the repo
        # root and is loaded from a resolved path, so route it there.
        dest = _ae_config_path() if arc_name == _AE_EXPORT_CONFIG else root / arc_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            bak = f"{dest}.preimport.{ts}.bak"
            import shutil as _sh
            _sh.copy2(dest, bak)
            os.chmod(bak, 0o600)
            backups.append(bak)
        tmp = f"{dest}.{ts}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        # See manager-side _import_apply_manager: stale sidecars must
        # go before the os.replace, not after, to close the race
        # window where a fresh SQLite opener could land on
        # (new DB + old sidecars) and roll the old WAL forward.
        if arc_name.endswith(".db"):
            _ae_archive.clear_sqlite_sidecars(str(dest))
        os.replace(tmp, str(dest))
        written.append(str(dest))
    logger.warning("AE import applied: %d files, ts=%s, patched=%s, host_remap=%s",
                   len(written), ts, ",".join(patched_keys) or "none",
                   host_remap_applied)
    return {"ok": True, "written": written, "backups": backups, "ts": ts,
            "patched_toml_keys": patched_keys,
            "host_remap_applied": host_remap_applied,
            "note": "Restart the alarm engine for the imported rules / "
                    "alerts / config to take effect."}


@app.get("/api/alarm/dbstats/sqlite")
async def sqlite_dbstats() -> dict:
    now = time.time()
    if (now - _SQLITE_STATS_CACHE["at"]) < _SQLITE_STATS_TTL_S and _SQLITE_STATS_CACHE["payload"]:
        return _SQLITE_STATS_CACHE["payload"]
    # to_thread so the SQLite RLock contention (shared with the hot
    # write path: write_alert, bump_refresh) doesn't stall the loop.
    payload = await asyncio.to_thread(_collect_sqlite_stats)
    _SQLITE_STATS_CACHE["payload"] = payload
    _SQLITE_STATS_CACHE["at"] = now
    return payload


# ── WebSocket endpoint ──────────────────────────────────────────

def _ws_origin_allowed(websocket: WebSocket) -> bool:
    """Return True if the WebSocket handshake's Origin is acceptable.

    CORSMiddleware does not apply to WebSocket handshakes, so we have to
    validate Origin here ourselves — otherwise any browser on the LAN can
    open a cross-site WebSocket to /ws and exfiltrate live alert/metric
    events (CSWSH).

    Policy:
      - Missing Origin (non-browser client like curl / native agent) is
        allowed; CSWSH only applies when a browser attaches its ambient
        network position to an attacker's page.
      - If cors_origins is configured to a non-wildcard list, Origin must
        match one of those entries exactly.
      - If cors_origins is "*" (or empty), Origin must be same-origin —
        scheme://host[:port] must match the request's Host header.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True

    raw = (settings.alarm_engine.cors_origins or "").strip()
    configured = [o.strip() for o in raw.split(",") if o.strip()]
    explicit = [o for o in configured if o != "*"]
    if explicit:
        return origin in explicit

    host = websocket.headers.get("host") or ""
    if not host:
        return False
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    same_origin = f"{scheme}://{host}"
    return origin == same_origin


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Main WebSocket endpoint for live dashboard updates."""
    global ws_manager
    if ws_manager is None:
        await websocket.close(code=4001, reason="WebSocket service not initialized")
        return
    if not _ws_origin_allowed(websocket):
        # Close before accept() so no event data is leaked to a rejected peer.
        await websocket.close(code=1008, reason="origin not allowed")
        return
    await websocket_endpoint_impl(websocket, ws_manager)


async def websocket_endpoint_impl(websocket: WebSocket, manager: WebSocketConnectionManager) -> None:
    """Implementation of the WebSocket endpoint."""
    client_id = await manager.connect(websocket)
    manager.subscribe_all(client_id)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "event": "error",
                    "data": {"message": "Invalid JSON"},
                }))
                continue

            action = msg.get("action", "")
            event_type = msg.get("event_type", "")

            if action == "subscribe":
                manager.subscribe_all(client_id) if not event_type else manager.subscribe(client_id, event_type)
            elif action == "unsubscribe":
                manager.unsubscribe_all(client_id) if not event_type else manager.unsubscribe(client_id, event_type)
            elif action == "ping":
                await websocket.send_text(json.dumps({
                    "event": "pong",
                    "data": {},
                }))
            else:
                await websocket.send_text(json.dumps({
                    "event": "error",
                    "data": {"message": f"Unknown action: {action}"},
                }))

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected: {client_id}")
    except Exception as e:
        logger.error(f"WebSocket error for {client_id}: {e}")
    finally:
        await manager.disconnect(client_id)


def _resolve_ae_path(p: str) -> Path:
    """Resolve a configured cert path; relative paths are anchored at the
    alarm-engine package root (…/llm-systems-alarm-engine), where its data/
    dir lives — the same place the manager writes / the admin copies ae-tls.*."""
    pp = Path(p).expanduser()
    if not pp.is_absolute():
        pp = Path(__file__).resolve().parent.parent / pp
    return pp


def main() -> None:
    """Programmatic uvicorn launcher. Serves HTTPS when [alarm_engine].tls_enabled
    and the cert/key are present; otherwise plain HTTP. When TLS is enabled but
    the cert is missing it logs a clear error and falls back to HTTP (fail-open)
    so a missing-cert misconfig degrades rather than taking the engine down."""
    import uvicorn
    global _tls_status
    host = settings.alarm_engine.host
    port = settings.alarm_engine.port
    ssl_kwargs: dict = {}
    if bool(settings.alarm_engine.tls_enabled):
        crt = _resolve_ae_path(settings.alarm_engine.tls_cert_file)
        key = _resolve_ae_path(settings.alarm_engine.tls_key_file)
        if crt.is_file() and key.is_file():
            ssl_kwargs = {"ssl_certfile": str(crt), "ssl_keyfile": str(key)}
            _tls_status = {"enabled": True, "active": True, "error": None}
            logger.warning("Alarm engine: serving HTTPS on %s:%d (cert %s)", host, port, crt)
        else:
            msg = (f"tls_enabled but cert/key not found: {crt} / {key} — "
                   "copy ae-tls.{crt,key} from the manager's data/ dir into this "
                   "host's llm-systems-alarm-engine/data/ dir; serving plain HTTP")
            _tls_status = {"enabled": True, "active": False, "error": msg}
            logger.error("ALARM ENGINE TLS: %s", msg)
    # uvicorn's OWN logger stays at warning (matching the prior unit's
    # `--log-level warning`); the app logger is configured separately above from
    # settings.alarm_engine.log_level, so this doesn't quiet our own logs.
    uvicorn.run(
        app, host=host, port=port,
        log_level="warning",
        access_log=False, loop="uvloop", http="httptools",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()