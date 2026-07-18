#!/usr/bin/env bash
# Full-script uninstall oracle for CI (#418). Drives tools/installer/uninstall.sh
# through its REAL rm -rf + userdel path (the #416 refusal path is already
# covered in package-test.yml) and asserts the host is clean afterwards. Chains
# onto a host the fresh-install job left fully installed. DESTRUCTIVE — run it
# LAST. Run as root.
#
# confirm() in uninstall.sh returns 1 the instant stdin is not a TTY, so the
# top-level "Proceed?" gate aborts under `</dev/null` having removed NOTHING (a
# false green). We allocate a PTY with util-linux `script` and flood 'y' so
# every prompt (units, sudoers, dirs, caches, runtime user, InfluxDB) is taken.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNINSTALL="$SCRIPT_DIR/uninstall.sh"
RUN_USER="${LLMSYS_RUN_USER:-llmsys}"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ FAIL: $*"; echo "---- uninstall output ----"; cat "$OUT" || true; exit 1; }

echo "── 0. Preconditions: this host is actually installed ────────────────"
[ -f "$UNINSTALL" ] || fail "uninstall.sh not found at $UNINSTALL"
command -v script >/dev/null 2>&1 || fail "util-linux 'script' (PTY driver) not available"
[ -d /opt/llm-systems-manager ] || fail "/opt/llm-systems-manager absent — nothing to uninstall (host not installed?)"
# Snapshot the runtime user BEFORE removal so step 5's absence check is a real
# before/after delta, not a vacuous pass against a user that never existed.
getent passwd "$RUN_USER" >/dev/null 2>&1 \
  || fail "precondition: runtime user '$RUN_USER' not present pre-uninstall (set LLMSYS_RUN_USER for --user hosts)"
INFLUX_WAS_INSTALLED=0
if systemctl list-unit-files --no-legend 2>/dev/null | awk '{print $1}' | grep -qx influxdb.service; then
  INFLUX_WAS_INSTALLED=1
fi
pass "uninstall.sh present; host has an install to remove (user '$RUN_USER' present)"

echo "── 1. Drive the destructive uninstall through a PTY (answer y) ───────"
# `script -e` returns the child's exit code; `yes` gets SIGPIPE when script
# exits, so read script's status from PIPESTATUS (not the pipeline's).
set +e
yes 'y' | script -qec "bash '$UNINSTALL'" /dev/null > "$OUT" 2>&1
rc="${PIPESTATUS[1]}"
set -e
[ "$rc" = "0" ] || fail "uninstall.sh exited $rc (want 0)"
grep -q "Uninstall complete" "$OUT" || fail "uninstall did not reach the 'Uninstall complete' banner"
pass "uninstall.sh ran to completion (exit 0)"

echo "── 2. No llm-systems systemd units remain ───────────────────────────"
if systemctl list-unit-files --no-legend 2>/dev/null | awk '{print $1}' \
     | grep -qE '^llm-systems-(manager|alarm-engine|agent)\.service$'; then
  fail "an llm-systems unit is still registered with systemd"
fi
for u in manager alarm-engine agent; do
  for p in "/etc/systemd/system/llm-systems-$u.service" "/lib/systemd/system/llm-systems-$u.service"; do
    [ ! -e "$p" ] || fail "unit file survived: $p"
  done
  ! systemctl is-active --quiet "llm-systems-$u" || fail "llm-systems-$u is still active"
done
pass "no units registered, on disk, or active"

echo "── 3. No install trees remain ───────────────────────────────────────"
[ ! -e /opt/llm-systems-manager ] || fail "/opt/llm-systems-manager survived rm -rf"
[ ! -e /opt/llm-systems-agent ]   || fail "/opt/llm-systems-agent survived rm -rf"
pass "both /opt trees removed"

echo "── 4. No leftover sudoers fragments or log dir ──────────────────────"
[ ! -e /etc/sudoers.d/llm-systems-manager ] || fail "sudoers fragment survived: manager"
[ ! -e /etc/sudoers.d/llm-systems-agent ]   || fail "sudoers fragment survived: agent"
[ ! -e /var/log/llm-systems-manager ]       || fail "/var/log/llm-systems-manager survived"
pass "no sudoers fragments or log dir left"

echo "── 5. Runtime user removed (userdel -r) ─────────────────────────────"
if getent passwd "$RUN_USER" >/dev/null 2>&1; then fail "runtime user '$RUN_USER' survived uninstall"; fi
[ ! -e "/home/$RUN_USER" ] || fail "/home/$RUN_USER survived userdel -r"
pass "runtime user '$RUN_USER' and its home removed"

echo "── 6. InfluxDB purged (blanket-y also runs the InfluxDB block) ──────"
# The 'y' stream answers the InfluxDB prompts too, so a host that had it
# installed must end with the service gone and its data dir removed.
if [ "$INFLUX_WAS_INSTALLED" = 1 ]; then
  if systemctl list-unit-files --no-legend 2>/dev/null | awk '{print $1}' | grep -qx influxdb.service; then
    fail "influxdb.service still registered after uninstall"
  fi
  [ ! -e /var/lib/influxdb ] || fail "/var/lib/influxdb survived uninstall"
  pass "influxdb.service removed and /var/lib/influxdb gone"
else
  pass "InfluxDB was not installed on this host — nothing to purge"
fi

echo
echo "ALL UNINSTALL ASSERTIONS PASSED (host is clean)"
