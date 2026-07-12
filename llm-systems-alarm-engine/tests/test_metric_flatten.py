"""
Unit tests for backend.integration.metric_flatten.

Pure data transformation — no DB / network. Each test fabricates a nested
metric dict the way the agent emits it and asserts on the flat dict-of-
MetricPoint-shapes returned by metric_to_points().
"""
from __future__ import annotations

import math

from backend.integration.metric_flatten import (
    ALIAS_TABLE,
    SKIP_TOP_LEVEL,
    _slug,
    flatten,
    metric_to_points,
    resolve,
)


# Helpers ────────────────────────────────────────────────────────────────────

def _by_metric(points: list[dict]) -> dict[tuple[str, str], dict]:
    return {(p["source"], p["metric_name"]): p for p in points}


# ── flatten() ────────────────────────────────────────────────────────────────

class TestFlatten:
    def test_skips_top_level_annotations(self):
        m = {"ts": "2026-01-01T00:00:00Z", "platform": "linux",
             "interval": 5, "cpu_total": 42.0}
        out = dict(flatten(m))
        assert ("cpu_total",) in out
        assert all(k not in out for k in (("ts",), ("platform",), ("interval",)))

    def test_skip_top_level_matches_canonical_set(self):
        # Regression: SKIP_TOP_LEVEL is the authoritative list. If something
        # gets added/removed there, this test prompts a review.
        assert SKIP_TOP_LEVEL == frozenset({"ts", "platform", "interval"})

    def test_descends_into_nested_dicts(self):
        m = {"gpu": {"temp_c": 78.0, "power_watts": 320.5}}
        out = dict(flatten(m))
        assert out[("gpu", "temp_c")] == 78.0
        assert out[("gpu", "power_watts")] == 320.5

    def test_skips_strings_booleans_nones(self):
        m = {"gpu": {"vendor": "AMD", "active": True, "missing": None,
                     "temp_c": 80.0}}
        out = dict(flatten(m))
        assert ("gpu", "temp_c") in out
        for skip in (("gpu", "vendor"), ("gpu", "active"), ("gpu", "missing")):
            assert skip not in out

    def test_skips_nan_and_inf(self):
        m = {"cpu": {"a": float("nan"), "b": float("inf"),
                     "c": float("-inf"), "d": 1.0}}
        out = dict(flatten(m))
        assert ("cpu", "d") in out
        for skip in (("cpu", "a"), ("cpu", "b"), ("cpu", "c")):
            assert skip not in out

    def test_int_values_kept_as_float(self):
        m = {"cpu": {"cores": 16}}
        out = dict(flatten(m))
        assert out[("cpu", "cores")] == 16.0
        assert isinstance(out[("cpu", "cores")], float)

    def test_list_of_dicts_descended_via_identifier_key(self):
        m = {"disk": [
            {"mountpoint": "/", "percent": 55.0, "free_gb": 200.0},
            {"mountpoint": "/home", "percent": 12.0, "free_gb": 800.0},
        ]}
        out = dict(flatten(m))
        assert out[("disk", "root", "percent")] == 55.0
        assert out[("disk", "root", "free_gb")] == 200.0
        assert out[("disk", "home", "percent")] == 12.0
        assert out[("disk", "home", "free_gb")] == 800.0
        # The identifier key itself is stripped — never a metric on its own.
        assert ("disk", "root", "mountpoint") not in out

    def test_list_of_dicts_with_alternate_identifier_keys(self):
        for id_key in ("device", "name", "id", "path"):
            m = {"things": [{id_key: "foo", "val": 1.0}]}
            out = dict(flatten(m))
            assert out[("things", "foo", "val")] == 1.0, f"{id_key} not recognized"

    def test_list_of_scalars_skipped(self):
        # No identifier key → skip entire list
        m = {"raw": [1, 2, 3], "cpu": {"x": 1.0}}
        out = dict(flatten(m))
        assert ("cpu", "x") in out
        assert all(k[0] != "raw" for k in out)

    def test_list_items_missing_identifier_skipped(self):
        m = {"disk": [
            {"mountpoint": "/", "percent": 55.0},
            {"percent": 12.0},  # no identifier — skipped
        ]}
        out = dict(flatten(m))
        assert ("disk", "root", "percent") in out
        # The second item is unreachable; its percent shouldn't appear under
        # any identifier-y path
        assert sum(1 for k in out if k[:1] == ("disk",)) == 1


# ── _slug() ──────────────────────────────────────────────────────────────────

class TestSlug:
    def test_root_path(self):
        assert _slug("/") == "root"

    def test_drops_leading_trailing_slashes(self):
        assert _slug("/home") == "home"
        assert _slug("/var/log/") == "var_log"

    def test_replaces_slashes_with_underscores(self):
        assert _slug("/mnt/data") == "mnt_data"

    def test_replaces_spaces(self):
        assert _slug("My Volume") == "My_Volume"

    def test_strips_colons(self):
        assert _slug("C:/foo") == "C_foo"

    def test_empty_input(self):
        assert _slug("") == ""
        assert _slug(None) == ""


# ── resolve() ────────────────────────────────────────────────────────────────

class TestResolve:
    def test_aliased_path_uses_canonical_name_and_unit(self):
        # cpu_total → (cpu, usage_percent, %, no transform)
        src, name, val, unit = resolve(("cpu_total",), 42.5)
        assert (src, name, val, unit) == ("cpu", "usage_percent", 42.5, "%")

    def test_aliased_path_applies_transform(self):
        # net/bytes_sent_per_sec → Mbps: 125_000 B/s == 1 Mbps (bytes*8/1e6).
        src, name, val, unit = resolve(("net", "bytes_sent_per_sec"), 125_000.0)
        assert src == "net"
        assert name == "sent_mbps"
        assert unit == "Mbps"
        assert val == 1.0

    def test_throughput_metrics_convert_bytes_to_megabits(self):
        # All four net/disk throughput aliases share the bytes/s → Mbps math.
        for path in (
            ("net", "bytes_sent_per_sec"),
            ("net", "bytes_recv_per_sec"),
            ("disk_io", "read_bytes_per_sec"),
            ("disk_io", "write_bytes_per_sec"),
        ):
            _, name, val, unit = resolve(path, 1_000_000.0)
            assert unit == "Mbps", path
            assert name.endswith("_mbps"), path
            # 1_000_000 B/s * 8 / 1e6 == 8 Mbps.
            assert val == 8.0, path

    def test_unaliased_two_part_path(self):
        # Falls back to path[0] as source, "_".join(path[1:]) as name
        src, name, val, unit = resolve(("custom", "metric_a"), 99.0)
        assert (src, name, val, unit) == ("custom", "metric_a", 99.0, None)

    def test_unaliased_three_part_path(self):
        src, name, val, unit = resolve(("disk", "root", "iops"), 5000.0)
        assert (src, name, val, unit) == ("disk", "root_iops", 5000.0, None)

    def test_unaliased_single_part_path(self):
        # Single-segment with no alias falls back to ("source", "value", ...)
        src, name, val, unit = resolve(("orphan",), 1.0)
        assert (src, name, val, unit) == ("orphan", "value", 1.0, None)

    def test_vllm_fields_alias_with_units(self):
        # #358: vllm chart fields keep their names and gain catalog units.
        src, name, val, unit = resolve(("vllm", "kv_cache_usage_pct"), 42.0)
        assert (src, name, val, unit) == ("vllm", "kv_cache_usage_pct", 42.0, "%")
        src, name, val, unit = resolve(("vllm", "tokens_per_second"), 10.5)
        assert (src, name, val, unit) == ("vllm", "tokens_per_second", 10.5, "tps")
        src, name, val, unit = resolve(("vllm", "requests_running"), 3)
        assert (src, name) == ("vllm", "requests_running")

    def test_all_alias_entries_have_consistent_shape(self):
        # Self-consistency: every alias is (str, str, str|None, callable|None)
        for path, alias in ALIAS_TABLE.items():
            assert isinstance(path, tuple) and all(isinstance(p, str) for p in path)
            src, name, unit, transform = alias
            assert isinstance(src, str) and src
            assert isinstance(name, str) and name
            assert unit is None or isinstance(unit, str)
            assert transform is None or callable(transform)


# ── metric_to_points() ───────────────────────────────────────────────────────

class TestMetricToPoints:
    def test_basic_cpu_temp(self):
        points = metric_to_points({"cpu_total": 50.0, "cpu_temp_c": 65.0})
        by = _by_metric(points)
        assert by[("cpu", "usage_percent")]["value"] == 50.0
        assert by[("cpu", "usage_percent")]["unit"] == "%"
        assert by[("cpu", "temp_c")]["value"] == 65.0
        assert by[("cpu", "temp_c")]["unit"] == "°C"

    def test_hostname_from_kwarg_wins_over_metric_host(self):
        points = metric_to_points({"host": "from-payload", "cpu_total": 1.0},
                                  hostname="from-kwarg")
        assert points[0]["hostname"] == "from-kwarg"

    def test_hostname_from_metric_payload(self):
        points = metric_to_points({"host": "from-payload", "cpu_total": 1.0})
        assert points[0]["hostname"] == "from-payload"

    def test_hostname_omitted_when_absent(self):
        points = metric_to_points({"cpu_total": 1.0})
        assert "hostname" not in points[0]

    def test_timestamp_from_kwarg_wins(self):
        points = metric_to_points({"ts": "old-ts", "cpu_total": 1.0},
                                  timestamp="kwarg-ts")
        assert points[0]["timestamp"] == "kwarg-ts"

    def test_timestamp_from_metric_ts(self):
        points = metric_to_points({"ts": "metric-ts", "cpu_total": 1.0})
        assert points[0]["timestamp"] == "metric-ts"

    def test_unit_omitted_when_alias_has_no_unit(self):
        # llama.active_slots alias has unit=None
        points = metric_to_points({"llama": {"active_slots": 4}})
        p = points[0]
        assert (p["source"], p["metric_name"], p["value"]) == ("llama", "active_slots", 4.0)
        assert "unit" not in p

    def test_last_write_wins_on_source_metric_collision(self):
        # Both paths alias to (cpu, usage_percent) — last one observed wins.
        # The flatten iteration order follows dict insertion order in CPython 3.7+.
        m = {"cpu_total": 10.0, "extra_cpu_total": 20.0}
        # Only cpu_total is in ALIAS_TABLE; extra_cpu_total falls back to
        # (extra_cpu_total, value). So they don't collide; verify cpu_total wins.
        points = metric_to_points(m)
        by = _by_metric(points)
        assert by[("cpu", "usage_percent")]["value"] == 10.0

    def test_disk_per_mountpoint_emitted_separately(self):
        m = {"disk": [
            {"mountpoint": "/", "percent": 50.0},
            {"mountpoint": "/var", "percent": 80.0},
        ]}
        points = metric_to_points(m)
        # disk paths aren't aliased — fall back to ("disk", "<mount>_percent")
        by = _by_metric(points)
        assert by[("disk", "root_percent")]["value"] == 50.0
        assert by[("disk", "var_percent")]["value"] == 80.0

    def test_skips_nan_inf_strings_bools(self):
        m = {
            "cpu_total": 42.0,
            "cpu_temp_c": math.nan,
            "host": "ignored-as-string",  # consumed for hostname, not as metric
            "gpu": {"missing": math.inf, "ok": 1.0, "label": "AMD"},
        }
        points = metric_to_points(m)
        names = {(p["source"], p["metric_name"]) for p in points}
        assert ("cpu", "usage_percent") in names
        assert ("gpu", "ok") in names
        # No leaks of unhandled types:
        assert ("cpu", "temp_c") not in names  # NaN dropped
        assert ("gpu", "missing") not in names  # inf dropped
        assert not any("label" in p["metric_name"] for p in points)

    def test_vllm_nested_sample(self):
        # #358: sample["vllm"] numeric leaves land as source="vllm" points;
        # the state/model strings are skipped.
        m = {"vllm": {"state": "running", "model": "m1",
                      "kv_cache_usage_pct": 40.0, "tokens_per_second": 5.0,
                      "requests_waiting": 2}}
        by = _by_metric(metric_to_points(m))
        assert by[("vllm", "kv_cache_usage_pct")]["value"] == 40.0
        assert by[("vllm", "tokens_per_second")]["value"] == 5.0
        assert by[("vllm", "requests_waiting")]["value"] == 2
        assert not any("state" in n or "model" in n
                       for _, n in by.keys())

    def test_empty_input(self):
        assert metric_to_points({}) == []

    def test_returns_only_skipped_keys(self):
        # Nothing flatten-able → empty
        assert metric_to_points({"ts": "x", "platform": "linux"}) == []
