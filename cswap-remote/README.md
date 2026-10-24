# cswap-remote

一个自己托管的 Claude Code「Remote Control」：在服务器上运行，手机用浏览器打开（可以添加到主屏幕当 App 用）。

- **会话列表**：列出服务器上所有正在运行的 Claude Code 会话。等你处理的排在最前面（「需要你处理」）。每行显示状态点、标题、目录、更新时间和运行中的智能体数（🤖 2）。tmux 里运行的可以操作，VS Code 里的只能查看。下面是「最近的对话」：14 天内已经关掉的对话，点「继续」就在 tmux 里用 `claude --resume` 接着同一个对话。
- **对话**：像官方 App 一样的聊天界面，实时更新（不用刷新）。Claude 的回复按 Markdown 显示（代码块带复制按钮）；工具调用显示成卡片：命令和输出、文件修改的红绿 diff、出错的卡片会自动展开，连续的普通工具调用折叠成「N 次工具调用」。往上滑或点顶部的「加载更早的消息」会加载更早的记录（页面短到滑不动时会自动加载）。重连、切回前台时已经展开的输出不会收起。claude 在 tmux 窗口里退出后，对话内容保留，末尾标一行「claude 已退出」。
- **发指令**：底部固定的输入栏，支持多行。Claude 正在运行时可以排队下一条消息（时间线里显示为灰色的「排队中」，送达后消失；后台任务通知之类系统自动排队的消息不显示），输入框为空时发送按钮变成「■ 停止」。⌨ 按钮里有 ⏎、Esc、1/2/3、↑↓、⇧Tab、Ctrl-C。左边的模式按钮可以切换「默认 / 自动接受编辑 / 计划 / 自动」（不会切到跳过权限）。模式只从输入框下面那一行读取，看不到输入框时不会按键。
- **等你处理**：Claude 要你批准命令、批准改文件或回答问题时，输入栏上方出现卡片：显示命令或 diff，每个选项一个大按钮；也可以写一句「换个做法」直接告诉 Claude。如果终端里的提示已经变了，按钮不会误按，会提示你重新看。有提示等你处理时，底部输入框和模式按钮暂时不能用（这时按回车或 ⇧Tab 会直接替你选「Yes」）。Claude 同时发起了好几个操作、或者是子智能体在请求权限时，卡片直接显示终端原文，不会把命令张冠李戴。一次问好几个问题时，卡片显示终端里当前那一题。按钮按下后会锁住一会儿，连点两下不会按两次。
- **进度**：输入栏上方显示当前状态（思考中 / 正在运行 Bash / 等待你批准）和用时，正在运行的智能体（可以点开看每个智能体的记录）和任务清单（3/7）。
- **终端**、**改动**：在右上角菜单里。终端实时显示 tmux 里的画面；改动显示改了哪些文件（+/−行数）和每个文件的 diff。
- **账号与额度**：显示 `cswap policy list` 的结果，可以手动切换账号。
- **推送（可选）**：Claude 等你处理或完成一轮时，通过 ntfy 推送到手机。推送只包含会话名和状态，不包含代码和对话内容。

会话运行的是**原版 `claude`**，不属于任何 claude.ai 账号。cswap 切换账号后，正在跑的会话从下一次请求开始自动改用新账号，不会中断。本程序**不读取、不保存任何 Claude 凭据或 token**。

## 需要

- Linux，Python 3.8 及以上（只用标准库）
- tmux（`sudo apt install tmux`；没有管理员权限可以用 conda：`conda create -n tmux -c conda-forge tmux`）
- 可选：cswap（账号页面需要），Tailscale（手机访问需要）

## 启动

```bash
cswap-remote serve --host tailscale
```

`cswap-remote` 是全套安装包装好的命令；直接用源码时换成 `python3 cswap_remote.py`。

- `--host tailscale` 表示只在本机的 Tailscale 地址上监听，只有你 Tailscale 网络里的设备能访问。
- 程序会拒绝监听公网地址。它能往你的 Claude 会话里打字，**不要暴露到公网**。
- 不加 `--host` 时默认只监听 `127.0.0.1`。这时可以用 `tailscale serve` 提供 HTTPS 访问，或者用 SSH 隧道。

第一次运行会生成访问密钥，保存在 `~/.config/cswap-remote/config.json`，权限 0600。

## 在手机上打开

```bash
cswap-remote url
```

1. 这条命令会输出一个带密钥的链接。把它发到手机上用浏览器打开，打开一次就会记住密钥。**这个链接等于密码，不要分享。**
2. iPhone 用 Safari 打开 → 分享 → 「添加到主屏幕」；安卓用 Chrome → 菜单 → 「添加到主屏幕」。
3. 手机要先装 Tailscale App，并登录同一个 Tailscale 账号。

要让所有设备的登录都失效，执行 `cswap_remote.py rotate-token`。

## 用法提示

- **在手机上开新会话**：点「＋ 新会话」，选目录，就会在 tmux 里启动 `claude`。
  - 第一次进入某个目录时，claude 会问是否信任这个目录。默认选项是 No，要先按 ↓ 再按 ⏎。
- **在电脑上接着用同一个会话**：执行 `tmux attach -t cr-<名字>`。按 `Ctrl+b` 再按 `d` 离开，会话会继续运行。
- **让手机能操作你在电脑上开的会话**：在 VS Code 里运行命令「cswap: 在 tmux 里启动 Claude（手机可操作）」（cswap-status 插件 0.3.0 起，也在状态栏的账号菜单里），或者自己在终端运行 `tmux new -s 名字` 再运行 `claude`。
- **VS Code 面板里的会话**：在手机上只能查看。要在手机上接着做，先在电脑上关掉那个面板，再在手机上点「在手机上继续这个对话」。面板还开着时会被拒绝，避免两个 claude 同时写同一个对话。
- **账号切换**：cswap 的自动切换照常在后台运行。手机上的「切换到这个」等同于执行 `cswap switch N`。已停用、到上限或不在使用时段的账号会标成黄色并显示原因；切到这样的账号前会先提醒：自动切换会在约 1 分钟内切回别的账号。

## 推送通知（可选）

```bash
python3 cswap_remote.py notify <一个很长的随机名字>
```

1. 执行上面的命令，设置一个 ntfy 主题名。
2. 手机上安装 ntfy App，订阅同一个主题名。

ntfy.sh 上的主题是公开的，**谁知道名字谁就能看到**，所以名字要足够长、足够随机。推送内容只有会话名和状态。也可以用 `--server` 指定你自己搭的 ntfy 服务。关闭推送：`notify off`。

## 开机自动运行（systemd 用户服务）

用全套安装包的话，运行 `install.sh --remote` 就会装好并启动这个服务。手动装：

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/cswap-remote.service <<'EOF'
[Unit]
Description=cswap-remote
After=network-online.target

[Service]
ExecStart=%h/.local/bin/cswap-remote serve --host tailscale
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now cswap-remote
sudo loginctl enable-linger "$USER"   # 不登录也保持运行（需要一次 sudo）
```

## 接口（v0.2）

所有 `/api/` 接口都要带 `Authorization: Bearer <密钥>`。v0.2 新增的接口：

| 接口 | 作用 |
| --- | --- |
| `GET /api/session/<k>/stream?c=<页面 id>` | 实时推送（Server-Sent Events，页面用 `fetch()` 读，所以能带密钥）。先发 `snapshot`（最近 300 条事件、智能体、任务清单、待处理提示），之后是 `event`（新事件）、`patch`（同一个 id 的事件有更新，比如工具结果到了）、`status`、`agents`、`todos`、`ping`（每 5 秒）、`gone`（会话结束）。同时最多 8 个连接，第 9 个返回 503（页面会过一会儿重试，不算失败）。同一个页面 id 再连一次时，旧的连接立即结束（手机换网络后留下的死连接不会占满名额）；写入卡住 20 秒的连接也会被关掉。claude 退出、窗口回到 shell 时只发 `status`，不会再发一个空的 `snapshot`。 |
| `GET /api/session/<k>/events?before=<id>&limit=100` | 往前翻更早的记录，返回 `{events, more, pending}`（不带 `before` 时返回最新的，另外带 `agents` 和 `todos`；实时连接失败时页面用它轮询，`pending` 和实时连接里的一样，所以轮询时也能点权限按钮，轮询期间每 30 秒试一次恢复实时连接）。找不到 `before` 这条记录时直接返回空列表。 |
| `GET /api/session/<k>/agent/<agentId>/events` | 某个子智能体的记录 |
| `GET /api/session/<k>/toolresult/<tool_use_id>` | 某次工具调用的完整输出（最多 5 MB，从记录末尾往前找） |
| `POST /api/session/<k>/answer` | 回答提示：`{choice: "1"…"9" 或 "esc", label?, text?, rawHash}`。终端画面跟 `rawHash` 对不上、或者这个数字键现在对应的选项不是 `label` 时返回 409「提示已经变化，请重新查看」，不会按错键；同一个提示 3 秒内只回答一次。`text` 只在提示关掉、输入框回来以后才发送（返回里的 `sentText`）。 |
| `POST /api/session/<k>/interrupt` | 停止（发送 Esc） |
| `POST /api/session/<k>/mode` | 切换权限模式：`{target: "default" / "acceptEdits" / "plan" / "auto"}`，最多按 6 次 ⇧Tab，切不到返回 409。有提示在等你处理、或者看不到输入框下面的模式行时，返回 409 并且一个键都不按（每按一次 ⇧Tab 前都会重新检查） |

| `GET /api/recent` | 14 天内已经关掉的对话（不含正在运行的）：`{conversations: [{sessionId, title, cwd, updatedAt, cwdExists}]}` |
| `POST /api/resume` | `{sessionId}`：在新的 `cr-` tmux 会话里运行 `claude --resume <id>`，返回 `{key, tmux}`。这个对话还有 claude 在运行时返回 409。 |

`/send`、`/key`、`/pane`、`/git`、`/start`、`/kill` 和账号接口跟 v0.1 一样（`/git` 多了 `files`：每个文件的增删行数）。`/send` 在有提示等你处理时返回 409，不会把文字和回车打进权限对话框；`/key` 照常发送你明确按下的键。会话列表的每一项多了 `title`、`agentsRunning`、`mode`。

## 测试

```bash
python3 -m unittest discover -s tests
```

测试用一个临时的假 Claude 目录（通过 `CLAUDE_CONFIG_DIR` 指定），只监听 `127.0.0.1`，不会碰你真实的会话。tmux 相关的测试会临时建 `cr-fixture-*` 会话，测完就关掉：里面运行 `tests/fake_tui.py`（一个模仿 Claude Code 画面的小脚本：权限对话框、输入框和模式行，把收到的每个回答和每条消息记到日志里，不运行任何别的东西），或者运行 `sleep` 来模拟 claude 退出。

## 安全

- 能打开这个页面的人，就能在你的服务器上通过 Claude 执行命令。所以要做到三点：只在 Tailscale 或本机上监听，访问密钥保密，不要用 `--allow-public`。
- 本程序只读取以下文件：
  - `~/.claude/sessions/*.json`：会话状态
  - `~/.claude/projects/*/*.jsonl`：会话记录，以及同一个项目目录下的 `subagents/`（子智能体记录）、`workflows/`（工作流状态）、`tool-results/`（过长的工具输出）
  - `~/.claude/tasks/<会话 id>/*.json`：任务清单
  - `~/.claude.json` 里的项目目录列表
- 本程序不读取 `.credentials.json`，也不读取会话目录里的 `*.key` 文件。读取工具输出文件时，路径必须在这个会话的项目目录里面。
- 没带正确密钥的请求不会读取请求体，直接返回 401 并断开；同时最多 64 个连接。
- 思考过程的内容不会发到手机上，只显示「思考…」。
- 页面只运行自己的脚本（CSP `script-src 'self'; style-src 'self'`，没有内联脚本和内联样式）。会话记录和终端里的文字一律当作不可信内容：先转义再显示，Markdown 里只保留 http(s) 链接。

## 开发与测试

- 前端是不需要编译的原生 JS：`static/index.html`（页面骨架）、`static/style.css`、`static/md.js`（Markdown 渲染，先转义）、`static/app.js`。
- 运行全部测试：`python3 -m unittest discover -s tests`。找得到 node 时（PATH 里，或 VS Code Server 自带的 `~/.vscode-server/cli/servers/*/server/node`）会一起运行 `tests/md_test.js` 和 `tests/app_test.js`，也可以单独运行：`node tests/md_test.js`。
