"""#1031 ledger and pattern forecast checks."""
import pytest

import forecast_checks as fc
from tests.forecast_fakes import DAY, NOW, data, flat, ramp, run

BASE = NOW - (NOW % DAY)          # 2026-09-21 00:00 UTC; NOW is 14:13:20 that day
MONTH_START = 1788220800.0        # 2026-09-01 00:00 UTC (day 21 of a 30-day month)

LEDGER_IDS = ("power_cost", "model_errors", "model_churn", "alarm_patterns", "agent_health",
              "service_health", "bench_outcomes", "capacity", "idle_waste", "weekly_digest")


def _energy_rows():
    """14 days of hourly rows whose Wh per 1k tokens steps 10 -> 14, plus a 30-day pre-month baseline."""
    rows = []
    for day in range(14):
        per_k = 14.0 if day >= 11 else 10.0
        day_start = BASE - (13 - day) * DAY
        for hour in range(24):
            ts = day_start + hour * 3600.0
            if ts > NOW:
                continue
            rows.append({"ts": ts, "host": "rig", "wh": per_k * 2.0, "tokens": 2000.0,
                         "active_s": 1800.0, "observed_s": 3600.0})
    rows += [{"ts": MONTH_START - (i + 1) * DAY, "host": "rig", "wh": 250.0, "tokens": 0.0,
              "active_s": 0.0, "observed_s": 3600.0} for i in range(30)]
    return rows


def test_power_cost_efficiency_and_month_projection():
    # 7.044 kWh to date, 20.6 days into a 30-day month; the last week's 0.55 kWh a day adds 5.2 kWh for the rest:
    # 12.2 kWh at $0.15 = $1.84, against 7.5 kWh in the 30 days before the month (+63 %).
    rows = _energy_rows()
    by = {f.subject: f for f in run("power_cost", data(energy=lambda s, e: rows))}
    eff = by["efficiency"]
    assert (eff.check, eff.host, eff.severity) == ("power_cost", "rig", "info")
    assert eff.summary == "Energy per 1k tokens up 40 %"
    cost = by["cost"]
    assert cost.host is None and cost.severity == "warning"
    assert cost.summary == "This month's energy cost is heading for $1.84, 63 % over last month"
    assert "Hosts that joined" not in cost.detail


def test_power_cost_month_projection_follows_recent_use_not_the_month_average():
    rows = [r for r in _energy_rows() if r["ts"] < MONTH_START]
    # A heavy first fortnight and a quiet last week: 14 x 1 kWh, then 7 x 0.1 kWh.
    rows += [{"ts": MONTH_START + i * DAY, "host": "rig", "wh": 1000.0 if i < 14 else 100.0, "tokens": 0.0,
              "active_s": 0.0, "observed_s": 3600.0} for i in range(21)]
    (f,) = run("power_cost", data(energy=lambda s, e: rows))
    # 14.7 kWh to date plus 9.4 quiet days at 0.1 kWh, not 14.7 x 30 / 21 = 21 kWh.
    assert f.rate == pytest.approx(14.7 + 0.1 * (30 - (NOW - MONTH_START) / DAY), rel=0.02)


def test_power_cost_month_projection_leaves_out_hosts_that_joined_this_month():
    rows = _energy_rows()
    rows += [{"ts": MONTH_START + 10 * DAY + i * DAY, "host": "newbox", "wh": 5000.0, "tokens": 0.0,
              "active_s": 0.0, "observed_s": 3600.0} for i in range(10)]
    cost = {f.subject: f for f in run("power_cost", data(energy=lambda s, e: rows))}["cost"]
    assert "$1.84" in cost.summary
    assert cost.detail.endswith("Hosts that joined this month are left out: newbox.")


def test_power_cost_warns_when_the_month_is_half_again_over():
    rows = [r for r in _energy_rows() if r["ts"] < MONTH_START]
    rows += [{"ts": MONTH_START + i * DAY, "host": "rig", "wh": 1000.0, "tokens": 0.0,
              "active_s": 0.0, "observed_s": 3600.0} for i in range(21)]
    # 21 kWh to date plus 9.4 days at 1 kWh -> 30.4 kWh projected against 7.5 kWh = 4.1x.
    (f,) = run("power_cost", data(energy=lambda s, e: rows))
    assert f.subject == "cost" and f.severity == "warning" and "$4.56" in f.summary


OCT_1 = 1790812800.0              # 2026-10-01 00:00 UTC, the month after the fake's NOW


def _at(now, rows):
    """The fake's readers on a different clock, so month-boundary rules can be exercised."""
    base = data(energy=lambda s, e: rows)
    return fc.CheckData(now=now, window_s=14 * DAY, **base._readers)


def test_month_bounds_follow_the_local_calendar():
    # 2026-10-01 02:00 UTC is still Sep 30 at UTC-5 and already Oct 1 at UTC+2.
    start, day, days = fc._month_bounds(OCT_1 + 7200.0, -5 * 3600.0)
    assert (day, days) == (30, 30) and start == MONTH_START + 5 * 3600.0
    start, day, days = fc._month_bounds(OCT_1 - 3600.0, 2 * 3600.0)
    assert (day, days) == (1, 31) and start == OCT_1 - 2 * 3600.0


def test_power_cost_month_gate_uses_the_local_day_of_month():
    rows = [{"ts": OCT_1 - (i + 1) * DAY, "host": "rig", "wh": 250.0, "tokens": 0.0,
             "active_s": 0.0, "observed_s": 3600.0} for i in range(30)]
    rows += [{"ts": OCT_1 + i * 3600.0, "host": "rig", "wh": 100.0, "tokens": 0.0,
              "active_s": 0.0, "observed_s": 3600.0} for i in range(7 * 24)]
    base = data(energy=lambda s, e: rows)
    now = OCT_1 + 6 * DAY + 3600.0            # Oct 7 01:00 UTC: day 7 in UTC, still day 6 at UTC-5
    at = lambda tz: fc.CheckData(now=now, window_s=14 * DAY, tz_offset_s=tz, **base._readers)
    assert [f.subject for f in fc.BY_ID["power_cost"].detect(at(0.0))] == ["cost"]
    assert fc.BY_ID["power_cost"].detect(at(-5 * 3600.0)) == []


def test_power_cost_skips_the_month_projection_early_in_the_month():
    rows = [{"ts": OCT_1 - (i + 1) * DAY, "host": "rig", "wh": 250.0, "tokens": 0.0,
             "active_s": 0.0, "observed_s": 3600.0} for i in range(30)]
    rows += [{"ts": OCT_1 + i * 3600.0, "host": "rig", "wh": 100.0, "tokens": 0.0,
              "active_s": 0.0, "observed_s": 3600.0} for i in range(36)]
    # 3.6 kWh over 36 hours would project to 55.8 kWh, 7x last month — suppressed on day 2,
    # and still suppressed on day 10 because those 36 hours cover under 5 days.
    assert fc.BY_ID["power_cost"].detect(_at(OCT_1 + 1 * DAY + 43200.0, rows)) == []
    assert fc.BY_ID["power_cost"].detect(_at(OCT_1 + 9 * DAY + 43200.0, rows)) == []


def _mean_buckets(early, late, step_s=1800.0, days=14):
    """One mean-valued bucket every step_s, switching from `early` to `late` three days back."""
    n = int(days * DAY / step_s)
    t0 = NOW - n * step_s
    return [(t0 + i * step_s, late if t0 + i * step_s >= NOW - 3 * DAY else early) for i in range(n + 1)]


def test_model_errors_rate_jump():
    s = {("forecast", "gateway_requests_qwen3-32b", "mgr"): _mean_buckets(1.0, 1.0),
         ("forecast", "gateway_errors_qwen3-32b", "mgr"): _mean_buckets(0.02, 0.11),
         ("forecast", "gateway_timeouts_qwen3-32b", "mgr"): _mean_buckets(0.02, 0.5),
         ("forecast", "gateway_failovers_qwen3-32b", "mgr"): _mean_buckets(0.0, 0.0)}
    (f,) = run("model_errors", data(s, hosts=("mgr",)))
    assert f.subject == "qwen3-32b" and f.severity == "warning"
    assert "2 %" in f.summary and "11 %" in f.summary
    assert f.detail == "Mostly timeouts."


def test_model_errors_share_is_errors_over_requests_and_never_passes_100_percent():
    """Every request times out once and then fails: the share is 100 %, not 200 %."""
    s = {("forecast", "gateway_requests_solo", "mgr"): _mean_buckets(1.0, 1.0),
         ("forecast", "gateway_errors_solo", "mgr"): _mean_buckets(0.0, 1.0),
         ("forecast", "gateway_timeouts_solo", "mgr"): _mean_buckets(0.0, 1.0),
         ("forecast", "gateway_failovers_solo", "mgr"): _mean_buckets(0.0, 0.0)}
    (f,) = run("model_errors", data(s, hosts=("mgr",)))
    assert f.summary == "solo errors rose from 0 % to 100 % of requests"
    assert f.rate == 100.0 and f.severity == "critical"


def test_model_errors_names_failovers_when_they_lead():
    s = {("forecast", "gateway_requests_hop", "mgr"): _mean_buckets(1.0, 1.0),
         ("forecast", "gateway_errors_hop", "mgr"): _mean_buckets(0.0, 0.3),
         ("forecast", "gateway_timeouts_hop", "mgr"): _mean_buckets(0.0, 0.1),
         ("forecast", "gateway_failovers_hop", "mgr"): _mean_buckets(0.0, 0.8)}
    (f,) = run("model_errors", data(s, hosts=("mgr",)))
    assert f.detail == "Mostly failovers to another host."
    assert "requests went to another host" not in f.detail and "of them" not in f.detail


def test_model_errors_ignores_quiet_models_and_grades_critical():
    def counts(value):
        return [(NOW - (14 - i) * DAY, value) for i in range(15)]
    # Only four buckets carry requests in the last three days — under the floor.
    quiet = {("forecast", "gateway_requests_tiny", "mgr"): counts(2), ("forecast", "gateway_errors_tiny", "mgr"): counts(2)}
    assert run("model_errors", data(quiet, hosts=("mgr",))) == []
    bad = {("forecast", "gateway_requests_big", "mgr"): _mean_buckets(1.0, 1.0),
           ("forecast", "gateway_errors_big", "mgr"): _mean_buckets(0.0, 0.4)}
    (f,) = run("model_errors", data(bad, hosts=("mgr",)))
    assert f.severity == "critical" and "40 %" in f.summary


def _per_minute(early_per_day, late_per_day, step_s=1800.0, days=14):
    """A per-minute counter read back as 30-minute means over whole UTC days: `n` a day is n/1440 a bucket."""
    t0 = BASE - days * DAY
    return [(t0 + i * step_s, (late_per_day if t0 + i * step_s >= BASE - 3 * DAY else early_per_day) / 1440.0)
            for i in range(int(days * DAY / step_s))]


def test_model_churn_counts_the_buckets_not_their_means():
    """150 + 150 a day rising to 300 + 300: the means are 20 a day until they are turned into counts."""
    s = {("forecast", "model_loads", "mgr"): _per_minute(150.0, 300.0),
         ("forecast", "model_unloads", "mgr"): _per_minute(150.0, 300.0)}
    (f,) = run("model_churn", data(s, hosts=("mgr",)))
    assert f.summary == "Model loads and unloads doubled to 600 a day"
    assert f.rate == pytest.approx(600.0, abs=1.0)


def test_model_churn_doubling():
    # 4 loads + 4 unloads a day for 11 days, then 12 + 12: daily total 8 -> 24.
    def churn(early, late):
        return [(NOW - (13 - i) * DAY, (early if i < 11 else late) / 1440.0) for i in range(14)]
    s = {("forecast", "model_loads", "mgr"): churn(4.0, 12.0), ("forecast", "model_unloads", "mgr"): churn(4.0, 12.0)}
    (f,) = run("model_churn", data(s, hosts=("mgr",)))
    assert (f.host, f.subject, f.severity) == ("mgr", "loads", "info")
    assert f.summary == "Model loads and unloads doubled to 24 a day"
    assert f.suggested_action == "Check Autopilot's pins and idle windows — it may be flapping."
    assert f.confidence in ("medium", "high")


def test_model_churn_needs_an_earlier_level_to_double_from():
    quiet = [(NOW - (13 - i) * DAY, 0.0 if i < 11 else 40.0) for i in range(14)]
    assert run("model_churn", data({("forecast", "model_loads", "mgr"): quiet}, hosts=("mgr",))) == []


def test_alarm_patterns_periodic_flapping_cofire_rising():
    base = NOW - (NOW % DAY)
    rows = [{"id": f"p{i}", "rule": "GPU temp high", "host": "rig", "severity": "warning",
             "created": base - i * DAY + 14 * 3600 + 60, "closed": base - i * DAY + 14 * 3600 + 3600} for i in range(1, 8)]
    rows += [{"id": f"f{i}", "rule": "Net blip", "host": "rig", "severity": "info",
              "created": NOW - i * 5000.0, "closed": NOW - i * 5000.0 + 90} for i in range(1, 9)]
    rows += [{"id": f"c{i}", "rule": r, "host": "box", "severity": "warning", "created": NOW - i * 7000.0 + off, "closed": None}
             for i in range(1, 7) for r, off in (("A high", 0), ("B high", 30))]
    subjects = {f.subject: f for f in run("alarm_patterns", data(alerts=lambda s, e: rows, hosts=("rig", "box")))}
    assert "periodic:GPU temp high" in subjects and "14:00" in subjects["periodic:GPU temp high"].summary
    assert "flapping" in subjects and "cofire:A high+B high" in subjects
    assert subjects["flapping"].host == "rig" and "Net blip" in subjects["flapping"].summary
    assert all(f.severity in ("info", "warning") for f in subjects.values())


def _flappers(rule, count, offset=0.0, host="rig"):
    """`count` alerts for one rule that each close 90 s after they open."""
    return [{"id": f"{rule}-{k}", "rule": rule, "host": host, "severity": "info",
             "created": NOW - k * 5000.0 - offset, "closed": NOW - k * 5000.0 - offset + 90.0}
            for k in range(1, count + 1)]


def test_alarm_patterns_flapping_is_one_finding_per_host():
    rows = []
    for i in range(16):
        rows += _flappers(f"R{i:02d}", 5 + i, offset=i * 600.0)
    findings = run("alarm_patterns", data(alerts=lambda s, e: rows))
    (f,) = [x for x in findings if x.subject == "flapping"]
    assert (f.check, f.host, f.severity) == ("alarm_patterns", "rig", "info")
    assert f.summary == "16 alert rules on rig clear themselves within minutes — most often “R15” (20 times)"
    lines = f.detail.splitlines()
    assert lines[:5] == ["“R15” × 20", "“R14” × 19", "“R13” × 18", "“R12” × 17", "“R11” × 16"]
    assert lines[5] == "and 11 more" and len(lines) == 6
    assert f.suggested_action == "Give these rules a longer window so short blips stop paging."


def test_alarm_patterns_flapping_without_a_host_names_no_host():
    rows = [{"id": f"g{k}", "rule": "Gateway errors", "host": "", "severity": "warning",
             "created": NOW - k * 7000.0, "closed": NOW - k * 7000.0 + 60.0} for k in range(1, 8)]
    (f,) = [f for f in run("alarm_patterns", data(alerts=lambda s, e: rows)) if f.subject == "flapping"]
    assert f.host is None and f.summary == "“Gateway errors” clears itself within minutes — 7 times"


def test_alarm_patterns_flapping_reads_singular_for_one_rule():
    (f,) = [x for x in run("alarm_patterns", data(alerts=lambda s, e: _flappers("Net blip", 6)))
            if x.subject == "flapping"]
    assert f.summary == "“Net blip” on rig clears itself within minutes — 6 times"
    assert f.detail == "“Net blip” × 6"


def test_alarm_patterns_periodic_skips_test_rules_and_keeps_the_five_strongest():
    base = NOW - (NOW % DAY)
    rows = []
    for i, name in enumerate(("Rule A", "Rule B", "Rule C", "Rule D", "Rule E", "Rule F")):
        rows += [{"id": f"{name}-{day}", "rule": name, "host": "rig", "severity": "info",
                  "created": base - day * DAY + 14 * 3600 + i * 400.0, "closed": None}
                 for day in range(1, 6 + i)]
    rows += [{"id": f"t{day}", "rule": "Testing alert", "host": "rig", "severity": "info",
              "created": base - day * DAY + 14 * 3600 + 2800.0, "closed": None} for day in range(1, 13)]
    subjects = {f.subject for f in run("alarm_patterns", data(alerts=lambda s, e: rows))}
    periodic = sorted(s for s in subjects if s.startswith("periodic:"))
    assert periodic == ["periodic:Rule B", "periodic:Rule C", "periodic:Rule D",
                        "periodic:Rule E", "periodic:Rule F"]


def test_alarm_patterns_caps_apply_to_each_host_on_its_own():
    base = NOW - (NOW % DAY)
    rows = []
    for i in range(6):
        rows += [{"id": f"n{i}-{day}", "rule": f"Noisy {i}", "host": "loud", "severity": "info",
                  "created": base - day * DAY + 14 * 3600 + i * 400.0, "closed": None} for day in range(1, 13)]
    rows += [{"id": f"q{day}", "rule": "Quiet rule", "host": "calm", "severity": "info",
              "created": base - day * DAY + 9 * 3600, "closed": None} for day in range(1, 7)]
    found = [f for f in run("alarm_patterns", data(alerts=lambda s, e: rows)) if f.subject.startswith("periodic:")]
    assert sum(1 for f in found if f.host == "loud") == fc.PATTERNS_MAX
    assert [f.subject for f in found if f.host == "calm"] == ["periodic:Quiet rule"]


def test_cofire_candidates_pair_only_rules_that_fired_close_together():
    rules = {"a": [100.0, 5000.0], "b": [390.0], "c": [9000.0], "d": [299.0, 301.0]}
    assert fc._cofire_candidates(rules) == [("a", "b"), ("a", "d"), ("b", "d")]
    assert fc._cofire_candidates({"a": [1.0], "b": [10_000.0]}) == []


def test_alarm_patterns_periodic_keeps_rules_that_only_contain_the_word_test():
    base = NOW - (NOW % DAY)
    rows = []
    for i, name in enumerate(("Latest snapshot stale", "Contest sweep", "Tests nightly", "test rule")):
        rows += [{"id": f"{name}-{day}", "rule": name, "host": "rig", "severity": "info",
                  "created": base - day * DAY + 9 * 3600 + i * 400.0, "closed": None} for day in range(1, 7)]
    subjects = {f.subject for f in run("alarm_patterns", data(alerts=lambda s, e: rows))}
    assert sorted(s for s in subjects if s.startswith("periodic:")) == ["periodic:Contest sweep",
                                                                       "periodic:Latest snapshot stale"]


def _cofire_rows(a, b, host, metric_a=None, metric_b=None, n=6):
    """`n` alerts of each rule on one host, 30 s apart, with the metric key only when one is given."""
    rows = []
    for k in range(1, n + 1):
        for rule, metric, off in ((a, metric_a, 0.0), (b, metric_b, 30.0)):
            row = {"id": f"{host}{rule}{k}", "rule": rule, "host": host, "severity": "warning",
                   "created": NOW - k * 7000.0 + off, "closed": None}
            if metric is not None:
                row["metric"] = metric
            rows.append(row)
    return rows


def test_alarm_patterns_cofire_skips_same_name_different_severity_without_a_metric():
    rows = _cofire_rows("RAM usage critical", "RAM usage warning", "rig")
    rows += _cofire_rows("RAM usage critical", "RAM usage warning", "box",
                         metric_a="system/ram_percent", metric_b="system/ram_used_bytes")
    rows += _cofire_rows("RAM usage high", "Swap usage high", "mgr",
                         metric_a="system/ram_percent", metric_b="system/swap_percent")
    findings = run("alarm_patterns", data(alerts=lambda s, e: rows, hosts=("rig", "box", "mgr")))
    assert sorted(f.subject for f in findings if f.subject.startswith("cofire:")) == \
        ["cofire:RAM usage high+Swap usage high"]


def test_alarm_patterns_cofire_ignores_rows_with_no_host():
    rows = _cofire_rows("A high", "B high", "", metric_a="system/cpu_total", metric_b="system/gpu_temperature_c")
    rows += [{"id": f"old{i}", "rule": "Old blip", "host": "", "severity": "warning",
              "created": NOW - 8 * DAY - i * 3600.0, "closed": None} for i in range(2)]
    assert run("alarm_patterns", data(alerts=lambda s, e: rows)) == []


def test_alarm_patterns_cofire_skips_one_metric_watched_two_ways():
    def pair(a, b, metric_a, metric_b, host):
        return [{"id": f"{host}{r}{k}", "rule": r, "host": host, "severity": "warning", "metric": m,
                 "created": NOW - k * 7000.0 + off, "closed": None}
                for k in range(1, 7) for r, m, off in ((a, metric_a, 0.0), (b, metric_b, 30.0))]

    rows = pair("RAM usage critical", "RAM usage warning", "system/ram_percent", "system/ram_percent", "rig")
    rows += pair("Disk alert", "Disk guard", "system/disk_root_percent", "system/disk_root_percent", "box")
    rows += pair("A high", "B high", "system/cpu_total", "system/gpu_temperature_c", "mgr")
    subjects = {f.subject for f in run("alarm_patterns", data(alerts=lambda s, e: rows, hosts=("rig", "box", "mgr")))}
    assert [s for s in subjects if s.startswith("cofire:")] == ["cofire:A high+B high"]


def test_alarm_patterns_rising_ignores_rows_with_no_host():
    rows = [{"id": f"a{i}", "rule": "Disk nearly full", "host": "", "severity": "warning",
             "created": NOW - 8 * DAY - i * 3600.0, "closed": None} for i in range(3)]
    rows += [{"id": f"b{i}", "rule": "Disk nearly full", "host": None, "severity": "warning",
              "created": NOW - i * 3600.0 - 60.0, "closed": None} for i in range(12)]
    assert run("alarm_patterns", data(alerts=lambda s, e: rows)) == []


def test_alarm_patterns_rising_week_is_a_warning():
    rows = [{"id": f"a{i}", "rule": "Disk nearly full", "host": "rig", "severity": "warning",
             "created": NOW - 8 * DAY - i * 3600.0, "closed": None} for i in range(3)]
    rows += [{"id": f"b{i}", "rule": "Disk nearly full", "host": "rig", "severity": "warning",
              "created": NOW - i * 3600.0 - 60.0, "closed": None} for i in range(12)]
    by = {f.subject: f for f in run("alarm_patterns", data(alerts=lambda s, e: rows))}
    assert by["rising"].severity == "warning" and by["rising"].summary == "Alerts on rig doubled this week (12)"


def test_agent_health_gaps_version_clock():
    gaps = [(NOW - (13 - day) * DAY - 43200.0 + k * 600.0, 120.0)
            for day in range(14) for k in range(1 if day < 11 else 6)]
    skew = [(NOW - 3600.0 * (k + 1), v) for k, v in enumerate((7.0, -8.0, 9.0, -8.0, 8.5))]
    d = data({("forecast", "agent_heartbeat_gap_s", "rig"): gaps, ("forecast", "agent_clock_skew_s", "rig"): skew},
             agents=lambda: [{"host": "rig", "version": "v2026.09.01-1", "last_seen": NOW}],
             latest_agent_version=lambda: "v2026.09.17-1")
    by = {f.subject: f for f in run("agent_health", d)}
    assert by["heartbeat"].severity == "warning"
    assert by["heartbeat"].summary == "Heartbeat gaps rising — on 6 occasions a day"
    assert by["version"].severity == "info" and by["version"].confidence == "high"
    assert by["version"].summary == "Agent is on v2026.09.01-1, latest is v2026.09.17-1"
    assert by["clock"].severity == "warning" and by["clock"].summary == "Clock is 8 s off"


def _skew_series(per_day: float, days: int = 5, base: float = 0.0):
    """Hourly |offset| growing per_day seconds a day with ±0.2 s jitter, ending at NOW."""
    return [(NOW - (days * DAY - h * 3600.0), -(base + per_day * (h / 24.0) + (0.2 if h % 2 else -0.2)))
            for h in range(days * 24)]


def test_agent_health_clock_drift_is_an_info_finding():
    """#1091: an offset still under 5 s but growing 1+ s a day is reported as drift."""
    d = data({("forecast", "agent_clock_skew_s", "rig"): _skew_series(1.2, days=4)},
             agents=lambda: [{"host": "rig", "version": "v1", "last_seen": NOW}], latest_agent_version=lambda: "v1")
    by = {f.subject: f for f in run("agent_health", d)}
    assert by["clock"].severity == "info" and by["clock"].summary == "Clock is drifting 1.2 s a day"
    assert by["clock"].unit == "s per day" and abs(by["clock"].rate - 1.2) < 0.05
    assert "time-sync" in by["clock"].suggested_action


def test_agent_health_clock_drift_needs_three_days_and_a_slope():
    quiet = data({("forecast", "agent_clock_skew_s", "rig"): _skew_series(1.2, days=2)},
                 agents=lambda: [{"host": "rig", "version": "v1", "last_seen": NOW}], latest_agent_version=lambda: "v1")
    assert run("agent_health", quiet) == []
    flat = data({("forecast", "agent_clock_skew_s", "rig"): _skew_series(0.0, base=3.0)},
                agents=lambda: [{"host": "rig", "version": "v1", "last_seen": NOW}], latest_agent_version=lambda: "v1")
    assert run("agent_health", flat) == []


def test_agent_health_offset_warning_replaces_the_drift_note():
    """A large offset keeps the single 'clock' subject at warning, so the ledger sees a severity rise, not two rows."""
    d = data({("forecast", "agent_clock_skew_s", "rig"): _skew_series(2.0, base=2.0)},
             agents=lambda: [{"host": "rig", "version": "v1", "last_seen": NOW}], latest_agent_version=lambda: "v1")
    rows = [f for f in run("agent_health", d) if f.subject == "clock"]
    assert len(rows) == 1 and rows[0].severity == "warning" and rows[0].summary.startswith("Clock is ")
    assert "drifting" not in rows[0].summary


def test_agent_health_is_quiet_when_the_agent_is_current():
    d = data(agents=lambda: [{"host": "rig", "version": "v2026.09.17-1", "last_seen": NOW}],
             latest_agent_version=lambda: "v2026.09.17-1")
    assert run("agent_health", d) == []


def test_service_health_latency_backlog_db_backup_rss():
    backlog = [(NOW - k * DAY - 3600.0, 4.0) for k in range(3)] + [(NOW - k * DAY - 7200.0, 0.0) for k in range(7)]
    # Per-minute means read back one point a day: 5 then 30 errors a day.
    log_errors = [(NOW - (13 - i) * DAY - 3600.0, (5.0 if i < 11 else 30.0) / 1440.0) for i in range(14)]
    d = data({("manager_self_monitor", "manager_api_latency_ms", "mgr"): ramp(per_day=20.0, start=140.0),
              ("manager_streams", "worker_backlog", "mgr"): backlog,
              ("forecast", "db_manager_bytes", "mgr"): ramp(per_day=1.5e8, start=2.0e8),
              ("forecast", "db_wal_bytes", "mgr"): ramp(per_day=1.5e8, start=2.0e8),
              ("forecast", "backup_age_s", "mgr"): flat(3 * DAY),
              ("forecast", "log_errors", "mgr"): log_errors,
              ("processes", "manager_rss_mb", "mgr"): ramp(per_day=50.0, start=100.0)}, hosts=("mgr",))
    by = {f.subject: f for f in run("service_health", d)}
    assert by["manager_api_latency_ms"].severity == "warning"
    assert by["manager_api_latency_ms"].summary.startswith("Manager API responses are slowing — daily peaks ")
    assert by["manager_api_latency_ms"].summary.endswith(" ms")
    assert "p95" not in by["manager_api_latency_ms"].summary + by["manager_api_latency_ms"].detail
    assert by["manager_api_latency_ms"].confidence in ("medium", "high")
    assert by["backlog"].severity == "warning" and by["backlog"].summary == "Work is queuing up — a backlog on 3 of the last 7 days"
    assert by["db:manager"].severity == "info" and by["db:manager"].summary == "The manager database is growing 7 % a day"
    assert by["db:wal"].summary == "The database journal is growing 7 % a day"
    assert by["backup"].severity == "warning" and by["backup"].summary == "No backup for 3 days"
    assert by["log_errors"].severity == "info" and by["log_errors"].summary == "Errors in the log doubled to 30 a day"
    assert by["rss:manager"].severity == "warning" and by["rss:manager"].summary == "Manager memory grows 6 % a day"
    for key in ("rss:manager", "db:manager"):
        assert by[key].graph["end"] == NOW + fc.TREND_AHEAD_S and by[key].graph["fit"]
    assert by["backlog"].graph["end"] == NOW


def test_service_health_db_growth_on_a_sparse_series_stays_low_confidence():
    # 10 points over 13.5 days: a raw-series fit, so the hourly bands apply and 10 points is not enough.
    sparse = [(NOW - (9 - i) * 1.5 * DAY, 2.0e8 + i * 1.5 * 1.5e8) for i in range(10)]
    (f,) = run("service_health", data({("forecast", "db_manager_bytes", "mgr"): sparse}, hosts=("mgr",)))
    assert f.subject == "db:manager" and f.rate > 5.0 and f.confidence == "low"


def test_service_health_log_errors_need_an_earlier_level_to_double_from():
    quiet = [(NOW - (13 - i) * DAY - 3600.0, 0.0 if i < 11 else 40.0) for i in range(14)]
    assert run("service_health", data({("forecast", "log_errors", "mgr"): quiet}, hosts=("mgr",))) == []


def test_service_health_is_quiet_when_everything_is_steady():
    d = data({("manager_self_monitor", "manager_api_latency_ms", "mgr"): flat(300.0),
              ("manager_streams", "worker_backlog", "mgr"): flat(0.0),
              ("forecast", "db_manager_bytes", "mgr"): flat(2.0e9),
              ("forecast", "backup_age_s", "mgr"): flat(7200.0),
              ("processes", "manager_rss_mb", "mgr"): flat(800.0)}, hosts=("mgr",))
    assert run("service_health", d) == []


def test_bench_outcomes_accept_and_score():
    runs = [{"ts": NOW - k * DAY, "model": "qwen3-32b", "host": "rig", "gen_tps": 30.0, "ppt_tps": 800.0,
             "accept_rate": r, "baseline": 0, "ok": 1} for k, r in ((5, 0.80), (4, 0.82), (3, 0.78), (1, 0.55))]
    cards = [{"ts": NOW - 9 * DAY, "host": "rig", "model": "qwen3-32b", "tok_s": 88.0},
             {"ts": NOW - 2 * DAY, "host": "rig", "model": "qwen3-32b", "tok_s": 71.0}]
    by = {f.subject: f for f in run("bench_outcomes", data(bench_runs=lambda: runs, report_cards=lambda: cards))}
    assert by["accept:qwen3-32b"].severity == "info"
    assert by["accept:qwen3-32b"].summary == "Draft accept rate for qwen3-32b fell to 55 %"
    assert by["score:qwen3-32b"].summary == "Report card speed for qwen3-32b dropped from 88 to 71 tok/s"


def test_bench_outcomes_ignores_steady_runs_and_empty_readers():
    runs = [{"ts": NOW - k * DAY, "model": "m", "host": "rig", "gen_tps": 30.0, "ppt_tps": 800.0,
             "accept_rate": 0.80, "baseline": 0, "ok": 1} for k in (5, 4, 3, 1)]
    cards = [{"ts": NOW - 9 * DAY, "host": "rig", "model": "m", "tok_s": 88.0},
             {"ts": NOW - 2 * DAY, "host": "rig", "model": "m", "tok_s": 86.0}]
    assert run("bench_outcomes", data(bench_runs=lambda: runs, report_cards=lambda: cards)) == []
    assert run("bench_outcomes", data()) == []
    lone = data(bench_runs=lambda: runs[:1], report_cards=lambda: cards[:1])
    assert run("bench_outcomes", lone) == []


def test_capacity_pinned_no_longer_fits():
    # 24 GB VRAM, 18 GB pinned, VRAM use 80 % -> 92 % over 14 days: other use is
    # 24 x 0.92 - 18 = 4.08 GB and grows 0.2057 GB/day, so the last 1.92 GB goes in 9.33 days.
    d = data({("system", "gpu_vram_usage_percent", "rig"): ramp(per_day=12.0 / 14.0, start=80.0)},
             catalog=lambda: [{"host": "rig", "model": "qwen3-32b", "size_gb": 18.0, "pinned": 1},
                              {"host": "rig", "model": "spare", "size_gb": 9.0, "pinned": 0}],
             host_mem=lambda: {"rig": {"ram_gb": 64.0, "vram_gb": 24.0}})
    (f,) = run("capacity", d)
    assert (f.host, f.subject, f.severity) == ("rig", "pinned", "warning")
    assert f.summary == "rig cannot hold its pinned models in 9 days"
    assert abs((f.predicted_at - NOW) / DAY - 9.33) < 0.1


def test_capacity_already_over():
    # 26 GB pinned on a 24 GB card: the set does not fit at any level of other use, so it is due now.
    d = data({("system", "gpu_vram_usage_percent", "rig"): ramp(per_day=0.2, start=95.0)},
             catalog=lambda: [{"host": "rig", "model": "qwen3-32b", "size_gb": 18.0, "pinned": 1},
                              {"host": "rig", "model": "gemma3-12b", "size_gb": 8.0, "pinned": 1}],
             host_mem=lambda: {"rig": {"ram_gb": 64.0, "vram_gb": 24.0}})
    (f,) = run("capacity", d)
    assert (f.host, f.subject, f.severity) == ("rig", "pinned", "critical")
    assert f.summary == "rig cannot hold its pinned models any more"
    assert f.predicted_at == NOW


def test_capacity_is_quiet_when_memory_is_flat():
    d = data({("system", "gpu_vram_usage_percent", "rig"): flat(82.0)},
             catalog=lambda: [{"host": "rig", "model": "m", "size_gb": 18.0, "pinned": 1}],
             host_mem=lambda: {"rig": {"ram_gb": 64.0, "vram_gb": 24.0}})
    assert run("capacity", d) == []


def test_idle_waste_hours_and_cost():
    rows = [{"ts": NOW - (i + 1) * 3600.0, "host": "rig", "wh": 60.0, "tokens": 0.0,
             "active_s": 0.0, "observed_s": 3600.0} for i in range(50)]
    rows += [{"ts": NOW - 8 * DAY - i * 3600.0, "host": "rig", "wh": 80.0, "tokens": 5000.0,
              "active_s": 900.0, "observed_s": 3600.0} for i in range(24)]
    (f,) = run("idle_waste", data(energy=lambda s, e: rows))
    assert (f.host, f.severity) == ("rig", "info")
    assert f.summary == "Awake 50 h this week with no requests — about 3.0 kWh ($0.45)"
    assert f.suggested_action == "Shorten idle-sleep on rig."


def test_idle_waste_ignores_a_trickle_under_half_a_kilowatt_hour():
    rows = [{"ts": NOW - (i + 1) * 3600.0, "host": "rig", "wh": 8.0, "tokens": 0.0,
             "active_s": 0.0, "observed_s": 3600.0} for i in range(50)]
    rows += [{"ts": NOW - 8 * DAY - i * 3600.0, "host": "rig", "wh": 80.0, "tokens": 5000.0,
              "active_s": 900.0, "observed_s": 3600.0} for i in range(24)]
    # 50 idle hours, but only 0.4 kWh of them.
    assert run("idle_waste", data(energy=lambda s, e: rows)) == []


def _idle_rows(wh):
    """50 idle hours at `wh` each, plus a busy day nine days back so the window is long enough."""
    rows = [{"ts": NOW - (i + 1) * 3600.0, "host": "rig", "wh": wh, "tokens": 0.0,
             "active_s": 0.0, "observed_s": 3600.0} for i in range(50)]
    rows += [{"ts": NOW - 8 * DAY - i * 3600.0, "host": "rig", "wh": 80.0, "tokens": 5000.0,
              "active_s": 900.0, "observed_s": 3600.0} for i in range(24)]
    return rows


def test_idle_waste_reports_right_on_the_half_kilowatt_hour_floor():
    (f,) = run("idle_waste", data(energy=lambda s, e: _idle_rows(10.0)))
    assert f.rate == pytest.approx(0.5) and "about 0.5 kWh" in f.summary


def test_idle_waste_stays_quiet_just_under_the_floor():
    assert run("idle_waste", data(energy=lambda s, e: _idle_rows(9.8))) == []


def test_idle_waste_ignores_short_or_busy_hours():
    rows = [{"ts": NOW - (i + 1) * 3600.0, "host": "rig", "wh": 60.0, "tokens": 0.0,
             "active_s": 0.0, "observed_s": 1200.0} for i in range(50)]
    rows += [{"ts": NOW - 9 * DAY - i * 3600.0, "host": "rig", "wh": 60.0, "tokens": 0.0,
              "active_s": 0.0, "observed_s": 3600.0} for i in range(50)]
    assert run("idle_waste", data(energy=lambda s, e: rows)) == []


def test_weekly_digest_orders_and_handles_empty():
    rows = [{"host": "a", "summary": "info thing", "severity": "info", "predicted_at": None},
            {"host": "b", "summary": "crit thing", "severity": "critical", "predicted_at": NOW + DAY},
            {"host": "c", "summary": "warn late", "severity": "warning", "predicted_at": NOW + 9 * DAY},
            {"host": "d", "summary": "warn soon", "severity": "warning", "predicted_at": NOW + 4 * DAY}]
    (f,) = run("weekly_digest", data(findings=lambda: rows))
    assert f.detail.splitlines() == ["b: crit thing", "d: warn soon", "c: warn late"]
    assert f.summary == "3 things need attention this week — plus 1 note (counted Monday)"
    (g,) = run("weekly_digest", data())
    assert g.summary == "Nothing needs attention this week (counted Monday)"


def test_weekly_digest_names_the_week_and_counts_one():
    (f,) = run("weekly_digest", data(findings=lambda: [{"host": "a", "summary": "one", "severity": "warning"}]))
    assert (f.check, f.host, f.severity, f.subject) == ("weekly_digest", None, "info", "2026-W39")
    assert f.summary == "1 thing needs attention this week (counted Monday)" and f.detail == "a: one"


def test_weekly_digest_counts_only_warnings_and_criticals():
    rows = [{"host": "h", "summary": f"warn {i}", "severity": "warning", "predicted_at": NOW + (i + 1) * DAY}
            for i in range(4)]
    rows += [{"host": "h", "summary": f"note {i}", "severity": "info", "predicted_at": None} for i in range(47)]
    (f,) = run("weekly_digest", data(findings=lambda: rows))
    assert f.summary == "4 things need attention this week — plus 47 notes (counted Monday)"
    assert f.detail.splitlines() == ["h: warn 0", "h: warn 1", "h: warn 2"]


def test_weekly_digest_leaves_out_model_only_rows():
    rows = [{"host": "a", "summary": "real", "severity": "warning", "predicted_at": NOW + DAY, "verified": "measured"},
            {"host": "b", "summary": "guess", "severity": "critical", "predicted_at": NOW, "verified": "model"},
            {"host": "c", "summary": "note", "severity": "info", "predicted_at": None, "verified": "model"}]
    (f,) = run("weekly_digest", data(findings=lambda: rows))
    assert f.summary == "1 thing needs attention this week (counted Monday)" and f.detail == "a: real"


@pytest.mark.parametrize("check_id", ["power_cost", "model_errors", "model_churn", "alarm_patterns",
                                      "agent_health", "service_health", "capacity", "idle_waste"])
def test_empty_readers_report_collecting(check_id):
    with pytest.raises(fc.Collecting) as e:
        run(check_id, data())
    assert e.value.min_days == fc.BY_ID[check_id].min_days and e.value.have_days == 0.0


def test_ledger_checks_survive_malformed_readers():
    junk = data(alerts=lambda s, e: [None, {"rule": None}, {"rule": "x", "host": "rig", "created": None, "closed": "nope"}],
                energy=lambda s, e: [None, {"ts": None}, {"ts": NOW, "host": None, "wh": "x", "tokens": None}],
                bench_runs=lambda: [None, {"ok": 1, "accept_rate": None},
                                    {"ok": 1, "host": "rig", "model": "m", "accept_rate": "x", "ts": None}],
                report_cards=lambda: [None, {"host": "rig", "model": "m", "tok_s": None, "ts": NOW}],
                catalog=lambda: [None, {"host": "rig", "pinned": 1, "size_gb": None}],
                host_mem=lambda: {"rig": None},
                agents=lambda: [None, {"host": "rig", "version": None}],
                findings=lambda: [None, {"host": None, "summary": None, "severity": "nope", "predicted_at": "x"}],
                latest_agent_version=lambda: None, price_kwh=lambda: None)
    for check_id in LEDGER_IDS:
        try:
            out = run(check_id, junk)
        except fc.Collecting:
            continue
        assert isinstance(out, list)


def test_bucket_max_keeps_the_highest_value_per_bucket_at_its_centre():
    pts = [(0.0, 1.0), (100.0, 5.0), (3599.0, 2.0), (3600.0, 7.0), (9000.0, 3.0)]
    assert fc._bucket_max(pts, 3600.0) == [(1800.0, 5.0), (5400.0, 7.0), (9000.0, 3.0)]
    assert fc._bucket_max([], 3600.0) == []


def test_bucket_counts_turn_a_per_minute_mean_into_counts_per_bucket():
    # 30-minute buckets of a per-minute rate: 2 a minute is 60 in the bucket.
    assert fc.bucket_counts([(0.0, 2.0), (1800.0, 0.5), (3600.0, 0.0)]) == [(0.0, 60.0), (1800.0, 15.0), (3600.0, 0.0)]
    assert fc.bucket_counts([(0.0, 2.0)]) == [] and fc.bucket_counts(None) == []
    assert fc.bucket_counts([(5.0, 1.0), (5.0, 1.0)]) == []
