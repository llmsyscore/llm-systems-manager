#!/usr/bin/env bash
# First sign-in oracle for CI (#1160). Reads the temporary admin password the
# installer, updater or admin password tool printed into LOG, signs in with it,
# and asserts the forced change. Run as root on the manager host.
#
# Usage: ci-first-signin.sh LOG
# Env: MGR_URL, MGR_DIR, ADMIN_PW (the password set at the change step).
set -euo pipefail

LOG="${1:?usage: ci-first-signin.sh LOG}"
# shellcheck source=tools/installer/ci-admin-login.sh
. "$(dirname "${BASH_SOURCE[0]}")/ci-admin-login.sh"

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ FAIL: $*"; exit 1; }

JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

for _ in $(seq 1 60); do
  if [ "$(_ci_code "$MGR_URL/health")" = "200" ]; then break; fi
  sleep 1
done

echo "── 1. The log shows the sign-in name and a temporary password ───────"
USER_SHOWN="$(sed -n 's/.*Sign in as:[[:space:]]*//p' "$LOG" | tail -1)"
TEMP_SHOWN="$(sed -n 's/.*Temporary password:[[:space:]]*//p' "$LOG" | tail -1)"
if [ -z "$USER_SHOWN" ] || [ -z "$TEMP_SHOWN" ]; then fail "no sign-in name or temporary password in $LOG"; fi
if [ "${#TEMP_SHOWN}" -lt 12 ]; then fail "temporary password is shorter than 12 characters"; fi
pass "sign-in name and temporary password are shown"

echo "── 2. The temporary password is not in the data or config directories ─"
if grep -rqF -- "$TEMP_SHOWN" "$MGR_DIR/data" "$MGR_DIR/config" 2>/dev/null; then
  fail "the temporary password is on disk under $MGR_DIR"
fi
pass "not found under the data or config directories"

echo "── 3. Sign in, get held at the change step, set a new password ──────"
if ! why="$(ci_first_signin "$JAR" "$USER_SHOWN" "$TEMP_SHOWN")"; then fail "$why"; fi
c="$(_ci_code -b "$JAR" "$MGR_URL/api/admin/audit-log")"
if [ "$c" != "200" ]; then fail "admin route after the change returned $c (want 200)"; fi
pass "temporary password signs in once, a change is forced, admin routes open after it"

echo "── 4. The temporary and the old shipped password no longer sign in ──"
c="$(_ci_login "$JAR" "$USER_SHOWN" "$TEMP_SHOWN")"
if [ "$c" != "401" ]; then fail "temporary password still signs in after the change ($c)"; fi
c="$(_ci_login "$JAR" "$USER_SHOWN" "$USER_SHOWN")"
if [ "$c" != "401" ]; then fail "the old shipped password signs in ($c)"; fi
c="$(_ci_login "$JAR" "$USER_SHOWN" "$ADMIN_PW")"
case "$c" in 302|303) : ;; *) fail "the new password does not sign in ($c)" ;; esac
pass "only the new password signs in"

echo
echo "ALL FIRST SIGN-IN ASSERTIONS PASSED"
