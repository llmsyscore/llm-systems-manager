"""Forecast checks (#1031): deterministic detectors over injected readers; one Finding per trend."""
from __future__ import annotations

import calendar
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import forecast_math as fm

DAY = 86400.0
SEVERITIES = ("info", "warning", "critical")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}
DAILY_BANDS = (8, 12)
# How far a trend with no limit to reach is drawn past today.
TREND_AHEAD_S = 5 * DAY


@dataclass
class Finding:
    check: str
    host: Optional[str]
    subject: Optional[str]
    severity: str
    summary: str
    detail: str = ""
    since: Optional[float] = None
    predicted_at: Optional[float] = None
    rate: Optional[float] = None
    unit: str = ""
    confidence: str = "medium"
    suggested_action: str = ""
    graph: Optional[dict] = None

    @property
    def fingerprint(self) -> str:
        return f"{self.check}|{self.host or '-'}|{self.subject or '-'}"

    def to_row(self) -> dict:
        return {**asdict(self), "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    detect: Callable[["CheckData"], "list[Finding]"]
    min_days: float = 3.0
    tower: bool = True


class Collecting(Exception):
    def __init__(self, have_days: float, min_days: float):
        super().__init__(f"collecting {have_days:.1f}/{min_days:.0f} days")
        self.have_days, self.min_days = float(have_days), float(min_days)


class CheckData:
    """Readers a check may use; every reader is injected and may return an empty list."""
    def __init__(self, *, now: float, window_s: float, tz_offset_s: float = 0, **readers: Callable[..., Any]):
        self.now, self.window_s, self.tz_offset_s = float(now), float(window_s), float(tz_offset_s)
        self.start = self.now - self.window_s
        self._readers = readers

    def __getattr__(self, name: str) -> Callable[..., Any]:
        try:
            return self._readers[name]
        except KeyError:
            raise AttributeError(name) from None

    def window(self, source: str, name: str, host: str, points: int = 720,
               agg: str = "mean") -> "list[tuple[float, float]]":
        return fm.clean(self.series(source, name, host, self.start, self.now, points, agg=agg))

    @staticmethod
    def span(points) -> float:
        return (points[-1][0] - points[0][0]) / DAY if len(points) >= 2 else 0.0

    def need(self, points, min_days: float) -> None:
        have = self.span(points)
        if have < min_days:
            raise Collecting(have, min_days)


def severity_for(seconds_to_impact: Optional[float], cap: str = "critical") -> str:
    if seconds_to_impact is None:
        sev = "info"
    elif seconds_to_impact < 3 * DAY:
        sev = "critical"
    elif seconds_to_impact < 14 * DAY:
        sev = "warning"
    else:
        sev = "info"
    return sev if _RANK[sev] <= _RANK[cap] else cap


def slug(text: Any) -> str:
    s = str(text or "").strip("/").replace("/", "_").replace(" ", "_").replace(":", "")
    return s or "root"


def graph_spec(source, name, host, start, end, threshold=None, fit: Optional[fm.Fit] = None,
               agg: str = "mean", limit: bool = True) -> dict:
    """What the page needs to draw a finding: the series, how to bucket it, the fit, and a line that is
    either a limit the fit stops at or (limit=False) a reference level."""
    return {"source": source, "name": name, "host": host, "start": start, "end": end, "threshold": threshold,
            "fit": {"slope_per_s": fit.slope_per_s, "intercept": fit.intercept} if fit else None,
            "agg": agg, "limit": bool(limit)}


def fmt_days(seconds: float) -> str:
    if seconds < 2 * DAY:
        return f"{max(1, round(seconds / 3600.0))} hours"
    return f"{round(seconds / DAY)} days"


def fmt_date(ts: float, tz_offset_s: float = 0) -> str:
    d = datetime.fromtimestamp(float(ts) + tz_offset_s, tz=timezone.utc)
    return f"{d.strftime('%b')} {d.day}"


def fmt_weekday(ts: float, tz_offset_s: float = 0) -> str:
    """Local weekday name, e.g. Monday."""
    return datetime.fromtimestamp(float(ts) + tz_offset_s, tz=timezone.utc).strftime("%A")


def _daily_confidence(fit: Optional[fm.Fit]) -> str:
    """Confidence for a fit made over daily buckets, where a window holds far fewer points."""
    return fm.confidence(fit, *DAILY_BANDS)


def _bump(severity: str) -> str:
    """Next severity up, capped at critical."""
    return SEVERITIES[min(_RANK.get(severity, 0) + 1, len(SEVERITIES) - 1)]


def _bucket_max(points, bucket_s: float) -> "list[tuple[float, float]]":
    """Highest value per bucket, stamped at the bucket centre."""
    acc: "dict[int, float]" = {}
    for t, v in points:
        k = int(t // bucket_s)
        acc[k] = v if k not in acc else max(acc[k], v)
    return [(k * bucket_s + bucket_s / 2.0, v) for k, v in sorted(acc.items())]


def bucket_counts(points) -> "list[tuple[float, float]]":
    """Mean buckets of a per-minute counter as counts, using the median spacing between points."""
    pts = list(points or [])
    if len(pts) < 2:
        return []
    gaps = sorted(pts[i + 1][0] - pts[i][0] for i in range(len(pts) - 1))
    span = _median(gaps)
    if span <= 0:
        return []
    return [(t, v * span / 60.0) for t, v in pts]


def _mount_label(d: CheckData, host: str, mount: str) -> str:
    """Real mountpoint for a metric-name slug; never invents path separators."""
    real = (d.mounts(host) or {}).get(mount)
    if real:
        return str(real)
    if mount == "root":
        return "/"
    return mount if "_" in mount else "/" + mount


def _disk_fill(d: CheckData) -> "list[Finding]":
    """Mount-by-mount projection of percent-used to 100 %."""
    out, best, seen = [], 0.0, False
    for host in d.hosts():
        for name in d.names("system", host):
            if not (name.startswith("disk_") and name.endswith("_percent")):
                continue
            seen = True
            mount = name[len("disk_"):-len("_percent")]
            pts = d.window("system", name, host)
            best = max(best, d.span(pts))
            if d.span(pts) < 3.0:
                continue
            fit = fm.linear_fit(pts)
            secs = fm.time_to(fit, 100.0, d.now)
            if secs is None or secs > 30 * DAY or fm.confidence(fit) == "low":
                continue
            total = d.window("system", f"disk_{mount}_total_bytes", host, points=4)
            gb_day = fit.slope_per_s * DAY / 100.0 * total[-1][1] / 1e9 if total else None
            label = _mount_label(d, host, mount)
            rate_txt = f" (+{gb_day:.1f} GB/day)" if gb_day is not None else f" (+{fit.slope_per_s * DAY:.2f} %/day)"
            out.append(Finding("disk_fill", host, mount, severity_for(secs), f"{label} full in {fmt_days(secs)}{rate_txt}",
                               detail=f"Disk use on {label} has grown steadily and reaches 100 % around {fmt_date(d.now + secs, d.tz_offset_s)}.",
                               since=pts[0][0], predicted_at=d.now + secs,
                               rate=gb_day if gb_day is not None else fit.slope_per_s * DAY,
                               unit="GB/day" if gb_day is not None else "%/day", confidence=fm.confidence(fit),
                               suggested_action=f"Free space on {label} or move it to a larger volume before {fmt_date(d.now + secs, d.tz_offset_s)}.",
                               graph=graph_spec("system", name, host, d.start, d.now + secs, 100.0, fit)))
    if not out and (not seen or best < 3.0):
        raise Collecting(best, 3.0)
    return out


_LOAD_SERIES = (("cpu_total", "cpu", "CPU"), ("ram_percent", "ram", "RAM"), ("gpu_gpu_util_percent", "gpu", "GPU"))


def _load_shift(d: CheckData) -> "list[Finding]":
    """Sustained step changes in CPU, RAM or GPU load."""
    out, best, seen = [], 0.0, False
    for host in d.hosts():
        for name, subject, label in _LOAD_SERIES:
            pts = d.window("system", name, host)
            if not pts:
                continue
            seen = True
            best = max(best, d.span(pts))
            if d.span(pts) < 3.0:
                continue
            step = fm.step_change(pts, min_shift=15.0, min_side=12)
            if step is None or d.now - step.at < 12 * 3600:
                continue
            when = fmt_date(step.at, d.tz_offset_s)
            down = step.after < step.before
            summary = (f"{label} dropped from about {step.before:.0f} % to {step.after:.0f} % on {when}" if down
                       else f"{label} stepped from about {step.before:.0f} % to {step.after:.0f} % on {when}")
            action = (f"Nothing to do unless this was unexpected — check what stopped on {host} around that time."
                      if down else f"Check what started on {host} around that time.")
            out.append(Finding("load_shift", host, subject, "info" if down else "warning", summary,
                               detail="A sustained change, not a dip." if down else "A sustained change, not a spike.",
                               since=step.at, rate=step.after - step.before, unit="%", confidence="high",
                               suggested_action=action,
                               graph=graph_spec("system", name, host, d.start, d.now)))
    if not out and (not seen or best < 3.0):
        raise Collecting(best, 3.0)
    return out


_MEM_SERIES = (("ram_percent", "ram", "RAM"), ("gpu_vram_usage_percent", "vram", "VRAM"))
_MODEL_ACTIONS = ("model.load", "model.unload", "autopilot.load", "autopilot.unload")


def _memory_headroom(d: CheckData) -> "list[Finding]":
    """Rising RAM or VRAM use on hosts whose loaded models never changed."""
    out, best, seen, checked, skipped = [], 0.0, False, 0, 0
    changed = {r.get("host") for r in (d.audit(_MODEL_ACTIONS, d.start, d.now) or [])}
    for host in d.hosts():
        if host in changed:
            skipped += 1
            continue
        checked += 1
        for name, subject, label in _MEM_SERIES:
            pts = d.window("system", name, host)
            if not pts:
                continue
            seen = True
            best = max(best, d.span(pts))
            if d.span(pts) < 3.0:
                continue
            # Skips a series that steps: a model came or went, which is not a leak.
            if fm.step_change(pts, min_shift=10.0, min_side=12) is not None:
                continue
            fit = fm.linear_fit(pts)
            if fit is None or fm.confidence(fit) == "low":
                continue
            rate = fit.slope_per_s * DAY
            secs = fm.time_to(fit, 95.0, d.now)
            if rate < 0.3 or secs is None or secs > 30 * DAY:
                continue
            when = fmt_date(d.now + secs, d.tz_offset_s)
            out.append(Finding("memory_headroom", host, subject, severity_for(secs),
                               f"Out of {label} headroom in {fmt_days(secs)} — rising {rate:.1f} %/day with no model change",
                               detail=f"{label} use on {host} reaches 95 % around {when} and no model was loaded or unloaded in this window.",
                               since=pts[0][0], predicted_at=d.now + secs, rate=rate, unit="%/day",
                               confidence=fm.confidence(fit),
                               suggested_action=f"Restart the serving process on {host} during a quiet hour, then watch whether the climb returns.",
                               graph=graph_spec("system", name, host, d.start, d.now + secs, 95.0, fit)))
    # Silent only when every candidate host was skipped for a model change.
    if not out and not (skipped and not checked) and (not seen or best < 3.0):
        raise Collecting(best, 3.0)
    return out


_TEMP_SERIES = (("gpu_temperature_c", "gpu_gpu_util_percent", "gpu", "GPU", 83.0),
                ("cpu_temp_c", "cpu_total", "cpu", "CPU", 95.0))


def _thermal_trend(d: CheckData) -> "list[Finding]":
    """Temperature creep once load is regressed out."""
    out, best, seen = [], 0.0, False
    for host in d.hosts():
        for temp_name, util_name, subject, label, limit in _TEMP_SERIES:
            temp = d.window("system", temp_name, host)
            util = d.window("system", util_name, host)
            if not temp or not util:
                continue
            seen = True
            best = max(best, d.span(temp))
            if d.span(temp) < 7.0:
                continue
            fit = fm.residual_trend(temp, util)
            if fit is None or fm.confidence(fit) == "low":
                continue
            rate = fit.slope_per_s * DAY
            if rate < 0.2:
                continue
            raw = fm.linear_fit(temp)
            secs = fm.time_to(raw, limit, d.now)
            detail = f"At the same load, {label} on {host} is {rate:.1f} °C/day warmer than it was."
            if secs is not None:
                detail += f" At this pace it reaches {limit:.0f} °C around {fmt_date(d.now + secs, d.tz_offset_s)}."
            out.append(Finding("thermal_trend", host, subject, severity_for(secs, cap="warning"),
                               f"{label} runs {rate:.1f} °C/day hotter at the same load", detail=detail,
                               since=temp[0][0], predicted_at=(d.now + secs) if secs is not None else None,
                               rate=rate, unit="°C/day", confidence=fm.confidence(fit),
                               suggested_action=f"Check fans, dust and airflow on {host}.",
                               graph=graph_spec("system", temp_name, host, d.start,
                                                d.now + secs if secs is not None else d.now, limit, raw)))
    if not out and (not seen or best < 7.0):
        raise Collecting(best, 7.0)
    return out


def _throughput(d: CheckData) -> "list[Finding]":
    """Generation speed falling away from its benchmark baseline."""
    out, best, seen = [], 0.0, False
    runs = d.bench_runs() or []
    for host in d.hosts():
        pts = [(t, v) for t, v in d.window("llama", "tokens_per_second", host, agg="max") if v > 0]
        if not pts:
            continue
        seen = True
        best = max(best, d.span(pts))
        if d.span(pts) < 3.0:
            continue
        fit = fm.linear_fit(pts)
        if fit is None or fit.slope_per_s >= 0:
            continue
        recent = [v for t, v in pts if t >= d.now - 3 * DAY]
        rows = [r for r in runs if r.get("host") == host and r.get("baseline") and r.get("ok") and r.get("gen_tps")]
        row = max(rows, key=lambda r: r.get("ts") or 0.0) if rows else None
        if row:
            base, word, subject = float(row["gen_tps"]), "baseline", row.get("model") or "llama"
            detail_base = f"a baseline of {base:.1f} tok/s"
        else:
            early = [v for t, v in pts if t <= pts[0][0] + 3 * DAY]
            base, word, subject = (sum(early) / len(early)) if early else 0.0, "earlier", "llama"
            detail_base = f"{base:.1f} tok/s over the first three days"
        if not recent or base <= 0:
            continue
        now_tps = sum(recent) / len(recent)
        if now_tps >= 0.9 * base:
            continue
        pct = (1.0 - now_tps / base) * 100.0
        out.append(Finding("throughput", host, subject, "warning" if now_tps < 0.75 * base else "info",
                           f"Generation speed {pct:.0f} % under the {word} level",
                           detail=f"The last three days average {now_tps:.1f} tok/s against {detail_base}.",
                           since=pts[0][0], rate=fit.slope_per_s * DAY, unit="tok/s/day",
                           confidence=fm.confidence(fit),
                           suggested_action=f"Re-run the benchmark on {host} and check for a model, quantisation or setting change.",
                           graph=graph_spec("llama", "tokens_per_second", host, d.start, d.now + TREND_AHEAD_S,
                                            base, fit, agg="max", limit=False)))
    if not out and (not seen or best < 3.0):
        raise Collecting(best, 3.0)
    return out


_SLOT_SERIES = (("llama", "requests_processing", "active_slots", "requests_deferred", "llama", "slots",
                 "Add a replica for the busiest model or raise parallel slots on {host}."),
                ("manager_streams", "stream_active", "stream_limit", "", "streams", "live-stream connections",
                 "Raise the stream limit or close idle dashboards."))


def _slot_pressure(d: CheckData) -> "list[Finding]":
    """Daily peak concurrency climbing towards the pool size."""
    out, best, seen = [], 0.0, False
    for host in d.hosts():
        for source, busy_name, pool_name, queue_name, subject, noun, action in _SLOT_SERIES:
            pts = d.window(source, busy_name, host, agg="max")
            pool = d.window(source, pool_name, host, agg="max")
            if not pts or not pool:
                continue
            seen = True
            best = max(best, d.span(pts))
            recent = [v for t, v in pool if t >= d.now - 3 * DAY] or [v for _, v in pool]
            slots = float(round(max(recent)))
            if d.span(pts) < 3.0 or slots < 1:
                continue
            peaks = _bucket_max(_bucket_max(pts, 3600.0), DAY)
            fit = fm.linear_fit(peaks)
            if fit is None or fit.slope_per_s <= 0:
                continue
            secs = fm.time_to(fit, slots, d.now)
            saturated = secs is None and fit.at(d.now) >= slots
            if not saturated and (secs is None or secs > 30 * DAY):
                continue
            queued = bool(queue_name) and any(
                v > 0 for t, v in d.window(source, queue_name, host, agg="max") if t >= d.now - DAY)
            if saturated:
                severity = "warning" if queued or not queue_name else "info"
            else:
                severity = _bump(severity_for(secs)) if queued else severity_for(secs)
            pool = f"the only {noun[:-1]}" if slots == 1 else f"all {slots:.0f} {noun}"
            verb = "is" if slots == 1 else "are"
            summary = (f"{pool[:1].upper()}{pool[1:]} {verb} already in use at peak times" if saturated
                       else f"{pool[:1].upper()}{pool[1:]} in use within {fmt_days(secs)}")
            detail = (f"Daily peak use on {host} has reached {pool}." if saturated
                      else f"Daily peak use on {host} is climbing towards {pool}.")
            if saturated and queued:
                detail += " Requests waited for a free one in the last day."
            out.append(Finding("slot_pressure", host, subject, severity, summary, detail=detail,
                               since=pts[0][0], predicted_at=None if saturated else d.now + secs,
                               rate=fit.slope_per_s * DAY, unit="per day", confidence=_daily_confidence(fit),
                               suggested_action=action.format(host=host),
                               graph=graph_spec(source, busy_name, host, d.start,
                                                d.now if saturated else d.now + secs, slots, fit, agg="max")))
    if not out and (not seen or best < 3.0):
        raise Collecting(best, 3.0)
    return out


def _f(value, default: float = 0.0) -> float:
    """Finite float from a reader field, else the default."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _rows(reader_result) -> "list[dict]":
    """Only the dict rows of a reader result."""
    return [r for r in (reader_result or []) if isinstance(r, dict)]


def _row_span(rows, key: str = "ts") -> float:
    """Days covered by a set of reader rows."""
    stamps = [t for t in (_f(r.get(key)) for r in rows) if t > 0]
    return (max(stamps) - min(stamps)) / DAY if len(stamps) >= 2 else 0.0


def _by_day(points, fn=sum) -> "dict[int, float]":
    """One value per UTC day index, folded with fn."""
    acc: "dict[int, list[float]]" = {}
    for t, v in points or []:
        acc.setdefault(int(t // DAY), []).append(v)
    return {k: _f(fn(vs)) for k, vs in acc.items()}


def _daily(points, fn=sum) -> "list[tuple[float, float]]":
    """Daily values stamped at the bucket centre, oldest first."""
    return [(k * DAY + DAY / 2.0, v) for k, v in sorted(_by_day(points, fn).items())]


def _mean(values) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def _median(values) -> float:
    vals = sorted(values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _ends(values, n: int = 3) -> "tuple[float, float]":
    """Means of the first and last n daily values."""
    vals = list(values)
    return _mean(vals[:n]), _mean(vals[-n:])


def _early_late(points, now: float) -> "tuple[list[float], list[float]]":
    """Values older than three days, and values within the last three days."""
    cut = now - 3 * DAY
    return [v for t, v in points or [] if t < cut], [v for t, v in points or [] if t >= cut]


def _month_bounds(now: float, tz_offset_s: float) -> "tuple[float, int, int]":
    """Start of the local month as epoch seconds, day of month, days in month."""
    local = datetime.fromtimestamp(float(now) + tz_offset_s, tz=timezone.utc)
    start = datetime(local.year, local.month, 1, tzinfo=timezone.utc).timestamp() - tz_offset_s
    return start, local.day, calendar.monthrange(local.year, local.month)[1]


MONTH_RATE_DAYS = 7.0


def _power_cost(d: CheckData) -> "list[Finding]":
    """Energy per 1k tokens drifting up per host, and this month's cost against last month's."""
    month_start, day_of_month, days_in_month = _month_bounds(d.now, d.tz_offset_s)
    rows = _rows(d.energy(min(d.start, month_start - 30 * DAY), d.now))
    out, per_host = [], {}
    for r in rows:
        ts, host = _f(r.get("ts")), r.get("host")
        if host and ts >= d.start:
            per_host.setdefault(host, []).append((ts, _f(r.get("wh")), _f(r.get("tokens"))))
    for host, items in sorted(per_host.items()):
        acc: "dict[int, list[float]]" = {}
        for ts, wh, tokens in items:
            day = acc.setdefault(int(ts // DAY), [0.0, 0.0])
            day[0] += wh
            day[1] += tokens
        pts = [(k * DAY + DAY / 2.0, wh / (tokens / 1000.0)) for k, (wh, tokens) in sorted(acc.items()) if tokens >= 1000.0]
        if len(pts) < 6:
            continue
        first, last = _ends([v for _, v in pts])
        fit = fm.linear_fit(pts)
        if first <= 0 or last < 1.2 * first or (fit is not None and fit.slope_per_s <= 0):
            continue
        pct = (last / first - 1.0) * 100.0
        out.append(Finding("power_cost", host, "efficiency", "info", f"Energy per 1k tokens up {pct:.0f} %",
                           detail=f"{host} used about {first:.0f} Wh per 1 000 tokens early in the window and about {last:.0f} Wh now.",
                           since=pts[0][0], rate=last - first, unit="Wh per 1k tokens",
                           confidence=_daily_confidence(fit),
                           suggested_action=f"Check what else runs on {host}, or re-run the benchmark to pick faster settings."))
    before = [r for r in rows if month_start - 30 * DAY <= _f(r.get("ts")) < month_start]
    known = {r.get("host") for r in before}
    month_rows = [r for r in rows if _f(r.get("ts")) >= month_start]
    this_month = [r for r in month_rows if r.get("host") in known]
    joined = sorted({str(r.get("host")) for r in month_rows if r.get("host") and r.get("host") not in known})
    mtd = sum(_f(r.get("wh")) for r in this_month) / 1000.0
    prev = sum(_f(r.get("wh")) for r in before) / 1000.0
    # Projects the month only from at least seven days in and five days of rows.
    if mtd > 0 and prev > 0 and day_of_month >= 7 and _row_span(this_month) >= 5.0:
        elapsed = max(1.0, (d.now - month_start) / DAY)
        recent_s = min(MONTH_RATE_DAYS, elapsed) * DAY
        recent = sum(_f(r.get("wh")) for r in this_month if _f(r.get("ts")) >= d.now - recent_s) / 1000.0
        projected = mtd + recent / (recent_s / DAY) * max(0.0, days_in_month - elapsed)
        ratio = projected / prev
        if ratio >= 1.25:
            cost, pct = projected * _f(d.price_kwh()), (ratio - 1.0) * 100.0
            detail = (f"{mtd:.1f} kWh in the first {day_of_month} days, and the last week's daily use for the rest, "
                      f"puts the month at about {projected:.1f} kWh against {prev:.1f} kWh before it.")
            if joined:
                detail += f" Hosts that joined this month are left out: {', '.join(joined[:3])}{' and more' if len(joined) > 3 else ''}."
            out.append(Finding("power_cost", None, "cost", "warning" if ratio >= 1.5 else "info",
                               f"This month's energy cost is heading for ${cost:.2f}, {pct:.0f} % over last month",
                               detail=detail, rate=projected, unit="kWh",
                               suggested_action="Look at which host gained the most, and at idle hours you could sleep through."))
    span = _row_span(rows)
    if not out and span < 7.0:
        raise Collecting(span, 7.0)
    return out


_GATEWAY_PREFIX = "gateway_requests_"
_GATEWAY_KINDS = ("errors", "timeouts", "failovers")


MIN_ERROR_BUCKETS = 12


def _share(bad, total) -> float:
    """Failed share of the requests in the same buckets, never over 100 %."""
    return min(1.0, bad / total) if total > 0 else 0.0


def _model_errors(d: CheckData) -> "list[Finding]":
    """Failures taking a rising share of a model's requests."""
    out, best, seen = [], 0.0, False
    for host in d.hosts():
        for name in d.names("forecast", host) or []:
            if not name.startswith(_GATEWAY_PREFIX):
                continue
            model = name[len(_GATEWAY_PREFIX):]
            pts = d.window("forecast", name, host)
            if not pts:
                continue
            seen = True
            best = max(best, d.span(pts))
            if d.span(pts) < 3.0:
                continue
            early_pts, late_pts = _early_late(pts, d.now)
            if sum(1 for v in late_pts if v > 0) < MIN_ERROR_BUCKETS:
                continue
            counts = {k: _early_late(d.window("forecast", f"gateway_{k}_{model}", host), d.now) for k in _GATEWAY_KINDS}
            late = _share(sum(counts["errors"][1]), sum(late_pts))
            early = (_share(sum(counts["errors"][0]), sum(early_pts))
                     if sum(1 for v in early_pts if v > 0) >= MIN_ERROR_BUCKETS else 0.0)
            if late < 0.05 and not (early > 0 and late >= 3 * early and late >= 0.02):
                continue
            timeouts, failovers = sum(counts["timeouts"][1]), sum(counts["failovers"][1])
            if timeouts or failovers:
                detail = "Mostly timeouts." if timeouts >= failovers else "Mostly failovers to another host."
            else:
                detail = "Requests to this model are failing outright."
            out.append(Finding("model_errors", host, model, "critical" if late >= 0.25 else "warning",
                               f"{model} errors rose from {early * 100:.0f} % to {late * 100:.0f} % of requests",
                               detail=detail,
                               since=pts[0][0], rate=late * 100.0, unit="% of requests",
                               suggested_action=f"Check the server answering for {model}, and route elsewhere while it recovers.",
                               graph=graph_spec("forecast", name, host, d.start, d.now)))
    if not out and (not seen or best < 3.0):
        raise Collecting(best, 3.0)
    return out


_CHURN_SERIES = ("model_loads", "model_unloads", "model_wakes")


def _model_churn(d: CheckData) -> "list[Finding]":
    """Model loads, unloads and wakes running well above their earlier rate."""
    out, best, seen = [], 0.0, False
    for host in d.hosts():
        pts: "list[tuple[float, float]]" = []
        for name in _CHURN_SERIES:
            pts += bucket_counts(d.window("forecast", name, host))
        if not pts:
            continue
        pts.sort()
        seen = True
        best = max(best, d.span(pts))
        if d.span(pts) < 7.0:
            continue
        daily = _daily(pts)
        if len(daily) < 6:
            continue
        first, last = _ends([v for _, v in daily])
        if first <= 0 or last < 2.0 * first or last < 10.0:
            continue
        fit = fm.rate_trend(daily)
        out.append(Finding("model_churn", host, "loads", "warning" if last >= 50.0 else "info",
                           f"Model loads and unloads doubled to {last:.0f} a day",
                           detail=f"{host} was changing models about {first:.0f} times a day and is now doing it {last:.0f} times.",
                           since=pts[0][0], rate=last, unit="per day",
                           confidence=_daily_confidence(fit),
                           suggested_action="Check Autopilot's pins and idle windows — it may be flapping.",
                           graph=graph_spec("forecast", "model_loads", host, d.start, d.now, fit=fit)))
    if not out and (not seen or best < 7.0):
        raise Collecting(best, 7.0)
    return out


ALERT_ROWS_MAX = 10000
PATTERNS_MAX = 5
FLAPPING_LINES = 5
COFIRE_S = 300.0
_LEVEL_WORDS = ("warning", "critical", "info", "high", "low")
_TEST_RULE = re.compile(r"\btest", re.I)


def _rule_key(rule: str) -> str:
    """Rule name without its severity words, so two thresholds on one metric read alike."""
    return " ".join(w for w in str(rule or "").lower().split() if w not in _LEVEL_WORDS)


def _per_host(ranked, host_of, cap: int = PATTERNS_MAX) -> list:
    """The first `cap` ranked items of every host, in rank order."""
    taken: "dict[str, int]" = {}
    out = []
    for item in ranked:
        host = str(host_of(item) or "")
        if taken.get(host, 0) < cap:
            taken[host] = taken.get(host, 0) + 1
            out.append(item)
    return out


def _cofire_candidates(rules: "dict[str, list[float]]", within_s: float = COFIRE_S) -> "list[tuple[str, str]]":
    """Sorted rule pairs with at least one alert within `within_s` of each other, found through time buckets."""
    index: "dict[int, set]" = {}
    for rule, times in rules.items():
        for t in times:
            index.setdefault(int(t // within_s), set()).add(rule)
    pairs: set = set()
    for k, here in index.items():
        near = here | index.get(k + 1, set())
        for a in here:
            pairs.update((a, b) if a < b else (b, a) for b in near if b != a)
    return sorted(pairs)


def _alarm_patterns(d: CheckData) -> "list[Finding]":
    """Alerts that repeat on a clock, clear themselves, travel in pairs or pile up."""
    rows = _rows(d.alerts(d.start, d.now))
    if len(rows) > ALERT_ROWS_MAX:
        rows = sorted(rows, key=lambda r: _f(r.get("created")))[-ALERT_ROWS_MAX:]
    groups: "dict[tuple[str, str], list[dict]]" = {}
    for r in rows:
        rule, host = str(r.get("rule") or ""), str(r.get("host") or "")
        if rule:
            groups.setdefault((rule, host), []).append(r)
    out: "list[Finding]" = []
    # Ranking tuples: periodic (-share, -count, rule, host, hour, count, first), flapping (-count, rule, count, first).
    periodic: "list[tuple]" = []
    flapping: "dict[str, list[tuple]]" = {}
    metrics: "dict[tuple[str, str], set]" = {}
    for (rule, host), items in sorted(groups.items()):
        created = sorted(_f(r.get("created")) for r in items)
        metrics[(rule, host)] = {str(r.get("metric")) for r in items if r.get("metric")}
        hour = fm.periodic(created, d.tz_offset_s)
        if hour is not None and not _TEST_RULE.search(rule):
            share = fm.hour_histogram(created, d.tz_offset_s)[hour] / len(created)
            periodic.append((-share, -len(created), rule, host, hour, len(created), created[0]))
        lives = sorted(_f(r.get("closed")) - _f(r.get("created")) for r in items if r.get("closed") is not None)
        lives = [s for s in lives if s >= 0]
        if len(lives) >= 5 and _median(lives) < 300.0:
            flapping.setdefault(host, []).append((-len(lives), rule, len(lives), created[0]))
    for _, _, rule, host, hour, count, first in _per_host(sorted(periodic), lambda t: t[3]):
        out.append(Finding("alarm_patterns", host or None, f"periodic:{rule}", "info",
                           f"“{rule}” fires around {hour:02d}:00 most days",
                           detail=f"{count} alerts, nearly all in the same hour of the day.",
                           since=first, suggested_action=f"Look for a job or backup that runs around {hour:02d}:00."))
    for host, items in sorted(flapping.items()):
        items.sort()
        where = f" on {host}" if host else ""
        top_rule, top_count = items[0][1], items[0][2]
        n = len(items)
        summary = (f"“{top_rule}”{where} clears itself within minutes — {top_count} times" if n == 1
                   else f"{n} alert rules{where} clear themselves within minutes — most often “{top_rule}” ({top_count} times)")
        lines = [f"“{rule}” × {count}" for _, rule, count, _ in items[:FLAPPING_LINES]]
        if n > FLAPPING_LINES:
            lines.append(f"and {n - FLAPPING_LINES} more")
        out.append(Finding("alarm_patterns", host or None, "flapping", "info", summary,
                           detail="\n".join(lines), since=min(first for _, _, _, first in items),
                           rate=float(sum(count for _, _, count, _ in items)), unit="alerts",
                           suggested_action="Give these rules a longer window so short blips stop paging."))
    by_host: "dict[str, dict[str, list[float]]]" = {}
    for (rule, host), items in groups.items():
        by_host.setdefault(host, {})[rule] = sorted(_f(r.get("created")) for r in items)
    pairs: "list[tuple]" = []
    for host, rules in sorted(by_host.items()):
        if not host:
            continue
        for a, b in _cofire_candidates({r: t for r, t in rules.items() if len(t) >= 5}):
            if _rule_key(a) == _rule_key(b) or (metrics.get((a, host), set()) & metrics.get((b, host), set())):
                continue
            share = min(fm.cofire(rules[a], rules[b]), fm.cofire(rules[b], rules[a]))
            if share >= 0.8:
                pairs.append((-share, host, a, b))
        stamps = [t for times in rules.values() for t in times]
        this_week = sum(1 for t in stamps if t >= d.now - 7 * DAY)
        last_week = sum(1 for t in stamps if d.now - 14 * DAY <= t < d.now - 7 * DAY)
        if last_week >= 1 and this_week >= 10 and this_week >= 2 * last_week:
            out.append(Finding("alarm_patterns", host, "rising", "warning",
                               f"Alerts on {host} doubled this week ({this_week})",
                               detail=f"{this_week} alerts in the last seven days against {last_week} the week before.",
                               since=d.now - 14 * DAY, rate=float(this_week), unit="alerts per week",
                               suggested_action=f"Look at what changed on {host} this week."))
    for _, host, a, b in _per_host(sorted(pairs), lambda t: t[1]):
        times = by_host[host]
        out.append(Finding("alarm_patterns", host or None, f"cofire:{a}+{b}", "info",
                           f"“{a}” and “{b}” always fire together",
                           detail=f"{len(times[a])} and {len(times[b])} alerts on {host or 'this host'}, within five minutes of each other every time.",
                           since=min(times[a][0], times[b][0]),
                           suggested_action="Keep whichever of the two is more useful and silence the other."))
    span = _row_span(rows, "created")
    if not out and (not rows or span < 7.0):
        raise Collecting(span, 7.0)
    return out


def _agent_health(d: CheckData) -> "list[Finding]":
    """Heartbeat gaps, version drift and clock skew on the agents."""
    out, best, seen = [], 0.0, False
    rows = _rows(d.agents())
    latest = str(d.latest_agent_version() or "")
    for host in d.hosts():
        gaps = d.window("forecast", "agent_heartbeat_gap_s", host, agg="max")
        if gaps:
            seen = True
            best = max(best, d.span(gaps))
            daily = _daily([(t, 1.0 if v > 90.0 else 0.0) for t, v in gaps])
            if d.span(gaps) >= 3.0 and len(daily) >= 6:
                first, last = _ends([v for _, v in daily])
                fit = fm.rate_trend(daily)
                if last >= 5.0 and last > first and (fit is None or fit.slope_per_s > 0):
                    out.append(Finding("agent_health", host, "heartbeat", "warning",
                                       f"Heartbeat gaps rising — on {last:.0f} occasions a day",
                                       detail=f"{host} went quiet for more than 90 seconds about {last:.0f} times a day, up from {first:.0f}.",
                                       since=gaps[0][0], rate=last, unit="per day",
                                       confidence=_daily_confidence(fit),
                                       suggested_action=f"Check the network and the agent service on {host}.",
                                       graph=graph_spec("forecast", "agent_heartbeat_gap_s", host, d.start, d.now)))
        skew = d.window("forecast", "agent_clock_skew_s", host)
        if skew:
            seen = True
            best = max(best, d.span(skew))
            recent = [abs(v) for t, v in skew if t >= d.now - DAY]
            if recent and _median(recent) > 5.0:
                out.append(Finding("agent_health", host, "clock", "warning", f"Clock is {_median(recent):.0f} s off",
                                   detail=f"The clock on {host} disagrees with the manager, which smears its readings across time.",
                                   since=skew[0][0], rate=_median(recent), unit="s",
                                   suggested_action=f"Turn on time sync on {host}.",
                                   graph=graph_spec("forecast", "agent_clock_skew_s", host, d.start, d.now)))
    for r in rows:
        host, version = r.get("host"), str(r.get("version") or "")
        if host and version and latest and version != latest:
            out.append(Finding("agent_health", host, "version", "info", f"Agent is on {version}, latest is {latest}",
                               detail=f"{host} has not been updated with the rest of the machines.",
                               confidence="high", suggested_action=f"Update the agent on {host} from the Agents tab."))
    if not out and not rows and (not seen or best < 3.0):
        raise Collecting(best, 3.0)
    return out


_LATENCY_SERIES = (("manager_api_latency_ms", "Manager API responses are slowing"),
                   ("ae_health_latency_ms", "Alarm engine checks are slowing"),
                   ("influx_query_5m_latency_ms", "History queries are slowing"))
_DB_LABELS = {"manager": "manager database", "audit": "audit database", "energy": "energy database",
              "wal": "database journal"}


def _growth_pct(d: CheckData, pts) -> "tuple[Optional[fm.Fit], float]":
    """Fit of a series and its growth as a percentage of today's value per day."""
    fit = fm.linear_fit(pts)
    level = fit.at(d.now) if fit else 0.0
    return fit, (fit.slope_per_s * DAY / level * 100.0) if fit and level > 0 else 0.0


def _service_health(d: CheckData) -> "list[Finding]":
    """Latency, backlog, database growth, backup age, log errors and process memory on the manager."""
    out, best, seen = [], 0.0, False
    for host in d.hosts():
        for name, label in _LATENCY_SERIES:
            pts = d.window("manager_self_monitor", name, host, agg="max")
            if not pts:
                continue
            seen = True
            best = max(best, d.span(pts))
            daily = _daily(pts, fn=lambda vs: _f(fm.percentile(vs, 95)))
            if d.span(pts) < 3.0 or len(daily) < 6:
                continue
            first, last = _ends([v for _, v in daily])
            fit = fm.linear_fit(daily)
            if first <= 0 or last < 1.5 * first or last < 200.0 or (fit is not None and fit.slope_per_s <= 0):
                continue
            out.append(Finding("service_health", host, name, "warning",
                               f"{label} — daily peaks {first:.0f} → {last:.0f} ms",
                               detail="The slowest responses of the day take much longer than they did.",
                               since=pts[0][0], rate=last - first, unit="ms",
                               confidence=_daily_confidence(fit),
                               suggested_action="Check load and disk speed on the manager, then restart it during a quiet hour.",
                               graph=graph_spec("manager_self_monitor", name, host, d.start, d.now + TREND_AHEAD_S,
                                                fit=fit, agg="max")))
        backlog = d.window("manager_streams", "worker_backlog", host)
        if backlog:
            seen = True
            best = max(best, d.span(backlog))
            busy = sum(1 for _, v in _daily([p for p in backlog if p[0] >= d.now - 7 * DAY], fn=max) if v > 0)
            if busy >= 3:
                out.append(Finding("service_health", host, "backlog", "warning",
                                   f"Work is queuing up — a backlog on {busy} of the last 7 days",
                                   detail="Requests waited for a free worker instead of starting straight away.",
                                   since=backlog[0][0], rate=float(busy), unit="days",
                                   suggested_action="Raise the worker count, or close dashboards that hold a live connection open.",
                                   graph=graph_spec("manager_streams", "worker_backlog", host, d.start, d.now)))
        for name in d.names("forecast", host) or []:
            if not (name.startswith("db_") and name.endswith("_bytes")):
                continue
            pts = d.window("forecast", name, host)
            if not pts:
                continue
            seen = True
            best = max(best, d.span(pts))
            fit, pct = _growth_pct(d, pts)
            if d.span(pts) < 3.0 or pct < 5.0:
                continue
            key = name[len("db_"):-len("_bytes")]
            label = _DB_LABELS.get(key, f"{key} database")
            out.append(Finding("service_health", host, f"db:{key}", "info", f"The {label} is growing {pct:.0f} % a day",
                               detail=f"It holds about {fit.at(d.now) / 1e9:.1f} GB today.",
                               since=pts[0][0], rate=pct, unit="%/day", confidence=fm.confidence(fit),
                               suggested_action="Shorten how long history is kept, or give the manager more disk.",
                               graph=graph_spec("forecast", name, host, d.start, d.now + TREND_AHEAD_S, fit=fit)))
        backup = d.window("forecast", "backup_age_s", host)
        if backup:
            seen = True
            best = max(best, d.span(backup))
            age = backup[-1][1]
            if age > 2 * DAY:
                out.append(Finding("service_health", host, "backup", "warning", f"No backup for {fmt_days(age)}",
                                   detail="The newest backup is older than the two days the schedule allows.",
                                   since=backup[0][0], rate=age / DAY, unit="days", confidence="high",
                                   suggested_action="Run a backup now and check the backup schedule."))
        errors = d.window("forecast", "log_errors", host)
        if errors:
            seen = True
            best = max(best, d.span(errors))
            daily = _daily(bucket_counts(errors))
            first, last = _ends([v for _, v in daily])
            if len(daily) >= 6 and first > 0 and last >= 2.0 * first and last >= 20.0:
                out.append(Finding("service_health", host, "log_errors", "info",
                                   f"Errors in the log doubled to {last:.0f} a day",
                                   detail=f"The log carried about {first:.0f} errors a day earlier in the window.",
                                   since=errors[0][0], rate=last, unit="per day",
                                   suggested_action="Read the service log for the repeated message and fix what causes it.",
                                   graph=graph_spec("forecast", "log_errors", host, d.start, d.now)))
        for name in d.names("processes", host) or []:
            if not name.endswith("_rss_mb"):
                continue
            pts = d.window("processes", name, host)
            if not pts:
                continue
            seen = True
            best = max(best, d.span(pts))
            fit, pct = _growth_pct(d, pts)
            if d.span(pts) < 7.0 or pct < 5.0:
                continue
            svc = name[:-len("_rss_mb")]
            label = svc.replace("_", " ")
            out.append(Finding("service_health", host, f"rss:{svc}", "warning",
                               f"{label[:1].upper()}{label[1:]} memory grows {pct:.0f} % a day",
                               detail=f"It holds about {fit.at(d.now):.0f} MB today and has climbed every day this week.",
                               since=pts[0][0], rate=pct, unit="%/day", confidence=fm.confidence(fit),
                               suggested_action=f"Restart {label} during a quiet hour, then watch whether the climb returns.",
                               graph=graph_spec("processes", name, host, d.start, d.now + TREND_AHEAD_S, fit=fit)))
    if not out and (not seen or best < 3.0):
        raise Collecting(best, 3.0)
    return out


def _bench_outcomes(d: CheckData) -> "list[Finding]":
    """Draft accept rate and report-card generation speed slipping for a model."""
    out, runs, cards = [], {}, {}
    for r in _rows(d.bench_runs()):
        if r.get("ok") and r.get("accept_rate") is not None and r.get("host") and r.get("model"):
            runs.setdefault((r["host"], r["model"]), []).append((_f(r.get("ts")), _f(r.get("accept_rate"))))
    for r in _rows(d.report_cards()):
        if r.get("tok_s") is not None and r.get("host") and r.get("model"):
            cards.setdefault((r["host"], r["model"]), []).append((_f(r.get("ts")), _f(r.get("tok_s"))))
    for (host, model), items in sorted(runs.items()):
        if len(items) < 3:
            continue
        items.sort()
        newest, base = items[-1][1], _median([v for _, v in items[:-1]])
        if base <= 0 or newest >= 0.85 * base:
            continue
        pct = newest * 100.0 if newest <= 1.0 else newest
        out.append(Finding("bench_outcomes", host, f"accept:{model}", "info",
                           f"Draft accept rate for {model} fell to {pct:.0f} %",
                           detail=f"Earlier runs on {host} accepted about {(base * 100.0 if base <= 1.0 else base):.0f} % of the draft tokens.",
                           since=items[0][0], rate=pct, unit="%",
                           suggested_action=f"Re-run the draft benchmark on {host}, or pick a draft model closer to {model}."))
    for (host, model), items in sorted(cards.items()):
        if len(items) < 2:
            continue
        items.sort()
        newest, best = items[-1][1], max(v for _, v in items[:-1])
        if best <= 0 or newest > best * 0.9:
            continue
        out.append(Finding("bench_outcomes", host, f"score:{model}", "info",
                           f"Report card speed for {model} dropped from {best:.0f} to {newest:.0f} tok/s",
                           detail=f"The newest report card on {host} generates well under its best speed.",
                           since=items[0][0], predicted_at=None, rate=newest - best, unit="tok/s",
                           suggested_action=f"Open the report card for {model} and re-run it on {host} to see whether the speed holds."))
    return out


def _capacity(d: CheckData) -> "list[Finding]":
    """Hosts whose pinned models stop fitting as other memory use grows."""
    out, best, seen = [], 0.0, False
    pinned: "dict[str, float]" = {}
    for r in _rows(d.catalog()):
        if r.get("pinned") and r.get("host"):
            pinned[r["host"]] = pinned.get(r["host"], 0.0) + _f(r.get("size_gb"))
    memory = d.host_mem() or {}
    for host, needed in sorted(pinned.items()):
        info = memory.get(host) if isinstance(memory, dict) else None
        info = info if isinstance(info, dict) else {}
        vram = _f(info.get("vram_gb"))
        capacity = vram if vram > 0 else _f(info.get("ram_gb"))
        name = "gpu_vram_usage_percent" if vram > 0 else "ram_percent"
        if capacity <= 0 or needed <= 0:
            continue
        pts = d.window("system", name, host)
        if not pts:
            continue
        seen = True
        best = max(best, d.span(pts))
        if d.span(pts) < 3.0:
            continue
        fit = fm.linear_fit(pts)
        if fit is None or fit.slope_per_s <= 0 or fm.confidence(fit) == "low":
            continue
        other = max(0.0, capacity * fit.at(d.now) / 100.0 - needed)
        free, growth = capacity - needed - other, capacity * fit.slope_per_s * DAY / 100.0
        if growth <= 0:
            continue
        secs = max(0.0, free) / growth * DAY
        if secs > 30 * DAY:
            continue
        summary = (f"{host} cannot hold its pinned models any more" if free <= 0
                   else f"{host} cannot hold its pinned models in {fmt_days(secs)}")
        out.append(Finding("capacity", host, "pinned", severity_for(secs), summary,
                           detail=f"Its pinned models need {needed:.0f} GB of the {capacity:.0f} GB on {host}, and everything else is using more each day.",
                           since=pts[0][0], predicted_at=d.now + secs,
                           rate=growth, unit="GB/day", confidence=fm.confidence(fit),
                           suggested_action=f"Unpin a model on {host}, move one elsewhere, or free the memory the rest is holding.",
                           graph=graph_spec("system", name, host, d.start, d.now + secs, fit=fit)))
    if not out and (not seen or best < 3.0):
        raise Collecting(best, 3.0)
    return out


IDLE_MIN_KWH = 0.5


def _idle_waste(d: CheckData) -> "list[Finding]":
    """Hosts awake and drawing power with no requests to serve."""
    rows = _rows(d.energy(d.start, d.now))
    per_host: "dict[str, list[float]]" = {}
    for r in rows:
        host, ts = r.get("host"), _f(r.get("ts"))
        wh = _f(r.get("wh"))
        if not host or ts < d.now - 7 * DAY or _f(r.get("observed_s")) < 3000.0 or _f(r.get("tokens")) != 0 or wh <= 0:
            continue
        acc = per_host.setdefault(host, [0.0, 0.0])
        acc[0] += 1.0
        acc[1] += wh
    price, out = _f(d.price_kwh()), []
    for host, (hours, wh) in sorted(per_host.items()):
        kwh = wh / 1000.0
        if hours < 40 or kwh < IDLE_MIN_KWH:
            continue
        out.append(Finding("idle_waste", host, "idle", "info",
                           f"Awake {hours:.0f} h this week with no requests — about {kwh:.1f} kWh (${kwh * price:.2f})",
                           detail=f"{host} was powered up and served nothing for {hours:.0f} of the last 168 hours.",
                           since=d.now - 7 * DAY, rate=kwh, unit="kWh per week",
                           suggested_action=f"Shorten idle-sleep on {host}."))
    span = _row_span(rows)
    if not out and span < 7.0:
        raise Collecting(span, 7.0)
    return out


def _weekly_digest(d: CheckData) -> "list[Finding]":
    """The week's three most pressing open findings; never raises Collecting."""
    rows = [r for r in _rows(d.findings()) if str(r.get("verified") or "") != "model"]
    rows.sort(key=lambda r: (-_RANK.get(str(r.get("severity") or ""), 0), _f(r.get("predicted_at"), math.inf)))
    local = datetime.fromtimestamp(d.now + d.tz_offset_s, tz=timezone.utc).isocalendar()
    lines = [f"{r.get('host') or 'All hosts'}: {r.get('summary') or ''}" for r in rows[:3]]
    count = sum(1 for r in rows if _RANK.get(str(r.get("severity") or ""), 0) >= _RANK["warning"])
    notes = len(rows) - count
    summary = ("Nothing needs attention this week" if not count
               else f"{count} thing{' needs' if count == 1 else 's need'} attention this week")
    if notes:
        summary += f" — plus {notes} note{'' if notes == 1 else 's'}"
    summary += f" (counted {fmt_weekday(d.now, d.tz_offset_s)})"
    return [Finding("weekly_digest", None, f"{local[0]}-W{local[1]:02d}", "info", summary,
                    detail="\n".join(lines), since=d.now, confidence="high",
                    suggested_action="Open the Forecast tab for the full list." if rows else "")]


CHECKS: "list[Check]" = [
    Check("disk_fill", "Disk fill", _disk_fill), Check("load_shift", "Load shift", _load_shift),
    Check("memory_headroom", "Memory headroom", _memory_headroom), Check("thermal_trend", "Thermal trend", _thermal_trend, min_days=7.0),
    Check("power_cost", "Power and cost", _power_cost, min_days=7.0), Check("throughput", "Throughput", _throughput),
    Check("model_errors", "Model errors", _model_errors), Check("slot_pressure", "Slot pressure", _slot_pressure),
    Check("model_churn", "Model churn", _model_churn, min_days=7.0), Check("alarm_patterns", "Alarm patterns", _alarm_patterns, min_days=7.0),
    Check("agent_health", "Agent health", _agent_health), Check("service_health", "Service health", _service_health),
    Check("bench_outcomes", "Bench outcomes", _bench_outcomes, min_days=0.0), Check("capacity", "Capacity", _capacity),
    Check("idle_waste", "Idle waste", _idle_waste, min_days=7.0),
    Check("weekly_digest", "Weekly digest", _weekly_digest, min_days=0.0, tower=False),
]
BY_ID = {c.id: c for c in CHECKS}
IDS = tuple(c.id for c in CHECKS)
TITLES = {c.id: c.title for c in CHECKS}
