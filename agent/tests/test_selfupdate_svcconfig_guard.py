# agent/tests/test_selfupdate_svcconfig_guard.py
"""#307/#313/#318/#315/#316: self-update must refuse to deploy new code on a
llama host whose root-owned svcconfig helper is missing, older than the staged
template, OR baked for a different unit than the configured one — but must not
block a non-llama host, a --no-sudoers host (deliberate opt-out), or the root
--update path, which installs the helper before deploying."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"


def _extract(marker: str, is_func: bool) -> str:
    lines = INSTALL_SH.read_text().splitlines()
    if is_func:
        s = next(i for i, l in enumerate(lines) if l.startswith(marker + "()"))
        e = next(i for i in range(s + 1, len(lines)) if lines[i] == "}")
    else:
        s = next(i for i, l in enumerate(lines) if l.strip() == marker)
        e = next(i for i in range(s + 1, len(lines)) if lines[i] == "  fi")
    return "\n".join(lines[s:e + 1])


GUARD = _extract('_lu_guard="$(_resolved_llama_unit)"', is_func=False)
YAML_SCALAR = _extract("_yaml_scalar", is_func=True)
WRAP_VER = _extract("_svcconfig_wrapper_version", is_func=True)
WRAP_UNIT = _extract("_svcconfig_wrapper_unit_path", is_func=True)


def _run(*, from_self="true", unit_name="llama_server.service", unit_present=True,
         have_ver=None, want_ver=1, llama_enabled="true", baked_unit=None,
         sudoers_present=True, tmp_path):
    unitdir = tmp_path / "systemd"; unitdir.mkdir()
    if unit_present:
        (unitdir / unit_name).write_text("[Service]\n")
    installdir = tmp_path / "install"; installdir.mkdir()
    (installdir / "agent_config.yaml").write_text(
        "" if llama_enabled is None else f"LLAMA_ENABLED: {llama_enabled}\n")
    tmpl = tmp_path / "tmpl"; tmpl.mkdir()
    (tmpl / "llm-svcconfig-apply.sh.tmpl").write_text(
        f"#!/usr/bin/env bash\n# SVCCONFIG_WRAPPER_VERSION={want_ver}\n")
    sudoers = tmp_path / "sudoers-llm-systems-agent"
    if sudoers_present:
        sudoers.write_text("stub\n")
    if have_ver is None:
        wrap = "/nonexistent/wrap"
    else:
        wf = tmp_path / "llm-svcconfig-apply"
        body = f"#!/bin/sh\n# SVCCONFIG_WRAPPER_VERSION={have_ver}\n"
        if baked_unit is not None:
            body += f"UNIT_PATH='{unitdir}/{baked_unit}'\n"
        wf.write_text(body); wf.chmod(0o755)
        wrap = str(wf)
    block = GUARD.replace("/etc/systemd/system/", f"{unitdir}/") \
                 .replace("/usr/local/sbin/llm-svcconfig-apply", "$WRAP") \
                 .replace("/etc/sudoers.d/llm-systems-agent", str(sudoers))
    script = (
        "set -u\n"
        f"{YAML_SCALAR}\n{WRAP_VER}\n{WRAP_UNIT}\n"
        '_resolved_llama_unit() { printf "%s" "$UNITNAME"; }\n'
        'AGENT_OS="linux"\n'
        f"FROM_SELF_UPDATE={from_self}\n"
        "SRC_DIR=/tmp/staged\n"
        f'INSTALL_DIR="{installdir}"\n'
        f'TMPL_DIR="{tmpl}"\n'
        f'UNITNAME="{unit_name}"\n'
        f'WRAP="{wrap}"\n'
        f"{block}\n"
        "echo PROCEEDED\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def _aborted(r):
    return r.returncode == 1 and "one-time root install/update" in r.stderr

def _proceeded(r):
    return r.returncode == 0 and "PROCEEDED" in r.stdout


@pytest.mark.parametrize("unit_name", ["llama_server.service", "my-llama.service", "llama@gpu0.service"])
def test_aborts_when_wrapper_absent(unit_name, tmp_path):
    assert _aborted(_run(unit_name=unit_name, have_ver=None, want_ver=1, tmp_path=tmp_path))


def test_aborts_on_version_drift(tmp_path):
    # installed helper older than the staged template => must abort
    assert _aborted(_run(have_ver=1, want_ver=2, tmp_path=tmp_path))


def test_aborts_when_installed_has_no_marker(tmp_path):
    # already-migrated host with a pre-versioning wrapper (marker => 0) < v1
    assert _aborted(_run(have_ver=0, want_ver=1, tmp_path=tmp_path))


def test_proceeds_when_wrapper_current(tmp_path):
    assert _proceeded(_run(have_ver=1, want_ver=1, tmp_path=tmp_path))


def test_proceeds_when_installed_newer(tmp_path):
    assert _proceeded(_run(have_ver=3, want_ver=2, tmp_path=tmp_path))


def test_proceeds_on_non_llama_host_no_unit_file(tmp_path):
    assert _proceeded(_run(unit_present=False, have_ver=None, tmp_path=tmp_path))


def test_llama_disabled_with_stray_unit_file_does_not_block(tmp_path):
    assert _proceeded(_run(llama_enabled="false", have_ver=None, tmp_path=tmp_path))


def test_unknown_llama_enabled_still_guards(tmp_path):
    assert _aborted(_run(llama_enabled=None, have_ver=None, tmp_path=tmp_path))


def test_does_not_block_root_update(tmp_path):
    assert _proceeded(_run(from_self="false", have_ver=None, tmp_path=tmp_path))


# --- #315: wrapper baked for a different (renamed) unit --------------------

def test_aborts_on_renamed_unit_mismatch(tmp_path):
    # helper is version-current but baked for the OLD unit path => must abort
    r = _run(unit_name="new-llama.service", have_ver=1, want_ver=1,
             baked_unit="llama_server.service", tmp_path=tmp_path)
    assert _aborted(r)
    assert "baked for" in r.stderr and "new-llama.service" in r.stderr


def test_proceeds_when_baked_unit_matches(tmp_path):
    assert _proceeded(_run(unit_name="my-llama.service", have_ver=1, want_ver=1,
                           baked_unit="my-llama.service", tmp_path=tmp_path))


def test_wrapper_without_unit_path_line_is_not_a_mismatch(tmp_path):
    # pre-#315 harness stubs / unreadable UNIT_PATH: version check still rules
    assert _proceeded(_run(have_ver=1, want_ver=1, baked_unit=None, tmp_path=tmp_path))


# --- #316: --no-sudoers hosts (no managed sudoers file) --------------------

def test_no_sudoers_host_proceeds_when_wrapper_absent(tmp_path):
    # no managed sudoers file: warn + proceed instead of aborting
    r = _run(have_ver=None, sudoers_present=False, tmp_path=tmp_path)
    assert _proceeded(r)
    assert "--no-sudoers" in r.stdout


def test_no_sudoers_host_proceeds_on_mismatch(tmp_path):
    r = _run(unit_name="new-llama.service", have_ver=1, want_ver=1,
             baked_unit="llama_server.service", sudoers_present=False,
             tmp_path=tmp_path)
    assert _proceeded(r)


def test_no_sudoers_host_prints_no_note_when_wrapper_current(tmp_path):
    r = _run(have_ver=1, want_ver=1, sudoers_present=False, tmp_path=tmp_path)
    assert _proceeded(r)
    assert "--no-sudoers" not in r.stdout


def test_root_update_installs_wrapper_before_deploying_code():
    text = INSTALL_SH.read_text()
    apply_at = text.index('_apply_sudoers_and_wrapper || exit 1')
    deploy_at = text.index('_section "Updating agent code"')
    assert apply_at < deploy_at
