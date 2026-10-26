# api-worker

让 Claude Code 把整块活儿派出去做，不占本地 Claude Code 订阅的用量。两种去处：

| 去处 | 花的钱 | 能看到的代码 | 怎么说 |
| --- | --- | --- | --- |
| **API worker**（本机后台） | Console 的 API 额度（套餐每月送的 API credits） | 本机文件，包括没提交的 | 「派给 API worker」 |
| **云端会话**（claude.ai/code） | 云端会话额度（用完或过期后改用套餐的普通用量） | 只有推送到远程仓库的代码 | 「开个云端会话做…」 |

为什么要这样绕一下：套餐送的 API credits 能用在 API、Agent SDK 和 Managed Agents 上，**不能用在 Claude Code 上**。所以 Claude Code 本身还是走订阅，它通过这个插件把任务交给 Agent SDK，后者用 API key 计费。

## 安装

1. **领取额度，创建 key**
   - 在 claude.ai 的 Settings > Billing 里领取 API credits，关联到一个 Console 组织。额度每个账单周期过期。
   - 到 platform.claude.com 的 API Keys 页面创建一个 key（`sk-ant-api…`）。
2. **装工作进程**（Python 3.10+，会自动用 uv 建一个独立的 venv）：
   ```bash
   ./install.sh
   api-worker set-key        # 粘贴 key，输入不显示，保存为 0600 权限的文件
   ```
   用 1Password 的话，可以不存文件：
   ```bash
   api-worker config --set 'keyCommand=op read op://Private/Claude API/credential'
   ```
3. **装 Claude Code 插件**（在这个文件夹里运行）：
   ```bash
   claude plugin marketplace add "$PWD"
   claude plugin install api-worker@api-worker
   ```

用全套安装包（`cswap-suite.tar.gz` 的 `install.sh`）装时，第 2 步的 `./install.sh` 和第 3 步都会自动做，只剩添加 key：`api-worker set-key`，或者在 VS Code 的 cswap 侧栏里添加。

**不要**把 API key 放进 `ANTHROPIC_API_KEY` 环境变量或 Claude Code 的设置里。那样 Claude Code 自己也会用这个 key，结果是不能用、报额度不足。

## 怎么用

直接跟 Claude Code 说，例如：

- 「把给 utils 写单元测试的活儿派给 API worker，可以跑测试，预算 3 美元」
- 「API worker 的任务怎么样了」
- 「把 t1a2b3c4 的结果拿来，看看它改了什么」

Claude 会调用下面这些工具：

| 工具 | 作用 |
| --- | --- |
| `dispatch` | 派任务，立刻返回任务编号 |
| `status` | 查看状态、花费和最近几步 |
| `result` | 取最终报告 |
| `list` | 列出任务和本月花费 |
| `cancel` | 停止任务（云端会话要在它的页面上停） |
| `cloud` | 开一个云端会话，返回会话链接；可以带上要上传的文件 |
| `collect` | 把云端桌面任务的输出复制到本地 |

任务结束时，派发它的会话会弹出一条提示。

也可以在终端直接用：

```bash
api-worker start -p "任务内容" --budget 3 --allow-bash
api-worker list                 # JSON：任务、每个 key 的花费、cswap 账号
api-worker list --no-accounts   # 不运行 cswap，只要任务和花费（适合定时刷新）
api-worker spend
api-worker close <id>          # 把做完的云端任务标记为已结束
```

`list` 里每个任务都有 `state` 和 `final`。`final` 为 true 表示任务已经结束（done、failed、cancelled、lost、closed），之后不会再有变化，界面和提示直接看它，不用自己维护状态列表。`list` 每次最多花 15 秒去查云端任务的结果分支，没查到的下次接着查。

不在带远程的 git 仓库里开的云端任务没有结果分支，这里无法知道它什么时候做完，状态一直是 `cloud`（“已交给云端”）。到会话页面看完后，用 `api-worker close <id>`（或 cswap 侧栏里任务的右键“标记为已结束”）把它标记为 `closed`。

## 多个 API key

每个账号的 Console 组织各有一份 API 额度，可以都存进来，每个 key 有自己的每月上限：

```bash
api-worker set-key --name jj --budget 200       # 粘贴 key，输入不显示
api-worker set-key --name gmail --budget 100
api-worker set-key --name work --command 'op read op://Private/Claude API/credential'
api-worker spend                                # 每个 key 本月花了多少、还剩多少
api-worker remove-key gmail
```

派任务时默认用本月剩得最多的 key，也可以指定：「用 jj 那个 key 派给 API worker…」。以前用 `set-key` 存的那一个叫 `default`。

## 云端会话

插件在后台的 tmux 里运行官方的 `claude --cloud`（它只能在终端里用），拿到链接后就关掉 tmux，云端会话自己继续跑。需要 `tmux`。

- 先在项目目录里运行一次 `claude` 并选择信任这个目录，否则会提示你先去做。
- 云端从远程仓库拉代码。有没提交、没推送的改动时，会提醒你云端看不到。
- 花费不计入这里的每月统计，去 claude.ai 的设置页面看云端额度还剩多少。
- 开会话之前就会在任务列表里记一条，状态是 `launching`。开会话可能要几分钟（上传文件、切账号、等链接），插件给它的上限是 400 秒。万一中途被打断，这条任务会变成 `lost`，提示你去 claude.ai/code 看看会话是不是已经开了；之后结果分支出现了，它照样会变成 `done`。启动失败的会记成 `failed`，带上原因。

**结果自动拉回**：在有远程地址的 git 仓库里，插件会让云端会话做完后把改动和报告 `CLOUD_RESULT.md` 推到分支 `claude/aw-<编号>`。

- 插件每两分钟用 `git ls-remote` 查一次这个分支。分支出现了，任务就算完成，派任务的会话会弹出提示。
- 「看结果」时，插件把分支拉到 `origin/claude/aw-<编号>`，不切换分支，也不动工作区，然后返回报告、提交记录和改动统计。
- 合并：`git merge origin/claude/aw-<编号>`。
- 不是 git 仓库的目录拉不回结果，只能打开链接看。

**云端桌面仓库（目录不在 GitHub 上时）**：用一个私有仓库当中转站，云端会话在里面干活，你本地零散的文件夹也能用云端额度。

一次性设置：

1. **建仓库**：在 GitHub 上建一个**私有**仓库，例如 `cloud-desk`，什么都不用勾。
2. **加 deploy key**：仓库 Settings → Deploy keys → Add deploy key。粘贴 `~/.ssh/cloud_desk.pub` 的内容，**勾选 Allow write access**。这个 key 只能访问这一个仓库。
3. **授权 Claude**：在 claude.ai/code 连接 GitHub，并授权 Claude 访问这个仓库。
4. **连上仓库**：在这台电脑上运行
   ```bash
   api-worker desk-setup --repo git@github-cloud-desk:<你的用户名>/cloud-desk.git
   cd ~/.local/share/api-worker/desk && claude    # 选择信任这个目录，然后退出
   ```

之后在不是 git 仓库的目录里说「开云端会话分析 data/ 里的 csv」，Claude 会自动：

1. 把它指定的文件上传到 `tasks/<编号>/input/`。不会上传整个目录。
   - 像 `.env`、`*.pem`、`*.key`、`credentials` 这样的文件，以及内容里有 key 或私钥的文件，会被拒绝并提示你。
   - `~/.ssh`、`~/.claude`、cswap 和 api-worker 的配置目录一律不能上传。
   - 一次最多 20 MB，可以用 `config --set deskMaxMb=…` 调整。
2. 云端把结果写到 `tasks/<编号>/output/`。
3. 做完后弹出提示。「看结果」会显示报告和输出文件列表，「取回文件」会把输出复制到原目录的 `cloud-results/<编号>/`，不会覆盖已有文件。

本来就在 GitHub 上的项目还是用它自己的仓库，不经过云端桌面仓库。旧任务的文件会一直留在仓库里，隔一段时间去 GitHub 上清理一下。

**用别的账号的云端额度**：「用 3 号账号开云端会话…」。

- 插件会用 cswap 切到那个账号，开会话，再切回来，前后几秒钟。
- 这几秒里，所有正在运行的 Claude Code 会话都在用那个账号。
- 多个切换会排队，不会同时进行。
- 读不到 cswap 当前用的是哪个账号时，插件不会切换，直接报错，免得切过去回不来。
- `cswap auto` 可能在这几秒里自己切了账号。切回之前插件会先看一眼：当前账号已经不是它切过去的那个，就不再切回，在返回结果和任务记录的 `warnings` 里说明，免得把自动切换的结果撤掉。
- 切换命令报错但其实已经切过去了、开会话出错，或者进程收到 SIGTERM/SIGHUP，都会先切回原来的账号再退出（`kill -9` 除外）。
- setup-token 账号（比如 `@token.local`）多半开不了云端会话，因为需要完整登录。

终端里：`echo "任务" | api-worker cloud --cwd 项目目录 [--account 3]`

## 安全和花费限制

| 项目 | 默认 | 修改方法 |
| --- | --- | --- |
| 单个任务预算 | $2 | 派任务时指定；最高 $20：`config --set maxBudgetUsd=…` |
| 每月上限 | $200 | 已花的加上进行中任务的预算，超过就拒绝新任务：`config --set monthlyBudgetUsd=…` |
| 同时运行的任务 | 3 个 | `config --set maxParallel=…` |
| 默认模型 | claude-sonnet-5-5 | `config --set model=…` |

- **不碰订阅登录**：工作进程有自己的 `CLAUDE_CONFIG_DIR`，启动前清掉所有 Claude 登录相关的环境变量，只给它 API key。
- **key 不外露**：key 不会写进任务文件或日志，出错信息里也会替换成 `[key]`。
- **权限默认收紧**：
  - 可以读任何文件，但只能改任务目录里的文件。
  - 跑命令要指定 `allow_bash`，上网要指定 `allow_web`。
  - 其他工具（包括 MCP）一律不给用。
- **不动你正在编辑的文件**：在 git 仓库里，每个任务都有自己的 worktree 和分支（`api-worker/<编号>`），做完你再审阅、合并。合并后可以清理：
  ```bash
  git worktree remove ~/.local/share/api-worker/worktrees/<编号>
  git branch -d api-worker/<编号>
  ```
  不想用 worktree 可以加 `--no-worktree`。
- **本月花费以官方为准**：这里的统计只算通过本工具派发的任务。实际额度以 Console 的 Usage 页面为准。

任务文件放在 `~/.local/share/api-worker/tasks/<编号>/`：`task.json`、`status.json`、`events.jsonl`（每一步）、`result.md`。

## 测试

```bash
~/.local/share/api-worker/venv/bin/python -m unittest discover -s tests   # 工作进程，用假的 SDK，不花钱
claude plugin test .                                                      # Claude Code 插件
claude plugin validate .
```
