#!/usr/bin/env bash
# Perf-unit merge oracle for CI (#976): drives agent/install/install.sh on a real
# systemd host against a stub manager and asserts what the installer does to
# /etc/systemd/system/performance.service and powersave.service:
#   1. install over an old-shape unit with operator edits: merged, backup, daemon-reload
#   2. --update with an unchanged upstream: no-op, no new backup
#   3. --update with a changed upstream example: 3-way merge keeps operator lines
#   4. --from-self-update as the agent user: reports only, unit untouched
#   5. root --update afterwards applies what the self-update reported
# The stub serves /api/agents (reachability + collision check) and
# /api/agent-tarball (the "upstream" the update fetches). Run as root from the
# repo checkout.
set -euo pipefail

REPO="${REPO:-$PWD}"
INSTALL_DIR="${INSTALL_DIR:-/opt/llm-systems-agent}"
AGENT_USER="${AGENT_USER:-llmsys}"
MGR_PORT="${MGR_PORT:-5000}"
MGR_URL="http://127.0.0.1:$MGR_PORT"
SD=/etc/systemd/system
PERF="$SD/performance.service"
SAVE="$SD/powersave.service"
INSTALL_SH="$REPO/agent/install/install.sh"
EXAMPLE="$REPO/agent/install/examples/performance.service"
WORK="$(mktemp -d -t perf-oracle.XXXXXX)"
LOG="$WORK/install.log"
STUB_PID=""

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ FAIL: $*"; [[ -f "$LOG" ]] && { echo "── last installer output ──"; tail -60 "$LOG"; }; exit 1; }
cleanup() { if [[ -n "$STUB_PID" ]]; then kill "$STUB_PID" 2>/dev/null || true; fi; rm -rf "$WORK"; }
trap cleanup EXIT

[[ "$(id -u)" == "0" ]] || fail "run as root"
[[ -f "$INSTALL_SH" && -f "$EXAMPLE" ]] || fail "run from the repo checkout (REPO=$REPO)"
[[ -d /run/systemd/system ]] || fail "systemd is not running here"

# ── stub manager: registry probe + the agent tarball the update fetches ──────
mkdir -p "$WORK/stub"
cat > "$WORK/stub/server.py" <<'PY'
import http.server, os, sys
ROOT = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/agents":
            return self._send(200, b'{"agents": []}', "application/json")
        if p == "/health":
            return self._send(200, b'{"status": "ok"}', "application/json")
        if p == "/api/agent-tarball":
            f = os.path.join(ROOT, "agent.tar.gz")
            if not os.path.exists(f):
                return self._send(404, b"no tarball staged", "text/plain")
            with open(f, "rb") as fh:
                return self._send(200, fh.read(), "application/gzip")
        return self._send(404, b"nope", "text/plain")
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
PY
python3 "$WORK/stub/server.py" "$WORK/stub" "$MGR_PORT" &
STUB_PID=$!
for _ in $(seq 1 20); do
  curl -fsS "$MGR_URL/health" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS "$MGR_URL/api/agents" | grep -q '"agents"' || fail "stub manager did not come up on $MGR_URL"
pass "stub manager on $MGR_URL"

# stage_upstream MUTATOR — tarball of the checkout's agent/ (under agent/), with
# the performance example edited by MUTATOR (a bash function taking the path).
stage_upstream() {
  local mut="$1" tree="$WORK/stub/tree"
  rm -rf "$tree"; mkdir -p "$tree"
  cp -a "$REPO/agent" "$tree/agent"
  rm -rf "$tree/agent/tests" "$tree/agent/__pycache__"
  "$mut" "$tree/agent/install/examples/performance.service"
  tar -C "$tree" -czf "$WORK/stub/agent.tar.gz" agent
}
mut_none() { :; }
mut_v2() {
  sed -i 's/^# This is a STARTING POINT\. Tune it for your hardware:/# This is a STARTING POINT (v2). Tune it for your hardware:/' "$1"
  printf '\n# CI upstream example (v2)\n# ExecStart=/usr/bin/echo ci-upstream-v2\n' >> "$1"
}
mut_v3() {
  mut_v2 "$1"
  printf '\n# CI upstream example (v3)\n# ExecStart=/usr/bin/echo ci-upstream-v3\n' >> "$1"
}

backups() { find "$SD" -maxdepth 1 -name 'performance.service.bak-*' | wc -l; }
unit_has() { grep -qF -- "$1" "$PERF"; }
assert_operator_lines() {
  unit_has "ExecStart=/usr/bin/nvidia-smi -pm 1" || fail "operator-uncommented nvidia -pm line lost"
  unit_has "ExecStart=/usr/bin/nvidia-smi -pl 300" || fail "operator-edited nvidia -pl 300 lost"
  unit_has "# CI operator note" || fail "operator note lost"
  unit_has "ExecStart=/usr/bin/true" || fail "operator-added ExecStart lost"
  if ! unit_has "[X-Operator]" || ! unit_has "Note=keep me"; then fail "operator section lost"; fi
  if grep -q '^ExecStart=/usr/bin/nvidia-smi -pl 350' "$PERF"; then fail "shipped -pl 350 came back live next to the operator's -pl 300"; fi
}

# ── seed: a pre-#966 unit with operator edits; no powersave.service ──────────
id "$AGENT_USER" >/dev/null 2>&1 || useradd -r -m -d "/home/$AGENT_USER" -s /bin/bash "$AGENT_USER"
rm -f "$PERF" "$SAVE" "$SD"/performance.service.bak-* "$SD"/powersave.service.bak-*
{
  sed -e 's|^After=multi-user.target$|After=multi-user.target\nConditionPathExists=/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor|' \
      -e 's|^# ExecStart=/usr/bin/nvidia-smi -pm 1$|ExecStart=/usr/bin/nvidia-smi -pm 1|' \
      -e 's|^# ExecStart=/usr/bin/nvidia-smi -pl 350$|ExecStart=/usr/bin/nvidia-smi -pl 300|' \
      -e 's|^#   2\. Edit this file in place.*$|#   2. (old header line the operator never touched)|' "$EXAMPLE"
  printf '\n# CI operator note\nExecStart=/usr/bin/true\n\n[X-Operator]\nNote=keep me\n'
} > "$PERF"
grep -q ConditionPathExists "$PERF" || fail "seed unit lacks the old Condition line"
pass "seeded old-shape $PERF with operator edits; no $SAVE"

# ── 1. fresh install merges the old unit and installs the missing one ────────
echo "── 1. install --install-perf-units"
if ! bash "$INSTALL_SH" --manager-url "$MGR_URL" --description "ci perf oracle" --role system_only \
       --install-perf-units --no-start </dev/null >"$LOG" 2>&1; then
  fail "install.sh exited non-zero"
fi
grep -qE "✓ merged performance.service|merged /etc/systemd/system/performance.service" "$LOG" || fail "installer did not report the merge"
grep -qF "✓ installed $SAVE" "$LOG" || fail "installer did not install $SAVE"
grep -qF "daemon-reload complete" "$LOG" || fail "no daemon-reload after the merge"
if unit_has "ConditionPathExists"; then fail "retired Condition line survived the merge"; fi
unit_has "#   2. Edit this file in place" || fail "upstream header line did not refresh"
assert_operator_lines
[[ "$(backups)" == "1" ]] || fail "expected one backup after the install merge, got $(backups)"
cmp -s "$SAVE" "$REPO/agent/install/examples/powersave.service" || fail "$SAVE is not the shipped example"
systemctl cat performance >/dev/null 2>&1 || fail "systemd does not know performance.service after daemon-reload"
systemctl cat performance | grep -qF "nvidia-smi -pl 300" || fail "systemd sees stale unit content"
pass "install merged the old unit (backup, operator lines kept, Condition dropped) and installed powersave.service"

# The update path fetches from the manager with the agent's token; stage one.
install -d -o "$AGENT_USER" -g "$AGENT_USER" "$INSTALL_DIR/data"
printf 'ci-token\n' > "$INSTALL_DIR/data/token"
chown "$AGENT_USER:$AGENT_USER" "$INSTALL_DIR/data/token"
export LLMSYS_ALLOW_INSECURE_UPDATE=1

# ── 2. update with an unchanged upstream: nothing moves ──────────────────────
echo "── 2. --update, upstream unchanged"
stage_upstream mut_none
bash "$INSTALL_SH" --update </dev/null >"$LOG" 2>&1 || fail "update (unchanged) exited non-zero"
grep -qF "= $PERF unchanged" "$LOG" || fail "performance.service was not reported unchanged"
grep -qF "= $SAVE unchanged" "$LOG" || fail "powersave.service was not reported unchanged"
[[ "$(backups)" == "1" ]] || fail "an unchanged update wrote a backup"
assert_operator_lines
pass "update with an unchanged upstream is a no-op"

# ── 3. update with a changed upstream example: 3-way merge ───────────────────
echo "── 3. --update, upstream example v2"
stage_upstream mut_v2
bash "$INSTALL_SH" --update </dev/null >"$LOG" 2>&1 || fail "update (v2) exited non-zero"
grep -qE "merged /etc/systemd/system/performance.service" "$LOG" || fail "v2 update did not report a merge"
unit_has "# ExecStart=/usr/bin/echo ci-upstream-v2" || fail "new upstream example line missing"
unit_has "STARTING POINT (v2)" || fail "upstream header change missing"
assert_operator_lines
[[ "$(backups)" == "2" ]] || fail "expected two backups after the v2 merge, got $(backups)"
systemctl cat performance | grep -qF "ci-upstream-v2" || fail "systemd sees stale unit content after v2"
pass "changed upstream merged in, operator lines intact, backup written"

# ── 4. self-update as the agent user: reports, never writes ──────────────────
echo "── 4. --from-self-update as $AGENT_USER, upstream example v3"
stage_upstream mut_v3
# src/ is wiped after every update, so the self-update runs the checkout's installer (the
# fetch repopulates src/agent and the merge reads the fetched example).
# shellcheck disable=SC2024  # the log lives in root's work dir on purpose; the redirect is root's
sudo -u "$AGENT_USER" LLMSYS_ALLOW_INSECURE_UPDATE=1 bash "$INSTALL_SH" \
  --from-self-update </dev/null >"$LOG" 2>&1 || fail "self-update exited non-zero"
grep -qF "merging needs root" "$LOG" || fail "self-update did not report the pending merge"
if unit_has "ci-upstream-v3"; then fail "self-update wrote the unit"; fi
[[ "$(backups)" == "2" ]] || fail "self-update wrote a backup"
pass "self-update reported the pending merge and left the unit alone"

# ── 5. root update applies what the self-update reported ─────────────────────
echo "── 5. root --update after the self-update"
bash "$INSTALL_SH" --update </dev/null >"$LOG" 2>&1 || fail "update (v3) exited non-zero"
unit_has "ci-upstream-v3" || fail "v3 upstream line missing after the root update"
assert_operator_lines
[[ "$(backups)" == "3" ]] || fail "expected three backups after the v3 merge, got $(backups)"
pass "root update merged v3; operator lines intact"

echo "perf-unit merge oracle: all checks passed"
