"""Coverage for Flux-injection hardening: rule tag validation + escaping."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from config.unified_config import settings
from backend.api.routes import rules
from backend.models.alarm_rule import AlarmRuleCreate, AlarmRuleUpdate, RuleType
from backend.storage.influxdb_client import _flux_str


def test_flux_str_escapes_quote_and_backslash():
    assert _flux_str('foo"bar') == 'foo\\"bar'
    assert _flux_str("a\\b") == "a\\\\b"
    assert _flux_str('x") or r._measurement == "y') == 'x\\") or r._measurement == \\"y'
    assert _flux_str("line\nbreak\r") == "line\\nbreak\\r"
    assert _flux_str("x${die()}") == "x\\${die()}"
    assert _flux_str("plain_name") == "plain_name"


def _rule_kwargs(**overrides):
    kw = dict(
        name="t", metric_source="system", metric_name="cpu_total",
        rule_type=RuleType.THRESHOLD_ABOVE,
        config={"threshold": {"value": 90.0}},
    )
    kw.update(overrides)
    return kw


def test_rule_create_accepts_normal_tags():
    r = AlarmRuleCreate(**_rule_kwargs(
        metric_name="liquidctl_aio_Liquid temperature_value"))
    assert " " in r.metric_name


@pytest.mark.parametrize("bad", [
    'foo") or r._measurement == "x',
    'a"b', "a\\b", "", "x" * 129, "new\nline", "trailing\n", "${x}",
])
def test_rule_create_rejects_flux_metachars(bad):
    with pytest.raises(ValidationError):
        AlarmRuleCreate(**_rule_kwargs(metric_name=bad))
    with pytest.raises(ValidationError):
        AlarmRuleCreate(**_rule_kwargs(metric_source=bad))


def test_rule_update_validates_when_present():
    assert AlarmRuleUpdate(metric_name=None).metric_name is None
    assert AlarmRuleUpdate(metric_name="cpu_total").metric_name == "cpu_total"
    with pytest.raises(ValidationError):
        AlarmRuleUpdate(metric_name='a"b')


def test_rules_route_returns_422(monkeypatch):
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", "", raising=False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", "", raising=False)
    app = FastAPI()
    app.include_router(rules.router)
    client = TestClient(app)
    payload = dict(_rule_kwargs(metric_name='x") or r.source == "gpu'))
    payload["rule_type"] = "threshold_above"
    r = client.post("/api/alarm/rules", json=payload)
    assert r.status_code == 422
