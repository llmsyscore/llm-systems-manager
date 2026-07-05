"""#214: gateway API-key branch in the auth gate."""
from flask import Flask

import auth


def _ctx(app, headers=None):
    return app.test_request_context("/api/gateway/v1/models", headers=headers or {})


def test_key_matches(monkeypatch):
    app = Flask(__name__)
    monkeypatch.setattr(auth, "_gateway_api_keys", lambda: ["k-one", "k-two"])
    with _ctx(app, {"Authorization": "Bearer k-two"}):
        assert auth._gateway_key_ok() is True


def test_wrong_key_rejected(monkeypatch):
    app = Flask(__name__)
    monkeypatch.setattr(auth, "_gateway_api_keys", lambda: ["k-one"])
    with _ctx(app, {"Authorization": "Bearer nope"}):
        assert auth._gateway_key_ok() is False


def test_empty_keys_reject_everything(monkeypatch):
    app = Flask(__name__)
    monkeypatch.setattr(auth, "_gateway_api_keys", lambda: [])
    with _ctx(app, {"Authorization": "Bearer anything"}):
        assert auth._gateway_key_ok() is False
