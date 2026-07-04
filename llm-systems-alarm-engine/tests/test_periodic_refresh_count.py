"""#220 follow-up: periodic-refresh purge log always showed 0 rows."""
from backend.alarm_engine import _periodic_refresh_count


def test_int_result_counts_directly():
    assert _periodic_refresh_count(7) == 7


def test_sequence_result_counts_by_length():
    assert _periodic_refresh_count([1, 2, 3]) == 3


def test_none_result_counts_as_zero():
    assert _periodic_refresh_count(None) == 0
