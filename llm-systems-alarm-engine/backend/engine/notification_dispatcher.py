"""Notification dispatcher for sending alerts through multiple channels.

Supports notification channels:
- Toast notifications (WebSocket push to frontend)
- Email
- Webhooks
- Discord webhooks
"""

import asyncio
import json
import logging
import smtplib
import time
from email.mime.text import MIMEText
from typing import Any, Callable, Optional
from uuid import uuid4

import httpx

from ..models.alert import Alert
from ..models.notification import (
    ChannelType,
    NotificationChannel,
    NotificationChannelCreate,
)
from config.unified_config import settings  # noqa: E402

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """Dispatches notifications through configured channels."""

    def __init__(
        self,
        websocket_send: Optional[Callable] = None,
        notification_repository=None,
        alert_repository=None,
    ):
        self.websocket_send = websocket_send
        self.notification_repository = notification_repository
        self.alert_repository = alert_repository

        # Notification channels (channel_id -> NotificationChannel)
        self._channels: dict[str, NotificationChannel] = {}

        # Per-alert state used by the policy filters. In-memory only;
        # restarts reset everything (acceptable — a freshly-restarted
        # service should re-evaluate suppression windows).
        # consecutive eval-cycles a given alert has been firing:
        self._breach_count: dict[str, int] = {}
        # incident_id -> first-dispatch monotonic ts, for #215 suppression:
        self._incident_dispatched: dict[str, float] = {}
        # alert_ids with at least one successful non-toast send this cycle:
        self._nontoast_send_ok: set[str] = set()
        # last dispatch ts keyed by (config_id_str, alert_id_str):
        self._last_dispatch_ts: dict[tuple[str, str], float] = {}
        # remembers whether a given policy has ever dispatched for an
        # alert — needed so notify_on_clear only fires when the alert
        # actually reached this policy in the first place:
        self._dispatched_first: set[tuple[str, str]] = set()

        # Channel enable flags (derived from channel configs)
        self._channels_enabled: dict[str, bool] = {
            "toast": True,
            "email": False,
            "sms": False,
            "webhook": False,
            "discord": False,
        }

        # Custom notification rules (rule_id -> list of channel_ids)
        self._custom_rules: dict[str, list[str]] = {}

    def add_channel(self, channel_create: NotificationChannelCreate) -> NotificationChannel:
        """Add a notification channel."""
        channel = channel_create.to_channel()
        self._channels[str(channel.channel_id)] = channel
        self._update_channel_flags()
        return channel

    def update_channel(self, channel_id: str, updates: dict[str, Any]) -> Optional[NotificationChannel]:
        """Update a notification channel."""
        channel = self._channels.get(channel_id)
        if not channel:
            return None
        # Apply updates to channel config
        return channel

    def remove_channel(self, channel_id: str) -> bool:
        """Remove a notification channel."""
        if channel_id not in self._channels:
            return False
        del self._channels[channel_id]
        self._update_channel_flags()
        return True

    def get_channel(self, channel_id: str) -> Optional[NotificationChannel]:
        """Get a notification channel by ID."""
        return self._channels.get(channel_id)

    def list_channels(self) -> list[NotificationChannel]:
        """List all notification channels."""
        return list(self._channels.values())

    def _update_channel_flags(self) -> None:
        """Update enabled flags based on configured channels."""
        for channel in self._channels.values():
            if channel.channel_type == ChannelType.TOAST:
                self._channels_enabled["toast"] = channel.enabled
            elif channel.channel_type == ChannelType.EMAIL:
                self._channels_enabled["email"] = channel.enabled
            elif channel.channel_type == ChannelType.SMS:
                self._channels_enabled["sms"] = channel.enabled
            elif channel.channel_type == ChannelType.WEBHOOK:
                self._channels_enabled["webhook"] = channel.enabled
            elif channel.channel_type == ChannelType.DISCORD:
                self._channels_enabled["discord"] = channel.enabled

    async def _get_all_channels_async(self) -> list[NotificationChannel]:
        """Async version: load channels from repository if wired, otherwise fall back to in-memory."""
        if self.notification_repository is not None:
            try:
                return await self.notification_repository.list_channels()
            except Exception as e:
                logger.warning(f"Could not load channels from repository: {e}")
        return list(self._channels.values())

    def send_notifications(self, alert: Alert) -> None:
        """Public: alert just *fired or is still firing* — apply policy
        filters and dispatch to matching channels (rate-limited per policy)."""
        logger.info(f"Dispatching notification for alert {alert.alert_id}")
        asyncio.create_task(self._send_notifications_async(alert, event="firing"))

    def notify_alert_resolved(self, alert: Alert) -> None:
        """Public: alert just *resolved*. Always emits a toast informing
        the user the alert cleared (per the UX rule that clears are loud
        enough to notice). Per-policy non-toast clears still respect each
        policy's notify_on_clear flag — only policies that opted in fire
        their channels here, and only for alerts they actually dispatched
        on in the first place."""
        logger.info(f"Dispatching CLEAR notification for alert {alert.alert_id}")
        asyncio.create_task(self._send_notifications_async(alert, event="resolved"))

    def notify_alert_acknowledged(self, alert: Alert) -> None:
        """Public: alert was acknowledged via the UI or API. Emits a single
        toast confirming the acknowledgement. Non-toast channels are
        intentionally NOT dispatched — that's the whole point of acking.
        Subsequent continuing-breach cycles for this alert won't fire any
        non-toast notification either (see _policies_that_should_dispatch)."""
        logger.info(f"Dispatching ACK notification for alert {alert.alert_id}")
        asyncio.create_task(self._send_notifications_async(alert, event="acknowledged"))

    def _enabled_policies(self) -> list:
        if self.notification_repository is None:
            return []
        try:
            all_configs = self.notification_repository.list_configs()
            return [c for c in all_configs if getattr(c, "enabled", True)]
        except Exception as e:
            logger.warning(f"Could not load notification configs: {e}")
            return []

    def _policies_that_should_dispatch(self, alert: Alert, event: str,
                                       policies: Optional[list] = None) -> set[str]:
        """Apply ALL policy filters (severity/source/name/host + min_alarm_count
        + repeat_interval + notify_on_clear) and return the set of channel_id
        strings to deliver to. event ∈ {"firing", "resolved", "acknowledged"}.

        Side-effects: updates self._breach_count, self._last_dispatch_ts,
        self._dispatched_first, self._breach_count.pop on resolve/ack.
        """
        if policies is None:
            policies = self._enabled_policies()
        alert_id = str(getattr(alert, "alert_id", ""))

        # Acknowledge path: NEVER dispatch via non-toast channels. The
        # whole point of acking is to silence everything but the in-UI
        # toast confirmation. We still clear per-alert state so that if
        # the alert flaps and re-fires later, min_alarm_count starts over.
        if event == "acknowledged":
            self._breach_count.pop(alert_id, None)
            return set()

        # Once an alert is in ACKNOWLEDGED state, suppress non-toast
        # dispatch on every subsequent "firing" cycle. The dispatcher
        # never sends to email/webhook/etc. for ack'd alerts.
        if event == "firing":
            status = str(getattr(alert, "status", "")).lower()
            if status == "acknowledged":
                return set()

        # Resolution path: emit per-policy clear notifications only.
        if event == "resolved":
            matched: set[str] = set()
            for policy in policies:
                try:
                    if not policy.matches_alert(alert):
                        continue
                    if not getattr(policy, "notify_on_clear", False):
                        continue
                    key = (str(policy.config_id), alert_id)
                    if key not in self._dispatched_first:
                        # never dispatched a firing notification for this
                        # alert via this policy — don't send a stray clear
                        continue
                    for cid in (policy.channels or []):
                        matched.add(str(cid))
                except Exception as e:
                    logger.warning("policy %s clear-eval failed: %s",
                                   getattr(policy, "config_id", "?"), e)
            # Forget per-alert state so a future re-fire starts fresh.
            self._breach_count.pop(alert_id, None)
            # Keep _dispatched_first / _last_dispatch_ts cleared too so the
            # next fire cycle's min_alarm_count gate starts fresh.
            for k in list(self._dispatched_first):
                if k[1] == alert_id:
                    self._dispatched_first.discard(k)
            for k in list(self._last_dispatch_ts):
                if k[1] == alert_id:
                    self._last_dispatch_ts.pop(k, None)
            return matched

        # Firing path: increment breach count, then for each matching policy
        # check min_alarm_count + repeat_interval before adding its channels.
        breaches = self._breach_count.get(alert_id, 0) + 1
        self._breach_count[alert_id] = breaches
        now = time.time()

        matched: set[str] = set()
        verdicts: list[str] = []
        for policy in policies:
            pid = str(getattr(policy, "config_id", "?"))[:8]
            pname = getattr(policy, "name", "") or pid
            try:
                if not policy.matches_alert(alert):
                    verdicts.append(f"{pname}=filter_miss")
                    continue
                min_count = int(getattr(policy, "min_alarm_count", 1) or 1)
                if breaches < min_count:
                    verdicts.append(f"{pname}=min_count({breaches}/{min_count})")
                    continue
                key = (str(policy.config_id), alert_id)
                window_s = int(getattr(policy, "repeat_interval_minutes", 0) or 0) * 60
                last = self._last_dispatch_ts.get(key, 0)
                if window_s > 0 and key in self._dispatched_first and (now - last) < window_s:
                    verdicts.append(f"{pname}=repeat_suppressed({int(now-last)}s/{window_s}s)")
                    continue
                cids = [str(c) for c in (policy.channels or [])]
                if not cids:
                    verdicts.append(f"{pname}=matched_but_no_channels")
                    continue
                matched.update(cids)
                verdicts.append(f"{pname}=matched({len(cids)}ch)")
                self._last_dispatch_ts[key] = now
                self._dispatched_first.add(key)
            except Exception as e:
                verdicts.append(f"{pname}=eval_error:{e}")
                logger.warning("policy %s firing-eval failed: %s", pid, e)

        logger.info(
            "dispatch alert=%s metric=%s/%s host=%s sev=%s policies=%d breaches=%d verdicts=[%s] → %d channel(s)",
            alert_id[:8], getattr(alert, "metric_source", "?"),
            getattr(alert, "metric_name", "?"), getattr(alert, "source_host", "?"),
            getattr(alert, "severity", "?"), len(policies), breaches,
            ", ".join(verdicts) if verdicts else "no enabled policies",
            len(matched),
        )
        return matched

    def _is_incident_joiner(self, alert) -> bool:
        iid = getattr(alert, "incident_id", None)
        return bool(iid) and iid != str(alert.alert_id)

    def _incident_channel_suppressed(self, alert, event: str) -> bool:
        """Suppress channel (non-toast) dispatch for firing joiner alerts
        once the incident has already notified."""
        cfg = getattr(settings.alarm_engine, "correlation", None)
        if event != "firing" or not bool(getattr(cfg, "notify_per_incident", True)):
            return False
        iid = getattr(alert, "incident_id", None)
        return bool(iid and self._is_incident_joiner(alert)
                    and iid in self._incident_dispatched)

    def _record_incident_dispatch(self, alert) -> None:
        iid = getattr(alert, "incident_id", None)
        if iid:
            self._incident_dispatched.setdefault(iid, time.monotonic())

    def _sweep_incident_dispatched(self) -> None:
        """Drop dispatch claims for incidents with no ongoing member."""
        if self.alert_repository is None or not self._incident_dispatched:
            return
        try:
            live = {getattr(a, "incident_id", None)
                    for a in self.alert_repository.get_active()}
            for iid in list(self._incident_dispatched):
                if iid not in live:
                    self._incident_dispatched.pop(iid, None)
        except Exception:
            pass

    def _incident_size(self, alert) -> int:
        iid = getattr(alert, "incident_id", None)
        if not iid or self.alert_repository is None:
            return 1
        try:
            return max(1, sum(1 for a in self.alert_repository.get_active()
                              if getattr(a, "incident_id", None) == iid))
        except Exception:
            return 1

    async def _send_notifications_async(self, alert: Alert, event: str = "firing") -> None:
        """Async implementation: loads channels + policies and dispatches.

        Policy semantics (NotificationConfig acts as an alarm policy):

          * Each enabled policy has filters (min_severity, metric_sources,
            metric_names, source_hosts). A policy matches an alert when ALL
            its filters pass (empty filter = permissive).
          * min_alarm_count gates the FIRST dispatch — a policy with N=5
            waits for 5 consecutive eval cycles of the same alert.
          * repeat_interval_minutes rate-limits subsequent dispatches per
            (policy, alert) pair.
          * notify_on_clear=True triggers a separate dispatch when the
            alert resolves (event="resolved"), addressed to the same
            channel set.
          * Non-toast channels NEVER fire without an enabled, matching
            policy. "Channel enabled" is a necessary but not sufficient
            condition — a policy must select the channel.

        Toast is independent of policies (it's the in-browser default), but
        sticky behaviour is still derived from any enabled policy with
        auto_dismiss=False.
        """
        channels = await self._get_all_channels_async()
        policies = self._enabled_policies()
        sticky = any(not getattr(p, "auto_dismiss", True) for p in policies)

        # _policies_that_should_dispatch has side effects on the breach
        # counter — call it exactly once per event. With no enabled
        # policies it returns an empty set, so non-toast channels stay
        # silent and only the first-breach toast still fires.
        breaches_before = self._breach_count.get(str(getattr(alert, "alert_id", "")), 0)
        matched_channel_ids = self._policies_that_should_dispatch(alert, event, policies)
        is_first_breach = (event == "firing" and breaches_before == 0)

        tasks = []
        # Toast fires on resolve/ack, first breach, or any policy match.
        # Uses the pre-suppression matched set — toasts are never suppressed.
        emit_toast = (
            self.websocket_send is not None
            and (event in ("resolved", "acknowledged")
                 or is_first_breach
                 or bool(matched_channel_ids))
        )
        if emit_toast:
            tasks.append(self._send_toast(alert, sticky=sticky, event=event))

        # #215: sweep stale claims, then clear non-toast channels once the
        # incident already dispatched.
        self._sweep_incident_dispatched()
        incident_suppressed = self._incident_channel_suppressed(alert, event)
        if incident_suppressed:
            matched_channel_ids = set()

        # #215: claim the incident before any send so same-cycle joiners
        # already see it as dispatched (this prefix never yields).
        claimed_iid = None
        if event == "firing" and matched_channel_ids and not incident_suppressed:
            cfg = getattr(settings.alarm_engine, "correlation", None)
            iid = getattr(alert, "incident_id", None)
            if iid and bool(getattr(cfg, "notify_per_incident", True)):
                if iid not in self._incident_dispatched:
                    claimed_iid = iid
                self._incident_dispatched.setdefault(iid, time.monotonic())

        def _passes_policy(ch) -> bool:
            return str(ch.channel_id) in matched_channel_ids

        email_channels   = [c for c in channels if c.channel_type == ChannelType.EMAIL   and c.enabled and _passes_policy(c)]
        sms_channels     = [c for c in channels if c.channel_type == ChannelType.SMS     and c.enabled and _passes_policy(c)]
        webhook_channels = [c for c in channels if c.channel_type == ChannelType.WEBHOOK and c.enabled and _passes_policy(c)]
        discord_channels = [c for c in channels if c.channel_type == ChannelType.DISCORD and c.enabled and _passes_policy(c)]

        if matched_channel_ids and not (email_channels or sms_channels or webhook_channels or discord_channels):
            # Policies matched but every selected channel is either disabled
            # or has no representation in the loaded channel list. Surface
            # both the policy-selected IDs and what's actually loaded so the
            # operator can spot a stale channel reference.
            loaded_ids = {str(c.channel_id): (c.channel_type.value, c.enabled) for c in channels}
            logger.warning(
                "alert %s: policies selected %d channel(s) but none dispatchable "
                "(matched=%s, loaded_channels=%s)",
                str(getattr(alert, "alert_id", "?"))[:8],
                len(matched_channel_ids), sorted(matched_channel_ids), loaded_ids,
            )
        elif incident_suppressed:
            logger.info(
                "alert %s: incident %s already dispatched — non-toast channels silent",
                str(getattr(alert, "alert_id", "?"))[:8],
                getattr(alert, "incident_id", "?"),
            )
        elif not matched_channel_ids and event == "firing":
            logger.info(
                "alert %s: no policy matched on firing — non-toast channels silent "
                "(severity=%s metric=%s/%s host=%s)",
                str(getattr(alert, "alert_id", "?"))[:8],
                getattr(alert, "severity", "?"),
                getattr(alert, "metric_source", "?"),
                getattr(alert, "metric_name", "?"),
                getattr(alert, "source_host", "?"),
            )

        if email_channels:
            tasks.append(self._send_email_channels(alert, email_channels))
        if sms_channels:
            tasks.append(self._send_sms_channels(alert, sms_channels))
        if webhook_channels:
            tasks.append(self._send_webhook_channels(alert, webhook_channels))
        if discord_channels:
            tasks.append(self._send_discord_channels(alert, discord_channels))

        if tasks:
            await self._run_all(tasks)

        # #215: release the claim when no non-toast send for this alert
        # succeeded, so joiners aren't silenced by a failed root dispatch.
        alert_key = str(getattr(alert, "alert_id", ""))
        sent_ok = alert_key in self._nontoast_send_ok
        self._nontoast_send_ok.discard(alert_key)
        if claimed_iid and not sent_ok:
            self._incident_dispatched.pop(claimed_iid, None)

    async def _run_all(self, tasks: list[asyncio.Task]) -> None:
        """Run tasks and log any failures."""
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Notification channel {i} failed: {result}")

    async def _send_toast(self, alert: Alert, sticky: bool = False,
                          event: str = "firing") -> None:
        """Send toast notification via WebSocket. event ∈ {"firing",
        "resolved", "acknowledged"}. Resolved/acknowledged toasts use
        severity=info so they read calmer in the UI."""
        if not self.websocket_send:
            return

        is_clear = (event == "resolved")
        is_ack = (event == "acknowledged")
        severity = "info" if (is_clear or is_ack) else str(getattr(alert, "severity", "warning")).lower()
        rule_name = alert.rule_name or "Alert"
        if is_clear:
            rule_name = f"Cleared: {rule_name}"
        elif is_ack:
            rule_name = f"Acknowledged: {rule_name}"

        # Build a body that shows host, source, and metric=value so the user
        # knows which device/metric triggered the alert without opening the
        # events tab. Hostname leads when present.
        body_parts = []
        host = getattr(alert, "source_host", None)
        if host:
            body_parts.append(host)
        if alert.metric_source:
            body_parts.append(alert.metric_source)
        if alert.metric_name and alert.current_value is not None:
            try:
                val_str = f"{round(float(alert.current_value), 2)}"
            except (TypeError, ValueError):
                val_str = str(alert.current_value)
            body_parts.append(f"{alert.metric_name} = {val_str}")
        body = " · ".join(body_parts) if body_parts else (alert.message or "")
        if is_clear:
            body = (body + " (alarm cleared)").strip()
        elif is_ack:
            body = (body + " (acknowledged — further alerts suppressed)").strip()

        toast_data = {
            "type": "notification",
            "action": "toast",
            "data": {
                "title": rule_name,
                "body": body,
                "severity": severity,
                "alert_id": str(alert.alert_id),
                "sticky": bool(sticky),
                "source_host": host or "",
                "metric_source": alert.metric_source or "",
                "metric_name": alert.metric_name or "",
                "incident_id": str(getattr(alert, "incident_id", "") or ""),
                "incident_size": self._incident_size(alert),
            },
        }

        try:
            await self.websocket_send(json.dumps(toast_data))
            logger.info(f"Toast notification sent for {alert.alert_id}")
            # Record the delivery so it shows up in the Notifications → Delivery
            # History panel. Without this only test messages would appear.
            if self.notification_repository is not None:
                try:
                    self.notification_repository.record_delivery(
                        channel_id=None,
                        channel_type="toast",
                        title=rule_name,
                        body=body,
                        severity=severity,
                        recipient="webui",
                        success=True,
                    )
                except Exception as rec_e:
                    logger.warning(f"Could not record toast delivery: {rec_e}")
        except Exception as e:
            logger.error(f"Failed to send toast: {e}")

    async def _send_email_channels(self, alert: Alert, channels: list[NotificationChannel]) -> None:
        """Send email notifications via the given channel list."""
        for channel in channels:
            config = channel.config
            if not config.email or not config.email.enabled:
                continue
            recipients_str = config.email.to_email
            subject = f"[{alert.severity.upper()}] Alarm Alert: {(alert.rule_name or 'Alert')}"
            body = f"""Alarm Alert

Rule: {(alert.rule_name or 'Alert')}
Metric: {alert.metric_source}/{alert.metric_name}
Current Value: {alert.current_value}
Threshold: {alert.threshold_value}
Severity: {alert.severity}
Status: {alert.status}
Message: {alert.message}
Time: {alert.created_at}
"""
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = "alarm-engine@llm-systems"
            msg["To"] = recipients_str
            err = None
            try:
                await asyncio.to_thread(self._send_sync_email, msg, config)
                logger.info(f"Email sent for alert {alert.alert_id}")
            except Exception as e:
                err = str(e)
                logger.error(f"Failed to send email: {e}")
            self._record_delivery(alert, channel, "email", recipients_str,
                                  subject, body, success=err is None, error_message=err)

    def _record_delivery(self, alert: Alert, channel: "NotificationChannel",
                         channel_type: str, recipient: str,
                         title: str, body: str,
                         success: bool, error_message: Optional[str] = None) -> None:
        """Persist a delivery row so the Notifications History panel reflects
        non-toast sends too. Best-effort: failure to record never blocks the
        actual notification path."""
        if success:
            self._nontoast_send_ok.add(str(getattr(alert, "alert_id", "")))
            self._record_incident_dispatch(alert)
        if self.notification_repository is None:
            return
        try:
            self.notification_repository.record_delivery(
                channel_id=str(channel.channel_id) if channel is not None else None,
                channel_type=channel_type,
                title=title,
                body=body,
                severity=str(getattr(alert, "severity", "info")).lower(),
                recipient=recipient or "",
                success=success,
                error_message=error_message,
            )
        except Exception as e:
            logger.warning("record_delivery(%s) failed: %s", channel_type, e)

    async def _send_sms_channels(self, alert: Alert, channels: list[NotificationChannel]) -> None:
        """Send SMS notifications via the given channel list."""
        for channel in channels:
            config = channel.config
            if not config.sms or not config.sms.enabled:
                continue
            message = f"[{alert.severity.upper()}] {(alert.rule_name or 'Alert')}: {alert.message}"
            logger.info(f"SMS would be sent to {config.sms.to_number}: {message}")
            self._record_delivery(alert, channel, "sms", config.sms.to_number,
                                  (alert.rule_name or "Alert"), message, success=True)

    async def _send_webhook_channels(self, alert: Alert, channels: list[NotificationChannel]) -> None:
        """Send webhook notifications via the given channel list."""
        for channel in channels:
            config = channel.config
            if not config.webhook or not config.webhook.enabled:
                continue
            payload = {
                "alert_id": str(alert.alert_id),
                "rule_id": str(alert.rule_id),
                "rule_name": (alert.rule_name or "Alert"),
                "metric_source": alert.metric_source,
                "metric_name": alert.metric_name,
                "current_value": alert.current_value,
                "threshold_value": alert.threshold_value,
                "severity": alert.severity,
                "status": alert.status,
                "message": alert.message,
                "created_at": alert.created_at.isoformat(),
            }
            headers = {"Content-Type": "application/json"}
            if config.webhook.headers:
                headers.update(config.webhook.headers)
            err = None
            try:
                async with httpx.AsyncClient(timeout=settings.notifications.timeouts.http) as client:
                    await client.post(config.webhook.url, json=payload, headers=headers)
                logger.info(f"Webhook sent for alert {alert.alert_id}")
            except Exception as e:
                err = str(e)
                logger.error(f"Failed to send webhook: {e}")
            self._record_delivery(alert, channel, "webhook", config.webhook.url,
                                  (alert.rule_name or "Alert"),
                                  alert.message or "", success=err is None, error_message=err)

    async def _send_discord_channels(self, alert: Alert, channels: list[NotificationChannel]) -> None:
        """Send Discord notifications via the given channel list."""
        for channel in channels:
            config = channel.config
            if not config.discord or not config.discord.enabled:
                continue
            color_map = {"critical": 16711680, "warning": 16776960, "info": 29696}
            color = color_map.get(alert.severity, 29696)
            embed = {
                "title": f"Alarm: {(alert.rule_name or 'Alert')}",
                "description": alert.message,
                "color": color,
                "fields": [
                    {"name": "Metric", "value": f"{alert.metric_source}/{alert.metric_name}", "inline": True},
                    {"name": "Value", "value": str(alert.current_value), "inline": True},
                    {"name": "Threshold", "value": str(alert.threshold_value), "inline": True},
                    {"name": "Status", "value": alert.status, "inline": True},
                ],
                "footer": {"text": f"Alert ID: {alert.alert_id}"},
                "timestamp": alert.created_at.isoformat(),
            }
            payload = {"embeds": [embed]}
            if config.discord.username:
                payload["username"] = config.discord.username
            err = None
            try:
                async with httpx.AsyncClient(timeout=settings.notifications.timeouts.http) as client:
                    await client.post(config.discord.webhook_url, json=payload)
                logger.info(f"Discord notification sent for alert {alert.alert_id}")
            except Exception as e:
                err = str(e)
                logger.error(f"Failed to send Discord notification: {e}")
            self._record_delivery(alert, channel, "discord", config.discord.webhook_url,
                                  embed["title"], alert.message or "",
                                  success=err is None, error_message=err)

    def _send_sync_email(self, msg: MIMEText, config) -> None:
        """Synchronous email send (run in thread). Reads SMTP host/port/
        user/password from [notifications.smtp] in llm-systems.toml; falls
        back to localhost:25 only if no SMTP server is configured. STARTTLS
        + login are used whenever the port isn't 25 (i.e. any real relay)."""
        try:
            smtp = settings.notifications.smtp
            host = (smtp.server or "").strip() or "localhost"
            port = int(smtp.port or 25)
            user = (smtp.user or "").strip() or None
            password = (smtp.password or "").strip() or None
        except Exception:
            host, port, user, password = "localhost", 25, None, None

        # Many providers (iCloud, Gmail) require From: to match the
        # authenticated mailbox. Rewrite it when we have an SMTP user.
        if user:
            if msg.get("From"):
                msg.replace_header("From", user)
            else:
                msg["From"] = user

        with smtplib.SMTP(host, port, timeout=settings.notifications.timeouts.smtp) as server:
            server.ehlo()
            if port != 25:
                server.starttls()
                server.ehlo()
            if user and password:
                server.login(user, password)
            server.send_message(msg)

    def add_custom_rule(self, rule_id: str, channel_ids: list[str]) -> None:
        """Add a custom notification rule mapping rule to channels."""
        self._custom_rules[rule_id] = channel_ids
        logger.info(f"Added custom notification rule for {rule_id}")

    def remove_custom_rule(self, rule_id: str) -> bool:
        """Remove a custom notification rule."""
        if rule_id in self._custom_rules:
            del self._custom_rules[rule_id]
            logger.info(f"Removed custom notification rule {rule_id}")
            return True
        return False

    def get_custom_rules(self) -> dict[str, list[str]]:
        """Get all custom notification rules."""
        return dict(self._custom_rules)

    def get_channel_status(self) -> dict:
        """Get status of all notification channels."""
        return dict(self._channels_enabled)

    async def send_notification(
        self,
        title: str,
        body: str,
        severity: str = "info",
        channel_type: Optional[ChannelType] = None,
        channel_id: Optional[str] = None,
        recipient: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        config_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Send a notification directly (used for testing and ad-hoc alerts).

        If channel_type is provided, sends via that type.
        If channel_id is provided, sends via that specific channel.
        Otherwise falls back to toast.
        """
        delivery_id = str(uuid4())
        success = False
        error_message = ""
        response_code = None

        try:
            if channel_id:
                # Send via specific channel
                channel = self.get_channel(channel_id)
                if not channel:
                    return {"success": False, "error": f"Channel {channel_id} not found"}
                channel_type = channel.channel_type
                recipient = recipient or (channel.config.recipient if hasattr(channel.config, "recipient") else "local")

            if not channel_type:
                channel_type = ChannelType.TOAST

            # Dispatch based on channel type
            if channel_type == ChannelType.TOAST:
                if self.websocket_send:
                    toast_data = {
                        "type": "notification",
                        "action": "toast",
                        "data": {
                            "title": title,
                            "body": body,
                            "severity": severity,
                            "delivery_id": delivery_id,
                        },
                    }
                    await self.websocket_send(json.dumps(toast_data))
                    success = True
                else:
                    logger.info(f"[Toast] {title}: {body}")
                    success = True

            elif channel_type == ChannelType.EMAIL:
                to_email = recipient or "localhost"
                subject = title
                msg = MIMEText(body)
                msg["Subject"] = subject
                msg["From"] = "alarm-engine@llm-systems"
                msg["To"] = to_email
                try:
                    await asyncio.to_thread(self._send_sync_email, msg, type("Config", (), {"email": type("EmailCfg", (), {"to_email": to_email, "enabled": True})()})())
                    success = True
                except Exception as e:
                    error_message = str(e)

            elif channel_type == ChannelType.WEBHOOK:
                payload = {
                    "title": title,
                    "body": body,
                    "severity": severity,
                    "metadata": metadata or {},
                    "delivery_id": delivery_id,
                }
                async with httpx.AsyncClient(timeout=settings.notifications.timeouts.http) as client:
                    resp = await client.post(recipient or "http://localhost:9999/webhook", json=payload)
                    response_code = resp.status_code
                    success = 200 <= resp.status_code < 300

            elif channel_type == ChannelType.DISCORD:
                embed = {
                    "title": title,
                    "description": body,
                    "color": {"critical": 16711680, "warning": 16776960, "info": 29696}.get(severity, 29696),
                    "fields": [
                        {"name": "Severity", "value": severity, "inline": True},
                        {"name": "Delivery ID", "value": delivery_id, "inline": True},
                    ],
                }
                async with httpx.AsyncClient(timeout=settings.notifications.timeouts.http) as client:
                    resp = await client.post(recipient or "https://discord.com/api/webhooks/fake", json={"embeds": [embed]})
                    response_code = resp.status_code
                    success = 200 <= resp.status_code < 300

            elif channel_type == ChannelType.SMS:
                logger.info(f"[SMS] Would send to {recipient}: {body}")
                success = True

            else:
                logger.info(f"[{channel_type}] {title}: {body}")
                success = True

        except Exception as e:
            error_message = str(e)
            logger.error(f"Notification send failed: {e}")

        return {
            "success": success,
            "delivery_id": delivery_id,
            "channel_type": channel_type.value if channel_type else "unknown",
            "recipient": recipient,
            "response_code": response_code,
            "error": error_message,
        }
