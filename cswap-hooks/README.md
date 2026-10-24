# cswap-hooks

cswap 每次切换账号之后运行的钩子。自动切换（`cswap auto`）和手动切换（`cswap switch`、TUI、VS Code 侧边栏的 ⇄、cswap-remote 手机页面）都会触发。

## 文件

| 文件 | 作用 |
| --- | --- |
| `on-switch.sh` | 钩子本身，cswap 的 `autoswitch.onSwitchCommand` 指向它。每次切换记一行日志，再运行 `pin-effort.py`。 |
| `pin-effort.py` | 让每个账号、每个模型用同一个推理强度（effort）。只改 `~/.claude/settings.json` 的 `modelSettings`，和已经设好的一样就不写文件，所以连续运行多少次都没关系，也很快。 |
| （已归档） | 旧的 `restart-remote-control.sh` 移到了 `dist/archive/`：现在用 tmux 加 cswap-remote，切换后不需要重启 Remote Control。 |

## 安装

1. 建一个指向 `on-switch.sh` 的链接（脚本会顺着链接找到同目录的 `pin-effort.py`，这个文件夹放在哪里都行）：
   ```bash
   ln -sf "$PWD/on-switch.sh" ~/.local/bin/cswap-on-switch    # 在本文件夹里运行
   ```
2. 告诉 cswap：
   ```bash
   cswap config set autoswitch.onSwitchCommand ~/.local/bin/cswap-on-switch
   ```
3. 选一个推理强度（不选的话钩子只记日志）：
   ```bash
   python3 pin-effort.py high     # low、medium、high、xhigh、max
   python3 pin-effort.py --show   # 看选了什么、settings.json 里现在是什么
   python3 pin-effort.py --off    # 不再统一
   ```
   注意：选了之后，下次切换会把你手动改过的 effort 改回来。

cswap 运行钩子时会设置 `CSWAP_FROM`、`CSWAP_TO`、`CSWAP_TO_EMAIL`、`CSWAP_TRIGGER`（自动切换是触发原因，例如 `policy-limit`；手动切换是 `manual`）。

## 日志

`${XDG_STATE_HOME:-~/.local/state}/cswap-hooks/hook.log`，每次切换一行，例如：

```
2026-10-22 17:18:18 switch 2 -> 3 (policy-limit)
```

只记账号编号和触发原因，不记邮箱。超过 1000 行时只保留最近 800 行。`pin-effort.py` 出错时，错误也写在这里。

## 测试

```bash
python3 -m unittest discover -s tests
```
