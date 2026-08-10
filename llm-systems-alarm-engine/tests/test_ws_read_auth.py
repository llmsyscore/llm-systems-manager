"""#519: the /ws live stream takes the same bearer gate as the management
routes — management_token, else ingest_token, open when neither is set —
and rejections close 1008 before accept()."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from config.unified_config import settings
from backend import alarm_engine as ae
from backend.api.auth import management_bearer_ok


class _StubWsManager:
    async def connect(self, ws):
        await ws.accept()
        return "cid"

    def subscribe_all(self, cid):
        pass

    def subscribe(self, cid, event_type):
        pass

    def unsubscribe_all(self, cid):
        pass

    def unsubscribe(self, cid, event_type):
        pass

    async def disconnect(self, cid):
        pass


@pytest.fixture(autouse=True)
def _stub_ws_manager(monkeypatch):
    monkeypatch.setattr(ae, "ws_manager", _StubWsManager())


def _set_tokens(monkeypatch, ingest="", management=""):
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", ingest, raising=False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", management, raising=False)
    monkeypatch.setattr(settings.alarm_engine, "cors_origins", "", raising=False)


def _assert_connects(headers=None):
    client = TestClient(ae.app)
    with client.websocket_connect("/ws", headers=headers or {}) as ws:
        ws.send_json({"action": "ping"})
        assert ws.receive_json()["event"] == "pong"


def _assert_rejected(headers=None):
    client = TestClient(ae.app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers=headers or {}):
            pass
    assert exc.value.code == 1008


def test_open_when_no_token_configured(monkeypatch):
    _set_tokens(monkeypatch)
    _assert_connects()


def test_placeholder_token_stays_open(monkeypatch):
    _set_tokens(monkeypatch, ingest="REPLACE_ME")
    _assert_connects()


def test_missing_bearer_is_rejected_when_token_set(monkeypatch):
    _set_tokens(monkeypatch, ingest="sekrit")
    _assert_rejected()


def test_correct_ingest_bearer_connects(monkeypatch):
    _set_tokens(monkeypatch, ingest="sekrit")
    _assert_connects({"Authorization": "Bearer sekrit"})


def test_wrong_bearer_is_rejected(monkeypatch):
    _set_tokens(monkeypatch, ingest="sekrit")
    _assert_rejected({"Authorization": "Bearer wrong"})


def test_management_token_wins_over_ingest(monkeypatch):
    # Same rule as require_management_token: when both are set, only the
    # management token opens the read surface.
    _set_tokens(monkeypatch, ingest="write-tok", management="read-tok")
    _assert_connects({"Authorization": "Bearer read-tok"})
    _assert_rejected({"Authorization": "Bearer write-tok"})


def test_cross_origin_rejected_even_with_valid_bearer(monkeypatch):
    _set_tokens(monkeypatch, ingest="sekrit")
    _assert_rejected({"Authorization": "Bearer sekrit",
                      "Origin": "http://evil.example"})


def test_management_bearer_ok_unit(monkeypatch):
    _set_tokens(monkeypatch)
    assert management_bearer_ok(None) is True
    _set_tokens(monkeypatch, ingest="tok")
    assert management_bearer_ok("Bearer tok") is True
    assert management_bearer_ok("Bearer nope") is False
    assert management_bearer_ok(None) is False
    assert management_bearer_ok("tok") is False          # not a Bearer header
