# 发布到 GitHub

这份说明写给维护这套工具的人：怎么把代码推到 GitHub、怎么发安装包，以及推送前必须检查什么。命令都在本仓库的根目录运行，除非另有说明。

公开之后很难真正撤回：别人可能已经克隆，GitHub 和搜索引擎也会缓存。所以每次推送前都先跑检查（见“每次推送前”）。

## 三个仓库

| 本地文件夹 | GitHub | 许可证 | 推送的分支 |
| --- | --- | --- | --- |
| 本仓库（cswap-status、api-worker、cswap-remote、cswap-satellite、cswap-hooks、suite，以后还有 claude-review） | `cyuneo/cswap-suite` | PolyForm Noncommercial 1.0.0 | `main` |
| `claude-swap/`（自己的 git 仓库，本仓库不跟踪它） | `cyuneo/claude-swap`，fork 自 `realiti4/claude-swap` | MIT（原项目的许可证，原作者 Onur Cetinkol 的版权声明要保留） | `chenyu-account-policy` |
| `agent-monitor/`（自己的 git 仓库） | `cyuneo/cyuneo-agent-monitor`（已经公开） | PolyForm Noncommercial 1.0.0 | `main` |

`dist/`（打好的安装包）和 `xray/`（和这套工具无关）在 `.gitignore` 里，不会被推送。

## 一次性准备

### gh（GitHub 命令行）

装在 `~/.local/bin/gh`，用的是 GitHub 官方发布的 Linux 包，没有用 sudo。更新时同样做：

```bash
tag=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])")
v=${tag#v}
curl -fsSLO "https://github.com/cli/cli/releases/download/$tag/gh_${v}_linux_amd64.tar.gz"
curl -fsSLO "https://github.com/cli/cli/releases/download/$tag/gh_${v}_checksums.txt"
grep " gh_${v}_linux_amd64.tar.gz\$" "gh_${v}_checksums.txt" | sha256sum -c -   # 必须显示 OK
tar xzf "gh_${v}_linux_amd64.tar.gz"
install -m 755 "gh_${v}_linux_amd64/bin/gh" ~/.local/bin/gh
```

### 登录

```bash
gh auth login -h github.com -p https -w   # 显示验证码，在浏览器 github.com/login/device 输入并授权
gh auth setup-git                         # git push 也用 gh 的登录
gh auth status                            # 确认登录的账号是 cyuneo 组织的管理员（现在用的是 cyuneoai-art）
```

令牌由 gh 保存，不要复制到聊天、文件或提交里。

## 每次推送前

1. **测试全部通过：** `suite/test.sh`
2. **检查不能公开的内容：** `suite/check-public.sh`。claude-swap 用 `suite/check-public.sh claude-swap chenyu-account-policy`。
   - **找到密钥、令牌，或文件里有本机路径、主机名：** 脚本失败，先处理再推送。
   - **历史里的本机路径和邮箱地址：** 只列出来，看一遍有没有不该公开的。主仓库历史里有一处旧 README 写过本机路径，这是已知的，决定保留。
3. **只推已经提交的内容：** 推送只会带上提交，工作区里没提交的改动（包括别的会话正在改的文件）不会被推上去。先 `git status`、`git log origin/main..main` 看清楚这次会推哪些提交。
4. **只写分支名推送：** `git push origin main`。不要用 `git push --all`、`--mirror`、`--tags`。
   - 本机有 `refs/claude-review/*`（Claude Review 的快照，里面有提示词和本机路径），只有这样推才不会带上它们。

## 第一次发布

### 主仓库

```bash
gh repo create cyuneo/cswap-suite --public \
  --description "Local multi-account toolkit for Claude Code: account switching, VS Code side bar, API worker" \
  --source . --remote origin
git push -u origin main
```

不要给 `gh repo create` 加 `--push`，推送自己做，才能确认推的是什么。

### claude-swap（fork）

```bash
gh repo fork realiti4/claude-swap --clone=false --default-branch-only
cd claude-swap
git remote add origin https://github.com/cyuneo/claude-swap.git
git remote add upstream https://github.com/realiti4/claude-swap.git   # 以后想看原项目的更新时用
git push -u origin chenyu-account-policy
gh repo edit cyuneo/claude-swap --default-branch chenyu-account-policy
```

把默认分支设成 `chenyu-account-policy`，打开仓库首页看到的就是我们的版本。原项目的 `main` 留在 fork 里，不用管。

### Agent Monitor

仓库已经公开。本地 `cswap-switch-resume` 分支上还有 0.8.0 到 0.9.2 的改动没有发布：配合 cswap 的自动续跑、在 cswap 侧栏里的概览。发布它就是把这个分支合进 `main` 再推送，以及更新 VS Code 扩展市场上的版本。这是 Agent Monitor 自己的发布，要另外决定，不是 cswap 套装发布的一部分。

## 发布安装包（GitHub Releases）

用户从 Releases 下载 `cswap-suite.tar.gz`，解压后运行 `install.sh`（见 `suite/README.md`）。

```bash
suite/test.sh && suite/check-public.sh
python3 suite/build.py                    # 生成 dist/cswap-suite.tar.gz，打包时会检查有没有凭据文件
tag=v$(date +%Y.%m.%d)                    # 同一天发第二次就在后面加 .2
git tag -a "$tag" -m "cswap suite $tag"
git push origin "$tag"                    # 只推这一个标签
gh release create "$tag" dist/cswap-suite.tar.gz --verify-tag \
  --title "cswap suite $tag" --notes "这次改了什么（每条一行）"
```

`build.py` 要用到旁边的 `claude-swap/` 和 `agent-monitor/`。打包前确认这两个仓库停在要发布的提交上，并且已经推送。这样发布出去的安装包，别人能从公开的源码重新打出来。

### 从干净的克隆打包

新机器、或者想确认公开的源码能打出同样的包时：

```bash
git clone https://github.com/cyuneo/cswap-suite.git && cd cswap-suite
git clone -b chenyu-account-policy https://github.com/cyuneo/claude-swap.git claude-swap
git clone https://github.com/cyuneo/cyuneo-agent-monitor.git agent-monitor
suite/test.sh && python3 suite/build.py
```

## 以后的更新

```bash
suite/test.sh && suite/check-public.sh
git push origin main
( cd claude-swap && ../suite/check-public.sh . chenyu-account-policy && git push origin chenyu-account-policy )
```

改动值得给用户新安装包时，再按上面“发布安装包”发一个 Release。

## 推错了怎么办

- **推上去了密钥或令牌：** 先作废它，再做别的。
  - Console API key：在 Console 里删除。
  - GitHub 令牌：`gh auth refresh` 或在 GitHub 设置里撤销。
  - Claude 登录：在 Claude 里退出那个会话。
  - 改历史、强制推送都挡不住已经被克隆或缓存的副本，所以作废才是关键。
- **推上去了不该公开的文件（不是密钥）：** 在新提交里删掉。要从历史里彻底去掉，需要改写历史并强制推送（`git filter-repo`）。这会影响所有已经克隆的人，先停下来想清楚。
- **Release 发错了：** `gh release delete <tag> --cleanup-tag`，然后重新发。
