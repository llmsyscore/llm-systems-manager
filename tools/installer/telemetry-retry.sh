#!/usr/bin/env bash
# telemetry-retry.sh PENDING ENDPOINT LOG PYTHON ROOT — two more delivery attempts, then stop.
# Delays default to 60 and 300 seconds; LLMSYS_TELEMETRY_RETRY_DELAYS="a b" overrides (tests).
set -u
PEND="$1"; EP="$2"; LOGF="$3"; PY="$4"; ROOT="$5"
read -r D1 D2 <<<"${LLMSYS_TELEMETRY_RETRY_DELAYS:-60 300}"
n=2
for d in "$D1" "$D2"; do
  sleep "$d"
  [[ -f "$PEND" ]] || exit 0
  if curl -fsS --max-time 5 -o /dev/null -H 'Content-Type: application/json' --data-binary "@$PEND" "$EP" 2>/dev/null \
     || "$PY" -I "$ROOT/llm-systems-manager/backend/telemetry.py" send --endpoint "$EP" --file "$PEND" 2>/dev/null; then
    mv -f "$PEND" "$(dirname "$PEND")/telemetry-last.json"
    printf '%s attempt %s ok\n' "$(date -u +%FT%TZ)" "$n" >> "$LOGF"
    exit 0
  fi
  printf '%s attempt %s failed\n' "$(date -u +%FT%TZ)" "$n" >> "$LOGF"
  n=$((n + 1))
done
exit 0
