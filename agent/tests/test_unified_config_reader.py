# agent/tests/test_unified_config_reader.py
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PY = _AGENT_ROOT / "unified_config_reader.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ucr = _load("unified_config_reader", _MODULE_PY)

_FULL_TOML = """
[influxdb]
host = "10.0.0.5"
port = 8086
org  = "llm-systems-manager"
metrics_bucket = "alarm_engine_metrics"
metrics_rollup_bucket = "alarm_engine_metrics_rollup"

[influxdb.tokens]
metrics = "secret-token"
metrics_rollup = "rollup-token"
"""


def test_read_full(tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text(_FULL_TOML)
    cfg = ucr.read_influx_settings(str(p))
    assert cfg == {
        "host": "10.0.0.5",
        "port": 8086,
        "org": "llm-systems-manager",
        "metrics_bucket": "alarm_engine_metrics",
        "metrics_rollup_bucket": "alarm_engine_metrics_rollup",
        "token": "secret-token",
        "rollup_token": "rollup-token",
    }


def test_partial_config_mirrors_unified_defaults(tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text('[influxdb]\nmetrics_bucket="b"\n[influxdb.tokens]\nmetrics="t"\n')
    cfg = ucr.read_influx_settings(str(p))
    assert cfg["host"] == "localhost"
    assert cfg["org"] == "llm-systems-manager"
    assert cfg["port"] == 8086


def test_explicit_empty_host_org_preserved(tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text('[influxdb]\nhost=""\norg=""\nmetrics_bucket="b"\n')
    cfg = ucr.read_influx_settings(str(p))
    assert cfg["host"] == ""
    assert cfg["org"] == ""


def test_rollup_token_absent_is_empty(tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text(
        '[influxdb]\nhost="h"\nmetrics_bucket="b"\n[influxdb.tokens]\nmetrics="t"\n'
    )
    cfg = ucr.read_influx_settings(str(p))
    assert cfg["rollup_token"] == ""


def test_missing_file_returns_none(tmp_path):
    assert ucr.read_influx_settings(str(tmp_path / "nope.toml")) is None


def test_no_toml_parser_returns_none(tmp_path, monkeypatch):
    p = tmp_path / "llm-systems.toml"
    p.write_text(_FULL_TOML)
    monkeypatch.setattr(ucr, "tomllib", None)
    assert ucr.read_influx_settings(str(p)) is None


def test_no_influx_table_returns_none(tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text("[manager]\nport = 5000\n")
    assert ucr.read_influx_settings(str(p)) is None


def test_rollup_falls_back_to_metrics_bucket(tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text(
        '[influxdb]\nhost="h"\nmetrics_bucket="b"\n[influxdb.tokens]\nmetrics="t"\n'
    )
    cfg = ucr.read_influx_settings(str(p))
    assert cfg["metrics_rollup_bucket"] == "b"


def test_cached_returns_same_object_until_mtime_changes(tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text(_FULL_TOML)
    import os
    os.utime(p, (1_000_000, 1_000_000))
    c1 = ucr.read_influx_settings_cached(str(p))
    c2 = ucr.read_influx_settings_cached(str(p))
    assert c1 is c2  # cache hit: no re-parse, same object
    assert c1["host"] == "10.0.0.5"
    # Rewrite with new content + newer mtime -> cache invalidates, re-reads.
    p.write_text(_FULL_TOML.replace("10.0.0.5", "10.0.0.9"))
    os.utime(p, (2_000_000, 2_000_000))
    c3 = ucr.read_influx_settings_cached(str(p))
    assert c3 is not c1
    assert c3["host"] == "10.0.0.9"


def test_cached_missing_file_returns_none(tmp_path):
    assert ucr.read_influx_settings_cached(str(tmp_path / "nope.toml")) is None


def test_cached_matches_uncached(tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text(_FULL_TOML)
    assert ucr.read_influx_settings_cached(str(p)) == ucr.read_influx_settings(str(p))


def test_env_path_wins(tmp_path, monkeypatch):
    envp = tmp_path / "env.toml"
    envp.write_text(_FULL_TOML)
    monkeypatch.setenv("LLM_SYSTEMS_CONFIG", str(envp))
    assert ucr.resolve_unified_config_path("/does/not/exist.toml") == envp


def test_override_used_when_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_SYSTEMS_CONFIG", raising=False)
    p = tmp_path / "ovr.toml"
    p.write_text(_FULL_TOML)
    assert ucr.resolve_unified_config_path(str(p)) == p
