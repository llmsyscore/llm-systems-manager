"""#1010/#1290: the TOML reference and slim example must track config/unified_config.py.example."""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tomllib
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
from pydantic import BaseModel

_REPO = Path(__file__).resolve().parents[2]
_PY_EXAMPLE = _REPO / "config" / "unified_config.py.example"
_TOML_REFERENCE = _REPO / "config" / "llm-systems.toml.reference"
_TOML_EXAMPLE = _REPO / "config" / "llm-systems.toml.example"

# TOML files ship a placeholder here on purpose; the model default is blank.
_PLACEHOLDERS = {
    "alarm_engine.ingest_token",
    "influxdb.tokens.metrics",
    "influxdb.tokens.metrics_rollup",
    "notifications.smtp.server",
    "notifications.smtp.user",
    "notifications.smtp.password",
}

# Shipped value intentionally differs from the model default.
_SHIPPED_OVERRIDES = {
    "manager.security.admin_cidrs": "installer swaps the placeholder /24 for the detected LAN",
}

# Keys the slim install template holds; everything else runs on its default.
SLIM_KEYS = frozenset({
    "manager.host", "manager.port", "manager.tls_port",
    "manager.tls_cert_file", "manager.tls_key_file",
    "manager.alarm_engine_url",
    "manager.agents.enrollment_secret",
    "manager.security.admin_cidrs",
    "manager.auth.username", "manager.auth.password_hash",
    "manager.telemetry.install_report", "manager.telemetry.crash_reports", "manager.telemetry.checkin",
    "manager.telemetry.heartbeat",
    "alarm_engine.host", "alarm_engine.port", "alarm_engine.tls_enabled",
    "alarm_engine.manager_url", "alarm_engine.cors_origins",
    "alarm_engine.ingest_token", "alarm_engine.management_token",
    "influxdb.host", "influxdb.port", "influxdb.org",
    "influxdb.metrics_bucket", "influxdb.metrics_rollup_bucket",
    "influxdb.tokens.metrics", "influxdb.tokens.metrics_rollup", "influxdb.tokens.admin",
    "notifications.smtp.server", "notifications.smtp.port",
    "notifications.smtp.user", "notifications.smtp.password",
    "notifications.twilio.account_sid", "notifications.twilio.auth_token", "notifications.twilio.from_number",
    "notifications.discord.webhook_url",
    "paths.log_dir",
    "logging.level",
})


@pytest.fixture(scope="module")
def example_settings(tmp_path_factory):
    # Load the tracked .example with no TOML so every value is a model default.
    missing = tmp_path_factory.mktemp("cfg") / "absent.toml"
    saved = os.environ.get("LLM_SYSTEMS_CONFIG")
    os.environ["LLM_SYSTEMS_CONFIG"] = str(missing)
    name = "unified_config_example_1010"
    try:
        loader = SourceFileLoader(name, str(_PY_EXAMPLE))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        sys.modules[name] = mod
        loader.exec_module(mod)
        assert mod.CONFIG_PATH is None
        yield mod.Settings()
    finally:
        sys.modules.pop(name, None)
        if saved is None:
            os.environ.pop("LLM_SYSTEMS_CONFIG", None)
        else:
            os.environ["LLM_SYSTEMS_CONFIG"] = saved


def _load_toml(path: Path):
    text = path.read_text(encoding="utf-8")
    return tomllib.loads(text), text


@pytest.fixture(scope="module")
def toml_reference():
    return _load_toml(_TOML_REFERENCE)


@pytest.fixture(scope="module")
def toml_example():
    return _load_toml(_TOML_EXAMPLE)


def _flatten(table: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in table.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _section_body(text: str, section: str) -> str:
    m = re.search(r"^\[" + re.escape(section) + r"\]\n((?:(?!^\[)[\s\S])*)", text, re.MULTILINE)
    return m.group(1) if m else ""


def _commented_key(text: str, section: str, key: str) -> bool:
    return re.search(r"^#\s*" + re.escape(key) + r"\s*=", _section_body(text, section), re.MULTILINE) is not None


def _walk(model: BaseModel, prefix: str, table, text: str, problems: list[str]) -> None:
    fields = type(model).model_fields
    for name in fields:
        value = getattr(model, name)
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, BaseModel):
            sub = table.get(name) if isinstance(table, dict) else None
            if sub is None:
                problems.append(f"section [{path}] missing from the TOML example")
                sub = {}
            _walk(value, path, sub, text, problems)
            continue
        present = isinstance(table, dict) and name in table
        if isinstance(value, list) and value and isinstance(value[0], BaseModel):
            value = [v.model_dump() for v in value]
        if not present:
            if value is None and _commented_key(text, prefix, name):
                continue
            problems.append(f"{path} missing from the TOML example (default {value!r})")
            continue
        got = table[name]
        if got == value or path in _PLACEHOLDERS or path in _SHIPPED_OVERRIDES:
            continue
        if value is None and got == "":
            continue
        problems.append(f"{path}: model default {value!r} != TOML example {got!r}")
    if isinstance(table, dict):
        for key in table:
            if key not in fields:
                problems.append(f"{prefix}.{key} is in the TOML example but not in the schema")


def test_reference_matches_schema_defaults(example_settings, toml_reference):
    data, text = toml_reference
    problems: list[str] = []
    _walk(example_settings, "", data, text, problems)
    assert not problems, "\n".join(problems)


def test_placeholders_and_overrides_still_exist(example_settings, toml_reference):
    data, _ = toml_reference
    for path in sorted(_PLACEHOLDERS | set(_SHIPPED_OVERRIDES)):
        node = data
        for part in path.split("."):
            assert isinstance(node, dict) and part in node, f"{path} allow-listed but absent from the TOML example"
            node = node[part]
    assert data["alarm_engine"]["ingest_token"] == "REPLACE_ME"


def test_slim_example_keys_exist_in_schema_at_defaults(example_settings, toml_example):
    data, _ = toml_example
    problems: list[str] = []
    for path, got in _flatten(data).items():
        node = example_settings
        missing = False
        for part in path.split("."):
            if not isinstance(node, BaseModel) or part not in type(node).model_fields:
                missing = True
                break
            node = getattr(node, part)
        if missing:
            problems.append(f"{path} is in the slim example but not in the schema")
            continue
        if got == node or path in _PLACEHOLDERS or path in _SHIPPED_OVERRIDES:
            continue
        if node is None and got == "":
            continue
        problems.append(f"{path}: model default {node!r} != slim example {got!r}")
    assert not problems, "\n".join(problems)


def test_slim_example_holds_exactly_the_bootstrap_keys(toml_example):
    data, _ = toml_example
    keys = set(_flatten(data))
    assert keys == SLIM_KEYS, f"extra: {sorted(keys - SLIM_KEYS)} missing: {sorted(SLIM_KEYS - keys)}"


def test_slim_example_is_a_strict_subset_of_the_reference(toml_example, toml_reference):
    slim = _flatten(toml_example[0])
    ref = _flatten(toml_reference[0])
    problems = [f"{p}: slim {v!r} != reference {ref.get(p, '<absent>')!r}"
                for p, v in slim.items() if p not in ref or ref[p] != v]
    assert not problems, "\n".join(problems)
    assert set(slim) < set(ref)


def test_live_loader_matches_example():
    # The gitignored loader must be a byte copy of the tracked example.
    live = _REPO / "config" / "unified_config.py"
    if not live.exists():
        pytest.skip("no live loader")
    assert live.read_text(encoding="utf-8") == _PY_EXAMPLE.read_text(encoding="utf-8")
