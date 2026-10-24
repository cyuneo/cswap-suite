#!/usr/bin/env python3
"""cswap-remote: a small, self-hosted "Remote Control" for Claude Code.

Runs on the machine where Claude Code and claude-swap live and serves a
phone-friendly web app (add it to the home screen) that can:

  * list every running Claude Code session (tmux-hosted ones are controllable,
    others such as VS Code sessions are shown read-only),
  * show a session's progress from Claude Code's own transcript, live
    (Server-Sent Events over fetch), including tool calls with their output
    and diffs, running sub-agents / workflow agents and the todo list,
  * answer permission prompts and questions with one tap (options are read
    from the tmux pane), stop a turn, switch the permission mode,
  * type into a tmux-hosted session (prompts, permission answers, keys),
  * start / stop sessions in tmux,
  * show git status and diff for a session's directory,
  * show claude-swap accounts and usage (`cswap policy list --json`) and switch,
  * optionally push a notification (ntfy) when a session needs you or is done.

Sessions are not tied to a claude.ai account: they run the unmodified
`claude` binary inside tmux, so a cswap account switch does not interrupt
them (Claude Code re-reads its credentials file when it changes).

This program never reads or stores Claude credentials or tokens. It only
reads ~/.claude/sessions/*.json (session status), transcripts (and their
subagents/, workflows/, tool-results/ folders) under ~/.claude/projects,
~/.claude/tasks/<sessionId>/*.json (task list) and ~/.claude.json's project
list.

Standard library only. Python 3.8+.
"""

import argparse
import glob
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.2.0"
HOME = Path.home()
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
CLAUDE_JSON = (
    Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    if os.environ.get("CLAUDE_CONFIG_DIR")
    else HOME / ".claude.json"
)
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "cswap-remote"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"

TMUX_PREFIX = "cr-"
TAIL_BYTES = 2_000_000
MAX_HISTORY_BYTES = 128_000_000
SNAPSHOT_EVENTS = 300
PAGE_EVENTS = 100
KEEP_EVENTS = 3000          # events a live stream remembers (for patches)
INPUT_CLIP = 4000           # per string field of a tool input
USER_CLIP = 20_000
TEXT_CLIP = 60_000
RESULT_LINES = 200
RESULT_CHARS = 20_000
PATCH_LINES = 3000
TOOL_RESULT_MAX = 5_000_000  # bytes returned by /toolresult
AGENT_TAIL_BYTES = 64 * 1024
MAX_AGENTS = 30
AGENT_STALE_MS = 30 * 60_000  # a silent background agent counts as done after this
HANDBACK_TOOL = "SubagentHandback"
MAX_STREAMS = 8
MAX_CONNECTIONS = 64        # open HTTP connections (threads) at a time
POLL_INTERVAL = 0.3         # seconds between stream polls
PING_INTERVAL = 5
STREAM_SEND_TIMEOUT = 20    # a stream write blocked this long means the client is gone
RESOLVE_INTERVAL = 2.0      # re-list sessions at least this often while streaming
PANE_INTERVAL = 1.0         # re-capture a waiting pane at most this often
PANES_STALE = 15.0          # reuse the last good `tmux list-panes` this long when it fails
MODE_WAIT = 0.35
ANSWER_SETTLE = 1.5         # after an answer key, wait this long at most for the screen to change
ANSWER_REPEAT = 3.0         # the same prompt (rawHash) is answered once within this many seconds
RESULT_LINE_MAX = 32_000_000  # longest transcript line /toolresult will parse
MAX_DIFF_BYTES = 400_000
MAX_SEND_CHARS = 20_000

# Foreground commands that mean "nobody but a shell is in this pane".
SHELLS = {"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh", "-bash", "-zsh", "-sh"}

# Keys the phone may press in a tmux pane (tmux key names).
ALLOWED_KEYS = {
    "Enter", "Escape", "Up", "Down", "Left", "Right", "Tab", "BTab",
    "C-c", "C-d", "1", "2", "3", "4", "y", "n", "Space", "BSpace",
}

# /answer choices: an option number or Escape.
ANSWER_CHOICES = {str(i) for i in range(1, 10)} | {"esc"}

# Permission modes the phone may pick (bypassPermissions is never selected).
MODES = ("default", "acceptEdits", "plan", "auto")
MODE_LABELS = {"default": "默认", "acceptEdits": "自动接受编辑", "plan": "计划", "auto": "自动",
               "bypassPermissions": "跳过权限"}
# Footer text Claude Code shows for each mode (lower-cased); none => default.
MODE_FOOTERS = (
    ("bypass permissions on", "bypassPermissions"),
    ("accept edits on", "acceptEdits"),
    ("plan mode on", "plan"),
    ("auto mode on", "auto"),
)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config():
    cfg = {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text("utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        sys.exit(f"cswap-remote: cannot read {CONFIG_FILE}: {e}")
    changed = False
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(32)
        changed = True
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 8787)
    cfg.setdefault("projects", [])
    cfg.setdefault("cswap", "")
    cfg.setdefault("claude", "")
    cfg.setdefault("ntfy", {"server": "https://ntfy.sh", "topic": ""})
    if changed or not CONFIG_FILE.exists():
        save_config(cfg)
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, CONFIG_FILE)


def find_binary(configured, name, extra=()):
    for cand in [configured, *extra, shutil.which(name)]:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def run(cmd, timeout=15, input_text=None):
    """Run a command without a shell; return (code, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, stdin=None if input_text is not None else subprocess.DEVNULL,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def proc_start_ticks(pid):
    """Field 22 of /proc/<pid>/stat (start time in clock ticks), or None."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm (field 2) may contain spaces/parens: split after the last ')'.
    rest = data[data.rfind(")") + 2:].split()
    try:
        return int(rest[19])
    except (IndexError, ValueError):
        return None


def parent_pid(pid):
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
        return int(data[data.rfind(")") + 2:].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def ancestors(pid, limit=30):
    out = []
    while pid and pid > 1 and len(out) < limit:
        out.append(pid)
        pid = parent_pid(pid)
    return out


def short_home(p):
    s = str(p)
    h = str(HOME)
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


# --------------------------------------------------------------------------
# tmux
# --------------------------------------------------------------------------

class Tmux:
    def __init__(self):
        self.bin = shutil.which("tmux")
        self._last = (0.0, [])  # (monotonic time, panes) of the last good listing

    def available(self):
        return bool(self.bin)

    def panes(self):
        """All panes: list of dicts (first pane of each session first).

        When tmux fails or times out (not merely "no server running"), the
        last good listing is reused for PANES_STALE seconds, so one slow
        call does not re-key every tmux session (t:%N -> c:<pid>).
        """
        if not self.bin:
            return []
        fmt = "\t".join([
            "#{session_name}", "#{pane_id}", "#{pane_pid}", "#{pane_current_path}",
            "#{pane_current_command}", "#{session_attached}", "#{window_index}", "#{pane_index}",
        ])
        code, out, err = run([self.bin, "list-panes", "-a", "-F", fmt], timeout=5)
        if code != 0:
            if "no server running" in err or "error connecting" in err:
                self._last = (time.monotonic(), [])
                return []
            at, last = self._last
            return list(last) if time.monotonic() - at < PANES_STALE else []
        panes = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 8:
                continue
            try:
                pid = int(parts[2])
            except ValueError:
                continue
            panes.append({
                "session": parts[0], "pane": parts[1], "pid": pid, "cwd": parts[3],
                "command": parts[4], "attached": parts[5] not in ("", "0"),
                "order": (parts[6], parts[7]),
            })
        self._last = (time.monotonic(), panes)
        return panes

    def capture(self, pane, lines=200):
        """Pane text with `lines` of scrollback, or only the visible screen
        when lines is None (what prompts and the mode footer are read from)."""
        cmd = [self.bin, "capture-pane", "-p", "-J", "-t", pane]
        if lines is not None:
            cmd += ["-S", f"-{lines}"]
        code, out, err = run(cmd, timeout=5)
        if code != 0:
            raise ApiError(HTTPStatus.BAD_GATEWAY, err.strip() or "capture failed")
        return out.rstrip("\n")

    def send_text(self, pane, text, submit=True):
        # Bracketed paste keeps multi-line text as one prompt instead of
        # submitting at the first newline.
        buf = f"cswap-remote-{secrets.token_hex(4)}"
        code, _, err = run([self.bin, "load-buffer", "-b", buf, "-"], input_text=text, timeout=5)
        if code != 0:
            raise ApiError(HTTPStatus.BAD_GATEWAY, err.strip() or "load-buffer failed")
        code, _, err = run([self.bin, "paste-buffer", "-p", "-d", "-b", buf, "-t", pane], timeout=5)
        if code != 0:
            raise ApiError(HTTPStatus.BAD_GATEWAY, err.strip() or "paste failed")
        if submit:
            time.sleep(0.15)
            self.send_key(pane, "Enter")

    def send_key(self, pane, key):
        code, _, err = run([self.bin, "send-keys", "-t", pane, key], timeout=5)
        if code != 0:
            raise ApiError(HTTPStatus.BAD_GATEWAY, err.strip() or "send-keys failed")

    def new_session(self, name, cwd):
        """Start a detached session running the user's shell; return its pane id."""
        code, out, err = run([self.bin, "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name,
                              "-c", cwd, "-x", "120", "-y", "40"], timeout=10)
        pane = out.strip()
        if code != 0 or not pane.startswith("%"):
            raise ApiError(HTTPStatus.BAD_GATEWAY, err.strip() or "tmux new-session failed")
        time.sleep(0.4)  # let the login shell print its prompt
        return pane

    def type_line(self, pane, line):
        code, _, err = run([self.bin, "send-keys", "-t", pane, "-l", line], timeout=5)
        if code == 0:
            code, _, err = run([self.bin, "send-keys", "-t", pane, "Enter"], timeout=5)
        if code != 0:
            raise ApiError(HTTPStatus.BAD_GATEWAY, err.strip() or "send-keys failed")

    def kill_session(self, name):
        code, _, err = run([self.bin, "kill-session", "-t", f"={name}"], timeout=5)
        if code != 0:
            raise ApiError(HTTPStatus.BAD_GATEWAY, err.strip() or "kill-session failed")


# --------------------------------------------------------------------------
# Claude Code sessions
# --------------------------------------------------------------------------

def live_claude_sessions():
    """Running Claude Code processes from ~/.claude/sessions/<pid>.json.

    Only the *.json status files are read (never the *.key files next to
    them). A file counts as live when its pid is running and, when the file
    records it, the process start time still matches (pid reuse guard).
    """
    out = []
    for f in glob.glob(str(CLAUDE_DIR / "sessions" / "*.json")):
        try:
            d = json.loads(Path(f).read_text("utf-8"))
        except (OSError, ValueError):
            continue
        pid = d.get("pid") if isinstance(d, dict) else None
        if not isinstance(pid, int):
            continue
        ticks = proc_start_ticks(pid)
        if ticks is None:
            continue
        recorded = d.get("procStart")
        if recorded is not None and str(recorded).isdigit() and int(recorded) != ticks:
            continue
        out.append(d)
    return out


_CONV_CACHE = {}


def conversation_info(path):
    """Title / cwd of a transcript from its head and tail (cached by mtime
    and size): customTitle > aiTitle > lastPrompt, like the VS Code list."""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _CONV_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    chunks = []
    try:
        with open(path, "rb") as f:
            chunks.append(f.read(256_000))
            if st.st_size > 512_000:
                f.seek(st.st_size - 512_000)
                f.readline()
            chunks.append(f.read())
    except OSError:
        return {}
    info = {"title": None, "cwd": None, "hasUser": False}
    custom = ai = last = first = None
    for chunk in chunks:
        for line in chunk.split(b"\n"):
            if not line.strip():
                continue
            d = _parse_line(line)
            if not d:
                continue
            t = d.get("type")
            if t == "custom-title" and isinstance(d.get("customTitle"), str):
                custom = d["customTitle"]
            elif t == "ai-title" and isinstance(d.get("aiTitle"), str):
                ai = d["aiTitle"]
            elif t == "last-prompt" and isinstance(d.get("lastPrompt"), str) and d["lastPrompt"].strip():
                last = d["lastPrompt"]
            elif t in ("user", "assistant") and not d.get("isSidechain"):
                if isinstance(d.get("cwd"), str):
                    info["cwd"] = d["cwd"]
                if t == "user" and not d.get("isMeta"):
                    info["hasUser"] = True
                    c = (d.get("message") or {}).get("content")
                    if first is None and isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                        first = c
    title = custom or ai
    last = last or first
    if not title and last:
        t = " ".join(last.split())
        title = t if len(t) <= 60 else t[:60] + "…"
    info["title"] = title
    if len(_CONV_CACHE) > 500:
        _CONV_CACHE.clear()
    _CONV_CACHE[path] = (key, info)
    return info


def transcript_path(session_id):
    if not session_id or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
        return None
    matches = glob.glob(str(CLAUDE_DIR / "projects" / "*" / f"{session_id}.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda p: os.path.getmtime(p))


def session_dir_of(transcript):
    """<projects>/<enc>/<sessionId>: where subagents/, workflows/ and
    tool-results/ of a transcript live."""
    return transcript[:-len(".jsonl")] if transcript.endswith(".jsonl") else transcript


def _inside(path, root):
    """True when path (after resolving links) lies inside directory root."""
    try:
        rp, rr = os.path.realpath(path), os.path.realpath(root)
    except (OSError, ValueError):
        return False
    return rp.startswith(rr.rstrip(os.sep) + os.sep)


def _clip(s, n):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n})"


def _clip_input(v, depth=0):
    """A tool input with every string clipped to INPUT_CLIP characters."""
    if isinstance(v, str):
        return v if len(v) <= INPUT_CLIP else v[:INPUT_CLIP] + "…"
    if depth >= 6:
        return None
    if isinstance(v, dict):
        return {str(k): _clip_input(x, depth + 1) for k, x in list(v.items())[:100]}
    if isinstance(v, list):
        return [_clip_input(x, depth + 1) for x in v[:200]]
    return v if isinstance(v, (int, float, bool)) or v is None else None


def _clip_output(s):
    """(text, truncated): at most RESULT_LINES lines / RESULT_CHARS chars."""
    if not isinstance(s, str):
        return "", False
    truncated = False
    lines = s.split("\n")
    if len(lines) > RESULT_LINES:
        s = "\n".join(lines[:RESULT_LINES])
        truncated = True
    if len(s) > RESULT_CHARS:
        s = s[:RESULT_CHARS]
        truncated = True
    return s, truncated


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, dict) and b.get("type") == "image":
                parts.append("[图片]")
        return "\n".join(parts)
    return ""


def _patch_of(tur):
    """(hunks, truncated) from an Edit/Write/MultiEdit toolUseResult."""
    hunks, budget, truncated = [], PATCH_LINES, False
    sp = tur.get("structuredPatch")
    if isinstance(sp, list) and sp:
        for h in sp:
            if not isinstance(h, dict):
                continue
            if budget <= 0:
                truncated = True
                break
            lines = [x[:2000] for x in (h.get("lines") or []) if isinstance(x, str)]
            if len(lines) > budget:
                lines, truncated = lines[:budget], True
            budget -= len(lines)
            hunks.append({"oldStart": _int(h.get("oldStart")), "oldLines": _int(h.get("oldLines")),
                          "newStart": _int(h.get("newStart")), "newLines": _int(h.get("newLines")),
                          "lines": lines})
    elif tur.get("type") == "create" and isinstance(tur.get("content"), str):
        # A new file has no structuredPatch: show it as one all-added hunk.
        lines = tur["content"].split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        n = len(lines)
        if n > budget:
            lines, truncated = lines[:budget], True
        hunks.append({"oldStart": 0, "oldLines": 0, "newStart": 1, "newLines": n,
                      "lines": ["+" + x[:2000] for x in lines]})
    return hunks, truncated


def _tool_result(block, tur):
    text, truncated = _clip_output(_result_text(block.get("content")))
    res = {"text": text, "stdout": None, "stderr": None, "interrupted": False,
           "filePath": None, "patch": [], "truncated": truncated}
    if isinstance(tur, dict):
        for k in ("stdout", "stderr"):
            if isinstance(tur.get(k), str):
                res[k], t = _clip_output(tur[k])
                res["truncated"] = res["truncated"] or t
        res["interrupted"] = bool(tur.get("interrupted"))
        if isinstance(tur.get("filePath"), str):
            res["filePath"] = tur["filePath"]
        res["patch"], t = _patch_of(tur)
        res["truncated"] = res["truncated"] or t
        if isinstance(tur.get("agentId"), str):
            res["agentId"] = tur["agentId"]
    return res


def _user_text(s):
    """(kind, text) for one piece of user text, or None to hide it."""
    if not isinstance(s, str) or not s.strip():
        return None
    st = s.lstrip()
    if st.startswith("<local-command-") or st.startswith("<bash-stdout>") or st.startswith("<bash-stderr>"):
        return None
    if "<command-name>" in st:
        m = re.search(r"<command-name>(.*?)</command-name>", st, re.S)
        if not m:
            return None
        name = m.group(1).strip()
        name = name if name.startswith("/") else "/" + name
        a = re.search(r"<command-args>(.*?)</command-args>", st, re.S)
        args = a.group(1).strip() if a else ""
        return "user", _clip(name + (" " + args if args else ""), USER_CLIP)
    if st.startswith("<bash-input>"):
        m = re.search(r"<bash-input>(.*?)</bash-input>", st, re.S)
        return ("user", "! " + _clip(m.group(1), USER_CLIP)) if m else None
    if st.startswith("<task-notification>"):
        m = re.search(r"<summary>(.*?)</summary>", st, re.S)
        return "system", "后台任务：" + _clip(m.group(1).strip(), 300) if m else "后台任务有更新"
    if st.startswith("[Request interrupted by user"):
        return "system", "已中断"
    return "user", _clip(s, USER_CLIP)


def _norm_todos(todos):
    out = []
    for t in todos if isinstance(todos, list) else []:
        if not isinstance(t, dict):
            continue
        status = t.get("status") if t.get("status") in ("pending", "in_progress", "completed") else "pending"
        out.append({"content": _clip(str(t.get("content") or t.get("subject") or ""), 500), "status": status,
                    "activeForm": _clip(t["activeForm"], 500) if isinstance(t.get("activeForm"), str) else None})
    return out


# Text that starts with an XML-ish tag is generated by Claude Code, not typed.
_MACHINE_RE = re.compile(r"\s*<[a-z][\w-]*[\s>]")


class Normalizer:
    """Turns transcript records into the v0.2 event objects.

    Events have stable ids (record uuid + block index, tool_use id for
    tools, byte offset for records without uuid) so a live stream can send
    a later tool_result as a patch of the tool event. feed() returns a list
    of ("event" | "patch", event). Thinking text is never kept.
    """

    def __init__(self, sidechain=False, keep=KEEP_EVENTS):
        self.sidechain = sidechain  # True for a subagent's own transcript
        self.keep = keep
        self.events = []
        self.by_id = {}
        self.open_tools = {}  # tool_use id -> event still waiting for its result
        self.tool_msg = {}    # open tool_use id -> id of the assistant message it came in
        self.queued = []
        self.title = None
        self.mode = None
        self.todos = None

    def pending_tool(self):
        """The oldest tool_use of the newest turn that has no result yet, or None."""
        return next(iter(self.pending_tools()), None)

    def pending_tools(self):
        """Open tool_uses (no tool_result yet) of the newest assistant
        message that has any, oldest first. Parallel calls share one
        message and Claude Code asks about them in order; older open calls
        (e.g. from a turn that was killed) are left out."""
        tools = list(self.open_tools.values())
        if not tools:
            return []
        last = self.tool_msg.get(tools[-1]["id"])
        if last is None:
            return [tools[-1]]
        return [t for t in tools if self.tool_msg.get(t["id"]) == last]

    def _add(self, ev, out):
        if ev["id"] in self.by_id:  # duplicated record (e.g. after a resume)
            return
        self.events.append(ev)
        self.by_id[ev["id"]] = ev
        out.append(("event", ev))
        if len(self.events) > self.keep + self.keep // 5:
            for old in self.events[:len(self.events) - self.keep]:
                self.by_id.pop(old["id"], None)
                self.open_tools.pop(old["id"], None)
                self.tool_msg.pop(old["id"], None)
            self.events = self.events[-self.keep:]

    def feed(self, d, offset=0):
        out = []
        if not isinstance(d, dict):
            return out
        t = d.get("type")
        ts = d.get("timestamp")
        if t in ("ai-title", "custom-title"):
            v = d.get("aiTitle") or d.get("customTitle")
            if isinstance(v, str) and v:
                self.title = v
            return out
        if t == "queue-operation":
            self._queue(d, offset, out)
            return out
        if d.get("isSidechain") and not self.sidechain:
            return out
        uid = d.get("uuid") if isinstance(d.get("uuid"), str) else f"o{offset}"
        if t == "system":
            st = d.get("subtype")
            text = {"compact_boundary": "对话已压缩", "api_error": "API 请求出错，正在重试…"}.get(st)
            if text is None and st == "informational" and isinstance(d.get("content"), str):
                text = _clip(d["content"], 2000)
            if text:
                self._add({"id": uid, "kind": "system", "ts": ts, "text": text}, out)
            return out
        if t not in ("user", "assistant") or d.get("isMeta") or d.get("isCompactSummary"):
            return out
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        content = msg.get("content")
        if t == "user":
            if isinstance(d.get("permissionMode"), str):
                self.mode = d["permissionMode"]
            pieces = [content] if isinstance(content, str) else []
            results = []
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    pieces.append(b.get("text"))
                elif b.get("type") == "image":
                    pieces.append("[图片]")
                elif b.get("type") == "tool_result":
                    results.append(b)
            shown = [x for x in (_user_text(p) for p in pieces) if x]
            if shown:
                kind = "system" if all(k == "system" for k, _ in shown) else "user"
                text = "\n".join(x for k, x in shown if k == kind)
                self._add({"id": uid, "kind": kind, "ts": ts, "text": text}, out)
            tur = d.get("toolUseResult") if len(results) == 1 else None
            for b in results:
                self._result(b, tur, out)
            return out
        for i, b in enumerate(content if isinstance(content, list) else []):
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                self._add({"id": f"{uid}:{i}", "kind": "text", "ts": ts, "text": _clip(b["text"], TEXT_CLIP)}, out)
            elif bt in ("thinking", "redacted_thinking"):
                self._add({"id": f"{uid}:{i}", "kind": "thinking", "ts": ts}, out)
            elif bt == "tool_use" and isinstance(b.get("id"), str):
                name = str(b.get("name") or "?")
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                ev = {"id": b["id"], "kind": "tool", "ts": ts, "name": name, "input": _clip_input(inp),
                      "status": "running", "result": None}
                if b["id"] not in self.by_id:
                    self.open_tools[b["id"]] = ev
                    mid = msg.get("id") or d.get("requestId") or uid
                    self.tool_msg[b["id"]] = mid if isinstance(mid, str) else uid
                self._add(ev, out)
                if name == "TodoWrite" and isinstance(inp.get("todos"), list):
                    self.todos = _norm_todos(inp["todos"])
        return out

    def _result(self, block, tur, out):
        tid = block.get("tool_use_id")
        self.open_tools.pop(tid, None)
        self.tool_msg.pop(tid, None)
        ev = self.by_id.get(tid)
        if not ev or ev.get("kind") != "tool":
            return
        ev["status"] = "error" if block.get("is_error") else "ok"
        ev["result"] = _tool_result(block, tur)
        out.append(("patch", ev))

    def _queue(self, d, offset, out):
        op, text = d.get("operation"), d.get("content")
        if op == "enqueue" and isinstance(text, str) and text.strip():
            qid, text = f"q{offset}", _clip(text, USER_CLIP)
            # Machine messages (task notifications, messages from other
            # sessions, ...) are queued too: track them so dequeues stay in
            # step, but only show what a person typed.
            shown = _user_text(text)
            if shown and shown[0] == "user" and not _MACHINE_RE.match(text):
                self._add({"id": qid, "kind": "queued", "ts": d.get("timestamp"), "text": text}, out)
            self.queued.append((qid, text))
        elif op in ("dequeue", "remove") and self.queued:
            entry = self.queued[0]
            if isinstance(text, str):
                entry = next((q for q in self.queued if q[1] == _clip(text, USER_CLIP)), entry)
            self.queued.remove(entry)
            ev = self.by_id.get(entry[0])
            if ev:
                ev["done"] = True  # delivered or dropped: the UI hides the chip
                out.append(("patch", ev))


def _parse_line(line):
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def read_records(path, start=0, end=None):
    """[(offset, record)] for the complete lines of path from byte `start`
    (a partial first line is skipped) up to `end`."""
    out = []
    with open(path, "rb") as f:
        if start > 0:
            f.seek(start - 1)
            data = f.read((end - start + 1) if end is not None else -1)
            nl = data.find(b"\n")
            if nl < 0:
                return out
            pos, base = nl + 1, start - 1
        else:
            data = f.read(end if end is not None else -1)
            pos, base = 0, 0
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        d = _parse_line(data[pos:nl])
        if d is not None:
            out.append((base + pos, d))
        pos = nl + 1
    return out


class TranscriptTail:
    """Follows a .jsonl file by byte offset.

    open() returns the records of the last `start_bytes`; read_new() returns
    records appended since (a line is only returned once its newline has
    been written), or None when the file was replaced or shrank, in which
    case the caller re-opens and starts over.
    """

    def __init__(self, path, start_bytes=TAIL_BYTES):
        self.path = path
        self.start_bytes = start_bytes
        self.ino = None
        self.start = 0    # offset of the first byte considered
        self.offset = 0   # bytes consumed (complete lines + self.buf)
        self.buf = b""
        self._skip = False

    def open(self):
        st = os.stat(self.path)
        self.ino = st.st_ino
        self.start = max(0, st.st_size - self.start_bytes)
        self.offset, self.buf, self._skip = self.start, b"", False
        if self.start > 0:
            with open(self.path, "rb") as f:
                f.seek(self.start - 1)
                self._skip = f.read(1) != b"\n"  # started mid-line
        return self._consume(st.st_size)

    def read_new(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        if st.st_ino != self.ino or st.st_size < self.offset:
            return None
        if st.st_size == self.offset:
            return []
        return self._consume(st.st_size)

    def _consume(self, size):
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        base = self.offset - len(self.buf)
        self.offset += len(data)
        data = self.buf + data
        out, pos = [], 0
        if self._skip:
            nl = data.find(b"\n")
            if nl < 0:
                self.buf = data
                return out
            pos, self._skip = nl + 1, False
        while True:
            nl = data.find(b"\n", pos)
            if nl < 0:
                break
            d = _parse_line(data[pos:nl])
            if d is not None:
                out.append((base + pos, d))
            pos = nl + 1
        self.buf = data[pos:]
        return out


def find_last(path, needle, end=None, limit=MAX_HISTORY_BYTES):
    """Offset of the last occurrence of bytes `needle` that ends at or
    before `end` (default: the end of the file), searching backwards at
    most `limit` bytes in 4 MB chunks; None when not found."""
    size = os.path.getsize(path) if end is None else end
    floor = max(0, size - limit)
    chunk = 4_000_000
    with open(path, "rb") as f:
        hi = size
        while hi > floor:
            lo = max(floor, hi - chunk)
            f.seek(lo)
            data = f.read(min(size, hi + len(needle) - 1) - lo)
            i = data.rfind(needle)
            if i >= 0:
                return lo + i
            hi = lo
    return None


def _line_end(path, pos, size, limit=RESULT_LINE_MAX):
    """Offset just past the newline that ends the line holding byte `pos`
    (looks at most `limit` bytes ahead; returns size when there is none)."""
    with open(path, "rb") as f:
        f.seek(pos)
        p = pos
        while p < size and p - pos < limit:
            data = f.read(min(1_000_000, size - p))
            if not data:
                break
            i = data.find(b"\n")
            if i >= 0:
                return p + i + 1
            p += len(data)
    return size


def _line_start(path, pos):
    """Offset of the first byte of the line holding byte `pos`."""
    nl = find_last(path, b"\n", pos, RESULT_LINE_MAX)
    return 0 if nl is None else nl + 1


def _event_offset(path, size, event_id):
    """A byte offset at or after the record that produced event `event_id`
    (ids carry an offset, a record uuid or a tool_use id), or None."""
    m = re.fullmatch(r"[oq](\d{1,15})", event_id)
    if m:
        off = int(m.group(1))
        return off if off < size else None
    base = event_id.split(":", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", base):
        return None
    # The last mention may be a later record (a child's parentUuid, the
    # tool_result): that is after the record we want, which is all we need.
    return find_last(path, b'"' + base.encode() + b'"', size)


def load_page(path, before=None, limit=PAGE_EVENTS, sidechain=False):
    """`limit` events ending just before the event with id
    `before` (or the newest ones).

    For `before`, the record is first located by a bounded byte search
    (an unknown id returns an empty page at once). Records are normalized
    from a window that grows backwards from there, up to a margin past it
    (so results that follow their tool calls closely are merged).

    Returns (events, more, normalizer); the normalizer (None for an empty
    page) has read to the end of the file when `before` is None."""
    size = os.path.getsize(path)
    if before is None:
        anchor, stop = size, size
    else:
        anchor = _event_offset(path, size, str(before))
        if anchor is None:
            return [], False, None
        stop = min(size, _line_end(path, anchor, size) + TAIL_BYTES)
    window = TAIL_BYTES
    while True:
        start = max(0, anchor - window)
        norm = Normalizer(sidechain, keep=10 ** 9)
        for off, rec in read_records(path, start, stop):
            norm.feed(rec, off)
        evs = norm.events
        if before is None:
            end = len(evs)
        else:
            end = next((i for i, e in enumerate(evs) if e["id"] == before), None)
            if end is None:  # the id is not an event of this transcript
                return [], False, None
        if end >= limit or start == 0:
            return evs[max(0, end - limit):end], end > limit or start > 0, norm
        if window >= MAX_HISTORY_BYTES:
            return evs[max(0, end - limit):end], end > 0, norm
        window *= 4


def load_events(path, before=None, limit=PAGE_EVENTS, sidechain=False):
    """(events, more): see load_page()."""
    return load_page(path, before, limit, sidechain)[:2]


_RE_MODE = re.compile(rb'"permissionMode"\s*:\s*"(\w+)"')
_RE_RESULT_ID = re.compile(rb'"tool_use_id"\s*:\s*"([A-Za-z0-9_-]+)"')
_RE_IS_ERROR = re.compile(rb'"is_error"\s*:\s*true')
_RE_SIDECHAIN = re.compile(rb'"isSidechain"\s*:\s*true')
_RE_NOTE = re.compile(rb"<tool-use-id>([A-Za-z0-9_-]+)</tool-use-id>.{0,400}?<status>(\w+)</status>", re.S)


class TranscriptIndex:
    """Facts that need the whole transcript, kept current by scanning only
    appended bytes: title, permission mode, finished tool_use ids (for
    sub-agent status), background task notifications and the last TodoWrite.
    """

    def __init__(self, path):
        self.path = path
        self.ino = None
        self._reset()

    def _reset(self):
        self.offset = 0
        self.ai_title = self.custom_title = self.last_prompt = None
        self.mode = None
        self.mode_version = 0
        self.results = {}  # tool_use id -> is_error
        self.notes = {}    # tool_use id -> task-notification status
        self.todos = None

    @property
    def title(self):
        # Same order as the VS Code extension's session list: a rename, then
        # Claude's generated title, then the last prompt.
        if self.custom_title or self.ai_title:
            return self.custom_title or self.ai_title
        if self.last_prompt:
            t = " ".join(self.last_prompt.split())
            return t if len(t) <= 60 else t[:60] + "…"
        return None

    def update(self):
        st = os.stat(self.path)
        if st.st_ino != self.ino or st.st_size < self.offset:
            self._reset()
            self.ino = st.st_ino
        if st.st_size == self.offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            carry = b""  # unscanned bytes; they start at self.offset
            while True:
                chunk = f.read(4_000_000)
                if not chunk:
                    break
                data = carry + chunk
                last = data.rfind(b"\n")
                if last < 0:
                    carry = data
                    continue
                for line in data[:last].split(b"\n"):
                    self._scan(line)
                self.offset += last + 1
                carry = data[last + 1:]

    def _scan(self, line):
        if b'"last-prompt"' in line:
            d = _parse_line(line)
            if d and d.get("type") == "last-prompt" and isinstance(d.get("lastPrompt"), str) and d["lastPrompt"].strip():
                self.last_prompt = d["lastPrompt"]
        if b'"ai-title"' in line or b'"custom-title"' in line:
            d = _parse_line(line)
            if d and d.get("type") == "ai-title" and isinstance(d.get("aiTitle"), str):
                self.ai_title = d["aiTitle"]
            elif d and d.get("type") == "custom-title" and isinstance(d.get("customTitle"), str):
                self.custom_title = d["customTitle"]
        if b'"permissionMode"' in line and not _RE_SIDECHAIN.search(line):
            m = _RE_MODE.search(line)
            if m:
                mode = m.group(1).decode()
                if mode != self.mode:
                    self.mode = mode
                    self.mode_version += 1
        if b'"tool_result"' in line:
            err = bool(_RE_IS_ERROR.search(line))
            for m in _RE_RESULT_ID.finditer(line):
                self.results[m.group(1).decode()] = err
        if b"<task-notification>" in line:
            for m in _RE_NOTE.finditer(line):
                self.notes[m.group(1).decode()] = m.group(2).decode()
        if b'"TodoWrite"' in line and not _RE_SIDECHAIN.search(line):
            d = _parse_line(line)
            msg = (d or {}).get("message")
            for b in (msg.get("content") if isinstance(msg, dict) else None) or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "TodoWrite":
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if isinstance(inp.get("todos"), list):
                        self.todos = _norm_todos(inp["todos"])


_INDEX_LOCK = threading.Lock()
_INDEXES = {}


def transcript_index(path):
    """The up-to-date TranscriptIndex of path (shared, cached)."""
    with _INDEX_LOCK:
        idx = _INDEXES.get(path)
        if idx is None:
            if len(_INDEXES) > 200:
                _INDEXES.clear()
            idx = _INDEXES[path] = TranscriptIndex(path)
        try:
            idx.update()
        except OSError:
            pass
        return idx


# ---- sub-agents -----------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_AGENT_TAILS = {}  # path -> ((mtime_ns, size), (lastAction, finished))
_JOURNALS = {}     # path -> ((mtime_ns, size), {agentId: info})


def _action_summary(name, inp):
    if isinstance(inp, dict):
        for key in ("command", "file_path", "pattern", "description"):
            v = inp.get(key)
            if isinstance(v, str) and v.strip():
                return _clip(f"{name} · {' '.join(v.split())}", 120)
    return _clip(str(name), 120)


def agent_tail(path):
    """(lastAction, finished) from the last 64KB of an agent transcript."""
    try:
        st = os.stat(path)
    except OSError:
        return None, False
    sig = (st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        hit = _AGENT_TAILS.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        recs = read_records(path, max(0, st.st_size - AGENT_TAIL_BYTES), st.st_size)
    except OSError:
        recs = []
    action, finished, last_seen = None, False, False
    for _, d in reversed(recs):
        if d.get("type") not in ("user", "assistant"):
            continue
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        blocks = [b for b in msg.get("content") or [] if isinstance(b, dict)] if isinstance(msg.get("content"), list) else []
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if d["type"] == "assistant" and not last_seen:
            # The newest assistant message decides: a final answer, or the
            # hand-back call a background agent ends with.
            last_seen = True
            # (Records hold one block each, so a lone thinking block is not an end.)
            finished = any(b.get("name") == HANDBACK_TOOL for b in uses) or (
                not uses and any(b.get("type") == "text" for b in blocks))
        if d["type"] == "assistant":
            for b in reversed(blocks):
                if b.get("type") == "tool_use" and b.get("name") == HANDBACK_TOOL:
                    continue
                if b.get("type") == "tool_use":
                    action = _action_summary(b.get("name") or "?", b.get("input"))
                elif b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                    action = " ".join(b["text"].split())[:120]
                if action:
                    break
        if action:
            break
    with _CACHE_LOCK:
        if len(_AGENT_TAILS) > 2000:
            _AGENT_TAILS.clear()
        _AGENT_TAILS[path] = (sig, (action, finished))
    return action, finished


def _journal(path):
    """{agentId: {label, phase, status|None}} from a workflow journal.jsonl."""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        hit = _JOURNALS.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    agents = {}
    try:
        recs = read_records(path)
    except OSError:
        recs = []
    for _, d in recs:
        aid = d.get("agentId")
        if not isinstance(aid, str):
            continue
        if d.get("type") == "started":
            agents[aid] = {"label": d.get("label") if isinstance(d.get("label"), str) else None,
                           "phase": d.get("phase") if isinstance(d.get("phase"), str) else None,
                           "status": None}
        elif d.get("type") in ("result", "failed") and aid in agents:
            agents[aid]["status"] = "done" if d["type"] == "result" else "failed"
    with _CACHE_LOCK:
        if len(_JOURNALS) > 500:
            _JOURNALS.clear()
        _JOURNALS[path] = (sig, agents)
    return agents


def _mtime_ms(path):
    try:
        return int(os.stat(path).st_mtime * 1000)
    except OSError:
        return None


def discover_agents(transcript, index=None, full=True, alive=True):
    """(agents, running) for a session transcript.

    Task agents: <session>/subagents/agent-*.jsonl (+ .meta.json). A
    foreground one runs until its tool_use has a tool_result in the parent
    transcript; a background one until its task-notification arrives (or,
    lacking that, until its own transcript ends with a final answer).
    Workflow agents: <session>/subagents/workflows/wf_*/journal.jsonl; a run
    is live while <session>/workflows/<run>.json does not exist.

    A foreground one whose transcript has been silent for AGENT_STALE_MS
    counts as done too (claude may have died mid-Task, so no result will
    come). With alive=False (the session's claude is gone) nothing is
    running any more: what was still running counts as failed.

    With full=False only running agents are collected (cheap; for the list).
    Items carry a private "_path" key, removed by public_agents().
    """
    index = index or transcript_index(transcript)
    sdir = session_dir_of(transcript)
    sub = os.path.join(sdir, "subagents")
    agents = []
    for jl in glob.glob(os.path.join(glob.escape(sub), "agent-*.jsonl")):
        aid = os.path.basename(jl)[len("agent-"):-len(".jsonl")]
        meta = {}
        try:
            meta = json.loads(Path(jl[:-len(".jsonl")] + ".meta.json").read_text("utf-8"))
        except (OSError, ValueError):
            pass
        meta = meta if isinstance(meta, dict) else {}
        tuid = meta.get("toolUseId") if isinstance(meta.get("toolUseId"), str) else None
        note = index.notes.get(tuid) if tuid else None
        if note:
            status = "done" if note == "completed" else "failed"
        elif tuid and tuid in index.results and meta.get("requestShape") != "background":
            status = "failed" if index.results[tuid] else "done"
        elif tuid and meta.get("requestShape") != "background":
            quiet = time.time() * 1000 - (_mtime_ms(jl) or 0)
            status = "done" if quiet > AGENT_STALE_MS else "running"
        else:
            # Background (or no meta): finished once its transcript ends with
            # a final answer / hand-back and has been quiet for a while, or
            # after a long silence (its notification is not always logged).
            quiet = time.time() * 1000 - (_mtime_ms(jl) or 0)
            done = (quiet > 30_000 and agent_tail(jl)[1]) or quiet > AGENT_STALE_MS
            status = "done" if done else "running"
        if status == "running" and not alive:
            status = "failed"
        if not full and status != "running":
            continue
        agents.append({"id": aid, "kind": "task",
                       "label": _clip(str(meta.get("description") or meta.get("agentType") or "智能体"), 200),
                       "phase": None, "status": status, "lastAction": None,
                       "updatedAt": _mtime_ms(jl) if full else None, "runId": None,
                       "toolUseId": tuid, "_path": jl})
    for journal in glob.glob(os.path.join(glob.escape(sub), "workflows", "wf_*", "journal.jsonl")):
        wdir = os.path.dirname(journal)
        run_id = os.path.basename(wdir)
        live = not os.path.exists(os.path.join(sdir, "workflows", run_id + ".json"))
        if not full and not live:
            continue
        for aid, a in _journal(journal).items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", aid):
                continue
            jl = os.path.join(wdir, f"agent-{aid}.jsonl")
            status = a["status"]
            if not status:
                # A run that was stopped part-way never writes its final
                # wf_X.json, so "live" alone would keep its agents running
                # forever: also require recent activity.
                quiet = time.time() * 1000 - (_mtime_ms(jl) or _mtime_ms(journal) or 0)
                status = "running" if live and alive and quiet <= AGENT_STALE_MS else "failed"
            if not full and status != "running":
                continue
            agents.append({"id": aid, "kind": "workflow", "label": _clip(a["label"] or "智能体", 200),
                           "phase": a["phase"], "status": status, "lastAction": None,
                           "updatedAt": (_mtime_ms(jl) or _mtime_ms(journal)) if full else None,
                           "runId": run_id, "toolUseId": None, "_path": jl})
    running = sum(1 for a in agents if a["status"] == "running")
    agents.sort(key=lambda a: (a["status"] != "running", -(a["updatedAt"] or 0)))
    return agents, running


def public_agents(agents, limit=MAX_AGENTS):
    """The first `limit` agents with lastAction filled in, private keys dropped."""
    out = []
    for a in agents[:limit]:
        a = dict(a)
        path = a.pop("_path", None)
        if path and a.get("lastAction") is None:
            a["lastAction"] = agent_tail(path)[0]
        out.append(a)
    return out


def find_agent_file(transcript, agent_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", agent_id or ""):
        return None
    sub = os.path.join(session_dir_of(transcript), "subagents")
    cands = [os.path.join(sub, f"agent-{agent_id}.jsonl")]
    cands += glob.glob(os.path.join(glob.escape(sub), "workflows", "wf_*", f"agent-{agent_id}.jsonl"))
    for c in cands:
        if os.path.isfile(c) and _inside(c, os.path.dirname(transcript)):
            return c
    return None


def agent_dirs_signature(transcript):
    """Cheap fingerprint of the agent folders (changes when files appear)."""
    sdir = session_dir_of(transcript)
    sig = []
    for d in [os.path.join(sdir, "subagents"), os.path.join(sdir, "subagents", "workflows"),
              os.path.join(sdir, "workflows")] + glob.glob(os.path.join(glob.escape(sdir), "subagents", "workflows", "wf_*")):
        try:
            sig.append((d, os.stat(d).st_mtime_ns))
        except OSError:
            pass
    return tuple(sig)


# ---- todos ---------------------------------------------------------------

def tasks_todos(session_id):
    """Todos from ~/.claude/tasks/<sessionId>/*.json (the Task* tools)."""
    if not session_id or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
        return []
    rows = []
    for f in glob.glob(str(CLAUDE_DIR / "tasks" / session_id / "*.json")):
        try:
            d = json.loads(Path(f).read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("status") not in ("pending", "in_progress", "completed"):
            continue
        tid = str(d.get("id") or os.path.basename(f)[:-5])
        rows.append(((0, int(tid)) if tid.isdigit() else (1, tid), {
            "content": _clip(str(d.get("subject") or d.get("content") or ""), 500), "status": d["status"],
            "activeForm": _clip(d["activeForm"], 500) if isinstance(d.get("activeForm"), str) else None,
        }))
    rows.sort(key=lambda r: r[0])
    return [r for _, r in rows]


def session_todos(transcript, session_id):
    todos = transcript_index(transcript).todos if transcript else None
    return todos if todos is not None else tasks_todos(session_id)


def tasks_signature(session_id):
    try:
        return os.stat(CLAUDE_DIR / "tasks" / session_id).st_mtime_ns
    except (OSError, TypeError):
        return None


# ---- prompts in the pane ---------------------------------------------------

_BOX_RE = re.compile("[│┃║╭╮╰╯─━═┌┐└┘├┤┬┴┼╌╍╎╏▕▏]")
_OPTION_RE = re.compile(r"^\s*(?:[❯›>▶➜→]\s*)?(\d)[.)]\s+(\S.*?)\s*$")


def pane_raw(text):
    """(raw, rawHash): the last 20 non-empty lines of a pane capture."""
    lines = [x.rstrip() for x in (text or "").splitlines() if x.strip()]
    raw = "\n".join(lines[-20:])
    return raw, hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


_SOLID_RULE_RE = re.compile(r"^\s*\u2500{20,}\s*$")
_DASHED_RULE_RE = re.compile(r"^\s*[\u254c\u2504\u2508]{20,}\s*$")


def pane_dialog(text):
    """The dialog body above the numbered options: the lines between the
    last full-width rule and the options/question, without the Claude Code
    banner and conversation above it. "" when no menu is on screen."""
    lines = [x.rstrip() for x in (text or "").splitlines()]
    first_opt = None
    for i in range(len(lines) - 1, -1, -1):
        m = _OPTION_RE.match(_BOX_RE.sub("", lines[i]))
        if m and m.group(1) == "1":
            first_opt = i
            break
    if first_opt is None:
        return ""
    start = 0
    for i in range(first_opt - 1, -1, -1):
        if _SOLID_RULE_RE.match(lines[i]):
            start = i + 1
            break
    body = [x for x in lines[start:first_opt] if x.strip() and not _DASHED_RULE_RE.match(x)]
    question, _ = parse_pane_options(text)
    if body and question and body[-1].strip() == question.strip():
        body = body[:-1]
    return "\n".join(body[-30:])


def parse_pane_options(text):
    """(question, [{key, label}]) for the numbered menu at the bottom of a
    pane, e.g. Claude Code's permission dialog. ([], None) when none."""
    lines = [_BOX_RE.sub("", x).rstrip() for x in (text or "").splitlines()]
    lines = lines[-40:]
    blocks, cur = [], None
    for i, line in enumerate(lines):
        m = _OPTION_RE.match(line)
        if m:
            n, label = int(m.group(1)), m.group(2).strip()
            if n == 1:
                cur = {"start": i, "options": [{"key": "1", "label": label}]}
                blocks.append(cur)
            elif cur and n == len(cur["options"]) + 1:
                cur["options"].append({"key": str(n), "label": label})
            else:
                cur = None
        elif not line.strip():
            cur = None
    blocks = [b for b in blocks if len(b["options"]) >= 2]
    if not blocks:
        return None, []
    b = blocks[-1]
    q = []
    for line in reversed(lines[:b["start"]]):
        if not line.strip() or len(q) >= 3:
            break
        q.insert(0, line.strip())
    return (" ".join(q) or None), b["options"]


# A full-width rule or box edge: the input box is drawn between two of these.
_BORDER_RE = re.compile(r"^[╭╰┌└├]?[─━═]{8,}[╮╯┐┘┤]?$")
# The mode line under the input box, e.g. "⏵⏵ accept edits on (shift+tab to cycle)".
_FOOTER_RE = re.compile(r"^([⏵⏸▶►⏩]+\s*)?(bypass permissions|accept edits|plan mode|auto mode) on\b(.*)$")


_PROMPT_RE = re.compile(r"^[>❯›](\s|$)")


def _borders(lines):
    return [i for i, x in enumerate(lines) if _BORDER_RE.match(x.strip())]


def menu_on_screen(text):
    """True when the visible screen ends with a numbered menu (a permission
    dialog or a question) rather than the input box. A numbered list in
    Claude's output does not count: the input box (two rules) follows it."""
    _, options = parse_pane_options(text)
    if not options:
        return False
    lines = [x.rstrip() for x in (text or "").splitlines()][-40:]
    last_opt = max(i for i, x in enumerate(lines) if _OPTION_RE.match(_BOX_RE.sub("", x).rstrip()))
    return len([i for i in _borders(lines) if i > last_opt]) < 2


def detect_mode(text):
    """Permission mode from the footer of a pane capture: only the lines
    under the input box (below its last rule) are read, and only a line
    that starts with the mode text counts, so text in the prompt or in
    Claude's output cannot fake it. None when no input box is visible
    (e.g. a dialog replaced it)."""
    lines = [x.rstrip() for x in (text or "").splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    b = _borders(lines)
    if len(b) < 2 or len(lines) - b[-1] - 1 > 3:
        return None
    # Between the last two rules: the prompt ("> ..."), not a menu.
    inside = [_BOX_RE.sub("", x).strip() for x in lines[b[-2] + 1:b[-1]]]
    if not any(_PROMPT_RE.match(x) for x in inside) or any(_OPTION_RE.match(x) for x in inside):
        return None
    for line in lines[b[-1] + 1:]:
        m = _FOOTER_RE.match(line.strip().lower())
        if m and (m.group(1) or "shift+tab" in m.group(3)):
            return dict((n, mode) for n, mode in MODE_FOOTERS)[m.group(2) + " on"]
    return "default"


_TOOL_KEYS = ("command", "file_path", "notebook_path", "url", "pattern", "query", "path")


def _squash(s):
    """Text without whitespace and box drawing, for loose screen matching."""
    return re.sub(r"\s+", "", _BOX_RE.sub("", s or ""))


def _tool_sig(ev):
    """A short piece of a tool call's input that its dialog shows."""
    inp = ev.get("input") if isinstance(ev.get("input"), dict) else {}
    for k in _TOOL_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            if k in ("file_path", "notebook_path", "path"):
                v = os.path.basename(v.rstrip("/")) or v
            return _squash(v.strip().split("\n")[0])[:60]
    return None


def pick_tool(tools, pane_text):
    """(tool event or None, ambiguous) for the prompt on screen, from the
    open tool calls of the last turn (oldest first).

    With one candidate it is that one. With several, the one whose input
    shows on the screen; ambiguous (no tool-specific card) when none or
    more than one matches, or when a sub-agent (Task) call is open, since
    its own tool calls ask for permission too."""
    tools = [t for t in (tools or []) if t]
    if not tools:
        return None, False
    agentish = any(t.get("name") in ("Task", "Agent") for t in tools)
    if len(tools) == 1 and not agentish:
        return tools[0], False
    if pane_text:
        screen = _squash(pane_text)
        hits = [t for t in tools if t.get("name") not in ("Task", "Agent")
                and _tool_sig(t) and _tool_sig(t) in screen]
        if len(hits) == 1:
            return hits[0], False
    return None, True


def _question_for(qs, screen, pane_labels):
    """The AskUserQuestion entry the screen shows (multi-question calls go
    through their questions one by one), or None."""
    qs = [q for q in qs if isinstance(q, dict)] if isinstance(qs, list) else []
    sq = _squash(screen)
    for q in qs:
        if isinstance(q.get("question"), str) and _squash(q["question"])[:80] and _squash(q["question"])[:80] in sq:
            return q
    for q in qs:
        labels = [str(o.get("label")) for o in q.get("options") or [] if isinstance(o, dict)]
        if labels and pane_labels[:len(labels)] and all(
                _squash(p).startswith(_squash(lb)[:40]) for lb, p in zip(labels, pane_labels)):
            return q
    return qs[0] if len(qs) == 1 else None


def build_pending(item, tools, pane_text):
    """The `pending` object of the v0.2 API (None unless waiting).

    `tools`: the open tool calls of the last turn (a list, oldest first, or
    one event). Options always come from the screen when there is one:
    they are what a number key selects."""
    if item.get("status") != "waiting":
        return None
    if isinstance(tools, dict):
        tools = [tools]
    tool_event, ambiguous = pick_tool(tools, pane_text)
    if ambiguous:  # do not describe a call the prompt may not be about
        tool_event = None
    tool = tool_event.get("name") if tool_event else None
    inp = tool_event.get("input") if tool_event else None
    question, options, raw = None, [], ""
    if pane_text is not None:
        raw = pane_raw(pane_text)[0]
        question, options = parse_pane_options(pane_text)
    kind = "permission" if tool else "dialog"
    multi = False
    if tool == "AskUserQuestion" and isinstance(inp, dict):
        kind = "question"
        qs = inp.get("questions") if isinstance(inp.get("questions"), list) else []
        q = _question_for(qs, pane_text, [o["label"] for o in options])
        if q is not None:
            multi = bool(q.get("multiSelect"))
            if isinstance(q.get("question"), str):
                question = q["question"]
            labels = [str(o.get("label")) for o in q.get("options") or [] if isinstance(o, dict)]
            if pane_text is None and labels:
                options = [{"key": str(i + 1), "label": lb} for i, lb in enumerate(labels[:9])]
        elif pane_text is None and len(qs) > 1:
            question = f"（{len(qs)} 个问题，请在电脑上查看）"
    return {"kind": kind, "waitingFor": item.get("waitingFor") or "", "tool": tool, "input": inp,
            "question": question, "options": options, "raw": raw, "ambiguous": ambiguous,
            "dialog": pane_dialog(pane_text) if pane_text is not None else "",
            "multiSelect": multi, "rawHash": hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]}


def tool_result_text(transcript, tool_id):
    """Full output of one tool call: the saved tool-results file when there
    is one inside the session's project dir, else the transcript copy."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tool_id or ""):
        raise ApiError(HTTPStatus.BAD_REQUEST, "工具调用编号不对")
    project = os.path.dirname(transcript)
    text, saved = None, []
    found = _find_result_record(transcript, tool_id)
    if found:
        d, b = found
        text = _result_text(b.get("content"))
        tur = d.get("toolUseResult")
        if isinstance(tur, dict) and isinstance(tur.get("stdout"), str):
            text = tur["stdout"] + ("\n" + tur["stderr"] if isinstance(tur.get("stderr"), str) and tur["stderr"] else "")
        saved = re.findall(r"saved to:?\s*(/\S+?\.txt)\b", _result_text(b.get("content")))
    for p in [os.path.join(session_dir_of(transcript), "tool-results", tool_id + ".txt")] + saved:
        if _inside(p, project) and os.path.isfile(p):
            with open(p, "rb") as f:
                data = f.read(TOOL_RESULT_MAX)
            return data.decode("utf-8", "replace")
    if text is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "找不到这个工具调用的输出")
    if len(text) > TOOL_RESULT_MAX:
        text = text[:TOOL_RESULT_MAX] + f"\n… (+{len(text) - TOOL_RESULT_MAX})"
    return text


def _find_result_record(transcript, tool_id):
    """(record, tool_result block) for tool_id, searching the transcript
    backwards (at most MAX_HISTORY_BYTES) line by line; None if absent."""
    needle = b'"' + tool_id.encode() + b'"'
    size = os.path.getsize(transcript)
    end = size
    floor = max(0, size - MAX_HISTORY_BYTES)
    while end > floor:
        pos = find_last(transcript, needle, end, end - floor)
        if pos is None:
            return None
        start = _line_start(transcript, pos)
        stop = _line_end(transcript, pos, size)
        if stop - start > RESULT_LINE_MAX:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "这条输出太大，没法读取")
        with open(transcript, "rb") as f:
            f.seek(start)
            line = f.read(stop - start)
        if b'"tool_result"' in line:
            d = _parse_line(line)
            msg = (d or {}).get("message")
            for b in (msg.get("content") if isinstance(msg, dict) else None) or []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == tool_id:
                    return d, b
        end = start
    return None


def parse_transcript(path, limit=SNAPSHOT_EVENTS):
    """(title, events): the newest events of a transcript (v0.2 objects)."""
    try:
        events, _ = load_events(path, None, limit)
    except OSError:
        return None, []
    return transcript_index(path).title, events


def known_project_dirs(cfg, sessions):
    dirs = []
    for p in cfg.get("projects") or []:
        dirs.append(os.path.expanduser(p))
    for s in sessions:
        if s.get("cwd"):
            dirs.append(s["cwd"])
    try:
        projects = json.loads(CLAUDE_JSON.read_text("utf-8")).get("projects") or {}
        dirs.extend(projects.keys())
    except (OSError, ValueError, AttributeError):
        pass
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out[:60]


# --------------------------------------------------------------------------
# App state
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tmux = Tmux()
        self.claude = find_binary(cfg.get("claude"), "claude", [str(HOME / ".local/bin/claude")])
        self.cswap = find_binary(cfg.get("cswap"), "cswap", [str(HOME / ".local/bin/cswap")])
        self.lock = threading.Lock()
        self.key_lock = threading.Lock()  # one multi-key action (answer/mode) at a time
        self.streams = threading.BoundedSemaphore(MAX_STREAMS)
        self.mode_override = {}  # key -> (mode, index.mode_version when set)
        self.answered = {}       # pane -> (rawHash, monotonic time) of the last answer
        self.stream_clients = {}  # page instance id -> token of its current /stream
        self.stream_lock = threading.Lock()
        self._stream_seq = 0

    # ---- sessions -------------------------------------------------------

    def sessions(self):
        panes = self.tmux.panes()
        pane_by_pid = {p["pid"]: p for p in panes}
        # pane id -> (rank, item): one claude per pane. An interactive claude
        # closest to the pane wins over e.g. a `claude -p` it started itself.
        by_pane = {}
        items = []
        for s in live_claude_sessions():
            pane, depth = None, 0
            for depth, a in enumerate(ancestors(s["pid"])):
                if a in pane_by_pid:
                    pane = pane_by_pid[a]
                    break
            entry = s.get("entrypoint") or s.get("kind") or "claude"
            item = {
                "key": f"t:{pane['pane']}" if pane else f"c:{s['pid']}",
                "name": s.get("name") or os.path.basename(s.get("cwd") or "") or "claude",
                "cwd": s.get("cwd") or (pane["cwd"] if pane else ""),
                "status": s.get("status") or "unknown",
                "waitingFor": s.get("waitingFor") or "",
                "updatedAt": s.get("statusUpdatedAt") or s.get("updatedAt"),
                "sessionId": s.get("sessionId"),
                "source": "tmux" if pane else entry,
                "controllable": bool(pane),
                "tmux": pane["session"] if pane else None,
                "pane": pane["pane"] if pane else None,
                "owned": bool(pane and pane["session"].startswith(TMUX_PREFIX)),
                "pid": s["pid"],
                "kind": s.get("kind"),
            }
            if pane:
                rank = (0 if s.get("kind") == "interactive" else 1, depth)
                prev = by_pane.get(pane["pane"])
                if prev is None or rank < prev[0]:
                    by_pane[pane["pane"]] = (rank, item)
            else:
                items.append(item)
        items.extend(item for _, item in by_pane.values())
        # Panes of sessions we started that have no claude session file yet:
        # a plain shell, or claude still at its "trust this folder" screen.
        seen = set(by_pane)
        for p in panes:
            if p["session"].startswith(TMUX_PREFIX) and p["pane"] not in seen:
                seen.add(p["pane"])
                items.append({
                    "key": f"t:{p['pane']}", "name": p["session"][len(TMUX_PREFIX):],
                    "cwd": p["cwd"], "status": "shell" if p["command"] in SHELLS else "starting",
                    "waitingFor": "", "updatedAt": None, "sessionId": None, "source": "tmux",
                    "controllable": True, "tmux": p["session"], "pane": p["pane"], "owned": True,
                    "pid": None, "kind": None,
                })
        for item in items:
            self._decorate(item)
        order = {"waiting": 0, "busy": 1, "starting": 2, "idle": 3}
        items.sort(key=lambda i: (order.get(i["status"], 4), -(i["updatedAt"] or 0)))
        return items

    def _decorate(self, item):
        """Add title, mode and agentsRunning from the session's transcript."""
        item.update({"title": None, "agentsRunning": 0, "mode": None})
        path = transcript_path(item.get("sessionId"))
        if not path:
            return
        try:
            idx = transcript_index(path)
            item["title"] = idx.title
            item["mode"] = self.mode_of(item["key"], idx)
            item["agentsRunning"] = discover_agents(path, idx, full=False)[1]
        except OSError:
            pass

    def mode_of(self, key, idx):
        """The transcript's last permission mode, unless the phone changed it
        since (the transcript only records the mode with the next prompt)."""
        o = self.mode_override.get(key)
        if o and o[1] == idx.mode_version:
            return o[0]
        return idx.mode

    def find(self, key):
        for item in self.sessions():
            if item["key"] == key:
                return item
        return None

    def resolve(self, key):
        item = self.find(key)
        if item is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "会话不存在（可能已经结束）")
        return item

    def transcript_of(self, item):
        path = transcript_path(item.get("sessionId"))
        if not path:
            raise ApiError(HTTPStatus.NOT_FOUND, "这个会话还没有对话记录")
        return path

    def pane_of(self, item, need_program=True):
        """The tmux pane id for item, re-checked right before use.

        With need_program, refuse when the pane's foreground process is a
        shell: text sent there would run as shell commands.
        """
        if not item["controllable"]:
            raise ApiError(HTTPStatus.CONFLICT, "这个会话不在 tmux 里运行，手机上只能查看")
        for p in self.tmux.panes():
            if p["pane"] != item["pane"]:
                continue
            if item.get("pid") and p["pid"] not in ancestors(item["pid"]):
                break
            if need_program and p["command"] in SHELLS:
                raise ApiError(HTTPStatus.CONFLICT, "这个窗口里 claude 没在运行（当前是 shell），没有发送。可以点“启动 claude”。")
            return p["pane"]
        raise ApiError(HTTPStatus.NOT_FOUND, "找不到这个 tmux 窗口（可能已经关闭）")

    def start_claude(self, key, resume):
        item = self.resolve(key)
        if not item["owned"]:
            raise ApiError(HTTPStatus.CONFLICT, "只能在手机上新建的会话里启动 claude")
        pane = self.pane_of(item, need_program=False)
        if not any(p["pane"] == pane and p["command"] in SHELLS for p in self.tmux.panes()):
            raise ApiError(HTTPStatus.CONFLICT, "这个窗口里已经有程序在运行")
        if not self.claude:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "找不到 claude，请在配置里设置 \"claude\"")
        self.tmux.type_line(pane, shlex.quote(self.claude) + (" --continue" if resume else ""))

    def events(self, key, before=None, limit=PAGE_EVENTS):
        """(item, title, events, more, pending, etag, extra) for /events
        (history paging, and the polling fallback, which also needs
        `pending` and, on the newest page, agents and todos in `extra`)."""
        item = self.resolve(key)
        path = transcript_path(item.get("sessionId"))
        if not path:
            return item, None, [], False, self.pending(item), None, {}
        st = os.stat(path)
        events, more, norm = load_page(path, before, limit)
        extra = {}
        tools = None
        if before is None:  # the newest page: the open calls of the last turn are what a prompt is about
            tools = norm.pending_tools() if norm else None
            agents, _ = discover_agents(path)
            extra = {"agents": public_agents(agents), "todos": session_todos(path, item.get("sessionId"))}
        pending = self.pending(item, tools)
        tag = (f"{st.st_mtime_ns}-{st.st_size}-{item['status']}-{item['waitingFor']}-{item['key']}"
               f"-{item['agentsRunning']}-{item['mode']}-{before}-{limit}-{(pending or {}).get('rawHash')}"
               f"-{hashlib.sha1(json.dumps(extra, sort_keys=True).encode()).hexdigest()}")
        etag = '"' + hashlib.sha1(tag.encode("utf-8")).hexdigest()[:20] + '"'
        return item, transcript_index(path).title, events, more, pending, etag, extra

    def agent_events(self, key, agent_id):
        item = self.resolve(key)
        path = self.transcript_of(item)
        f = find_agent_file(path, agent_id)
        if not f:
            raise ApiError(HTTPStatus.NOT_FOUND, "找不到这个智能体")
        agents, _ = discover_agents(path)
        agent = next((a for a in agents if a["id"] == agent_id), None)
        agent = public_agents([agent])[0] if agent else {
            "id": agent_id, "kind": "task", "label": "智能体", "phase": None, "status": "done",
            "lastAction": agent_tail(f)[0], "updatedAt": _mtime_ms(f), "runId": None}
        events, _ = load_events(f, None, SNAPSHOT_EVENTS, sidechain=True)
        return {"agent": agent, "events": events}

    def tool_result(self, key, tool_id):
        return {"text": tool_result_text(self.transcript_of(self.resolve(key)), tool_id)}

    def pending(self, item, tools=None):
        """`pending` for item (captures the pane of a waiting tmux session).
        `tools`: the open tool calls of the last turn, oldest first."""
        if item.get("status") != "waiting":
            return None
        text = None
        if item.get("controllable"):
            try:
                text = self.tmux.capture(self.pane_of(item), None)
            except ApiError:
                text = None
        return build_pending(item, tools, text)

    # ---- prompts, stop, mode ---------------------------------------------

    def _no_prompt(self, item, screen, action):
        """Refuse (409) while a prompt is open: Enter, digits or Shift+Tab
        sent now would answer it (e.g. confirm "1. Yes")."""
        if item.get("status") == "waiting" or menu_on_screen(screen):
            raise ApiError(HTTPStatus.CONFLICT, f"Claude 正在等你处理提示，先处理完再{action}")

    def send(self, key, text, submit=True):
        """Type a message into the claude in a tmux pane (and press Enter)."""
        item = self.resolve(key)
        pane = self.pane_of(item)
        with self.key_lock:
            self._no_prompt(item, self.tmux.capture(pane, None), "发消息")
            self.tmux.send_text(pane, text, submit=submit)
        return {"ok": True}

    def _settle(self, pane, before_hash):
        """Wait (ANSWER_SETTLE at most) until the screen changes after a key;
        return the new screen."""
        deadline = time.monotonic() + ANSWER_SETTLE
        while True:
            time.sleep(0.1)
            screen = self.tmux.capture(pane, None)
            if pane_raw(screen)[1] != before_hash or time.monotonic() >= deadline:
                return screen

    def answer(self, key, choice, text, raw_hash, label=None):
        choice = str(choice or "").strip().lower()
        if choice not in ANSWER_CHOICES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "选项不对")
        text = str(text or "").strip()
        if len(text) > MAX_SEND_CHARS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "消息太长")
        raw_hash = str(raw_hash or "")
        pane = self.pane_of(self.resolve(key))
        with self.key_lock:
            screen = self.tmux.capture(pane, None)
            if pane_raw(screen)[1] != raw_hash:
                raise ApiError(HTTPStatus.CONFLICT, "提示已经变化，请重新查看")
            last = self.answered.get(pane)
            if last and last[0] == raw_hash and time.monotonic() - last[1] < ANSWER_REPEAT:
                raise ApiError(HTTPStatus.CONFLICT, "这个提示刚刚已经回答过了")
            _, options = parse_pane_options(screen)
            shown = ""
            if choice != "esc":
                if options and choice not in {o["key"] for o in options}:
                    raise ApiError(HTTPStatus.CONFLICT, "提示已经变化，请重新查看")
                shown = next((o["label"] for o in options if o["key"] == choice), "")
                # The label the phone showed must be the one this key selects now.
                if label is not None and options and _squash(str(label)) != _squash(shown):
                    raise ApiError(HTTPStatus.CONFLICT, "提示已经变化，请重新查看")
            self.tmux.send_key(pane, "Escape" if choice == "esc" else choice)
            self.answered[pane] = (raw_hash, time.monotonic())
            # Hold the lock until the screen reacts, so a second tap sees the
            # new screen (and a stale rawHash) instead of pressing again.
            screen = self._settle(pane, raw_hash)
            sent = False
            if text and (choice == "esc" or shown.lower().startswith("no")):
                if menu_on_screen(screen):  # another prompt came up: Enter would answer it
                    return {"ok": True, "sentText": False}
                self.tmux.send_text(pane, text)
                sent = True
        return {"ok": True, "sentText": sent}

    def interrupt(self, key):
        self.tmux.send_key(self.pane_of(self.resolve(key)), "Escape")
        return {"ok": True}

    def set_mode(self, key, target):
        if target not in MODES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "不支持的模式")
        item = self.resolve(key)
        pane = self.pane_of(item)

        def current():
            # Re-checked before every Shift+Tab: on a permission dialog it
            # means "Yes, allow all edits" instead of switching the mode.
            screen = self.tmux.capture(pane, None)
            self._no_prompt(item, screen, "切换模式")
            mode = detect_mode(screen)
            if mode is None:
                raise ApiError(HTTPStatus.CONFLICT, "看不到 claude 的输入框和模式提示，没有切换")
            return mode

        with self.key_lock:
            cur = current()
            presses = 0
            while cur != target and presses < 6:
                self.tmux.send_key(pane, "BTab")
                presses += 1
                time.sleep(MODE_WAIT)
                cur = current()
            if cur == "bypassPermissions":  # never leave it there
                self.tmux.send_key(pane, "BTab")
                time.sleep(MODE_WAIT)
                cur = current()
        if cur != target:
            raise ApiError(HTTPStatus.CONFLICT, f"没能切换到「{MODE_LABELS[target]}」（现在是「{MODE_LABELS.get(cur, cur)}」）")
        path = transcript_path(item.get("sessionId"))
        if path:
            self.mode_override[key] = (cur, transcript_index(path).mode_version)
        return {"mode": cur}

    # ---- live streams ----------------------------------------------------

    def stream_begin(self, client):
        """Register a /stream for page instance `client` (or None); a newer
        stream of the same client replaces the older one (a phone that
        reconnects after a network change leaves the old one behind).
        Returns a token for stream_current()."""
        with self.stream_lock:
            self._stream_seq += 1
            tok = self._stream_seq
            if client:
                if len(self.stream_clients) > 500:
                    self.stream_clients.clear()
                self.stream_clients[client] = tok
            return tok

    def stream_current(self, client, tok):
        return not client or self.stream_clients.get(client) == tok

    def stream_end(self, client, tok):
        with self.stream_lock:
            if client and self.stream_clients.get(client) == tok:
                del self.stream_clients[client]

    def new_session(self, cwd, name, resume, claude_args=None):
        if not self.tmux.available():
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "服务器上没有安装 tmux")
        if not self.claude:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "找不到 claude，请在配置里设置 \"claude\"")
        cwd = os.path.realpath(os.path.expanduser(cwd or ""))
        if not os.path.isdir(cwd):
            raise ApiError(HTTPStatus.BAD_REQUEST, "目录不存在")
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", name or os.path.basename(cwd)).strip("-")[:30] or "session"
        existing = {p["session"] for p in self.tmux.panes()}
        sess = TMUX_PREFIX + base
        n = 2
        while sess in existing:
            sess = f"{TMUX_PREFIX}{base}-{n}"
            n += 1
        pane = self.tmux.new_session(sess, cwd)
        args = claude_args if claude_args is not None else (["--continue"] if resume else [])
        self.tmux.type_line(pane, " ".join([shlex.quote(self.claude)] + [shlex.quote(a) for a in args]))
        return {"key": f"t:{pane}", "tmux": sess}

    def resume_conversation(self, session_id):
        """Continue a conversation whose claude is no longer running (e.g. its
        VS Code panel was closed) in a new cr- tmux session, with the official
        `claude --resume <id>`. Refuses while any live process still owns it,
        so two processes never write the same conversation."""
        path = transcript_path(session_id)
        if not path:
            raise ApiError(HTTPStatus.NOT_FOUND, "找不到这个对话")
        if any(d.get("sessionId") == session_id for d in live_claude_sessions()):
            raise ApiError(HTTPStatus.CONFLICT, "这个对话还开在别的地方（比如 VS Code 面板）。请先在电脑上关掉那个面板，再在手机上继续。")
        info = conversation_info(path)
        cwd = info.get("cwd")
        if not cwd or not os.path.isdir(cwd):
            raise ApiError(HTTPStatus.CONFLICT, "这个对话的工作目录已经不存在")
        return self.new_session(cwd, "", False, claude_args=["--resume", session_id])

    def recent_conversations(self, days=14, limit=30):
        live = {d.get("sessionId") for d in live_claude_sessions()}
        cutoff = time.time() - days * 86400
        files = []
        for f in glob.glob(str(CLAUDE_DIR / "projects" / "*" / "*.jsonl")):
            try:
                m = os.path.getmtime(f)
            except OSError:
                continue
            sid = os.path.basename(f)[:-6]
            if m >= cutoff and sid not in live and re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
                files.append((m, f, sid))
        out = []
        for m, f, sid in sorted(files, reverse=True):
            info = conversation_info(f)
            if not info.get("cwd") or not info.get("hasUser"):
                continue
            out.append({"sessionId": sid, "title": info.get("title"), "cwd": info["cwd"],
                        "updatedAt": int(m * 1000), "cwdExists": os.path.isdir(info["cwd"])})
            if len(out) >= limit:
                break
        return out

    def kill(self, key):
        item = self.resolve(key)
        if not item["owned"]:
            raise ApiError(HTTPStatus.CONFLICT, "只能结束在手机上新建的会话（cr- 开头）。你自己开的 tmux 会话请在电脑上关闭。")
        self.tmux.kill_session(item["tmux"])

    # ---- git ------------------------------------------------------------

    def git(self, key):
        item = self.resolve(key)
        cwd = item.get("cwd")
        if not cwd or not os.path.isdir(cwd):
            raise ApiError(HTTPStatus.NOT_FOUND, "这个会话没有工作目录")
        base = ["git", "-c", "core.quotepath=false", "-c", "color.ui=never", "-C", cwd]
        code, top, err = run(base + ["rev-parse", "--show-toplevel"], timeout=10)
        if code != 0:
            return {"cwd": cwd, "repo": False, "message": err.strip()}
        has_head = run(base + ["rev-parse", "--verify", "-q", "HEAD"], timeout=10)[0] == 0
        # No commits yet: compare the index with the empty tree instead.
        against = ["HEAD"] if has_head else ["--cached"]
        errors = []
        code, status, e = run(base + ["status", "--short", "--branch"], timeout=20)
        if code:
            errors.append(e.strip())
        code, stat, e = run(base + ["diff", *against, "--stat"], timeout=20)
        if code:
            errors.append(e.strip())
        files = []
        code, numstat, e = run(base + ["diff", *against, "--numstat"], timeout=20)
        if code:
            errors.append(e.strip())
        for line in numstat.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                files.append({"path": parts[2], "added": int(parts[0]) if parts[0].isdigit() else None,
                              "removed": int(parts[1]) if parts[1].isdigit() else None})
        code, others, _ = run(base + ["ls-files", "--others", "--exclude-standard", "--full-name", "--", ":/"], timeout=20)
        for line in others.splitlines()[:500] if code == 0 else []:
            files.append({"path": line, "added": None, "removed": None, "untracked": True})
        code, diff, e = run(base + ["diff", *against, "--no-ext-diff"], timeout=30)
        if code:
            errors.append(e.strip())
        truncated = len(diff.encode("utf-8", "replace")) > MAX_DIFF_BYTES
        if truncated:
            diff = diff.encode("utf-8", "replace")[:MAX_DIFF_BYTES].decode("utf-8", "ignore")
        log = run(base + ["log", "--oneline", "-8"], timeout=10)[1] if has_head else ""
        return {
            "cwd": cwd, "repo": True, "top": top.strip(), "status": status, "hasHead": has_head,
            "stat": stat, "diff": diff, "truncated": truncated, "log": log, "files": files,
            "errors": [x for x in errors if x],
        }

    # ---- accounts -------------------------------------------------------

    def accounts(self):
        if not self.cswap:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "找不到 cswap，请在配置里设置 \"cswap\"")
        code, out, err = run([self.cswap, "policy", "list", "--json"], timeout=30)
        start = out.find("{")
        if start < 0:
            raise ApiError(HTTPStatus.BAD_GATEWAY, (err or out).strip()[:500] or "cswap 运行失败")
        try:
            return json.loads(out[start:])
        except ValueError:
            raise ApiError(HTTPStatus.BAD_GATEWAY, "cswap returned invalid JSON")

    def switch(self, number):
        if not self.cswap:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "找不到 cswap")
        code, out, err = run([self.cswap, "switch", str(int(number))], timeout=90)
        text = (out + "\n" + err).strip()
        if code != 0:
            raise ApiError(HTTPStatus.BAD_GATEWAY, text[-800:] or "切换失败")
        return {"ok": True, "output": text[-800:]}


# --------------------------------------------------------------------------
# Notifications (optional, ntfy)
# --------------------------------------------------------------------------

class Notifier(threading.Thread):
    """Watches session status and pushes a short note via ntfy.

    Only metadata is sent (session name, state, what it waits for), never
    transcript text, code or account details. Disabled unless a topic is set.
    """

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.prev = {}
        self.prev_active = None

    def topic(self):
        n = self.app.cfg.get("ntfy") or {}
        return (n.get("server") or "https://ntfy.sh").rstrip("/"), n.get("topic") or ""

    def push(self, title, body, tags="robot"):
        server, topic = self.topic()
        if not topic:
            return
        qs = urllib.parse.urlencode({"title": title, "tags": tags})
        req = urllib.request.Request(
            f"{server}/{urllib.parse.quote(topic, safe='')}?{qs}", data=body.encode("utf-8"), method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:  # network trouble must never kill the watcher
            print(f"ntfy push failed: {e}", file=sys.stderr)

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"notifier: {e}", file=sys.stderr)
            time.sleep(3)

    def tick(self):
        if not self.topic()[1]:
            return
        cur = {}
        for s in self.app.sessions():
            cur[s["key"]] = s
            before = self.prev.get(s["key"])
            if before is None:
                continue
            name = s["name"]
            if s["status"] == "waiting" and before["status"] != "waiting":
                self.push(f"{name}: needs you", s["waitingFor"] or "waiting for input", "bell")
            elif s["status"] == "idle" and before["status"] == "busy":
                self.push(f"{name}: done", "Claude finished its turn", "white_check_mark")
        self.prev = cur


# --------------------------------------------------------------------------
# Live stream (Server-Sent Events)
# --------------------------------------------------------------------------

class StreamWatcher:
    """Follows one session for one /stream client.

    Polls every POLL_INTERVAL: the session's status file (re-listing the
    session when it changes, and at least every RESOLVE_INTERVAL), the
    transcript (byte-offset tail), the agent folders and the task list.
    `send(obj)` writes one message and raises OSError once the client is
    gone, which ends run().
    """

    def __init__(self, app, item, send, current=None):
        self.app = app
        self.key = item["key"]
        self.item = item
        self.send = send
        self.current = current or (lambda: True)  # False once a newer stream replaced this one
        self.path = None
        self.tail = None
        self.norm = Normalizer()
        self.pending = None
        self.pane_at = 0.0
        self.last_status = None
        self.agents = None
        self.agent_sig = None
        self.agents_at = 0.0
        self.todos = None
        self.todo_sig = None
        self.misses = 0

    # -- pieces --
    def _open(self):
        self.norm = Normalizer()
        self.tail = None
        self.path = transcript_path(self.item.get("sessionId"))
        if not self.path:
            return
        self.tail = TranscriptTail(self.path)
        try:
            recs = self.tail.open()
        except OSError:
            self.tail, recs = None, []
        for off, rec in recs:
            self.norm.feed(rec, off)

    def _session_sig(self):
        pid = self.item.get("pid")
        sig = []
        for f in (CLAUDE_DIR / "sessions", CLAUDE_DIR / "sessions" / f"{pid}.json"):
            try:
                sig.append(os.stat(f).st_mtime_ns)
            except OSError:
                sig.append(None)
        return tuple(sig)

    def _pid_alive(self):
        pid = self.item.get("pid")
        return not pid or os.path.exists(f"/proc/{pid}")

    def _agents(self):
        if not self.path:
            return []
        # Once claude has left (the pane is back at a shell), nothing it ran is still running.
        alive = bool(self.item.get("sessionId")) and self._pid_alive()
        agents, _ = discover_agents(self.path, alive=alive)
        return public_agents(agents)

    def _todos(self):
        return session_todos(self.path, self.item.get("sessionId"))

    def _status(self):
        return {"type": "status", "session": self.item, "pending": self.pending}

    def snapshot(self):
        self.pending = self.app.pending(self.item, self.norm.pending_tools())
        self.pane_at = time.monotonic()
        self.agents = self._agents()
        self.agent_sig = agent_dirs_signature(self.path) if self.path else None
        self.agents_at = time.monotonic()
        self.todos = self._todos()
        self.todo_sig = tasks_signature(self.item.get("sessionId"))
        self.last_status = json.dumps(self._status(), sort_keys=True, ensure_ascii=False)
        evs = self.norm.events
        return {"type": "snapshot", "session": self.item, "events": evs[-SNAPSHOT_EVENTS:],
                "more": bool(self.tail and self.tail.start > 0) or len(evs) > SNAPSHOT_EVENTS,
                "agents": self.agents, "todos": self.todos, "pending": self.pending}

    def _refresh_item(self, now):
        """Re-list the session. Returns "gone", "reopen" or None."""
        item = self.app.find(self.key)
        if item is None:
            self.misses += 1
            return "gone" if self.misses >= 2 else None
        self.misses = 0
        new_sid = item.get("sessionId")
        changed_session = new_sid != self.item.get("sessionId")
        self.item = item
        if changed_session and new_sid is None and self.path:
            # claude exited (the pane is back at a shell): keep showing the
            # conversation it left; only the status changes.
            return None
        if self.path and new_sid and self.path == transcript_path(new_sid):
            return None
        if changed_session or (self.path is None and transcript_path(new_sid)):
            return "reopen"
        return None

    def _pump(self):
        """Send new transcript events. True if anything was read, None on reset."""
        if not self.tail:
            return False
        recs = self.tail.read_new()
        if recs is None:
            return None
        for off, rec in recs:
            for typ, ev in self.norm.feed(rec, off):
                self.send({"type": typ, "event": ev})
        return bool(recs)

    def _maybe_status(self, now, grew):
        if self.item.get("status") == "waiting":
            if self.pending is None or grew or now - self.pane_at >= PANE_INTERVAL:
                self.pending = self.app.pending(self.item, self.norm.pending_tools())
                self.pane_at = now
        else:
            self.pending = None
        msg = self._status()
        sig = json.dumps(msg, sort_keys=True, ensure_ascii=False)
        if sig != self.last_status:
            self.last_status = sig
            self.send(msg)

    def _maybe_agents(self, now, grew):
        if not self.path or now - self.agents_at < 1.0:
            return
        sig = agent_dirs_signature(self.path)
        running = any(a["status"] == "running" for a in self.agents or [])
        if sig == self.agent_sig and not grew and not running:
            return
        self.agent_sig, self.agents_at = sig, now
        agents = self._agents()
        if agents != self.agents:
            self.agents = agents
            self.send({"type": "agents", "agents": agents})

    def _maybe_todos(self, grew):
        sig = tasks_signature(self.item.get("sessionId"))
        if not grew and sig == self.todo_sig:
            return
        self.todo_sig = sig
        todos = self._todos()
        if todos != self.todos:
            self.todos = todos
            self.send({"type": "todos", "todos": todos})

    # -- loop --
    def run(self):
        try:
            self._open()
            self.send(self.snapshot())
            sig = self._session_sig()
            last_resolve = last_ping = time.monotonic()
            while True:
                time.sleep(POLL_INTERVAL)
                if not self.current():
                    return  # the same page opened a newer stream
                now = time.monotonic()
                new_sig = self._session_sig()
                if new_sig != sig or not self._pid_alive() or now - last_resolve >= RESOLVE_INTERVAL:
                    sig, last_resolve = new_sig, now
                    what = self._refresh_item(now)
                    if what == "gone":
                        self.send({"type": "gone"})
                        return
                    if what == "reopen":
                        self._open()
                        self.send(self.snapshot())
                        continue
                grew = self._pump()
                if grew is None:  # transcript replaced or truncated: start over
                    self._open()
                    self.send(self.snapshot())
                    continue
                self._maybe_status(now, grew)
                self._maybe_agents(now, grew)
                self._maybe_todos(grew)
                if now - last_ping >= PING_INTERVAL:
                    last_ping = now
                    self.send({"type": "ping"})
        except OSError:
            return  # client went away
        except Exception as e:  # headers are already sent: just end the stream
            print(f"stream {self.key}: {e!r}", file=sys.stderr)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/manifest+json",
    ".svg": "image/svg+xml", ".png": "image/png",
}


def make_handler(app):
    token = app.cfg["token"]

    class Handler(BaseHTTPRequestHandler):
        server_version = f"cswap-remote/{VERSION}"
        protocol_version = "HTTP/1.1"
        timeout = 60  # drop idle or slow connections instead of holding a thread forever
        raw_body = b""

        def log_message(self, fmt, *args):  # no request logging (paths can hold names)
            pass

        def handle(self):
            # A phone dropping a keep-alive connection is normal: no traceback.
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError):
                pass

        # -- plumbing --
        def _send(self, status, body=b"", ctype="application/json", headers=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status=HTTPStatus.OK, headers=None):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), headers=headers)

        def _sse(self, obj):
            self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        def _stream(self, key, client):
            """GET /api/session/<k>/stream?c=<page id>: a long-lived
            text/event-stream response (read with fetch(), so the bearer
            header works). A newer stream with the same page id ends the
            older one, so reconnects do not pile up."""
            streams = getattr(app, "streams", None)
            if streams is None:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "实时连接太多了，请关掉其他打开的页面再试")
            client = client if client and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", client) else None
            tok = app.stream_begin(client)
            # the replaced stream notices within one poll and frees its slot
            if not streams.acquire(timeout=1.0 if client else 0.01):
                app.stream_end(client, tok)
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "实时连接太多了，请关掉其他打开的页面再试")
            try:
                item = app.resolve(key)
                # A write that blocks this long (client vanished without a
                # reset, e.g. after a network change) ends the stream.
                self.connection.settimeout(STREAM_SEND_TIMEOUT)
                self.close_connection = True
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Connection", "close")
                self.end_headers()
                StreamWatcher(app, item, self._sse, lambda: app.stream_current(client, tok)).run()
            finally:
                app.stream_end(client, tok)
                streams.release()

        def _authorized(self):
            h = self.headers.get("Authorization", "")
            given = h[7:] if h.startswith("Bearer ") else ""
            return hmac.compare_digest(given.encode(), token.encode())

        def _read_raw_body(self):
            """Read the whole request body up front (bounded), so every reply
            leaves the keep-alive stream in sync. Returns False if rejected."""
            self.raw_body = b""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > 256_000:
                self._json({"error": "请求太大或格式不对"}, HTTPStatus.BAD_REQUEST, {"Connection": "close"})
                return False
            if n:
                self.raw_body = self.rfile.read(n)
            return True

        def _body(self):
            if not self.raw_body:
                return {}
            try:
                data = json.loads(self.raw_body.decode("utf-8"))
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid JSON")
            if not isinstance(data, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "expected a JSON object")
            return data

        def _static(self, path):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            if "/" in name or name.startswith("."):
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            f = STATIC_DIR / name
            if not f.is_file():
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            ctype = STATIC_TYPES.get(f.suffix, "application/octet-stream")
            csp = ("default-src 'self'; script-src 'self'; style-src 'self'; "
                   "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self._send(HTTPStatus.OK, f.read_bytes(), ctype, {"Content-Security-Policy": csp})

        # -- routing --
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            # Nothing is read from a request body before the token is checked:
            # rejected requests are answered at once and the connection closed.
            if not path.startswith("/api/"):
                if method != "GET":
                    return self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"", "text/plain", {"Connection": "close"})
                return self._static(path)
            if not self._authorized():
                return self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED, {"Connection": "close"})
            if method == "POST" and not self._read_raw_body():
                return
            try:
                self._api(method, path, urllib.parse.parse_qs(url.query))
            except ApiError as e:
                self._json({"error": e.message}, e.status)
            except Exception as e:  # keep the server up; report briefly
                print(f"error handling {method} {path}: {e!r}", file=sys.stderr)
                self._json({"error": "服务器内部错误（详情见服务器日志）"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _api(self, method, path, query):
            parts = [urllib.parse.unquote(p) for p in path.split("/")[2:]]
            if method == "GET" and parts == ["status"]:
                return self._json({
                    "version": VERSION, "host": os.uname().nodename,
                    "tmux": app.tmux.available(), "claude": bool(app.claude), "cswap": bool(app.cswap),
                    "notify": bool((app.cfg.get("ntfy") or {}).get("topic")),
                })
            if method == "GET" and parts == ["sessions"]:
                return self._json({"sessions": app.sessions()})
            if method == "POST" and parts == ["sessions"]:
                b = self._body()
                return self._json(app.new_session(str(b.get("cwd") or ""), str(b.get("name") or ""), bool(b.get("resume"))))
            if method == "GET" and parts == ["recent"]:
                return self._json({"conversations": app.recent_conversations()})
            if method == "POST" and parts == ["resume"]:
                return self._json(app.resume_conversation(str(self._body().get("sessionId") or "")))
            if method == "GET" and parts == ["projects"]:
                dirs = known_project_dirs(app.cfg, app.sessions())
                return self._json({"projects": [{"path": d, "label": short_home(d)} for d in dirs]})
            if len(parts) == 5 and parts[0] == "session" and parts[2] == "agent" and parts[4] == "events":
                if method == "GET":
                    return self._json(app.agent_events(parts[1], parts[3]))
            if len(parts) == 4 and parts[0] == "session" and parts[2] == "toolresult" and method == "GET":
                return self._json(app.tool_result(parts[1], parts[3]))
            if len(parts) == 3 and parts[0] == "session":
                key, action = parts[1], parts[2]
                if method == "GET" and action == "stream" and self.command == "GET":
                    return self._stream(key, (query.get("c") or [None])[0])
                if method == "GET" and action == "events":
                    before = (query.get("before") or [None])[0]
                    try:
                        limit = max(1, min(500, int((query.get("limit") or [PAGE_EVENTS])[0])))
                    except ValueError:
                        raise ApiError(HTTPStatus.BAD_REQUEST, "limit 不对")
                    item, title, events, more, pending, body_etag, extra = app.events(key, before, limit)
                    inm = self.headers.get("If-None-Match")
                    if body_etag and inm == body_etag:
                        return self._send(HTTPStatus.NOT_MODIFIED, b"", headers={"ETag": body_etag})
                    hdr = {"ETag": body_etag} if body_etag else None
                    out = {"session": item, "title": title, "events": events, "more": more, "pending": pending}
                    out.update(extra or {})
                    return self._json(out, headers=hdr)
                if method == "POST" and action == "answer":
                    b = self._body()
                    label = b.get("label") if isinstance(b.get("label"), str) else None
                    return self._json(app.answer(key, b.get("choice"), b.get("text"), b.get("rawHash"), label))
                if method == "POST" and action == "interrupt":
                    return self._json(app.interrupt(key))
                if method == "POST" and action == "mode":
                    return self._json(app.set_mode(key, str(self._body().get("target") or "")))
                if method == "GET" and action == "pane":
                    pane = app.pane_of(app.resolve(key), need_program=False)
                    return self._json({"text": app.tmux.capture(pane)})
                if method == "GET" and action == "git":
                    return self._json(app.git(key))
                if method == "POST" and action == "send":
                    b = self._body()
                    text = str(b.get("text") or "")
                    if not text.strip():
                        raise ApiError(HTTPStatus.BAD_REQUEST, "消息是空的")
                    if len(text) > MAX_SEND_CHARS:
                        raise ApiError(HTTPStatus.BAD_REQUEST, "消息太长")
                    return self._json(app.send(key, text, submit=b.get("submit", True) is not False))
                if method == "POST" and action == "key":
                    b = self._body()
                    k = str(b.get("key") or "")
                    if k not in ALLOWED_KEYS:
                        raise ApiError(HTTPStatus.BAD_REQUEST, "不允许的按键")
                    pane = app.pane_of(app.resolve(key))
                    app.tmux.send_key(pane, k)
                    return self._json({"ok": True})
                if method == "POST" and action == "kill":
                    app.kill(key)
                    return self._json({"ok": True})
                if method == "POST" and action == "start":
                    app.start_claude(key, bool(self._body().get("resume")))
                    return self._json({"ok": True})
            if method == "GET" and parts == ["accounts"]:
                return self._json(app.accounts())
            if method == "POST" and parts == ["accounts", "switch"]:
                b = self._body()
                try:
                    number = int(b.get("number"))
                except (TypeError, ValueError):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "缺少账号编号")
                with app.lock:
                    return self._json(app.switch(number))
            raise ApiError(HTTPStatus.NOT_FOUND, "未知接口")

    return Handler


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most MAX_CONNECTIONS connections (one
    thread each) at a time; more are closed at once."""

    daemon_threads = True

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def host_is_private(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    if ip.is_loopback:
        return True
    if ip.version == 4:
        return ip in TAILSCALE_V4
    return ip in TAILSCALE_V6


def tailscale_ip():
    ts = shutil.which("tailscale")
    if not ts:
        return None
    code, out, _ = run([ts, "ip", "-4"], timeout=5)
    ip = out.strip().splitlines()[0] if code == 0 and out.strip() else None
    return ip


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(prog="cswap-remote", description="Self-hosted remote control for Claude Code sessions.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the web server (default)")
    s.add_argument("--host", help="bind address (default from config: 127.0.0.1). Use 'tailscale' for this machine's Tailscale IP")
    s.add_argument("--port", type=int)
    s.add_argument("--allow-public", action="store_true", help="allow binding to a non-loopback, non-Tailscale address")
    sub.add_parser("url", help="print the login URL (contains the access token)")
    sub.add_parser("rotate-token", help="replace the access token (logs out every device)")
    n = sub.add_parser("notify", help="configure ntfy push notifications")
    n.add_argument("topic", help="ntfy topic (use a long random one), or 'off'")
    n.add_argument("--server", default=None, help="ntfy server (default https://ntfy.sh)")
    args = ap.parse_args(argv)
    cfg = load_config()

    if args.cmd == "rotate-token":
        cfg["token"] = secrets.token_urlsafe(32)
        save_config(cfg)
        print("New token saved. Log in again on each device with: cswap-remote url")
        return
    if args.cmd == "notify":
        cfg.setdefault("ntfy", {})
        cfg["ntfy"]["topic"] = "" if args.topic == "off" else args.topic
        if args.server:
            cfg["ntfy"]["server"] = args.server
        save_config(cfg)
        print("Notifications off." if args.topic == "off" else f"Notifications go to {cfg['ntfy'].get('server') or 'https://ntfy.sh'}/{args.topic}")
        return

    host = getattr(args, "host", None) or cfg["host"]
    if host == "tailscale":
        host = tailscale_ip() or sys.exit("cswap-remote: no Tailscale IPv4 address found")
    port = getattr(args, "port", None) or int(cfg["port"])
    shown = host if host not in ("127.0.0.1", "localhost") else (tailscale_ip() or host)
    url = f"http://{shown}:{port}/#token={cfg['token']}"

    if args.cmd == "url":
        print(url)
        if host in ("127.0.0.1", "localhost"):
            print("(server binds to 127.0.0.1: reach it through `tailscale serve`, an SSH tunnel, or run with --host tailscale)")
        return

    if not host_is_private(host) and not getattr(args, "allow_public", False):
        sys.exit(f"cswap-remote: refusing to listen on {host}: not loopback or Tailscale. "
                 "This server can type into your Claude sessions; keep it private (or pass --allow-public).")
    app = App(cfg)
    Notifier(app).start()
    httpd = Server((host, port), make_handler(app))
    print(f"cswap-remote {VERSION} listening on http://{host}:{port}")
    print(f"tmux: {app.tmux.bin or 'NOT FOUND'} | claude: {app.claude or 'NOT FOUND'} | cswap: {app.cswap or 'NOT FOUND'}")
    print("Open on your phone (keep this URL private, it contains the token): cswap-remote url")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
