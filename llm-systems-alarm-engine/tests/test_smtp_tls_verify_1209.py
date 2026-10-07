"""#1209: email sends verify the mail server's certificate on both TLS paths,
using the system trust store or [notifications.smtp].ca_file."""
from __future__ import annotations

import ssl
from email.mime.text import MIMEText
from types import SimpleNamespace

import pytest

import backend.engine.notification_dispatcher as nd


class _FakeSMTP:
    calls: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.calls.append((type(self).__name__, port, context))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.calls.append(("starttls", context))

    def login(self, user, password):
        pass

    def send_message(self, msg):
        self.calls.append("send")


class _FakeSMTPSSL(_FakeSMTP):
    pass


def _settings(port, ca_file=""):
    return SimpleNamespace(notifications=SimpleNamespace(
        smtp=SimpleNamespace(server="mail.example", port=port, user="u@example", password="p", ca_file=ca_file),
        timeouts=SimpleNamespace(smtp=5)))


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    _FakeSMTP.calls = []
    monkeypatch.setattr(nd.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(nd.smtplib, "SMTP_SSL", _FakeSMTPSSL)


def _send(monkeypatch, port, ca_file=""):
    monkeypatch.setattr(nd, "settings", _settings(port, ca_file))
    nd.NotificationDispatcher._send_sync_email(SimpleNamespace(), MIMEText("body"), None)
    return _FakeSMTP.calls


def _is_verifying(ctx):
    return isinstance(ctx, ssl.SSLContext) and ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED


def test_port_465_connects_with_a_verifying_context(monkeypatch):
    calls = _send(monkeypatch, 465)
    name, port, ctx = calls[0]
    assert name == "_FakeSMTPSSL" and port == 465 and _is_verifying(ctx)
    assert calls[-1] == "send"


def test_starttls_uses_a_verifying_context(monkeypatch):
    calls = _send(monkeypatch, 587)
    assert calls[0][0] == "_FakeSMTP" and calls[0][2] is None
    tag, ctx = calls[1]
    assert tag == "starttls" and _is_verifying(ctx)


def test_port_25_stays_plain_and_builds_no_context(monkeypatch):
    seen = []
    monkeypatch.setattr(nd.ssl, "create_default_context", lambda **kw: seen.append(kw) or ssl.SSLContext())
    calls = _send(monkeypatch, 25)
    assert calls == [("_FakeSMTP", 25, None), "send"] and seen == []


def test_a_ca_file_becomes_the_trust_anchor(monkeypatch, tmp_path):
    seen = []
    real = ssl.create_default_context
    monkeypatch.setattr(nd.ssl, "create_default_context", lambda **kw: seen.append(kw) or real())
    _send(monkeypatch, 465, ca_file=str(tmp_path / "relay-ca.pem"))
    assert seen == [{"cafile": str(tmp_path / "relay-ca.pem")}]


def test_blank_ca_file_means_the_system_store(monkeypatch):
    seen = []
    real = ssl.create_default_context
    monkeypatch.setattr(nd.ssl, "create_default_context", lambda **kw: seen.append(kw) or real())
    _send(monkeypatch, 587, ca_file="  ")
    assert seen == [{"cafile": None}]


def test_a_missing_ca_file_fails_clearly(monkeypatch, tmp_path):
    monkeypatch.setattr(nd, "settings", _settings(465, str(tmp_path / "missing.pem")))
    with pytest.raises(RuntimeError, match="ca_file not found"):
        nd.NotificationDispatcher._send_sync_email(SimpleNamespace(), MIMEText("body"), None)
    assert _FakeSMTP.calls == []


def test_a_settings_object_without_ca_file_still_sends(monkeypatch):
    monkeypatch.setattr(nd, "settings", SimpleNamespace(notifications=SimpleNamespace(
        smtp=SimpleNamespace(server="mail.example", port=465, user=None, password=None),
        timeouts=SimpleNamespace(smtp=5))))
    nd.NotificationDispatcher._send_sync_email(SimpleNamespace(), MIMEText("body"), None)
    assert _FakeSMTP.calls[-1] == "send" and _is_verifying(_FakeSMTP.calls[0][2])
