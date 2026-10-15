"""#1274: the alert websocket bridge logs one line per relayed close — DEBUG
for an expected close code, WARNING naming the side for anything else — and
never a traceback."""
from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import pytest

import manager_mod as M

LOGGER = "llm-systems-manager"


def _closed(code=None, reason=""):
    rcvd = SimpleNamespace(code=code, reason=reason) if code is not None else None
    return SimpleNamespace(rcvd=rcvd)


@pytest.mark.parametrize("code", [1000, 1001, 1005, 1006])
def test_expected_close_is_one_debug_line(caplog, code):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        M._log_ws_relay_close("dashboard", _closed(code))
    recs = [r for r in caplog.records if "WS proxy" in r.getMessage()]
    assert len(recs) == 1
    assert recs[0].levelno == logging.DEBUG
    assert recs[0].exc_info is None
    assert f"code {code}" in recs[0].getMessage()


def test_internal_error_close_is_one_warning_naming_the_side(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        M._log_ws_relay_close("engine", _closed(1011))
    recs = [r for r in caplog.records if "WS proxy" in r.getMessage()]
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    assert recs[0].exc_info is None
    assert "engine side" in recs[0].getMessage() and "1011" in recs[0].getMessage()


def test_no_close_frame_reads_as_1006(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        M._log_ws_relay_close("dashboard", _closed())
    assert "code 1006" in caplog.records[-1].getMessage()
    assert caplog.records[-1].levelno == logging.DEBUG


def test_relay_names_both_sides():
    src = inspect.getsource(M._maybe_start_alarm_ws_proxy)
    assert "_log_ws_relay_close(" in src
    assert '"dashboard"' in src and '"engine"' in src and '"openclaw"' in src
