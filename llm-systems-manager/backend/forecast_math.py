"""Forecast math (#1031): pure numeric helpers over [(ts, value)] series; no I/O."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

MIN_POINTS = 8


@dataclass(frozen=True)
class Fit:
    slope_per_s: float
    intercept: float
    r2: float
    n: int

    def at(self, ts: float) -> float:
        return self.intercept + self.slope_per_s * float(ts)


@dataclass(frozen=True)
class Step:
    at: float
    before: float
    after: float


def clean(points) -> "list[tuple[float, float]]":
    out = []
    for p in points or []:
        try:
            t, v = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(t) and math.isfinite(v):
            out.append((t, v))
    return sorted(out)


def linear_fit(points) -> Optional[Fit]:
    pts = clean(points)
    n = len(pts)
    if n < MIN_POINTS:
        return None
    mx = sum(t for t, _ in pts) / n
    my = sum(v for _, v in pts) / n
    sxx = sum((t - mx) ** 2 for t, _ in pts)
    if sxx <= 0:
        return None
    slope = sum((t - mx) * (v - my) for t, v in pts) / sxx
    syy = sum((v - my) ** 2 for _, v in pts)
    sse = sum((v - (my + slope * (t - mx))) ** 2 for t, v in pts)
    r2 = 1.0 if syy <= 1e-12 else max(0.0, 1.0 - sse / syy)
    return Fit(slope, my - slope * mx, r2, n)


def time_to(fit: Optional[Fit], threshold: float, now: float) -> Optional[float]:
    if fit is None or abs(fit.slope_per_s) < 1e-12:
        return None
    secs = (float(threshold) - fit.at(now)) / fit.slope_per_s
    return secs if secs > 0 else None


def step_change(points, min_shift: float, min_side: int = 6) -> Optional[Step]:
    pts = clean(points)
    n = len(pts)
    if n < 2 * min_side:
        return None
    vals = [v for _, v in pts]
    mean = sum(vals) / n
    pre, pre2 = [0.0], [0.0]
    for v in vals:
        s = v - mean
        pre.append(pre[-1] + s)
        pre2.append(pre2[-1] + s * s)

    def sse(i, j):
        m = (pre[j] - pre[i]) / (j - i)
        return (pre2[j] - pre2[i]) - (j - i) * m * m

    best_i, best = None, math.inf
    for i in range(min_side, n - min_side + 1):
        s = sse(0, i) + sse(i, n)
        if s < best:
            best_i, best = i, s
    before = pre[best_i] / best_i + mean
    after = (pre[n] - pre[best_i]) / (n - best_i) + mean
    shift = abs(after - before)
    pooled = math.sqrt(max(best, 0.0) / max(1, n - 2))
    fit = linear_fit(pts)
    linear_sse = sum((v - fit.at(t)) ** 2 for t, v in pts) if fit else math.inf
    if shift < float(min_shift) or shift < 3.0 * pooled or best >= linear_sse:
        return None
    return Step(pts[best_i][0], before, after)


def _bucket_counts(points, bucket_s: float) -> "list[tuple[float, float, int]]":
    if not math.isfinite(bucket_s) or bucket_s <= 0:
        return []
    acc: "dict[int, list[float]]" = {}
    for t, v in clean(points):
        acc.setdefault(int(t // bucket_s), []).append(v)
    return [(k * bucket_s + bucket_s / 2.0, sum(vs) / len(vs), len(vs)) for k, vs in sorted(acc.items())]


def bucket_means(points, bucket_s: float) -> "list[tuple[float, float]]":
    return [(t, m) for t, m, _ in _bucket_counts(points, bucket_s)]


def rate_trend(points, bucket_s: float = 86400.0) -> Optional[Fit]:
    buckets = _bucket_counts(points, bucket_s)
    interior = buckets[1:-1] if len(buckets) > 2 else []
    if interior:
        counts = sorted(c for _, _, c in interior)
        mid = len(counts) // 2
        median = counts[mid] if len(counts) % 2 else (counts[mid - 1] + counts[mid]) / 2.0
        half = median / 2.0
        if buckets[0][2] < half:
            buckets = buckets[1:]
        if buckets and buckets[-1][2] < half:
            buckets = buckets[:-1]
    return linear_fit([(t, m) for t, m, _ in buckets])


def residual_trend(y_points, x_points, bucket_s: float = 3600.0) -> Optional[Fit]:
    ys, xs = dict(bucket_means(y_points, bucket_s)), dict(bucket_means(x_points, bucket_s))
    keys = sorted(set(ys) & set(xs))
    if len(keys) < MIN_POINTS:
        return None
    mx = sum(xs[k] for k in keys) / len(keys)
    my = sum(ys[k] for k in keys) / len(keys)
    sxx = sum((xs[k] - mx) ** 2 for k in keys)
    beta = 0.0 if sxx <= 0 else sum((xs[k] - mx) * (ys[k] - my) for k in keys) / sxx
    return linear_fit([(k, ys[k] - (my + beta * (xs[k] - mx))) for k in keys])


def hour_histogram(timestamps, tz_offset_s: float = 0) -> "list[int]":
    hist = [0] * 24
    for t in timestamps or []:
        hist[int(((float(t) + tz_offset_s) % 86400.0) // 3600)] += 1
    return hist


def periodic(timestamps, tz_offset_s: float = 0, min_events: int = 5, share: float = 0.6) -> Optional[int]:
    stamps = list(timestamps or [])
    if len(stamps) < min_events:
        return None
    hist = hour_histogram(stamps, tz_offset_s)
    top = max(range(24), key=lambda h: hist[h])
    return top if hist[top] >= min_events and hist[top] / len(stamps) >= share else None


def cofire(a, b, within_s: float = 300.0) -> float:
    a, b = sorted(a or []), sorted(b or [])
    if not a:
        return 0.0
    hit, j = 0, 0
    for t in a:
        while j < len(b) and b[j] < t - within_s:
            j += 1
        if j < len(b) and abs(b[j] - t) <= within_s:
            hit += 1
    return hit / len(a)


def confidence(fit: Optional[Fit], min_medium: int = 24, min_high: int = 48) -> str:
    """Grade a fit; the point counts are the bands for hourly series, loosened for daily buckets."""
    if fit is None:
        return "low"
    if fit.r2 >= 0.8 and fit.n >= min_high:
        return "high"
    if fit.r2 >= 0.5 and fit.n >= min_medium:
        return "medium"
    return "low"


def percentile(values, q: float) -> Optional[float]:
    vals = []
    for v in values or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            vals.append(f)
    vals.sort()
    if not vals:
        return None
    pos = (len(vals) - 1) * float(q) / 100.0
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)
