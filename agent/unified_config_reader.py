# agent/unified_config_reader.py
"""Read the unified-config TOML's [influxdb] section, returning InfluxDB
connection settings for the agent's self-monitor probe."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore

DEFAULT_TOML_PATH = "/opt/llm-systems-manager/config/llm-systems.toml"


def resolve_unified_config_path(path_override: str = "") -> Optional[Path]:
    """Resolve the TOML path: $LLM_SYSTEMS_CONFIG → path_override → default.
    A set env var is honored absolutely, returning the file or None."""
    env = os.environ.get("LLM_SYSTEMS_CONFIG")
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    if path_override:
        p = Path(path_override).expanduser()
        return p if p.is_file() else None
    p = Path(DEFAULT_TOML_PATH)
    return p if p.is_file() else None


def read_influx_settings(path_override: str = "") -> Optional[dict]:
    """Parse [influxdb] from the unified-config TOML; None if unavailable."""
    if tomllib is None:
        return None
    p = resolve_unified_config_path(path_override)
    if p is None:
        return None
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    influx = data.get("influxdb")
    if not isinstance(influx, dict):
        return None
    tokens = influx.get("tokens") if isinstance(influx.get("tokens"), dict) else {}
    metrics_bucket = influx.get("metrics_bucket", "")
    return {
        "host": influx.get("host", "localhost"),
        "port": int(influx.get("port", 8086) or 8086),
        "org": influx.get("org", "llm-systems-manager"),
        "metrics_bucket": metrics_bucket,
        "metrics_rollup_bucket": influx.get("metrics_rollup_bucket") or metrics_bucket,
        "token": tokens.get("metrics", "") or "",
        "rollup_token": tokens.get("metrics_rollup", "") or "",
    }
