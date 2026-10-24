# cswap 全套工具（安装包）

新机器只要这一个压缩包。里面的 cswap 是我们自己的版本（`cswap/` 里的安装包），不用再下载原来的 claude-swap 项目。

## 安装

先装好 Claude Code 和 uv（`curl -LsSf https://astral.sh/uv/install.sh | sh`），然后：

```bash
tar xzf cswap-suite.tar.gz
./cswap-suite/install.sh          # 加 --auto：自动切换作为后台服务一直运行
```

- 装到 `~/.local/share/cswap-suite`，命令在 `~/.local/bin`。解压出来的文件夹装完可以删掉。
- 更新：拿新的压缩包再装一次，或者运行 `~/.local/share/cswap-suite/install.sh`。正在运行的后台服务会自动重启到新版本。
- `./install.sh --help` 看全部选项（`--remote`、`--no-api-worker`、`--no-vscode`）。
- 后台服务要在没登录时也运行，运行一次 `loginctl enable-linger`。

## 里面有什么

| 部分 | 作用 |
| --- | --- |
| `cswap/` | cswap（多账号切换），我们的版本：个人上限、自动切换状态、暂停、切换后钩子 |
| `cswap-hooks/` | 每次切换后运行：统一推理强度，记日志（只记账号号码） |
| `cswap-satellite/` | 这台机器不登录，用服务器上的账号（setup-token），并同步用量 |
| `cswap-remote/` | 手机网页：看会话、回答权限提示、切账号 |
| `api-worker/` | Claude Code 插件：把任务交给用 Console API key 计费的后台 worker |
| `cswap-status-*.vsix` | VS Code 插件：左侧 cswap 侧栏（账号用量圆环、主账号、自动切换阈值、API Worker） |
| `systemd/` | 后台服务的配置（`--auto`、`--remote` 时装） |

## 加账号

- 在这台机器登录：VS Code 左侧 cswap 侧栏点 ＋。也可以在终端登录 Claude Code 后运行 `cswap add`。
- 用服务器上已经登录的账号、这台不登录：`cswap-satellite setup --server 服务器`，再运行 `cswap-satellite install-timer`（详见 `cswap-satellite/README.md`）。

## 版本和来源

各部分的版本见 `VERSION`。cswap 源自 [realiti4/claude-swap](https://github.com/realiti4/claude-swap)（MIT 许可，原许可证在 `cswap/LICENSE`），之后由我们单独维护。
