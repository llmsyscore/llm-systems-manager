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

# Strong references to in-flight dispatch tasks; released when each finishes.
_dispatch_tasks: set = set()


def _spawn_dispatch(coro) -> None:
    """create_task + keep a strong reference until the task finishes."""
    task = asyncio.create_task(coro)
    _dispatch_tasks.add(task)
    task.add_done_callback(_dispatch_tasks.discard)


def _alert_headline(alert: Alert, event: str) -> "tuple[str, str, str]":
    """(title, body, severity) for one alert event. Shared by the toast and
    web-push channels; resolved/acknowledged events read as info."""
    is_clear = (event == "resolved")
    is_ack = (event == "acknowledged")
    severity = ("info" if (is_clear or is_ack)
                else str(getattr(alert, "severity", "warning")).lower())
    title = getattr(alert, "rule_name", None) or "Alert"
    if is_clear:
        title = f"Cleared: {title}"
    elif is_ack:
        title = f"Acknowledged: {title}"

    # Host, source and metric=value, so the notification names the device and
    # the measurement without opening the events tab.
    parts = []
    host = getattr(alert, "source_host", None)
    if host:
        parts.append(host)
    if alert.metric_source:
        parts.append(alert.metric_source)
    if alert.metric_name and alert.current_value is not None:
        try:
            val_str = f"{round(float(alert.current_value), 2)}"
        except (TypeError, ValueError):
            val_str = str(alert.current_value)
        parts.append(f"{alert.metric_name} = {val_str}")
    body = " · ".join(parts) if parts else (getattr(alert, "message", "") or "")
    if is_clear:
        body = (body + " (alarm cleared)").strip()
    elif is_ack:
        body = (body + " (acknowledged — further alerts suppressed)").strip()
    return title, body, severity


# ── web push (#538) ─────────────────────────────────────────────────────────
# Tokens that read as "not configured", matching the ingest/management gate.
_UNSET_TOKENS = {"", "REPLACE_ME"}


def _webpush_url(config) -> str:
    """Manager notify endpoint. Blank config falls back to the co-located
    manager on loopback, which is the single-host deployment."""
    url = (getattr(config, "url", "") or "").strip()
    if url:
        return url
    port = int(getattr(settings.manager, "port", 5000) or 5000)
    return f"http://127.0.0.1:{port}/api/companion/push/notify"


def _webpush_token(config) -> str:
    """Bearer for the notify endpoint: the channel's own, else the shared
    alarm-engine token the manager already accepts."""
    for raw in (getattr(config, "token", None),
                getattr(settings.alarm_engine, "management_token", ""),
                getattr(settings.alarm_engine, "ingest_token", "")):
        tok = (raw or "").strip()
        if tok not in _UNSET_TOKENS:
            return tok
    return ""


class NotificationDispatcher:
    """Dispatches notifications through configured channels."""

    # Minimum seconds between _sweep_incident_dispatched full scans.
    _SWEEP_MIN_INTERVAL_S = 5.0

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
        self._sms_unsupported_warned: set = set()

        # Per-alert state used by the policy filters. In-memory only;
        # restarts reset everything (acceptable — a freshly-restarted
        # service should re-evaluate suppression windows).
        # consecutive eval-cycles a given alert has been firing:
        self._breach_count: dict[str, int] = {}
        # incident_id -> first-dispatch monotonic ts, for #215 suppression:
        self._incident_dispatched: dict[str, float] = {}
        # alert_ids with at least one successful channel send this cycle:
        self._send_ok: set[str] = set()
        # last dispatch ts keyed by (config_id_str, alert_id_str):
        self._last_dispatch_ts: dict[tuple[str, str], float] = {}
        # remembers whether a given policy has ever dispatched for an
        # alert — needed so notify_on_clear only fires when the alert
        # actually reached this policy in the first place:
        self._dispatched_first: set[tuple[str, str]] = set()
        # monotonic ts of the last full _sweep_incident_dispatched scan:
        self._last_sweep_ts: float = float("-inf")

        # Channel enable flags (derived from channel configs)
        self._channels_enabled: dict[str, bool] = {
            "toast": True,
            "email": False,
            "sms": False,
            "webhook": False,
            "discord": False,
            "webpush": False,
        }

        # Custom notification rules (rule_id -> list of channel_ids)
        self._custom_rules: dict[str, list[str]] = {}

    def add_channel(self, channel_create: NotificationChannelCreate) -> NotificationChannel:
        """Add a notification channel."""
        channel = channel_create.to_channel()
        self._channels[str(channel.channel_id)] = channel
        self._update_channel_flags()
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
            elif channel.channel_type == ChannelType.WEBPUSH:
                self._channels_enabled["webpush"] = channel.enabled

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
        _spawn_dispatch(self._send_notifications_async(alert, event="firing"))

    def notify_alert_resolved(self, alert: Alert) -> None:
        """Public: alert just *resolved*. Policies with notify_on_clear
        fire their channels (toast included) for alerts they dispatched."""
        logger.info(f"Dispatching CLEAR notification for alert {alert.alert_id}")
        _spawn_dispatch(self._send_notifications_async(alert, event="resolved"))

    def notify_alert_acknowledged(self, alert: Alert) -> None:
        """Public: alert was acknowledged. No channel is dispatched, and
        later breach cycles stay silent (see _policies_that_should_dispatch)."""
        logger.info(f"Dispatching ACK notification for alert {alert.alert_id}")
        _spawn_dispatch(self._send_notifications_async(alert, event="acknowledged"))

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

        # Acknowledge path: no channel dispatch; per-alert state clears so
        # a later re-fire starts min_alarm_count over.
        if event == "acknowledged":
            self._breach_count.pop(alert_id, None)
            return set()

        # Acknowledged alerts stay silent on every later "firing" cycle.
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
        """Suppress channel dispatch for firing joiner alerts once the
        incident has already notified."""
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
        """Drop claims for incidents with no ongoing member, or whose root
        alert (the ongoing alert whose alert_id equals the incident_id) has
        closed. Runs at most once per _SWEEP_MIN_INTERVAL_S."""
        if self.alert_repository is None or not self._incident_dispatched:
            return
        now = time.monotonic()
        if now - self._last_sweep_ts < self._SWEEP_MIN_INTERVAL_S:
            return
        self._last_sweep_ts = now
        try:
            active = self.alert_repository.get_active()
            live_incidents = {getattr(a, "incident_id", None) for a in active}
            live_alert_ids = {str(getattr(a, "alert_id", "")) for a in active}
            for iid in list(self._incident_dispatched):
                if iid not in live_incidents or iid not in live_alert_ids:
                    self._incident_dispatched.pop(iid, None)
        except Exception as e:
            logger.debug("incident-dispatch sweep skipped: %s", e)

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
          * No channel fires without an enabled, matching policy.
            "Channel enabled" is necessary but not sufficient — a policy
            must select the channel.

        Toast follows the same rules (#811): it fires only when an enabled
        policy selects an enabled Toast channel; sticky comes from those
        policies' auto_dismiss=False.
        """
        channels = await self._get_all_channels_async()
        policies = self._enabled_policies()

        # _policies_that_should_dispatch has side effects on the breach
        # counter — call it exactly once per event. With no enabled
        # policies it returns an empty set, so every channel stays silent.
        matched_channel_ids = self._policies_that_should_dispatch(alert, event, policies)

        tasks = []
        # #215: sweep stale claims, then clear channels once the incident
        # already dispatched.
        self._sweep_incident_dispatched()
        incident_suppressed = self._incident_channel_suppressed(alert, event)
        if incident_suppressed:
            matched_channel_ids = set()

        # #215: claims the incident for this alert; no await occurs above
        # this point in the function.
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

        toast_channels   = [c for c in channels if c.channel_type == ChannelType.TOAST   and c.enabled and _passes_policy(c)]
        email_channels   = [c for c in channels if c.channel_type == ChannelType.EMAIL   and c.enabled and _passes_policy(c)]
        sms_channels     = [c for c in channels if c.channel_type == ChannelType.SMS     and c.enabled and _passes_policy(c)]
        webhook_channels = [c for c in channels if c.channel_type == ChannelType.WEBHOOK and c.enabled and _passes_policy(c)]
        discord_channels = [c for c in channels if c.channel_type == ChannelType.DISCORD and c.enabled and _passes_policy(c)]
        webpush_channels = [c for c in channels if c.channel_type == ChannelType.WEBPUSH and c.enabled and _passes_policy(c)]

        if matched_channel_ids and not (toast_channels or email_channels or sms_channels or webhook_channels or discord_channels or webpush_channels):
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
                "alert %s: incident %s already dispatched — channels silent",
                str(getattr(alert, "alert_id", "?"))[:8],
                getattr(alert, "incident_id", "?"),
            )
        elif not matched_channel_ids and event == "firing":
            logger.info(
                "alert %s: no policy matched on firing — channels silent "
                "(severity=%s metric=%s/%s host=%s)",
                str(getattr(alert, "alert_id", "?"))[:8],
                getattr(alert, "severity", "?"),
                getattr(alert, "metric_source", "?"),
                getattr(alert, "metric_name", "?"),
                getattr(alert, "source_host", "?"),
            )

        if toast_channels and self.websocket_send is not None:
            sticky, dismiss_s = self._toast_behaviour(alert, policies, toast_channels)
            tasks.append(self._send_toast(alert, sticky=sticky, event=event,
                                          channel=toast_channels[0],
                                          extra_channels=toast_channels[1:],
                                          dismiss_seconds=dismiss_s))
        if email_channels:
            tasks.append(self._send_email_channels(alert, email_channels, event=event))
        if sms_channels:
            tasks.append(self._send_sms_channels(alert, sms_channels, event=event))
        if webhook_channels:
            tasks.append(self._send_webhook_channels(alert, webhook_channels, event=event))
        if discord_channels:
            tasks.append(self._send_discord_channels(alert, discord_channels, event=event))
        if webpush_channels:
            tasks.append(self._send_webpush_channels(alert, webpush_channels, event=event))

        if tasks:
            await self._run_all(tasks)

        # #215: releases the claim when no channel send for this alert succeeded.
        alert_key = str(getattr(alert, "alert_id", ""))
        sent_ok = alert_key in self._send_ok
        self._send_ok.discard(alert_key)
        if claimed_iid and not sent_ok:
            self._incident_dispatched.pop(claimed_iid, None)

    async def _run_all(self, tasks: list[asyncio.Task]) -> None:
        """Run tasks and log any failures."""
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Notification channel {i} failed: {result}")

    def _toast_behaviour(self, alert: Alert, policies: list,
                         toast_channels: list) -> "tuple[bool, int]":
        """(sticky, dismiss_seconds) from the matching policies that select
        one of these toast channels: sticky if any has auto_dismiss=False,
        else the longest toast_dismiss_seconds (default 10)."""
        ids = {str(c.channel_id) for c in toast_channels}
        sticky, seconds = False, 0
        for p in policies:
            if not ids & {str(c) for c in (getattr(p, "channels", None) or [])}:
                continue
            try:
                if not p.matches_alert(alert):
                    continue
            except Exception:
                continue
            if not getattr(p, "auto_dismiss", True):
                sticky = True
            seconds = max(seconds, int(getattr(p, "toast_dismiss_seconds", 10) or 10))
        return sticky, (seconds or 10)

    async def _send_toast(self, alert: Alert, sticky: bool = False,
                          event: str = "firing", channel=None,
                          extra_channels: Optional[list] = None,
                          dismiss_seconds: int = 10) -> None:
        """Send one toast over the WebSocket and record a delivery per toast
        channel. event ∈ {"firing", "resolved", "acknowledged"}."""
        if not self.websocket_send:
            return

        rule_name, body, severity = _alert_headline(alert, event)
        host = getattr(alert, "source_host", None)

        toast_data = {
            "type": "notification",
            "action": "toast",
            "data": {
                "title": rule_name,
                "body": body,
                "severity": severity,
                "alert_id": str(alert.alert_id),
                "sticky": bool(sticky),
                "dismiss_seconds": int(dismiss_seconds or 10),
                "source_host": host or "",
                "metric_source": alert.metric_source or "",
                "metric_name": alert.metric_name or "",
                "incident_id": str(getattr(alert, "incident_id", "") or ""),
                "incident_size": self._incident_size(alert),
            },
        }

        err = None
        try:
            await self.websocket_send(json.dumps(toast_data))
            logger.info(f"Toast notification sent for {alert.alert_id}")
        except Exception as e:
            err = str(e)
            logger.error(f"Failed to send toast: {e}")
        for ch in [channel] + list(extra_channels or []):
            self._record_delivery(alert, ch, "toast", "webui", rule_name, body,
                                  success=err is None, error_message=err, event=event)

    async def _send_email_channels(self, alert: Alert, channels: list[NotificationChannel],
                                   event: str = "firing") -> None:
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
                                  subject, body, success=err is None, error_message=err,
                                  event=event)

    def _record_delivery(self, alert: Alert, channel: "NotificationChannel",
                         channel_type: str, recipient: str,
                         title: str, body: str,
                         success: bool, error_message: Optional[str] = None,
                         event: str = "firing") -> None:
        """Persist a delivery row tagged with the alert id. Best-effort:
        failure to record never blocks the notification path."""
        if success:
            self._send_ok.add(str(getattr(alert, "alert_id", "")))
            if event == "firing":
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
                metadata={"alert_id": str(getattr(alert, "alert_id", ""))},
            )
        except Exception as e:
            logger.warning("record_delivery(%s) failed: %s", channel_type, e)

    async def _send_sms_channels(self, alert: Alert, channels: list[NotificationChannel],
                                 event: str = "firing") -> None:
        """Send SMS notifications via the given channel list."""
        for channel in channels:
            config = channel.config
            if not config.sms or not config.sms.enabled:
                continue
            message = f"[{alert.severity.upper()}] {(alert.rule_name or 'Alert')}: {alert.message}"
            if channel.channel_id not in self._sms_unsupported_warned:
                self._sms_unsupported_warned.add(channel.channel_id)
                logger.warning("SMS channel to %s is not implemented — nothing sent",
                               config.sms.to_number)
            self._record_delivery(alert, channel, "sms", config.sms.to_number,
                                  (alert.rule_name or "Alert"), message, success=False,
                                  error_message="SMS sending is not implemented (no provider integration)",
                                  event=event)

    async def _send_webhook_channels(self, alert: Alert, channels: list[NotificationChannel],
                                     event: str = "firing") -> None:
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
                    resp = await client.post(config.webhook.url, json=payload, headers=headers)
                if 200 <= resp.status_code < 300:
                    logger.info(f"Webhook sent for alert {alert.alert_id}")
                else:
                    err = f"HTTP {resp.status_code}"
                    logger.error(f"Webhook for alert {alert.alert_id} returned {resp.status_code}")
            except Exception as e:
                err = str(e)
                logger.error(f"Failed to send webhook: {e}")
            self._record_delivery(alert, channel, "webhook", config.webhook.url,
                                  (alert.rule_name or "Alert"),
                                  alert.message or "", success=err is None, error_message=err,
                                  event=event)

    async def _send_discord_channels(self, alert: Alert, channels: list[NotificationChannel],
                                     event: str = "firing") -> None:
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
                    resp = await client.post(config.discord.webhook_url, json=payload)
                if 200 <= resp.status_code < 300:
                    logger.info(f"Discord notification sent for alert {alert.alert_id}")
                else:
                    err = f"HTTP {resp.status_code}"
                    logger.error(f"Discord webhook for alert {alert.alert_id} returned {resp.status_code}")
            except Exception as e:
                err = str(e)
                logger.error(f"Failed to send Discord notification: {e}")
            self._record_delivery(alert, channel, "discord", config.discord.webhook_url,
                                  embed["title"], alert.message or "",
                                  success=err is None, error_message=err,
                                  event=event)

    async def _send_webpush_channels(self, alert: Alert, channels: list[NotificationChannel],
                                     event: str = "firing") -> None:
        """Hand each alert to the manager's companion notify endpoint, which
        owns the VAPID key and the device subscriptions and does the sending."""
        for channel in channels:
            config = channel.config
            if not config.webpush or not config.webpush.enabled:
                continue
            url = _webpush_url(config.webpush)
            token = _webpush_token(config.webpush)
            title, body, severity = _alert_headline(alert, event)
            payload = {
                "title": title,
                "body": body,
                "severity": severity,
                # One tag per alert, so a re-fire replaces the phone's existing
                # notification instead of stacking a duplicate.
                "tag": f"lsm-alert-{alert.alert_id}",
                # Deep link to this alert, not just the Alerts tab.
                "url": f"/companion?tab=alerts&alert={alert.alert_id}",
                "alert_id": str(alert.alert_id),
                "event": event,
            }
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            err = None
            try:
                async with httpx.AsyncClient(
                        timeout=settings.notifications.timeouts.http,
                        verify=bool(getattr(config.webpush, "verify_tls", True))) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                if 200 <= resp.status_code < 300:
                    logger.info("Web push handed off for alert %s", alert.alert_id)
                else:
                    err = f"HTTP {resp.status_code}"
                    logger.error("Web push notify for alert %s returned %s",
                                 alert.alert_id, resp.status_code)
            except Exception as e:
                err = str(e)
                logger.error(f"Failed to hand off web push: {e}")
            self._record_delivery(alert, channel, "webpush", url, title, body,
                                  success=err is None, error_message=err,
                                  event=event)

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

            elif channel_type == ChannelType.WEBPUSH:
                token = _webpush_token(None)
                async with httpx.AsyncClient(timeout=settings.notifications.timeouts.http) as client:
                    resp = await client.post(
                        recipient or _webpush_url(None),
                        json={"title": title, "body": body, "severity": severity,
                              "tag": "lsm-test", "url": "/companion?tab=alerts"},
                        headers={"Authorization": f"Bearer {token}"} if token else {})
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
