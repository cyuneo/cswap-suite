#!/usr/bin/env python3
"""cswap-satellite: use your Claude accounts on more computers without
logging in on each one.

One machine (the "server") holds normal logins of the accounts and is the
only one that reads their usage. Every other computer (a "satellite") gets
each account as a long-lived setup-token (`claude setup-token`, billed to the
subscription like a normal login) and pulls usage readings from the server
over SSH, so cswap's usage display, limits and auto-switch keep working.

What this tool never does:
- It never generates, prints, logs or writes a token. You paste a token
  (hidden input, refused when echo can't be turned off) or it is read from
  1Password (`op read`); it goes straight to `cswap add-token -` on stdin.
- Nothing about tokens crosses the network. The SSH call only runs
  `cswap list --json` on the server (emails, slot numbers, usage figures).
- It changes logins only through cswap's own commands (`add-token`, and
  `switch --force` to activate a renewed token of the active account).

Commands: setup, sync-usage, install-timer, uninstall-timer, status.
Python 3.8+ standard library only.
"""

import argparse
import getpass
import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

VERSION = "0.4.0"
TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]{20,}")
DEFAULT_REMOTE_CSWAP = "~/.local/bin/cswap"
TIMER_NAME = "cswap-satellite-usage"
LAUNCHD_LABEL = "local.cswap-satellite.usage"
SCHEMA_VERSION = 1  # of `cswap list --json`
LOCAL = "local"  # --server local: read this machine's own cswap, no SSH
PASS_ENV = ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR")


class Fail(Exception):
    pass


def run(cmd, **kw):
    """subprocess.run that reports a missing program as Fail, not a traceback."""
    try:
        return subprocess.run(cmd, **kw)
    except subprocess.TimeoutExpired:
        raise Fail(f"{os.path.basename(cmd[0])} 运行超时。")
    except OSError as e:
        raise Fail(f"无法运行 {cmd[0]}：{e.strerror or e}")


# ------------------------------------------------------------ locations

def is_mac():
    return platform.system() == "Darwin"


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "cswap-satellite"


def config_path():
    return config_dir() / "config.json"


def state_path():
    return config_dir() / "last-sync.json"


def read_json(p):
    try:
        d = json.loads(Path(p).read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(p, d):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", "utf-8")
    os.replace(str(tmp), str(p))


def load_config():
    return read_json(config_path())


def cswap_backup_root():
    """Same rule as claude-swap's paths.get_backup_root()."""
    if platform.system() == "Linux":
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg:
            x = Path(os.path.expanduser(xdg))
            if x.is_absolute():
                return x / "claude-swap"
        return Path.home() / ".local" / "share" / "claude-swap"
    return Path.home() / ".claude-swap-backup"


def local_state():
    """({slot: {"email", "org"}}, active slot or None) from cswap's
    sequence.json. Metadata only; credentials live elsewhere and are never
    opened here."""
    d = read_json(cswap_backup_root() / "sequence.json")
    out = {}
    accounts = d.get("accounts")
    for num, a in (accounts.items() if isinstance(accounts, dict) else []):
        if isinstance(a, dict) and isinstance(a.get("email"), str):
            out[str(num)] = {"email": a["email"], "org": a.get("organizationUuid") or ""}
    active = d.get("activeAccountNumber")
    return out, (str(active) if active is not None else None)


def local_accounts():
    return local_state()[0]


def find_cswap(explicit=None):
    for cand in (explicit, os.environ.get("CSWAP_SATELLITE_CSWAP"),
                 str(Path.home() / ".local" / "bin" / "cswap"), shutil.which("cswap")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return os.path.abspath(cand)
    raise Fail("找不到 cswap。请先在这台电脑上安装 claude-swap（cswap）。")


def find_ssh(explicit=None):
    cand = explicit or os.environ.get("CSWAP_SATELLITE_SSH") or "ssh"
    p = shutil.which(cand)
    if not p:
        raise Fail(f"找不到 ssh 命令：{cand}")
    return os.path.abspath(p)


def check_server(server):
    if not server:
        raise Fail("没有指定服务器。用 --server 主机名（第一次 setup 之后会记住）。")
    if server.startswith("-") or any(c.isspace() for c in server):
        raise Fail(f"服务器名不对：{server!r}")
    return server


# ------------------------------------------------------------ server data

def fetch_remote(server, ssh, remote_cswap, timeout=90, local_cswap=None):
    """`cswap list --json` from the server: slot numbers, emails, usage.
    With server "local", from this machine's own cswap (for usage bindings
    on the machine that holds the real logins)."""
    check_server(server)
    if server == LOCAL:
        cmd = [local_cswap or find_cswap(), "list", "--json"]
    else:
        cmd = [ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", server,
               f"{remote_cswap} list --json"]
    r = run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise Fail(f"在服务器 {server} 上运行 cswap list --json 失败：{err[0][:200]}")
    try:
        doc = json.loads(r.stdout.decode("utf-8"))
    except ValueError:
        raise Fail("服务器返回的不是 JSON（cswap 版本太旧？）。")
    if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), list):
        raise Fail("服务器返回的数据格式不对。")
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        raise Fail(f"服务器 cswap 的数据版本 {doc.get('schemaVersion')!r} 不认识。")
    return doc


def remote_rows(doc):
    rows = []
    for r in doc.get("accounts") or []:
        if (isinstance(r, dict) and isinstance(r.get("number"), int) and not isinstance(r.get("number"), bool)
                and isinstance(r.get("email"), str) and r["email"]):
            rows.append(r)
    return rows


def is_real_login(r):
    """A server slot that is a normal browser login (not a setup-token or
    API-key slot). loginExpiresAt alone is not enough: older logins and
    unreadable credentials don't report it."""
    if r["email"].endswith("@token.local"):
        return False
    if r.get("usageStatus") == "api_key" or r.get("kind") == "api_key":
        return False
    return bool(r.get("loginExpiresAt") or r.get("organizationUuid"))


def select_accounts(doc, wanted=None):
    """Accounts to set up: the given slot numbers, or by default every
    server account that is a real login."""
    rows = remote_rows(doc)
    if wanted:
        by = {r["number"]: r for r in rows}
        missing = [n for n in wanted if n not in by]
        if missing:
            raise Fail("服务器上没有这些槽位：" + ", ".join(map(str, missing)))
        return [by[n] for n in wanted]
    return [r for r in rows if is_real_login(r)]


def plan_slot(row, local):
    """What to do for one server account on this machine:
    ("add", slot) for a new token slot at the server's slot number,
    ("update", slot) to refresh an existing token slot in place, or
    ("skip", reason). cswap identifies token slots by (email, org ""), and
    `add-token --slot N` would silently move such an account from another
    slot, so every slot holding this email is looked at, not only N."""
    num, email = str(row["number"]), row["email"]
    for m, a in sorted(local.items()):
        if a["email"] == email and a["org"] == "":
            return "update", m
    for m, a in sorted(local.items()):
        if a["email"] == email:
            return "skip", f"这台电脑的槽位 {m} 已经是这个账号的正常登录，不需要 token"
    cur = local.get(num)
    if cur is None:
        return "add", num
    return "skip", (f"这台电脑的槽位 {num} 已经是另一个账号。"
                    f"确认不要它了再运行 cswap remove {num}，然后重新 setup")


def usage_document(doc, local, fetched_s=0.0, binds=None):
    """Server readings re-keyed to this machine's slots, as `cswap
    import-usage -` expects. Matching: same slot number and email first,
    then a unique email. Only token slots (org "") take readings, with the
    org rewritten; real logins keep fetching their own.
    Rows cswap would reject (bad age) are dropped so they can't fail the
    whole import."""
    rows = []
    for r in remote_rows(doc):
        age = r.get("usageAgeSeconds")
        if (r.get("usageStatus") != "ok" or not isinstance(r.get("usage"), dict)
                or not isinstance(age, (int, float)) or isinstance(age, bool) or not math.isfinite(age)):
            continue
        rows.append(r)
    by_email = {}
    for num, a in local.items():
        by_email.setdefault(a["email"], []).append(num)
    remote_by_email = {}
    for r in remote_rows(doc):
        remote_by_email.setdefault(r["email"], []).append(r)
    out, used = [], set()
    for r in rows:
        num = str(r["number"])
        target = None
        if num in local and local[num]["email"] == r["email"]:
            target = num
        elif len(by_email.get(r["email"], [])) == 1 and len(remote_by_email.get(r["email"], [])) == 1:
            target = by_email[r["email"]][0]
        if target is None or target in used:
            continue
        if local[target]["org"]:
            # A real login reads its own usage; feeding it (with --hold) would
            # stop its own fetching and could pin a stale reading.
            continue
        used.add(target)
        out.append({
            "email": local[target]["email"],
            "organizationUuid": "",
            "usageStatus": "ok",
            "usage": r["usage"],
            "usageAgeSeconds": max(0.0, float(r["usageAgeSeconds"])) + max(0.0, fetched_s),
        })
    # Bindings: a token slot whose token was made from another account (its
    # cswap label is synthetic, e.g. setup-token-4@token.local) takes that
    # account's reading; usage is per account, not per token.
    ok_rows = {r["number"]: r for r in rows}
    for lslot, rnum in sorted((binds or {}).items()):
        lslot = str(lslot)
        r = ok_rows.get(int(rnum)) if str(rnum).isdigit() else None
        if r is None or lslot in used or lslot not in local or local[lslot]["org"] != "":
            continue
        used.add(lslot)
        out.append({
            "email": local[lslot]["email"],
            "organizationUuid": "",
            "usageStatus": "ok",
            "usage": r["usage"],
            "usageAgeSeconds": max(0.0, float(r["usageAgeSeconds"])) + max(0.0, fetched_s),
        })
    return {"schemaVersion": SCHEMA_VERSION, "accounts": out}


# ------------------------------------------------------------ tokens

def check_token(tok):
    tok = (tok or "").strip()
    if tok.startswith("sk-ant-api"):
        raise Fail("这是按量付费的 API key，不是订阅的 setup-token（应该以 sk-ant-oat01- 开头）。")
    if not TOKEN_RE.fullmatch(tok):
        raise Fail("看起来不是 setup-token（应该是 sk-ant-oat01- 开头的一整串，中间没有空格）。")
    return tok


def have_tty():
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
        return True
    except OSError:
        return False


def read_token(row, op_template):
    if op_template:
        op = shutil.which("op")
        if not op:
            raise Fail("找不到 1Password 命令行 op。")
        ref = op_template.format(email=row["email"], number=row["number"])
        r = run([op, "read", ref], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if r.returncode != 0:
            raise Fail(f"op read 失败（{ref}）。")
        return r.stdout.decode("utf-8", "replace")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(f"粘贴账号 {row['number']} 的 setup-token（不会显示，直接回车跳过）：")
        except getpass.GetPassWarning:
            raise Fail("无法关闭输入回显，为了不让 token 显示在屏幕上，拒绝读取。")


def add_token(cswap, row, tok, action, slot, capture=False):
    """`cswap add-token -`; --slot only for a new slot, so an update takes
    cswap's refresh-in-place path and keeps the slot's alias/disabled flag."""
    cmd = [cswap, "add-token", "-", "--email", row["email"]]
    if action == "add":
        cmd[3:3] = ["--slot", slot]
    out = subprocess.PIPE if capture else None
    r = run(cmd, input=(tok + "\n").encode("utf-8"), stdout=out, stderr=out, timeout=60)
    return r.returncode == 0


def plan_one(r, local, seen):
    """plan_slot plus the rule that one email can only be one token slot:
    the first server row with an email wins (`seen` holds emails already
    taken by earlier rows)."""
    if r["email"] in seen:
        return "skip", "同一个邮箱在服务器上有两个组织，这台电脑只能用 token 加其中一个（用 --accounts 选）"
    return plan_slot(r, local)


def install_one(cswap, r, tok, action, slot, active, capture=False):
    """Add/refresh one token slot and verify it; returns (ok, [messages])."""
    label = f"账号 {r['number']}（{r['email']}）"
    ok = add_token(cswap, r, tok, action, slot, capture)
    after = local_accounts().get(slot)
    if not ok or not after or after["email"] != r["email"] or after["org"] != "":
        return False, [f"{label}：cswap 没有写入。"]
    where = f"，在本机槽位 {slot}" if slot != str(r["number"]) else ""
    msgs = [f"{'已更新' if action == 'update' else '已添加'}{label}{where}"]
    if action == "update" and slot == active:
        # The live login of the active slot still holds the old token;
        # a later switch-away would back it up over the new one.
        out = subprocess.DEVNULL if capture else None
        s = run([cswap, "switch", slot, "--force"], stdin=subprocess.DEVNULL, stdout=out, stderr=out, timeout=120)
        if s.returncode == 0:
            msgs.append(f"账号 {slot} 正在这台电脑上使用，已切换到新 token。")
        else:
            msgs.append(f"注意：账号 {slot} 正在这台电脑上使用。请退出正在运行的 claude 后运行 "
                        f"cswap switch {slot} --force，否则下次切换账号时新 token 会被旧的覆盖。")
    return True, msgs


def prepare(a, cfg):
    """Server rows to set up, with cswap's store migrated and read."""
    server = check_server(a.server or cfg.get("server"))
    remote_cswap = a.remote_cswap or cfg.get("remoteCswap") or DEFAULT_REMOTE_CSWAP
    cswap = find_cswap(a.cswap)
    doc = fetch_remote(server, find_ssh(a.ssh), remote_cswap, local_cswap=cswap)
    wanted = [int(x) for x in a.accounts.split(",")] if getattr(a, "accounts", None) else None
    chosen = select_accounts(doc, wanted)
    if not chosen:
        raise Fail("服务器上没有找到正常登录的账号。可以用 --accounts 1,2,3 直接指定。")
    # Lets cswap finish any pending data-dir migration before we read its store
    # (any command that opens the store does; `list --json` is upstream's oldest
    # read command. It may also refresh stale usage over the network: setup only).
    run([cswap, "list", "--json"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    cfg.update({"server": server, "remoteCswap": remote_cswap})
    write_json(config_path(), cfg)
    return server, cswap, chosen


# ------------------------------------------------------------ commands

def cmd_setup(a):
    cfg = load_config()
    if not a.op and not have_tty():
        raise Fail("需要在终端里运行，才能隐藏输入 token（通过 ssh 运行请加 -t）；或者用 --op 从 1Password 读取。")
    server, cswap, chosen = prepare(a, cfg)
    print(f"服务器 {server} 上的账号：")
    for r in chosen:
        print(f"  {r['number']}: {r['email']}")
    if not a.op:
        print("\n每个账号需要一个 setup-token：在任意一台电脑上用这个账号登录浏览器，运行 claude setup-token，"
              "把得到的 sk-ant-oat01-… 存进密码管理器，再粘贴到这里。\n")
    done, seen = 0, set()
    for r in chosen:
        label = f"账号 {r['number']}（{r['email']}）"
        local, active = local_state()
        action, slot = plan_one(r, local, seen)
        seen.add(r["email"])
        if action == "skip":
            print(f"跳过{label}：{slot}")
            continue
        try:
            tok = read_token(r, a.op)
            if not tok.strip():
                print(f"跳过{label}")
                continue
            tok = check_token(tok)
        except Fail as e:
            print(f"{label}：{e}")
            continue
        except EOFError:
            print("\n输入结束，停止。")
            break
        ok, msgs = install_one(cswap, r, tok, action, slot, active)
        tok = None  # noqa: F841 - drop the only reference
        done += ok
        print("\n  ".join(msgs))
    print(f"\n完成 {done} 个。服务器设置已记住：{config_path()}")
    if done:
        print("下一步：\n"
              "  1. cswap switch <编号>        选一个账号开始用\n"
              "  2. 在 claude 里运行 /status   确认是订阅登录，不是 API key\n"
              f"  3. {prog()} install-timer   每 5 分钟从服务器同步用量")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("\n注意：这台电脑设置了 ANTHROPIC_API_KEY 环境变量，Claude Code 可能会优先用它按量付费。")
    return 0


def cmd_plan(a):
    """For the VS Code extension: what setup would do, as JSON."""
    server, _, chosen = prepare(a, load_config())
    local, _ = local_state()
    seen, rows = set(), []
    for r in chosen:
        action, slot = plan_one(r, local, seen)
        seen.add(r["email"])
        rows.append({"number": r["number"], "email": r["email"], "action": action,
                     "slot": slot if action != "skip" else None,
                     "reason": slot if action == "skip" else None})
    print(json.dumps({"server": server, "accounts": rows}, ensure_ascii=False))
    return 0


def cmd_add(a):
    """For the VS Code extension: add one account; the token arrives on
    stdin (a pipe, never a terminal, so it is never echoed)."""
    if sys.stdin.isatty():
        raise Fail("add 只从管道读取 token；在终端里请用 setup。")
    tok = check_token(sys.stdin.read(4096))
    a.accounts = None
    _, cswap, chosen = prepare(a, load_config())
    seen = set()
    for r in chosen:
        if r["number"] == a.account:
            local, active = local_state()
            action, slot = plan_one(r, local, seen)
            if action == "skip":
                raise Fail(f"跳过账号 {r['number']}：{slot}")
            ok, msgs = install_one(cswap, r, tok, action, slot, active, capture=True)
            tok = None  # noqa: F841
            print("\n".join(msgs))
            return 0 if ok else 1
        seen.add(r["email"])
    raise Fail(f"服务器上没有正常登录的账号 {a.account}。")


def record_sync(ok, message):
    try:
        write_json(state_path(), {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": ok, "message": message[:300]})
    except OSError:
        pass


def cmd_sync(a):
    try:
        msg = sync_once(a)
    except Fail as e:
        if not a.dry_run:
            record_sync(False, str(e))
        raise
    if not a.dry_run:
        record_sync(True, msg)
    if not a.quiet or msg.startswith("没有"):
        print(msg)
    return 0


def sync_once(a):
    cfg = load_config()
    server = a.server or cfg.get("server")
    remote_cswap = a.remote_cswap or cfg.get("remoteCswap") or DEFAULT_REMOTE_CSWAP
    cswap = find_cswap(a.cswap)
    t0 = time.monotonic()
    doc = fetch_remote(server, find_ssh(a.ssh), remote_cswap, local_cswap=cswap)
    payload = usage_document(doc, local_accounts(), time.monotonic() - t0, cfg.get("bind") or {})
    if not payload["accounts"]:
        return "没有可以同步的用量（服务器上没有读到用量，或者这台电脑没有对应的账号）。"
    if a.dry_run:
        return json.dumps({"accounts": [{"email": x["email"], "usageAgeSeconds": round(x["usageAgeSeconds"])}
                                        for x in payload["accounts"]]}, ensure_ascii=False, indent=2)
    r = run([cswap, "import-usage", "-", "--hold", str(a.hold)],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    out, err = import_output(r.stdout), import_output(r.stderr)
    if r.returncode != 0:
        raise Fail("cswap import-usage 失败：" + ((err or out)[-1:] or [""])[0][:300])
    # cswap prints its "Done: …" summary on stderr; stdout wins if it ever has one.
    return (out or err or [f"已同步 {len(payload['accounts'])} 个账号"])[-1][:300]


def import_output(raw):
    """Non-empty lines of cswap's output, without its "newer version" notice."""
    lines = (re.sub(r"\x1b\[[0-9;]*m", "", x).strip() for x in raw.decode("utf-8", "replace").splitlines())
    return [x for x in lines if x and not x.startswith(UPDATE_NOTICE)]


UPDATE_NOTICE = "A newer version of claude-swap is available"


def prog():
    return "cswap-satellite"


def timer_command(a):
    cfg = load_config()
    server = check_server(a.server or cfg.get("server"))
    cmd = [sys.executable, str(Path(__file__).resolve()), "sync-usage", "--quiet",
           "--server", server, "--cswap", find_cswap(a.cswap), "--ssh", find_ssh(a.ssh),
           "--hold", str(a.hold)]
    if a.remote_cswap or cfg.get("remoteCswap"):
        cmd += ["--remote-cswap", a.remote_cswap or cfg.get("remoteCswap")]
    return cmd


def job_env():
    """Store locations the interactive shell uses, handed to the timer job
    (systemd/launchd don't read shell rc files)."""
    return {k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}


def cron_line(cmd, interval):
    every = max(1, int(interval) // 60)
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in job_env().items())
    line = " ".join(shlex.quote(c) for c in cmd)
    return f"*/{every} * * * * " + (env + " " if env else "") + line.replace("%", "\\%")


def launchd_plist(cmd, interval, log, env=None):
    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    args = "".join(f"\n    <string>{esc(c)}</string>" for c in cmd)
    envxml = ""
    if env:
        envxml = ("\n  <key>EnvironmentVariables</key>\n  <dict>"
                  + "".join(f"\n    <key>{esc(k)}</key>\n    <string>{esc(v)}</string>" for k, v in env.items())
                  + "\n  </dict>")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>{envxml}
  <key>StartInterval</key>
  <integer>{int(interval)}</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{esc(log)}</string>
  <key>StandardErrorPath</key>
  <string>{esc(log)}</string>
</dict>
</plist>
"""


def systemd_units(cmd, interval, env=None):
    def q(s):  # ExecStart word: systemd expands %specifiers and $VARS even inside quotes
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'

    def qenv(s):  # Environment= does specifiers but no $ expansion
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    envlines = "".join(f"Environment={qenv(k + '=' + v)}\n" for k, v in (env or {}).items())
    service = ("[Unit]\nDescription=cswap-satellite: pull Claude account usage from the server\n\n"
               "[Service]\nType=oneshot\n" + envlines + "ExecStart=" + " ".join(q(c) for c in cmd) + "\n")
    timer = ("[Unit]\nDescription=cswap-satellite usage sync timer\n\n"
             f"[Timer]\nOnBootSec=1min\nOnUnitActiveSec={int(interval)}s\nPersistent=false\n\n"
             "[Install]\nWantedBy=timers.target\n")
    return service, timer


def user_systemd_ok():
    if not Path("/run/systemd/system").is_dir() or not shutil.which("systemctl"):
        return None
    r = run(["systemctl", "--user", "show-environment"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def unit_dir(manager_env=None):
    """Where the user manager looks: its own XDG_CONFIG_HOME, else ~/.config."""
    for line in (manager_env or "").splitlines():
        if line.startswith("XDG_CONFIG_HOME="):
            v = line.split("=", 1)[1].strip()
            if v.startswith("/"):
                return Path(v) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def plist_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def timer_installed():
    if is_mac():
        r = run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return r.returncode == 0
    if not shutil.which("systemctl"):
        return False
    r = run(["systemctl", "--user", "is-enabled", f"{TIMER_NAME}.timer"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    return r.returncode == 0


def test_like_job(cmd):
    """Run one dry sync with roughly the environment a timer job gets, to
    catch an SSH key that only works through the shell's agent."""
    env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME") if k in os.environ}
    env.update(job_env())
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    sock = os.environ.get("SSH_AUTH_SOCK", "")
    if is_mac() and "com.apple.launchd" in sock:
        env["SSH_AUTH_SOCK"] = sock  # launchd hands Apple's agent to every job
    test = [c for c in cmd if c != "--quiet"] + ["--dry-run"]
    r = run(test, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    return r.returncode == 0, r.stdout.decode("utf-8", "replace").strip()


def cmd_install_timer(a):
    if a.interval < 60:
        raise Fail("间隔至少 60 秒。")
    cmd = timer_command(a)
    env = job_env()
    if a.hold < a.interval * 2:
        print(f"提示：--hold {a.hold} 比两次同步的间隔短，偶尔会由本机自己去查用量（setup-token 查不到）。")
    if is_mac():
        p = plist_path()
        log = str(Path.home() / "Library" / "Logs" / "cswap-satellite.log")
        p.parent.mkdir(parents=True, exist_ok=True)
        uid = str(os.getuid())
        run(["launchctl", "bootout", f"gui/{uid}", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.write_text(launchd_plist(cmd, a.interval, log, env), "utf-8")
        r = None
        for _ in range(3):  # bootstrap right after bootout can briefly fail with EIO
            r = run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], stderr=subprocess.PIPE)
            if r.returncode == 0:
                break
            time.sleep(1)
        if r.returncode != 0:
            p.unlink()
            raise Fail("launchctl bootstrap 失败：" + r.stderr.decode("utf-8", "replace").strip()[:200])
        print(f"已安装：{p}\n日志：{log}")
    else:
        manager_env = user_systemd_ok()
        if manager_env is None:
            raise Fail("这台电脑没有可用的 systemd 用户服务（比如 WSL 没开 systemd）。"
                       "请运行 crontab -e，加入这一行：\n" + cron_line(cmd, a.interval))
        d = unit_dir(manager_env)
        d.mkdir(parents=True, exist_ok=True)
        service, timer = systemd_units(cmd, a.interval, env)
        files = [d / f"{TIMER_NAME}.service", d / f"{TIMER_NAME}.timer"]
        files[0].write_text(service, "utf-8")
        files[1].write_text(timer, "utf-8")
        run(["systemctl", "--user", "daemon-reload"])
        r = run(["systemctl", "--user", "enable", "--now", f"{TIMER_NAME}.timer"])
        if r.returncode != 0:
            for f in files:
                f.unlink()
            run(["systemctl", "--user", "daemon-reload"])
            raise Fail("systemctl --user enable 失败。也可以用 cron：\n" + cron_line(cmd, a.interval))
        print(f"已安装：{files[1]}\n"
              f"日志：journalctl --user -u {TIMER_NAME}\n"
              "（要在没登录时也运行：sudo loginctl enable-linger $USER）")
    ok, out = test_like_job(cmd)
    if not ok:
        print("\n注意：用定时任务的环境试连了一次服务器，失败了：\n  " + out.splitlines()[-1][:300] +
              "\n多半是 SSH 私钥有密码、只能通过当前终端里的 ssh-agent 使用，而定时任务拿不到这个 agent。办法："
              "在 ~/.ssh/config 里给这台服务器写 IdentityAgent（指向固定位置的 agent socket），"
              "或者单独准备一把没有密码的 key，在服务器的 authorized_keys 里限制成 command=\"~/.local/bin/cswap list --json\"。")
    return 0


def cmd_uninstall_timer(a):
    if is_mac():
        p = plist_path()
        run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if p.exists():
            p.unlink()
    else:
        manager_env = user_systemd_ok()
        if manager_env is not None:
            run(["systemctl", "--user", "disable", "--now", f"{TIMER_NAME}.timer"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for d in {unit_dir(manager_env), unit_dir()}:
            for ext in ("timer", "service"):
                f = d / f"{TIMER_NAME}.{ext}"
                if f.exists():
                    f.unlink()
        if manager_env is not None:
            run(["systemctl", "--user", "daemon-reload"])
    print("已移除定时同步。")
    return 0


def cmd_bind(a):
    m = re.fullmatch(r"(\d+)=(\d+)", a.pair or "")
    if not m:
        raise Fail("格式：bind 本机槽位=服务器槽位，例如 bind 4=1")
    lslot, rnum = m.group(1), m.group(2)
    local = local_accounts()
    if lslot not in local:
        raise Fail(f"这台电脑的 cswap 里没有槽位 {lslot}。")
    if local[lslot]["org"] != "":
        raise Fail(f"槽位 {lslot} 是正常登录，自己就能查用量，不需要绑定。")
    cfg = load_config()
    cfg.setdefault("bind", {})[lslot] = rnum
    write_json(config_path(), cfg)
    print(f"已绑定：槽位 {lslot} 显示服务器账号 {rnum} 的用量（下次同步生效）。")
    return 0


def cmd_unbind(a):
    cfg = load_config()
    if (cfg.get("bind") or {}).pop(str(a.slot), None) is None:
        raise Fail(f"槽位 {a.slot} 没有绑定。")
    write_json(config_path(), cfg)
    print(f"已取消槽位 {a.slot} 的绑定。")
    return 0


def cmd_status(a):
    cfg = load_config()
    print(f"cswap-satellite {VERSION}")
    print(f"服务器：{cfg.get('server') or '（还没有 setup）'}")
    local, active = local_state()
    if not local:
        print("这台电脑的 cswap 里还没有账号。")
    for num in sorted(local, key=lambda n: int(n) if n.isdigit() else 0):
        kind = "setup-token" if local[num]["org"] == "" else "正常登录"
        print(f"  {num}: {local[num]['email']}（{kind}）{' ← 正在用' if num == active else ''}")
    for lslot, rnum in sorted((cfg.get("bind") or {}).items()):
        print(f"绑定：槽位 {lslot} ← 服务器账号 {rnum} 的用量")
    print(f"定时同步：{'已启用' if timer_installed() else '未启用'}")
    st = read_json(state_path())
    if st:
        print(f"上次同步：{st.get('time')} {'成功' if st.get('ok') else '失败'} — {st.get('message')}")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("注意：设置了 ANTHROPIC_API_KEY，Claude Code 可能会用它按量付费。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog=prog(), description="在多台电脑上用同一批 Claude 账号，不用每台登录。")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--server", help="服务器的 SSH 主机名（ssh 能直接连上的名字）；local 表示就是这台电脑")
        p.add_argument("--remote-cswap", help=f"服务器上 cswap 的路径（默认 {DEFAULT_REMOTE_CSWAP}）")
        p.add_argument("--cswap", help="本机 cswap 的路径")
        p.add_argument("--ssh", help="ssh 命令的路径")

    p = sub.add_parser("setup", help="把服务器上的账号用 setup-token 加到这台电脑")
    common(p)
    p.add_argument("--accounts", help="只设置这些槽位，例如 1,2,3（默认：服务器上所有正常登录的账号）")
    p.add_argument("--op", metavar="TEMPLATE",
                   help='从 1Password 读取 token，例如 "op://Private/Claude {email}/credential"（可用 {email} {number}）')
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("bind", help="让一个 setup-token 槽位显示另一个账号的用量，例如 bind 4=1")
    p.add_argument("pair", metavar="本机槽位=服务器槽位")
    p.set_defaults(fn=cmd_bind)

    p = sub.add_parser("unbind", help="取消绑定")
    p.add_argument("slot", type=int)
    p.set_defaults(fn=cmd_unbind)

    p = sub.add_parser("plan", help="（给插件用）以 JSON 列出 setup 会怎么处理每个账号")
    common(p)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("add", help="（给插件用）从标准输入读一个 token，加一个账号")
    common(p)
    p.add_argument("--account", type=int, required=True)
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("sync-usage", help="从服务器拉一次用量")
    common(p)
    p.add_argument("--hold", type=int, default=900, help="这段时间内本机不自己查用量（秒，默认 900）")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只显示会同步哪些账号")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("install-timer", help="安装定时同步（macOS launchd / Linux systemd）")
    common(p)
    p.add_argument("--interval", type=int, default=300, help="同步间隔秒数（默认 300）")
    p.add_argument("--hold", type=int, default=900)
    p.set_defaults(fn=cmd_install_timer)

    p = sub.add_parser("uninstall-timer", help="移除定时同步")
    p.set_defaults(fn=cmd_uninstall_timer)

    p = sub.add_parser("status", help="显示这台电脑的设置")
    p.set_defaults(fn=cmd_status)

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    try:
        return a.fn(a)
    except Fail as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
