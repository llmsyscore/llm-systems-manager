"""update.sh: the release marker follows the code sync, and is left alone when a manager/AE update was skipped or failed."""
import re
import subprocess
from pathlib import Path

UPDATE = Path(__file__).resolve().parents[1] / "update.sh"


def _function(name: str) -> str:
    """The text of one top-level shell function in update.sh."""
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", UPDATE.read_text(), re.M | re.S)
    assert m, f"{name} not found in update.sh"
    return m.group(0)


def _marker_block() -> str:
    """The end-of-run Release marker step."""
    text = UPDATE.read_text()
    return text[text.index("# ── Release marker"):text.index("# ── Prune upstream-removed files")]


STUBS = """
set -euo pipefail
REPO_SRC=$1; LLMSYS_INSTALL_DIR=$2; APPLY=$3; WANTED=$4; DRY_RUN=0; PARANOID_VERIFY=0
UPDATED_COMPONENTS=(); RESTART_UNITS=(); FAILED_UNITS=(); VERIFY_FAILURES=0; MARKER_HOLD=0
HAVE_MANAGER=true; HAVE_AE=true
step() { echo "$*" >> "$LLMSYS_INSTALL_DIR/steps"; }
component_wanted() { [[ "$WANTED" == 1 ]]; }
banner() { :; }; log() { :; }; ok() { :; }; warn() { :; }; err() { :; }
files_changed() { echo "backend/app.py"; }
prompt_yn() { [[ "$APPLY" == 1 ]]; }
backup_changed_files() { :; }
sync_dir() { step sync; }
refresh_venv() { step venv; return 0; }
verify_venv_imports() { return 0; }
restamp_release_marker() { step stamp; }
"""


def run_update(tmp_path, key: str, reqs: str = "requirements.txt", apply: bool = True,
               wanted: bool = True) -> list:
    src, dest = tmp_path / "src", tmp_path / "dest"
    (src / "comp").mkdir(parents=True)
    (dest / "comp").mkdir(parents=True)
    script = (STUBS + _function("_hold_marker") + _function("update_component")
              + f'update_component L comp comp u.service {key} "{reqs}"\n' + _marker_block())
    r = subprocess.run(["bash", "-c", script, "x", str(src), str(dest), str(int(apply)), str(int(wanted))],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    steps = dest / "steps"
    return steps.read_text().split() if steps.exists() else []


def test_manager_marker_lands_before_the_venv_refresh(tmp_path):
    assert run_update(tmp_path, "manager") == ["sync", "stamp", "venv", "stamp"]


def test_alarm_engine_marker_lands_before_the_venv_refresh(tmp_path):
    assert run_update(tmp_path, "alarm-engine") == ["sync", "stamp", "venv", "stamp"]


def test_agent_source_sync_does_not_stamp_early(tmp_path):
    assert run_update(tmp_path, "agent", reqs="") == ["sync", "stamp"]


def test_declined_manager_update_leaves_the_marker(tmp_path):
    assert run_update(tmp_path, "manager", apply=False) == []


def test_manager_filtered_out_leaves_the_marker(tmp_path):
    assert run_update(tmp_path, "manager", wanted=False) == []
