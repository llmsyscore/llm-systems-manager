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
            assert anon.get(icon["src"]).status_code == 200, icon["src"]

    def test_every_service_worker_shell_path_serves(self, client):
        """Every path sw.js precaches must exist, or install caches nothing."""
        body = client.get("/sw.js").data.decode()
        shell = re.findall(r"^\s*'(/[^']+)',", body, re.M)
        assert len(shell) >= 5, f"SHELL list not parsed: {shell}"
        for path in shell:
            assert client.get(path).status_code == 200, path


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
    /companion (standalone has no URL bar). It must never leave the origin."""

    @pytest.mark.parametrize("raw", ["/companion", "/", "/companion?x=1",
                                     "/admin/agents"])
    def test_accepts_same_origin_paths(self, raw):
        assert auth.safe_next(raw) == raw

    @pytest.mark.parametrize("raw", [
        "//evil.example/x",           # protocol-relative
        "https://evil.example/x",
        "http://evil.example",
        "/\\evil.example",            # backslash
        "\\\\evil.example",
        "javascript:alert(1)",
        "evil.example", "", None, "/ space",
    ])
    def test_rejects_offsite_and_malformed(self, raw):
        assert auth.safe_next(raw) is None

    def test_login_returns_to_next_after_success(self, monkeypatch, sandbox):
        """Both flows: next carried on the GET (stashed in session) and next
        carried on the POST query string."""
        monkeypatch.setattr(auth, "auth_mode", lambda: "required")
        with M.app.test_client() as c:
            c.get("/login?next=%2Fcompanion")
            r = c.post("/login", data={"username": "llmadmin",
                                       "password": "llmadmin"})
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/companion")

        with M.app.test_client() as c:
            c.get("/login")
            r = c.post("/login?next=%2Fcompanion",
                       data={"username": "llmadmin", "password": "llmadmin"})
        assert r.headers["Location"].endswith("/companion")

    def test_login_ignores_offsite_next(self, monkeypatch, sandbox):
        monkeypatch.setattr(auth, "auth_mode", lambda: "required")
        with M.app.test_client() as c:
            c.get("/login?next=https%3A%2F%2Fevil.example%2Fx")
            r = c.post("/login", data={"username": "llmadmin",
                                       "password": "llmadmin"})
        assert "evil.example" not in r.headers.get("Location", "")


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
        assert client.get(
            "/api/companion/push/subscriptions").get_json()["count"] == 1
