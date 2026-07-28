"""#468: report_cards SQLite storage round-trip."""
from __future__ import annotations

import sqlite3

import report_card


def _conn():
    c = sqlite3.connect(":memory:")
    report_card.init_table(c)
    return c


CARD = {"ts": 1000, "agent_id": "a" * 32, "provider": "llama",
        "mode": "standard", "preset_version": "preset_v1", "eligible": True,
        "result": {"gen_tps": 42.5}}


def test_insert_and_latest_roundtrip():
    c = _conn()
    report_card.insert_card(c, CARD)
    got = report_card.latest_card(c, "a" * 32, "llama")
    assert got["result"]["gen_tps"] == 42.5
    assert got["eligible"] is True


def test_latest_returns_newest():
    c = _conn()
    report_card.insert_card(c, CARD)
    report_card.insert_card(c, {**CARD, "ts": 2000, "result": {"gen_tps": 50.0}})
    assert report_card.latest_card(c, "a" * 32, "llama")["result"]["gen_tps"] == 50.0


def test_latest_none_for_unknown():
    assert report_card.latest_card(_conn(), "b" * 32, "llama") is None


def test_history_filters_and_orders():
    c = _conn()
    for ts in (3000, 1000, 2000):
        report_card.insert_card(c, {**CARD, "ts": ts,
                                    "result": {"gen_tps": ts, "model": "m1"}})
    report_card.insert_card(c, {**CARD, "result": {"gen_tps": 1, "model": "m2"}})
    rows = report_card.history(c, "a" * 32, "llama", "m1")
    assert [r["ts"] for r in rows] == [1000, 2000, 3000]


def test_init_table_is_idempotent():
    c = _conn()
    report_card.init_table(c)
    report_card.insert_card(c, CARD)
    assert report_card.latest_card(c, "a" * 32, "llama") is not None
