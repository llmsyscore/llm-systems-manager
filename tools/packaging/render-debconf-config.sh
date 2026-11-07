#!/usr/bin/env bash
# Renders the debconf config script: embeds the detector in place of the
# marker line. Usage: render-debconf-config.sh CONFIG DETECTOR OUT
set -euo pipefail
cfg="${1:?}"; det="${2:?}"; out="${3:?}"
term=LLMSYS_DETECT_EOF
if grep -qx "$term" "$det"; then
  echo "ERROR: $det contains the heredoc terminator $term" >&2; exit 1
fi
marker='  # LLMSYS_EMBED_DETECT'
grep -qxF "$marker" "$cfg" || { echo "ERROR: embed marker missing in $cfg" >&2; exit 1; }
: > "$out"
while IFS= read -r line; do
  if [ "$line" = "$marker" ]; then
    printf '  python3 -I - --sh 2>/dev/null <<'"'%s'"'\n' "$term" >> "$out"
    cat "$det" >> "$out"
    printf '%s\n  return 0\n' "$term" >> "$out"
  else
    printf '%s\n' "$line" >> "$out"
  fi
done < "$cfg"
chmod 0755 "$out"
