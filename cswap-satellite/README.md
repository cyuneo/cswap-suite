# cswap-satellite

在多台电脑上用同一批 Claude 账号，**每台电脑都不用打开浏览器登录**，cswap 的用量显示、限额策略和自动切换照常工作。

- **服务器**：一台一直开着的机器，账号在上面正常登录（`cswap add`），由它负责查用量。
- **其他电脑**：每个账号用一个长期有效的 setup-token（`claude setup-token`，大约一年有效，走订阅额度，不按量收费），每 5 分钟通过 SSH 从服务器拉一次用量。

## 一次性准备：给每个账号生成 setup-token

每个账号只需要做一次：

1. 在浏览器里登录这个账号的 claude.ai。
2. 在任意一台电脑上运行 `claude setup-token`，按提示授权。
3. 把得到的 `sk-ant-oat01-…` 存进密码管理器，条目里写上账号邮箱和生成日期。
4. 跑完运行一下 `cswap status`，确认这台电脑当前用的账号没被改掉。

然后在日历上设一个 11 个月后的提醒，到时候换新 token。

**不要**把 token 发到聊天里（包括发给 Claude），也不要放进网盘、邮件或 dotfiles 仓库。

## 每台新电脑

需要先满足三个条件：

- 装好 Claude Code 和本套工具：在 `cswap-suite.tar.gz` 解压出的文件夹里运行 `./install.sh`。它自带我们的 cswap，也会装好 `cswap-satellite` 命令。不要装原版 claude-swap，原版没有 `cswap policy` 这些命令；
- 这台电脑能用 `ssh 服务器` 直接连上服务器，免密码，比如用 SSH key 走 Tailscale；
- 有 Python 3.8 或更高版本。

```bash
# 1. 放好工具（用全套安装包装过就跳过这一步）
mkdir -p ~/.local/bin
cp cswap_satellite.py ~/.local/bin/cswap-satellite && chmod +x ~/.local/bin/cswap-satellite

# 2. 加账号：自动读取服务器上的账号列表，逐个提示粘贴 token（输入不显示）
cswap-satellite setup --server 服务器主机名

#    用 1Password 的话可以全自动：
#    cswap-satellite setup --server 服务器主机名 --op "op://Private/Claude {email}/credential"

# 3. 定时同步用量（macOS 用 launchd，Linux 用 systemd）
cswap-satellite install-timer

# 4. 开始用
cswap switch 1
```

装好之后，在 Claude Code 里运行 `/status`，确认用的是订阅登录、不是 API key。

- 默认会设置服务器上所有正常登录的账号。只想设置其中几个，用 `--accounts 1,2,3`。
- 槽位号和服务器保持一致。下面几种情况会跳过，不会覆盖或挪动本机已有的账号：
  - 本机某个槽位已经是别的账号；
  - 这个账号已经在本机另一个槽位上，这时会原地更新那个槽位；
  - 同一个邮箱在服务器上属于两个组织，这时只能选其中一个。
- `setup` 必须在终端里运行，因为要隐藏输入的 token。通过 ssh 远程运行请加 `-t`。没有终端时会拒绝读取 token，以免 token 显示在屏幕上。
- `install-timer` 装好后，会用定时任务的环境试连一次服务器。如果你的 SSH 私钥有密码、只能通过终端里的 ssh-agent 使用，这一步会提示你怎么改。
- Linux 没有 systemd 用户服务时（比如 WSL 没开 systemd），会给你一行 cron 配置。

## 每年换 token

生成新 token 后，在每台电脑上再运行一次 `cswap-satellite setup`。同一个账号会原地更新，不会多出一个账号，别名和「已停用」状态也会保留。

如果更新的正好是这台电脑正在用的账号，工具会自动运行 `cswap switch N --force`，让新 token 立刻生效。不这样做的话，下次切换账号时新 token 会被旧的覆盖。自动切换失败时会提示你退出 claude 后手动运行这条命令。

## 用量绑定（setup-token 槽位显示另一个账号的用量）

有的 setup-token 槽位在 cswap 里只有自动起的名字，比如 `setup-token-4@token.local`，cswap 不知道它属于哪个账号，所以查不到用量。如果你知道它是用哪个账号生成的，就绑定过去：

```bash
cswap-satellite bind 4=1            # 槽位 4 显示服务器账号 1 的用量
cswap-satellite install-timer --server local   # 在服务器本机上：直接读本机 cswap，不走 SSH
cswap-satellite unbind 4            # 取消绑定
```

只有 setup-token 槽位会接收同步过来的用量。正常登录的账号一直自己查，不会被同步覆盖，也不会被暂停查询。

## 其他命令

| 命令 | 作用 |
| --- | --- |
| `cswap-satellite status` | 本机有哪些账号、哪个正在用、定时同步是否启用、上次同步成功没有 |
| `cswap-satellite sync-usage` | 手动同步一次用量（`--dry-run` 只看会同步哪些账号） |
| `cswap-satellite uninstall-timer` | 移除定时同步 |
| `cswap-satellite bind 4=1` / `unbind 4` | 用量绑定 |

## 安全

- 工具本身不生成、不显示、不保存 token。token 只从隐藏输入或 `op read` 读进内存，经由标准输入交给 `cswap add-token -`，最后存在 cswap 自己的凭据库里：macOS 是钥匙串，Linux 是 0600 权限的文件。
- 通过网络传的只有服务器上 `cswap list --json` 的结果（槽位号、邮箱、用量百分比），**不含任何 token**。
- 不直接改 Claude Code 的登录，也不改 cswap 的认证代码。所有登录相关的改动都通过 cswap 自己的命令完成：`add-token`，以及换 token 时的 `switch --force`。
- 电脑丢了或者不再用了：在那台电脑上运行 `cswap remove <编号>`。电脑不在手边的话，就把所有账号的 token 都重新生成，再在其他电脑上 `setup` 一遍。
- setup-token 只能调用模型，查不到用量，所以用量要靠服务器同步过来。服务器关机时，各台电脑的用量显示会停在最后一次同步的数字，最多保持 15 分钟，之后显示「不可用」。这时还能手动切换账号，但不能按用量自动切换。

## 测试

```bash
python3 -m unittest discover -s tests
```

端到端测试会在临时目录里运行真正的 cswap，用假的 `ssh` 和 `op`，token 也是测试时临时生成的假值，不会碰你的真实账号。
