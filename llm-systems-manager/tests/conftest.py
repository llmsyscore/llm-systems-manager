"""
Shared pytest setup for the manager test suite.

Loads the manager module under the name `manager_mod` so tests can import
symbols from it (`from manager_mod import _scrypt_hash, ...`). The actual
file uses a hyphen in the filename (`llm-systems-manager.py`), which
prevents a plain `import` — `importlib.util.spec_from_file_location` is the
canonical workaround.
"""
from __future__ import annotations

import importlib.util
import sys
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


def _load_manager_module():
    spec = importlib.util.spec_from_file_location("manager_mod", _MANAGER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["manager_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


# Eager load — module-level so every test file can `from manager_mod import …`.
manager_mod = _load_manager_module()
