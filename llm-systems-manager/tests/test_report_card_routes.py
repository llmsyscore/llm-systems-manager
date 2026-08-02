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
                        lambda aid, prov=None: {"watts": 200.0,
                                                "source": "psu", "gpus": []})
    monkeypatch.setattr(rc, "bench_base_url",
                        lambda p, a, probe=None: ("http://x/llama/openai", {}))
    monkeypatch.setattr(rc, "_agent_call",
                        lambda *a, **k: (True, None), raising=False)
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


def test_public_card_allowlists_fields(client):
    # Fails closed: a field added to the card later is not published until
    # it is explicitly allowlisted.
    card = {"ts": 1, "agent_id": "a" * 32, "provider": "llama",
            "mode": "standard", "preset_version": "preset_v1",
            "eligible": True, "result": {"gen_tps": 1.0},
            "internal_hostname": "secret-box", "token": "shh"}
    pub = rc._public_card(card)
    assert "agent_id" not in pub
    assert "internal_hostname" not in pub and "token" not in pub
    assert set(pub) == {"ts", "provider", "mode", "preset_version",
                        "eligible", "result"}


def test_confirm_vllm_does_not_bypass_a_load_failure(client, monkeypatch):
    # confirm_vllm answers needs_confirm only; it must not let a model that
    # failed to load through to the bench as an eligible card.
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "load_failed", "model": "m",
                                         "reference": "m", "is_reference": True,
                                         "error": "llama refused to load m"})
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small", "confirm_vllm": True})
    events = _drain(client, r.get_json()["job_id"])
    errs = [e for e in events if e.get("event") == "error"]
    assert errs and "refused to load" in errs[0]["error"]
    assert rc.latest_card(rc._conn_factory(), "a" * 32, "llama") is None


def test_row_mapping_survives_column_reordering():
    # _row_to_card maps by name from _COLS rather than fixed tuple indices.
    conn = sqlite3.connect(":memory:")
    rc.init_table(conn)
    rc.insert_card(conn, {"ts": 7, "agent_id": "b" * 32, "provider": "vllm",
                          "mode": "custom", "preset_version": "preset_v1",
                          "eligible": False, "result": {"model": "m"}})
    got = rc.latest_card(conn, "b" * 32, "vllm")
    assert got["ts"] == 7 and got["provider"] == "vllm"
    assert got["mode"] == "custom" and got["eligible"] is False
    assert got["result"]["model"] == "m"


# ── #492: post-run unload + delete offer ─────────────────────────────

def _unload_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_agent_call",
                        lambda agent, method, path, timeout=15, **kw:
                            (calls.append((method, path, kw.get("json"))),
                             (True, None))[-1])
    return calls


def test_standard_run_unloads_the_model_after_the_bench(client, monkeypatch):
    calls = _unload_calls(monkeypatch)
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    events = _drain(client, r.get_json()["job_id"])
    assert ("POST", "/llama/unload", {"model": "m"}) in calls
    phases = [e.get("phase") for e in events if e.get("event") == "progress"]
    assert "unload" in phases


def test_custom_run_does_not_unload(client, monkeypatch):
    calls = _unload_calls(monkeypatch)
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "lms", "mode": "custom",
        "model": "my-model"})
    _drain(client, r.get_json()["job_id"])
    assert not any(p.endswith("/unload") for _m, p, _b in calls)


def test_vllm_run_never_unloads(client, monkeypatch):
    calls = _unload_calls(monkeypatch)
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "vllm", "mode": "standard",
        "model_key": "small"})
    _drain(client, r.get_json()["job_id"])
    assert not any(p.endswith("/unload") for _m, p, _b in calls)


def test_bench_failure_still_unloads(client, monkeypatch):
    calls = _unload_calls(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("provider streamed no tokens")
    monkeypatch.setattr(rc, "run_bench", boom)
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    events = _drain(client, r.get_json()["job_id"])
    assert any(e.get("event") == "error" for e in events)
    assert any(p == "/llama/unload" for _m, p, _b in calls)


def test_done_event_offers_deletion_after_a_provisioned_download(client, monkeypatch):
    src = rc.preset_source("small", "llama")
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "needs_download",
                                         "model": src["model_id"],
                                         "reference": src["model_id"],
                                         "is_reference": True, "source": src})
    monkeypatch.setattr(rc, "provision_model",
                        lambda agent, prov, s, emit, should_cancel=None:
                            {"status": "ready", "model": s["model_id"]})
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small", "confirm_download": True})
    events = _drain(client, r.get_json()["job_id"])
    done = [e for e in events if e.get("event") == "done"]
    assert done and done[0]["cleanup"] == {"downloaded": True,
                                           "deletable": True,
                                           "model_key": "small"}


def test_lms_download_is_flagged_but_not_deletable(client, monkeypatch):
    src = rc.preset_source("small", "lms")
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "needs_download",
                                         "model": src["model_id"],
                                         "reference": src["model_id"],
                                         "is_reference": True, "source": src})
    monkeypatch.setattr(rc, "provision_model",
                        lambda agent, prov, s, emit, should_cancel=None:
                            {"status": "ready", "model": "qwen2.5-1.5b-instruct"})
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "lms", "mode": "standard",
        "model_key": "small", "confirm_download": True})
    events = _drain(client, r.get_json()["job_id"])
    done = [e for e in events if e.get("event") == "done"]
    assert done and done[0]["cleanup"] == {"downloaded": True,
                                           "deletable": False,
                                           "model_key": "small"}


def test_preexisting_model_offers_no_deletion(client):
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    events = _drain(client, r.get_json()["job_id"])
    done = [e for e in events if e.get("event") == "done"]
    assert done and done[0]["cleanup"]["downloaded"] is False


def test_delete_model_calls_the_agent_with_cache_purge(client, monkeypatch):
    seen = []
    monkeypatch.setattr(rc, "_agent_json",
                        lambda agent, method, path, timeout=15, **kw:
                            (seen.append((method, path)),
                             {"ok": True, "deleted_files": ["a.gguf"],
                              "cache_error": None})[-1])
    r = client.post("/api/reportcard/delete-model", json={
        "agent": "a" * 32, "provider": "llama", "model_key": "small"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    method, path = seen[0]
    assert method == "DELETE"
    assert path.startswith("/llama/config/")
    assert "delete_cache=true" in path
    assert rc.preset_source("small", "llama")["model_id"] in path


def test_delete_model_rejects_non_llama_providers(client):
    r = client.post("/api/reportcard/delete-model", json={
        "agent": "a" * 32, "provider": "lms", "model_key": "small"})
    assert r.status_code == 400


def test_delete_model_rejects_unknown_model_key(client):
    r = client.post("/api/reportcard/delete-model", json={
        "agent": "a" * 32, "provider": "llama", "model_key": "huge"})
    assert r.status_code == 400


def test_delete_model_surfaces_agent_failure(client, monkeypatch):
    monkeypatch.setattr(rc, "_agent_json",
                        lambda *a, **k: {"ok": False, "error": "Permission denied"})
    r = client.post("/api/reportcard/delete-model", json={
        "agent": "a" * 32, "provider": "llama", "model_key": "small"})
    assert r.status_code == 502
    assert "Permission denied" in r.get_json()["error"]
