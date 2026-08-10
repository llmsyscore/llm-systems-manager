"""#538: the webpush notification channel — endpoint/token resolution, the
shared alert headline, the sender, and policy routing through the dispatcher.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import backend.engine.notification_dispatcher as nd
from backend.engine.notification_dispatcher import (
    NotificationDispatcher,
    _alert_headline,
    _webpush_token,
    _webpush_url,
)
from backend.models.notification import (
    ChannelSpecificConfig,
    ChannelType,
    NotificationChannel,
    WebPushConfig,
)
from backend._time import now_utc


# ── endpoint + token resolution ─────────────────────────────────────────────

class TestWebPushUrl:
    def test_blank_falls_back_to_the_local_manager(self, monkeypatch):
        monkeypatch.setattr(nd.settings.manager, "port", 5000, raising=False)
        assert _webpush_url(WebPushConfig()) == \
            "http://127.0.0.1:5000/api/companion/push/notify"

    def test_honours_the_configured_manager_port(self, monkeypatch):
        monkeypatch.setattr(nd.settings.manager, "port", 8500, raising=False)
        assert ":8500/" in _webpush_url(WebPushConfig())

    def test_an_explicit_url_wins(self):
        cfg = WebPushConfig(url="https://mgr.example/api/companion/push/notify")
        assert _webpush_url(cfg) == "https://mgr.example/api/companion/push/notify"

    def test_a_missing_config_still_resolves(self, monkeypatch):
        monkeypatch.setattr(nd.settings.manager, "port", 5000, raising=False)
        assert _webpush_url(None).endswith("/api/companion/push/notify")


class TestWebPushToken:
    @pytest.fixture(autouse=True)
    def _tokens(self, monkeypatch):
        monkeypatch.setattr(nd.settings.alarm_engine, "management_token",
                            "mgmt", raising=False)
        monkeypatch.setattr(nd.settings.alarm_engine, "ingest_token",
                            "ing", raising=False)

    def test_the_channel_token_wins(self):
        assert _webpush_token(WebPushConfig(token="own")) == "own"

    def test_falls_back_to_the_management_token(self):
        assert _webpush_token(WebPushConfig()) == "mgmt"

    def test_falls_back_to_the_ingest_token_last(self, monkeypatch):
        monkeypatch.setattr(nd.settings.alarm_engine, "management_token", "",
                            raising=False)
        assert _webpush_token(WebPushConfig()) == "ing"

    @pytest.mark.parametrize("placeholder", ["", "  ", "REPLACE_ME"])
    def test_placeholders_read_as_unset(self, placeholder, monkeypatch):
        monkeypatch.setattr(nd.settings.alarm_engine, "management_token",
                            placeholder, raising=False)
        monkeypatch.setattr(nd.settings.alarm_engine, "ingest_token",
                            placeholder, raising=False)
        assert _webpush_token(WebPushConfig(token=placeholder)) == ""

    def test_a_missing_config_still_resolves(self):
        assert _webpush_token(None) == "mgmt"


# ── shared alert headline ───────────────────────────────────────────────────

def _alert(**over):
    base = dict(alert_id=uuid4(), rule_id=uuid4(), rule_name="GPU hot",
                metric_source="system", metric_name="gpu_temperature_c",
                current_value=91.234, threshold_value=85.0,
                severity="critical", status="active", message="gpu is hot",
                source_host="llama-box", created_at=datetime.now(timezone.utc),
                incident_id=None)
    base.update(over)
    return SimpleNamespace(**base)


class TestAlertHeadline:
    def test_firing_leads_with_host_source_and_value(self):
        title, body, sev = _alert_headline(_alert(), "firing")
        assert title == "GPU hot"
        assert body == "llama-box · system · gpu_temperature_c = 91.23"
        assert sev == "critical"

    def test_resolved_is_prefixed_and_reads_as_info(self):
        title, body, sev = _alert_headline(_alert(), "resolved")
        assert title == "Cleared: GPU hot"
        assert body.endswith("(alarm cleared)")
        assert sev == "info"

    def test_acknowledged_is_prefixed_and_reads_as_info(self):
        title, body, sev = _alert_headline(_alert(), "acknowledged")
        assert title == "Acknowledged: GPU hot"
        assert "acknowledged" in body
        assert sev == "info"

    def test_falls_back_to_the_message_with_no_metric_context(self):
        a = _alert(source_host=None, metric_source=None, metric_name=None,
                   current_value=None, rule_name=None)
        title, body, _ = _alert_headline(a, "firing")
        assert title == "Alert"
        assert body == "gpu is hot"

    def test_a_non_numeric_value_is_stringified_not_raised(self):
        _, body, _ = _alert_headline(_alert(current_value="n/a"), "firing")
        assert "gpu_temperature_c = n/a" in body


# ── sender ──────────────────────────────────────────────────────────────────

class _Capture:
    """Stand-in httpx.AsyncClient recording the single POST it receives."""

    calls: list = []

    def __init__(self, status=200, **kw):
        self.kw = kw

    @classmethod
    def factory(cls, status):
        cls.calls = []

        class _Resp:
            status_code = status

        class _Client(cls):
            def __init__(self, *a, **k):
                super().__init__(**k)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                cls.calls.append({"url": url, "json": json,
                                  "headers": headers or {}, "verify": self.kw})
                return _Resp()
        return _Client


class _Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def record_delivery(self, **kw):
        self.calls.append(kw)


def _channel(cfg=None, cid=None):
    return NotificationChannel(
        channel_id=cid or uuid4(), name="phones", description=None,
        channel_type=ChannelType.WEBPUSH,
        config=ChannelSpecificConfig(webpush=cfg or WebPushConfig()),
        enabled=True, rule_ids=[], created_at=now_utc(),
        last_sent_at=None, send_count=0, fail_count=0)


async def _send(monkeypatch, status=200, cfg=None, event="firing"):
    monkeypatch.setattr(nd.settings.alarm_engine, "management_token", "mgmt",
                        raising=False)
    monkeypatch.setattr(nd.httpx, "AsyncClient", _Capture.factory(status))
    rec = _Recorder()
    d = NotificationDispatcher(notification_repository=rec)
    alert = _alert()
    await d._send_webpush_channels(alert, [_channel(cfg)], event=event)
    return d, rec, alert


class TestSendWebPush:
    async def test_posts_the_alert_with_a_bearer_and_a_deep_link(self, monkeypatch):
        _, rec, alert = await _send(monkeypatch)
        call = _Capture.calls[0]
        assert call["headers"]["Authorization"] == "Bearer mgmt"
        assert call["json"]["title"] == "GPU hot"
        assert call["json"]["severity"] == "critical"
        # Deep-links to the alert itself, not just the Alerts tab.
        assert call["json"]["url"] == f"/companion?tab=alerts&alert={alert.alert_id}"
        assert call["json"]["alert_id"] == str(alert.alert_id)
        assert rec.calls[0]["success"] is True
        assert rec.calls[0]["channel_type"] == "webpush"

    async def test_the_tag_is_per_alert_so_a_refire_replaces_it(self, monkeypatch):
        _, _, alert = await _send(monkeypatch)
        assert _Capture.calls[0]["json"]["tag"] == f"lsm-alert-{alert.alert_id}"

    async def test_2xx_marks_the_alert_as_sent(self, monkeypatch):
        d, _, alert = await _send(monkeypatch, status=204)
        assert str(alert.alert_id) in d._nontoast_send_ok

    async def test_non_2xx_is_a_failure(self, monkeypatch):
        d, rec, alert = await _send(monkeypatch, status=503)
        assert rec.calls[0]["success"] is False
        assert "503" in (rec.calls[0]["error_message"] or "")
        assert str(alert.alert_id) not in d._nontoast_send_ok

    async def test_a_transport_error_is_recorded_not_raised(self, monkeypatch):
        monkeypatch.setattr(nd.settings.alarm_engine, "management_token", "m",
                            raising=False)

        class _Boom:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): raise OSError("connection refused")

        monkeypatch.setattr(nd.httpx, "AsyncClient", _Boom)
        rec = _Recorder()
        d = NotificationDispatcher(notification_repository=rec)
        await d._send_webpush_channels(_alert(), [_channel()])
        assert rec.calls[0]["success"] is False
        assert "refused" in (rec.calls[0]["error_message"] or "")

    async def test_a_disabled_channel_config_sends_nothing(self, monkeypatch):
        await _send(monkeypatch, cfg=WebPushConfig(enabled=False))
        assert _Capture.calls == []

    async def test_no_token_configured_sends_no_auth_header(self, monkeypatch):
        monkeypatch.setattr(nd.settings.alarm_engine, "ingest_token", "",
                            raising=False)
        monkeypatch.setattr(nd.settings.alarm_engine, "management_token", "",
                            raising=False)
        monkeypatch.setattr(nd.httpx, "AsyncClient", _Capture.factory(200))
        d = NotificationDispatcher(notification_repository=_Recorder())
        await d._send_webpush_channels(_alert(), [_channel()])
        assert "Authorization" not in _Capture.calls[0]["headers"]

    async def test_a_resolved_event_sends_the_cleared_headline(self, monkeypatch):
        await _send(monkeypatch, event="resolved")
        assert _Capture.calls[0]["json"]["title"].startswith("Cleared:")
        assert _Capture.calls[0]["json"]["event"] == "resolved"


# ── policy routing ──────────────────────────────────────────────────────────

class _Repo:
    def __init__(self, channels, policies):
        self._channels, self._policies = list(channels), list(policies)
        self.deliveries: list[dict] = []

    async def list_channels(self):
        return list(self._channels)

    def list_configs(self):
        return list(self._policies)

    def record_delivery(self, **kw):
        self.deliveries.append(kw)


def _policy(cid, matches=True):
    return SimpleNamespace(config_id="pol-1", name="p", enabled=True,
                           min_alarm_count=1, repeat_interval_minutes=0,
                           channels=[cid], notify_on_clear=False,
                           auto_dismiss=True,
                           matches_alert=lambda alert: matches)


async def _dispatch(policies_match: bool):
    cid = uuid4()
    ch = _channel(cid=cid)
    d = NotificationDispatcher(
        notification_repository=_Repo([ch], [_policy(str(cid), policies_match)]))
    sent = []

    async def fake(alert, chans, event="firing"):
        sent.append([str(c.channel_id) for c in chans])

    d._send_webpush_channels = fake
    await d._send_notifications_async(_alert(incident_id=None), event="firing")
    return sent, str(cid)


class TestPolicyRouting:
    async def test_a_matching_policy_dispatches_the_channel(self):
        sent, cid = await _dispatch(True)
        assert sent == [[cid]]

    async def test_no_matching_policy_stays_silent(self):
        """"Channel enabled" is necessary but not sufficient — the same rule
        every other non-toast channel follows."""
        sent, _ = await _dispatch(False)
        assert sent == []

    def test_the_enable_flag_tracks_the_channel(self):
        d = NotificationDispatcher()
        assert d.get_channel_status()["webpush"] is False
        d._channels[str(uuid4())] = _channel()
        d._update_channel_flags()
        assert d.get_channel_status()["webpush"] is True


# ── toast regression guard ──────────────────────────────────────────────────

async def test_toast_body_survived_the_headline_extraction():
    """The toast and web push now share one builder; the toast payload must
    read exactly as it did before."""
    sent = []

    async def ws(text):
        sent.append(text)

    d = NotificationDispatcher(websocket_send=ws)
    await d._send_toast(_alert(), sticky=False, event="firing")
    import json
    data = json.loads(sent[0])["data"]
    assert data["title"] == "GPU hot"
    assert data["body"] == "llama-box · system · gpu_temperature_c = 91.23"
    assert data["severity"] == "critical"
    assert data["source_host"] == "llama-box"


# ── cross-service contract ──────────────────────────────────────────────────

class TestManagerContract:
    """The AE and the manager live in one repo but ship as separate services;
    these guard the payload/route contract between them against silent drift."""

    @staticmethod
    def _manager_src() -> str:
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        return (root / "llm-systems-manager" / "backend" / "companion.py").read_text()

    def test_the_default_route_exists_on_the_manager(self):
        assert '"/api/companion/push/notify"' in self._manager_src()

    @pytest.mark.parametrize("key", ["title", "body", "severity", "tag", "url"])
    def test_every_payload_key_is_read_by_the_manager(self, key):
        assert f'body.get("{key}")' in self._manager_src(), key

    async def test_the_sender_emits_exactly_the_documented_keys(self, monkeypatch):
        await _send(monkeypatch)
        assert set(_Capture.calls[0]["json"]) == {
            "title", "body", "severity", "tag", "url", "alert_id", "event"}
