#!/usr/bin/env bash
# Installs the cswap suite on this machine from the folder it is in: our cswap
# (from the wheel in cswap/, so nothing is fetched from the original
# claude-swap project), the switch hook, cswap-satellite, cswap-remote,
# api-worker and the VS Code extension. Running it again updates. --help for
# the options.
set -euo pipefail

usage() {
  cat <<'EOF'
用法：./install.sh [选项]
  （不加选项）     装 cswap（我们的版本）、切换钩子、cswap-satellite、cswap-remote、api-worker、VS Code 插件
  --auto           另外把自动切换（cswap auto）装成后台服务，一直运行
  --remote         另外把手机页面（cswap-remote）装成后台服务（需要 tmux 和 Tailscale）
  --no-api-worker  不装 api-worker
  --no-vscode      不装 VS Code 插件
再运行一次就是更新。
EOF
}

here="$(cd "$(dirname "$0")" && pwd)"
prefix="${XDG_DATA_HOME:-$HOME/.local/share}/cswap-suite"
bin="$HOME/.local/bin"
auto=0 remote=0 api=1 vscode=1
for a in "$@"; do
  case "$a" in
    --auto) auto=1 ;;
    --remote) remote=1 ;;
    --no-api-worker) api=0 ;;
    --no-vscode) vscode=0 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "不认识的选项：$a" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v uv >/dev/null; then
  echo "先装 uv（Python 工具管理器，需要时会自己准备 Python 3.12+）：" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  echo "装好后重开终端，再运行 $0" >&2
  exit 1
fi

# The suite lives in one fixed place, so the commands linked below do not
# point into the folder it was unpacked in.
if [ "$here" != "$prefix" ]; then
  mkdir -p "$(dirname "$prefix")"
  rm -rf "$prefix.new"
  cp -R "$here" "$prefix.new"
  rm -rf "$prefix"
  mv "$prefix.new" "$prefix"
fi
echo "== 安装 $(head -1 "$prefix/VERSION") 到 $prefix"

# cswap: replaces any earlier cswap, including the original claude-swap.
uv tool install --force --python ">=3.12" "$(ls "$prefix"/cswap/claude_swap-*.whl | head -1)"
mkdir -p "$bin"
for link in cswap-on-switch:cswap-hooks/on-switch.sh cswap-satellite:cswap-satellite/cswap_satellite.py \
  cswap-remote:cswap-remote/cswap_remote.py; do
  chmod +x "$prefix/${link#*:}"
  ln -sfn "$prefix/${link#*:}" "$bin/${link%%:*}"
done
"$bin/cswap" config set autoswitch.onSwitchCommand "$bin/cswap-on-switch" >/dev/null
echo "命令已装到 $bin：cswap、cswap-on-switch（每次切换后运行）、cswap-satellite、cswap-remote"

if [ "$api" = 1 ]; then
  "$prefix/api-worker/install.sh"
  if command -v claude >/dev/null; then
    # A folder marketplace is read in place, so it must be the installed copy,
    # not wherever an earlier install (or a source checkout) registered it.
    registered="$(claude plugin marketplace list --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit()
for m in d if isinstance(d, list) else d.get("marketplaces", []):
    if m.get("name") == "api-worker":
        print(m.get("path") or m.get("installLocation") or "")
' || true)"
    if [ -n "$registered" ] && [ "$registered" != "$prefix/api-worker" ]; then
      claude plugin marketplace remove api-worker >/dev/null 2>&1 || true
    fi
    claude plugin marketplace add "$prefix/api-worker" >/dev/null 2>&1 ||
      claude plugin marketplace update api-worker >/dev/null 2>&1 || true
    if claude plugin install api-worker@api-worker >/dev/null 2>&1 ||
      claude plugin update api-worker@api-worker >/dev/null 2>&1; then
      echo "Claude Code 的 api-worker 插件已装（重开 Claude Code 生效）"
    else
      echo "api-worker 插件没装上，可以手动运行：claude plugin marketplace add $prefix/api-worker && claude plugin install api-worker@api-worker"
    fi
  else
    echo "还没装 Claude Code：装好后再运行一次 $prefix/install.sh，就会装上 api-worker 插件"
  fi
fi

if [ "$vscode" = 1 ]; then
  vsix="$(ls "$prefix"/cswap-status-*.vsix | head -1)"
  if command -v code >/dev/null && code --install-extension "$vsix" --force >/dev/null 2>&1; then
    echo "VS Code 插件已装：重载窗口（Developer: Reload Window）后左侧出现 cswap 图标"
  else
    echo "VS Code 插件：在 VS Code 连到这台机器的终端里运行  code --install-extension $vsix"
  fi
fi

# Background services. Already running ones are restarted to pick up the new
# version; new ones only with --auto / --remote.
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
have_systemd() { command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; }
service() { # name, wanted, command to run by hand without systemd
  if [ "$2" = 1 ]; then
    if have_systemd; then
      mkdir -p "$units"
      cp "$prefix/systemd/$1.service" "$units/"
      systemctl --user daemon-reload
      systemctl --user enable --now "$1.service" >/dev/null 2>&1
      systemctl --user restart "$1.service"
      echo "后台服务 $1 已运行（查看：systemctl --user status $1）"
    else
      echo "这台机器没有 systemd 用户服务，$1 没装成服务；可以在 tmux 里运行：$3"
    fi
  elif have_systemd && systemctl --user is-active --quiet "$1.service"; then
    systemctl --user restart "$1.service"
    echo "后台服务 $1 已重启到新版本"
  fi
}
service cswap-auto "$auto" "cswap auto"
service cswap-remote "$remote" "cswap-remote serve --host tailscale"

case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "注意：$bin 不在 PATH 里，在 ~/.bashrc 里加一行：export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
cat <<EOF

装好了。接下来加账号（任选一种）：
  · 在这台机器登录：VS Code 左侧 cswap 侧栏点 ＋；或者终端里登录 Claude Code 后运行 cswap add
  · 用服务器上已经登录的账号（这台不登录）：cswap-satellite setup --server 服务器 && cswap-satellite install-timer
要自动切换常驻后台：$prefix/install.sh --auto
EOF
