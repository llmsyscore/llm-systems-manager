"""Core rule engine that orchestrates threshold evaluation, anomaly detection, and alerting.

This is the central processing unit that:
- Loads and manages alarm rules
- Evaluates incoming metric data against rules
- Triggers alerts when conditions are met
- Coordinates with notification dispatcher
"""

import asyncio
import logging
import time
from .._time import now_utc
from typing import Optional

from ..models.alarm_rule import AlarmRule, RuleType
from ..models.alert import AlertCreate, AlertStatus
from ..models.metrics import MetricPoint
from ..storage.repositories import RuleRepository, AlertRepository, MetricRepository
from .alert_manager import AlertManager
from .threshold_evaluator import ThresholdEvaluator
from .anomaly_detector import AnomalyDetector

logger = logging.getLogger(__name__)


class RuleEngine:
    """Central rule evaluation engine."""

    def __init__(
        self,
        rule_repository: RuleRepository,
        alert_repository: AlertRepository,
        alert_manager: AlertManager,
        notification_dispatcher,  # NotificationDispatcher instance
        metric_repo: Optional[MetricRepository] = None,
        threshold_evaluator: Optional[ThresholdEvaluator] = None,
        anomaly_detector: Optional[AnomalyDetector] = None,
        evaluation_interval: float = 30.0,  # seconds
    ):
        self.rule_repository = rule_repository
        self.alert_repository = alert_repository
        self.alert_manager = alert_manager
        self.notification_dispatcher = notification_dispatcher
        self.metric_repo = metric_repo
        self.anomaly_detector = anomaly_detector or AnomalyDetector()
        self.threshold_evaluator = threshold_evaluator or ThresholdEvaluator(
            anomaly_detector=self.anomaly_detector
        )
        self.evaluation_interval = evaluation_interval

        # Background evaluation task
        self._evaluation_task: Optional[asyncio.Task] = None
        self._running = False

        # Recent evaluations for logging
        self._recent_evaluations: list[dict] = []
        self._max_evaluations_log = 100

        # Per-rule last-persist epoch — throttles the sync InfluxDB write of
        # `last_evaluated_at` so high-frequency eval cycles don't pin the DB.
        # 300 s (5 min) is a 5x reduction from the previous 60 s and matches
        # the granularity the rules UI shows for "last evaluated"; it's the
        # difference between a ~1.2 s burst-of-18-sync-writes every minute
        # and the same burst every 5 minutes.
        self._last_persist_ts: dict[str, float] = {}
        self._persist_min_interval_s: float = 300.0

        # Per-rule consecutive "ok" cycle counts, used to auto-resolve active
        # alerts only after the metric has been below threshold for N cycles
        # in a row (hysteresis — prevents flapping when a value hovers right
        # at the threshold). N is configured per-rule via auto_resolve_cycles;
        # a value of 0 disables auto-resolve for that rule.
        self._ok_streak: dict[str, int] = {}

    async def start(self) -> None:
        """Start the rule engine evaluation loop."""
        if self._running:
            return

        self._running = True
        self._evaluation_task = asyncio.create_task(self._evaluation_loop())
        logger.info("Rule engine started")

    async def stop(self) -> None:
        """Stop the rule engine evaluation loop."""
        self._running = False
        if self._evaluation_task:
            self._evaluation_task.cancel()
            try:
                await self._evaluation_task
            except asyncio.CancelledError:
                pass
        self._evaluation_task = None
        logger.info("Rule engine stopped")

    async def _evaluation_loop(self) -> None:
        """Continuous evaluation loop."""
        while self._running:
            try:
                await self.evaluate_all()
            except Exception:
                logger.exception("Error during rule evaluation")

            # Sleep in small increments to allow responsive stop
            for _ in range(int(self.evaluation_interval * 10)):
                if not self._running:
                    break
                await asyncio.sleep(0.1)

    async def evaluate_all(self) -> None:
        """Evaluate all enabled rules against current metrics."""
        cycle_start = time.perf_counter()
        rules = self.rule_repository.get_all(enabled_only=True)
        triggered = 0
        violations = 0
        per_rule_ms: list[float] = []

        # Snapshot active alerts once per cycle. AlertRepository keeps its
        # own TTL cache, invalidated on every state change (fire, auto-resolve,
        # manual ack/close/ignore/delete, bulk ops), so this read is sub-ms on
        # a cache hit and always reflects manual actions. Offloaded to a worker
        # thread to keep the event loop responsive on a cache miss.
        try:
            loop = asyncio.get_running_loop()
            active_alerts_snapshot = await loop.run_in_executor(
                None, self.alert_repository.get_active,
            )
        except Exception:
            logger.exception("Failed to fetch active alerts snapshot")
            active_alerts_snapshot = []

        for rule in rules:
            r_start = time.perf_counter()
            try:
                fired = await self._evaluate_rule(rule, active_alerts=active_alerts_snapshot)
                if fired:
                    triggered += 1
                    violations += 1
            except Exception:
                logger.exception(f"Error evaluating rule {rule.rule_id}")
            per_rule_ms.append((time.perf_counter() - r_start) * 1000.0)
            # `_evaluate_rule` does sync work (threshold math, metric lookups,
            # InfluxDB alert writes) with no internal awaits. Without an
            # explicit yield, the whole rule list runs in one event-loop
            # tick — every HTTP handler queues up behind it. A single
            # `sleep(0)` lets pending tasks (GET /rules/{id}, /alerts, etc.)
            # interleave between rules, so API tail-latency tracks one rule's
            # eval cost instead of the whole cycle's.
            await asyncio.sleep(0)

        cycle_ms = (time.perf_counter() - cycle_start) * 1000.0
        avg_rule_ms = (sum(per_rule_ms) / len(per_rule_ms)) if per_rule_ms else 0.0
        logger.info(
            "Rule eval cycle: %d rules in %.1f ms (avg %.2f ms/rule), %d triggered",
            len(rules), cycle_ms, avg_rule_ms, triggered,
        )

    async def _evaluate_rule(
        self,
        rule: AlarmRule,
        active_alerts: Optional[list] = None,
    ) -> bool:
        """Evaluate a single rule against current metric data.

        Returns True if the rule fired and a new alert was created.

        `active_alerts` is an optional snapshot of currently-active alerts
        provided by the caller (evaluate_all) to avoid one InfluxDB query
        per rule per cycle. If None, the snapshot is fetched on demand.
        """
        # Fetch latest metric points from repository
        if self.metric_repo is None:
            logger.debug(f"No metric data for {rule.metric_source}/{rule.metric_name}")
            return False
        # If the rule is scoped to a specific host, only see that host's points
        # — prevents cross-host attribution when multiple agents push the same
        # source:metric pair (e.g. cpu/usage_percent on linux-host AND mac-mini).
        metric_points = self.metric_repo.get_points(
            rule.metric_source, rule.metric_name,
            hostname=rule.source_host, limit=200,
        )

        scope = f"{rule.source_host}:" if rule.source_host else ""
        if not metric_points or len(metric_points) == 0:
            logger.debug(
                "No metric data for rule %s (%s%s/%s)",
                rule.name, scope, rule.metric_source, rule.metric_name,
            )
            return False

        current_value = metric_points[-1].value

        # Evaluate the rule
        alert_create = self.threshold_evaluator.evaluate_rule(
            rule, current_value, metric_points
        )

        alert_triggered = alert_create is not None

        # Log evaluation
        self._recent_evaluations.append({
            "timestamp": now_utc().isoformat(),
            "rule_id": str(rule.rule_id),
            "rule_name": rule.name,
            "metric": f"{rule.metric_source}/{rule.metric_name}",
            "value": current_value,
            "status": "violation" if alert_triggered else "ok",
            "alert_triggered": alert_triggered,
        })

        # Trim log
        if len(self._recent_evaluations) > self._max_evaluations_log:
            self._recent_evaluations = self._recent_evaluations[-self._max_evaluations_log:]

        # Process alert if triggered, or auto-resolve any active alert for
        # this rule once the metric has recovered for N consecutive cycles.
        fired = False
        rule_key = str(rule.rule_id)
        resolve_after = getattr(rule, "auto_resolve_cycles", 2) or 0

        # For threshold rules, "recovered" must hold across the recent data
        # window, not just the latest sample. Without this check a single
        # transient dip (noisy metric, host-interleaved point, ingest hiccup)
        # advanced the streak and could close an alert while the metric was
        # still mostly above threshold — the flapping the user observed.
        # For anomaly rule types (z-score, moving avg, etc.) the evaluator
        # already considers a window, so the single-sample result is sound.
        recovered_now = (not alert_triggered)
        if recovered_now and resolve_after > 0 and rule.rule_type in (
            RuleType.THRESHOLD_ABOVE,
            RuleType.THRESHOLD_BELOW,
            RuleType.THRESHOLD_RANGE,
        ):
            # Inspect the last N samples — N covers ~2× the streak window so
            # one stray reading inside the window resets us instead of
            # tipping us over the edge.
            window_n = max(resolve_after * 2, 5)
            recent = metric_points[-window_n:]
            for p in recent:
                if self.threshold_evaluator.evaluate_rule(rule, p.value, [p]) is not None:
                    recovered_now = False
                    break

        if not alert_triggered and resolve_after > 0:
            if recovered_now:
                self._ok_streak[rule_key] = self._ok_streak.get(rule_key, 0) + 1
            else:
                # A recent sample still breaches — not actually recovered.
                # Drop the streak so a brief lull mid-trend cannot accumulate
                # toward auto-resolve.
                self._ok_streak.pop(rule_key, None)
            if self._ok_streak.get(rule_key, 0) >= resolve_after:
                # Use the per-cycle snapshot when provided; only fall back to
                # a fresh query if this method was called outside evaluate_all.
                _alerts = active_alerts if active_alerts is not None else self.alert_repository.get_active()
                to_resolve = [
                    a for a in _alerts
                    if str(a.rule_id) == rule_key
                    and a.status in (AlertStatus.ACTIVE, AlertStatus.ACKNOWLEDGED)
                ]
                for a in to_resolve:
                    closed = self.alert_manager.close_alert(
                        str(a.alert_id), reason="auto", resolved_value=current_value,
                    )
                    if closed is not None:
                        logger.info(
                            "ALERT AUTO-RESOLVED: rule=%s metric=%s/%s value=%.2f "
                            "(below threshold for %d cycles) alert_id=%s",
                            rule.name, rule.metric_source, rule.metric_name,
                            current_value, self._ok_streak[rule_key], a.alert_id,
                        )
                        # Give policies with notify_on_clear=True a chance to
                        # send a "cleared" notification. The dispatcher also
                        # uses this hook to reset per-alert state (breach
                        # count, last-dispatch timestamps).
                        if self.notification_dispatcher is not None:
                            try:
                                self.notification_dispatcher.notify_alert_resolved(closed)
                            except Exception:
                                logger.exception(
                                    f"Failed to dispatch clear notification for alert {a.alert_id}"
                                )
        if alert_triggered and alert_create is not None:
            self._ok_streak.pop(rule_key, None)
            # Check if we already have an active alert for this rule to avoid duplicates
            _alerts = active_alerts if active_alerts is not None else self.alert_repository.get_active()
            existing = [
                a for a in _alerts
                if str(a.rule_id) == str(rule.rule_id)
                and a.status in (AlertStatus.ACTIVE, AlertStatus.ACKNOWLEDGED)
            ]

            if not existing:
                t0 = time.perf_counter()
                created_alert = self.alert_manager.process_alert(alert_create)
                proc_ms = (time.perf_counter() - t0) * 1000.0
                logger.info(
                    "ALERT FIRED: rule=%s host=%s metric=%s/%s value=%.2f threshold=%.2f severity=%s alert_id=%s (process %.1f ms)",
                    rule.name,
                    alert_create.source_host or "—",
                    rule.metric_source, rule.metric_name,
                    current_value, alert_create.threshold_value,
                    alert_create.severity,
                    created_alert.alert_id if created_alert else "deduped",
                    proc_ms,
                )
                # Fire-and-forget notification dispatch
                if created_alert is not None and self.notification_dispatcher is not None:
                    try:
                        d0 = time.perf_counter()
                        self.notification_dispatcher.send_notifications(created_alert)
                        logger.info(
                            "Notifications dispatched for alert %s (queue %.1f ms)",
                            created_alert.alert_id,
                            (time.perf_counter() - d0) * 1000.0,
                        )
                    except Exception:
                        logger.exception(f"Failed to dispatch notifications for alert {created_alert.alert_id}")
                fired = created_alert is not None
            else:
                # Active alert already exists for this rule — refresh it with
                # the latest sample so the UI sees current_value tick, the
                # last-evaluated timestamp advance, and trigger_count increment
                # rather than the alert appearing frozen at first detection.
                refreshed = self.alert_repository.refresh(
                    existing[0], current_value
                )
                logger.debug(
                    "Rule %s refreshed active alert %s (count=%d, value=%.2f)",
                    rule.name, refreshed.alert_id,
                    refreshed.trigger_count, refreshed.current_value,
                )
                # Re-fire the dispatcher on each continuing breach so the
                # policy-side filters (min_alarm_count, repeat_interval_minutes)
                # get a chance to evaluate. The dispatcher tracks per-alert
                # state and decides whether to actually send anything; this
                # call is cheap when no policy is due to dispatch.
                if self.notification_dispatcher is not None:
                    try:
                        self.notification_dispatcher.send_notifications(refreshed)
                    except Exception:
                        logger.exception(
                            f"Failed to consider continuing notifications for alert {refreshed.alert_id}"
                        )

        # Update rule last_evaluated_at — keep in-memory always; throttle the
        # InfluxDB persist. Every-cycle sync write was the dominant Influx
        # CPU source (11 rules × ~5s cycles → ~2 sync writes/sec for a purely
        # cosmetic timestamp). Persist on fire (already covered by alert path)
        # or every `_persist_min_interval_s` seconds.
        rule.last_evaluated_at = now_utc()
        now_s = time.time()
        rkey = str(rule.rule_id)
        last = self._last_persist_ts.get(rkey, 0.0)
        if fired or (now_s - last) >= self._persist_min_interval_s:
            self.rule_repository._save_rule(rule)
            self._last_persist_ts[rkey] = now_s
        return fired

    async def evaluate_single(
        self, rule: AlarmRule, metric_point: MetricPoint
    ) -> Optional[AlertCreate]:
        """Evaluate a single rule against a metric point (manual trigger).

        Returns an AlertCreate if a violation is detected, None otherwise.
        """
        alert_create = self.threshold_evaluator.evaluate_rule(
            rule, metric_point.value, [metric_point]
        )

        if alert_create is not None:
            self.alert_manager.process_alert(alert_create)
            logger.info(f"Manual rule violation: {rule.name} - {alert_create.message}")

        return alert_create

    def get_recent_evaluations(self, limit: int = 50) -> list[dict]:
        """Get recent evaluation results."""
        return self._recent_evaluations[-limit:]

    def get_evaluation_stats(self) -> dict:
        """Get evaluation statistics."""
        total = len(self._recent_evaluations)
        violations = sum(1 for e in self._recent_evaluations if e.get("alert_triggered"))

        return {
            "total_evaluations": total,
            "violations": violations,
            "violation_rate": round(violations / total * 100, 2) if total > 0 else 0,
        }

    @property
    def is_running(self) -> bool:
        """Check if the rule engine is currently running."""
        return self._running

    # metric_repo is now set in __init__ via dependency injection
