"""#470: /api/energy route contract."""
from __future__ import annotations

import sqlite3
import time

import pytest

import energy as en

A1 = "a" * 32


@pytest.fixture
def client(monkeypatch, tmp_path):
    from flask import Flask
    app = Flask(__name__)
    conn = sqlite3.connect(tmp_path / "t.db", check_same_thread=False)
    en.init_table(conn)
    monkeypatch.setattr(en, "_conn_factory", lambda: conn, raising=False)
    en.register_routes(app, ctx=None, db_path=str(tmp_path / "t.db"))
    return app.test_client(), conn


def _seed(conn, hour_ts, gen=1_000_000, prompt=0, wh=100.0, active_wh=60.0):
    en.upsert_increment(conn, {
        "hour_ts": hour_ts, "agent_id": A1, "hostname": "box",
        "observed_s": 3600.0, "active_s": 1200.0, "power_s": 3600.0,
        "energy_wh": wh, "active_energy_wh": active_wh,
        "tokens_gen": gen, "tokens_prompt": prompt, "power_source": "psu"})


def test_summary_defaults_to_current_month(client):
    c, conn = client
    hour = int(time.time() // 3600) * 3600
    _seed(conn, hour)
    body = c.get("/api/energy/summary").get_json()
    assert body["ok"] is True
    assert body["window"]["label"] == en.current_month()
    assert body["totals"]["tokens_gen"] == 1_000_000
    assert body["totals"]["kwh"] == 0.1
    assert body["hosts"][0]["hostname"] == "box"
    assert body["since_ts"] == hour
    assert "price_kwh" in body["config"]


def test_summary_days_window(client):
    c, conn = client
    now = int(time.time() // 3600) * 3600
    _seed(conn, now)
    _seed(conn, now - 10 * 86400)
    day1 = c.get("/api/energy/summary?days=1").get_json()
    day30 = c.get("/api/energy/summary?days=30").get_json()
    assert day1["totals"]["tokens_gen"] == 1_000_000
    assert day30["totals"]["tokens_gen"] == 2_000_000


def test_summary_explicit_month_bounds(client):
    c, conn = client
    start, _end = en.month_bounds("2026-06")
    _seed(conn, start + 3600)
    body = c.get("/api/energy/summary?month=2026-06").get_json()
    assert body["totals"]["tokens_gen"] == 1_000_000
    other = c.get("/api/energy/summary?month=2026-05").get_json()
    assert other["totals"]["tokens_gen"] == 0


def test_summary_price_overrides(client):
    c, conn = client
    hour = int(time.time() // 3600) * 3600
    _seed(conn, hour)
    body = c.get("/api/energy/summary?price_kwh=1.0&cloud_out=2.0"
                 "&cloud_in=0").get_json()
    assert body["totals"]["cost_usd"] == pytest.approx(0.1)
    assert body["totals"]["cloud_cost_usd"] == pytest.approx(2.0)
    assert body["price_kwh"] == 1.0


def test_summary_rejects_bad_inputs(client):
    c, _conn = client
    assert c.get("/api/energy/summary?month=nope").status_code == 400
    assert c.get("/api/energy/summary?days=x").status_code == 400
    assert c.get("/api/energy/summary?price_kwh=abc").status_code == 400


def test_summary_empty_db(client):
    c, _conn = client
    body = c.get("/api/energy/summary").get_json()
    assert body["ok"] is True and body["since_ts"] is None
    assert body["totals"]["kwh"] is None and body["savings_usd"] is None


def test_hourly_rollup_across_agents(client):
    c, conn = client
    hour = int(time.time() // 3600) * 3600
    _seed(conn, hour)
    en.upsert_increment(conn, {
        "hour_ts": hour, "agent_id": "b" * 32, "hostname": "mac",
        "observed_s": 3600.0, "active_s": 0.0, "power_s": 0.0,
        "energy_wh": 0.0, "active_energy_wh": 0.0,
        "tokens_gen": 0, "tokens_prompt": 0, "power_source": None})
    body = c.get("/api/energy/hourly?hours=2").get_json()
    assert body["ok"] is True
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["hour_ts"] == hour
    assert row["energy_wh"] == 100.0 and row["observed_s"] == 7200.0


def test_hourly_agent_filter(client):
    c, conn = client
    hour = int(time.time() // 3600) * 3600
    _seed(conn, hour)
    body = c.get("/api/energy/hourly?hours=2&agent=" + "b" * 32).get_json()
    assert body["rows"] == []
    body = c.get("/api/energy/hourly?hours=2&agent=" + A1).get_json()
    assert len(body["rows"]) == 1


def test_hourly_caps_and_validates(client):
    c, _conn = client
    assert c.get("/api/energy/hourly?hours=x").status_code == 400
    body = c.get("/api/energy/hourly?hours=999999").get_json()
    assert body["hours"] == en._HOURLY_MAX_H


def test_hourly_honors_month_and_days_windows(client):
    c, conn = client
    june_start, _end = en.month_bounds("2026-06")
    _seed(conn, june_start + 3600)
    hour_now = int(time.time() // 3600) * 3600
    _seed(conn, hour_now)
    june = c.get("/api/energy/hourly?month=2026-06").get_json()
    assert june["ok"] is True and june["label"] == "2026-06"
    assert [r["hour_ts"] for r in june["rows"]] == [june_start + 3600]
    days = c.get("/api/energy/hourly?days=1").get_json()
    assert days["label"] == "last 1 days"
    assert [r["hour_ts"] for r in days["rows"]] == [hour_now]
    assert c.get("/api/energy/hourly?month=nope").status_code == 400
    default = c.get("/api/energy/hourly").get_json()
    assert default["label"] == "last 168 hours"


def test_config_fallback_without_ctx(client):
    c, _conn = client
    cfg = c.get("/api/energy/summary").get_json()["config"]
    assert cfg["price_kwh"] == 0.15
    assert cfg["cloud_price_in_per_mtok"] == en.CLOUD_PRICE_IN_DEFAULT
    assert cfg["cloud_price_out_per_mtok"] == en.CLOUD_PRICE_OUT_DEFAULT
    assert cfg["cloud_price_label"]


def test_cfg_energy_reads_ctx_and_reportcard_fallback():
    class _Obj:
        pass

    ctx = _Obj(); ctx.settings = _Obj(); ctx.settings.manager = _Obj()
    ctx.settings.manager.reportcard = _Obj()
    ctx.settings.manager.reportcard.price_kwh = 0.31
    cfg = en._cfg_energy(ctx)
    assert cfg["price_kwh"] == 0.31

    ctx.settings.manager.energy = _Obj()
    ctx.settings.manager.energy.price_kwh = 0.42
    ctx.settings.manager.energy.cloud_price_in_per_mtok = 1.0
    ctx.settings.manager.energy.cloud_price_out_per_mtok = 2.0
    ctx.settings.manager.energy.cloud_price_label = "test tier"
    cfg = en._cfg_energy(ctx)
    assert cfg == {"price_kwh": 0.42, "cloud_price_in_per_mtok": 1.0,
                   "cloud_price_out_per_mtok": 2.0,
                   "cloud_price_label": "test tier"}


# ── #541: local-day anchoring via ?tz_offset_min ──────────────────────


class _Args(dict):
    """Minimal werkzeug-args stand-in for _window_from_args."""


def _win(now, **kw):
    return en._window_from_args(_Args(kw), now)


def test_window_without_tz_offset_is_trailing_and_utc_hour_aligned():
    now = 1_786_294_567.0                       # mid-hour, deliberately
    start, end, label = _win(now, days="1")
    assert end == int(now // 3600 + 1) * 3600
    assert end - start == 86400
    assert label == "last 1 days"


def test_tz_offset_anchors_the_window_to_local_midnight():
    # 2026-08-09 17:56:07Z; at UTC-4 that is 13:56 local, so the local day
    # runs [2026-08-09 04:00Z, 2026-08-10 04:00Z).
    now = 1_786_312_567.0
    start, end, label = _win(now, days="1", tz_offset_min="-240")
    assert (end - start) == 86400
    assert start <= now < end                   # now falls inside today
    assert start % 3600 == 0 and end % 3600 == 0
    assert (start - 4 * 3600) % 86400 == 0      # local midnight at UTC-4
    assert label == "today (local)"


def test_tz_offset_east_of_utc_and_multi_day_label():
    now = 1_786_312_567.0
    start, end, label = _win(now, days="7", tz_offset_min="600")   # UTC+10
    assert (end - start) == 7 * 86400
    assert (end + 10 * 3600) % 86400 == 0
    assert label == "last 7 local days"


def test_half_hour_zone_snaps_to_the_nearest_whole_hour():
    # Hour buckets are UTC-aligned, so +5:30 can only resolve to +6 (330/60
    # rounds to 6, not 5 — banker's rounding would give 6 as well).
    now = 1_786_312_567.0
    start, _, _ = _win(now, days="1", tz_offset_min="330")
    assert (start + 6 * 3600) % 86400 == 0  # +5:30 snaps to +6


def test_bad_or_out_of_range_tz_offset_falls_back_to_trailing():
    now = 1_786_312_567.0
    for bad in ("abc", "5000", "-5000", ""):
        _, end, label = _win(now, days="1", tz_offset_min=bad)
        assert end == int(now // 3600 + 1) * 3600, bad
        assert label == "last 1 days", bad


def test_tz_offset_is_ignored_for_month_windows():
    now = 1_786_312_567.0
    a = _win(now, tz_offset_min="-240")
    b = _win(now)
    assert a == b


def test_summary_route_accepts_tz_offset_and_reports_local_label(client):
    c, conn = client
    _seed(conn, int(time.time() // 3600) * 3600)
    body = c.get("/api/energy/summary?days=1&tz_offset_min=-240").get_json()
    assert body["ok"] is True
    assert body["window"]["label"] == "today (local)"
    assert body["window"]["elapsed_s"] <= 86400
