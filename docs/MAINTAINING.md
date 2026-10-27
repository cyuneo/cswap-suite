# 维护说明

这里是源码。装好的副本在 `~/.local/share/cswap-suite`，改这里的文件不会直接影响正在运行的服务。

## 改代码之后

```bash
suite/test.sh                          # 全部测试（约 1 分钟）
git add -A && git commit               # claude-swap/、agent-monitor/ 是各自的仓库（克隆到这两个文件夹），另外提交
python3 suite/build.py --install       # 先跑测试，再打包，再装到这台机器（重启正在运行的服务）
```

VS Code 插件装好后要重载窗口；Claude Code 的 api-worker 插件要重开 Claude Code。推送到 GitHub、发安装包见 [PUBLISHING.md](PUBLISHING.md)（推送前先跑 `suite/check-public.sh`）。

## 各部分

| 文件夹 | 是什么 | 怎么运行 |
| --- | --- | --- |
| [claude-swap/](../claude-swap/) | 我们自己维护的 cswap，源自 realiti4/claude-swap（MIT 许可）。仓库 cyuneo/claude-swap，分支 `chenyu-account-policy`，合并过原项目到 c896cdd。比原项目多了：个人上限 `cswap policy`、切换后钩子、自动切换状态文件、暂停 `cswap auto --pause/--resume` | 安装包里是它打成的 wheel，`install.sh` 用 `uv tool install` 安装。systemd 服务 `cswap-auto` |
| [cswap-status-vscode/](../cswap-status-vscode/) | VS Code 插件：左侧活动栏的 cswap 侧边栏（账号用量圆环 / 自动切换 / API Worker 三个面板）和状态栏。加账号、切换、设主账号、改上限和阈值、加 API key 都在侧边栏里做 | 安装包里带 vsix，`install.sh` 用 `code --install-extension` 安装 |
| [agent-monitor/](../agent-monitor/) | VS Code 插件 Agent Monitor（自己的 git 仓库，远程 github.com/cyuneo/cyuneo-agent-monitor，也在 VS Code 扩展市场上）：看所有 Claude Code / Codex 会话；额度用完时，cswap 一换号就自动续跑 | 安装包里带它打成的 vsix（`agent-monitor/scripts/build-vsix.py`），`install.sh` 装上；已装的版本更新就不动 |
| [api-worker/](../api-worker/) | Claude Code 插件：把任务交给用 Console API key 计费的后台 Agent SDK worker，或开云端会话 | `install.sh` 装 Python 环境和 `api-worker` 命令，并把插件注册到安装好的副本 |
| [cswap-hooks/](../cswap-hooks/) | 每次切换后运行的钩子：统一推理强度（pin-effort）并记日志 | `~/.local/bin/cswap-on-switch` 链接到 `on-switch.sh`，cswap 设置 `autoswitch.onSwitchCommand` 指向它 |
| [cswap-remote/](../cswap-remote/) | 手机网页：看会话、回答权限提示、切账号 | systemd 服务 `cswap-remote`（`~/.local/bin/cswap-remote serve --host tailscale`） |
| [cswap-satellite/](../cswap-satellite/) | 在别的电脑上用 setup-token 使用这些账号，并同步用量 | `~/.local/bin/cswap-satellite`；只在那些电脑上开定时器 `cswap-satellite-usage.timer`（保存真实登录的那台不用开） |
| [suite/](../suite/) | 全套安装包：`install.sh`（一键安装）、`test.sh`（全部测试）、`build.py`（打包）、说明、后台服务配置 | `python3 suite/build.py [--install]` 生成 `dist/cswap-suite.tar.gz` |
| [dist/](../dist/) | `cswap-suite.tar.gz`（全套安装包，VS Code 插件的 vsix 在里面）；`archive/` 放旧版本。不进 git | 见上一行 |

## 新机器

只要 `dist/cswap-suite.tar.gz` 一个文件，不用下载原来的 claude-swap 项目：

```bash
scp dist/cswap-suite.tar.gz 新机器:
ssh 新机器 'tar xzf cswap-suite.tar.gz && ./cswap-suite/install.sh'   # 加 --auto：自动切换常驻后台
```

新机器上要先有 Claude Code 和 uv。装到 `~/.local/share/cswap-suite`，命令在 `~/.local/bin`。安装包里不含账号、令牌、key，打包时会检查；`VERSION` 里记着打包时的 git 提交。

## 运行中的服务（systemd 用户服务）

```bash
systemctl --user status cswap-auto cswap-remote
journalctl --user -u cswap-auto -n 50        # 自动切换的日志（只写账号号码，不写邮箱）
```

暂停自动切换用 `cswap auto --pause 2h`（或 `forever`），恢复用 `cswap auto --resume`。VS Code 的「自动切换」面板里也有按钮。不要用 `systemctl stop` 来暂停：重新登录后服务会自己起来。

## 数据和日志在哪

| 路径 | 内容 |
| --- | --- |
| `~/.local/share/claude-swap/` | cswap 的数据：`settings.json`、`account_limits.json`（个人上限）、`autoswitch_status.json`（服务每次检查后写的状态）、`autoswitch_history.jsonl`（切换历史，只有号码）。`credentials/` 是登录凭据，不要拷贝或分享 |
| `~/.local/state/cswap-hooks/hook.log` | 钩子日志 |
| `~/.config/cswap-hooks/effort.json` | pin-effort 选的推理强度 |
| `~/.config/api-worker/` | api-worker 的 key（权限 0600）和配置 |
| `~/.local/share/api-worker/` | api-worker 的 Python 环境、任务记录、cloud desk |

## 各部分之间怎么连

- VS Code 插件、cswap-remote、api-worker 都只通过 `cswap policy list --json` 读账号和自动切换状态（只读本地数据，不联网），切换时调用 `cswap switch N`。字段说明见 claude-swap 的 README。
- 任何一次切换（自动或手动）都会运行钩子，并记进 `autoswitch_history.jsonl`。
- 服务和设置里引用的程序都走 `~/.local/bin/` 下的链接，指向 `~/.local/share/cswap-suite`；`install.sh` 每次都会重写。

## 测试

```bash
suite/test.sh                  # 全部
suite/test.sh vscode remote    # 只跑某几部分：cswap api-worker hooks satellite remote vscode
```

没有装 node 时，JavaScript 测试用 VS Code Server 自带的 node。
