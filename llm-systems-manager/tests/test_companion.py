"""
PWA companion (#522 phase 1): VAPID key management, the push-subscription
store, and the companion/manifest/service-worker/push routes.

Route tests monkeypatch companion._store / companion._data_dir so the live
data/ directory is never touched.
"""
from __future__ import annotations

import base64
import json

import pytest

import companion


# ── VAPID keys ───────────────────────────────────────────────────────────────

class TestVapidKeys:
    def test_creates_private_key_file_0600(self, tmp_path):
        pem = companion.ensure_vapid_key(tmp_path)
        assert pem.exists()
        assert (pem.stat().st_mode & 0o777) == 0o600

    def test_key_is_stable_across_calls(self, tmp_path):
        p1 = companion.ensure_vapid_key(tmp_path)
        first = p1.read_bytes()
        p2 = companion.ensure_vapid_key(tmp_path)
        assert p1 == p2
        assert p2.read_bytes() == first

    def test_public_key_is_b64url_uncompressed_point(self, tmp_path):
        key = companion.vapid_public_key_b64(tmp_path)
        # 65-byte uncompressed P-256 point → 87 chars unpadded base64url,
        # leading byte 0x04 encodes to "B".
        assert len(key) == 87
        assert key.startswith("B")
        assert "=" not in key and "+" not in key and "/" not in key
        raw = base64.urlsafe_b64decode(key + "=")
        assert len(raw) == 65 and raw[0] == 0x04

    def test_public_key_stable_across_calls(self, tmp_path):
        assert (companion.vapid_public_key_b64(tmp_path)
                == companion.vapid_public_key_b64(tmp_path))


# ── subscription validation ─────────────────────────────────────────────────

def _sub(endpoint="https://push.example.net/send/abc", p256dh="BEx", auth_="a1"):
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth_}}


class TestValidSubscription:
    def test_accepts_wellformed(self):
        assert companion.valid_subscription(_sub()) is True

    @pytest.mark.parametrize("bad", [
        None, "", 42, [], {},
        {"endpoint": "https://x.example/e"},                      # no keys
        {"endpoint": "http://x.example/e",
         "keys": {"p256dh": "k", "auth": "a"}},                   # not https
        {"endpoint": "https://x.example/e", "keys": {"auth": "a"}},
        {"endpoint": "https://x.example/e", "keys": {"p256dh": "k"}},
        {"keys": {"p256dh": "k", "auth": "a"}},                   # no endpoint
    ])
    def test_rejects_malformed(self, bad):
        assert companion.valid_subscription(bad) is False


# ── SubscriptionStore ────────────────────────────────────────────────────────

class TestSubscriptionStore:
    def test_add_persists_and_survives_reload(self, tmp_path):
        path = tmp_path / "push_subscriptions.json"
        store = companion.SubscriptionStore(path)
        assert store.add(_sub(), ua="TestUA") is True
        again = companion.SubscriptionStore(path)
        subs = again.list()
        assert len(subs) == 1
        assert subs[0]["endpoint"] == _sub()["endpoint"]

    def test_store_file_mode_0600(self, tmp_path):
        path = tmp_path / "push_subscriptions.json"
        companion.SubscriptionStore(path).add(_sub())
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_duplicate_endpoint_updates_not_duplicates(self, tmp_path):
        store = companion.SubscriptionStore(tmp_path / "s.json")
        store.add(_sub(p256dh="OLD"))
        store.add(_sub(p256dh="NEW"))
        subs = store.list()
        assert store.count() == len(subs) == 1
        assert subs[0]["keys"]["p256dh"] == "NEW"

    def test_remove_by_endpoint(self, tmp_path):
        store = companion.SubscriptionStore(tmp_path / "s.json")
        store.add(_sub())
        assert store.remove(_sub()["endpoint"]) is True
        assert store.count() == 0
        assert store.remove(_sub()["endpoint"]) is False

    def test_cap_at_max_subscriptions(self, tmp_path):
        store = companion.SubscriptionStore(tmp_path / "s.json")
        for i in range(companion.MAX_SUBSCRIPTIONS):
            assert store.add(_sub(endpoint=f"https://p.example/e{i}")) is True
        assert store.add(_sub(endpoint="https://p.example/overflow")) is False
        assert store.count() == companion.MAX_SUBSCRIPTIONS

    def test_corrupt_file_treated_as_empty(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{not json")
        store = companion.SubscriptionStore(path)
        assert store.list() == []
        assert store.add(_sub()) is True


# ── routes ───────────────────────────────────────────────────────────────────

import auth  # noqa: E402
import manager_mod as M  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect the companion module's store + VAPID dir to tmp_path so route
    tests never write into the live data/ directory."""
    monkeypatch.setattr(companion, "_data_dir", tmp_path)
    monkeypatch.setattr(
        companion, "_store", companion.SubscriptionStore(tmp_path / "s.json"))
    return tmp_path


@pytest.fixture
def anon(monkeypatch, sandbox):
    monkeypatch.setattr(auth, "auth_mode", lambda: "required")
    with M.app.test_client() as c:
        yield c


@pytest.fixture
def client(monkeypatch, sandbox):
    monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
    with M.app.test_client() as c:
        yield c


class TestOpenSurface:
    def test_manifest_serves_unauthenticated(self, anon):
        r = anon.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert r.mimetype == "application/manifest+json"

    def test_manifest_members(self, anon):
        m = json.loads(anon.get("/manifest.webmanifest").data)
        assert m["start_url"] == "/companion"
        assert m["display"] == "standalone"
        assert m["scope"] == "/"
        sizes = {i["sizes"] for i in m["icons"]}
        assert {"192x192", "512x512"} <= sizes

    def test_sw_serves_unauthenticated_with_version_stamp(self, anon):
        r = anon.get("/sw.js")
        assert r.status_code == 200
        assert r.mimetype in ("text/javascript", "application/javascript")
        assert "no-cache" in (r.headers.get("Cache-Control") or "")
        body = r.data.decode()
        assert M.__version__ in body
        assert "__MGR_VERSION__" not in body

    @pytest.mark.parametrize("path", [
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/apple-touch-icon.png",
    ])
    def test_icon_paths_not_auth_gated(self, anon, path):
        r = anon.get(path)
        assert r.status_code != 401
        assert not (r.status_code == 302 and "/login" in r.headers.get("Location", ""))


class TestGatedSurface:
    def test_companion_page_redirects_anon_to_login(self, anon):
        r = anon.get("/companion")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/companion/push/public-key"),
        ("GET", "/api/companion/push/subscriptions"),
        ("POST", "/api/companion/push/subscribe"),
        ("POST", "/api/companion/push/unsubscribe"),
        ("POST", "/api/companion/push/test"),
    ])
    def test_push_api_requires_auth(self, anon, method, path):
        r = anon.open(path, method=method, json={})
        assert r.status_code == 401

    def test_companion_page_serves_authenticated(self, client):
        r = client.get("/companion")
        assert r.status_code == 200
        body = r.data.decode()
        assert 'rel="manifest"' in body


class TestPushApi:
    def test_public_key_round_trip(self, client, sandbox):
        r = client.get("/api/companion/push/public-key")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["key"] == companion.vapid_public_key_b64(sandbox)

    def test_subscribe_list_unsubscribe(self, client):
        r = client.post("/api/companion/push/subscribe", json=_sub())
        assert r.status_code == 200 and r.get_json()["ok"] is True
        r = client.get("/api/companion/push/subscriptions")
        assert r.get_json()["count"] == 1
        r = client.post("/api/companion/push/unsubscribe",
                        json={"endpoint": _sub()["endpoint"]})
        assert r.get_json() == {"ok": True, "removed": True}
        assert client.get("/api/companion/push/subscriptions").get_json()["count"] == 0

    def test_subscribe_rejects_malformed(self, client):
        r = client.post("/api/companion/push/subscribe",
                        json={"endpoint": "http://insecure.example/e"})
        assert r.status_code == 400

    def test_test_push_503_when_pywebpush_missing(self, client, monkeypatch):
        def _raise():
            raise ImportError("no pywebpush")
        monkeypatch.setattr(companion, "_webpush_funcs", _raise)
        client.post("/api/companion/push/subscribe", json=_sub())
        r = client.post("/api/companion/push/test")
        assert r.status_code == 503

    def test_test_push_no_subscriptions_400(self, client):
        r = client.post("/api/companion/push/test")
        assert r.status_code == 400

    def test_test_push_sends_to_every_subscription(self, client, monkeypatch):
        sent = []

        class FakeWebPushException(Exception):
            def __init__(self, msg, response=None):
                super().__init__(msg)
                self.response = response

        def fake_webpush(subscription_info, **kw):
            sent.append(subscription_info["endpoint"])

        monkeypatch.setattr(companion, "_webpush_funcs",
                            lambda: (fake_webpush, FakeWebPushException))
        client.post("/api/companion/push/subscribe",
                    json=_sub(endpoint="https://p.example/e1"))
        client.post("/api/companion/push/subscribe",
                    json=_sub(endpoint="https://p.example/e2"))
        r = client.post("/api/companion/push/test")
        body = r.get_json()
        assert r.status_code == 200
        assert body["sent"] == 2 and body["failed"] == 0
        assert sorted(sent) == ["https://p.example/e1", "https://p.example/e2"]

    def test_test_push_prunes_gone_subscription(self, client, monkeypatch):
        class FakeWebPushException(Exception):
            def __init__(self, msg, response=None):
                super().__init__(msg)
                self.response = response

        class _Gone:
            status_code = 410

        def fake_webpush(subscription_info, **kw):
            if subscription_info["endpoint"].endswith("dead"):
                raise FakeWebPushException("gone", response=_Gone())

        monkeypatch.setattr(companion, "_webpush_funcs",
                            lambda: (fake_webpush, FakeWebPushException))
        client.post("/api/companion/push/subscribe",
                    json=_sub(endpoint="https://p.example/live"))
        client.post("/api/companion/push/subscribe",
                    json=_sub(endpoint="https://p.example/dead"))
        body = client.post("/api/companion/push/test").get_json()
        assert body["sent"] == 1 and body["pruned"] == 1
        assert client.get(
            "/api/companion/push/subscriptions").get_json()["count"] == 1
