"""Build identity in run conditions: _build_identity and run_conditions.start install."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from conftest import llama_inventory as inv

_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))
import test_llama_props as _lp  # noqa: E402  (loads providers.llama with stubs)

llama = _lp.llama


def _rc():
    if "collectors" not in sys.modules:
        pkg = types.ModuleType("collectors")
        pkg.__path__ = [str(_AGENT_ROOT / "collectors")]
        sys.modules["collectors"] = pkg
    for name, attrs in (("collectors.gpu", {"collect_gpu": lambda: {}}),
                        ("collectors.system", {"collect_system_metrics": lambda: {}, "read_cpu_name": lambda: None})):
        if name not in sys.modules:
            m = types.ModuleType(name)
            m.__dict__.update(attrs)
            sys.modules[name] = m
    spec = importlib.util.spec_from_file_location("run_conditions_bi", _AGENT_ROOT / "providers" / "run_conditions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(llama, "llama_inventory", inv)
    monkeypatch.setitem(inv._state, "last", None)
    return inv


def test_build_name_strips_bin_and_build():
    assert inv._build_name("/x/llama-rocm/build/bin/llama-server") == "llama-rocm"
    assert inv._build_name("/x/llama-vulkan/bin/llama-server") == "llama-vulkan"


def test_build_identity_listed_row(listed, tmp_path):
    b = tmp_path / "rocm-build" / "bin" / "llama-server"
    b.parent.mkdir(parents=True)
    b.write_text("x")
    rp = str(b.resolve())
    inv._state["last"] = {"builds": [{"path": rp, "name": "rocm-build", "backend": "ROCm",
                                      "version": "6150", "build": "b6150", "note": "fast"}]}
    assert llama._build_identity(str(b)) == {"name": "rocm-build", "path": rp, "backend": "ROCm",
                                             "version": "6150", "build": "b6150", "note": "fast"}


def test_build_identity_unlisted_falls_back(listed, tmp_path):
    b = tmp_path / "plain" / "llama-server"
    b.parent.mkdir()
    b.write_text("x")
    got = llama._build_identity(str(b))
    assert got["name"] == "plain" and got["path"] == str(b.resolve())
    assert got["backend"] == inv.detect_backend(b.parent)
    assert got["version"] is None and got["build"] is None and got["note"] == ""
    assert llama._build_identity("") is None


def test_start_puts_install_in_backend():
    rc = _rc()
    inst = {"name": "rocm-build", "backend": "ROCm"}
    assert rc.start({"install": inst})["backend"]["install"] == inst


def test_start_omits_install_when_none():
    rc = _rc()
    assert "install" not in rc.start({"install": None})["backend"]
