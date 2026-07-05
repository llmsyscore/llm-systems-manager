"""#214: [manager.gateway] config keys carry safe defaults."""
from config.unified_config import settings


def test_gateway_defaults():
    gw = getattr(settings.manager, "gateway", None)
    assert gw is not None
    assert bool(getattr(gw, "enabled", None)) is True
    assert list(getattr(gw, "api_keys", None) or []) == []
    assert float(getattr(gw, "read_timeout_s", 0)) == 600.0
