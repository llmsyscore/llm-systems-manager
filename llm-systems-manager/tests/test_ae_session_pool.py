"""#455: _ae_session must mount an HTTPAdapter sized past urllib3's default
pool of 10, scaled to the configured worker pool, on both URL schemes."""
from manager_mod import _ae_session, settings


def _threads() -> int:
    return int(getattr(settings.manager, "http_threads", 64) or 64)


def test_ae_session_pool_scales_with_http_threads():
    for scheme in ("http://", "https://"):
        adapter = _ae_session.get_adapter(scheme + "ae.internal")
        assert adapter._pool_maxsize >= _threads(), scheme
        assert adapter._pool_connections >= 1, scheme


def test_ae_session_pool_exceeds_urllib3_default():
    adapter = _ae_session.get_adapter("https://ae.internal")
    assert adapter._pool_maxsize > 10
