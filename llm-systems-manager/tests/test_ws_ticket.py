"""
Alarm WS bridge ticket (#514): issue/verify pair, the route's auth gating,
and the handshake gate's accept/reject behaviour.
"""
from __future__ import annotations

import time

import pytest

import auth
import manager_mod as M


# ── issue / verify ───────────────────────────────────────────────────────────

class TestTicketRoundTrip:
    def test_issued_ticket_verifies(self):
        assert M._verify_ws_ticket(M._issue_ws_ticket()) is True

    def test_shape_is_expiry_nonce_sig(self):
        expiry_str, nonce, sig = M._issue_ws_ticket().split(".")
        assert int(expiry_str) > time.time()
        assert len(nonce) == 16  # 8 random bytes, hex
        assert len(sig) == 64  # sha256 hexdigest

    def test_expiry_honours_requested_ttl(self):
        before = int(time.time())
        expiry = int(M._issue_ws_ticket(ttl=60).split(".", 1)[0])
        assert before + 60 <= expiry <= before + 61


class TestTicketRejection:
    @pytest.mark.parametrize("bad", [
        "", "   ", "no-dot", ".", "abc.def", "|.|",
        "notanint.aa" * 1, "999999999999.", ".deadbeef",
    ])
    def test_malformed_rejected(self, bad):
        assert M._verify_ws_ticket(bad) is False

    def test_none_rejected(self):
        assert M._verify_ws_ticket(None) is False

    def test_expired_rejected(self):
        assert M._verify_ws_ticket(M._issue_ws_ticket(ttl=-1)) is False

    def test_tampered_signature_rejected(self):
        expiry, sig = M._issue_ws_ticket().split(".", 1)
        flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
        assert M._verify_ws_ticket(f"{expiry}.{flipped}") is False

    def test_tampered_expiry_rejected(self):
        """Extending the expiry must invalidate the signature."""
        expiry, sig = M._issue_ws_ticket(ttl=60).split(".", 1)
        assert M._verify_ws_ticket(f"{int(expiry) + 86400}.{sig}") is False

    def test_foreign_secret_rejected(self, monkeypatch):
        stolen = M._issue_ws_ticket()
        monkeypatch.setattr(M, "_manager_secret", lambda: b"a-different-secret")
        assert M._verify_ws_ticket(stolen) is False


# ── route ────────────────────────────────────────────────────────────────────

class TestTicketRoute:
    def test_requires_a_session(self, monkeypatch):
        monkeypatch.setattr(auth, "auth_mode", lambda: "required")
        with M.app.test_client() as c:
            r = c.get("/api/alarm-ws-ticket")
        assert r.status_code in (302, 401, 403)

    def test_authenticated_session_gets_a_usable_ticket(self, monkeypatch):
        monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
        with M.app.test_client() as c:
            r = c.get("/api/alarm-ws-ticket")
        assert r.status_code == 200
        body = r.get_json()
        assert M._verify_ws_ticket(body["ticket"]) is True
        assert body["ttl_s"] > 0

    def test_not_shadowed_by_the_alarm_proxy_catch_all(self):
        """Resolves to the local view, not the /api/alarm/<path> AE proxy."""
        adapter = M.app.url_map.bind("localhost")
        endpoint, _ = adapter.match("/api/alarm-ws-ticket", method="GET")
        assert endpoint == "alarm_ws_ticket"


# ── handshake gate ───────────────────────────────────────────────────────────
# Exercises ws_handshake_denial itself — the function the proxy handler calls.

class TestHandshakeGate:
    def test_valid_ticket_is_bridged(self):
        assert M.ws_handshake_denial(f"/ws/alarm?ticket={M._issue_ws_ticket()}") is None

    def test_missing_ticket_rejected(self):
        assert M.ws_handshake_denial("/ws/alarm") == (1008, "unauthorized")

    def test_empty_ticket_rejected(self):
        assert M.ws_handshake_denial("/ws/alarm?ticket=") == (1008, "unauthorized")

    def test_expired_ticket_rejected(self):
        t = M._issue_ws_ticket(ttl=-1)
        assert M.ws_handshake_denial(f"/ws/alarm?ticket={t}") == (1008, "unauthorized")

    def test_tampered_ticket_rejected(self):
        expiry, sig = M._issue_ws_ticket().split(".", 1)
        bad = f"{expiry}.{'0' * 64}"
        assert M.ws_handshake_denial(f"/ws/alarm?ticket={bad}") == (1008, "unauthorized")

    def test_wrong_path_rejected_before_the_ticket_is_read(self):
        t = M._issue_ws_ticket()
        assert M.ws_handshake_denial(f"/nope?ticket={t}") == (1008, "unknown path")

    def test_other_query_params_do_not_break_extraction(self):
        t = M._issue_ws_ticket()
        assert M.ws_handshake_denial(f"/ws/alarm?foo=1&ticket={t}&bar=2") is None

    @pytest.mark.parametrize("target", ["", "/", None])
    def test_degenerate_targets_rejected(self, target):
        assert M.ws_handshake_denial(target) == (1008, "unknown path")


class TestHandlerUsesTheGate:
    """Source assertions: the closure handler delegates to the shared gate."""
    def test_proxy_handler_calls_ws_handshake_denial(self):
        import inspect
        src = inspect.getsource(M._maybe_start_alarm_ws_proxy)
        assert "ws_handshake_denial(req_target)" in src
        assert "_verify_ws_ticket" not in src  # gate lives in one place only


# ── OpenClaw bridge path ─────────────────────────────────────────────────────

class TestOpenclawBridgeTickets:
    def test_openclaw_ticket_bridges_on_its_own_path(self):
        t = M._issue_ws_ticket(path="/ws/openclaw")
        assert M.ws_handshake_denial(f"/ws/openclaw?ticket={t}") is None

    def test_tickets_are_bound_to_their_path(self):
        alarm = M._issue_ws_ticket()
        claw = M._issue_ws_ticket(path="/ws/openclaw")
        assert M.ws_handshake_denial(f"/ws/openclaw?ticket={alarm}") == (1008, "unauthorized")
        assert M.ws_handshake_denial(f"/ws/alarm?ticket={claw}") == (1008, "unauthorized")

    def test_openclaw_ticket_is_single_use(self):
        t = M._issue_ws_ticket(path="/ws/openclaw")
        assert M.ws_handshake_denial(f"/ws/openclaw?ticket={t}") is None
        assert M.ws_handshake_denial(f"/ws/openclaw?ticket={t}") == (1008, "unauthorized")

    def test_bridge_path_resolution(self):
        assert M._ws_bridge_path("/ws/openclaw?ticket=x") == "/ws/openclaw"
        assert M._ws_bridge_path("/ws/alarm?ticket=x") == "/ws/alarm"
        assert M._ws_bridge_path("/nope") == ""

    def test_route_issues_an_openclaw_ticket(self, monkeypatch):
        monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
        with M.app.test_client() as c:
            r = c.get("/api/openclaw-ws-ticket")
        assert r.status_code == 200
        body = r.get_json()
        assert M._verify_ws_ticket(body["ticket"], "/ws/openclaw") is True
        assert M._verify_ws_ticket(body["ticket"]) is False

    def test_openclaw_ticket_route_requires_a_session(self, monkeypatch):
        monkeypatch.setattr(auth, "auth_mode", lambda: "required")
        with M.app.test_client() as c:
            r = c.get("/api/openclaw-ws-ticket")
        assert r.status_code in (302, 401, 403)

    def test_proxy_handler_dispatches_on_the_bridge_path(self):
        import inspect
        src = inspect.getsource(M._maybe_start_alarm_ws_proxy)
        assert '_ws_bridge_path(req_target) == "/ws/openclaw"' in src
