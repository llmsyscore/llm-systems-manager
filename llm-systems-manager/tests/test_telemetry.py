"""telemetry.py: identity file, payload shape, crash sanitising, dedupe, CLI."""
from __future__ import annotations
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import telemetry

BACKEND = Path(__file__).resolve().parents[1] / "backend"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    s = SimpleNamespace(manager=SimpleNamespace(telemetry=SimpleNamespace(
        install_report=True, crash_reports=True, heartbeat=False,
        endpoint="https://telemetry.example.test/v1/event")))
    telemetry.configure(settings=s, data_dir=tmp_path, version="v2026.10.14-1",
                        root=BACKEND.parent.parent)
    monkeypatch.setattr(telemetry, "_snapshot_settings", lambda: s)
    yield tmp_path
    telemetry._agent_count_fn = None
    telemetry._provider_mix_fn = None


def test_install_identity_is_stable_and_timestamped(tmp_path):
    iid, at = telemetry.install_identity(tmp_path)
    uuid.UUID(iid)
    assert at.endswith("Z")
    assert telemetry.install_identity(tmp_path) == (iid, at)
    assert (tmp_path / "install-id").read_text().strip() == iid


def test_payload_has_schema_fields_and_no_host_names(cfg):
    import socket
    p = telemetry.build_payload("install", data_dir=cfg, version="v1", channel="script",
                                mode="3", provider="llama", outcome="ok")
    assert p["schema"] == 1 and p["event"] == "install"
    for k in ("install_id", "installed_at", "sent_at", "version", "channel", "mode",
              "os", "arch", "ram_gb", "gpu_vendor", "gpu_count", "provider", "outcome"):
        assert k in p, k
    assert p["agent_count"] is None and p["frames"] is None
    text = json.dumps(p)
    assert socket.gethostname() not in text
    assert str(cfg) not in text


def test_heartbeat_payload_omits_hardware(cfg):
    telemetry.configure(settings=telemetry._settings, data_dir=cfg, version="v1",
                        root=BACKEND.parent.parent,
                        agent_count_fn=lambda: 3, provider_mix_fn=lambda: {"llama": 2, "lms": 1})
    p = telemetry.build_payload("heartbeat", data_dir=cfg, version="v1", channel="deb")
    assert p["agent_count"] == 3 and p["provider_mix"] == "llama=2,lms=1"
    assert p["cpu_model"] is None and p["ram_gb"] is None


def test_frames_are_relative_and_message_free(cfg):
    root = BACKEND.parent.parent
    try:
        raise ValueError("secret-host.example.lan /etc/passwd")
    except ValueError as e:
        p = telemetry.build_crash_payload(e, e.__traceback__, component="manager", data_dir=cfg,
                                          version="v1", channel="script", root=root, uptime_s=12)
    assert p["event"] == "crash" and p["exc_type"] == "ValueError"
    assert "secret-host" not in json.dumps(p) and "/etc/passwd" not in json.dumps(p)
    assert p["frames"].startswith("llm-systems-manager/tests/test_telemetry.py:")
    assert str(root) not in p["frames"]
    assert len(p["crash_hash"]) == 16


def test_frames_cap(cfg):
    def deep(n):
        if n == 0:
            raise RuntimeError("x")
        deep(n - 1)
    try:
        deep(40)
    except RuntimeError as e:
        txt = telemetry.frames_text(e.__traceback__, BACKEND.parent.parent)
    assert txt.count(";") <= 19 and len(txt) <= 2048


def test_dedupe_window_and_daily_cap(cfg):
    now = time.time()
    assert telemetry.allow_crash(cfg, "aaaa", now)
    assert not telemetry.allow_crash(cfg, "aaaa", now + 60)
    assert telemetry.allow_crash(cfg, "aaaa", now + 86401)
    for h in ("b1", "b2", "b3", "b4"):
        assert telemetry.allow_crash(cfg, h, now + 86402)
    assert not telemetry.allow_crash(cfg, "b5", now + 86403)


def test_send_never_raises_and_records(cfg, monkeypatch):
    sent = []
    class R:  # minimal response stand-in
        status_code = 204
    monkeypatch.setattr(telemetry, "_post", lambda url, body, timeout: sent.append((url, body)) or R())
    p = telemetry.build_payload("install", data_dir=cfg, version="v1", channel="script")
    assert telemetry.send(p, "https://telemetry.example.test/v1/event") is True
    telemetry.record_sent(cfg, p)
    assert json.loads((cfg / "telemetry-last.json").read_text())["event"] == "install"
    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(telemetry, "_post", boom)
    assert telemetry.send(p, "https://telemetry.example.test/v1/event") is False


def test_pending_roundtrip(cfg, monkeypatch):
    p = telemetry.build_payload("install", data_dir=cfg, version="v1", channel="brew")
    telemetry.write_pending(cfg, p)
    assert (cfg / "telemetry-pending.json").exists()
    monkeypatch.setattr(telemetry, "send", lambda payload, endpoint, timeout=5.0: True)
    assert telemetry.flush_pending(cfg, "https://x/v1/event") is True
    assert not (cfg / "telemetry-pending.json").exists()
    assert (cfg / "telemetry-last.json").exists()


def test_hook_never_raises(cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("inner")
    monkeypatch.setattr(telemetry, "build_crash_payload", boom)
    telemetry.report_exception(ValueError("x"), None, component="manager")  # must not raise


def test_cli_payload_and_facts(tmp_path):
    out = subprocess.run([sys.executable, "-I", str(BACKEND / "telemetry.py"), "payload",
                          "--event", "install", "--channel", "script", "--mode", "1",
                          "--data-dir", str(tmp_path), "--version", "v9", "--root", str(BACKEND.parent.parent)],
                         capture_output=True, text=True, check=True).stdout
    p = json.loads(out)
    assert p["event"] == "install" and p["version"] == "v9" and p["channel"] == "script"
    facts = json.loads(subprocess.run([sys.executable, "-I", str(BACKEND / "telemetry.py"), "facts"],
                                      capture_output=True, text=True, check=True).stdout)
    assert "os" in facts and "arch" in facts


def test_failed_heartbeat_keeps_pending_install(cfg, monkeypatch):
    monkeypatch.setattr(telemetry, "send", lambda payload, endpoint, timeout=5.0: False)
    inst = telemetry.build_payload("install", data_dir=cfg, version="v1", channel="script")
    assert telemetry._deliver(inst) is False
    pend = cfg / "telemetry-pending.json"
    assert json.loads(pend.read_text())["event"] == "install"
    hb = telemetry.build_payload("heartbeat", data_dir=cfg, version="v1", channel="script")
    assert telemetry._deliver(hb) is False
    assert json.loads(pend.read_text())["event"] == "install"


def test_pinned_import_error_does_not_fall_back(cfg, monkeypatch):
    import urllib.request
    from tests.conftest import REAL_TELEMETRY_POST
    monkeypatch.setattr(telemetry, "_post", REAL_TELEMETRY_POST)

    def _bad(*a, **k):
        raise ImportError("inside session")
    monkeypatch.setitem(sys.modules, "companion", SimpleNamespace(_push_session=_bad))

    used = []

    def _no(*a, **k):
        used.append(1)
        raise AssertionError("urllib used")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _no)
    p = telemetry.build_payload("install", data_dir=cfg, version="v1", channel="script")
    assert telemetry.send(p, "https://telemetry.example.test/v1/event") is False
    assert used == []


def test_allow_crash_threads(cfg):
    import threading
    now = time.time()
    res = []
    ts = [threading.Thread(target=lambda i=i: res.append(telemetry.allow_crash(cfg, f"h{i}", now)))
          for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(res) == 5


def test_frames_trim_keeps_innermost(cfg):
    root = BACKEND.parent.parent
    fake = [SimpleNamespace(filename=str(root / ("f" * 150 + str(i) + ".py")), lineno=i, name="n")
            for i in range(20)]
    import traceback
    orig = traceback.extract_tb
    try:
        traceback.extract_tb = lambda tb: fake
        txt = telemetry.frames_text(None, root)
    finally:
        traceback.extract_tb = orig
    assert len(txt) <= 2048 and txt.endswith(":19:n") and "0.py:0:" not in txt


def test_detect_channel_is_memoised(monkeypatch):
    calls = []
    monkeypatch.setattr(telemetry, "_channel_cache", {})
    monkeypatch.setattr(telemetry.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(telemetry.subprocess, "run",
                        lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=1))
    root = BACKEND.parent.parent
    first = telemetry.detect_channel(root)
    n = len(calls)
    assert telemetry.detect_channel(root) == first and len(calls) == n


def test_report_exception_dedupe_gate_runs_first(cfg, monkeypatch):
    hits = []
    for name in ("detect_channel", "build_payload", "_blank", "_deliver"):
        monkeypatch.setattr(telemetry, name, lambda *a, _n=name, **k: hits.append(_n))
    monkeypatch.setattr(telemetry, "allow_crash", lambda *a, **k: False)
    telemetry.report_exception(ValueError("x"), None, component="manager")
    assert hits == []


def test_install_identity_replaces_symlink_without_writing_through(tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep")
    (tmp_path / telemetry.IDFILE).symlink_to(target)
    iid, _ = telemetry.install_identity(tmp_path)
    f = tmp_path / telemetry.IDFILE
    assert not f.is_symlink() and f.read_text().strip() == iid
    assert target.read_text() == "keep"


def test_write_json_does_not_follow_planted_temp_symlink(tmp_path, monkeypatch):
    target = tmp_path / "victim"
    target.write_text("keep")
    dest = tmp_path / "out.json"
    import os, threading
    (tmp_path / f"out.{os.getpid()}.{threading.get_ident()}.tmp").symlink_to(target)
    telemetry._write_json(dest, {"a": 1})
    assert target.read_text() == "keep" and not dest.is_symlink()


def test_endpoint_env_override_and_default(monkeypatch):
    monkeypatch.delenv("LLMSYS_TELEMETRY_ENDPOINT", raising=False)
    assert telemetry.endpoint() == telemetry.DEFAULT_ENDPOINT
    monkeypatch.setenv("LLMSYS_TELEMETRY_ENDPOINT", "https://x.example.test/e")
    assert telemetry.endpoint() == "https://x.example.test/e"


def test_append_log_refuses_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (tmp_path / telemetry.LOG).symlink_to(victim)
    telemetry._append_log(tmp_path, "x")
    assert victim.read_text() == "keep"
