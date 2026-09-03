"""#811: toast is a normal channel — it fires only when an enabled policy
routes a matching alert to an enabled Toast channel."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from backend.engine.notification_dispatcher import NotificationDispatcher
from backend.models.notification import ChannelType


class FakeNotifRepo:
    def __init__(self, channels, policies):
        self._channels = list(channels)
        self._policies = list(policies)
        self.deliveries = []

    async def list_channels(self):
        return list(self._channels)

    def list_configs(self):
        return list(self._policies)

    def record_delivery(self, **kw):
        self.deliveries.append(kw)


def _alert(severity="critical", status="active", incident=None):
    aid = uuid4()
    return SimpleNamespace(
        alert_id=aid, rule_id=uuid4(), rule_name="GPU hot",
        metric_source="system", metric_name="gpu_temperature_c",
        current_value=91.2, threshold_value=85.0, severity=severity,
        status=status, message="hot", source_host="llama-box",
        created_at=datetime.now(timezone.utc),
        incident_id=incident or str(aid),
    )


def _toast_channel(enabled=True, cid="toast-1"):
    return SimpleNamespace(channel_id=cid, channel_type=ChannelType.TOAST,
                           enabled=enabled, config=None)


def _policy(channels=("toast-1",), enabled=True, min_count=1,
            notify_on_clear=False, auto_dismiss=True, matches=True,
            dismiss_seconds=10):
    return SimpleNamespace(config_id="pol-1", name="p", enabled=enabled,
                           min_alarm_count=min_count, repeat_interval_minutes=0,
                           channels=list(channels), notify_on_clear=notify_on_clear,
                           auto_dismiss=auto_dismiss,
                           toast_dismiss_seconds=dismiss_seconds,
                           matches_alert=lambda alert: matches)


def _dispatcher(channels, policies):
    sent = []

    async def ws(text):
        sent.append(json.loads(text))

    repo = FakeNotifRepo(channels, policies)
    return NotificationDispatcher(websocket_send=ws, notification_repository=repo), sent, repo


async def test_no_toast_channel_means_no_toast():
    d, sent, _ = _dispatcher([], [_policy()])
    await d._send_notifications_async(_alert(), event="firing")
    assert sent == []


async def test_disabled_toast_channel_is_silent_even_with_a_matching_policy():
    d, sent, _ = _dispatcher([_toast_channel(enabled=False)], [_policy()])
    await d._send_notifications_async(_alert(), event="firing")
    assert sent == []


async def test_enabled_channel_without_any_policy_is_silent():
    d, sent, _ = _dispatcher([_toast_channel()], [])
    await d._send_notifications_async(_alert(), event="firing")
    assert sent == []


async def test_disabled_policy_is_silent():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy(enabled=False)])
    await d._send_notifications_async(_alert(), event="firing")
    assert sent == []


async def test_non_matching_policy_is_silent():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy(matches=False)])
    await d._send_notifications_async(_alert(), event="firing")
    assert sent == []


async def test_enabled_channel_and_matching_policy_toasts_on_firing():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy()])
    alert = _alert()
    await d._send_notifications_async(alert, event="firing")
    assert len(sent) == 1
    assert sent[0]["type"] == "notification" and sent[0]["action"] == "toast"
    assert sent[0]["data"]["alert_id"] == str(alert.alert_id)
    assert sent[0]["data"]["sticky"] is False


async def test_min_alarm_count_defers_the_first_toast():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy(min_count=2)])
    alert = _alert()
    await d._send_notifications_async(alert, event="firing")
    assert sent == []
    await d._send_notifications_async(alert, event="firing")
    assert len(sent) == 1


async def test_clear_toasts_only_when_the_policy_notifies_on_clear():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy(notify_on_clear=True)])
    alert = _alert()
    await d._send_notifications_async(alert, event="firing")
    await d._send_notifications_async(alert, event="resolved")
    assert len(sent) == 2
    assert sent[1]["data"]["severity"] == "info"
    assert sent[1]["data"]["title"].startswith("Cleared:")


async def test_clear_is_silent_without_notify_on_clear():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy(notify_on_clear=False)])
    alert = _alert()
    await d._send_notifications_async(alert, event="firing")
    await d._send_notifications_async(alert, event="resolved")
    assert len(sent) == 1


async def test_acknowledge_sends_no_channel_toast():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy()])
    alert = _alert()
    await d._send_notifications_async(alert, event="firing")
    await d._send_notifications_async(alert, event="acknowledged")
    assert len(sent) == 1


async def test_sticky_follows_the_matching_policy():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy(auto_dismiss=False)])
    await d._send_notifications_async(_alert(), event="firing")
    assert sent[0]["data"]["sticky"] is True


async def test_dismiss_seconds_come_from_the_policy():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy(dismiss_seconds=45)])
    await d._send_notifications_async(_alert(), event="firing")
    assert sent[0]["data"]["dismiss_seconds"] == 45
    assert sent[0]["data"]["sticky"] is False


async def test_dismiss_seconds_default_to_ten():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy()])
    await d._send_notifications_async(_alert(), event="firing")
    assert sent[0]["data"]["dismiss_seconds"] == 10


async def test_joiner_inside_a_dispatched_incident_is_silent():
    d, sent, _ = _dispatcher([_toast_channel()], [_policy()])
    root = _alert()
    await d._send_notifications_async(root, event="firing")
    joiner = _alert(incident=root.incident_id)
    await d._send_notifications_async(joiner, event="firing")
    assert len(sent) == 1


async def test_delivery_is_recorded_against_the_channel_with_the_alert_id():
    d, _, repo = _dispatcher([_toast_channel(cid="toast-9")], [_policy(channels=("toast-9",))])
    alert = _alert()
    await d._send_notifications_async(alert, event="firing")
    assert len(repo.deliveries) == 1
    row = repo.deliveries[0]
    assert row["channel_id"] == "toast-9"
    assert row["channel_type"] == "toast"
    assert row["recipient"] == "webui"
    assert row["metadata"] == {"alert_id": str(alert.alert_id)}
    # A successful toast counts as a send: the incident claim is kept.
    assert alert.incident_id in d._incident_dispatched
