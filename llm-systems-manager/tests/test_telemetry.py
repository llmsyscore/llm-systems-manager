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
        install_report=True, crash_reports=True, checkin=False, heartbeat=False,
        endpoint="https://telemetry.example.test/v1/event")))
    telemetry.configure(settings=s, data_dir=tmp_path, version="v2026.10.13-1",
                        root=BACKEND.parent.parent)
    monkeypatch.setattr(telemetry, "_snapshot_settings", lambda: s)
    yield tmp_path
    telemetry._agent_count_fn = None
    telemetry._provider_mix_fn = None
    telemetry._release_fn = telemetry._manager_version_fn = None
    telemetry._ae_version_fn = telemetry._agent_versions_fn = None
    telemetry._features_fn = None


def test_install_identity_is_stable_and_timestamped(tmp_path):
    iid, at = telemetry.install_identity(tmp_path)
    uuid.UUID(iid)
    assert at.endswith("Z")
    assert telemetry.install_identity(tmp_path) == (iid, at)
    assert (tmp_path / "install-id").read_text().strip() == iid


def test_payload_has_schema_fields_and_no_host_names(cfg):
    import socket
    p = telemetry.build_payload("install", data_dir=cfg, version="v1", channel="script",
                                mode="3", outcome="ok")
    assert p["schema"] == 1 and p["event"] == "install"
    for k in ("install_id", "installed_at", "sent_at", "version", "channel", "mode",
              "os", "arch", "ram_gb", "gpu_vendor", "gpu_count", "outcome"):
        assert k in p, k
    assert "agent_count" not in p and "frames" not in p
    text = json.dumps(p)
    assert socket.gethostname() not in text
    assert str(cfg) not in text


def test_heartbeat_payload_includes_host_facts(cfg):
    telemetry.configure(settings=telemetry._settings, data_dir=cfg, version="v1",
                        root=BACKEND.parent.parent,
                        agent_count_fn=lambda: 3, provider_mix_fn=lambda: {"llama": 2, "lms": 1})
    p = telemetry.build_payload("heartbeat", data_dir=cfg, version="v1", channel="deb")
    assert p["agent_count"] == 3 and p["provider_mix"] == "llama=2,lms=1"
    assert p["os"] and p["arch"] and isinstance(p["cpu_model"], str)


def test_heartbeat_carries_component_versions_and_install_release(cfg):
    telemetry.configure(settings=telemetry._settings, data_dir=cfg, version="v1",
                        root=BACKEND.parent.parent, release_fn=lambda: "v2026.10",
                        manager_version_fn=lambda: "m1", alarm_engine_version_fn=lambda: "ae1",
                        agent_versions_fn=lambda: {"v2": 1, "v1": 7})
    hb = telemetry.build_payload("heartbeat", data_dir=cfg, version="v1", channel="deb")
    assert (hb["release"], hb["manager_version"], hb["alarm_engine_version"]) == ("v2026.10", "m1", "ae1")
    assert hb["agent_versions"] == "v1=7,v2=1"
    ins = telemetry.build_payload("install", data_dir=cfg, version="v1", channel="deb")
    assert ins["release"] == "v2026.10" and "manager_version" not in ins and "agent_versions" not in ins
    telemetry.configure(settings=telemetry._settings, data_dir=cfg, version="v1",
                        root=BACKEND.parent.parent)
    assert "release" not in telemetry.build_payload("install", data_dir=cfg, version="v1", channel="deb")


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
                          "--data-dir", str(tmp_path), "--version", "v9", "--root", str(BACKEND.parent.parent),
                          "--release", "v2026.10.1"],
                         capture_output=True, text=True, check=True).stdout
    p = json.loads(out)
    assert p["release"] == "v2026.10.1"
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


def test_urllib_fallback_sets_product_user_agent(cfg, monkeypatch):
    import urllib.request
    from tests.conftest import REAL_TELEMETRY_POST
    monkeypatch.setattr(telemetry, "_post", REAL_TELEMETRY_POST)
    monkeypatch.setitem(sys.modules, "companion", None)
    seen = []

    class _R:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(self, req, *a, **k):
        seen.append(req.get_header("User-agent"))
        return _R()
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _open)
    p = telemetry.build_payload("install", data_dir=cfg, version="v1", channel="script")
    assert telemetry.send(p, "https://telemetry.example.test/v1/event") is True
    assert seen == ["llm-systems-manager"]


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


def test_frames_skip_venv_and_site_packages(tmp_path):
    lib, app = {}, {}
    exec(compile("def boom():\n    raise ValueError('x')\n",
                 str(tmp_path / "venv/lib/python3.12/site-packages/pkg/mod.py"), "exec"), lib)
    exec(compile("def outer(f):\n    f()\n", str(tmp_path / "backend/app.py"), "exec"), app)
    try:
        app["outer"](lib["boom"])
    except ValueError as e:
        txt = telemetry.frames_text(e.__traceback__, tmp_path)
    assert txt == "backend/app.py:2:outer"


@pytest.fixture
def sched(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(telemetry, "FIRST_DELAY_S", 0)
    monkeypatch.setattr(telemetry, "TICK_S", 0)
    for k in ("attempt", "sent", "not_before"):
        monkeypatch.setitem(telemetry._hb, k, 0.0)
    monkeypatch.setattr(telemetry, "_spread_s", lambda d: 0)
    monkeypatch.setattr(telemetry, "_channel_cache", {})
    monkeypatch.setattr(telemetry, "_detect_channel", lambda root: "script")
    monkeypatch.setattr(telemetry, "_deliver", lambda p: sent.append(p["event"]) or True)
    monkeypatch.setattr(telemetry, "flush_pending", lambda d, e: sent.append("flush") or True)

    def run():
        sent.clear()
        telemetry.start_scheduler(lambda: True).join(5)
        return list(sent)
    return run


def _stamp(cfg, age_s):
    (cfg / telemetry.HEARTBEAT_STAMP).write_text(telemetry._iso(time.time() - age_s) + "\n")
    (cfg / telemetry.LAST).write_text("{}")


def test_scheduler_sends_stale_heartbeat_at_first_tick(cfg, sched):
    telemetry._settings.manager.telemetry.heartbeat = True
    _stamp(cfg, 25 * 3600)
    assert sched() == ["heartbeat"]


def test_scheduler_skips_recent_heartbeat(cfg, sched):
    telemetry._settings.manager.telemetry.heartbeat = True
    _stamp(cfg, 3600)
    assert sched() == []


def test_deliver_writes_heartbeat_stamp(cfg, monkeypatch):
    monkeypatch.setattr(telemetry, "send", lambda payload, endpoint, timeout=5.0: True)
    assert telemetry._heartbeat_age() == float("inf")
    telemetry._deliver({"event": "heartbeat", "sent_at": telemetry._iso()})
    assert telemetry._heartbeat_age() < 60


def test_send_now_sends_heartbeat_even_when_toggle_off(cfg, monkeypatch):
    telemetry._settings.manager.telemetry.heartbeat = False
    seen = []
    monkeypatch.setattr(telemetry, "_deliver", lambda p: seen.append(p) or True)
    r = telemetry.send_now()
    assert r["ok"] is True and r["event"] == "heartbeat" and r["sent_at"]
    assert seen[0]["event"] == "heartbeat"


def test_send_now_never_raises(cfg, monkeypatch):
    def boom(p):
        raise RuntimeError("x")
    monkeypatch.setattr(telemetry, "_deliver", boom)
    assert telemetry.send_now() == {"ok": False, "error": "RuntimeError"}


def test_scheduler_first_start_paths(cfg, sched):
    (cfg / telemetry.PENDING).write_text("{}")
    assert sched() == ["flush"]
    (cfg / telemetry.PENDING).unlink()
    assert sched() == ["install"]
    telemetry._settings.manager.telemetry.install_report = False
    assert sched() == []


def test_scheduler_prewarms_channel(cfg, sched):
    sched()
    assert str(Path(telemetry._root).resolve()) in telemetry._channel_cache


def test_failed_heartbeat_waits_an_hour_before_the_next_attempt(cfg, monkeypatch):
    telemetry._settings.manager.telemetry.heartbeat = True
    _stamp(cfg, 25 * 3600)
    for k in ("attempt", "sent", "not_before"):
        monkeypatch.setitem(telemetry._hb, k, 0.0)
    monkeypatch.setattr(telemetry, "_detect_channel", lambda root: "script")
    tries = []
    monkeypatch.setattr(telemetry, "_deliver", lambda p: tries.append(p["event"]) and False)
    telemetry._heartbeat_tick()
    telemetry._heartbeat_tick()
    assert tries == ["heartbeat"]
    telemetry._hb["attempt"] -= telemetry.HEARTBEAT_RETRY_S + 1
    telemetry._heartbeat_tick()
    assert tries == ["heartbeat", "heartbeat"]


def test_sent_heartbeat_is_not_repeated_when_the_stamp_write_fails(cfg, monkeypatch):
    telemetry._settings.manager.telemetry.heartbeat = True
    _stamp(cfg, 25 * 3600)
    for k in ("attempt", "sent", "not_before"):
        monkeypatch.setitem(telemetry._hb, k, 0.0)
    monkeypatch.setattr(telemetry, "_detect_channel", lambda root: "script")
    monkeypatch.setattr(telemetry, "send", lambda payload, endpoint, timeout=5.0: True)

    def no_stamp(path, text):
        raise OSError("read-only")
    monkeypatch.setattr(telemetry, "_write_stamp", no_stamp)
    sent = []
    real = telemetry._deliver
    monkeypatch.setattr(telemetry, "_deliver", lambda p: sent.append(p["event"]) or real(p))
    try:
        telemetry._heartbeat_tick()
    except OSError:
        pass  # the stamp write failing is the case under test
    assert sent == ["heartbeat"] and telemetry._heartbeat_age() > telemetry.HEARTBEAT_EVERY_S
    telemetry._hb["attempt"] -= telemetry.HEARTBEAT_RETRY_S + 1
    telemetry._heartbeat_tick()
    assert sent == ["heartbeat"]


def test_heartbeat_spread_is_stable_per_install_and_delays_the_first_tick(cfg, monkeypatch):
    a = telemetry._spread_s(cfg)
    assert 0 <= a < telemetry.HEARTBEAT_SPREAD_S and telemetry._spread_s(cfg) == a
    telemetry._settings.manager.telemetry.heartbeat = True
    _stamp(cfg, 25 * 3600)
    for k in ("attempt", "sent"):
        monkeypatch.setitem(telemetry._hb, k, 0.0)
    monkeypatch.setitem(telemetry._hb, "not_before", time.time() + 600)
    tries = []
    monkeypatch.setattr(telemetry, "_deliver", lambda p: tries.append(p["event"]) or True)
    telemetry._heartbeat_tick()
    assert tries == []
    telemetry._hb["not_before"] = 0.0
    telemetry._heartbeat_tick()
    assert tries == ["heartbeat"]


def test_send_now_refuses_a_second_concurrent_send(cfg, monkeypatch):
    import threading
    gate = threading.Event()
    results = []

    def slow(p):
        gate.wait(5)
        return True
    monkeypatch.setattr(telemetry, "_deliver", slow)
    t = threading.Thread(target=lambda: results.append(telemetry.send_now()))
    t.start()
    for _ in range(100):
        if telemetry._send_lock.locked():
            break
        time.sleep(0.01)
    assert telemetry.send_now() == {"ok": False, "error": "busy"}
    gate.set()
    t.join(5)
    assert results[0]["ok"] is True


def test_append_log_creates_a_private_file(tmp_path):
    telemetry._append_log(tmp_path, "x")
    f = tmp_path / telemetry.LOG
    assert f.read_text().endswith(" x\n")
    assert (f.stat().st_mode & 0o777) == 0o600


def test_install_payload_carries_a_known_source_only(cfg):
    p = telemetry.build_payload("install", data_dir=cfg, version="v1", channel="script", source="git")
    assert p["source"] == "git"
    assert "source" not in telemetry.build_payload("update", data_dir=cfg, version="v1", channel="script", source="weird")
    assert "source" not in telemetry.build_payload("heartbeat", data_dir=cfg, version="v1", channel="script", source="git")


def test_source_of_install_maps_the_probe():
    assert [telemetry.source_of_install(s) for s in ("release-file", "dpkg", "rpm", "git", None)] == \
        ["release", "release", "release", "git", None]


def test_heartbeat_features_are_counts_and_failures_drop_the_field(cfg):
    telemetry.configure(settings=telemetry._settings, data_dir=cfg, version="v1", root=BACKEND.parent.parent,
                        features_fn=lambda: {"benchmark": 2, "rules": 12, "tower": 0})
    hb = telemetry.build_payload("heartbeat", data_dir=cfg, version="v1", channel="deb")
    assert hb["features"] == "benchmark=2,rules=12,tower=0"
    assert "features" not in telemetry.build_payload("install", data_dir=cfg, version="v1", channel="deb")

    def boom():
        raise RuntimeError("x")
    telemetry.configure(settings=telemetry._settings, data_dir=cfg, version="v1", root=BACKEND.parent.parent,
                        features_fn=boom)
    assert "features" not in telemetry.build_payload("heartbeat", data_dir=cfg, version="v1", channel="deb")


def test_cli_payload_source(tmp_path):
    out = subprocess.run([sys.executable, "-I", str(BACKEND / "telemetry.py"), "payload",
                          "--event", "update", "--channel", "script", "--data-dir", str(tmp_path),
                          "--version", "v9", "--root", str(BACKEND.parent.parent), "--source", "local"],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out)["source"] == "local"


def test_install_source_falls_back_to_the_tree(cfg, tmp_path, monkeypatch):
    import companion
    monkeypatch.setattr(companion, "_installed_release", lambda: {"source": "dpkg"})
    assert telemetry._install_source() == "release"
    monkeypatch.setattr(companion, "_installed_release", lambda: {"source": None})
    root = tmp_path / "tree"
    root.mkdir()
    monkeypatch.setattr(telemetry, "_root", root)
    assert telemetry._install_source() == "local"
    (root / ".git").mkdir()
    assert telemetry._install_source() == "git"


def test_checkin_payload_has_only_identity_version_os_channel(cfg):
    telemetry.configure(settings=telemetry._settings, data_dir=cfg, version="v1",
                        root=BACKEND.parent.parent, release_fn=lambda: "1.2.3")
    p = telemetry.build_payload("checkin", data_dir=cfg, version="v1", channel="deb", mode="3")
    assert set(p) == {"schema", "event", "install_id", "installed_at", "sent_at", "version",
                      "release", "channel", "os", "os_version"}
    assert p["event"] == "checkin" and "mode" not in p and p["release"] == "1.2.3"
    for k in ("arch", "ram_gb", "gpu_vendor", "agent_count"):
        assert k not in p


def test_daily_event_prefers_heartbeat_then_checkin(cfg):
    t = telemetry._settings.manager.telemetry
    for hb, ci, want in ((True, True, "heartbeat"), (True, False, "heartbeat"),
                         (False, True, "checkin"), (False, False, None)):
        t.heartbeat, t.checkin = hb, ci
        assert telemetry._daily_event() == want


def test_tick_sends_checkin_when_only_checkin_is_on(cfg, monkeypatch):
    telemetry._settings.manager.telemetry.checkin = True
    sent = []
    monkeypatch.setitem(telemetry._hb, "not_before", 0.0)
    monkeypatch.setitem(telemetry._hb, "attempt", 0.0)
    monkeypatch.setitem(telemetry._hb, "sent", 0.0)
    monkeypatch.setattr(telemetry, "_deliver", lambda p: sent.append(p["event"]) or True)
    telemetry._heartbeat_tick()
    assert sent == ["checkin"]


def test_deliver_writes_stamp_for_checkin(cfg, monkeypatch):
    monkeypatch.setattr(telemetry, "send", lambda payload, endpoint, timeout=5.0: True)
    telemetry._deliver({"event": "checkin", "sent_at": telemetry._iso()})
    assert telemetry._heartbeat_age() < 60


def test_do_not_track_disables_every_kind_and_send_now(cfg, sched, monkeypatch):
    t = telemetry._settings.manager.telemetry
    t.install_report = t.crash_reports = t.checkin = t.heartbeat = True
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    for k in ("install_report", "crash_reports", "checkin", "heartbeat"):
        assert telemetry.enabled(k) is False
    assert telemetry.send_now()["error"] == "do_not_track"
    telemetry.write_pending(cfg, {"event": "install", "install_id": "x"})
    assert sched() == []
    assert (cfg / telemetry.PENDING).is_file()
    assert telemetry.status()["do_not_track"] is True


@pytest.mark.parametrize("val,expected", [("", False), ("0", False), ("false", False),
                                          (" FALSE ", False), ("1", True), ("true", True),
                                          ("yes", True)])
def test_do_not_track_zero_or_false_is_not_set(monkeypatch, val, expected):
    monkeypatch.setenv("DO_NOT_TRACK", val)
    assert telemetry.do_not_track() is expected
