"""#214: [manager.gateway] config keys carry safe defaults."""
import importlib
import sys


def test_gateway_defaults(tmp_path, monkeypatch):
    # Fresh-install defaults: load against an empty toml, not the live
    # operator config (which may legitimately carry gateway keys).
    cfg = tmp_path / "llm-systems.toml"
    cfg.write_text("")
    monkeypatch.setenv("LLM_SYSTEMS_CONFIG", str(cfg))
    sys.modules.pop("config.unified_config", None)
    try:
        uc = importlib.import_module("config.unified_config")
        gw = getattr(uc.settings.manager, "gateway", None)
        assert gw is not None
        assert bool(getattr(gw, "enabled", None)) is True
        assert list(getattr(gw, "api_keys", None) or []) == []
        assert float(getattr(gw, "read_timeout_s", 0)) == 600.0
    finally:
        sys.modules.pop("config.unified_config", None)


def test_gateway_live_config_shape():
    # Whatever the operator configured, the shape must hold.
    from config.unified_config import settings
    gw = getattr(settings.manager, "gateway", None)
    assert gw is not None
    assert isinstance(getattr(gw, "enabled", None), bool)
    keys = list(getattr(gw, "api_keys", None) or [])
    assert all(isinstance(k, str) and k for k in keys)
    assert float(getattr(gw, "read_timeout_s", 0)) > 0
