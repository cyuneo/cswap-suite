# cswap suite

[English](README.md) | [한국어](README.ko.md) | **简体中文**

一套本地工具，给使用自己多个 Claude Code 账号的人用。它以 [claude-swap](https://github.com/cyuneo/claude-swap)（`cswap`）为核心。cswap 负责切换 Claude Code 当前登录的账号，在此基础上本套件还提供：

- **自动切换。** 当前账号快到用量上限时，cswap 会切到剩余额度最多的账号。每个账号可以单独设置上限和使用时段，还可以留一个账号作备用，其他账号都用完了才用它。
- **VS Code 侧栏。** 显示每个账号的用量圆环、自动切换状态（可暂停和恢复），以及 API worker 的任务和花费。
- **手机网页**（cswap-remote）。通过你自己的 Tailscale 网络，在手机上查看会话、回应权限请求、切换账号。
- **API worker**（一个 Claude Code 插件）。把后台任务交给一个用你的 Console API key 计费的 worker，或者启动一个云端会话。它有总开关、每个任务的预算和每月上限。
- **Agent Monitor**（一个 VS Code 扩展，在[单独的仓库](https://github.com/cyuneo/cyuneo-agent-monitor)里）。查看你所有的会话；某个对话因用量上限停下时，等上限重置或 cswap 切换账号后，它会自动继续。

所有东西都留在你自己的机器上：设置、登录信息和日志从不上传。没有远程账号池，账号也不会和任何人共享。

## 环境要求

- Linux。在 Ubuntu 上测试过；后台服务使用 systemd 用户服务。
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 和 [uv](https://docs.astral.sh/uv/)（`curl -LsSf https://astral.sh/uv/install.sh | sh`）。
- VS Code，用于侧栏和 Agent Monitor（可选）。

## 安装

从 [Releases](https://github.com/cyuneo/cswap-suite/releases) 下载 `cswap-suite.tar.gz`，然后：

```bash
tar xzf cswap-suite.tar.gz
./cswap-suite/install.sh          # 加上 --auto，让自动切换作为后台服务一直运行
```

- 安装位置是 `~/.local/share/cswap-suite`，命令放在 `~/.local/bin`。解压出来的文件夹装完后可以删掉。
- 更新时，用同样的方法安装新版本的安装包，或者运行 `~/.local/share/cswap-suite/install.sh`。正在运行的服务会重启，换成新版本。
- 选项：`--auto`（自动切换服务）、`--remote`（手机网页服务）、`--no-api-worker`、`--no-vscode`、`--no-monitor`、`--force-vscode`（即使已装的扩展版本更新，也装包里自带的版本）。`./install.sh --help` 会列出这些选项。
- 如果希望退出登录后服务仍在运行，运行一次 `loginctl enable-linger`。

## 添加账号

- **在本机上：** 在 VS Code 的 cswap 侧栏里点 ＋，或者在终端里登录 Claude Code 后运行 `cswap add`。
- **在另一台不该自己登录的电脑上：** 通过长期有效的 setup token 使用一台已登录机器上的账号，用量通过 SSH 同步：先运行 `cswap-satellite setup --server <host>`，再运行 `cswap-satellite install-timer`。详见 [cswap-satellite/README.md](cswap-satellite/README.md)（中文）。

## 仓库内容

| 文件夹 | 说明 |
| --- | --- |
| [cswap-status-vscode/](cswap-status-vscode/) | VS Code 扩展：cswap 侧栏和状态栏 |
| [api-worker/](api-worker/) | Claude Code 插件，用你的 API key 或在云端运行后台任务 |
| [cswap-remote/](cswap-remote/) | 手机网页 |
| [cswap-satellite/](cswap-satellite/) | 通过 setup token 在其他电脑上使用这些账号 |
| [cswap-hooks/](cswap-hooks/) | 每次切换后运行的部分：保持推理强度不变，并写一行日志 |
| [suite/](suite/) | 安装程序、安装包构建脚本和测试 |

相关仓库：

| 仓库 | 说明 |
| --- | --- |
| [cyuneo/claude-swap](https://github.com/cyuneo/claude-swap) | 我们的 cswap：fork 自 [realiti4/claude-swap](https://github.com/realiti4/claude-swap)，加入了按账号设置的上限、自动切换状态和暂停，以及切换钩子 |
| [cyuneo/cyuneo-agent-monitor](https://github.com/cyuneo/cyuneo-agent-monitor) | Agent Monitor |

各部分的详细文档是中文的，放在各自的文件夹里。给维护者的说明见 [docs/MAINTAINING.md](docs/MAINTAINING.md) 和 [docs/PUBLISHING.md](docs/PUBLISHING.md)。

## 许可证

[PolyForm Noncommercial 1.0.0](LICENSE)：你可以使用、修改和分享，但不能用于商业目的。第三方项目及其许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 致谢

- Onur Cetinkol 的 [claude-swap](https://github.com/realiti4/claude-swap)（MIT）：我们的 cswap 是它的修改版，沿用其原始许可证。
- Claude Code 参与了代码的编写和审查。

Claude 和 Claude Code 是 Anthropic 的产品。这是一个与它们配合使用的独立个人项目，与 Anthropic 没有关联。
