"""#215: incident_id self-root + correlation_group threading."""
from backend.models.alert import AlertCreate


def _ac(**kw):
    base = dict(metric_source="gpu", metric_name="temp", current_value=90.0,
                threshold_value=85.0, message="hot", source_host="hostA")
    base.update(kw)
    return AlertCreate(**base)


def test_to_alert_self_roots_incident():
    a = _ac().to_alert()
    assert a.incident_id == str(a.alert_id)


def test_to_alert_honors_assigned_incident():
    a = _ac(incident_id="root-1").to_alert()
    assert a.incident_id == "root-1"


def test_to_dict_carries_incident():
    a = _ac(incident_id="root-1").to_alert()
    assert a.to_dict()["incident_id"] == "root-1"


def test_rule_models_carry_correlation_group():
    from backend.models.alarm_rule import AlarmRuleCreate, AlarmRuleUpdate
    assert "correlation_group" in AlarmRuleCreate.model_fields
    assert "correlation_group" in AlarmRuleUpdate.model_fields
