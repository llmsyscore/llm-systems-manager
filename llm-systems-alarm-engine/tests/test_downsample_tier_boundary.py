# Tier-ladder grain selection for long-window metric reads (#597): a
# request for exactly N seconds arrives at N + a few ms and must not
# fall through to the next-coarser tier.
from types import SimpleNamespace

from backend.storage.repositories import _downsample_every

TIERS = [
    SimpleNamespace(max_window_s=21600, every="1m"),
    SimpleNamespace(max_window_s=86400, every="1m"),
    SimpleNamespace(max_window_s=604800, every="5m"),
    SimpleNamespace(max_window_s=2592000, every="30m"),
]


def test_exact_24h_request_keeps_the_1m_tier():
    assert _downsample_every(86400.005, TIERS) == "1m"


def test_mid_tier_windows():
    assert _downsample_every(7200.0, TIERS) == "1m"
    assert _downsample_every(200000.0, TIERS) == "5m"
    assert _downsample_every(1000000.0, TIERS) == "30m"


def test_well_past_a_boundary_moves_to_the_next_tier():
    assert _downsample_every(86402.0, TIERS) == "5m"


def test_beyond_last_tier_falls_to_largest_bucket():
    assert _downsample_every(10**8, TIERS) == "30m"


def test_no_tiers_returns_none():
    assert _downsample_every(86400.0, []) is None
    assert _downsample_every(86400.0, None) is None
