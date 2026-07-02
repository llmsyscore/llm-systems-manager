"""#215: one channel notification per incident; toasts unaffected."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

from backend.engine.notification_dispatcher import NotificationDispatcher
from backend.models.notification import ChannelType


def _alert(incident=None, own_root=False):
    aid = uuid4()
    return SimpleNamespace(alert_id=aid,
                           incident_id=str(aid) if own_root else incident)


def _disp():
    return NotificationDispatcher()


def test_root_alert_never_suppressed():
    d = _disp()
    assert d._incident_channel_suppressed(_alert(own_root=True), "firing") is False


def test_joiner_suppressed_after_root_dispatch():
    d = _disp()
    root = _alert(own_root=True)
    d._record_incident_dispatch(root)
    joiner = _alert(incident=root.incident_id)
    assert d._incident_channel_suppressed(joiner, "firing") is True


def test_joiner_not_suppressed_before_any_dispatch():
    d = _disp()
    assert d._incident_channel_suppressed(_alert(incident="inc-x"), "firing") is False


def test_resolved_event_never_suppressed():
    d = _disp()
    d._record_incident_dispatch(_alert(own_root=True))
    assert d._incident_channel_suppressed(_alert(incident="inc-x"), "resolved") is False


def test_config_off_disables_suppression(monkeypatch):
    import backend.engine.notification_dispatcher as nd
    monkeypatch.setattr(
        nd, "settings",
        SimpleNamespace(alarm_engine=SimpleNamespace(
            correlation=SimpleNamespace(notify_per_incident=False))))
    d = _disp()
    root = _alert(own_root=True)
    d._record_incident_dispatch(root)
    assert d._incident_channel_suppressed(_alert(incident=root.incident_id), "firing") is False


def test_incident_size_counts_ongoing_members():
    repo = SimpleNamespace(get_active=lambda: [
        SimpleNamespace(incident_id="inc-1"), SimpleNamespace(incident_id="inc-1"),
        SimpleNamespace(incident_id="other")])
    d = NotificationDispatcher(alert_repository=repo)
    assert d._incident_size(SimpleNamespace(alert_id=uuid4(), incident_id="inc-1")) == 2


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


def _webhook_channel(cid="ch-1"):
    return SimpleNamespace(channel_id=cid, channel_type=ChannelType.WEBHOOK,
                           enabled=True, config=None)


def _policy(cid="ch-1"):
    return SimpleNamespace(config_id="pol-1", name="p", enabled=True,
                           min_alarm_count=1, repeat_interval_minutes=0,
                           channels=[cid], notify_on_clear=False,
                           matches_alert=lambda alert: True)


def _wired_dispatcher():
    repo = FakeNotifRepo([_webhook_channel()], [_policy()])
    return NotificationDispatcher(notification_repository=repo)


async def test_same_cycle_root_and_joiner_send_one_channel_notification():
    d = _wired_dispatcher()
    sends = []

    async def fake_webhook(alert, chans):
        await asyncio.sleep(0)
        sends.append(str(alert.alert_id))
        d._record_delivery(alert, chans[0], "webhook", "u", "t", "b", success=True)

    d._send_webhook_channels = fake_webhook
    root = _alert(own_root=True)
    joiner = _alert(incident=root.incident_id)
    await asyncio.gather(
        d._send_notifications_async(root, event="firing"),
        d._send_notifications_async(joiner, event="firing"))
    assert sends == [str(root.alert_id)]


async def test_failed_root_send_releases_claim():
    d = _wired_dispatcher()

    async def failing_webhook(alert, chans):
        await asyncio.sleep(0)
        d._record_delivery(alert, chans[0], "webhook", "u", "t", "b",
                           success=False, error_message="boom")

    d._send_webhook_channels = failing_webhook
    root = _alert(own_root=True)
    await d._send_notifications_async(root, event="firing")
    assert root.incident_id not in d._incident_dispatched
    joiner = _alert(incident=root.incident_id)
    assert d._incident_channel_suppressed(joiner, "firing") is False


async def test_stale_incident_entries_swept_on_dispatch():
    alert_repo = SimpleNamespace(get_active=lambda: [])
    d = NotificationDispatcher(alert_repository=alert_repo)
    d._incident_dispatched["stale-inc"] = 0.0
    await d._send_notifications_async(_alert(own_root=True), event="firing")
    assert "stale-inc" not in d._incident_dispatched
