"""#220: [alarm_engine.retention] defaults."""
from config.unified_config import settings


def test_retention_defaults():
    r = getattr(settings.alarm_engine, "retention", None)
    assert r is not None
    assert int(getattr(r, "alert_history_days", 0)) == 90
    assert float(getattr(r, "purge_interval_s", 0)) == 3600.0
