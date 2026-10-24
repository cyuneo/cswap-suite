#!/usr/bin/env python3
"""api-worker: background Claude Agent SDK tasks paid from a Console API key.

Claude Max/Team plans include monthly Claude API credits that cover the
Agent SDK but not Claude Code. This tool lets an interactive Claude Code
session (subscription) hand self-contained jobs to Agent SDK workers that
run on the API key instead, through the `api-worker` Claude Code plugin or
this command line.

Safety rails:
- The key lives in ~/.config/api-worker/key (0600) or comes from a
  `keyCommand` (e.g. `op read ...`); it is put only into the worker's own
  environment, never printed, logged or stored in task files.
- A worker runs with its own CLAUDE_CONFIG_DIR and no inherited Claude
  login variables, so it can never fall back to the subscription login.
- Every task has a dollar cap (Agent SDK max_budget_usd); a monthly ledger
  refuses new tasks that could push the month past `monthlyBudgetUsd`.
- Tools are allowed by a callback: reading anywhere in the task directory,
  writing only inside it, Bash and web access only when the task asked.
- In a git repository the task gets its own worktree and branch by default,
  so it never edits the files an interactive session is working on.

Commands: set-key, start, run (internal), list, status, result, log, cancel,
spend, config. Needs Python 3.10+ with claude-agent-sdk (see install.sh).
"""

import argparse
import asyncio
import getpass
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

VERSION = "0.4.1"
# Console API keys: sk-ant-api03-... and the newer sk-ant-usr... form.
KEY_RE = re.compile(r"sk-ant-(?:api\d\d|usr[0-9a-z]*)-[A-Za-z0-9_\-]{20,}")
DEFAULTS = {
    "model": "claude-sonnet-5-5",
    "budgetUsd": 2.0,         # per task unless asked otherwise
    "maxBudgetUsd": 20.0,     # hard cap for one task
    "monthlyBudgetUsd": 200.0,
    "maxParallel": 3,
    "maxTurns": 200,
    "keyCommand": "",       # legacy: the key command of the key named "default"
    "keyCommands": {},      # key name -> command that prints that key
    "keyBudgets": {},       # key name -> monthly cap in USD (default monthlyBudgetUsd)
    "deskRepo": "",         # private git repo (URL) cloud tasks use when their folder has none
    "deskMaxMb": 20.0,      # most one task may upload to the desk repo
}
# Environment that could carry a Claude login into the worker.
LOGIN_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
             "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
             "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE",
             "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
READ_TOOLS = {"Read", "Glob", "Grep", "LS", "TodoWrite", "Task", "Agent", "ToolSearch"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
WEB_TOOLS = {"WebFetch", "WebSearch"}


class Fail(Exception):
    pass


# ------------------------------------------------------------ paths & config

def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "api-worker"


def data_dir():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "api-worker"


def tasks_dir():
    return data_dir() / "tasks"


def read_json(p, default=None):
    try:
        d = json.loads(Path(p).read_text("utf-8"))
        return d if isinstance(d, dict) else (default if default is not None else {})
    except (OSError, ValueError):
        return default if default is not None else {}


def write_json(p, d, mode=None):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", "utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, p)


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in read_json(config_dir() / "config.json").items() if k in DEFAULTS})
    return cfg


NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")


def keys_dir():
    return config_dir() / "keys"


def key_names(cfg):
    """Names of the stored keys: files in keys/, key commands, and the legacy
    single key as "default"."""
    names = set(cfg.get("keyCommands") or {})
    if keys_dir().is_dir():
        names |= {p.name for p in keys_dir().iterdir() if NAME_RE.fullmatch(p.name)}
    if cfg.get("keyCommand") or (config_dir() / "key").exists():
        names.add("default")
    return sorted(names)


def key_budget(cfg, name):
    return float((cfg.get("keyBudgets") or {}).get(name, cfg["monthlyBudgetUsd"]))


def read_key(cfg, name="default"):
    """The API key called `name`, from its command or its 0600 file. Never printed."""
    command = (cfg.get("keyCommands") or {}).get(name) or (cfg.get("keyCommand") if name == "default" else "")
    if command:
        try:
            r = subprocess.run(command, shell=True, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise Fail(f"key「{name}」的命令运行失败。")
        key = r.stdout.strip()
    else:
        p = keys_dir() / name
        if not p.exists() and name == "default":
            p = config_dir() / "key"
        try:
            st = p.stat()
        except OSError:
            raise Fail(f"没有名为「{name}」的 API key。运行 api-worker set-key --name {name}。")
        if st.st_mode & 0o077:
            raise Fail(f"{p} 的权限太宽，请运行 chmod 600 {p}。")
        key = p.read_text("utf-8").strip()
    if not KEY_RE.fullmatch(key):
        raise Fail(f"key「{name}」格式不对（应该以 sk-ant-api 或 sk-ant-usr 开头）。")
    return key


def pick_key(cfg, name, budget):
    """The key a new task runs on: the named one, or the one with the most
    left this month that can still cover `budget`."""
    names = key_names(cfg)
    if not names:
        raise Fail("还没有设置 API key。运行 api-worker set-key。")
    if name and name not in names:
        raise Fail(f"没有名为「{name}」的 key。现有：{', '.join(names)}")
    room = {}
    for n in ([name] if name else names):
        spent, reserved = month_spend(n)
        room[n] = key_budget(cfg, n) - spent - reserved
    best = max(room, key=lambda n: room[n])
    if room[best] < budget:
        raise Fail(f"本月额度不够：{'、'.join(f'{n} 还剩 ${max(r, 0):.2f}' for n, r in room.items())}，"
                   f"这个任务要预留 ${budget:.2f}。")
    read_key(cfg, best)  # fail now, not in the background
    return best


# ------------------------------------------------------------ tasks

def task_path(tid):
    if not re.fullmatch(r"t[0-9a-f]{8}", tid or ""):
        raise Fail(f"任务编号不对：{tid!r}")
    p = tasks_dir() / tid
    if not (p / "task.json").exists():
        raise Fail(f"没有任务 {tid}。")
    return p


# States in which a task has ended (`list` reports them as "final": true). A
# "cloud" task without a result branch stays "cloud", not final; a "lost"
# cloud launch still turns "done" if its result branch shows up later.
FINAL_STATES = frozenset(("done", "failed", "cancelled", "lost"))


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie or a reused pid is not our worker
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return any(b"api_worker" in c for c in cmd)
    except OSError:
        return True  # no /proc (macOS): trust kill(0)


def task_info(p):
    t = read_json(p / "task.json")
    s = read_json(p / "status.json")
    state = s.get("state", "queued")
    if state in ("queued", "running") and not pid_alive(s.get("pid")) and time.time() - s.get("updatedAt", 0) > 30:
        state = "lost"  # the worker died without writing an end state
    if t.get("kind") == "cloud":  # runs on claude.ai; it reports back through a git branch
        state = s.get("state")
        if state == "launching" and not pid_alive(s.get("pid")):
            state = "lost"  # killed while starting (e.g. a timeout); the session may exist anyway
        elif state not in ("launching", "done", "failed"):
            state = "running" if t.get("resultBranch") else "cloud"
        note = None
        if state == "lost":
            note = "启动时被中断，没拿到会话链接；云端会话可能已经开了，到 claude.ai/code 查看"
        elif state == "cloud":
            note = "不是带远程的 git 仓库，结果拉不回来，只能在 sessionUrl 查看"
        return {"id": p.name, "kind": "cloud", "title": t.get("title"), "state": state,
                "final": state in FINAL_STATES,
                "cwd": t.get("cwd"), "account": t.get("account"), "sessionUrl": t.get("sessionUrl"),
                "resultBranch": t.get("resultBranch"), "createdAt": t.get("createdAt"),
                "endedAt": s.get("endedAt"), "checkedAt": s.get("checkedAt"), "error": s.get("error"),
                "warnings": t.get("warnings") or [], "note": note}
    return {"id": p.name, "kind": "api", "title": t.get("title"), "state": state, "cwd": t.get("cwd"),
            "final": state in FINAL_STATES,
            "workdir": s.get("workdir") or t.get("cwd"), "branch": s.get("branch"),
            "model": t.get("model"), "budgetUsd": t.get("budgetUsd"), "costUsd": s.get("costUsd"),
            "turns": s.get("turns"), "createdAt": t.get("createdAt"), "endedAt": s.get("endedAt"),
            "lastActivity": s.get("lastActivity"), "error": s.get("error"), "key": t.get("key", "default")}


def all_tasks():
    out = []
    root = tasks_dir()
    if root.is_dir():
        for p in root.iterdir():
            if (p / "task.json").exists():
                out.append(task_info(p))
    out.sort(key=lambda t: t.get("createdAt") or 0, reverse=True)
    return out


def month_key(ts=None):
    return time.strftime("%Y-%m", time.localtime(ts or time.time()))


def month_spend(key=None):
    """Spent this month (on one key, or all): finished tasks' costs plus
    running tasks' caps."""
    spent, reserved = 0.0, 0.0
    for t in all_tasks():
        if t.get("kind") != "api" or month_key(t.get("createdAt")) != month_key():
            continue
        if key is not None and t.get("key", "default") != key:
            continue
        if t["state"] in ("queued", "running"):
            reserved += float(t.get("budgetUsd") or 0)
        else:
            spent += float(t.get("costUsd") or 0)
    return round(spent, 4), round(reserved, 4)


def git(cwd, *args, timeout=60, text=True):
    """git -C cwd args: never prompts, never waits for stdin; text=False gives
    stdout as bytes (git show of a binary file)."""
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=text, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "" if text else b"", "timeout" if text else b"timeout")


def make_worktree(cwd, tid):
    """A git worktree on a new branch for the task, or None outside git."""
    r = git(cwd, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return None, None
    top = r.stdout.strip()
    rel = os.path.relpath(cwd, top)
    branch = f"api-worker/{tid}"
    wt = data_dir() / "worktrees" / tid
    r = git(top, "worktree", "add", "-q", "-b", branch, str(wt), timeout=300)
    if r.returncode != 0:
        raise Fail("建 git worktree 失败：" + (r.stderr.strip().splitlines() or [""])[-1][:200])
    return str(wt / rel) if rel != "." else str(wt), branch


def cmd_start(a):
    cfg = load_config()
    prompt = a.prompt if a.prompt is not None else sys.stdin.read()
    prompt = (prompt or "").strip()
    if not prompt:
        raise Fail("任务内容是空的。")
    cwd = os.path.realpath(os.path.expanduser(a.cwd or os.getcwd()))
    if not os.path.isdir(cwd):
        raise Fail(f"目录不存在：{cwd}")
    budget = float(a.budget if a.budget is not None else cfg["budgetUsd"])
    if not 0 < budget <= float(cfg["maxBudgetUsd"]):
        raise Fail(f"单个任务的预算要在 0 到 {cfg['maxBudgetUsd']} 美元之间。")
    key_name = pick_key(cfg, a.key, budget)
    running = sum(1 for t in all_tasks() if t["state"] in ("queued", "running"))
    if running >= int(cfg["maxParallel"]):
        raise Fail(f"已经有 {running} 个任务在运行（上限 {cfg['maxParallel']}），等它们结束再派。")
    tid = "t" + uuid.uuid4().hex[:8]
    p = tasks_dir() / tid
    p.mkdir(parents=True)
    workdir, branch = (None, None)
    if a.worktree:
        workdir, branch = make_worktree(cwd, tid)
    task = {"id": tid, "title": (a.title or prompt.splitlines()[0])[:80], "prompt": prompt, "cwd": cwd,
            "workdir": workdir or cwd, "branch": branch, "model": a.model or cfg["model"],
            "budgetUsd": budget, "maxTurns": int(cfg["maxTurns"]), "allowBash": bool(a.allow_bash),
            "allowWeb": bool(a.allow_web), "key": key_name, "createdAt": time.time()}
    write_json(p / "task.json", task)
    write_json(p / "status.json", {"state": "queued", "updatedAt": time.time(), "workdir": task["workdir"],
                                   "branch": branch})
    # status.json is the worker's from here on: it records its own pid and
    # state, so no read-modify-write here can put it back to "queued".
    log = open(p / "worker.log", "ab")
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", tid],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                     start_new_session=True, cwd=task["workdir"])
    log.close()
    print(json.dumps({"id": tid, "title": task["title"], "workdir": task["workdir"], "branch": branch,
                      "model": task["model"], "budgetUsd": budget, "key": key_name}, ensure_ascii=False))
    return 0


# ------------------------------------------------------------ the worker

def inside(path, root):
    try:
        rp = os.path.realpath(os.path.join(root, os.path.expanduser(path)))
    except (TypeError, ValueError):
        return False
    return rp == root or rp.startswith(root.rstrip(os.sep) + os.sep)


def decide(tool, inp, task):
    """(allowed, reason) for one tool call of a worker."""
    root = os.path.realpath(task["workdir"])
    if tool in READ_TOOLS:
        return True, ""
    if tool in WRITE_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if path and inside(path, root):
            return True, ""
        return False, f"只能修改任务目录 {root} 里面的文件。"
    if tool == "Bash":
        return (True, "") if task.get("allowBash") else (False, "这个任务没有允许运行命令（派任务时 allow_bash=true 才行）。")
    if tool in WEB_TOOLS:
        return (True, "") if task.get("allowWeb") else (False, "这个任务没有允许上网。")
    return False, f"后台任务不能使用 {tool}。"


def brief(tool, inp):
    for k in ("command", "file_path", "pattern", "url", "query", "description", "notebook_path"):
        if isinstance(inp.get(k), str):
            return inp[k][:160]
    return ""


def cmd_run(a):
    p = task_path(a.id)
    task = read_json(p / "task.json")
    st_path = p / "status.json"

    def status(**kw):
        st = read_json(st_path)
        st.update(kw, updatedAt=time.time())
        write_json(st_path, st)

    status(state="running", pid=os.getpid(), startedAt=time.time())
    events = open(p / "events.jsonl", "a", encoding="utf-8")

    def event(**kw):
        kw["t"] = time.time()
        events.write(json.dumps(kw, ensure_ascii=False) + "\n")
        events.flush()
        status(lastActivity=kw.get("text") or kw.get("tool") or kw.get("kind"))

    try:
        key = read_key(load_config(), task.get("key", "default"))
    except Fail as e:
        status(state="failed", error=str(e), endedAt=time.time())
        return 1
    # The worker's own Claude home and only the API key as a login.
    for k in LOGIN_ENV:
        os.environ.pop(k, None)
    home = data_dir() / "claude-home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(home)
    os.environ["ANTHROPIC_API_KEY"] = key
    del key

    import claude_agent_sdk as sdk

    async def can_use(tool, inp, ctx):
        ok, why = decide(tool, inp or {}, task)
        event(kind="permission", tool=tool, allowed=ok, text=why or None)
        return sdk.PermissionResultAllow() if ok else sdk.PermissionResultDeny(message=why)

    async def prompts():
        yield {"type": "user", "message": {"role": "user", "content": task["prompt"]}}

    opts = sdk.ClaudeAgentOptions(
        cwd=task["workdir"], model=task["model"], max_budget_usd=float(task["budgetUsd"]),
        max_turns=int(task.get("maxTurns") or 200), can_use_tool=can_use, permission_mode="default",
        setting_sources=[],
        system_prompt={"type": "preset", "preset": "claude_code", "append":
                       "You are a background worker dispatched by another Claude session. Nobody can answer "
                       "questions while you work: decide sensibly yourself, finish the task, and end with a "
                       "concise report of what you did, what changed (files, branch) and anything left open."},
    )
    result = None

    async def main():
        nonlocal result
        turns = 0
        async for m in sdk.query(prompt=prompts(), options=opts):
            if isinstance(m, sdk.AssistantMessage):
                turns += 1
                for b in m.content:
                    if isinstance(b, sdk.TextBlock) and b.text.strip():
                        event(kind="text", text=b.text[:2000])
                    elif isinstance(b, sdk.ToolUseBlock):
                        event(kind="tool", tool=b.name, text=brief(b.name, b.input or {}))
                status(turns=turns)
            elif isinstance(m, sdk.ResultMessage):
                result = m

    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001 - report, never with the key
        msg = str(e).replace(os.environ.get("ANTHROPIC_API_KEY", "\0"), "[key]")[:500]
        status(state="failed", error=f"{type(e).__name__}: {msg}", endedAt=time.time())
        return 1
    finally:
        events.close()
    if result is None:
        status(state="failed", error="Agent SDK 没有返回结果。", endedAt=time.time())
        return 1
    (p / "result.md").write_text(result.result or "", "utf-8")
    ok = not result.is_error and result.subtype == "success"
    status(state="done" if ok else "failed", costUsd=result.total_cost_usd, turns=result.num_turns,
           endedAt=time.time(), error=None if ok else f"{result.subtype}" + (
               f" (HTTP {result.api_error_status})" if getattr(result, "api_error_status", None) else ""))
    return 0 if ok else 1


# ------------------------------------------------------------ queries

def key_rows(cfg):
    rows = []
    for n in key_names(cfg):
        spent, reserved = month_spend(n)
        budget = key_budget(cfg, n)
        rows.append({"name": n, "spentUsd": spent, "reservedUsd": reserved, "monthlyBudgetUsd": budget,
                     "leftUsd": round(max(budget - spent - reserved, 0), 4)})
    return rows


LIST_CLOUD_BUDGET = 15.0  # seconds `list` may spend asking remotes about cloud tasks


def cmd_list(a):
    cfg = load_config()
    refresh_cloud(budget=LIST_CLOUD_BUDGET)
    rows = all_tasks()[: a.limit]
    spent, reserved = month_spend()
    out = {"tasks": rows, "monthSpentUsd": spent, "monthReservedUsd": reserved, "keys": key_rows(cfg)}
    if a.accounts:
        out["accounts"] = cswap_accounts()
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_status(a):
    p = task_path(a.id)
    if read_json(p / "task.json").get("kind") == "cloud":
        refresh_cloud(p, force=True)
        print(json.dumps(task_info(p), ensure_ascii=False))
        return 0
    info = task_info(p)
    ev = []
    try:
        lines = (p / "events.jsonl").read_text("utf-8").splitlines()[-a.tail:]
        ev = [json.loads(x) for x in lines if x.strip()]
    except (OSError, ValueError):
        pass
    info["recent"] = [{"kind": e.get("kind"), "tool": e.get("tool"), "text": e.get("text")} for e in ev]
    print(json.dumps(info, ensure_ascii=False))
    return 0


def cmd_result(a):
    p = task_path(a.id)
    if read_json(p / "task.json").get("kind") == "cloud":
        refresh_cloud(p, force=True)
        info = task_info(p)
        info.update(cloud_result(p) if info["state"] == "done" else {"result": None})
        print(json.dumps(info, ensure_ascii=False))
        return 0
    info = task_info(p)
    try:
        info["result"] = (p / "result.md").read_text("utf-8")
    except OSError:
        info["result"] = None
    print(json.dumps(info, ensure_ascii=False))
    return 0


def cmd_cancel(a):
    p = task_path(a.id)
    if read_json(p / "task.json").get("kind") == "cloud":
        raise Fail("云端会话要在它的页面上停止：" + str(read_json(p / "task.json").get("sessionUrl")))
    st = read_json(p / "status.json")
    pid = st.get("pid")
    if st.get("state") not in ("queued", "running") or not pid_alive(pid):
        raise Fail(f"任务 {a.id} 没有在运行。")
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    st.update(state="cancelled", endedAt=time.time(), updatedAt=time.time())
    write_json(p / "status.json", st)
    print(json.dumps({"id": a.id, "state": "cancelled"}))
    return 0


def git_warnings(cwd):
    """What a cloud session, which clones the pushed repository, will not see."""
    if git(cwd, "rev-parse", "--show-toplevel").returncode != 0:
        return ["这个目录不是 git 仓库，云端会话从空目录开始，看不到这里的文件。"]
    out = []
    if not git(cwd, "remote").stdout.strip():
        out.append("仓库没有远程地址，云端会话拿不到代码。")
    if git(cwd, "status", "--porcelain").stdout.strip():
        out.append("有未提交的改动，云端看不到。")
    ahead = git(cwd, "rev-list", "--count", "@{u}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
        out.append(f"有 {ahead.stdout.strip()} 个提交没推送，云端看不到。")
    elif ahead.returncode != 0:
        out.append("当前分支没有推送到远程，云端会用默认分支。")
    return out


CLOUD_URL_RE = re.compile(r"View:\s*(https://claude\.ai/code/\S+)")
CLOUD_REPORT = "CLOUD_RESULT.md"


def cswap_bin():
    return shutil.which("cswap") or shutil.which("cswap", path=str(Path.home() / ".local" / "bin"))


def cswap_accounts():
    """The cswap accounts (number, email, active), read offline; [] without cswap."""
    exe = cswap_bin()
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "policy", "list", "--json"], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        rows = json.loads(r.stdout).get("accounts", [])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return [{"number": str(x.get("number")), "email": x.get("email"), "active": bool(x.get("active"))}
            for x in rows]


def resolve_account(want, accounts):
    for x in accounts:
        if want in (x["number"], x["email"]):
            return x
    raise Fail(f"cswap 里没有账号「{want}」。现有：" +
               ", ".join(f"{x['number']} {x['email']}" for x in accounts))


def active_account():
    """The cswap account that is live now, or None when it cannot be read."""
    return next((x for x in cswap_accounts() if x["active"]), None)


class AccountSwitch:
    """Makes `number` the live Claude login for a moment and switches back.
    Every running Claude Code session shares that login meanwhile, so this is
    kept short and serialized with a lock. cswap auto (its own lock) may
    switch in between: then the account is left where it put it, and
    `warning` says so. SIGTERM/SIGHUP (a plugin timeout) and a switch that
    failed half-way still end with the switch back."""

    def __init__(self, number):
        self.number, self.previous, self.lock, self.warning = number, None, None, None
        self.exe, self.signals, self.closing, self.killed = None, {}, False, None

    def _signal(self, signum, frame):
        if self.closing:  # already switching back: finish that first
            self.killed = signum
            return
        raise SystemExit(128 + signum)

    def __enter__(self):
        import fcntl
        import threading
        self.exe = cswap_bin()
        if not self.exe:
            raise Fail("要指定账号需要 cswap。")
        data_dir().mkdir(parents=True, exist_ok=True)
        self.lock = open(data_dir() / "account-switch.lock", "w")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        if threading.current_thread() is threading.main_thread():
            for s in (signal.SIGTERM, signal.SIGHUP):
                self.signals[s] = signal.signal(s, self._signal)
        try:
            active = active_account()
            if not active:  # without it there is nothing to switch back to
                raise Fail("读不到 cswap 当前用的是哪个账号，切过去就切不回来，所以没有切换。"
                           "先确认 cswap policy list 能正常运行。")
            if active["number"] == self.number:
                return self
            self.previous = active["number"]  # from here on, any way out switches back
            try:
                r = subprocess.run([self.exe, "switch", self.number], capture_output=True, text=True,
                                   timeout=60, stdin=subprocess.DEVNULL)
                ok, err = r.returncode == 0, (r.stderr or r.stdout).strip()[-300:]
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, err = False, str(e)[-300:]
            now = active_account()
            if not ok or not now or now["number"] != self.number:
                raise Fail(f"切换到账号 {self.number} 失败：" + err)
            return self
        except BaseException:
            self._close()
            raise

    def __exit__(self, *exc):
        self._close()
        return False

    def _close(self):
        """Switch back unless someone else moved the account meanwhile; then
        release the lock and the signal handlers."""
        self.closing = True
        try:
            if self.previous:
                now = active_account()
                if now and now["number"] == self.previous:
                    pass  # already back (or the switch never happened)
                elif now and now["number"] != self.number:
                    # cswap auto (or someone) switched meanwhile: switching back would undo it
                    self.warning = (f"开会话期间当前账号被改成了 {now['number']}（多半是 cswap 自动切换），"
                                    f"所以没有切回 {self.previous}。")
                else:  # still on the borrowed account, or unreadable: switch back
                    r = subprocess.run([self.exe, "switch", self.previous], capture_output=True, text=True,
                                       timeout=60, stdin=subprocess.DEVNULL)
                    if r.returncode != 0:
                        self.warning = f"切回账号 {self.previous} 失败，请手动运行 cswap switch {self.previous}。"
        except (OSError, subprocess.TimeoutExpired):
            self.warning = f"切回账号 {self.previous} 失败，请手动运行 cswap switch {self.previous}。"
        finally:
            self.previous = None
            self.lock.close()
            for s, h in self.signals.items():
                signal.signal(s, h)
            self.signals = {}
        if self.killed:
            raise SystemExit(128 + self.killed)


def launch_cloud(cwd, prompt, timeout):
    """Run `claude --cloud` (it needs a terminal) in a detached tmux pane and
    return the session link it prints."""
    tmux, claude = shutil.which("tmux"), shutil.which("claude") or shutil.which(
        "claude", path=str(Path.home() / ".local" / "bin"))
    if not tmux or not claude:
        raise Fail("需要 tmux 和 claude 命令。")
    name = "aw-cloud-" + uuid.uuid4().hex[:8]
    # The prompt goes in as an argument, never through a shell string.
    r = subprocess.run([tmux, "new-session", "-d", "-s", name, "-x", "200", "-y", "50", "-c", cwd, "--",
                        "sh", "-c", '"$0" --cloud "$1"; sleep 120', claude, prompt],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Fail("tmux 启动失败：" + r.stderr.strip()[:200])
    screen = ""
    try:
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(1)
            cap = subprocess.run([tmux, "capture-pane", "-p", "-J", "-t", name], capture_output=True, text=True)
            if cap.returncode != 0:
                break
            screen = cap.stdout
            m = CLOUD_URL_RE.search(screen)
            if m:
                return m.group(1)
            if "trust" in screen and "folder" in screen:
                raise Fail(f"Claude Code 还没信任这个目录。先在 {cwd} 里运行一次 claude，"
                           "选「Yes, I trust this folder」，再派任务。")
    finally:
        subprocess.run([tmux, "kill-session", "-t", name], capture_output=True)
    tail = "\n".join(x for x in screen.splitlines() if x.strip())[-400:]
    raise Fail("没拿到云端会话链接。终端最后显示：" + (tail or "（空）"))


# ------------------------------------------------------------ the cloud desk
# A private repository for cloud tasks whose folder is not on GitHub: the
# files a task needs are copied to tasks/<id>/input/, pushed, and the cloud
# session writes tasks/<id>/output/ on its result branch.

SECRET_NAME_RE = re.compile(
    r"(^\.env(\..*)?$|\.pem$|\.key$|\.p12$|\.pfx$|\.keystore$|^id_(rsa|ed25519|ecdsa|dsa)|\.netrc$|"
    r"credential|secret|token|password|\.kdbx$|^\.npmrc$|^\.pypirc$)", re.I)
SECRET_TEXT_RE = re.compile(rb"sk-ant-[a-z]{3}|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bghp_[A-Za-z0-9]{20}|"
                            rb"\bgithub_pat_|\bAKIA[0-9A-Z]{16}\b|\bxox[bap]-")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             ".tox", "dist", "build", ".next", ".cache"}


def desk_dir():
    return data_dir() / "desk"


def private_roots():
    home = Path.home()
    roots = [home / ".ssh", home / ".gnupg", home / ".claude", home / ".aws", home / ".kube", config_dir(),
             data_dir(), Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "claude-swap",
             home / ".claude-swap-backup", home / ".config" / "gh"]
    return [os.path.realpath(r) for r in roots]


def desk_files(paths, cwd, max_bytes):
    """[(source, relative name)] for the files to upload; refuses secrets."""
    out, total, refused = [], 0, []
    blocked = private_roots()
    for raw in paths:
        src = os.path.realpath(os.path.join(cwd, os.path.expanduser(raw)))
        if not os.path.exists(src):
            raise Fail(f"文件不存在：{raw}")
        if any(src == b or src.startswith(b + os.sep) for b in blocked):
            raise Fail(f"{raw} 在凭据或配置目录里，不能上传。")
        base = os.path.dirname(src.rstrip(os.sep))
        walk = [(src, [], [])] if os.path.isfile(src) else os.walk(src)
        for root, dirs, files in walk:
            if os.path.isfile(root):
                root, files = os.path.dirname(root), [os.path.basename(root)]
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                f = os.path.join(root, name)
                rel = os.path.relpath(f, base)
                if os.path.islink(f) or not os.path.isfile(f):
                    continue
                if SECRET_NAME_RE.search(name):
                    refused.append(rel)
                    continue
                with open(f, "rb") as h:
                    if SECRET_TEXT_RE.search(h.read(2_000_000)):
                        refused.append(rel)
                        continue
                total += os.path.getsize(f)
                if total > max_bytes:
                    raise Fail(f"要上传的文件超过 {max_bytes // 1_000_000} MB，请只选需要的文件。")
                out.append((f, rel))
    if not out:
        raise Fail("没有可以上传的文件。" + (f"（拒绝了疑似含凭据的：{', '.join(refused[:10])}）" if refused else ""))
    return out, refused


def desk_ready(cfg):
    d = desk_dir()
    if not cfg.get("deskRepo") or not (d / ".git").is_dir():
        raise Fail("还没设置云端桌面仓库。在终端运行：api-worker desk-setup --repo git@github-cloud-desk:<你>/<仓库>.git")
    return str(d)


def desk_branch(d):
    r = git(d, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return r.stdout.strip().split("/", 1)[1] if r.returncode == 0 and "/" in r.stdout else "main"


def desk_stage(tid, prompt, files, origin_cwd):
    """Commit the task's inputs to the desk repo and push them."""
    cfg = load_config()
    d = desk_ready(cfg)
    staged, refused = desk_files(files or [], origin_cwd, int(float(cfg["deskMaxMb"]) * 1_000_000)) \
        if files else ([], [])
    main = desk_branch(d)
    for args in (("fetch", "-q", "origin"), ("checkout", "-q", "-B", main, f"origin/{main}"),
                 ("reset", "-q", "--hard", f"origin/{main}"), ("clean", "-qfd")):
        r = git(d, *args, timeout=120)
        if r.returncode != 0 and not (args[0] == "checkout" and "origin/" in args[-1]):
            raise Fail(f"云端桌面仓库 git {args[0]} 失败：" + r.stderr.strip()[-300:])
    task_root = Path(d) / "tasks" / tid
    for src, rel in staged:
        dst = task_root / "input" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    task_root.mkdir(parents=True, exist_ok=True)
    (task_root / "TASK.md").write_text(f"# {tid}\n\nFrom `{origin_cwd}`\n\n{prompt}\n", "utf-8")
    for args in (("add", f"tasks/{tid}"), ("commit", "-qm", f"task {tid}: inputs")):
        r = git(d, *args)
        if r.returncode != 0:
            raise Fail(f"云端桌面仓库 git {args[0]} 失败：" + (r.stderr or r.stdout).strip()[-300:])
    r = git(d, "push", "-q", "origin", f"HEAD:{main}", timeout=180)
    if r.returncode != 0:
        git(d, "pull", "-q", "--rebase", "origin", main, timeout=180)
        r = git(d, "push", "-q", "origin", f"HEAD:{main}", timeout=180)
        if r.returncode != 0:
            raise Fail("推送到云端桌面仓库失败：" + r.stderr.strip()[-300:])
    return d, [rel for _, rel in staged], refused


def cmd_desk_setup(a):
    """Point api-worker at the private desk repository and clone it."""
    url = a.repo.strip()
    if not re.fullmatch(r"(git@[\w.-]+:|https://|ssh://|file://|/)[^\s]+", url):
        raise Fail("仓库地址不对，例如 git@github-cloud-desk:你的用户名/cloud-desk.git")
    d = desk_dir()
    if (d / ".git").is_dir():
        git(str(d), "remote", "set-url", "origin", url)
    else:
        d.parent.mkdir(parents=True, exist_ok=True)
        r = git(str(d.parent), "clone", "-q", url, str(d), timeout=300)
        if r.returncode != 0:
            raise Fail("克隆失败（GitHub 上加了 deploy key、勾了写权限吗？）：" + r.stderr.strip()[-300:])
    ds = str(d)
    git(ds, "config", "user.name", "api-worker")
    git(ds, "config", "user.email", "api-worker@users.noreply.github.com")
    if git(ds, "rev-parse", "HEAD").returncode != 0:  # an empty repository
        (d / "README.md").write_text("# cloud desk\n\nTasks dispatched by api-worker: `tasks/<id>/input` "
                                     "is what was sent, results come back on `claude/aw-<id>` branches.\n")
        git(ds, "checkout", "-q", "-b", "main")
        git(ds, "add", "README.md")
        git(ds, "commit", "-qm", "cloud desk")
    r = git(ds, "push", "-q", "-u", "origin", "HEAD", timeout=180)
    if r.returncode != 0:
        raise Fail("推送失败（deploy key 要勾选 Allow write access）：" + r.stderr.strip()[-300:])
    git(ds, "remote", "set-head", "origin", "--auto")
    save_config(deskRepo=url)
    print(f"云端桌面仓库准备好了：{url}\n本地副本：{d}\n"
          f"最后一步：在这个目录里运行一次 claude 并选择信任，云端会话才能从这里启动：\n  cd {d} && claude")
    return 0


def cmd_collect(a):
    """Copy a finished desk task's output/ into a local folder."""
    p = task_path(a.id)
    t = read_json(p / "task.json")
    if not t.get("desk"):
        raise Fail("只有用云端桌面仓库跑的任务需要取回文件；别的任务直接合并结果分支。")
    refresh_cloud(p, force=True)
    if read_json(p / "status.json").get("state") != "done":
        raise Fail("任务还没做完。")
    repo, branch = t["repo"], t["resultBranch"]
    ref = f"refs/remotes/origin/{branch}"
    r = git(repo, "fetch", "-q", "origin", f"+refs/heads/{branch}:{ref}", timeout=180)
    if r.returncode != 0:
        raise Fail("拉取结果分支失败：" + r.stderr.strip()[-300:])
    prefix = f"tasks/{t['id']}/output/"
    names = [n for n in git(repo, "ls-tree", "-r", "--name-only", ref, prefix).stdout.splitlines() if n]
    if not names:
        raise Fail(f"结果分支里没有 {prefix}。看看报告：api-worker result {t['id']}")
    dest = Path(os.path.realpath(os.path.expanduser(a.to or os.path.join(t["originCwd"], "cloud-results", t["id"]))))
    if dest.exists() and any(dest.iterdir()):
        raise Fail(f"{dest} 已经有东西了，换一个目录（--to）。")
    written = []
    for n in names:
        rel = n[len(prefix):]
        out = (dest / rel).resolve()
        if not str(out).startswith(str(dest) + os.sep):
            continue
        blob = git(repo, "show", f"{ref}:{n}", text=False)
        if blob.returncode != 0:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(blob.stdout)
        written.append(rel)
    print(json.dumps({"id": t["id"], "to": str(dest), "files": written}, ensure_ascii=False))
    return 0


def cmd_cloud_start(a):
    """Start a Claude Code cloud session for a task. In a git repository with a
    remote, the session is asked to push its work and a report to a branch,
    which `status`/`result` then pull back."""
    prompt = sys.stdin.read().strip()
    if not prompt:
        raise Fail("任务内容是空的。")
    cwd = os.path.realpath(os.path.expanduser(a.cwd or os.getcwd()))
    if not os.path.isdir(cwd):
        raise Fail(f"目录不存在：{cwd}")
    cfg = load_config()
    tid = "t" + uuid.uuid4().hex[:8]
    top = git(cwd, "rev-parse", "--show-toplevel")
    remote = git(cwd, "remote").stdout.split()
    own_repo = top.returncode == 0 and bool(remote)
    use_desk = bool(a.desk or a.files) or (not own_repo and bool(cfg.get("deskRepo")))
    if a.account:  # check before anything is uploaded
        account = resolve_account(a.account, cswap_accounts())
    else:
        account = active_account()
    branch = f"claude/aw-{tid}"
    title = (a.title or prompt.splitlines()[0])[:80]
    p = tasks_dir() / tid
    # A provisional record first: if this process is killed (the plugin's
    # timeout), the task still shows in `list`, as "lost" once this pid is gone.
    task = {"id": tid, "kind": "cloud", "title": title, "prompt": prompt, "cwd": cwd, "originCwd": cwd,
            "desk": use_desk, "sent": [], "repo": None, "remote": None, "resultBranch": None,
            "reportPath": None, "account": account and f"{account['number']} {account['email']}",
            "sessionUrl": None, "warnings": [], "createdAt": time.time()}
    warnings = task["warnings"]
    write_json(p / "task.json", task)
    write_json(p / "status.json", {"state": "launching", "pid": os.getpid(), "updatedAt": time.time()})
    try:
        sent = []
        if use_desk:
            launch_dir, sent, refused = desk_stage(tid, prompt, a.files, cwd)
            repo, remote_name, report = launch_dir, "origin", f"tasks/{tid}/{CLOUD_REPORT}"
            warnings += [f"疑似含凭据，没有上传：{', '.join(refused[:20])}"] if refused else []
            full = (f"{prompt}\n\n---\nThis task's files were copied from the user's machine to `tasks/{tid}/input/` "
                    f"in this repository (see `tasks/{tid}/TASK.md`). Work only inside `tasks/{tid}/`. Put everything "
                    f"you produce in `tasks/{tid}/output/`. When you are finished: write a report `{report}` (what "
                    f"you did, which output files matter, how you checked it, anything left open), commit only "
                    f"`tasks/{tid}/`, on a new branch named exactly `{branch}`, and push that branch to origin. "
                    f"Push nothing else.")
        else:
            launch_dir = cwd
            warnings += git_warnings(cwd)
            repo, remote_name, report = (top.stdout.strip(), remote[0], CLOUD_REPORT) if own_repo else (None, None, None)
            if not own_repo:
                branch = None
            full = prompt
            if branch:
                full += (f"\n\n---\nWhen you are finished: commit all your changes together with a report file "
                         f"`{CLOUD_REPORT}` at the repository root (what you did, which files changed, how you "
                         f"checked it, anything left open) on a new branch named exactly `{branch}`, and push that "
                         f"branch to origin. Push nothing else. The person who dispatched this task reads that branch.")
        # where a session that did start will report, even if this process dies now
        task.update(cwd=launch_dir, sent=sent, repo=repo, remote=remote_name, resultBranch=branch, reportPath=report)
        write_json(p / "task.json", task)
        if a.account:
            switch = AccountSwitch(account["number"])
            try:
                with switch:
                    url = launch_cloud(launch_dir, full, a.timeout)
            finally:
                if switch.warning:
                    warnings.append(switch.warning)
        else:
            url = launch_cloud(launch_dir, full, a.timeout)
    except Exception as e:
        write_json(p / "task.json", task)
        write_json(p / "status.json", {"state": "failed", "endedAt": time.time(), "updatedAt": time.time(),
                                       "error": str(e)[-500:] if isinstance(e, Fail) else type(e).__name__})
        raise
    task["sessionUrl"] = url
    write_json(p / "task.json", task)
    write_json(p / "status.json", {"launchedAt": time.time(), "updatedAt": time.time()})
    print(json.dumps({"id": tid, "title": title, "sessionUrl": url, "account": account and account["email"],
                      "desk": use_desk, "sent": sent, "resultBranch": branch, "warnings": warnings,
                      "teleport": "claude --teleport " + url.split("/code/")[1].split("?")[0]},
                     ensure_ascii=False))
    return 0


def refresh_cloud(p=None, force=False, budget=None):
    """Mark cloud tasks done once their result branch appears on the remote
    (git ls-remote: reads the remote, changes nothing here). With `budget`
    (seconds), stop asking once that much time is spent; the tasks checked
    longest ago go first, so the rest get their turn on the next call."""
    paths = [p] if p else [x for x in tasks_dir().iterdir()] if tasks_dir().is_dir() else []
    due = []
    for q in paths:
        t = read_json(q / "task.json")
        if t.get("kind") != "cloud" or not t.get("resultBranch"):
            continue
        st = read_json(q / "status.json")
        if st.get("state") in ("done", "failed"):
            continue
        if st.get("state") == "launching" and pid_alive(st.get("pid")):
            continue  # `cloud` is still starting it and owns status.json
        if not force and (time.time() - st.get("checkedAt", 0) < 120 or
                          time.time() - t.get("createdAt", 0) > 14 * 86400):
            continue
        due.append((st.get("checkedAt", 0), q, t))
    end = None if budget is None else time.monotonic() + budget
    for _, q, t in sorted(due, key=lambda x: x[0]):
        left = 30 if end is None else min(30, end - time.monotonic())
        if left < 1:
            break
        r = git(t["repo"], "ls-remote", "--heads", t["remote"], t["resultBranch"], timeout=left)
        st = read_json(q / "status.json")
        st["checkedAt"] = time.time()
        if r.returncode == 0 and r.stdout.strip():
            st.update(state="done", endedAt=time.time())
        write_json(q / "status.json", st)


def cloud_result(p):
    """Fetch a finished cloud task's branch into a remote-tracking ref (the
    working tree is not touched) and read its report and changes."""
    t = read_json(p / "task.json")
    repo, remote, branch = t["repo"], t["remote"], t["resultBranch"]
    ref = f"refs/remotes/{remote}/{branch}"
    r = git(repo, "fetch", "-q", remote, f"+refs/heads/{branch}:{ref}", timeout=120)
    if r.returncode != 0:
        return {"result": None, "error": "拉取结果分支失败：" + r.stderr.strip()[-300:]}
    report = git(repo, "show", f"{ref}:{t.get('reportPath') or CLOUD_REPORT}")
    base = git(repo, "merge-base", "HEAD", ref).stdout.strip()
    rng = f"{base}..{ref}" if base else ref
    return {"result": report.stdout if report.returncode == 0 else None,
            "commits": git(repo, "log", "--oneline", rng).stdout.strip().splitlines()[:50],
            "diffStat": git(repo, "diff", "--stat", f"{base or 'HEAD'}", ref).stdout.strip()[-3000:],
            "branchRef": f"{remote}/{branch}",
            **({"outputs": [n.split("/output/", 1)[1] for n in git(
                repo, "ls-tree", "-r", "--name-only", ref, f"tasks/{t['id']}/output/").stdout.splitlines() if n],
                "howToGet": f"api-worker collect {t['id']}  (默认复制到 {t['originCwd']}/cloud-results/{t['id']}/)"}
               if t.get("desk") else {}),
            "howToMerge": f"git -C {repo} merge {remote}/{branch}"}


def cmd_spend(a):
    rows = key_rows(load_config())
    if not rows:
        print("还没有设置 API key。")
    for r in rows:
        print(f"{r['name']}: 本月（{month_key()}）已用 ${r['spentUsd']:.2f}，进行中预留 ${r['reservedUsd']:.2f}，"
              f"上限 ${r['monthlyBudgetUsd']:.2f}，还剩 ${r['leftUsd']:.2f}")
    return 0


def save_config(**changes):
    stored = read_json(config_dir() / "config.json")
    stored.update(changes)
    write_json(config_dir() / "config.json", stored)


def cmd_remove_key(a):
    cfg = load_config()
    if a.name not in key_names(cfg):
        raise Fail(f"没有名为「{a.name}」的 key。")
    for p in (keys_dir() / a.name, config_dir() / "key" if a.name == "default" else None):
        if p is not None and p.exists():
            p.unlink()
    cmds = dict(cfg.get("keyCommands") or {})
    cmds.pop(a.name, None)
    changes = {"keyCommands": cmds}
    if a.name == "default":
        changes["keyCommand"] = ""
    save_config(**changes)
    print(f"已删除 key「{a.name}」。")
    return 0


def cmd_set_key(a):
    if not NAME_RE.fullmatch(a.name):
        raise Fail("名字只能用字母、数字、点、下划线和横线，最多 32 个字符。")
    cfg = load_config()
    if a.budget is not None:
        if a.budget <= 0:
            raise Fail("每月上限要大于 0。")
        save_config(keyBudgets={**(cfg.get("keyBudgets") or {}), a.name: float(a.budget)})
    if a.command is not None:
        save_config(keyCommands={**(cfg.get("keyCommands") or {}), a.name: a.command})
        read_key(load_config(), a.name)
        print(f"key「{a.name}」改为运行命令读取。")
        return 0
    # --budget for a stored key with no new key typed or piped (e.g. from the
    # VS Code side bar): only the cap changes.
    budget_only = a.budget is not None and a.name in key_names(cfg)
    if budget_only and sys.stdin.isatty():
        print(f"key「{a.name}」的每月上限改为 ${a.budget:.2f}。")
        return 0
    if sys.stdin.isatty():
        try:
            os.close(os.open("/dev/tty", os.O_RDWR))
        except OSError:
            raise Fail("需要在终端里运行。")
        key = getpass.getpass("粘贴 Console 里创建的 API key（sk-ant-api… 或 sk-ant-usr…，不会显示）：")
    else:
        key = sys.stdin.read(4096)
        if budget_only and not key.strip():
            print(f"key「{a.name}」的每月上限改为 ${a.budget:.2f}。")
            return 0
    key = key.strip()
    if key.startswith("sk-ant-oat"):
        raise Fail("这是订阅的 setup-token，不是 API key。要用 Console（platform.claude.com）里创建的 API key。")
    if not KEY_RE.fullmatch(key):
        raise Fail("看起来不是 API key（应该以 sk-ant-api 或 sk-ant-usr 开头）。")
    p = keys_dir() / a.name
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    fd = os.open(str(p) + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    os.replace(str(p) + ".tmp", p)
    print(f"key「{a.name}」已保存到 {p}（权限 600），每月上限 ${key_budget(load_config(), a.name):.2f}。")
    return 0


def cmd_config(a):
    cfg = load_config()
    if a.set:
        k, _, v = a.set.partition("=")
        if k not in DEFAULTS or isinstance(DEFAULTS[k], dict):
            raise Fail("可以设置：" + ", ".join(DEFAULTS))
        cur = DEFAULTS[k]
        val = float(v) if isinstance(cur, float) else int(v) if isinstance(cur, int) else v
        save_config(**{k: val})
        cfg[k] = val
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="api-worker", description="用 Console API key 在后台跑 Agent SDK 任务。")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("set-key", help="保存 API key（终端里隐藏输入，或从管道读）；可以存多个")
    p.add_argument("--name", default="default", help="key 的名字，例如账号名（默认 default）")
    p.add_argument("--budget", type=float, help="这个 key 每月最多花多少美元")
    p.add_argument("--command", help="不存文件，改为运行这条命令读取 key（例如 op read …）")
    p.set_defaults(fn=cmd_set_key)
    p = sub.add_parser("remove-key", help="删除一个 key")
    p.add_argument("name")
    p.set_defaults(fn=cmd_remove_key)
    p = sub.add_parser("start", help="派一个任务（任务内容用 -p，或从标准输入）")
    p.add_argument("-p", "--prompt")
    p.add_argument("--cwd")
    p.add_argument("--title")
    p.add_argument("--model")
    p.add_argument("--budget", type=float, help="这个任务最多花多少美元")
    p.add_argument("--key", help="用哪个 key（默认：本月剩得最多的）")
    p.add_argument("--allow-bash", action="store_true")
    p.add_argument("--allow-web", action="store_true")
    p.add_argument("--no-worktree", dest="worktree", action="store_false",
                   help="直接在目录里改，不建 git worktree")
    p.set_defaults(fn=cmd_start)
    p = sub.add_parser("run")
    p.add_argument("id")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("list", help="列出任务（JSON）")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--no-accounts", dest="accounts", action="store_false",
                   help="不列 cswap 账号（不运行 cswap）")
    p.set_defaults(fn=cmd_list)
    for name, fn in (("status", cmd_status), ("result", cmd_result), ("cancel", cmd_cancel)):
        p = sub.add_parser(name)
        p.add_argument("id")
        if name == "status":
            p.add_argument("--tail", type=int, default=8)
        p.set_defaults(fn=fn)
    p = sub.add_parser("cloud", help="开一个云端会话（任务内容从标准输入读），花云端会话额度")
    p.add_argument("--cwd")
    p.add_argument("--account", help="用 cswap 里哪个账号（编号或邮箱，默认当前账号）")
    p.add_argument("--desk", action="store_true", help="用云端桌面仓库（目录不在 GitHub 上时默认就用）")
    p.add_argument("--file", dest="files", action="append", help="要上传给云端的文件或文件夹（可重复）")
    p.add_argument("--title")
    p.add_argument("--timeout", type=float, default=90)
    p.set_defaults(fn=cmd_cloud_start)
    p = sub.add_parser("desk-setup", help="设置云端桌面仓库（私有 GitHub 仓库）")
    p.add_argument("--repo", required=True)
    p.set_defaults(fn=cmd_desk_setup)
    p = sub.add_parser("collect", help="把云端桌面任务的输出复制到本地")
    p.add_argument("id")
    p.add_argument("--to")
    p.set_defaults(fn=cmd_collect)
    p = sub.add_parser("spend", help="本月花了多少")
    p.set_defaults(fn=cmd_spend)
    p = sub.add_parser("config", help="查看或修改设置，例如 config --set monthlyBudgetUsd=150")
    p.add_argument("--set")
    p.set_defaults(fn=cmd_config)
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    try:
        return a.fn(a)
    except Fail as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False) if a.cmd not in ("set-key", "remove-key", "spend", "config", "desk-setup")
              else f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
