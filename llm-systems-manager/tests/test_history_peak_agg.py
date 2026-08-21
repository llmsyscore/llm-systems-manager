"""#596: the history fan-out requests agg=max for throughput fields only,
so long-window AE reads hit the max rollup and burst peaks survive."""
import manager_mod


class _Resp:
    status_code = 200

    def json(self):
        return []


def _capture_get(calls):
    def _get(url, params=None, timeout=None):
        calls.append((url, dict(params or {})))
        return _Resp()
    return _get


def test_throughput_fields_request_max(monkeypatch):
    calls = []
    monkeypatch.setattr(manager_mod._ae_session, "get", _capture_get(calls))
    field, pts = manager_mod._fetch_history_series(
        "http://ae", "llama", "tokens_per_second", "llama_tps", 1440, 500)
    assert field == "llama_tps"
    assert pts == []
    assert calls[0][1].get("agg") == "max"


def test_non_throughput_fields_stay_mean(monkeypatch):
    calls = []
    monkeypatch.setattr(manager_mod._ae_session, "get", _capture_get(calls))
    manager_mod._fetch_history_series(
        "http://ae", "system", "cpu_total", "cpu_total", 1440, 500)
    assert "agg" not in calls[0][1]


def test_peak_fields_all_exist_in_legacy_map():
    fields = {f for _, _, f in manager_mod._HISTORY_LEGACY_FIELD_MAP}
    assert manager_mod._HISTORY_PEAK_FIELDS <= fields
