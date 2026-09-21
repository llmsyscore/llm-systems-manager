"""Test double for forecast_checks.CheckData readers (#1031)."""
import math

import forecast_checks as fc

DAY = 86400.0
NOW = 1_790_000_000.0


def ramp(days=14, per_day=1.0, start=78.0, step_s=3600.0, noise=0.0, end=NOW):
    n = int(days * DAY / step_s)
    t0 = end - n * step_s
    return [(t0 + i * step_s, start + per_day * (i * step_s / DAY) + noise * math.sin(i * 1.7)) for i in range(n + 1)]


def flat(value, days=14, step_s=3600.0, noise=0.0, end=NOW):
    return ramp(days, 0.0, value, step_s, noise, end)


def data(series=None, names=None, hosts=("rig",), **readers):
    """Readers over a {(source, name, host): points} map; an `agg`-keyed entry wins when that agg is asked for."""
    series, names = dict(series or {}), dict(names or {})

    def _series(source, name, host, start=None, end=None, points=720, agg="mean"):
        pts = series.get((source, name, host, agg), series.get((source, name, host), []))
        return [(t, v) for t, v in pts if (start is None or t >= start) and (end is None or t <= end)]

    def _names(source, host):
        return names.get((source, host)) or sorted({k[1] for k in series if k[0] == source and k[2] == host})

    base = dict(alerts=lambda s, e: [], energy=lambda s, e: [], bench_runs=lambda: [], report_cards=lambda: [],
                audit=lambda a, s, e: [], catalog=lambda: [], host_mem=lambda: {}, agents=lambda: [],
                mounts=lambda h: {},
                latest_agent_version=lambda: "", price_kwh=lambda: 0.15, findings=lambda: [])
    base.update(readers)
    return fc.CheckData(now=NOW, window_s=14 * DAY, series=_series, names=_names, hosts=lambda: list(hosts), **base)


def run(check_id, d):
    return fc.BY_ID[check_id].detect(d)
