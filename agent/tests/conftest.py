"""Load agent/providers/llama_install.py + llama_upgrade.py standalone for unit tests.

providers/__init__.py imports the full llama provider (fastapi/requests),
so we load the leaf module directly via importlib rather than as a package.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent           # …/agent
_LLAMA_INSTALL_PY = _AGENT_ROOT / "providers" / "llama_install.py"
_LLAMA_UPGRADE_PY = _AGENT_ROOT / "providers" / "llama_upgrade.py"

if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


llama_install = _load("llama_install", _LLAMA_INSTALL_PY)
llama_upgrade = _load("llama_upgrade", _LLAMA_UPGRADE_PY)
