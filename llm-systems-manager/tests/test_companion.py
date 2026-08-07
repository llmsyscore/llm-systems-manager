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
