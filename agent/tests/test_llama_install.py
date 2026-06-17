# agent/tests/test_llama_install.py
from __future__ import annotations

import types
import pytest
from conftest import llama_install as li


def _cfg(**kw):
    base = dict(LLAMA_BIN="/usr/local/llama-server/llama-server",
                LLAMA_BUILD_DIR="", LLAMA_BUILD_OPTS={},
                LLAMA_SYSTEMD_UNIT="llama_server.service")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_custom_script_default_uses_legacy_path():
    plan = li.plan("custom_script", {}, _cfg())
    assert plan.method == "custom_script"
    assert plan.steps == [["sudo", "-n", li.LEGACY_SCRIPT]]
    assert plan.resolve_binary() == "/usr/local/llama-server/llama-server"


def test_custom_script_honors_script_path_opt():
    plan = li.plan("custom_script", {"script_path": "/opt/x/build.sh"}, _cfg())
    assert plan.steps == [["sudo", "-n", "/opt/x/build.sh"]]


def test_empty_method_falls_back_to_custom_script():
    plan = li.plan("", {}, _cfg())
    assert plan.method == "custom_script"


def test_unknown_method_raises_install_error():
    with pytest.raises(li.InstallError):
        li.plan("nixpkgs", {}, _cfg())
