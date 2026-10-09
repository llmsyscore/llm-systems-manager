"""#1246: the InfluxDB 5m and 24h probes read one series each (newest 10
system/cpu_total points), and the 24h probe uses the rollup measurement."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_AGENT = Path(__file__).resolve().parents[1]
SRC = (_AGENT / "llm-systems-agent.py").read_text()


def _extract(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}()"
    return m.group(0)


def _flux():
    ns: dict = {}
    exec(_extract("_influx_probe_flux"), ns)
    return ns["_influx_probe_flux"]


def test_probe_reads_one_series_newest_first():
    q = _flux()("raw_b", "metrics", "-5m")
    assert 'from(bucket: "raw_b")' in q and "range(start: -5m)" in q
    assert 'r._measurement == "metrics"' in q
    assert 'r.source == "system"' in q and 'r.metric_name == "cpu_total"' in q
    assert 'r._field == "value"' in q
    assert q.index('sort(columns: ["_time"], desc: true)') < q.index("limit(n: 10)")


def test_24h_probe_uses_the_rollup_measurement():
    body = _extract("_probe_influxdb")
    assert '_influx_probe_flux(bucket, "metrics", "-5m")' in body
    assert 'rollup_measurement if use_rollup else "metrics", "-24h"' in body
    assert 'cfg.get("rollup_measurement") or "metrics_1m"' in body


def _reader():
    spec = importlib.util.spec_from_file_location("ucr", _AGENT / "unified_config_reader.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reader_exposes_the_rollup_measurement(tmp_path):
    ucr = _reader()
    p = tmp_path / "llm-systems.toml"
    p.write_text('[influxdb]\nmetrics_bucket="b"\n[influxdb.tokens]\nmetrics="t"\n')
    assert ucr.read_influx_settings(str(p))["rollup_measurement"] == "metrics_1m"
    p.write_text('[influxdb]\nmetrics_bucket="b"\n[influxdb.tokens]\nmetrics="t"\n'
                 '[alarm_engine.history.downsampling]\nrollup_measurement="m_custom"\n')
    assert ucr.read_influx_settings(str(p))["rollup_measurement"] == "m_custom"


def test_reader_tolerates_a_malformed_alarm_engine_section(tmp_path):
    ucr = _reader()
    p = tmp_path / "llm-systems.toml"
    p.write_text('[influxdb]\nmetrics_bucket="b"\n[influxdb.tokens]\nmetrics="t"\n'
                 'alarm_engine="x"\n')
    assert ucr.read_influx_settings(str(p))["rollup_measurement"] == "metrics_1m"
    p.write_text('[influxdb]\nmetrics_bucket="b"\n[influxdb.tokens]\nmetrics="t"\n'
                 '[alarm_engine]\nhistory="x"\n')
    assert ucr.read_influx_settings(str(p))["rollup_measurement"] == "metrics_1m"
