"""#1031 forecast math: fits, time-to-threshold, step change, residual trend, periodicity, co-firing."""
import math

import forecast_math as fm

DAY = 86400.0
T0 = 1_790_000_000.0


def ramp(days=14, per_day=1.0, start=78.0, step_s=21600.0, noise=0.0):
    n = int(days * DAY / step_s) + 1
    return [(T0 + i * step_s, start + per_day * (i * step_s / DAY) + noise * math.sin(i * 1.7)) for i in range(n)]


def test_linear_fit_recovers_slope_and_r2():
    fit = fm.linear_fit(ramp(per_day=0.93))
    assert fit.n == 57
    assert abs(fit.slope_per_s * DAY - 0.93) < 1e-6
    assert fit.r2 > 0.999


def test_linear_fit_needs_eight_points_and_a_time_span():
    assert fm.linear_fit(ramp()[:7]) is None
    assert fm.linear_fit([(T0, 1.0)] * 20) is None


def test_clean_drops_none_and_nan_and_sorts():
    assert fm.clean([(3, 1.0), (1, None), (2, float("nan")), (0, 5)]) == [(0.0, 5.0), (3.0, 1.0)]


def test_time_to_threshold():
    pts = ramp(per_day=0.93)
    now = pts[-1][0]
    secs = fm.time_to(fm.linear_fit(pts), 100.0, now)
    assert abs(secs / DAY - (100.0 - 91.02) / 0.93) < 0.05


def test_time_to_is_none_when_flat_falling_or_past():
    now = T0 + 14 * DAY
    assert fm.time_to(fm.linear_fit(ramp(per_day=0.0)), 100.0, now) is None
    assert fm.time_to(fm.linear_fit(ramp(per_day=-1.0)), 100.0, now) is None
    assert fm.time_to(fm.linear_fit(ramp(per_day=5.0)), 80.0, now) is None


def test_step_change_finds_the_start_and_levels():
    pts = [(T0 + i * 3600.0, (10.0 if i < 200 else 60.0) + math.sin(i) * 1.5) for i in range(336)]
    step = fm.step_change(pts, min_shift=15.0)
    assert abs(step.at - (T0 + 200 * 3600.0)) <= 3600.0
    assert abs(step.before - 10.0) < 1.0 and abs(step.after - 60.0) < 1.0


def test_step_change_rejects_a_ramp_noise_and_small_shifts():
    assert fm.step_change(ramp(days=14, per_day=4.0, start=10.0, step_s=3600.0), min_shift=15.0) is None
    assert fm.step_change([(T0 + i * 3600.0, 30.0 + math.sin(i) * 8.0) for i in range(336)], min_shift=15.0) is None
    assert fm.step_change([(T0 + i * 3600.0, 10.0 if i < 100 else 14.0) for i in range(336)], min_shift=15.0) is None


def test_rate_trend_uses_daily_means():
    base = T0 - (T0 % DAY)
    pts = [(base + i * 3600.0, float(i // 24)) for i in range(24 * 10)]
    fit = fm.rate_trend(pts)
    assert fit.n == 10 and abs(fit.slope_per_s * DAY - 1.0) < 1e-6


def test_rate_trend_drops_partial_edge_buckets():
    # A continuous ramp, so a thin first and last bucket are the only source of bias.
    day0 = T0 - (T0 % DAY)
    start = day0 + 20 * 3600.0  # 4h left in the first UTC day
    n_total = 4 + 9 * 24 + 5  # first bucket count=4, 9 full days, last bucket count=5 (both < half of 24)
    pts = [(start + i * 3600.0, (start + i * 3600.0 - start) / DAY) for i in range(n_total)]
    bm = fm.bucket_means(pts, DAY)
    fit = fm.rate_trend(pts)
    assert fit.n == len(bm) - 2
    assert abs(fit.slope_per_s * DAY - 1.0) < 1e-6
    plain = fm.linear_fit(bm)
    assert abs(plain.slope_per_s * DAY - 1.0) >= 1e-6


def test_rate_trend_keeps_sparse_interior_bucket():
    base = T0 - (T0 % DAY)
    pts = [(base + d * DAY + h * 3600.0, float(d)) for d in range(10) for h in range(24) if not (d == 5 and h >= 3)]
    fit = fm.rate_trend(pts)
    assert fit.n == len(fm.bucket_means(pts, DAY))


def test_rate_trend_small_bucket_counts():
    bs = 100.0
    pts = [(10.0 + i, 1.0) for i in range(2)] + [(100.0 + i * 4, 2.0) for i in range(24)]
    pts += [(210.0 + i, 3.0) for i in range(2)]
    assert fm.rate_trend(pts, bucket_s=bs) is None  # counts [2, 24, 2] -> both edges dropped -> n=1 < MIN_POINTS
    two_bucket = [(10.0 + i, 1.0) for i in range(3)] + [(110.0 + i, 2.0) for i in range(4)]
    assert fm.rate_trend(two_bucket, bucket_s=bs) is None  # fewer than 3 buckets: no interior, drops nothing


def test_residual_trend_ignores_load_driven_heat():
    util = [(T0 + i * 3600.0, 50.0 + 40.0 * math.sin(i / 5.0)) for i in range(24 * 14)]
    temp_same = [(t, 40.0 + 0.3 * u) for t, u in util]
    temp_creep = [(t, 40.0 + 0.3 * u + 0.5 * ((t - T0) / DAY)) for t, u in util]
    assert abs(fm.residual_trend(temp_same, util).slope_per_s * DAY) < 0.01
    assert abs(fm.residual_trend(temp_creep, util).slope_per_s * DAY - 0.5) < 0.02


def test_periodic_and_histogram():
    stamps = [T0 - (T0 % DAY) + d * DAY + 14 * 3600 + 120 for d in range(6)] + [T0 - (T0 % DAY) + 3 * 3600]
    assert fm.hour_histogram(stamps)[14] == 6
    assert fm.periodic(stamps) == 14
    assert fm.periodic(stamps[:3]) is None
    assert fm.periodic(stamps, tz_offset_s=-4 * 3600) == 10


def test_cofire_share():
    a = [T0, T0 + 1000, T0 + 5000, T0 + 9000]
    b = [T0 + 60, T0 + 1100, T0 + 20000]
    assert fm.cofire(a, b) == 0.5
    assert fm.cofire([], b) == 0.0


def test_confidence_bands():
    assert fm.confidence(fm.Fit(1.0, 0.0, 0.9, 60)) == "high"
    assert fm.confidence(fm.Fit(1.0, 0.0, 0.6, 30)) == "medium"
    assert fm.confidence(fm.Fit(1.0, 0.0, 0.9, 10)) == "low"
    assert fm.confidence(None) == "low"


def test_confidence_bands_follow_the_bucket_size():
    assert fm.confidence(fm.Fit(1.0, 0.0, 0.9, 12), 8, 12) == "high"
    assert fm.confidence(fm.Fit(1.0, 0.0, 0.6, 9), 8, 12) == "medium"
    assert fm.confidence(fm.Fit(1.0, 0.0, 0.9, 7), 8, 12) == "low"


def test_percentile():
    assert fm.percentile([1, 2, 3, 4, 5], 50) == 3
    assert fm.percentile([10, 20], 95) == 19.5
    assert fm.percentile([], 50) is None
    assert fm.percentile(["a", None, 3], 50) == 3


def test_step_change_finds_a_small_shift_with_a_large_offset():
    pts = [(T0 + i * 3600.0, (300.0 if i < 200 else 300.8) + math.sin(i) * 0.05) for i in range(336)]
    step = fm.step_change(pts, min_shift=0.5)
    assert step is not None
    assert abs(step.before - 300.0) < 0.05 and abs(step.after - 300.8) < 0.05
