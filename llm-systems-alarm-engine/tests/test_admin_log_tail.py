"""#984: management-token guarded tail of the alarm engine log for Tower's log_tail tool."""
from __future__ import annotations

from fastapi.testclient import TestClient

from config.unified_config import settings
from backend import alarm_engine as ae

PATH = "/api/alarm/admin/log/tail"


def _client():
    return TestClient(ae.app, raise_server_exceptions=False)


def _mgmt(monkeypatch, token="mgmt-secret"):
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", "", raising=False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", token, raising=False)
    return {"Authorization": f"Bearer {token}"}


def test_log_tail_requires_the_management_token(monkeypatch, tmp_path):
    headers = _mgmt(monkeypatch)
    monkeypatch.setattr(ae, "LOG_FILE", str(tmp_path / "ae.log"))
    assert _client().get(PATH).status_code == 401
    assert _client().get(PATH, headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = _client().get(PATH, headers=headers)
    assert r.status_code == 200 and r.json() == {"ok": True, "lines": [], "note": "log file does not exist yet"}


def test_log_tail_returns_the_last_lines_of_a_large_file(monkeypatch, tmp_path):
    headers = _mgmt(monkeypatch)
    log = tmp_path / "ae.log"
    log.write_text("".join(f"2026-09-14 21:00:{i % 60:02d} [INFO] line {i}\n" for i in range(3000)), encoding="utf-8")
    monkeypatch.setattr(ae, "LOG_FILE", str(log))
    body = _client().get(PATH, headers=headers).json()
    assert body["ok"] is True and body["lines"][-1].endswith("line 2999")
    assert 0 < len(body["lines"]) < 3000 and all(not ln.startswith("line") for ln in body["lines"])
