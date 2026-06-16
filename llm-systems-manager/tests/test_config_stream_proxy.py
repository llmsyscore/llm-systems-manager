"""Phase 1 (#110): new [manager] SSE-daemon config keys carry sane defaults."""
from __future__ import annotations

from config.unified_config import settings


def test_stream_proxy_port_default():
    assert int(getattr(settings.manager, "stream_proxy_port", -1)) == 5445


def test_stream_proxy_lifetime_default():
    assert float(getattr(settings.manager, "stream_proxy_lifetime_s", -1.0)) == 600.0


def test_stream_op_max_lifetime_default():
    assert float(getattr(settings.manager, "stream_op_max_lifetime_s", -1.0)) == 3600.0
