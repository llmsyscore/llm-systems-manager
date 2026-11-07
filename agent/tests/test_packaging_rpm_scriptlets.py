"""rpm scriptlets: '%' is escaped for rpm's macro expansion and verified after the build."""
import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BUILD = _ROOT / "tools" / "packaging" / "build-agent-package.sh"
_COMMON = _ROOT / "tools" / "packaging" / "agent" / "scripts" / "common.sh"


def test_build_escapes_and_verifies_rpm_scriptlets():
    src = _BUILD.read_text()
    assert "sed 's/%/%%/g'" in src and "--rpm-macro-expansion" not in src
    assert "POSTINFLAGS" in src and "run verbatim" in src
    assert all(f'"$SCRIPTS/rpm-{s}.esc"' in src for s in ("pre", "post", "preun", "postun"))
    assert 'rpm -qp --qf "%{$tag}"' in src and "differs from" in src


def test_common_sh_round_trips_through_rpm_expansion(tmp_path):
    esc = tmp_path / "post.esc"
    subprocess.run(["sed", "s/%/%%/g", str(_COMMON)], check=True, stdout=esc.open("w"))
    expanded = re.sub(r"%%", "%", esc.read_text())
    assert expanded == _COMMON.read_text()
    assert _COMMON.read_text().count("%%") >= 5
