"""
Shared pytest setup for the alarm-engine test suite.

Adds the package root to sys.path so tests can import `backend.*` without
having to install the engine as a package. This mirrors how the running
service imports modules (via `python -m backend.alarm_engine`).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PACKAGE_ROOT.parent

# Both paths are needed: REPO_ROOT for `config.unified_config`, PACKAGE_ROOT
# for `backend.*` imports.
for p in (_REPO_ROOT, _PACKAGE_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
