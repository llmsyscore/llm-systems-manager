"""#1031 series-backed forecast checks."""
import math

import pytest

import forecast_checks as fc
from tests.forecast_fakes import DAY, NOW, data, flat, ramp, run


def test_registry_is_complete_and_titled():
    assert fc.IDS == ("disk_fill", "load_shift", "memory_headroom", "thermal_trend", "power_cost", "throughput",
                      "model_errors", "slot_pressure", "model_churn", "alarm_patterns", "agent_health",
                      "service_health", "bench_outcomes", "capacity", "idle_waste", "weekly_digest")
    assert fc.TITLES["disk_fill"] == "Disk fill" and fc.TITLES["weekly_digest"] == "Weekly digest"
    # the digest restates findings Tower has already seen, so it never gets an investigation turn
    assert fc.BY_ID["weekly_digest"].tower is False
    assert fc.BY_ID["disk_fill"].tower is True


def test_severity_for():
    assert fc.severity_for(2 * DAY) == "critical"
    assert fc.severity_for(9 * DAY) == "warning"
    assert fc.severity_for(20 * DAY) == "info"
    assert fc.severity_for(1 * DAY, cap="warning") == "warning"
    assert fc.severity_for(None) == "info"


def test_slug_matches_alarm_engine():
    assert fc.slug("/") == "root" and fc.slug("/models") == "models" and fc.slug("/System/Volumes/Data") == "System_Volumes_Data"


def test_disk_fill_predicts_date_rate_and_graph():
    d = data({("system", "disk_models_percent", "rig"): ramp(per_day=0.93, start=78.0),
              ("system", "disk_models_total_bytes", "rig"): flat(2.0e12),
              ("system", "disk_root_percent", "rig"): flat(41.0)})
    (f,) = run("disk_fill", d)
    assert (f.check, f.host, f.subject, f.severity, f.confidence) == ("disk_fill", "rig", "models", "warning", "high")
    assert abs((f.predicted_at - NOW) / DAY - 9.66) < 0.2
    assert abs(f.rate - 18.6) < 0.2 and f.unit == "GB/day"
    assert f.summary.startswith("/models full in ") and "GB/day" in f.summary
    assert f.graph["name"] == "disk_models_percent" and f.graph["threshold"] == 100.0 and f.graph["fit"]["slope_per_s"] > 0


def test_disk_fill_ignores_far_flat_and_falling():
    d = data({("system", "disk_a_percent", "rig"): ramp(per_day=0.05, start=20.0),
              ("system", "disk_b_percent", "rig"): ramp(per_day=-1.0, start=90.0)})
    assert run("disk_fill", d) == []


def test_disk_fill_label_uses_real_mountpoint_and_never_fabricates():
    series = {("system", "disk_mnt_my_data_percent", "rig"): ramp(per_day=0.93, start=78.0)}
    (f,) = run("disk_fill", data(series, mounts=lambda h: {"mnt_my_data": "/mnt/my_data"}))
    assert f.summary.startswith("/mnt/my_data full in")
    (g,) = run("disk_fill", data(series))
    assert g.summary.startswith("mnt_my_data full in")
    assert "/mnt/my/data" not in g.summary + g.detail + g.suggested_action


def test_collecting_when_history_is_short():
    d = data({("system", "disk_models_percent", "rig"): ramp(days=1, per_day=2.0, start=90.0)})
    with pytest.raises(fc.Collecting) as e:
        run("disk_fill", d)
    assert e.value.min_days == 3.0 and e.value.have_days < 1.1


def test_load_shift_reports_step_and_caps_at_warning():
    pts = [(NOW - (336 - i) * 3600.0, (10.0 if i < 200 else 60.0) + math.sin(i)) for i in range(336)]
    (f,) = run("load_shift", data({("system", "cpu_total", "rig"): pts}))
    assert f.severity == "warning" and f.subject == "cpu"
    assert "10 %" in f.summary and "60 %" in f.summary
    assert abs(f.since - (NOW - 136 * 3600.0)) <= 3600.0


def test_load_shift_reports_a_drop_as_a_note():
    pts = [(NOW - (336 - i) * 3600.0, (81.0 if i < 200 else 39.0) + math.sin(i)) for i in range(336)]
    (f,) = run("load_shift", data({("system", "ram_percent", "rig"): pts}))
    assert f.severity == "info" and f.subject == "ram"
    assert f.summary == f"RAM dropped from about 81 % to 39 % on {fc.fmt_date(f.since)}"
    assert f.detail == "A sustained change, not a dip."
    assert f.suggested_action == "Nothing to do unless this was unexpected — check what stopped on rig around that time."


def test_memory_headroom_needs_no_model_change():
    series = {("system", "ram_percent", "rig"): ramp(per_day=1.9, start=64.0)}
    (f,) = run("memory_headroom", data(series))
    assert f.subject == "ram" and f.severity == "critical" and f.unit == "%/day"
    busy = data(series, audit=lambda a, s, e: [{"ts": NOW - 5 * DAY, "action": "model.load", "target": "m", "host": "rig"}])
    assert run("memory_headroom", busy) == []


def test_memory_headroom_ignores_a_step_a_manual_load_left_behind():
    """A model loaded six days ago steps RAM 45 % -> 72 %; the slow rise after it is not a leak."""
    step_at = NOW - 6 * DAY
    pts = [(t, (72.0 + 0.2 * (t - step_at) / DAY if t >= step_at else 45.0) + 0.4 * math.sin(i * 1.7))
           for i, (t, _v) in enumerate(ramp(per_day=0.0, start=0.0))]
    assert run("memory_headroom", data({("system", "ram_percent", "rig"): pts})) == []


def test_memory_headroom_still_collects_for_hosts_that_were_not_skipped():
    d = data({("system", "ram_percent", "other"): ramp(days=1, per_day=1.9, start=64.0)}, hosts=("rig", "other"),
             audit=lambda a, s, e: [{"ts": NOW - 5 * DAY, "action": "model.load", "target": "m", "host": "rig"}])
    with pytest.raises(fc.Collecting) as e:
        run("memory_headroom", d)
    assert e.value.have_days < 1.1


def test_thermal_trend_uses_residual():
    util = [(NOW - (336 - i) * 3600.0, 50.0 + 40.0 * math.sin(i / 5.0)) for i in range(336)]
    creep = [(t, 45.0 + 0.3 * u + 0.6 * ((t - util[0][0]) / DAY)) for t, u in util]
    same = [(t, 45.0 + 0.3 * u) for t, u in util]
    (f,) = run("thermal_trend", data({("system", "gpu_temperature_c", "rig"): creep, ("system", "gpu_gpu_util_percent", "rig"): util}))
    assert f.subject == "gpu" and abs(f.rate - 0.6) < 0.05 and f.unit == "°C/day"
    assert f.graph["fit"] is not None and f.graph["threshold"] == 83.0
    assert run("thermal_trend", data({("system", "gpu_temperature_c", "rig"): same, ("system", "gpu_gpu_util_percent", "rig"): util})) == []


def test_throughput_drop_against_baseline():
    tps = ramp(per_day=-0.8, start=42.0)
    runs = [{"ts": NOW - 20 * DAY, "model": "m", "host": "rig", "gen_tps": 42.0, "ppt_tps": 900.0, "accept_rate": None, "baseline": 1, "ok": 1}]
    (f,) = run("throughput", data({("llama", "tokens_per_second", "rig"): tps}, bench_runs=lambda: runs))
    assert f.severity in ("info", "warning") and "%" in f.summary and "baseline" in f.detail


def test_slot_pressure_predicts_saturation():
    peak = ramp(per_day=0.15, start=1.0)
    d = data({("llama", "requests_processing", "rig"): peak, ("llama", "active_slots", "rig"): flat(4.0),
              ("llama", "requests_deferred", "rig"): flat(0.0)})
    (f,) = run("slot_pressure", d)
    assert f.subject == "llama" and "slots" in f.summary and f.severity == "warning"
    assert NOW < f.predicted_at < NOW + 10 * DAY and "slots" in f.detail
    assert f.confidence in ("medium", "high")


def test_slot_pressure_already_saturated():
    peak = ramp(per_day=0.25, start=1.0)
    series = {("llama", "requests_processing", "rig"): peak, ("llama", "active_slots", "rig"): flat(4.0),
              ("llama", "requests_deferred", "rig"): flat(0.0)}
    (f,) = run("slot_pressure", data(series))
    assert f.predicted_at is None and f.severity == "info"
    assert f.summary.startswith("All 4 slots are already")
    queued = {**series, ("llama", "requests_deferred", "rig"): flat(2.0)}
    (g,) = run("slot_pressure", data(queued))
    assert g.predicted_at is None and g.severity == "warning" and g.detail.endswith("waited for a free one in the last day.")
    single = {**queued, ("llama", "active_slots", "rig"): flat(1.0)}
    (h,) = run("slot_pressure", data(single))
    assert h.summary == "The only slot is already in use at peak times" and "the only slot" in h.detail


def test_slot_pressure_reads_peaks_not_means():
    """Means of a bursty series never reach the pool size; the bucket maxima do."""
    series = {("llama", "requests_processing", "rig"): flat(0.5),
              ("llama", "requests_processing", "rig", "max"): ramp(per_day=0.25, start=1.0),
              ("llama", "active_slots", "rig"): flat(4.0),
              ("llama", "requests_deferred", "rig", "max"): flat(0.0)}
    (f,) = run("slot_pressure", data(series))
    assert f.summary.startswith("All 4 slots are already")


def test_slot_pressure_ignores_a_pool_that_never_reaches_one_slot():
    """A sleeping model leaves fractional bucket values; those are not a pool of slots."""
    series = {("llama", "requests_processing", "rig", "max"): ramp(per_day=0.25, start=1.0),
              ("llama", "requests_processing", "rig"): flat(0.5),
              ("llama", "active_slots", "rig"): flat(0.3)}
    assert run("slot_pressure", data(series)) == []


def test_slot_pressure_takes_the_largest_recent_pool_reading():
    pool = flat(4.0)[:-6] + [(t, 0.0) for t, _ in flat(4.0)[-6:]]
    series = {("llama", "requests_processing", "rig", "max"): ramp(per_day=0.25, start=1.0),
              ("llama", "requests_processing", "rig"): flat(0.5), ("llama", "active_slots", "rig"): pool}
    (f,) = run("slot_pressure", data(series))
    assert f.summary.startswith("All 4 slots are already") and f.graph["agg"] == "max"


def test_throughput_graph_reads_peaks_against_a_reference_line():
    d = data({("llama", "tokens_per_second", "rig"): ramp(per_day=-1.5, start=60.0)})
    (f,) = run("throughput", d)
    assert f.graph["agg"] == "max" and f.graph["limit"] is False and f.graph["end"] == NOW + fc.TREND_AHEAD_S


def test_slot_pressure_streams():
    d = data({("manager_streams", "stream_active", "mgr"): ramp(per_day=1.5, start=20.0),
              ("manager_streams", "stream_limit", "mgr"): flat(48.0)}, hosts=("mgr",))
    (f,) = run("slot_pressure", d)
    assert f.subject == "streams" and f.predicted_at > NOW
    assert "slots" not in f.summary and f.summary.startswith("All 48 live-stream connections in use within ")
    assert "live-stream connections" in f.detail
    assert f.suggested_action == "Raise the stream limit or close idle dashboards."
