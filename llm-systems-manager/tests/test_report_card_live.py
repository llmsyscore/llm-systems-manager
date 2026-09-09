"""#885: a stored live benchmark run attaches to the model's report card."""
from __future__ import annotations

import json
import sqlite3

import pytest

import bench_live as bl
import report_card as rc

AGENT = {"agent_id": "a" * 32, "token": "tok", "hostname": "h"}
MODEL = "org/m:Q4"


def _levels(pred=64.0):
    return [{"level": 1, "concurrency": 1, "bench": "throughput_1k", "osl": 256, "wall_s": 5.0, "rows": [],
             "all": {"pred_tps": pred, "prompt_tps": 400.0, "latency_s": 12.0, "accept_rate": 0.5,
                     "agg_pred_tps": pred, "completion_tokens": 1000}},
            {"level": 2, "concurrency": 2, "bench": "throughput_1k", "osl": 256, "wall_s": 5.0, "rows": [],
             "all": {"pred_tps": 58.0, "prompt_tps": 390.0, "latency_s": 14.0, "accept_rate": 0.5,
                     "agg_pred_tps": 110.0, "completion_tokens": 1000}}]


def _doc(run_id="r1", matrix=False):
    lv = _levels()
    if matrix:
        lv = [dict(lv[0]), {**lv[0], "bench": "throughput_32k", "all": {**lv[0]["all"], "pred_tps": 50.0}}]
    return {"run_id": run_id, "model_id": MODEL, "ok": True, "bench": "throughput_1k",
            "config": {"bench": "throughput_1k", "osl": 256, "concurrency": [1, 2],
                       "matrix": ({"benches": ["throughput_1k", "throughput_32k"], "osls": [256]} if matrix else None)},
            "levels": lv, "energy_wh": 3.0, "energy_source": "psu", "wh_per_ktok": 3.1, "elapsed_s": 1825.0}


@pytest.fixture
def env(monkeypatch, tmp_path):
    from flask import Flask
    app = Flask(__name__)
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db, check_same_thread=False)
    rc.init_table(conn)
    monkeypatch.setattr(rc, "_conn_factory", lambda: conn, raising=False)
    monkeypatch.setattr(rc, "_snapshot_power", lambda aid, prov=None: {"watts": 150.0, "source": "psu",
                        "gpus": [{"name": "GPU X", "vram_total_mb": 20000, "vram_used_mb": 17000, "power_w": 150.0}]})
    bl.register_routes(app, None, db_path=db, proxy=lambda *a, **k: {"ok": True},
                       agent_by_token=lambda t: AGENT if t == "tok" else None,
                       request_agent=lambda p: AGENT, note_tool_start=lambda p, t: None)
    rc.register_routes(app, db_path=db)
    c = app.test_client()
    c.conn = conn
    return c


def _store(c, doc):
    r = c.post("/api/benchmark/live/store", json=doc, headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200


def test_live_section_shape_and_matrix():
    meta = {"run_id": "r1", "ts": "2026-09-08T20:36:50+00:00", "agent_id": AGENT["agent_id"]}
    s = rc.live_section(meta, _doc(matrix=True))
    assert s["decode_tps"] == 64.0 and s["prefill_tps"] == 400.0 and s["latency_s"] == 12.0
    assert s["accept_rate"] == 0.5 and s["wh_per_ktok"] == 3.1 and s["energy_source"] == "psu"
    assert s["levels"] == [{"concurrency": 1, "pred_tps": 64.0, "agg_pred_tps": 64.0}]  # first cell only
    assert s["matrix"]["cells"] == [{"bench": "throughput_1k", "osl": 256, "pred_tps": 64.0},
                                    {"bench": "throughput_32k", "osl": 256, "pred_tps": 50.0}]
    plain = rc.live_section(meta, _doc())
    assert plain["matrix"] is None and [l["concurrency"] for l in plain["levels"]] == [1, 2]


def test_card_from_live_maps_energy():
    meta = {"run_id": "r1", "ts": "2026-09-08T20:36:50+00:00", "agent_id": AGENT["agent_id"], "model_id": MODEL}
    card = rc.card_from_live(meta, _doc(), 0.20, {"watts": None, "source": "psu", "gpus": []})
    r = card["result"]
    assert card["mode"] == "live" and card["eligible"] is False and card["provider"] == "llama"
    assert r["model"] == MODEL and r["gen_tps"] == 64.0 and r["prefill_tps"] == 400.0 and r["ttft_s"] is None
    # 3.1 Wh per 1k tokens → 1000 / (3.1 * 3600) tokens per joule
    assert r["tokens_per_joule"] == pytest.approx(1000 / (3.1 * 3600))
    # $/M tok = Wh/1k tok * price per kWh  (3.1 Wh/1k = 3.1 kWh/M tok)
    assert r["usd_per_mtok"] == pytest.approx(3.1 * 0.20)
    # avg watts from energy_wh over elapsed
    assert r["avg_watts"] == pytest.approx(3.0 / 1825.0 * 3600)
    assert r["power_source"] == "psu" and r["live"]["run_id"] == "r1"


def test_attach_creates_then_merges(env):
    _store(env, _doc("r1"))
    r = env.post("/api/reportcard/attach-live", json={"run_id": "r1"}).get_json()
    assert r["ok"] and r["attached"] == "created" and r["card"]["mode"] == "live"
    assert "agent_id" not in r["card"] and r["card"]["result"]["live"]["run_id"] == "r1"
    # a standard card for the same model on the same host now exists → the next attach merges into it
    rc.insert_card(env.conn, {"ts": 9_999_999_999, "agent_id": AGENT["agent_id"], "provider": "llama",
                              "mode": "standard", "preset_version": rc.PRESET_VERSION, "eligible": True,
                              "result": {"model": MODEL, "gen_tps": 70.0, "prefill_tps": 900.0}})
    _store(env, _doc("r2"))
    r2 = env.post("/api/reportcard/attach-live", json={"run_id": "r2"}).get_json()
    assert r2["attached"] == "merged" and r2["card"]["mode"] == "standard"
    assert r2["card"]["result"]["gen_tps"] == 70.0 and r2["card"]["result"]["live"]["run_id"] == "r2"
    latest = env.get(f"/api/reportcard/latest?agent={AGENT['agent_id']}&provider=llama&model={MODEL}").get_json()
    assert latest["card"]["result"]["live"]["run_id"] == "r2"
    # the merge updated the row in place: still exactly two cards
    assert env.conn.execute("SELECT COUNT(*) FROM report_cards").fetchone()[0] == 2


def test_attach_errors(env):
    assert env.post("/api/reportcard/attach-live", json={}).status_code == 400
    assert env.post("/api/reportcard/attach-live", json={"run_id": "nope"}).status_code == 404


def test_latest_model_filter_skips_other_models(env):
    rc.insert_card(env.conn, {"ts": 5, "agent_id": AGENT["agent_id"], "provider": "llama", "mode": "standard",
                              "preset_version": rc.PRESET_VERSION, "eligible": True, "result": {"model": "other"}})
    r = env.get(f"/api/reportcard/latest?agent={AGENT['agent_id']}&provider=llama&model={MODEL}").get_json()
    assert r["ok"] and r["card"] is None
