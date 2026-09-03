"""
Shared pytest setup for the alarm-engine test suite.

Adds the package root to sys.path so tests can import `backend.*` without
having to install the engine as a package. This mirrors how the running
service imports modules (via `python -m backend.alarm_engine`).
"""
from __future__ import annotations

import atexit
import logging
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PACKAGE_ROOT.parent

# Both paths are needed: REPO_ROOT for `config.unified_config`, PACKAGE_ROOT
# for `backend.*` imports.
for p in (_REPO_ROOT, _PACKAGE_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

# Per-session log dir: alarm_engine attaches its file handler to the root
# logger from settings.paths.log_dir at import, so redirect it first.
from config.unified_config import settings as _settings  # noqa: E402

_LIVE_LOG_DIR = Path(_settings.paths.log_dir)
_TMP_LOG_DIR = Path(tempfile.mkdtemp(prefix="llmsys-test-log-"))
_settings.paths.log_dir = str(_TMP_LOG_DIR)
atexit.register(shutil.rmtree, _TMP_LOG_DIR, True)


def _live_log_handlers() -> list:
    """File handlers on any logger whose file sits under the LIVE log dir."""
    names = list(logging.root.manager.loggerDict)
    loggers = [logging.getLogger()] + [logging.getLogger(n) for n in names]
    hits = set()
    for lg in loggers:
        for h in getattr(lg, "handlers", ()):
            base = getattr(h, "baseFilename", None)
            if base and Path(base).is_relative_to(_LIVE_LOG_DIR):
                hits.add(base)
    return sorted(hits)


@pytest.fixture(autouse=True, scope="session")
def _live_log_untouched():
    """Fail the session if any logger has a file handler in the LIVE log dir."""
    yield
    hits = _live_log_handlers()
    if hits:
        pytest.fail(f"tests attached log file handler(s) to the LIVE log dir: {hits} — log isolation broke")
