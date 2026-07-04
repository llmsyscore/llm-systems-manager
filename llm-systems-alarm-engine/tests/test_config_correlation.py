"""#215: [alarm_engine.correlation] defaults."""
from config.unified_config import settings


def test_correlation_defaults():
    c = getattr(settings.alarm_engine, "correlation", None)
    assert c is not None
    assert bool(getattr(c, "enabled", None)) is True
    assert float(getattr(c, "window_seconds", 0)) == 60.0
    assert bool(getattr(c, "notify_per_incident", None)) is True
