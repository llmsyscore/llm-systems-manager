# agent/tests/test_metric_sanitize.py
"""The metric client must never enqueue non-finite floats: inf/-inf/nan break
the JSON POST to the alarm engine ('Out of range float values are not JSON
compliant'). They must be scrubbed to null at the enqueue choke point."""
from __future__ import annotations

import json
import sys
import types

# The agent runtime ships `requests`; the test venv doesn't. The client only
# needs requests.Session to exist (no POST is made here), so stub it.
if "requests" not in sys.modules:
    _fake = types.ModuleType("requests")
    _fake.Session = type("Session", (), {})
    sys.modules["requests"] = _fake

import buffered_metric_client as bmc


def _client(tmp_path):
    return bmc.BufferedMetricClient(
        endpoint_url="http://example.invalid/api/alarm/metrics/ingest",
        host="testhost",
        cache_dir=tmp_path,
    )


def test_enqueue_scrubs_non_finite_floats_to_none(tmp_path):
    client = _client(tmp_path)
    sample = {
        "ts": 1,
        "liquidctl": {
            "psu": {"Estimated efficiency": {"value": float("inf"), "unit": ""}},
            "smart": {"fans": [{"id": 1, "speed": {"value": float("nan"), "unit": "rpm"}}]},
        },
        "neg": float("-inf"),
    }
    client.enqueue(sample)
    batch, _ = client._store.snapshot(10)
    s = batch[0]

    assert s["liquidctl"]["psu"]["Estimated efficiency"]["value"] is None
    assert s["liquidctl"]["smart"]["fans"][0]["speed"]["value"] is None
    assert s["neg"] is None
    # The strict encoder (allow_nan=False) is what the requests json= path uses
    # in prod; it must not raise on the scrubbed payload.
    json.dumps({"host": "h", "samples": batch}, allow_nan=False)


def test_enqueue_preserves_finite_and_non_numeric_values(tmp_path):
    client = _client(tmp_path)
    sample = {
        "f": 1.5,
        "zero": 0.0,
        "i": 7,
        "s": "text",
        "b": True,
        "none": None,
        "nested": {"x": [1, 2.0, {"y": 3.5}]},
    }
    client.enqueue(sample)
    batch, _ = client._store.snapshot(10)
    s = batch[0]

    assert s["f"] == 1.5
    assert s["zero"] == 0.0
    assert s["i"] == 7
    assert s["s"] == "text"
    assert s["b"] is True
    assert s["none"] is None
    assert s["nested"]["x"] == [1, 2.0, {"y": 3.5}]


def test_enqueue_does_not_mutate_caller_sample(tmp_path):
    client = _client(tmp_path)
    sample = {"v": float("inf")}
    client.enqueue(sample)
    # caller's original dict is untouched; only the stored copy is scrubbed
    assert sample["v"] == float("inf")
