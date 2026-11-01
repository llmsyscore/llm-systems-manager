"""#468: reportcard route contract (driver + readiness mocked)."""
from __future__ import annotations

import json
import sqlite3

import pytest

import report_card as rc

_REAL_RUN_BENCH = rc.run_bench

AGENT = {"agent_id": "a" * 32, "registered_from": "203.0.113.7",
         "hostname": "h", "bind_url": "http://h:9899", "token": "tok"}


@pytest.fixture(autouse=True)
def _clear_jobs():
    """Job registry is module state; a leaked active job would refuse the next run."""
    rc._JOBS.clear()
    rc.configure_queue(None)
    yield
    rc._JOBS.clear()
    rc.configure_queue(None)


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
    monkeypatch.setattr(rc, "_agent_json", lambda *a, **k: None)
    rc.register_routes(app, db_path=str(tmp_path / "t.db"))
    return app.test_client()


def _drain(client, job_id):
    body = client.get(f"/api/reportcard/stream/{job_id}").get_data(as_text=True)
    return [json.loads(ln[6:]) for ln in body.splitlines()
            if ln.startswith("data: ")]


def _streamed_bench(base_url, model, http_post, progress_cb=None, **_):
    clock = {"t": 0.0}

    def post(url, payload):
        for delay, tok in ((0.5, "a"), (0.1, "b"), (0.1, "c")):
            clock["t"] += delay
            yield "data: " + json.dumps({"choices": [{"delta": {"content": tok}}], "usage": None})
        yield "data: [DONE]"
    return _REAL_RUN_BENCH(base_url, model, post, now=lambda: clock["t"], progress_cb=progress_cb)


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


def test_stored_card_keeps_raw_reps_and_spread(client, monkeypatch):
    monkeypatch.setattr(rc, "run_bench", _streamed_bench)
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    _drain(client, r.get_json()["job_id"])
    card = rc.latest_card_for_model(rc._conn_factory(), "a" * 32, "llama", "m")
    reps = card["result"]["reps"]
    assert len(reps) == rc.REPS
    assert all({"prompt_tokens", "gen_tokens", "ttft_s", "gen_duration_s"} <= set(rep) for rep in reps)
    assert card["result"]["spread"]["gen_tps"]["n"] == rc.REPS


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


def test_first_drain_sees_exactly_one_done(client):
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    events = _drain(client, r.get_json()["job_id"])
    assert len([e for e in events if e.get("event") == "done"]) == 1


def test_stream_reemits_terminal_after_drain(client):
    # #778: a reconnect after the terminal event was drained re-emits it
    # instead of ending as an empty 200 (EventSource reconnect loop).
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    job_id = r.get_json()["job_id"]
    first = _drain(client, job_id)
    assert [e for e in first if e.get("event") == "done"], first
    second = _drain(client, job_id)
    done = [e for e in second if e.get("event") == "done"]
    assert len(done) == 1
    assert done[0]["card"]["result"]["gen_tps"] == 40.0


def test_stream_reemits_error_terminal_after_drain(client, monkeypatch):
    monkeypatch.setattr(rc, "run_bench",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    job_id = r.get_json()["job_id"]
    assert any(e.get("event") == "error" for e in _drain(client, job_id))
    second = _drain(client, job_id)
    errs = [e for e in second if e.get("event") == "error"]
    assert len(errs) == 1 and "boom" in errs[0]["error"]


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


def test_lms_download_is_deletable_too(client, monkeypatch):
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
                                           "deletable": True,
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


def test_delete_model_rejects_vllm(client):
    r = client.post("/api/reportcard/delete-model", json={
        "agent": "a" * 32, "provider": "vllm", "model_key": "small"})
    assert r.status_code == 400


def test_delete_model_resolves_the_lms_virtual_key(client, monkeypatch):
    seen = []

    def fake(agent, method, path, timeout=15, **kw):
        seen.append((method, path, kw.get("json")))
        if path == "/lms/models":
            return {"data": [{"id": "google/gemma-3-1b"},
                             {"id": "qwen2.5-1.5b-instruct"}]}
        return {"ok": True, "deleted_files": ["qwen2.5-1.5b-instruct-q4_k_m.gguf"],
                "freed_bytes": 1}
    monkeypatch.setattr(rc, "_agent_json", fake)
    r = client.post("/api/reportcard/delete-model", json={
        "agent": "a" * 32, "provider": "lms", "model_key": "small"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert ("POST", "/lms/delete", {"model": "qwen2.5-1.5b-instruct"}) in seen


def test_delete_model_lms_404s_when_the_key_is_absent(client, monkeypatch):
    monkeypatch.setattr(rc, "_agent_json",
                        lambda agent, method, path, timeout=15, **kw:
                            {"data": [{"id": "google/gemma-3-1b"}]})
    r = client.post("/api/reportcard/delete-model", json={
        "agent": "a" * 32, "provider": "lms", "model_key": "small"})
    assert r.status_code == 404


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


# ── custom-mode model picker (#490 follow-up) ────────────────────────

def test_models_endpoint_lists_the_hosts_provider_models(client, monkeypatch):
    monkeypatch.setattr(rc, "_agent_json",
                        lambda agent, method, path, timeout=15, **kw:
                            {"data": [{"id": "m1"}, {"id": "m2"}]}
                            if path == "/llama/models" else None)
    r = client.get("/api/reportcard/models?agent=" + "a" * 32
                   + "&provider=llama")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "models": ["m1", "m2"]}


def test_models_endpoint_requires_agent_and_known_provider(client):
    assert client.get("/api/reportcard/models?provider=llama").status_code == 400
    assert client.get("/api/reportcard/models?agent=x&provider=nope").status_code == 400


def test_models_endpoint_empty_when_agent_unreachable(client, monkeypatch):
    monkeypatch.setattr(rc, "_agent_json", lambda *a, **k: None)
    r = client.get("/api/reportcard/models?agent=" + "a" * 32
                   + "&provider=lms")
    assert r.status_code == 200 and r.get_json()["models"] == []


# ── /api/reportcard/recent + DELETE /api/reportcard/history (#769) ──

def _seed_cards(n=3):
    for i in range(n):
        rc.insert_card(rc._conn_factory(), {
            "ts": 1756550000 + i, "agent_id": f"agent{i}", "provider": "llama",
            "mode": "standard", "preset_version": "1", "eligible": True,
            "result": {"model": f"m{i}", "gen_tps": 40.0 + i,
                       "avg_watts": 200.0, "usd_per_mtok": 0.1,
                       "gpu_config": "GPU", "reps": ["secret"]}})


def test_recent_orders_newest_first_and_slims_result(client):
    _seed_cards()
    r = client.get("/api/reportcard/recent")
    assert r.status_code == 200
    cards = r.get_json()["cards"]
    assert [c["result"]["model"] for c in cards] == ["m2", "m1", "m0"]
    assert cards[0]["agent_id"] == "agent2"
    # result carries only the tile/ledger allowlist — no reps or extra keys.
    assert set(cards[0]["result"]) == {"model", "gen_tps", "avg_watts",
                                       "usd_per_mtok", "gpu_config"}


def test_recent_limit_clamped_and_default_on_garbage(client):
    _seed_cards(3)
    assert len(client.get("/api/reportcard/recent?limit=2").get_json()["cards"]) == 2
    assert len(client.get("/api/reportcard/recent?limit=abc").get_json()["cards"]) == 3
    # Negative values clamp to the floor of 1 rather than erroring.
    assert len(client.get("/api/reportcard/recent?limit=-5").get_json()["cards"]) == 1


def test_history_clear_deletes_all_cards(client):
    _seed_cards()
    r = client.delete("/api/reportcard/history")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.get("/api/reportcard/recent").get_json()["cards"] == []


# ── one report card per host (#887) ──────────────────────────────────

@pytest.fixture
def stalled(monkeypatch):
    """Jobs that never finish, so the agent stays busy for the next request."""
    monkeypatch.setattr(rc, "_run_job", lambda job_id, req: None)


def test_second_run_on_a_busy_agent_is_refused(client, stalled):
    first = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    assert first.status_code == 200
    second = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    assert second.status_code == 409
    body = second.get_json()
    assert body["ok"] is False
    # The client's gate-refusal matcher keys on this phrasing, so it re-queues.
    assert "in progress" in body["error"].lower()
    assert rc.active_agents() == ["a" * 32]


def test_a_second_host_still_runs_while_the_first_is_busy(client, stalled):
    client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    other = client.post("/api/reportcard/run", json={
        "agent": "b" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    assert other.status_code == 200 and other.get_json()["job_id"]


def test_the_agent_is_free_again_once_its_run_finishes(client):
    r = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    _drain(client, r.get_json()["job_id"])
    assert rc.agent_busy("a" * 32) is False
    again = client.post("/api/reportcard/run", json={
        "agent": "a" * 32, "provider": "llama", "mode": "standard",
        "model_key": "small"})
    assert again.status_code == 200


def test_two_simultaneous_starts_leave_one_job(client, stalled):
    import threading
    codes = []
    lock = threading.Lock()

    def go():
        r = client.post("/api/reportcard/run", json={
            "agent": "a" * 32, "provider": "llama", "mode": "standard",
            "model_key": "small"})
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200, 409, 409, 409]
    assert len([j for j in rc._JOBS.values() if not j["done"]]) == 1


# ── queued behind a busy host (#1136) ────────────────────────────────

RUN = {"agent": "a" * 32, "provider": "llama", "mode": "standard", "model_key": "small"}


def _queued_hook(seen):
    from flask import jsonify

    def hook(agent_id, provider, req):
        seen.append((agent_id, provider, req))
        return jsonify({"ok": True, "queued": True, "job_id": "q1", "position": 1,
                        "wait_for": "Benchmark on h"}), 202
    return hook


def test_queue_hook_answers_the_run_without_starting_the_runner(client):
    seen = []
    rc.configure_queue(_queued_hook(seen))
    r = client.post("/api/reportcard/run", json=dict(RUN, price_kwh=0.2))
    assert r.status_code == 202
    assert r.get_json()["queued"] is True and r.get_json()["job_id"] == "q1"
    assert rc._JOBS == {}
    assert seen[0][0] == "a" * 32 and seen[0][1] == "llama"
    assert seen[0][2] == {"agent": "a" * 32, "provider": "llama", "mode": "standard", "model": "",
                          "model_key": "small", "price_kwh": 0.2, "confirm_vllm": False,
                          "confirm_download": False}


def test_queue_hook_declining_runs_inline(client):
    rc.configure_queue(lambda aid, prov, req: None)
    r = client.post("/api/reportcard/run", json=RUN)
    assert r.status_code == 200 and r.get_json()["job_id"]
    assert [e for e in _drain(client, r.get_json()["job_id"]) if e.get("event") == "done"]


def test_busy_runner_is_queued_when_a_hook_is_set(client, stalled):
    assert client.post("/api/reportcard/run", json=RUN).status_code == 200
    seen = []
    rc.configure_queue(_queued_hook(seen))
    r = client.post("/api/reportcard/run", json=RUN)
    assert r.status_code == 202 and len(seen) == 1


def test_busy_runner_still_refuses_when_the_hook_declines(client, stalled):
    assert client.post("/api/reportcard/run", json=RUN).status_code == 200
    rc.configure_queue(lambda aid, prov, req: None)
    r = client.post("/api/reportcard/run", json=RUN)
    assert r.status_code == 409 and "in progress" in r.get_json()["error"].lower()


def test_precheck_answers_win_over_the_queue(client, monkeypatch):
    monkeypatch.setattr(rc, "ensure_ready",
                        lambda *a, **k: {"status": "needs_confirm", "model": "m", "reference": "ref"})
    seen = []
    rc.configure_queue(_queued_hook(seen))
    r = client.post("/api/reportcard/run", json=dict(RUN, provider="vllm"))
    assert r.status_code == 200 and r.get_json()["status"] == "needs_confirm"
    assert seen == []


def test_runner_helpers_start_report_and_cancel(client, stalled):
    assert rc.card_status("nope") == {"event": "lost"}
    job_id = rc.start_card(dict(RUN, model="", price_kwh=0.15, confirm_vllm=False, confirm_download=False))
    assert job_id and rc.card_status(job_id) is None
    assert rc.start_card(dict(RUN, model="", price_kwh=0.15, confirm_vllm=False, confirm_download=False)) is None
    rc.cancel_card(job_id)
    assert rc._JOBS[job_id]["cancel"].is_set()
    rc._JOBS[job_id]["done"] = True
    assert rc.card_status(job_id) == {"event": "error", "error": "run ended without a result"}
    rc._JOBS[job_id]["terminal"] = {"event": "cancelled"}
    assert rc.card_status(job_id) == {"event": "cancelled"}


_RUN_BODY = {"agent": "a" * 32, "provider": "llama", "mode": "standard",
             "model_key": "small"}


def test_job_stores_conditions_from_two_agent_calls(client, monkeypatch):
    calls = []

    def fake(agent, method, path, timeout=15, **kw):
        calls.append((path, kw.get("params")))
        return {"schema": 1, "captured_start": f"t{len(calls)}",
                "snapshot": {"start": {"temp_c": 40 + 10 * len(calls)}}}
    monkeypatch.setattr(rc, "_agent_json", fake)
    job = client.post("/api/reportcard/run", json=_RUN_BODY).get_json()["job_id"]
    done = [e for e in _drain(client, job) if e["event"] == "done"][0]
    cond = done["card"]["result"]["conditions"]
    assert calls == [("/llama/run-conditions", {"model": "m"})] * 2
    assert cond["captured_start"] == "t1" and cond["captured_end"] == "t2"
    assert cond["snapshot"]["end"]["temp_c"] == 60


def test_job_finishes_without_conditions_on_old_agent(client, monkeypatch):
    monkeypatch.setattr(rc, "_agent_json", lambda *a, **k: None)
    job = client.post("/api/reportcard/run", json=_RUN_BODY).get_json()["job_id"]
    done = [e for e in _drain(client, job) if e["event"] == "done"][0]
    assert "conditions" not in done["card"]["result"]


def test_job_survives_a_raising_conditions_call(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("agent gone")
    monkeypatch.setattr(rc, "_agent_json", boom)
    job = client.post("/api/reportcard/run", json=_RUN_BODY).get_json()["job_id"]
    done = [e for e in _drain(client, job) if e["event"] == "done"][0]
    assert "conditions" not in done["card"]["result"]


def test_cancelled_job_skips_the_end_conditions_call(client, monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_agent_json", lambda agent, method, path, timeout=15, **kw: calls.append(path) or None)

    def cancelling_bench(*a, **k):
        for job in rc._JOBS.values():
            job["cancel"].set()
        return {"ttft_s": 0.5, "prefill_tps": 1000.0, "gen_tps": 40.0, "reps": []}
    monkeypatch.setattr(rc, "run_bench", cancelling_bench)
    job = client.post("/api/reportcard/run", json=_RUN_BODY).get_json()["job_id"]
    _drain(client, job)
    assert calls.count("/llama/run-conditions") == 1


# ── #1395: Report Card on several llama.cpp builds ─────────────────────

BA, BB, BC = "/o/llama-a/build/bin/llama-server", "/o/llama-b/bin/llama-server", "/o/llama-c/llama-server"
_LISTING = {"ok": True, "builds": [
    {"path": BA, "name": "llama-a", "active": True, "ok": True},
    {"path": BB, "name": "llama-b", "active": False, "ok": True},
    {"path": BC, "name": "llama-c", "active": False, "ok": True}]}


@pytest.fixture
def builds_env(client, monkeypatch):
    import auth
    env = {"calls": [], "quiet": [], "audit": [], "fail": set(), "lost": set(), "listing": _LISTING,
           "active": BA, "reads": 0, "read_ok": None}

    def call(agent, method, path, timeout=15, **kw):
        env["calls"].append((method, path, (kw.get("json") or {}).get("path"), timeout))
        if path != "/llama/builds/activate":
            return True, None
        p = kw["json"]["path"]
        if p in env["fail"]:
            return False, "that build failed its version check"
        if p == env["active"]:
            return False, "that build is already active"
        env["active"] = p
        if p in env["lost"]:
            return False, "/llama/builds/activate did not respond"
        return True, None

    def jget(agent, method, path, timeout=15, **kw):
        if path != "/llama/builds" or env["listing"] is None:
            return None
        env["reads"] += 1
        if env["read_ok"] is not None and env["reads"] > env["read_ok"]:
            return None
        return {**env["listing"], "builds": [{**r, "active": r["path"] == env["active"]}
                                             for r in env["listing"]["builds"]]}
    monkeypatch.setattr(rc, "_agent_call", call)
    monkeypatch.setattr(rc, "_agent_json", jget)
    monkeypatch.setattr(auth, "effective_role", lambda: "admin")
    rc.configure_build_switch(quiet=env["quiet"].append,
                              audit=lambda by, aid, p, ok, err: env["audit"].append((by, aid, p, ok)),
                              actor=lambda: {"actor": "ops"})
    yield env
    rc.configure_build_switch(None, None, None)


def _activations(env):
    return [p for m, path, p, _t in env["calls"] if path == "/llama/builds/activate"]


def _run(client, **extra):
    body = {"agent": "a" * 32, "provider": "llama", "mode": "standard", "model_key": "small", **extra}
    return client.post("/api/reportcard/run", json=body)


def test_reportcard_builds_loop_switches_and_back(client, builds_env):
    r = _run(client, builds=[BA, BB, BB, BC])
    assert r.status_code == 200, r.get_json()
    events = _drain(client, r.get_json()["job_id"])
    assert _activations(builds_env) == [BB, BC, BA]
    assert all(t == 420 for _m, path, _p, t in builds_env["calls"] if path == "/llama/builds/activate")
    done = [e for e in events if e.get("event") == "done"][0]
    assert [c["result"]["build_name"] for c in done["cards"]] == ["llama-a", "llama-b", "llama-c"]
    assert [c["result"]["build_path"] for c in done["cards"]] == [BA, BB, BC]
    assert done["card"] == done["cards"][-1]
    back = [e for e in events if e.get("phase") == "switch_back"]
    assert len(back) == 1 and back[0]["ok"] is True
    assert len(builds_env["quiet"]) >= 4 and set(builds_env["quiet"]) == {"a" * 32}
    assert [(a[2], a[3]) for a in builds_env["audit"]] == [(BB, True), (BC, True), (BA, True)]
    assert builds_env["audit"][0][0] == {"actor": "ops"}


def test_reportcard_failed_switch_continues(client, builds_env):
    builds_env["fail"].add(BB)
    r = _run(client, builds=[BB, BC])
    events = _drain(client, r.get_json()["job_id"])
    failed = [e for e in events if e.get("phase") == "build_failed"]
    assert failed and failed[0]["build"] == "llama-b" and "version check" in failed[0]["error"]
    done = [e for e in events if e.get("event") == "done"][0]
    assert [c["result"]["build_name"] for c in done["cards"]] == ["llama-c"]
    assert _activations(builds_env) == [BB, BC, BA]


def test_reportcard_switch_back_on_error(client, builds_env, monkeypatch):
    n = {"i": 0}

    def bench(*a, **k):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("bench blew up")
        return {"ttft_s": 0.5, "prefill_tps": 1000.0, "gen_tps": 40.0, "reps": []}
    monkeypatch.setattr(rc, "run_bench", bench)
    r = _run(client, builds=[BB, BC])
    events = _drain(client, r.get_json()["job_id"])
    assert events[-1]["event"] == "error" and "bench blew up" in events[-1]["error"]
    assert _activations(builds_env) == [BB, BC, BA]
    assert [e["ok"] for e in events if e.get("phase") == "switch_back"] == [True]


def test_reportcard_cancel_between_builds_switches_back(client, builds_env, monkeypatch):
    def bench(*a, **k):
        rc.cancel_card(next(iter(rc._JOBS)))
        return {"ttft_s": 0.5, "prefill_tps": 1000.0, "gen_tps": 40.0, "reps": []}
    monkeypatch.setattr(rc, "run_bench", bench)
    r = _run(client, builds=[BB, BC])
    events = _drain(client, r.get_json()["job_id"])
    assert events[-1]["event"] == "cancelled"
    assert _activations(builds_env) == [BB, BA]


def test_reportcard_unreadable_builds_errors_before_switching(client, builds_env):
    builds_env["listing"] = None
    r = _run(client, builds=[BB])
    events = _drain(client, r.get_json()["job_id"])
    assert events[-1] == {"event": "error", "error": "could not read the installed builds", "cards": []}
    assert _activations(builds_env) == []


def test_reportcard_builds_non_llama_400(client, builds_env):
    r = _run(client, provider="vllm", builds=[BB])
    assert r.status_code == 400 and r.get_json()["error"] == "builds needs the llama provider"


def test_reportcard_builds_needs_admin(client, builds_env, monkeypatch):
    import auth
    monkeypatch.setattr(auth, "effective_role", lambda: "operator")
    r = _run(client, builds=[BB])
    assert r.status_code == 403 and r.get_json()["error"] == "switching builds needs an admin"
    assert rc._JOBS == {}


def test_reportcard_no_builds_unchanged(client, builds_env):
    seen = []
    rc.configure_queue(lambda aid, prov, req: seen.append(dict(req)))
    for extra in ({}, {"builds": []}):
        r = _run(client, **extra)
        events = _drain(client, r.get_json()["job_id"])
        done = [e for e in events if e.get("event") == "done"][0]
        assert set(done) == {"event", "card", "cleanup"}
        assert "build_name" not in done["card"]["result"]
        assert not any(e.get("phase") in ("switch_back", "build_failed") for e in events)
    assert all("builds" not in q and "by" not in q for q in seen)
    assert not any(path.startswith("/llama/builds") for _m, path, _p, _t in builds_env["calls"])
    assert builds_env["quiet"] == [] and builds_env["audit"] == []


def test_reportcard_switch_back_when_activate_timed_out_but_switched(client, builds_env):
    builds_env["lost"].add(BB)
    r = _run(client, builds=[BB])
    events = _drain(client, r.get_json()["job_id"])
    assert [e["build"] for e in events if e.get("phase") == "build_failed"] == ["llama-b"]
    assert _activations(builds_env) == [BB, BA] and builds_env["active"] == BA
    assert [e["ok"] for e in events if e.get("phase") == "switch_back"] == [True]
    assert events[-1]["event"] == "error" and events[-1]["cards"] == []


def test_reportcard_no_switch_back_when_failed_switch_left_start_active(client, builds_env):
    builds_env["fail"].add(BB)
    r = _run(client, builds=[BB])
    events = _drain(client, r.get_json()["job_id"])
    assert _activations(builds_env) == [BB]
    assert not any(e.get("phase") == "switch_back" for e in events)


def test_reportcard_switch_back_unreadable_treats_already_active_as_ok(client, builds_env):
    builds_env["read_ok"] = 1
    builds_env["lost"].add(BB)
    builds_env["fail"].add(BC)
    r = _run(client, builds=[BB])
    _drain(client, r.get_json()["job_id"])
    assert _activations(builds_env) == [BB, BA]
    builds_env["calls"].clear()
    builds_env["active"], builds_env["reads"] = BA, 0
    builds_env["lost"].clear()
    r = _run(client, builds=[BC])
    events = _drain(client, r.get_json()["job_id"])
    assert _activations(builds_env) == [BC, BA]
    assert [(e["ok"], e.get("error")) for e in events if e.get("phase") == "switch_back"] == [(True, None)]


def test_reportcard_error_and_cancel_carry_stored_cards(client, builds_env, monkeypatch):
    n = {"i": 0}

    def bench(*a, **k):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("bench blew up")
        return {"ttft_s": 0.5, "prefill_tps": 1000.0, "gen_tps": 40.0, "reps": []}
    monkeypatch.setattr(rc, "run_bench", bench)
    events = _drain(client, _run(client, builds=[BB, BC]).get_json()["job_id"])
    assert events[-1]["event"] == "error"
    assert [c["result"]["build_name"] for c in events[-1]["cards"]] == ["llama-b"]

    def cancelling(*a, **k):
        rc.cancel_card(next(j for j, v in rc._JOBS.items() if not v["done"]))
        return {"ttft_s": 0.5, "prefill_tps": 1000.0, "gen_tps": 40.0, "reps": []}
    monkeypatch.setattr(rc, "run_bench", cancelling)
    builds_env["active"] = BA
    events = _drain(client, _run(client, builds=[BB, BC]).get_json()["job_id"])
    assert events[-1]["event"] == "cancelled"
    assert [c["result"]["build_path"] for c in events[-1]["cards"]] == [BB]


def test_reportcard_emits_build_start_per_build(client, builds_env):
    events = _drain(client, _run(client, builds=[BA, BC]).get_json()["job_id"])
    starts = [{k: e[k] for k in ("build", "path", "index", "total")} for e in events if e.get("phase") == "build_start"]
    assert starts == [{"build": "llama-a", "path": BA, "index": 0, "total": 2},
                      {"build": "llama-c", "path": BC, "index": 1, "total": 2}]


def test_agent_call_error_body_surfaces_agent_detail(monkeypatch):
    import agent_registry

    class R:
        ok, status_code = False, 409

        def json(self):
            return {"detail": "a switch is already running"}
    monkeypatch.setattr(agent_registry, "agent_request", lambda *a, **k: (R(), [], None))
    assert rc._agent_call({"token": "t"}, "POST", "/llama/builds/activate") == \
        (False, "/llama/builds/activate did not respond")
    assert rc._agent_call({"token": "t"}, "POST", "/llama/builds/activate", error_body=True) == \
        (False, "a switch is already running")
