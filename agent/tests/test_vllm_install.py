# agent/tests/test_vllm_install.py
"""#125: installer grows vllm — flags, sudoers alias, wrapper bake,
journal group, unit template, required files."""
from __future__ import annotations

from pathlib import Path

_INSTALL_DIR = Path(__file__).resolve().parents[1] / "install"
INSTALL = _INSTALL_DIR / "install.sh"
SUDOERS = _INSTALL_DIR / "llm-systems-agent.sudoers.tmpl"
UNIT = _INSTALL_DIR / "vllm.service.tmpl"


def test_flags_and_writer():
    src = INSTALL.read_text()
    assert "--enable-vllm" in src and "--no-vllm" in src
    assert "ENABLE_VLLM=false" in src
    assert "_set('VLLM_ENABLED'" in src


def test_sudoers_alias():
    t = SUDOERS.read_text()
    assert "LSA_VLLM" in t and "${VLLM_UNIT}" in t
    assert "llm-vllm-svcconfig-apply" in t
    assert "LSA_LLAMA, LSA_PERF, LSA_VLLM" in t


def test_unit_template_tokens():
    t = UNIT.read_text()
    for tok in ("__VLLM_BIN__", "__MODEL__", "__USER__", "__EXTRA_ARGS__"):
        assert tok in t
    assert "ExecStart=" in t


def test_journal_group_grant():
    assert "systemd-journal" in INSTALL.read_text()


def test_second_wrapper_baked_for_vllm_unit():
    src = INSTALL.read_text()
    assert "_resolved_vllm_unit" in src
    assert "/usr/local/sbin/llm-vllm-svcconfig-apply" in src


def test_sudoers_autoskip_conditions_include_vllm():
    src = INSTALL.read_text()
    assert '"$ENABLE_VLLM" != "true"' in src
    assert '"$ENABLE_VLLM" == "true"' in src


def test_required_files_include_vllm():
    src = INSTALL.read_text()
    assert "providers/vllm.py" in src
    assert "vllm.service.tmpl" in src
