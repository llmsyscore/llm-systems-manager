# agent/tests/test_merge_perf_unit.py
"""#976 oracle: the installer merges a shipped perf unit into an installed copy
without losing operator edits, drops lines upstream removed, backs up first,
and is a no-op on the second run."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_AGENT = Path(__file__).resolve().parents[1]
_SCRIPT = _AGENT / "install" / "merge_perf_unit.py"
_INSTALL_SH = _AGENT / "install" / "install.sh"
_EXAMPLES = _AGENT / "install" / "examples"

_spec = importlib.util.spec_from_file_location("merge_perf_unit", _SCRIPT)
mpu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mpu)

OLD_EXAMPLE = """\
# performance.service — old header
# Activated by the agent's perf controller when llama-server wakes up.

[Unit]
Description=LLM Systems performance profile (CPU governor + accelerators)
ConditionPathExists=/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes

# CPU governor
ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g" 2>/dev/null || true; done'

# NVIDIA GPU persistence + power-limit (W)
# ExecStart=/usr/bin/nvidia-smi -pm 1
# ExecStart=/usr/bin/nvidia-smi -pl 350

# NZXT Kraken pump + fans
# ExecStart=/usr/bin/liquidctl --match Kraken set pump speed 20 30 35 95

[Install]
WantedBy=multi-user.target
"""

NEW_EXAMPLE = """\
# performance.service — new header
# Activated by the agent's power arbiter while any model is loading or awake.
# After each switch the agent reads back every sysfs / nvidia-smi line.

[Unit]
Description=LLM Systems performance profile (CPU governor + accelerators)
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes

# CPU governor → performance on every core
ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g" 2>/dev/null || true; done'

# NVIDIA GPU persistence + power-limit (W)
# ExecStart=/usr/bin/nvidia-smi -pm 1
# ExecStart=/usr/bin/nvidia-smi -pl 350

# NZXT Kraken pump + fans
# ExecStart=/usr/bin/liquidctl --match Kraken set pump speed 20 30 35 95

# Corsair PSU — Commander Pro fan curves (new example)
# ExecStart=/usr/bin/liquidctl --match Commander set fan speed 75

[Install]
WantedBy=multi-user.target
"""


def _operator_tuned(text: str) -> str:
    # Uncommented two lines, edited one value, added a line + a note, changed Description.
    t = text.replace("# ExecStart=/usr/bin/nvidia-smi -pm 1", "ExecStart=/usr/bin/nvidia-smi -pm 1")
    t = t.replace("# ExecStart=/usr/bin/nvidia-smi -pl 350", "ExecStart=/usr/bin/nvidia-smi -pl 300")
    t = t.replace("Description=LLM Systems performance profile (CPU governor + accelerators)", "Description=Rig A performance")
    t = t.replace("[Install]", "# rig A fans\nExecStart=/usr/bin/liquidctl --match Commander set fan speed 60\n\n[Install]")
    return t


def _assert_merged(out: str) -> None:
    assert "ConditionPathExists" not in out
    assert "ExecStart=/usr/bin/nvidia-smi -pm 1\n" in out
    assert "ExecStart=/usr/bin/nvidia-smi -pl 300\n" in out
    assert "# ExecStart=/usr/bin/nvidia-smi -pl 350" not in out
    assert "Description=Rig A performance\n" in out
    # The operator's Commander line takes the new example's slot, note included.
    assert ("# Corsair PSU — Commander Pro fan curves (new example)\n# rig A fans\n"
            "ExecStart=/usr/bin/liquidctl --match Commander set fan speed 60\n") in out
    assert "# ExecStart=/usr/bin/liquidctl --match Commander set fan speed 75" not in out
    assert "new header" in out and "old header" not in out
    assert "# ExecStart=/usr/bin/liquidctl --match Kraken set pump speed 20 30 35 95" in out
    assert out.count("ExecStart=/bin/bash -c 'for g in") == 1


def test_merge_two_way_keeps_operator_lines_and_drops_retired():
    ours = _operator_tuned(OLD_EXAMPLE)
    out = mpu.merge(ours, NEW_EXAMPLE)
    _assert_merged(out)
    assert mpu.merge(out, NEW_EXAMPLE) == out


def test_merge_three_way_matches_two_way_here():
    ours = _operator_tuned(OLD_EXAMPLE)
    assert mpu.merge(ours, NEW_EXAMPLE, OLD_EXAMPLE) == mpu.merge(ours, NEW_EXAMPLE)


def test_three_way_respects_operator_removed_and_commented_lines():
    ours = OLD_EXAMPLE.replace(
        "ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > \"$g\" 2>/dev/null || true; done'",
        "# ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > \"$g\" 2>/dev/null || true; done'",
    ).replace("After=multi-user.target\n\n[Service]", "[Service]")
    out = mpu.merge(ours, NEW_EXAMPLE, OLD_EXAMPLE)
    assert out.count("scaling_governor") == 1 and "\n# ExecStart=/bin/bash -c 'for g in" in out
    assert "After=multi-user.target" not in out


def test_three_way_upstream_description_change_only_wins_when_operator_left_it():
    newer = NEW_EXAMPLE.replace("Description=LLM Systems performance profile (CPU governor + accelerators)",
                                "Description=LLM Systems high-load profile")
    assert "Description=LLM Systems high-load profile\n" in mpu.merge(OLD_EXAMPLE, newer, OLD_EXAMPLE)
    ours = OLD_EXAMPLE.replace("Description=LLM Systems performance profile (CPU governor + accelerators)", "Description=Rig A")
    out = mpu.merge(ours, newer, OLD_EXAMPLE)
    assert "Description=Rig A\n" in out and "high-load" not in out


def test_edited_governor_value_replaces_shipped_line_in_place():
    ours = OLD_EXAMPLE.replace("echo performance > ", "echo schedutil > ")
    out = mpu.merge(ours, NEW_EXAMPLE)
    assert out.count("scaling_governor") == 1 and "echo schedutil > " in out


def test_untouched_old_install_becomes_the_new_example():
    assert mpu.merge(OLD_EXAMPLE, NEW_EXAMPLE) == NEW_EXAMPLE
    assert mpu.merge(OLD_EXAMPLE, NEW_EXAMPLE, OLD_EXAMPLE) == NEW_EXAMPLE


def test_operator_only_section_is_carried():
    ours = OLD_EXAMPLE + "\n[X-Site]\nRack=7\n"
    out = mpu.merge(ours, NEW_EXAMPLE)
    assert out.endswith("[Install]\nWantedBy=multi-user.target\n\n[X-Site]\nRack=7\n")


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SCRIPT), *args], capture_output=True, text=True)


def test_cli_backup_merge_and_second_run_noop(tmp_path):
    ex = tmp_path / "performance.service.example"
    dest = tmp_path / "performance.service"
    ex.write_text(NEW_EXAMPLE)
    dest.write_text(_operator_tuned(OLD_EXAMPLE))
    r = _cli("--example", str(ex), "--dest", str(dest), "--verbose")
    assert r.returncode == 0, r.stderr
    first, _, diff = r.stdout.partition("\n")
    assert first.startswith("merged performance.service (backup: ")
    assert diff.startswith("--- ") and "-ConditionPathExists" in diff
    backups = list(tmp_path.glob("performance.service.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == _operator_tuned(OLD_EXAMPLE)
    _assert_merged(dest.read_text())
    assert oct(dest.stat().st_mode & 0o777) == "0o644"
    r = _cli("--example", str(ex), "--dest", str(dest))
    assert r.returncode == 0 and r.stdout == "unchanged performance.service\n"
    assert len(list(tmp_path.glob("performance.service.bak-*"))) == 1


def test_cli_installs_when_absent_and_dry_run_writes_nothing(tmp_path):
    ex = tmp_path / "ex"
    dest = tmp_path / "powersave.service"
    ex.write_text(NEW_EXAMPLE)
    r = _cli("--example", str(ex), "--dest", str(dest), "--dry-run")
    assert r.stdout == "would-install powersave.service\n" and not dest.exists()
    r = _cli("--example", str(ex), "--dest", str(dest))
    assert r.stdout == "installed powersave.service\n" and dest.read_text() == NEW_EXAMPLE
    dest.write_text(_operator_tuned(OLD_EXAMPLE))
    r = _cli("--example", str(ex), "--dest", str(dest), "--dry-run")
    assert r.stdout.startswith("would-merge powersave.service\n")
    assert dest.read_text() == _operator_tuned(OLD_EXAMPLE)
    assert not list(tmp_path.glob("*.bak-*"))


def test_cli_force_replaces_with_backup(tmp_path):
    ex = tmp_path / "ex"
    dest = tmp_path / "performance.service"
    ex.write_text(NEW_EXAMPLE)
    dest.write_text(_operator_tuned(OLD_EXAMPLE))
    r = _cli("--example", str(ex), "--dest", str(dest), "--force")
    assert r.stdout.startswith("replaced performance.service (backup: ")
    assert dest.read_text() == NEW_EXAMPLE
    assert len(list(tmp_path.glob("performance.service.bak-*"))) == 1


def test_shipped_examples_merge_cleanly_from_the_pre_966_shape():
    for unit in ("performance.service", "powersave.service"):
        new = (_EXAMPLES / unit).read_text()
        old = new.replace("After=multi-user.target\n",
                          "After=multi-user.target\nConditionPathExists=/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor\n")
        assert "ConditionPathExists" in old
        assert mpu.merge(old, new) == new
        assert "ConditionPathExists" not in new


def test_installer_drives_the_merge_helper():
    text = _INSTALL_SH.read_text()
    assert text.count("\n    _merge_perf_units install\n") == 1 and text.count("\n  _merge_perf_units update\n") == 1
    # The update call happens before src/ is wiped, so the staged example is still there.
    assert text.index("_merge_perf_units update") < text.index('$SUDO find "$REPO_DIR_FOR_UPDATE" -mindepth 1')
    assert "--verbose)      VERBOSE=true; shift ;;" in text
    assert "PERF_BASE_DIR" in text and "override.conf" not in Path(_SCRIPT).read_text()


def _run_step7(tmp_path, **env) -> subprocess.CompletedProcess:
    """Run install.sh's _merge_perf_units as bash, with /etc/systemd/system redirected."""
    text = _INSTALL_SH.read_text()
    start = text.index("_merge_perf_units() {")
    end = text.index("\n}\n", start) + 3
    block = text[start:end].replace("/etc/systemd/system/", f"{tmp_path}/sd/")
    (tmp_path / "sd").mkdir(exist_ok=True)
    mode = "install" if env.pop("INSTALL_PERF_UNITS", "false") == "true" else "update"
    env.pop("DO_UPDATE", None)
    defaults = dict(AGENT_OS="linux", FORCE_OVERWRITE_PERF="false", VERBOSE="false",
                    FROM_SELF_UPDATE="false", PERF_BASE_DIR="", INSTALL_DIR="/opt/agent",
                    SUDO="", PYTHON3=sys.executable, TMPL_DIR=str(_AGENT / "install"))
    defaults.update(env)
    prelude = "set -euo pipefail\n" + "".join(f"{k}={v!r}\n" for k, v in defaults.items())
    prelude += "systemctl() { echo \"systemctl $*\"; }\n"
    return subprocess.run(["bash", "-c", prelude + block + f"\n_merge_perf_units {mode}\n"],
                          capture_output=True, text=True)


def test_install_sh_step_installs_merges_and_is_idempotent(tmp_path):
    sd = tmp_path / "sd"
    r = _run_step7(tmp_path, INSTALL_PERF_UNITS="true")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "✓ installed" in r.stdout and "systemctl daemon-reload" in r.stdout
    assert (sd / "performance.service").read_text() == (_EXAMPLES / "performance.service").read_text()
    assert (sd / "powersave.service").exists()

    # Operator tunes performance; a stale Condition line stands in for the old template.
    tuned = (sd / "performance.service").read_text().replace(
        "# ExecStart=/usr/bin/nvidia-smi -pl 350", "ExecStart=/usr/bin/nvidia-smi -pl 320"
    ).replace("After=multi-user.target\n", "After=multi-user.target\nConditionPathExists=/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor\n")
    (sd / "performance.service").write_text(tuned)

    r = _run_step7(tmp_path, DO_UPDATE="true", VERBOSE="true")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "✓ merged " in r.stdout and "(backup: " in r.stdout and "-ConditionPathExists" in r.stdout
    assert f"= {sd}/powersave.service unchanged" in r.stdout
    assert r.stdout.count("systemctl daemon-reload") == 1
    merged = (sd / "performance.service").read_text()
    assert "ExecStart=/usr/bin/nvidia-smi -pl 320\n" in merged and "ConditionPathExists" not in merged
    assert len(list(sd.glob("performance.service.bak-*"))) == 1

    r = _run_step7(tmp_path, DO_UPDATE="true")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "unchanged" in r.stdout and "daemon-reload" not in r.stdout
    assert len(list(sd.glob("*.bak-*"))) == 1


def test_install_sh_update_alone_adds_nothing_and_self_update_only_reports(tmp_path):
    sd = tmp_path / "sd"
    r = _run_step7(tmp_path, DO_UPDATE="true")
    assert r.returncode == 0 and r.stdout.strip() == "" and not list(sd.iterdir())

    stale = (_EXAMPLES / "powersave.service").read_text().replace(
        "After=multi-user.target\n", "After=multi-user.target\nConditionPathExists=/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor\n")
    (sd / "powersave.service").write_text(stale)
    r = _run_step7(tmp_path, DO_UPDATE="true", FROM_SELF_UPDATE="true")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "merging needs root" in r.stdout and "install.sh --update" in r.stdout
    assert (sd / "powersave.service").read_text() == stale and "daemon-reload" not in r.stdout
