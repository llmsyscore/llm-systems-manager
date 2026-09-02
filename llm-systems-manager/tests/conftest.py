"""
Shared pytest setup for the manager test suite.

Loads the manager module under the name `manager_mod` so tests can import
symbols from it (`from manager_mod import _scrypt_hash, ...`). The actual
file uses a hyphen in the filename (`llm-systems-manager.py`), which
prevents a plain `import` — `importlib.util.spec_from_file_location` is the
canonical workaround.
"""
from __future__ import annotations

import atexit
import importlib.util
import os
import pytest
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent          # …/llm-systems-manager/
_REPO_ROOT = _PACKAGE_ROOT.parent                                # …/llm-systems-manager (repo root)
_BACKEND_DIR = _PACKAGE_ROOT / "backend"
_MANAGER_PY = _BACKEND_DIR / "llm-systems-manager.py"

# REPO_ROOT for `config.unified_config`; BACKEND_DIR so the module's own
# relative imports (e.g. `_pki`) resolve.
for p in (_REPO_ROOT, _BACKEND_DIR):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)


# Per-session metrics.db for the suite (LLMSYS_METRICS_DB is read at import).
_LIVE_DB = _REPO_ROOT / "data" / "metrics.db"
_TMP_DATA = Path(tempfile.mkdtemp(prefix="llmsys-test-db-"))
os.environ["LLMSYS_METRICS_DB"] = str(_TMP_DATA / "metrics.db")
atexit.register(shutil.rmtree, _TMP_DATA, True)


def _live_audit_count():
    if not _LIVE_DB.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _load_manager_module():
    spec = importlib.util.spec_from_file_location("manager_mod", _MANAGER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["manager_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


# Eager load — module-level so every test file can `from manager_mod import …`.
manager_mod = _load_manager_module()


@pytest.fixture(autouse=True, scope="session")
def _live_audit_untouched():
    """Fail the session if any test writes the LIVE audit_log table."""
    before = _live_audit_count()
    yield
    after = _live_audit_count()
    if before is not None and after is not None and after != before:
        pytest.fail(f"tests wrote {after - before} row(s) into the LIVE audit_log — DB isolation broke")


@pytest.fixture(autouse=True, scope="session")
def _live_config_untouched():
    """Fail the session if any test writes the real llm-systems.toml."""
    import settings_toml_io as _sio
    path = _sio.resolve_config_path()
    before = path.stat().st_mtime_ns if path.exists() else None
    yield
    after = path.stat().st_mtime_ns if path.exists() else None
    if before != after:
        pytest.fail(f"a test wrote the LIVE config file {path} — patch settings_toml_io.resolve_config_path")
