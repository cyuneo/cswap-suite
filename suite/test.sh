#!/usr/bin/env bash
# Runs every test in the cswap suite; exits non-zero if any part fails.
#   suite/test.sh            all parts
#   suite/test.sh remote vscode   only these (names as listed below)
# Node for the JavaScript tests: `node` on PATH, else the one bundled with
# VS Code Server; without either those tests are skipped (and say so).
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
node="$(command -v node || ls -d "$HOME"/.vscode-server/cli/servers/Stable-*/server/node 2>/dev/null | tail -1)"
failed=() skipped=()

part() { # name, dir, command...
  local name=$1 dir=$2
  shift 2
  if [ ${#only[@]} -gt 0 ] && [[ " ${only[*]} " != *" $name "* ]]; then return; fi
  printf '== %-10s ' "$name"
  local out
  if out="$(cd "$root/$dir" && "$@" 2>&1)"; then
    echo "OK  $(printf '%s\n' "$out" | grep -E 'passed|^Ran |tests: OK|passed, 0 failed' | tail -1)"
  else
    echo "FAILED"
    printf '%s\n' "$out" | tail -25 | sed 's/^/   /'
    failed+=("$name")
  fi
}

vscode_tests() {
  for t in test/*.test.js; do "$node" "$t" || return 1; done
}

only=("$@")
part cswap claude-swap uv run --quiet pytest -q
part api-worker api-worker python3 -I -m unittest discover -s tests -q
part hooks cswap-hooks python3 -I -m unittest discover -s tests -q
part satellite cswap-satellite python3 -I -m unittest discover -s tests -q
part remote cswap-remote python3 -I -m unittest discover -s tests -q
part install suite python3 -I -m unittest discover -s tests -q
monitor_tests() { # its fake CLIs start with #!/usr/bin/env node
  PATH="$(dirname "$node"):$PATH" "$node" test/run.js
}

if [ -n "$node" ]; then
  part vscode cswap-status-vscode vscode_tests
  if [ -d "$root/agent-monitor" ]; then part monitor agent-monitor monitor_tests; fi
else
  skipped+=("vscode (no node)")
fi

[ ${#skipped[@]} -gt 0 ] && echo "skipped: ${skipped[*]}"
if [ ${#failed[@]} -gt 0 ]; then
  echo "FAILED: ${failed[*]}"
  exit 1
fi
echo "all tests passed"
