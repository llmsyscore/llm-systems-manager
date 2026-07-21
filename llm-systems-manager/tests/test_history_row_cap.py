"""#463: /api/history responses are row-thinned server-side so ring size no
longer scales the payload with fleet size. Raw remains via max_rows=0;
under-cap responses pass through untouched."""
from datetime import datetime, timedelta, timezone

from manager_mod import app, _thin_history_rows
import manager_mod


def _rows(n, start=None):
    start = start or (datetime.now(timezone.utc) - timedelta(minutes=50))
    return [{"ts": (start + timedelta(seconds=i)).isoformat(),
             "cpu_total": float(i % 100)} for i in range(n)]


# ── pure helper ──────────────────────────────────────────────────────────────

def test_under_cap_is_identity():
    rows = _rows(500)
    assert _thin_history_rows(rows, 1200) is rows


def test_zero_cap_disables():
    rows = _rows(3000)
    assert _thin_history_rows(rows, 0) is rows


def test_over_cap_thins_to_cap():
    rows = _rows(3000)
    out = _thin_history_rows(rows, 1200)
    assert len(out) <= 1200
    assert len(out) >= 1100  # near the cap, not degenerate


def test_endpoints_preserved_and_ordered():
    rows = _rows(2649)
    out = _thin_history_rows(rows, 1200)
    assert out[0] is rows[0]
    assert out[-1] is rows[-1]
    ts = [r["ts"] for r in out]
    assert ts == sorted(ts)


def test_rows_are_originals_not_copies():
    rows = _rows(2000)
    out = _thin_history_rows(rows, 100)
    assert all(any(r is o for o in rows) for r in out[:5])


# ── route ────────────────────────────────────────────────────────────────────

def _authed_client():
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["role"] = "admin"
    return client


def _seed_ring(n):
    manager_mod._history_rows = _rows(n)


def test_route_caps_by_default():
    _seed_ring(3000)
    client = _authed_client()
    r = client.get("/api/history")
    assert r.status_code == 200
    body = r.get_json()
    assert 0 < len(body) <= manager_mod._HISTORY_MAX_ROWS


def test_route_max_rows_zero_returns_raw():
    _seed_ring(3000)
    client = _authed_client()
    r = client.get("/api/history?max_rows=0")
    assert r.status_code == 200
    assert len(r.get_json()) == 3000


def test_route_custom_max_rows():
    _seed_ring(3000)
    client = _authed_client()
    r = client.get("/api/history?max_rows=100")
    assert r.status_code == 200
    assert 0 < len(r.get_json()) <= 100


def test_route_limit_still_wins_after_thinning():
    _seed_ring(3000)
    client = _authed_client()
    r = client.get("/api/history?limit=50")
    assert r.status_code == 200
    assert len(r.get_json()) == 50


def test_route_under_cap_unchanged():
    _seed_ring(400)
    client = _authed_client()
    r = client.get("/api/history")
    assert r.status_code == 200
    assert len(r.get_json()) == 400
