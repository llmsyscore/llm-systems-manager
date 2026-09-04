"""Static guards for the backup + restore CI oracle (#856): the script parses,
and the workflow that runs it is wired to the backup/restore code paths."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "installer" / "ci-backup-restore-smoke.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "backup-restore-test.yml"

BACKUP_CODE_PATHS = [
    "llm-systems-manager/backend/llm-systems-manager.py",
    "llm-systems-manager/backend/_archive.py",
    "llm-systems-alarm-engine/backend/alarm_engine.py",
    "llm-systems-alarm-engine/backend/_archive.py",
    "tools/installer/ci-backup-restore-smoke.sh",
    ".github/workflows/backup-restore-test.yml",
]


def test_script_parses_and_refuses_bare_env():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text
    # Every phase of the round trip is present, in order.
    order = ["0. Preconditions", "1. Seed", "2. Scheduled backup", "3. Destroy",
             "4. Restore", "5. Seeded state is back"]
    positions = [text.index(step) for step in order]
    assert positions == sorted(positions)


def test_script_asserts_alarm_engine_coverage():
    text = SCRIPT.read_text()
    assert "/api/admin/backup-now" in text
    assert ".last.components.alarm_engine.ok == true" in text
    assert "/api/alarm/admin/import/apply" in text
    assert "/api/admin/import/manager/apply" in text


def test_workflow_triggers_on_backup_code_and_runs_both_topologies():
    text = WORKFLOW.read_text()
    for p in BACKUP_CODE_PATHS:
        assert f'"{p}"' in text, f"workflow path filter lacks {p}"
    assert "--mode 1" in text
    assert "--mode 3" in text and "--mode 4" in text
    assert "PASSPHRASE=" in text
    assert text.count("ci-backup-restore-smoke.sh") >= 2
