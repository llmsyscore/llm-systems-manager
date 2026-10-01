#!/usr/bin/env bash
# Admin sign-in shared by the CI smoke scripts; source it. Reads MGR_URL,
# MGR_DIR (manager install root), ADMIN_USER and ADMIN_PW.
MGR_URL="${MGR_URL:-http://127.0.0.1:5000}"
MGR_DIR="${MGR_DIR:-/opt/llm-systems-manager}"
ADMIN_USER="${ADMIN_USER:-llmadmin}"
ADMIN_PW="${ADMIN_PW:-ci-admin-rotated-pw}"

# _ci_code ARGS… — HTTP status of a curl call; 000 when it cannot connect.
_ci_code() { curl -s -o /dev/null -w '%{http_code}' -m 10 "$@" || true; }

# _ci_login JAR USER PW — fresh session in JAR; prints the /login status.
_ci_login() {
  rm -f "$1"
  _ci_code -c "$1" --data-urlencode "username=$2" --data-urlencode "password=$3" "$MGR_URL/login"
}

# ci_temp_password — "<user> <password>" from the manager's admin password tool.
ci_temp_password() {
  runuser -u "$(stat -c %U "$MGR_DIR/data")" -- "$MGR_DIR/llm-systems-manager/venv/bin/python" \
    "$MGR_DIR/llm-systems-manager/backend/admin_password.py" reset --yes --porcelain --user "$ADMIN_USER"
}

# ci_first_signin JAR USER TEMP_PW — signs in with a temporary password, checks the
# forced change, sets ADMIN_PW. Prints the reason and returns 1 on a mismatch.
ci_first_signin() {
  local jar="$1" user="$2" temp="$3" c body
  c="$(_ci_login "$jar" "$user" "$temp")"
  case "$c" in 302|303) : ;; *) echo "sign-in with the temporary password returned $c"; return 1 ;; esac
  c="$(_ci_code -b "$jar" "$MGR_URL/api/me")"
  if [ "$c" != "403" ]; then echo "temporary password was not held at the change step (/api/me returned $c)"; return 1; fi
  body="$(jq -n --arg c "$temp" --arg n "$ADMIN_PW" '{current_password: $c, new_password: $n}')"
  c="$(_ci_code -b "$jar" -X POST -H 'Content-Type: application/json' -d "$body" "$MGR_URL/api/account/password")"
  if [ "$c" != "200" ]; then echo "password change returned $c"; return 1; fi
  c="$(_ci_code -b "$jar" "$MGR_URL/api/me")"
  if [ "$c" != "200" ]; then echo "/api/me after the password change returned $c"; return 1; fi
}

# ci_admin_login JAR — admin session in JAR with ADMIN_PW, set through a temporary
# password from the tool when needed. Prints the reason and returns 1 on failure.
ci_admin_login() {
  local jar="$1" c user="" temp=""
  c="$(_ci_login "$jar" "$ADMIN_USER" "$ADMIN_PW")"
  case "$c" in 302|303) return 0 ;; esac
  read -r user temp < <(ci_temp_password 2>/dev/null) || true
  if [ -z "$temp" ]; then echo "$c, and the admin password tool gave no temporary password"; return 1; fi
  ci_first_signin "$jar" "$user" "$temp"
}
