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
  （不加选项）     装 cswap（我们的版本）、切换钩子、cswap-satellite、cswap-remote、api-worker、VS Code 插件、Agent Monitor
  --auto           另外把自动切换（cswap auto）装成后台服务，一直运行
  --remote         另外把手机页面（cswap-remote）装成后台服务（需要 tmux 和 Tailscale）
  --no-api-worker  不装 api-worker
  --no-vscode      不装 VS Code 插件（cswap 侧栏和 Agent Monitor 都不装）
  --no-monitor     不装 Agent Monitor（看所有 Claude/Codex 会话；用包里带的版本，扩展市场上更新时也会更新）
  --force-vscode   VS Code 插件一律装包里的版本（默认已装的版本不比包里旧就不动）
再运行一次就是更新。
EOF
}

here="$(cd "$(dirname "$0")" && pwd)"
prefix="${XDG_DATA_HOME:-$HOME/.local/share}/cswap-suite"
bin="$HOME/.local/bin"
force_vscode=0 auto=0 remote=0 api=1 vscode=1 monitor=1
for a in "$@"; do
  case "$a" in
    --auto) auto=1 ;;
    --remote) remote=1 ;;
    --no-api-worker) api=0 ;;
    --no-vscode) vscode=0 ;;
    --no-monitor) monitor=0 ;;
    --force-vscode) force_vscode=1 ;;
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

# A folder that a newer bundle was unpacked over still holds the older
# packages: keep only the newest of each (by version, not by name order).
newest() { # glob matches -> the highest version, or nothing
  local f
  for f in "$@"; do if [ -e "$f" ]; then printf '%s\n' "$f"; fi; done | sort -V | tail -1
}
prune() { # glob matches -> remove all but the newest
  local keep f
  keep="$(newest "$@")"
  for f in "$@"; do
    if [ -e "$f" ] && [ "$f" != "$keep" ]; then rm -f "$f" && echo "删掉旧包 $(basename "$f")"; fi
  done
}
prune "$prefix"/cswap/claude_swap-*.whl
prune "$prefix"/cswap-status-*.vsix
prune "$prefix"/cyuneo-agent-monitor-*.vsix

# VS Code's command line: `code` from a terminal inside VS Code; otherwise
# (ssh, or a terminal whose VS Code window is gone) the newest VS Code Server
# on this machine, which installs into the same place. Empty: neither works.
code_cli() {
  if command -v code >/dev/null && code --list-extensions >/dev/null 2>&1; then
    echo code
    return
  fi
  local server
  # shellcheck disable=SC2012 # names are hashes: newest by time
  server="$(ls -dt "$HOME"/.vscode-server/cli/servers/Stable-*/server/bin/code-server 2>/dev/null | head -1 || true)"
  if [ -n "$server" ] && env -u VSCODE_IPC_HOOK_CLI "$server" --list-extensions >/dev/null 2>&1; then
    echo "$server"
  fi
}
vscode_cli="$(code_cli)"
vscode_run() { if [ "$vscode_cli" = code ]; then code "$@"; else env -u VSCODE_IPC_HOOK_CLI "$vscode_cli" "$@"; fi; }
# The version VS Code reports for an extension id, or nothing
ext_version() { vscode_run --list-extensions --show-versions 2>/dev/null | grep -i "^$1@" | cut -d@ -f2 || true; }
# Installs a vsix and checks VS Code now reports its version; prints why not
install_vsix() { # file, id, version
  local out
  if ! out="$(vscode_run --install-extension "$1" --force 2>&1)"; then
    printf '%s\n' "$out" | tail -3 | sed 's/^/   /'
    return 1
  fi
  [ "$(ext_version "$2")" = "$3" ] || { echo "   装完 VS Code 报告的版本是 $(ext_version "$2")，不是 $3"; return 1; }
}
# True when VS Code already has this version or a newer one (so the bundle's
# older copy must not replace it), unless --force-vscode
have_newer() { # id, version
  local have
  [ "$force_vscode" = 0 ] || return 1
  have="$(ext_version "$1")"
  [ -n "$have" ] && [ "$(printf '%s\n%s\n' "$2" "$have" | sort -V | tail -1)" = "$have" ]
}

# cswap: replaces any earlier cswap, including the original claude-swap.
uv tool install --force --python ">=3.12" "$(newest "$prefix"/cswap/claude_swap-*.whl)"
mkdir -p "$bin"
for link in cswap-on-switch:cswap-hooks/on-switch.sh cswap-satellite:cswap-satellite/cswap_satellite.py \
  cswap-remote:cswap-remote/cswap_remote.py; do
  chmod +x "$prefix/${link#*:}"
  ln -sfn "$prefix/${link#*:}" "$bin/${link%%:*}"
done
"$bin/cswap" config set autoswitch.onSwitchCommand "$bin/cswap-on-switch" >/dev/null
echo "命令已装到 $bin：cswap、cswap-on-switch（每次切换后运行）、cswap-satellite、cswap-remote"

if [ "$api" = 1 ]; then
  if ! "$prefix/api-worker/install.sh"; then
    echo "api-worker 没装好（上面是原因），其他部分照常装；之后可以再运行 $prefix/install.sh"
  fi
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
    # Install when missing, then always update: Claude Code keeps a copy per
    # version, and build.py puts a hash of the files in the version, so this
    # replaces the copy exactly when the files changed.
    claude plugin install api-worker@api-worker >/dev/null 2>&1 || true
    if claude plugin update api-worker@api-worker >/dev/null 2>&1; then
      echo "Claude Code 的 api-worker 插件已装（重开 Claude Code 生效）"
    else
      echo "api-worker 插件没装上，可以手动运行：claude plugin marketplace add $prefix/api-worker && claude plugin install api-worker@api-worker"
    fi
  else
    echo "还没装 Claude Code：装好后再运行一次 $prefix/install.sh，就会装上 api-worker 插件"
  fi
fi

if [ "$vscode" = 1 ]; then
  vsix="$(newest "$prefix"/cswap-status-*.vsix)"
  vver="$(basename "$vsix" .vsix)"
  vver="${vver#cswap-status-}"
  if [ -n "$vscode_cli" ] && have_newer chenyu.cswap-status "$vver"; then
    echo "VS Code 插件 $(ext_version chenyu.cswap-status) 已经装了（包里是 $vver）"
  elif [ -n "$vscode_cli" ] && install_vsix "$vsix" chenyu.cswap-status "$vver"; then
    echo "VS Code 插件 $vver 已装：重载窗口（Developer: Reload Window）后生效"
  else
    echo "VS Code 插件：在 VS Code 连到这台机器的终端里运行  code --install-extension $vsix"
  fi
  # Agent Monitor: the copy in this bundle unless a newer one is installed
  # (a later Marketplace release still updates it as usual); no copy in the
  # bundle -> from the Marketplace.
  monitor_id=cyuneo.cyuneo-agent-monitor
  if [ "$monitor" = 1 ] && [ -n "$vscode_cli" ]; then
    mvsix="$(newest "$prefix"/cyuneo-agent-monitor-*.vsix)"
    have="$(ext_version "$monitor_id")"
    if [ -n "$mvsix" ]; then
      want="$(basename "$mvsix" .vsix)"
      want="${want#cyuneo-agent-monitor-}"
      if have_newer "$monitor_id" "$want"; then
        echo "Agent Monitor $have 已经装了"
      elif install_vsix "$mvsix" "$monitor_id" "$want"; then
        echo "Agent Monitor $want 已装：重载窗口后生效"
      else
        echo "Agent Monitor 没装上：在 VS Code 连到这台机器的终端里运行  code --install-extension $mvsix"
      fi
    elif [ -n "$have" ]; then
      echo "Agent Monitor $have 已经装了"
    elif vscode_run --install-extension "$monitor_id" >/dev/null 2>&1; then
      echo "Agent Monitor 已装（从扩展市场）"
    else
      echo "Agent Monitor 没装上（要能连到 VS Code 扩展市场）：code --install-extension $monitor_id"
    fi
  elif [ "$monitor" = 1 ]; then
    mvsix="$(newest "$prefix"/cyuneo-agent-monitor-*.vsix)"
    echo "Agent Monitor：在 VS Code 连到这台机器的终端里运行  code --install-extension ${mvsix:-$monitor_id}"
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
# An old crontab line running `cswap auto --once` next to the service is a
# second engine: switches it makes come and go between the service's ticks.
cron_engines="$(crontab -l 2>/dev/null | grep -v '^[[:space:]]*#' | grep -c 'cswap auto' || true)"
if [ "${cron_engines:-0}" != 0 ]; then
  echo "注意：crontab 里还有一行在运行 cswap auto（第二个自动切换）。运行 crontab -e 删掉它，自动切换只留后台服务 cswap-auto。"
fi
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
