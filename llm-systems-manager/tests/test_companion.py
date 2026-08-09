"""
PWA companion (#522 phase 1): VAPID key management, the push-subscription
store, and the companion/manifest/service-worker/push routes.

Route tests monkeypatch companion._store / companion._data_dir so the live
data/ directory is never touched.
"""
from __future__ import annotations

import base64
import json
import re
from types import SimpleNamespace

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


class FakeWebPushException(Exception):
    def __init__(self, msg, response=None):
        super().__init__(msg)
        self.response = response


class TestValidPushEndpoint:
    """The manager POSTs to whatever endpoint is stored, so the host is
    restricted to public dotted names on 443 (no SSRF into the LAN)."""

    @pytest.mark.parametrize("url", [
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://fcm.googleapis.com/fcm/send/abc:def",
        "https://web.push.apple.com/QAbc123",
        "https://sea.notify.windows.com/w/?token=abc",
        "https://push.example.net:443/send/abc",
    ])
    def test_accepts_public_push_services(self, url):
        assert companion.valid_push_endpoint(url) is True

    @pytest.mark.parametrize("url", [
        # Shorthand / octal / overflowed IPv4 — every one of these resolves to
        # loopback or the LAN despite "looking" like a hostname.
        "https://127.1/x",
        "https://0177.0.0.1/x",
        "https://192.168.011.5/w",
        "https://192.168.257/x",
        "https://2130706433/x",
        "https://169.254.169%2e254/x",               # urllib3 decodes %2e later
        "https://push.example.net@127.0.0.1/x",      # userinfo
        "https://dev.local/x", "https://box.lan/x", "https://svc.internal/x",
        "http://push.example.net/send/abc",          # not https
        "https://127.0.0.1/send",                    # loopback literal
        "https://192.168.1.59:8086/write",           # private literal + port
        "https://10.0.0.5/x",
        "https://169.254.169.254/latest/meta-data/",  # link-local metadata
        "https://[::1]/x",
        "https://localhost/x",                       # dotless
        "https://influxdb:8086/write",               # internal name + port
        "https://manager/x",
        "https://dev.localhost/x",
        "https://push.example.net:8081/x",           # non-443 port
        "https://push.example.net:notaport/x",
        "", "   ", None, 42,
    ])
    def test_rejects_non_public_or_malformed(self, url):
        assert companion.valid_push_endpoint(url) is False


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
        added = store.add(_sub(), ua="TestUA")
        assert added is True
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
        removed = store.remove(_sub()["endpoint"])
        assert removed is True
        assert store.count() == 0
        removed_again = store.remove(_sub()["endpoint"])
        assert removed_again is False

    def test_cap_at_max_subscriptions(self, tmp_path):
        store = companion.SubscriptionStore(tmp_path / "s.json")
        for i in range(companion.MAX_SUBSCRIPTIONS):
            added = store.add(_sub(endpoint=f"https://p.example/e{i}"))
            assert added is True
        overflow = store.add(_sub(endpoint="https://p.example/overflow"))
        assert overflow is False
        assert store.count() == companion.MAX_SUBSCRIPTIONS

    def test_corrupt_file_treated_as_empty(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{not json")
        store = companion.SubscriptionStore(path)
        assert store.list() == []
        added = store.add(_sub())
        assert added is True


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
        "/static/icons/icon-maskable-512.png",
    ])
    def test_icon_paths_serve_anonymously(self, anon, path):
        # 200, not merely "not 401" — a missing file passes the weaker check
        # and installability breaks silently.
        r = anon.get(path)
        assert r.status_code == 200, path
        assert r.mimetype == "image/png"
        assert len(r.data) > 500

    def test_every_manifest_icon_exists(self, anon):
        manifest = json.loads(anon.get("/manifest.webmanifest").data)
        for icon in manifest["icons"]:
            code = anon.get(icon["src"]).status_code
            assert code == 200, icon["src"]

    def test_every_service_worker_shell_path_serves(self, client):
        """Every path sw.js precaches must exist, or install caches nothing."""
        body = client.get("/sw.js").data.decode()
        shell = re.findall(r"^\s*'(/[^']+)',", body, re.M)
        assert len(shell) >= 5, f"SHELL list not parsed: {shell}"
        for path in shell:
            code = client.get(path).status_code
            assert code == 200, path


class TestStaticIconGate:
    """The /static/icons/ exemption must match the NORMALIZED path — Werkzeug
    collapses ".." after the gate runs, so a raw-prefix check would serve the
    whole login-gated static tree to anonymous clients."""

    @pytest.fixture(autouse=True)
    def _required_mode(self, monkeypatch):
        monkeypatch.setattr(auth, "auth_mode", lambda: "required")

    @pytest.mark.parametrize("path", [
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/apple-touch-icon.png",
        "/static/icons/icon-maskable-512.png",
    ])
    def test_real_icons_are_open(self, path):
        with M.app.test_request_context(path):
            assert auth._auth_gate() is None

    @pytest.mark.parametrize("path", [
        "/static/icons/../index.html",
        "/static/icons/../js/companion.js",
        "/static/icons/../css/base.css",
        "/static/icons/../../backend/companion.py",
        "/static/icons/subdir/../../js/companion.js",
    ])
    def test_traversal_out_of_icons_is_gated(self, path):
        with M.app.test_request_context(path):
            assert auth._auth_gate() is not None

    @pytest.mark.parametrize("path", [
        # normpath() lands inside /static/icons/, but Werkzeug dispatches the
        # RAW path — these reach real <path:> handlers with no session.
        "/api/llm/profiles/../../../static/icons/p/activate",
        "/api/llm/profiles/../../../static/icons/p/save",
        "/api/alarm/../../static/icons/x",
        "/proxy/openclaw/../../static/icons/x",
    ])
    def test_traversal_into_icons_from_another_route_is_gated(self, path):
        with M.app.test_request_context(path, method="POST"):
            assert auth._auth_gate() is not None

    def test_unnormalized_paths_are_never_open(self):
        # A path that isn't already normalized can never take the exemption,
        # in either direction — that asymmetry is what produced two bypasses.
        for path in ("/static/icons/x/../icon-192.png",
                     "/static/icons/./icon-192.png",
                     "/static/icons//icon-192.png"):
            with M.app.test_request_context(path):
                assert auth._auth_gate() is not None, path

    def test_sibling_static_dirs_still_gated(self):
        for path in ("/static/js/companion.js", "/static/css/base.css",
                     "/static/iconsfoo/x.png"):
            with M.app.test_request_context(path):
                assert auth._auth_gate() is not None, path


class TestLoginNextRedirect:
    """?next= exists so an expired session inside the installed PWA returns to
    /companion (standalone has no URL bar). Only allowlisted same-origin paths
    are honored — the returned value is a constant, never request data."""

    @pytest.fixture
    def _login_ok(self, monkeypatch):
        # Deterministic auth: don't depend on the seeded credential, which is
        # environment-specific (CI's fresh store isn't llmadmin/llmadmin).
        monkeypatch.setattr(auth, "auth_mode", lambda: "required")
        import manager_users
        monkeypatch.setattr(
            manager_users, "authenticate",
            lambda u, p, ip: {"ok": True, "username": "op", "role": "admin"})

    def test_accepts_allowlisted_path(self):
        assert auth.safe_next("/companion") == "/companion"

    @pytest.mark.parametrize("raw", [
        "//evil.example/x",           # protocol-relative
        "https://evil.example/x",
        "http://evil.example",
        "/\\evil.example",            # backslash
        "\\\\evil.example",
        "javascript:alert(1)",
        "evil.example", "", None,
        # valid same-origin paths, but not on the allowlist → fall back to /
        "/", "/admin/agents", "/companion?x=1", "/companion/",
    ])
    def test_rejects_everything_else(self, raw):
        assert auth.safe_next(raw) is None

    def test_login_returns_to_next_on_get_then_post(self, _login_ok):
        with M.app.test_client() as c:
            c.get("/login?next=%2Fcompanion")   # stashed in session
            r = c.post("/login", data={"username": "op", "password": "x"})
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/companion")

    def test_login_returns_to_next_on_post_query(self, _login_ok):
        with M.app.test_client() as c:
            r = c.post("/login?next=%2Fcompanion",
                       data={"username": "op", "password": "x"})
        assert r.headers["Location"].endswith("/companion")

    def test_login_ignores_offsite_next(self, _login_ok):
        with M.app.test_client() as c:
            c.get("/login?next=https%3A%2F%2Fevil.example%2Fx")
            r = c.post("/login", data={"username": "op", "password": "x"})
        loc = r.headers.get("Location", "")
        assert "evil.example" not in loc
        assert loc.endswith("/")


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
        after = client.get("/api/companion/push/subscriptions").get_json()
        assert after["count"] == 0

    def test_subscribe_rejects_malformed(self, client):
        r = client.post("/api/companion/push/subscribe",
                        json={"endpoint": "http://insecure.example/e"})
        assert r.status_code == 400

    def test_subscribe_rejects_host_resolving_to_private_ip(self, client,
                                                            monkeypatch):
        """A public *name* pointing at a private address (nip.io-style) is
        refused — string checks alone can't see that."""
        monkeypatch.setattr(
            companion, "resolves_to_public_ip", lambda host: False)
        r = client.post("/api/companion/push/subscribe",
                        json=_sub(endpoint="https://10.0.0.5.nip.io/x"))
        assert r.status_code == 400
        assert "private" in r.get_json()["error"]

    def test_unsubscribe_survives_non_object_body(self, client):
        for body in ([1, 2, 3], "x", 7):
            r = client.post("/api/companion/push/unsubscribe", json=body)
            assert r.status_code == 200, body

    def test_push_send_refuses_redirects(self):
        """pywebpush passes no allow_redirects, so a 302 from a push endpoint
        would re-target scheme/host/port — the session must refuse them."""
        sess = companion._push_session()
        captured = {}

        class _Resp:
            status_code = 200

        def fake_super_request(self, *args, **kwargs):
            captured.update(kwargs)
            return _Resp()

        import requests
        orig = requests.Session.request
        requests.Session.request = fake_super_request
        try:
            sess.request("POST", "https://push.example.net/x")
        finally:
            requests.Session.request = orig
        assert captured.get("allow_redirects") is False


class TestResolvesToPublicIp:
    def _fake_getaddrinfo(self, addr):
        return lambda *a, **k: [(2, 1, 6, "", (addr, 443))]

    @pytest.mark.parametrize("addr", [
        "127.0.0.1", "10.0.0.5", "192.168.1.59", "169.254.169.254",
        "172.16.0.1", "::1",
    ])
    def test_private_addresses_rejected(self, addr, monkeypatch):
        monkeypatch.setattr(companion.socket, "getaddrinfo",
                            self._fake_getaddrinfo(addr))
        assert companion.resolves_to_public_ip("anything.example") is False

    def test_public_address_accepted(self, monkeypatch):
        monkeypatch.setattr(companion.socket, "getaddrinfo",
                            self._fake_getaddrinfo("34.107.221.82"))
        assert companion.resolves_to_public_ip("push.example.net") is True

    def test_unresolvable_name_passes(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("nxdomain")
        monkeypatch.setattr(companion.socket, "getaddrinfo", boom)
        assert companion.resolves_to_public_ip("nope.example") is True

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

    def test_test_push_targets_only_the_named_endpoint(self, client, monkeypatch):
        """The button tests THIS device — a stale sibling subscription must
        not turn an arrived push into a reported failure."""
        sent = []

        monkeypatch.setattr(companion, "_webpush_funcs", lambda: (
            lambda subscription_info, **kw: sent.append(
                subscription_info["endpoint"]), FakeWebPushException))
        client.post("/api/companion/push/subscribe",
                    json=_sub(endpoint="https://p.example/mine"))
        client.post("/api/companion/push/subscribe",
                    json=_sub(endpoint="https://p.example/stale"))
        body = client.post("/api/companion/push/test",
                           json={"endpoint": "https://p.example/mine"}).get_json()
        assert body["sent"] == 1 and body["failed"] == 0
        assert sent == ["https://p.example/mine"]

    def test_test_push_unknown_endpoint_404(self, client):
        client.post("/api/companion/push/subscribe", json=_sub())
        r = client.post("/api/companion/push/test",
                        json={"endpoint": "https://p.example/never-seen"})
        assert r.status_code == 404

    def test_test_push_still_fans_out_without_a_target(self, client, monkeypatch):
        sent = []
        monkeypatch.setattr(companion, "_webpush_funcs", lambda: (
            lambda subscription_info, **kw: sent.append(
                subscription_info["endpoint"]), FakeWebPushException))
        client.post("/api/companion/push/subscribe",
                    json=_sub(endpoint="https://p.example/a"))
        client.post("/api/companion/push/subscribe",
                    json=_sub(endpoint="https://p.example/b"))
        client.post("/api/companion/push/test", json={})
        assert sorted(sent) == ["https://p.example/a", "https://p.example/b"]

    def test_test_push_sends_to_every_subscription(self, client, monkeypatch):
        sent = []

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
        remaining = client.get(
            "/api/companion/push/subscriptions").get_json()["count"]
        assert remaining == 1


# ── #541: role gating on the push roster + the release check ──────────


@pytest.fixture
def operator(monkeypatch, sandbox):
    """A logged-in non-admin session. Deliberately carries no `user` key:
    _live_role_for_session falls back to the cookie role when the session has
    no named subject, so this doesn't depend on a user existing in the
    environment's store (CI's is empty; the live box has llmoperator) and the
    real effective_role / _require_admin still run."""
    monkeypatch.setattr(auth, "auth_mode", lambda: "required")
    with M.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "operator"
        yield c


class TestPushRoleGating:
    def test_operator_gets_the_count_but_not_the_device_roster(
            self, operator, sandbox):
        companion._store.add(_sub(), ua="ua")
        companion._store.add(_sub(endpoint="https://push.example.net/send/xyz"), ua="ua")
        r = operator.get("/api/companion/push/subscriptions")
        assert r.status_code == 200
        body = r.get_json()
        assert body["count"] == 2
        # Endpoint prefixes identify other operators' devices.
        assert "endpoints" not in body

    def test_admin_still_sees_the_roster(self, client, sandbox):
        companion._store.add(_sub(), ua="ua")
        body = client.get("/api/companion/push/subscriptions").get_json()
        assert body["count"] == 1
        assert len(body["endpoints"]) == 1

    def test_operator_cannot_fan_a_test_out_to_every_device(self, operator, sandbox):
        companion._store.add(_sub(), ua="ua")
        r = operator.post("/api/companion/push/test", json={})
        assert r.status_code == 403
        assert "endpoint" in (r.get_json() or {}).get("error", "")

    def test_operator_may_test_its_own_device(self, operator, sandbox, monkeypatch):
        companion._store.add(_sub(), ua="ua")
        monkeypatch.setattr(companion, "_webpush_funcs", lambda: None)
        monkeypatch.setattr(companion, "ensure_vapid_key", lambda d: d / "k.pem")
        monkeypatch.setattr(companion, "_send_one", lambda *a, **k: (True, False))
        r = operator.post("/api/companion/push/test",
                          json={"endpoint": "https://push.example.net/send/abc"})
        assert r.status_code == 200
        assert r.get_json()["sent"] == 1


class TestReleaseCheck:
    @staticmethod
    def _inst(tag, source="git", ahead=0):
        return {"tag": tag, "describe": tag, "ahead": ahead, "source": source}

    def test_disabled_by_default_makes_no_network_call(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(companion, "_latest_release",
                            lambda repo: called.append(repo) or (None, None, None))
        monkeypatch.setitem(companion._release, "enabled", None)
        # Pin the config value: the deployed TOML may have the check switched
        # on, and this asserts the OFF behaviour, not the local setting.
        from config.unified_config import settings as _s
        monkeypatch.setattr(_s.manager.companion, "release_check", False,
                            raising=False)
        body = client.get("/api/companion/release").get_json()
        assert body["enabled"] is False
        assert "latest" not in body
        assert called == []

    def test_enabled_reports_a_newer_tag(self, client, monkeypatch):
        monkeypatch.setattr(companion, "_latest_release", lambda repo: ("v9.9.9", "", None))
        monkeypatch.setattr(companion, "_installed_release", lambda: self._inst("v1.1.1"))
        monkeypatch.setitem(companion._release, "enabled", True)
        body = client.get("/api/companion/release").get_json()
        assert body["latest"] == "v9.9.9"
        assert body["update_available"] is True

    def test_a_release_candidate_is_never_offered_as_an_update(self, client, monkeypatch):
        # Keying the whole tag ranked v1.2.0-rc1 above v1.2.0.
        monkeypatch.setattr(companion, "_latest_release", lambda repo: ("v1.2.0-rc1", "", None))
        monkeypatch.setattr(companion, "_installed_release", lambda: self._inst("v1.2.0"))
        monkeypatch.setitem(companion._release, "enabled", True)
        assert client.get("/api/companion/release").get_json()["update_available"] is False

    def test_drift_past_a_tag_is_reported_but_is_not_an_update(self, client, monkeypatch):
        monkeypatch.setattr(companion, "_latest_release", lambda repo: ("v1.1.1", "", None))
        monkeypatch.setattr(companion, "_installed_release",
                            lambda: {"tag": "v1.1.1", "describe": "v1.1.1-8-gabc1234",
                                     "ahead": 8, "source": "git"})
        monkeypatch.setitem(companion._release, "enabled", True)
        body = client.get("/api/companion/release").get_json()
        assert body["ahead"] == 8
        assert body["describe"] == "v1.1.1-8-gabc1234"
        assert body["update_available"] is False

    def test_release_notes_fallback_when_the_install_cannot_name_itself(
            self, client, monkeypatch):
        # brew/deb/container with no RELEASE file: match this build stamp in
        # the newest release's notes instead of guessing. The body below is
        # the exact table release.yml publishes — see TestReleaseNotesContract.
        notes = (
            "Install this release:\n\n### Component versions\n\n"
            "| Component | Version |\n| --- | --- |\n"
            f"| Manager | `{M.__version__}` |\n"
            "| Alarm engine | `v2026.01.01-1` |\n"
            "| Agent | `v2026.01.01-1` |\n")
        monkeypatch.setattr(companion, "_installed_release", lambda: self._inst(None, None))
        monkeypatch.setattr(companion, "_latest_release", lambda repo: ("v9.9.9", notes, None))
        monkeypatch.setitem(companion._release, "enabled", True)
        body = client.get("/api/companion/release").get_json()
        assert body["update_available"] is False
        assert "release notes" in body["note"]

    def test_no_verdict_when_the_notes_do_not_mention_this_build(self, client, monkeypatch):
        monkeypatch.setattr(companion, "_installed_release", lambda: self._inst(None, None))
        monkeypatch.setattr(companion, "_latest_release",
                            lambda repo: ("v9.9.9", "some other notes", None))
        monkeypatch.setitem(companion._release, "enabled", True)
        body = client.get("/api/companion/release").get_json()
        assert body["update_available"] is None
        assert "no release tag" in body["note"]

    def test_same_release_is_not_an_update(self, client, monkeypatch):
        monkeypatch.setattr(companion, "_latest_release", lambda repo: ("v1.1.1", "", None))
        monkeypatch.setattr(companion, "_installed_release", lambda: self._inst("v1.1.1"))
        monkeypatch.setitem(companion._release, "enabled", True)
        assert client.get("/api/companion/release").get_json()["update_available"] is False

    def test_github_failure_degrades_without_raising(self, client, monkeypatch):
        monkeypatch.setattr(companion, "_latest_release",
                            lambda repo: (None, None, "ConnectionError"))
        monkeypatch.setattr(companion, "_installed_release", lambda: self._inst("v1.1.1"))
        monkeypatch.setitem(companion._release, "enabled", True)
        body = client.get("/api/companion/release").get_json()
        assert body["ok"] is True
        assert body["error"] == "ConnectionError"
        assert body["update_available"] is None

    def test_operator_cannot_toggle_the_check(self, operator):
        assert operator.put("/api/companion/release",
                            json={"enabled": True}).status_code == 403

    def test_admin_toggles_and_it_sticks(self, client, monkeypatch):
        monkeypatch.setitem(companion._release, "enabled", None)
        assert client.put("/api/companion/release",
                          json={"enabled": True}).get_json()["enabled"] is True
        monkeypatch.setattr(companion, "_latest_release", lambda repo: (None, None, None))
        assert client.get("/api/companion/release").get_json()["enabled"] is True

    def test_toggling_clears_the_cache_so_the_next_read_refetches(self, client):
        # 24 h TTL would otherwise pin a stale answer; flipping the switch is
        # the operator's way of saying "check now".
        companion._release["checked_at"] = 12345.0
        client.put("/api/companion/release", json={"enabled": False})
        assert companion._release["checked_at"] == 0.0

    def test_put_requires_the_enabled_field(self, client):
        assert client.put("/api/companion/release", json={}).status_code == 400


class TestInstallIdentity:
    def test_release_file_wins_and_marks_a_packaged_install(self, monkeypatch, tmp_path):
        (tmp_path / "RELEASE").write_text("v1.2.3\n")
        monkeypatch.setattr(companion, "_repo_root", lambda: tmp_path)
        got = companion._installed_release()
        assert got["tag"] == "v1.2.3"
        assert got["source"] == "release-file"
        assert companion._install_kind() == "package"

    def test_unsubstituted_placeholder_is_not_a_release(self, monkeypatch, tmp_path):
        # A git checkout keeps the literal $Format:...$ from export-subst.
        (tmp_path / "RELEASE").write_text("$Format:%(describe:tags)$\n")
        monkeypatch.setattr(companion, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(companion, "_run", lambda *a, **k: None)
        assert companion._installed_release()["source"] is None

    def test_release_file_drift_suffix_is_parsed(self, monkeypatch, tmp_path):
        (tmp_path / "RELEASE").write_text("v1.1.1-9-gfda4f2f\n")
        monkeypatch.setattr(companion, "_repo_root", lambda: tmp_path)
        got = companion._installed_release()
        assert got == {"tag": "v1.1.1", "describe": "v1.1.1-9-gfda4f2f",
                       "ahead": 9, "source": "release-file"}

    def test_falls_back_to_the_package_database(self, monkeypatch, tmp_path):
        monkeypatch.setattr(companion, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(companion, "_run",
                            lambda cmd, cwd=None: "1.1.1" if cmd[0] == "dpkg-query" else None)
        got = companion._installed_release()
        assert got["tag"] == "v1.1.1"
        assert got["source"] == "dpkg"

    def test_nothing_identifiable_returns_no_source(self, monkeypatch, tmp_path):
        monkeypatch.setattr(companion, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(companion, "_run", lambda *a, **k: None)
        assert companion._installed_release()["tag"] is None


class TestVersionCompare:
    @pytest.mark.parametrize("latest,current,expect", [
        ("v1.1.2", "v1.1.1", True),
        ("v1.2.0", "v1.1.9", True),
        ("v1.1.1", "v1.1.1", False),
        ("v1.1.0", "v1.1.1", False),
        ("v2.0.0", "v1.9.9", True),
        ("junk", "v1.1.0", False),
        ("v1.1.0", "", False),
    ])
    def test_newer(self, latest, current, expect):
        assert companion._newer(latest, current) is expect


class TestReleaseNotesContract:
    """The companion's last-resort check greps the published release notes for
    the running build stamp, so release.yml must keep emitting it. These guard
    the two halves of that contract against silent drift."""

    @staticmethod
    def _workflow() -> str:
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        return (root / ".github" / "workflows" / "release.yml").read_text()

    def test_release_notes_carry_all_three_component_versions(self):
        wf = self._workflow()
        for token in ("${MANAGER_VERSION}", "${AE_VERSION}", "${AGENT_VERSION}"):
            assert token in wf, token
        assert "### Component versions" in wf

    def test_publish_job_receives_the_versions_from_the_build_job(self):
        wf = self._workflow()
        for token in ("needs.build.outputs.manager_version",
                      "needs.build.outputs.ae_version",
                      "needs.build.outputs.agent_version"):
            assert token in wf, token

    def test_the_stamp_reader_matches_this_repo_s_declarations(self):
        # Mirrors the sed in release.yml: if a component ever renames or
        # reformats its version line, this fails before a release does.
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        for rel, name in (
                ("llm-systems-manager/backend/llm-systems-manager.py", "__version__"),
                ("llm-systems-alarm-engine/backend/alarm_engine.py", "__version__"),
                ("agent/llm-systems-agent.py", "VERSION")):
            text = (root / rel).read_text()
            m = re.search(rf'^{name}\s*=\s*"(.*)"', text, re.M)
            assert m and m.group(1).strip(), f"{rel}: no readable {name}"


# ── #538: alarm-engine → web-push bridge ────────────────────────────────────

class TestNotifyToken:
    @staticmethod
    def _settings(companion_tok="", management="", ingest=""):
        return SimpleNamespace(
            manager=SimpleNamespace(
                companion=SimpleNamespace(push_notify_token=companion_tok)),
            alarm_engine=SimpleNamespace(
                management_token=management, ingest_token=ingest))

    def test_companion_override_wins(self):
        s = self._settings("own", management="mgmt", ingest="ing")
        assert companion.notify_token(s) == "own"

    def test_falls_back_to_the_management_token(self):
        assert companion.notify_token(self._settings(management="mgmt",
                                                     ingest="ing")) == "mgmt"

    def test_falls_back_to_the_ingest_token_last(self):
        assert companion.notify_token(self._settings(ingest="ing")) == "ing"

    @pytest.mark.parametrize("placeholder", ["", "   ", "REPLACE_ME", None])
    def test_placeholders_read_as_unset(self, placeholder):
        s = self._settings(placeholder, management=placeholder,
                           ingest=placeholder)
        assert companion.notify_token(s) == ""

    def test_missing_config_sections_do_not_raise(self):
        assert companion.notify_token(SimpleNamespace()) == ""


class TestNotifyPath:
    """A push must never deep-link off this manager."""

    @pytest.mark.parametrize("url,want", [
        ("/companion?tab=alerts", "/companion?tab=alerts"),
        ("/companion", "/companion"),
        ("https://evil.example/x", "/companion"),
        ("//evil.example/x", "/companion"),
        ("/companion\\..\\evil", "/companion"),
        ("javascript:alert(1)", "/companion"),
        ("", "/companion"), (None, "/companion"), (42, "/companion"),
    ])
    def test_only_same_origin_paths_survive(self, url, want):
        assert companion.notify_path(url) == want

    def test_path_is_length_capped(self):
        assert len(companion.notify_path("/" + "a" * 500)) == 200


@pytest.fixture
def notify(monkeypatch, sandbox):
    """Session-less client (as the alarm engine is) with a notify token
    configured; the actual web-push send is stubbed out."""
    monkeypatch.setattr(auth, "auth_mode", lambda: "required")
    monkeypatch.setattr(companion, "notify_token", lambda s: "ae-secret")
    monkeypatch.setattr(companion, "_webpush_funcs", lambda: None)
    monkeypatch.setattr(companion, "ensure_vapid_key", lambda d: d / "k.pem")
    with M.app.test_client() as c:
        yield c


def _sent(monkeypatch, ok=True, prune=False):
    """Capture the payload each subscription is sent."""
    seen = []

    def fake(sub, payload, pem, contact):
        seen.append(json.loads(payload))
        return ok, prune

    monkeypatch.setattr(companion, "_send_one", fake)
    return seen


class TestPushNotify:
    URL = "/api/companion/push/notify"
    AUTH = {"Authorization": "Bearer ae-secret"}

    def test_the_route_is_reachable_without_a_session(self):
        """The alarm engine has no session cookie, so the before_request gate
        must hand this path to the route's own bearer check."""
        assert self.URL in auth.AUTH_OPEN_PATHS

    def test_valid_bearer_fans_out_to_every_device(self, notify, monkeypatch):
        companion._store.add(_sub(), ua="ua")
        companion._store.add(_sub(endpoint="https://push.example.net/send/xyz"))
        seen = _sent(monkeypatch)
        r = notify.post(self.URL, headers=self.AUTH,
                        json={"title": "GPU hot", "body": "gpu = 91",
                              "severity": "critical", "tag": "lsm-alert-1",
                              "url": "/companion?tab=alerts"})
        assert r.status_code == 200
        body = r.get_json()
        assert body == {"ok": True, "sent": 2, "failed": 0, "pruned": 0,
                        "subscriptions": 2}
        assert seen[0]["title"] == "GPU hot"
        assert seen[0]["url"] == "/companion?tab=alerts"

    @pytest.mark.parametrize("headers", [
        {}, {"Authorization": "Bearer wrong"}, {"Authorization": "ae-secret"},
        {"Authorization": "Basic ae-secret"},
    ])
    def test_missing_or_wrong_bearer_is_refused(self, notify, headers):
        companion._store.add(_sub(), ua="ua")
        r = notify.post(self.URL, headers=headers, json={"title": "x"})
        assert r.status_code == 403

    def test_no_configured_token_falls_back_to_admin(self, monkeypatch, sandbox):
        """Fail closed: a token-less install must not expose an unauthenticated
        send-to-every-device route."""
        monkeypatch.setattr(auth, "auth_mode", lambda: "required")
        monkeypatch.setattr(companion, "notify_token", lambda s: "")
        with M.app.test_client() as c:
            r = c.post(self.URL, headers={"Authorization": "Bearer "},
                       json={"title": "x"})
        assert r.status_code == 403

    def test_an_admin_session_may_drive_it_without_a_token(self, client,
                                                           monkeypatch, sandbox):
        companion._store.add(_sub(), ua="ua")
        monkeypatch.setattr(companion, "notify_token", lambda s: "")
        monkeypatch.setattr(companion, "_webpush_funcs", lambda: None)
        monkeypatch.setattr(companion, "ensure_vapid_key", lambda d: d / "k.pem")
        _sent(monkeypatch)
        r = client.post(self.URL, json={"title": "x"})
        assert r.status_code == 200 and r.get_json()["sent"] == 1

    def test_an_operator_session_may_not(self, operator, monkeypatch, sandbox):
        companion._store.add(_sub(), ua="ua")
        monkeypatch.setattr(companion, "notify_token", lambda s: "")
        r = operator.post(self.URL, json={"title": "x"})
        assert r.status_code == 403

    def test_offsite_urls_are_rewritten_to_the_companion_root(self, notify,
                                                              monkeypatch):
        companion._store.add(_sub(), ua="ua")
        seen = _sent(monkeypatch)
        notify.post(self.URL, headers=self.AUTH,
                    json={"title": "x", "url": "https://evil.example/steal"})
        assert seen[0]["url"] == "/companion"

    def test_oversized_text_is_clamped(self, notify, monkeypatch):
        companion._store.add(_sub(), ua="ua")
        seen = _sent(monkeypatch)
        notify.post(self.URL, headers=self.AUTH,
                    json={"title": "T" * 999, "body": "B" * 999,
                          "tag": "g" * 999, "severity": "s" * 99})
        assert len(seen[0]["title"]) == companion._MAX_TITLE
        assert len(seen[0]["body"]) == companion._MAX_BODY
        assert len(seen[0]["tag"]) == companion._MAX_TAG
        assert len(seen[0]["severity"]) == 16

    def test_no_subscriptions_is_a_no_op_success(self, notify):
        r = notify.post(self.URL, headers=self.AUTH, json={"title": "x"})
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "sent": 0, "failed": 0,
                                "pruned": 0, "subscriptions": 0}

    def test_non_object_bodies_are_rejected(self, notify):
        companion._store.add(_sub(), ua="ua")
        for bad in ([1, 2], "x", 7, None):
            r = notify.post(self.URL, headers=self.AUTH, json=bad)
            assert r.status_code == 400, bad

    def test_expired_endpoints_are_pruned_from_the_store(self, notify,
                                                         monkeypatch):
        companion._store.add(_sub(), ua="ua")
        _sent(monkeypatch, ok=False, prune=True)
        r = notify.post(self.URL, headers=self.AUTH, json={"title": "x"})
        assert r.get_json()["pruned"] == 1
        assert companion._store.count() == 0

    def test_a_failed_send_reports_not_ok(self, notify, monkeypatch):
        companion._store.add(_sub(), ua="ua")
        _sent(monkeypatch, ok=False)
        body = notify.post(self.URL, headers=self.AUTH,
                           json={"title": "x"}).get_json()
        assert body["ok"] is False and body["failed"] == 1


# ── fleet-wide history aggregation (#541 follow-up) ─────────────────────────

class TestFleetAllHistory:
    """The unscoped /api/history merge let the LAST host writing a timestamp
    win, so an 8-host fleet's cpu_total was one arbitrary host per row and two
    llama hosts' tok/s never summed. ?fleet=all aggregates per _FLEET_FIELD_AGG.
    The alarm engine downsamples onto wall-clock boundaries, so every host
    reports the same timestamps — this relies on that.
    """

    TS = "2026-08-09T23:40:00+00:00"

    def _series(self, monkeypatch):
        """Two hosts reporting cpu_total, gpu_temp and llama_tps at one ts."""
        per_field = {
            "cpu_total": [("h1", 10.0), ("h2", 30.0)],       # mean -> 20
            "gpu_temp": [("h1", 50.0), ("h2", 70.0)],        # max  -> 70
            "llama_tps": [("h1", 4.0), ("h2", 6.0)],         # sum  -> 10
        }

        def fake(base, source, metric_name, field, since_minutes, limit,
                 hostname=None):
            return field, [{"timestamp": self.TS, "value": v, "hostname": h}
                           for h, v in per_field.get(field, [])]

        monkeypatch.setattr(M, "_fetch_history_series", fake)
        monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.test")

    def test_aggregate_combines_hosts_per_field_rule(self, monkeypatch):
        self._series(monkeypatch)
        rows = M._build_history_rows(1440, 100, aggregate=True)
        assert len(rows) == 1
        assert rows[0]["cpu_total"] == 20.0
        assert rows[0]["gpu_temp"] == 70.0
        assert rows[0]["llama_tps"] == 10.0

    def test_the_unscoped_default_is_unchanged(self, monkeypatch):
        """Existing callers (the dashboard) must not shift under this."""
        self._series(monkeypatch)
        rows = M._build_history_rows(1440, 100)
        assert len(rows) == 1
        # last-writer-wins, exactly as before
        assert rows[0]["cpu_total"] == 30.0

    def test_a_host_reporting_nothing_does_not_drag_the_mean(self, monkeypatch):
        def fake(base, source, metric_name, field, since_minutes, limit,
                 hostname=None):
            if field != "cpu_total":
                return field, []
            return field, [
                {"timestamp": self.TS, "value": 10.0, "hostname": "h1"},
                {"timestamp": self.TS, "value": None, "hostname": "h2"},
            ]

        monkeypatch.setattr(M, "_fetch_history_series", fake)
        monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.test")
        rows = M._build_history_rows(60, 100, aggregate=True)
        assert rows[0]["cpu_total"] == 10.0

    def test_duplicate_points_from_one_host_count_once(self, monkeypatch):
        """A host emitting twice for the same bucket must not double its weight
        in a mean — the per-host dict keeps the last value, not both."""
        def fake(base, source, metric_name, field, since_minutes, limit,
                 hostname=None):
            if field != "cpu_total":
                return field, []
            return field, [
                {"timestamp": self.TS, "value": 10.0, "hostname": "h1"},
                {"timestamp": self.TS, "value": 20.0, "hostname": "h1"},
                {"timestamp": self.TS, "value": 60.0, "hostname": "h2"},
            ]

        monkeypatch.setattr(M, "_fetch_history_series", fake)
        monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.test")
        rows = M._build_history_rows(60, 100, aggregate=True)
        assert rows[0]["cpu_total"] == 40.0  # mean(20, 60), not mean(10,20,60)

    def test_route_serves_fleet_all(self, client, monkeypatch):
        self._series(monkeypatch)
        monkeypatch.setattr(M, "_history_scoped_cache", {})
        r = client.get("/api/history?since_minutes=1440&fleet=all")
        assert r.status_code == 200
        rows = r.get_json()
        assert rows and rows[0]["cpu_total"] == 20.0

    def test_an_unknown_provider_is_still_rejected(self, client, monkeypatch):
        monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.test")
        r = client.get("/api/history?fleet=not-a-provider")
        assert r.status_code == 400
