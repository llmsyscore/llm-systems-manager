# agent/tests/test_agent_privilege_hardening.py
"""Static + behavioral guards for the agent privilege-hardening changes:
#287 (sudoers), #289 (/tmp chown symlink), #292 (tarball fetch)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
SUDOERS = AGENT_DIR / "install" / "llm-systems-agent.sudoers.tmpl"
INSTALL_SH = AGENT_DIR / "install" / "install.sh"


# ── #287 sudoers ──────────────────────────────────────────────────────────
def test_sudoers_drops_tee_grant():
    text = SUDOERS.read_text()
    assert "tee" not in text, "the arbitrary-unit-write tee grant must be gone"


def test_sudoers_grants_only_no_arg_wrapper_and_build():
    text = SUDOERS.read_text()
    assert '/usr/local/sbin/llm-svcconfig-apply ""' in text
    assert '/usr/local/llama-server/build-llama-cpp.sh ""' in text


def test_sudoers_pins_liquidctl_to_status():
    text = SUDOERS.read_text()
    assert "liquidctl status --match Kraken" in text
    assert "liquidctl status --match HX1000i" in text
    assert 'liquidctl status --match "Smart Device"' in text
    # no bare, argument-unrestricted liquidctl grant
    assert not re.search(r"/usr/bin/liquidctl\s*,", text)
    assert not re.search(r"/usr/bin/liquidctl\s*$", text, re.MULTILINE)


def _render_sudoers(unit: str) -> str:
    return SUDOERS.read_text().replace("${AGENT_USER}", "llmagent").replace("${LLAMA_UNIT}", unit)


@pytest.mark.skipif(not shutil.which("visudo"), reason="visudo not available")
@pytest.mark.parametrize("unit", ["llama_server.service", "my-llama.service", "llama@gpu0.service"])
def test_rendered_sudoers_passes_visudo_for_any_unit(unit, tmp_path):
    f = tmp_path / "sudoers"; f.write_text(_render_sudoers(unit))
    r = subprocess.run(["visudo", "-c", "-f", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_sudoers_grants_track_configured_unit_name():
    rendered = _render_sudoers("my-llama.service")
    assert "systemctl start my-llama.service" in rendered
    assert "systemctl restart my-llama.service" in rendered
    assert "llama_server.service" not in rendered  # placeholder fully substituted


def _resolve_unit(override: str) -> str:
    lines = INSTALL_SH.read_text().splitlines()
    s = next(i for i, l in enumerate(lines) if l.startswith("_resolved_llama_unit()"))
    e = next(i for i in range(s + 1, len(lines)) if lines[i] == "}")
    block = "\n".join(lines[s:e + 1])
    script = f'INSTALL_DIR=/nonexistent\nLLAMA_SYSTEMD_UNIT_OVERRIDE="{override}"\n{block}\n_resolved_llama_unit'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout


@pytest.mark.parametrize("bad", [
    "x.service, ALL=(root) NOPASSWD: ALL #.service",  # sudoers injection attempt
    "evil",           # not a .service
    "../../etc.service",
    "",
])
def test_resolver_rejects_unsafe_unit_names(bad):
    assert _resolve_unit(bad) == "llama_server.service"


def test_resolver_accepts_valid_unit_name():
    assert _resolve_unit("my-llama.service") == "my-llama.service"


# ── #289 /tmp state-file chown ────────────────────────────────────────────
def test_state_file_chown_rejects_symlink():
    text = INSTALL_SH.read_text()
    assert '[[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]]' in text
    assert '$SUDO chown -h "$USER_ARG:$USER_GROUP" "$STATE_FILE"' in text


# ── #292 tarball fetch ────────────────────────────────────────────────────
def test_fetch_refuses_plaintext_http_without_optin():
    text = INSTALL_SH.read_text()
    assert 'LLMSYS_ALLOW_INSECURE_UPDATE' in text
    assert 'refusing to fetch the agent update over a non-HTTPS URL' in text


# The tar-slip guard, mirrored from install.sh's _fetch_agent_into. Behavioral
# check that it rejects escaping members and accepts a clean agent/ tree.
_GUARD_AWK = r'''
    /^\// || /(^|\/)\.\.(\/|$)/ { print; exit }
    $0 !~ /^agent(\/|$)/        { print; exit }'''


def test_guard_awk_matches_install_sh():
    text = INSTALL_SH.read_text()
    assert "tar -tzf" in text and 'refusing tarball: unsafe member path' in text
    assert r'/^\// || /(^|\/)\.\.(\/|$)/' in text
    assert r'$0 !~ /^agent(\/|$)/' in text


def _guard(members):
    inp = "\n".join(members) + "\n"
    p = subprocess.run(["awk", _GUARD_AWK], input=inp, capture_output=True, text=True)
    return p.stdout.strip()


@pytest.mark.parametrize("members", [
    ["agent/", "agent/providers/llama.py"],
    ["agent/..config"],  # filename starting with .. is not a parent ref
])
def test_guard_accepts_safe_members(members):
    assert _guard(members) == ""


@pytest.mark.parametrize("members,bad", [
    (["agent/x.py", "agent/../../etc/cron.d/evil"], "agent/../../etc/cron.d/evil"),
    (["../evil.sh"], "../evil.sh"),
    (["/etc/cron.d/passwd"], "/etc/cron.d/passwd"),
    (["notagent/x"], "notagent/x"),
    (["agent/.."], "agent/.."),
])
def test_guard_rejects_unsafe_members(members, bad):
    assert _guard(members) == bad
