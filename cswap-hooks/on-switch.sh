#!/usr/bin/env bash
# cswap autoswitch.onSwitchCommand: what to do after every account switch,
# automatic (CSWAP_TRIGGER = the trigger) or manual (CSWAP_TRIGGER=manual).
#   cswap config set autoswitch.onSwitchCommand ~/.local/bin/cswap-on-switch
# where ~/.local/bin/cswap-on-switch is a symlink to this file (see README.md).
# Logs account numbers and the trigger only, never the email.
set -u
self="$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")"   # through the symlink
here="$(cd "$(dirname "$self")" && pwd)"
state="${XDG_STATE_HOME:-$HOME/.local/state}/cswap-hooks"
log="$state/hook.log"
mkdir -p "$state"

# A manual switch right after an automatic one: run one after the other.
exec 9>>"$state/hook.lock"
command -v flock >/dev/null 2>&1 && flock -w 30 9

{
  printf '%s switch %s -> %s (%s)\n' "$(date '+%F %T')" "${CSWAP_FROM:-?}" "${CSWAP_TO:-?}" "${CSWAP_TRIGGER:-?}"
  # same effort on every account; writes settings.json only when something differs
  python3 "$here/pin-effort.py" 2>&1 | sed 's/^/  pin-effort: /'
} >>"$log"

# Keep the log under about 1000 lines.
if [ "$(wc -l <"$log")" -gt 1000 ]; then
  tail -n 800 "$log" >"$log.tmp" && mv "$log.tmp" "$log"
fi
exit 0
