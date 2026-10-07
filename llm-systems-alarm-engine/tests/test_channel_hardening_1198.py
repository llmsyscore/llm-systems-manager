"""#1198: the shared alarm-engine token only goes to the manager's notify URL,
channel addresses are checked when saved and when sent, and channel secrets
are masked on read (an echoed mask keeps the stored value)."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.engine.notification_dispatcher as nd
from backend._time import now_utc
from backend.api.auth import require_management_token
from backend.api.routes import notifications
from backend.engine.notification_dispatcher import (
    NotificationDispatcher,
    _webpush_token,
    destination_error,
)
from backend.models.notification import (
    SECRET_MASK,
    ChannelSpecificConfig,
    ChannelType,
    NotificationChannel,
    NotificationChannelUpdate,
    WebhookConfig,
    WebPushConfig,
    keep_masked_secrets,
    mask_channel_secrets,
)

NOTIFY = "/api/companion/push/notify"


@pytest.fixture(autouse=True)
def _tokens(monkeypatch):
    monkeypatch.setattr(nd.settings.alarm_engine, "management_token", "mgmt", raising=False)
    monkeypatch.setattr(nd.settings.alarm_engine, "ingest_token", "ing", raising=False)
    monkeypatch.setattr(nd.settings.alarm_engine, "manager_url",
                        "https://manager.example:5443", raising=False)


# ── shared token only reaches the manager ───────────────────────────────────

class TestSharedTokenDestination:
    def test_blank_url_is_the_local_manager(self):
        assert _webpush_token(WebPushConfig()) == "mgmt"

    def test_the_configured_manager_notify_url_gets_the_shared_token(self):
        assert _webpush_token(WebPushConfig(url=f"https://manager.example:5443{NOTIFY}")) == "mgmt"

    @pytest.mark.parametrize("url", [
        f"https://other.example:5443{NOTIFY}",          # different host
        f"https://manager.example:5444{NOTIFY}",        # different port
        f"http://manager.example:5443{NOTIFY}",         # different scheme
        "https://manager.example:5443/api/alarm/rules",  # different path
        f"https://manager.example:5443/evil/..{NOTIFY}",
    ])
    def test_any_other_destination_gets_no_shared_token(self, url):
        assert _webpush_token(WebPushConfig(url=url)) == ""

    def test_a_channel_token_still_goes_wherever_the_channel_points(self):
        assert _webpush_token(WebPushConfig(url="https://other.example/hook", token="own")) == "own"

    def test_loopback_names_match_each_other(self, monkeypatch):
        monkeypatch.setattr(nd.settings.alarm_engine, "manager_url",
                            "http://localhost:5000", raising=False)
        assert _webpush_token(WebPushConfig(url=f"http://127.0.0.1:5000{NOTIFY}")) == "mgmt"
        assert _webpush_token(WebPushConfig(url=f"http://127.0.0.1:5001{NOTIFY}")) == ""


# ── destination check ───────────────────────────────────────────────────────

class TestDestinationError:
    @pytest.mark.parametrize("url", [
        "ftp://files.example/hook",
        "http:///nohost",
        "not a url",
        "",
        "http://user:pw@hooks.example/x",
        "http://169.254.169.254/latest/meta-data",
        "http://224.0.0.1/",
        "http://0.0.0.0:5000/",
    ])
    def test_rejected(self, url):
        assert destination_error(url) is not None

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:5000" + NOTIFY,
        "https://10.0.0.5/hook",
        "https://[::1]:5443/x",
    ])
    def test_accepted(self, url):
        assert destination_error(url) is None


# ── masking helpers ─────────────────────────────────────────────────────────

def _channel(cfg: ChannelSpecificConfig, ctype=ChannelType.WEBHOOK, cid=None):
    return NotificationChannel(
        channel_id=cid or uuid4(), name="hook", description=None, channel_type=ctype,
        config=cfg, enabled=True, rule_ids=[], created_at=now_utc(),
        last_sent_at=None, send_count=0, fail_count=0)


def _webhook_channel(cid=None):
    return _channel(ChannelSpecificConfig(webhook=WebhookConfig(
        url="https://hooks.example/x", secret="s3", headers={"X-Key": "k1"})), cid=cid)


class TestMasking:
    def test_mask_hides_token_secret_and_header_values(self):
        ch = _channel(ChannelSpecificConfig(
            webpush=WebPushConfig(token="t0"),
            webhook=WebhookConfig(url="https://h/x", secret="s", headers={"A": "1"})))
        m = mask_channel_secrets(ch)
        assert m.config.webpush.token == SECRET_MASK
        assert m.config.webhook.secret == SECRET_MASK
        assert m.config.webhook.headers == {"A": SECRET_MASK}
        assert ch.config.webpush.token == "t0"            # original untouched

    def test_unset_secrets_stay_unset(self):
        m = mask_channel_secrets(_channel(ChannelSpecificConfig(webpush=WebPushConfig())))
        assert m.config.webpush.token is None

    def test_echoed_masks_keep_the_stored_values(self):
        upd = NotificationChannelUpdate(config=ChannelSpecificConfig(webhook=WebhookConfig(
            url="https://hooks.example/y", secret=SECRET_MASK,
            headers={"X-Key": SECRET_MASK, "X-New": "n", "X-Gone": SECRET_MASK})))
        keep_masked_secrets(upd, _webhook_channel())
        assert upd.config.webhook.secret == "s3"
        assert upd.config.webhook.headers == {"X-Key": "k1", "X-New": "n"}
        assert upd.config.webhook.url == "https://hooks.example/y"

    def test_a_new_value_replaces_the_stored_one(self):
        upd = NotificationChannelUpdate(config=ChannelSpecificConfig(
            webpush=WebPushConfig(token="fresh")))
        keep_masked_secrets(upd, _channel(ChannelSpecificConfig(webpush=WebPushConfig(token="old"))))
        assert upd.config.webpush.token == "fresh"


# ── routes ──────────────────────────────────────────────────────────────────

class _Repo:
    def __init__(self, channels):
        self.channels = {str(c.channel_id): c for c in channels}
        self.updates: list = []

    async def list_channels(self):
        return list(self.channels.values())

    async def get_channel(self, cid):
        return self.channels.get(cid)

    def create(self, payload):
        ch = payload.to_channel()
        self.channels[str(ch.channel_id)] = ch
        return ch

    async def update_channel(self, cid, update):
        self.updates.append(update)
        return self.channels[cid]

    def record_delivery(self, **kw):
        pass


@pytest.fixture()
def repo():
    r = _Repo([_webhook_channel()])
    notifications.set_repository(r)
    yield r
    notifications.set_repository(None)


@pytest.fixture()
def client(repo):
    app = FastAPI()
    app.include_router(notifications.router)
    app.dependency_overrides[require_management_token] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _create_body(url):
    return {"name": "n", "channel_type": "webhook", "config": {"webhook": {"url": url}}}


class TestRoutes:
    def test_reads_are_masked(self, client, repo):
        cid = next(iter(repo.channels))
        for body in (client.get("/api/alarm/notifications/channels").json()[0],
                     client.get(f"/api/alarm/notifications/channels/{cid}").json()):
            assert body["config"]["webhook"]["secret"] == SECRET_MASK
            assert body["config"]["webhook"]["headers"] == {"X-Key": SECRET_MASK}

    @pytest.mark.parametrize("url", ["ftp://x/y", "http://169.254.169.254/x", "http://a:b@h/x"])
    def test_create_rejects_a_bad_address(self, client, repo, url):
        r = client.post("/api/alarm/notifications/channels", json=_create_body(url))
        assert r.status_code == 400 and "address" in r.json()["detail"]
        assert len(repo.channels) == 1

    def test_create_accepts_a_good_address_and_masks_the_reply(self, client):
        body = _create_body("https://10.0.0.9/hook")
        body["config"]["webhook"]["secret"] = "shh"
        r = client.post("/api/alarm/notifications/channels", json=body)
        assert r.status_code == 201
        assert r.json()["config"]["webhook"]["secret"] == SECRET_MASK

    def test_update_keeps_the_secret_behind_an_echoed_mask(self, client, repo):
        cid = next(iter(repo.channels))
        r = client.put(f"/api/alarm/notifications/channels/{cid}", json={"config": {"webhook": {
            "url": "https://hooks.example/z", "secret": SECRET_MASK, "headers": {"X-Key": SECRET_MASK}}}})
        assert r.status_code == 200
        sent = repo.updates[0].config.webhook
        assert sent.secret == "s3" and sent.headers == {"X-Key": "k1"}

    def test_update_rejects_a_bad_address(self, client, repo):
        cid = next(iter(repo.channels))
        r = client.put(f"/api/alarm/notifications/channels/{cid}",
                       json={"config": {"webhook": {"url": "http://224.0.0.1/x"}}})
        assert r.status_code == 400 and repo.updates == []

    def test_update_of_an_unknown_channel_is_404(self, client):
        r = client.put(f"/api/alarm/notifications/channels/{uuid4()}", json={"enabled": False})
        assert r.status_code == 404

    def test_test_route_hands_the_saved_webpush_config_to_the_sender(self, client, repo, monkeypatch):
        cfg = WebPushConfig(url="https://other.example/notify", token="own", verify_tls=False)
        ch = _channel(ChannelSpecificConfig(webpush=cfg), ctype=ChannelType.WEBPUSH)
        repo.channels[str(ch.channel_id)] = ch
        seen = {}

        async def fake_send(self, **kw):
            seen.update(kw)
            return {"success": True}

        monkeypatch.setattr(NotificationDispatcher, "send_notification", fake_send)
        r = client.post("/api/alarm/notifications/test", json={"channel_id": str(ch.channel_id)})
        assert r.status_code == 200
        assert seen["webpush_config"] == cfg
        assert seen["recipient"] == "https://other.example/notify"


# ── sender ──────────────────────────────────────────────────────────────────

class _Client:
    calls: list = []

    def __init__(self, *a, **kw):
        self.kw = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _Client.calls.append({"url": url, "headers": headers or {}, "verify": self.kw.get("verify")})

        class _R:
            status_code = 200
        return _R()


class _Rec:
    def __init__(self):
        self.calls = []

    def record_delivery(self, **kw):
        self.calls.append(kw)


def _alert():
    from types import SimpleNamespace
    from datetime import datetime, timezone
    return SimpleNamespace(alert_id=uuid4(), rule_id=uuid4(), rule_name="r", metric_source="s",
                           metric_name="m", current_value=1.0, threshold_value=0.5,
                           severity="critical", status="active", message="msg",
                           source_host="h", created_at=datetime.now(timezone.utc), incident_id=None)


class TestSender:
    @pytest.fixture(autouse=True)
    def _client(self, monkeypatch):
        _Client.calls = []
        monkeypatch.setattr(nd.httpx, "AsyncClient", _Client)

    async def test_webpush_to_a_foreign_url_carries_no_shared_token(self):
        rec = _Rec()
        d = NotificationDispatcher(notification_repository=rec)
        ch = _channel(ChannelSpecificConfig(webpush=WebPushConfig(url="https://other.example/n")),
                      ctype=ChannelType.WEBPUSH)
        await d._send_webpush_channels(_alert(), [ch])
        assert "Authorization" not in _Client.calls[0]["headers"]
        assert rec.calls[0]["success"] is True

    async def test_a_refused_webhook_address_is_recorded_not_sent(self):
        rec = _Rec()
        d = NotificationDispatcher(notification_repository=rec)
        ch = _channel(ChannelSpecificConfig(webhook=WebhookConfig(url="http://169.254.169.254/x")))
        await d._send_webhook_channels(_alert(), [ch])
        assert _Client.calls == []
        assert rec.calls[0]["success"] is False
        assert "refused" in rec.calls[0]["error_message"]

    async def test_a_refused_discord_address_is_recorded_not_sent(self):
        from backend.models.notification import DiscordConfig
        rec = _Rec()
        d = NotificationDispatcher(notification_repository=rec)
        ch = _channel(ChannelSpecificConfig(discord=DiscordConfig(webhook_url="ftp://d/x")),
                      ctype=ChannelType.DISCORD)
        await d._send_discord_channels(_alert(), [ch])
        assert _Client.calls == [] and rec.calls[0]["success"] is False

    async def test_direct_webpush_send_uses_the_channel_token_and_tls_setting(self):
        d = NotificationDispatcher()
        cfg = WebPushConfig(url="https://other.example/n", token="own", verify_tls=False)
        out = await d.send_notification("t", "b", channel_type=ChannelType.WEBPUSH,
                                        recipient=cfg.url, webpush_config=cfg)
        assert out["success"] is True
        assert _Client.calls[0]["headers"] == {"Authorization": "Bearer own"}
        assert _Client.calls[0]["verify"] is False

    async def test_direct_webpush_send_to_a_foreign_recipient_has_no_shared_token(self):
        d = NotificationDispatcher()
        await d.send_notification("t", "b", channel_type=ChannelType.WEBPUSH,
                                  recipient="https://other.example/n")
        assert "Authorization" not in _Client.calls[0]["headers"]

    async def test_direct_webpush_send_to_the_manager_keeps_the_shared_token(self):
        d = NotificationDispatcher()
        await d.send_notification("t", "b", channel_type=ChannelType.WEBPUSH)
        assert _Client.calls[0]["headers"] == {"Authorization": "Bearer mgmt"}

    async def test_direct_webhook_send_refuses_a_bad_address(self):
        d = NotificationDispatcher()
        out = await d.send_notification("t", "b", channel_type=ChannelType.WEBHOOK,
                                        recipient="http://224.0.0.1/x")
        assert out["success"] is False and "refused" in out["error"]
        assert _Client.calls == []
