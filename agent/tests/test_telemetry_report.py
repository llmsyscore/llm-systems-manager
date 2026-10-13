"""Agent crash reports: consent file, sanitised payload, dedupe, hooks never raise."""
from __future__ import annotations
import importlib.util
import json
import sys
import time
from pathlib import Path

_AGENT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("telemetry_report", _AGENT / "telemetry_report.py")
TR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(TR)


def test_consent_roundtrip(tmp_path):
    assert TR.load_consent(str(tmp_path)) == {"crash_reports": False}
    TR.write_consent(str(tmp_path), install_report=True, crash_reports=True,
                     endpoint="https://t.example.test/v1/event",
                     install_id="123e4567-e89b-12d3-a456-426614174000", installed_at="2026-10-14T00:00:00Z")
    c = TR.load_consent(str(tmp_path))
    assert c["crash_reports"] is True and c["install_id"].startswith("123e")


def test_payload_is_sanitised(tmp_path):
    TR.write_consent(str(tmp_path), install_report=True, crash_reports=True,
                     endpoint="https://t.example.test/v1/event",
                     install_id="123e4567-e89b-12d3-a456-426614174000", installed_at="2026-10-14T00:00:00Z")
    try:
        raise ValueError("host.lan /opt/secret")
    except ValueError as e:
        p = TR.build_crash_payload(e, e.__traceback__, version="v1", root=str(_AGENT.parent),
                                   state_dir=str(tmp_path), uptime_s=5)
    assert p["schema"] == 1 and p["event"] == "crash" and p["component"] == "agent"
    assert p["exc_type"] == "ValueError" and p["channel"] == "agent"
    s = json.dumps(p)
    assert "host.lan" not in s and "/opt/secret" not in s and str(_AGENT.parent) not in s
    assert p["frames"].startswith("agent/tests/test_telemetry_report.py:")
    assert None not in p.values()


def test_dedupe(tmp_path):
    now = time.time()
    assert TR.allow_crash(str(tmp_path), "h1", now)
    assert not TR.allow_crash(str(tmp_path), "h1", now + 10)
    for h in ("h2", "h3", "h4", "h5"):
        assert TR.allow_crash(str(tmp_path), h, now)
    assert not TR.allow_crash(str(tmp_path), "h6", now)


def test_hooks_never_raise(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: calls.append(a))
    monkeypatch.setattr(TR.threading, "excepthook", TR.threading.excepthook)
    TR.install_hooks(state_dir=str(tmp_path), version="v1", root=str(_AGENT.parent))
    monkeypatch.setattr(TR, "build_crash_payload", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    sys.excepthook(ValueError, ValueError("x"), None)  # must not raise
    assert len(calls) == 1


class _Resp:
    status = 302

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_redirect_is_failure(monkeypatch):
    monkeypatch.setattr(TR.urllib.request.OpenerDirector, "open", lambda self, *a, **k: _Resp())
    assert TR.send({"a": 1}, "https://t.example.test/v1/event") is False


def test_send_non_https_opens_nothing(monkeypatch):
    def boom(self, *a, **k):
        raise AssertionError("opened")
    monkeypatch.setattr(TR.urllib.request.OpenerDirector, "open", boom)
    assert TR.send({"a": 1}, "http://t.example.test/v1/event") is False


def test_frames_skip_venv_and_site_packages(tmp_path):
    lib, app = {}, {}
    exec(compile("def boom():\n    raise ValueError('x')\n",
                 str(tmp_path / "venv/lib/python3.12/site-packages/pkg/mod.py"), "exec"), lib)
    exec(compile("def outer(f):\n    f()\n", str(tmp_path / "agent/app.py"), "exec"), app)
    try:
        app["outer"](lib["boom"])
    except ValueError as e:
        txt = TR.frames_text(e.__traceback__, str(tmp_path))
    assert txt == "agent/app.py:2:outer"
