"""#985: manager log tail/stream + alarm-engine log proxies for Admin › System Health."""
from __future__ import annotations

import json
import time

import manager_mod


def _admin_client(monkeypatch=None):
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def test_routes_registered():
    rules = {str(r) for r in manager_mod.app.url_map.iter_rules()}
    for path in ["/api/admin/log/tail", "/api/admin/log/stream",
                 "/api/admin/alarm-engine/log/tail", "/api/admin/alarm-engine/log/stream"]:
        assert path in rules, f"missing route {path}"


def test_routes_are_admin_gated():
    c = manager_mod.app.test_client()
    for path in ["/api/admin/log/tail", "/api/admin/log/stream",
                 "/api/admin/alarm-engine/log/tail", "/api/admin/alarm-engine/log/stream"]:
        assert c.get(path).status_code in (401, 403, 302), path


def test_manager_log_tail_returns_last_lines(monkeypatch, tmp_path):
    log = tmp_path / "m.log"
    log.write_text("".join(f"2026-09-17 01:00:{i % 60:02d} [INFO] line {i}\n" for i in range(3000)), encoding="utf-8")
    monkeypatch.setattr(manager_mod, "LOG_FILE", str(log))
    body = _admin_client(monkeypatch).get("/api/admin/log/tail").get_json()
    assert body["ok"] is True and body["lines"][-1].endswith("line 2999")
    assert 0 < len(body["lines"]) < 3000 and all(not ln.startswith("line") for ln in body["lines"])


def test_manager_log_tail_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_mod, "LOG_FILE", str(tmp_path / "absent.log"))
    body = _admin_client(monkeypatch).get("/api/admin/log/tail").get_json()
    assert body == {"ok": True, "lines": [], "note": "log file does not exist yet"}


def test_tail_log_sse_yields_appended_lines_and_keepalive(tmp_path):
    log = tmp_path / "m.log"
    log.write_text("old line\n", encoding="utf-8")
    gen = manager_mod._tail_log_sse(str(log), keepalive_s=0.0, max_lifetime_s=5.0)
    with open(log, "a", encoding="utf-8") as f:
        f.write("new line\n")
    first = next(gen)
    assert json.loads(first[len("data: "):].strip()) == {"line": "new line"}
    second = next(gen)
    assert json.loads(second[len("data: "):].strip()) == {"keepalive": True}
    gen.close()


def test_tail_log_sse_respects_lifetime_cap(tmp_path):
    log = tmp_path / "m.log"
    log.write_text("", encoding="utf-8")
    t0 = time.time()
    assert list(manager_mod._tail_log_sse(str(log), keepalive_s=60.0, max_lifetime_s=0.0)) == []
    assert time.time() - t0 < 1.0


def test_manager_log_stream_holds_and_releases_a_pool_slot(monkeypatch, tmp_path):
    log = tmp_path / "m.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(manager_mod, "LOG_FILE", str(log))
    monkeypatch.setattr(manager_mod.settings.manager, "stream_max_lifetime_s", 0.05, raising=False)
    pool = manager_mod.stream_pool.POOL
    before = pool._active
    r = _admin_client(monkeypatch).get("/api/admin/log/stream")
    assert r.status_code == 200 and r.mimetype == "text/event-stream"
    r.get_data()
    r.close()
    assert pool._active == before


def test_manager_log_stream_refuses_at_capacity(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_mod, "LOG_FILE", str(tmp_path / "m.log"))
    monkeypatch.setattr(manager_mod.stream_pool.POOL, "try_acquire", lambda: False)
    r = _admin_client(monkeypatch).get("/api/admin/log/stream")
    assert r.status_code == 503


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self.text = text
        self.headers = {"Content-Type": "text/event-stream"}
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def close(self):
        self.closed = True


def test_ae_log_tail_proxies_the_engine_payload(monkeypatch):
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return _Resp(200, {"ok": True, "lines": ["a", "b"]})
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae:8081/")
    monkeypatch.setattr(manager_mod._ae_session, "get", fake_get)
    r = _admin_client(monkeypatch).get("/api/admin/alarm-engine/log/tail")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "lines": ["a", "b"]}
    assert seen["url"] == "http://ae:8081/api/alarm/admin/log/tail"


def test_ae_log_tail_maps_auth_and_transport_failures(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae:8081")
    monkeypatch.setattr(manager_mod._ae_session, "get", lambda url, **kw: _Resp(401))
    r = _admin_client(monkeypatch).get("/api/admin/alarm-engine/log/tail")
    assert r.status_code == 502
    body = r.get_json()
    assert body["ok"] is False and body["failure"]["kind"] == "unauthorized"
    assert "management_token" in body["error"]

    def boom(url, **kw):
        raise OSError("no route")
    monkeypatch.setattr(manager_mod._ae_session, "get", boom)
    body = _admin_client(monkeypatch).get("/api/admin/alarm-engine/log/tail").get_json()
    assert body["ok"] is False and body["failure"]["kind"] == "unreachable"


def test_ae_log_tail_without_engine_url(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "")
    r = _admin_client(monkeypatch).get("/api/admin/alarm-engine/log/tail")
    assert r.status_code == 500


def test_ae_log_stream_releases_slot_on_upstream_rejection(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae:8081")
    resp = _Resp(404)
    monkeypatch.setattr(manager_mod._ae_session, "get", lambda url, **kw: resp)
    pool = manager_mod.stream_pool.POOL
    before = pool._active
    r = _admin_client(monkeypatch).get("/api/admin/alarm-engine/log/stream")
    assert r.status_code == 502 and r.get_json()["failure"]["kind"] == "unsupported"
    assert resp.closed and pool._active == before


def test_tail_log_sse_reopens_after_rotation(tmp_path):
    import os
    log = tmp_path / "m.log"
    log.write_text("before\n", encoding="utf-8")
    gen = manager_mod._tail_log_sse(str(log), keepalive_s=60.0, max_lifetime_s=10.0)
    with open(log, "a", encoding="utf-8") as f:
        f.write("pre-rotate\n")
    assert json.loads(next(gen)[len("data: "):].strip()) == {"line": "pre-rotate"}
    old_fd_count = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    os.rename(log, tmp_path / "m.log.1")
    log.write_text("after-rotate\n", encoding="utf-8")
    assert json.loads(next(gen)[len("data: "):].strip()) == {"line": "after-rotate"}
    if old_fd_count is not None:
        assert len(os.listdir("/proc/self/fd")) <= old_fd_count
    gen.close()
