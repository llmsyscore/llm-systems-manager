# agent/tests/test_installer_hardening_304.py
# Installer hardening: pinned deps, vendored udev rules, atomic apt sentinel,
# imggen probe gating (#304).
from __future__ import annotations

import re
import subprocess
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parent
INSTALL_SH = AGENT_DIR / "install" / "install.sh"
REQS = AGENT_DIR / "install" / "requirements.txt"
REQS_MON = AGENT_DIR / "install" / "requirements-monitor.txt"
UDEV_RULES = AGENT_DIR / "install" / "71-liquidctl.rules"
LIB_COMMON = REPO_ROOT / "tools" / "installer" / "lib-common.sh"
TOP_INSTALL = REPO_ROOT / "tools" / "installer" / "install.sh"


def _extract_func(source: Path, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", source.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from {source.name}"
    return m.group(0)


# ── #304.1: dependency upper caps ───────────────────────────────────────────

def _reqs(path: Path) -> list[str]:
    return [l.split(";")[0].strip() for l in path.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def test_requirements_have_upper_bounds():
    for line in _reqs(REQS) + _reqs(REQS_MON):
        assert "<" in line, f"unpinned (no upper bound): {line!r}"
        assert ">=" in line, f"missing lower bound: {line!r}"


# ── #304.2: vendored udev rules, no network fetch ───────────────────────────

def test_udev_rules_vendored_in_repo():
    assert UDEV_RULES.is_file()
    text = UDEV_RULES.read_text()
    assert "liquidctl" in text.lower()
    assert text.startswith("# Vendored from liquidctl")


def test_installer_installs_udev_from_disk_not_network():
    text = INSTALL_SH.read_text()
    # No raw.githubusercontent fetch of the udev rules remains.
    assert "raw.githubusercontent.com/liquidctl" not in text
    assert 'UDEV_SRC="$TMPL_DIR/71-liquidctl.rules"' in text
    # The install must source the vendored file, not curl/wget it.
    m = re.search(r'UDEV_SRC=.*?udevadm trigger', text, re.S)
    assert m and "curl" not in m.group(0) and "wget" not in m.group(0)


# ── #304.3: atomic apt-update sentinel ──────────────────────────────────────

def test_apt_stamp_created_atomically_not_unsafe_mktemp_u():
    text = TOP_INSTALL.read_text()
    assert "mktemp -u -t llmsys-apt-updated" not in text
    assert 'mktemp -t llmsys-apt-updated' in text


def test_apt_update_once_uses_nonempty_marker_semantics():
    body = _extract_func(LIB_COMMON, "apt_update_once")
    # done-signal is file NON-EMPTY (-s), and marking writes real content.
    assert '-s "$LLMSYS_APT_STAMP"' in body
    assert "-e \"$LLMSYS_APT_STAMP\"" not in body
    assert "printf 'updated" in body


def test_apt_sentinel_end_to_end_runs_once(tmp_path):
    # Pre-created empty stamp must NOT be read as "already updated"; after the
    # first run it is marked non-empty so a second call is a no-op.
    stamp = tmp_path / "stamp"
    stamp.write_text("")  # atomic-mktemp leaves it empty
    body = _extract_func(LIB_COMMON, "apt_update_once")
    script = (
        "set -euo pipefail\n"
        "apt_update_with_clock_recovery() { echo RAN; }\n"
        f"LLMSYS_APT_STAMP='{stamp}'\n_APT_UPDATED=0\n"
        f"{body}\n"
        "apt_update_once\napt_update_once\n"
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("RAN") == 1, out.stdout
    assert stamp.read_text().strip() == "updated"


# ── #304.5: imggen probe gated on LM Studio ownership of :1234 ───────────────

def test_imggen_probe_skips_1234_when_lms_enabled():
    body = _extract_func(INSTALL_SH, "_detect_imggen")
    # The :1234 HTTP probe must be guarded by an ENABLE_LMS check.
    m = re.search(r'ENABLE_LMS.*?127\.0\.0\.1:1234', body, re.S)
    assert m, "the :1234 probe is not gated on ENABLE_LMS"
