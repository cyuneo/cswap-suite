#!/usr/bin/env bash
# Installs the Python side of api-worker: a private venv with claude-agent-sdk
# and an `api-worker` command in ~/.local/bin. The Claude Code plugin is
# installed separately (see README).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
venv="${XDG_DATA_HOME:-$HOME/.local/share}/api-worker/venv"
# The SDK range the worker is tested with: a reinstall keeps a version in it
# (no download) instead of fetching the newest each time.
sdk='claude-agent-sdk>=0.2.174,<0.3'
if command -v uv >/dev/null; then
  [ -x "$venv/bin/python" ] || uv venv -q --python 3.12 "$venv" 2>/dev/null || uv venv -q "$venv"
  uv pip install -q --python "$venv/bin/python" "$sdk"
else
  [ -x "$venv/bin/python" ] || python3 -m venv "$venv"
  "$venv/bin/pip" install -q "$sdk"
fi
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/api-worker" <<SH
#!/bin/sh
exec "$venv/bin/python" "$here/worker/api_worker.py" "\$@"
SH
chmod +x "$HOME/.local/bin/api-worker"
echo "装好了：$venv"
echo "下一步：api-worker set-key"
