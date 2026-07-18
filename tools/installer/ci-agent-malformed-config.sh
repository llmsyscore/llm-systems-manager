#!/usr/bin/env bash
# Agent malformed-config -> no-restart-loop oracle for CI (#418). Plants an
# un-parseable agent_config.yaml, starts the unit, and asserts it exits 2 and
# systemd does NOT restart it (RestartPreventExitStatus=2 suppresses the
# Restart=always relaunch). Chains onto a host where the fresh-install job
# already installed + started llm-systems-agent. DESTRUCTIVE — run it LAST, it
# restores the good config at the end. Run as root.
set -euo pipefail

UNIT="${AGENT_UNIT:-llm-systems-agent}"

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ FAIL: $*"; exit 1; }
show() { systemctl show -p "$1" --value "$UNIT" 2>/dev/null; }

echo "── 0. Agent unit present; locate its config path + run user ─────────"
systemctl cat "$UNIT" >/dev/null 2>&1 || fail "unit $UNIT not found on this host"
WORKDIR="$(show WorkingDirectory)"
AGENT_USER="$(show User)"
[ -n "$WORKDIR" ] || fail "could not read WorkingDirectory from $UNIT"
[ -n "$AGENT_USER" ] || AGENT_USER=llmsys
# load() checks $cwd/agent_config.yaml FIRST (systemd cwd == WorkingDirectory),
# so a file here wins over every other candidate.
CONFIG="$WORKDIR/agent_config.yaml"
BACKUP="$WORKDIR/agent_config.yaml.ci-malformed-bak"
pass "unit=$UNIT workdir=$WORKDIR user=$AGENT_USER config=$CONFIG"

# Restore the pre-test config no matter how we exit, then re-arm the agent.
# PLANTED gates the rm: without it, a failure BEFORE we write the malformed
# file (e.g. the backup cp) would make restore() delete the original config.
HAD_CONFIG=0
PLANTED=0
restore() {
  if [ "$PLANTED" = 1 ]; then
    if [ "$HAD_CONFIG" = 1 ]; then mv -f "$BACKUP" "$CONFIG" 2>/dev/null || true
    else rm -f "$CONFIG" 2>/dev/null || true; fi
  else
    rm -f "$BACKUP" 2>/dev/null || true
  fi
  systemctl reset-failed "$UNIT" 2>/dev/null || true
  systemctl start "$UNIT" 2>/dev/null || true
}
trap restore EXIT

echo "── 1. Back up the good config + zero the restart counter ────────────"
if [ -f "$CONFIG" ]; then cp -a "$CONFIG" "$BACKUP"; HAD_CONFIG=1; fi
systemctl stop "$UNIT" 2>/dev/null || true
systemctl reset-failed "$UNIT" 2>/dev/null || true
pass "agent stopped; NRestarts reset to $(show NRestarts)"

echo "── 2. Plant an un-parseable agent_config.yaml ───────────────────────"
# 'foo: bar: baz' is a genuine ScannerError (mapping values not allowed) — it
# fails INSIDE the agent's try/except (llm-systems-agent.py:483-493) -> exit 2.
# A top-level list would PARSE then fail at data.items() (exit 1) — wrong path.
cat > "$CONFIG" <<'YAML'
# Deliberately malformed YAML planted by ci-agent-malformed-config.sh (#418).
foo: bar: baz
YAML
PLANTED=1
chown "$AGENT_USER" "$CONFIG" 2>/dev/null || true
chmod 0644 "$CONFIG"
pass "planted malformed config (owned by $AGENT_USER)"

echo "── 3. Start the agent; it must reach 'failed' within ~20s ───────────"
# Type=simple: start returns 0 immediately; the exit-2 lands ~1-2s later.
systemctl start "$UNIT" 2>/dev/null || true
reached=0
for _ in $(seq 1 20); do
  if [ "$(show ActiveState)" = "failed" ]; then reached=1; break; fi
  sleep 1
done
[ "$reached" = 1 ] || fail "unit never reached ActiveState=failed (state=$(show ActiveState) sub=$(show SubState))"
pass "unit reached failed (sub=$(show SubState))"

echo "── 4. Failure is the exit-2 config path, not a restart-limit hit ─────"
main_status="$(show ExecMainStatus)"; main_code="$(show ExecMainCode)"; result="$(show Result)"
[ "$main_status" = "2" ] || fail "ExecMainStatus=$main_status (want 2 — malformed-config exit)"
# ExecMainCode is the waitid() si_code: 1 = CLD_EXITED (exited normally), not a
# signal kill (CLD_KILLED=2). So exit-2 pairs with ExecMainCode=1.
[ "$main_code" = "1" ] || fail "ExecMainCode=$main_code (want 1 = CLD_EXITED, i.e. exited not signal-killed)"
[ "$result" = "exit-code" ] || fail "Result=$result (want exit-code, not start-limit-hit)"
pass "ExecMainStatus=2 (exited normally), Result=exit-code"

echo "── 5. No restart loop: NRestarts stays 0 across a settle window ──────"
n="$(show NRestarts)"
[ "$n" = "0" ] || fail "NRestarts=$n right after failure (RestartPreventExitStatus=2 did not suppress the relaunch)"
# The post-settle re-check below is the AUTHORITATIVE anti-loop assertion (the
# check above can pass transiently on the first exit even if prevention is off).
# Settle > 4x RestartSec (3s): a broken RestartPreventExitStatus would auto-
# relaunch here and bump NRestarts / flip ActiveState to activating.
sleep 15
n="$(show NRestarts)"; state="$(show ActiveState)"
[ "$n" = "0" ] || fail "NRestarts=$n after 15s settle — agent is restart-looping"
[ "$state" = "failed" ] || fail "ActiveState=$state after settle (want failed — an auto-restart is happening)"
pass "NRestarts=0 and still failed after 15s — no restart loop"

echo "── 6. Recovery: good config -> the agent starts again ───────────────"
trap - EXIT
if [ "$HAD_CONFIG" = 1 ]; then mv -f "$BACKUP" "$CONFIG"; else rm -f "$CONFIG"; fi
chown "$AGENT_USER" "$CONFIG" 2>/dev/null || true
systemctl reset-failed "$UNIT" 2>/dev/null || true
systemctl start "$UNIT" 2>/dev/null || true
recovered=0
for _ in $(seq 1 20); do
  if systemctl is-active --quiet "$UNIT"; then recovered=1; break; fi
  sleep 1
done
[ "$recovered" = 1 ] || fail "agent did not return to active after restoring the good config"
pass "agent active again with the good config"

echo
echo "ALL AGENT MALFORMED-CONFIG ASSERTIONS PASSED"
