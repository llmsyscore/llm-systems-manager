"""#215: [alarm_engine.correlation] defaults."""
from config.unified_config import AlarmEngineCorrelation, settings


def test_correlation_defaults():
    c = AlarmEngineCorrelation()
    assert c.enabled is True
    assert c.window_seconds == 60.0
    assert c.notify_per_incident is True


def test_correlation_section_is_wired():
    c = getattr(settings.alarm_engine, "correlation", None)
    assert isinstance(c, AlarmEngineCorrelation)
