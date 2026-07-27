"""#468: reportcard route contract (driver + readiness mocked)."""
from __future__ import annotations

import json
import sqlite3

import pytest

import report_card as rc

AGENT = {"agent_id": "a" * 32, "registered_from": "203.0.113.7",
         "hostname": "h", "bind_url": "http://h:9899", "token": "tok"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from flask import Flask
    app = Flask(__name__)
    conn = sqlite3.connect(tmp_path / "t.db", check_same_thread=False)
    rc.init_table(conn)
    monkeypatch.setattr(rc, "_conn_factory", lambda: conn, raising=False)
    monkeypatch.setattr(rc, "_agent_for", lambda aid: dict(AGENT), raising=False)
    monkeypatch.setattr(rc, "prod_deps", lambda agent: {}, raising=False)
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "ready", "model": "m",
                                         "reference": "m",
                                         "is_reference": True})
    monkeypatch.setattr(rc, "run_bench",
                        lambda *a, **k: {"ttft_s": 0.5, "prefill_tps": 1000.0,
                                         "gen_tps": 40.0, "reps": []})
    monkeypatch.setattr(rc, "_snapshot_power",
                        lambda aid, prov=None: {"psu_w": 200.0, "gpus": []})
    rc.register_routes(app, db_path=str(tmp_path / "t.db"))
    return app.test_client()


def _drain(client, job_id):
    body = client.get(f"/api/reportcard/stream/{job_id}").get_data(as_text=True)
    return [json.loads(ln[6:]) for ln in body.splitlines()
            if ln.startswith("data: ")]


def test_run_returns_job_and_stream_completes(client):
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    assert r.status_code == 200
    events = _drain(client, r.get_json()["job_id"])
    done = [e for e in events if e.get("event") == "done"]
    assert done, events
    card = done[0]["card"]
    assert card["result"]["gen_tps"] == 40.0
    assert card["eligible"] is True
    assert card["result"]["tokens_per_joule"] == pytest.approx(0.2)


def test_done_event_and_storage_never_leak_identity(client):
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    events = _drain(client, r.get_json()["job_id"])
    payload = json.dumps([e for e in events if e.get("event") == "done"])
    assert "a" * 32 not in payload
    assert "203.0.113.7" not in payload and "h" not in json.loads(payload)[0]["card"]


def test_custom_mode_never_eligible(client):
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "custom",
        "model": "my.gguf"})
    _drain(client, r.get_json()["job_id"])
    row = rc.latest_card(rc._conn_factory(), "a" * 32, "llama")
    assert row is not None and row["eligible"] is False


def test_non_reference_model_is_not_eligible(client, monkeypatch):
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "ready", "model": "served",
                                         "reference": "Qwen/Ref",
                                         "is_reference": False})
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "vllm", "mode": "standard",
        "model_key": "small"})
    _drain(client, r.get_json()["job_id"])
    assert rc.latest_card(rc._conn_factory(), "a" * 32, "vllm")["eligible"] is False


def test_run_rejects_unknown_provider(client):
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "nope", "mode": "standard"})
    assert r.status_code == 400


def test_run_rejects_unknown_mode_and_missing_agent(client):
    assert client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "wild"}).status_code == 400
    assert client.post("/api/reportcard/run", json={
        "provider": "llama", "mode": "standard"}).status_code == 400


def test_custom_mode_requires_a_model(client):
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "custom"})
    assert r.status_code == 400


def test_vllm_needs_confirm_short_circuits_without_a_job(client, monkeypatch):
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "needs_confirm",
                                         "model": "served",
                                         "reference": "Qwen/Ref",
                                         "is_reference": False})
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "vllm", "mode": "standard",
        "model_key": "small"})
    body = r.get_json()
    assert r.status_code == 200 and body["status"] == "needs_confirm"
    assert body["model"] == "served" and "job_id" not in body


def test_vllm_proceeds_once_confirmed(client, monkeypatch):
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "needs_confirm",
                                         "model": "served",
                                         "reference": "Qwen/Ref",
                                         "is_reference": False})
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "vllm", "mode": "standard",
        "model_key": "small", "confirm_vllm": True})
    assert "job_id" in r.get_json()


def test_bench_failure_emits_error_and_stores_nothing(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider streamed no tokens")
    monkeypatch.setattr(rc, "run_bench", boom)
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    events = _drain(client, r.get_json()["job_id"])
    assert any(e.get("event") == "error" for e in events)
    assert rc.latest_card(rc._conn_factory(), "a" * 32, "llama") is None


def test_not_ready_status_aborts_before_benching(client, monkeypatch):
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "load_failed",
                                         "model": "m",
                                         "reference": "m", "is_reference": True,
                                         "error": "llama refused to load m"})
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    events = _drain(client, r.get_json()["job_id"])
    assert any(e.get("event") == "error" for e in events)


def test_stream_unknown_job_is_404(client):
    assert client.get("/api/reportcard/stream/nope").status_code == 404


def test_latest_and_history_endpoints(client):
    conn = rc._conn_factory()
    rc.insert_card(conn, {"ts": 1, "agent_id": "a" * 32, "provider": "llama",
                          "mode": "standard", "preset_version": "preset_v1",
                          "eligible": True,
                          "result": {"gen_tps": 40.0, "model": "m"}})
    latest = client.get("/api/reportcard/latest?agent=" + "a" * 32 +
                        "&provider=llama").get_json()
    assert latest["card"]["result"]["gen_tps"] == 40.0
    h = client.get("/api/reportcard/history?agent=" + "a" * 32 +
                   "&provider=llama&model=m").get_json()
    assert len(h["cards"]) == 1


def test_latest_returns_null_card_when_absent(client):
    r = client.get("/api/reportcard/latest?agent=" + "b" * 32 + "&provider=llama")
    assert r.status_code == 200 and r.get_json()["card"] is None


def test_latest_requires_params(client):
    assert client.get("/api/reportcard/latest?provider=llama").status_code == 400


def test_preset_endpoint_lists_reference_models(client):
    body = client.get("/api/reportcard/preset").get_json()
    assert body["preset_version"] == rc.PRESET_VERSION
    assert [m["key"] for m in body["models"]] == ["small", "mid"]


def test_confirmed_vllm_run_still_fails_when_unavailable(client, monkeypatch):
    # confirm_vllm only bypasses needs_confirm; unavailable stays terminal.
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "unavailable", "model": None,
                                         "reference": "Qwen/Ref",
                                         "is_reference": False,
                                         "error": "vLLM is not serving a model"})
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "vllm", "mode": "standard",
        "model_key": "small", "confirm_vllm": True})
    events = _drain(client, r.get_json()["job_id"])
    errs = [e for e in events if e.get("event") == "error"]
    assert errs and "not serving" in errs[0]["error"]
    assert rc.latest_card(rc._conn_factory(), "a" * 32, "vllm") is None


def test_price_zero_is_honored(client):
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small", "price_kwh": 0})
    events = _drain(client, r.get_json()["job_id"])
    card = [e for e in events if e.get("event") == "done"][0]["card"]
    assert card["result"]["usd_per_mtok"] == 0.0


def test_needs_download_short_circuits_with_size_and_restart_warning(client, monkeypatch):
    src = rc.preset_source("small", "llama")
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "needs_download",
                                         "model": src["model_id"],
                                         "reference": src["model_id"],
                                         "is_reference": True, "source": src})
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    b = r.get_json()
    assert b["status"] == "needs_download" and "job_id" not in b
    assert b["repo"] == src["repo"] and b["quant"] == "Q4_K_M"
    assert b["approx_gb"] > 0 and b["restarts"] is True


def test_confirm_download_provisions_then_benches(client, monkeypatch):
    src = rc.preset_source("small", "llama")
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "needs_download",
                                         "model": src["model_id"],
                                         "reference": src["model_id"],
                                         "is_reference": True, "source": src})
    seen = []
    monkeypatch.setattr(rc, "provision_model",
                        lambda agent, prov, s, emit, should_cancel=None:
                            (seen.append(prov),
                             emit({"phase": "download"}),
                             {"status": "ready", "model": s["model_id"]})[-1])
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small", "confirm_download": True})
    events = _drain(client, r.get_json()["job_id"])
    assert seen == ["llama"]
    assert any(e.get("phase") == "download" for e in events)
    done = [e for e in events if e.get("event") == "done"]
    assert done and done[0]["card"]["eligible"] is True


def test_needs_download_without_confirmation_errors(client, monkeypatch):
    src = rc.preset_source("small", "llama")
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "needs_download",
                                         "model": src["model_id"],
                                         "reference": src["model_id"],
                                         "is_reference": True, "source": src})
    # confirm_vllm skips the precheck but must not silently provision.
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small", "confirm_vllm": True})
    events = _drain(client, r.get_json()["job_id"])
    assert any(e.get("event") == "error" for e in events)


def test_progress_events_carry_elapsed_seconds(client):
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    events = _drain(client, r.get_json()["job_id"])
    progress = [e for e in events if e.get("event") == "progress"]
    assert progress and all("elapsed_s" in e for e in progress)
    assert any(e.get("phase") == "resolving" for e in progress)


def test_cancel_unknown_job_is_404(client):
    assert client.post("/api/reportcard/cancel/nope").status_code == 404


def test_cancel_stops_the_run_and_stores_nothing(client, monkeypatch):
    import threading
    gate = threading.Event()

    def slow_bench(*a, **k):
        cb = k.get("progress_cb")
        if cb:
            cb({"phase": "warmup"})
        gate.wait(5)
        if cb:
            cb({"phase": "rep", "n": 1, "of": 3})   # raises _Cancelled
        return {"ttft_s": 0.5, "prefill_tps": 1.0, "gen_tps": 1.0, "reps": []}

    monkeypatch.setattr(rc, "run_bench", slow_bench)
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    job = r.get_json()["job_id"]
    assert client.post(f"/api/reportcard/cancel/{job}").get_json()["ok"] is True
    gate.set()
    events = _drain(client, job)
    assert any(e.get("event") == "cancelled" for e in events)
    assert rc.latest_card(rc._conn_factory(), "a" * 32, "llama") is None
