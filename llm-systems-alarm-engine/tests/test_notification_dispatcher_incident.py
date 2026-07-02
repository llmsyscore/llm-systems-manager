"""#215: one channel notification per incident; toasts unaffected."""
from types import SimpleNamespace
from uuid import uuid4

from backend.engine.notification_dispatcher import NotificationDispatcher


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
