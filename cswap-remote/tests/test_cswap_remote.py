"""Tests for cswap-remote.

Run: python3 -m unittest discover -s tests

Everything runs against a synthetic Claude config dir (CLAUDE_CONFIG_DIR is
pointed at a temporary fixture before the module is imported) and binds
127.0.0.1 only. The tmux tests create sessions named cr-fixture-* running
`cat` and kill them afterwards; they are skipped when tmux is missing.
"""

import atexit
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

FIXTURE = tempfile.mkdtemp(prefix="cswap-remote-test-")
atexit.register(shutil.rmtree, FIXTURE, True)
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(FIXTURE, "claude")
os.environ["XDG_CONFIG_HOME"] = os.path.join(FIXTURE, "xdg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cswap_remote as cr  # noqa: E402

CLAUDE = Path(os.environ["CLAUDE_CONFIG_DIR"])
PROJECTS = CLAUDE / "projects"
SESSIONS = CLAUDE / "sessions"


def write_jsonl(path, rows, mode="w"):
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")


def feed_all(rows, sidechain=False):
    n = cr.Normalizer(sidechain=sidechain)
    changes = []
    for i, r in enumerate(rows):
        changes.extend(n.feed(r, i * 100))
    return n, changes


def asst(uuid, *blocks, **extra):
    d = {"type": "assistant", "uuid": uuid, "timestamp": "t", "message": {"content": list(blocks)}}
    d.update(extra)
    return d


def user(uuid, content, **extra):
    d = {"type": "user", "uuid": uuid, "timestamp": "t", "message": {"content": content}}
    d.update(extra)
    return d


def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid, text, is_error=False):
    return {"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
            "content": [{"type": "text", "text": text}]}


def free_port():
    for port in range(18800, 18900):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port in 18800-18899")


# --------------------------------------------------------------------------
# Normalizer
# --------------------------------------------------------------------------

class NormalizerTests(unittest.TestCase):
    def test_messages_tools_and_results_merge(self):
        n, changes = feed_all([
            {"type": "ai-title", "aiTitle": "标题"},
            user("u1", "你好 <script>", permissionMode="acceptEdits"),
            user("u2", "hidden", isMeta=True),
            asst("a1", {"type": "thinking", "thinking": "SECRET-THOUGHT"},
                 {"type": "text", "text": "ok"},
                 tool_use("t1", "Bash", command="ls -la", description="列出文件")),
            user("u3", [tool_result("t1", "boom", True)],
                 toolUseResult={"stdout": "", "stderr": "boom", "interrupted": False}),
            "not a dict",
        ])
        self.assertEqual(n.title, "标题")
        self.assertEqual(n.mode, "acceptEdits")
        kinds = [(e["kind"], e["id"]) for e in n.events]
        self.assertEqual(kinds, [("user", "u1"), ("thinking", "a1:0"), ("text", "a1:1"), ("tool", "t1")])
        self.assertEqual(n.events[0]["text"], "你好 <script>")
        tool = n.by_id["t1"]
        self.assertEqual(tool["status"], "error")
        self.assertEqual(tool["input"], {"command": "ls -la", "description": "列出文件"})
        self.assertEqual(tool["result"]["text"], "boom")
        self.assertEqual(tool["result"]["stderr"], "boom")
        self.assertFalse(tool["result"]["truncated"])
        self.assertEqual(changes[-1], ("patch", tool))
        self.assertIsNone(n.pending_tool())
        self.assertNotIn("SECRET-THOUGHT", json.dumps(n.events))

    def test_running_tool_is_pending(self):
        n, _ = feed_all([asst("a1", tool_use("t1", "Read", file_path="/x"), tool_use("t2", "Bash", command="rm x"))])
        # Parallel calls: Claude Code asks about them in order, so the oldest open one comes first.
        self.assertEqual(n.pending_tool()["id"], "t1")
        self.assertEqual([t["id"] for t in n.pending_tools()], ["t1", "t2"])
        self.assertEqual(n.by_id["t1"]["status"], "running")
        self.assertIsNone(n.by_id["t1"]["result"])

    def test_pending_tools_are_the_open_calls_of_the_last_message(self):
        msg = lambda uuid, mid, *blocks: dict(asst(uuid, *blocks), message={"id": mid, "content": list(blocks)})
        n, _ = feed_all([
            msg("a0", "m0", tool_use("old", "Bash", command="sleep 1")),  # never answered (claude was killed)
            user("u1", "继续"),
            msg("a1", "m1", tool_use("rm", "Bash", command="rm -rf build/")),
            msg("a2", "m1", tool_use("ls", "Bash", command="ls")),
        ])
        self.assertEqual([t["id"] for t in n.pending_tools()], ["rm", "ls"])
        n.feed(user("u2", [tool_result("rm", "")]), 999)
        self.assertEqual([t["id"] for t in n.pending_tools()], ["ls"])

    def test_edit_patch_and_write_create(self):
        hunk = {"oldStart": 3, "oldLines": 2, "newStart": 3, "newLines": 2, "lines": [" a", "-b", "+c", " d"]}
        n, _ = feed_all([
            asst("a1", tool_use("e1", "Edit", file_path="/p/f.py", old_string="b", new_string="c"),
                 tool_use("w1", "Write", file_path="/p/new.txt", content="x\ny\n")),
            user("u1", [tool_result("e1", "edited")],
                 toolUseResult={"filePath": "/p/f.py", "structuredPatch": [hunk], "originalFile": "..."}),
            user("u2", [tool_result("w1", "created")],
                 toolUseResult={"type": "create", "filePath": "/p/new.txt", "content": "x\ny\n", "structuredPatch": []}),
        ])
        e = n.by_id["e1"]["result"]
        self.assertEqual(e["filePath"], "/p/f.py")
        self.assertEqual(e["patch"], [hunk])
        w = n.by_id["w1"]["result"]
        self.assertEqual(w["patch"], [{"oldStart": 0, "oldLines": 0, "newStart": 1, "newLines": 2, "lines": ["+x", "+y"]}])

    def test_clipping(self):
        big = "\n".join(f"line {i}" for i in range(500))
        n, _ = feed_all([
            asst("a1", tool_use("t1", "Bash", command="x" * 5000)),
            user("u1", [tool_result("t1", big)], toolUseResult={"stdout": big, "stderr": ""}),
        ])
        ev = n.by_id["t1"]
        self.assertLessEqual(len(ev["input"]["command"]), cr.INPUT_CLIP + 1)
        self.assertEqual(len(ev["result"]["text"].split("\n")), cr.RESULT_LINES)
        self.assertEqual(len(ev["result"]["stdout"].split("\n")), cr.RESULT_LINES)
        self.assertTrue(ev["result"]["truncated"])

    def test_sidechain_skipped_in_main_but_kept_for_agents(self):
        rows = [asst("s1", {"type": "text", "text": "sub"}, isSidechain=True)]
        self.assertEqual(feed_all(rows)[0].events, [])
        self.assertEqual([e["text"] for e in feed_all(rows, sidechain=True)[0].events], ["sub"])

    def test_slash_commands_system_and_queue(self):
        n, changes = feed_all([
            user("u1", "<command-name>/model</command-name>\n<command-args>opus</command-args>"),
            user("u2", "<local-command-stdout>Set model</local-command-stdout>"),
            user("u3", [{"type": "text", "text": "[Request interrupted by user]"}]),
            {"type": "system", "subtype": "compact_boundary", "uuid": "s1", "content": "Conversation compacted"},
            {"type": "system", "subtype": "turn_duration", "uuid": "s2"},
            {"type": "queue-operation", "operation": "enqueue", "content": "下一步"},
            {"type": "queue-operation", "operation": "dequeue"},
        ])
        self.assertEqual([(e["kind"], e["text"]) for e in n.events], [
            ("user", "/model opus"), ("system", "已中断"), ("system", "对话已压缩"), ("queued", "下一步"),
        ])
        self.assertEqual(n.events[-1]["id"], "q500")
        self.assertEqual(changes[-1][0], "patch")
        self.assertTrue(n.events[-1]["done"])

    def test_machine_queued_messages_hidden_but_counted(self):
        q = lambda op, content=None: dict({"type": "queue-operation", "operation": op},
                                          **({"content": content} if content else {}))
        n, changes = feed_all([
            q("enqueue", "<task-notification>\n<task-id>x</task-id>\n</task-notification>"),
            q("enqueue", '<cross-session-message from="uds:/s">hi</cross-session-message>'),
            q("enqueue", "我打的字"),
            q("dequeue"),  # the notification: must not mark the typed one done
            q("dequeue"),
        ])
        self.assertEqual([(e["kind"], e["text"]) for e in n.events], [("queued", "我打的字")])
        self.assertNotIn("done", n.events[0])
        n.feed(q("dequeue"), 999)
        self.assertTrue(n.events[0]["done"])

    def test_todowrite(self):
        n, _ = feed_all([asst("a1", tool_use("t1", "TodoWrite", todos=[
            {"content": "写测试", "status": "in_progress", "activeForm": "正在写测试"},
            {"content": "发布", "status": "weird"},
        ]))])
        self.assertEqual(n.todos, [
            {"content": "写测试", "status": "in_progress", "activeForm": "正在写测试"},
            {"content": "发布", "status": "pending", "activeForm": None},
        ])

    def test_parse_transcript_compat_and_odd_ids(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            write_jsonl(p, [{"type": "ai-title", "aiTitle": "T"}, user("u1", "hi"), "{broken"])
            title, events = cr.parse_transcript(p)
        self.assertEqual(title, "T")
        self.assertEqual([e["text"] for e in events], ["hi"])
        self.assertIsNone(cr.transcript_path("../../etc/passwd"))
        self.assertIsNone(cr.transcript_path("*"))
        self.assertIsNone(cr.transcript_path(None))


# --------------------------------------------------------------------------
# Tail, history paging, index
# --------------------------------------------------------------------------

class TailTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=FIXTURE)
        self.path = os.path.join(self.dir, "t.jsonl")

    def test_partial_lines(self):
        write_jsonl(self.path, [user("u1", "a")])
        tail = cr.TranscriptTail(self.path)
        self.assertEqual([r["uuid"] for _, r in tail.open()], ["u1"])
        line = json.dumps(user("u2", "b"))
        with open(self.path, "a") as f:
            f.write(line[:10])
        self.assertEqual(tail.read_new(), [])
        with open(self.path, "a") as f:
            f.write(line[10:] + "\n" + json.dumps(user("u3", "c"))[:5])
        recs = tail.read_new()
        self.assertEqual([r["uuid"] for _, r in recs], ["u2"])
        # The offset points at the start of the line in the file.
        with open(self.path, "rb") as f:
            f.seek(recs[0][0])
            self.assertTrue(f.readline().startswith(b'{"type": "user", "uuid": "u2"'))

    def test_start_mid_file_skips_partial_line(self):
        write_jsonl(self.path, [user(f"u{i}", "x" * 50) for i in range(20)])
        tail = cr.TranscriptTail(self.path, start_bytes=300)
        recs = tail.open()
        self.assertTrue(recs and tail.start > 0)
        self.assertEqual(recs[-1][1]["uuid"], "u19")
        self.assertTrue(all(isinstance(r, dict) for _, r in recs))

    def test_truncation_and_replace_reset(self):
        write_jsonl(self.path, [user("u1", "a"), user("u2", "b")])
        tail = cr.TranscriptTail(self.path)
        tail.open()
        write_jsonl(self.path, [user("u9", "z")])  # truncated in place
        self.assertIsNone(tail.read_new())
        self.assertEqual([r["uuid"] for _, r in tail.open()], ["u9"])
        other = self.path + ".new"
        write_jsonl(other, [user("u1", "a"), user("u2", "b"), user("u3", "c")])
        os.replace(other, self.path)  # new inode
        self.assertIsNone(tail.read_new())

    def test_load_events_paging(self):
        write_jsonl(self.path, [user(f"u{i}", f"m{i}") for i in range(10)])
        evs, more = cr.load_events(self.path, None, 3)
        self.assertEqual([e["id"] for e in evs], ["u7", "u8", "u9"])
        self.assertTrue(more)
        evs, more = cr.load_events(self.path, "u7", 3)
        self.assertEqual([e["id"] for e in evs], ["u4", "u5", "u6"])
        evs, more = cr.load_events(self.path, "u2", 5)
        self.assertEqual([e["id"] for e in evs], ["u0", "u1"])
        self.assertFalse(more)
        self.assertEqual(cr.load_events(self.path, "nope", 5), ([], False))
        self.assertEqual(cr.load_events(self.path, "u9:3", 5), ([], False))  # a record, but no such event

    def test_unknown_before_is_cheap(self):
        # Records without uuid get offset ids; an unknown id is answered
        # from a byte search, without normalizing the file again and again.
        write_jsonl(self.path, [{"type": "system", "subtype": "compact_boundary"}] * 3 + [user("u1", "x")])
        evs, _ = cr.load_events(self.path, None, 10)
        offs = [e["id"] for e in evs]
        self.assertEqual(offs[0], "o0")
        evs, more = cr.load_events(self.path, offs[2], 10)
        self.assertEqual(([e["id"] for e in evs], more), (offs[:2], False))
        self.assertEqual(cr.load_events(self.path, "o99999999", 10), ([], False))
        calls = []
        orig = cr.Normalizer.feed
        cr.Normalizer.feed = lambda self, *a: calls.append(1) or orig(self, *a)
        try:
            self.assertEqual(cr.load_events(self.path, "toolu_not_here", 10), ([], False))
        finally:
            cr.Normalizer.feed = orig
        self.assertEqual(calls, [])
        self.assertEqual(cr.find_last(self.path, b'"u1"'), Path(self.path).read_bytes().rfind(b'"u1"'))

    def test_index_is_incremental(self):
        write_jsonl(self.path, [
            {"type": "ai-title", "aiTitle": "旧"}, user("u1", "a", permissionMode="default"),
            user("u2", [tool_result("t1", "x", True)]),
        ])
        idx = cr.transcript_index(self.path)
        self.assertEqual((idx.title, idx.mode, idx.results), ("旧", "default", {"t1": True}))
        v = idx.mode_version
        write_jsonl(self.path, [
            {"type": "ai-title", "aiTitle": "新"}, user("u3", "b", permissionMode="plan"),
            user("u4", "<task-notification>\n<task-id>x</task-id>\n<tool-use-id>t9</tool-use-id>\n"
                       "<status>completed</status>\n<summary>完成</summary></task-notification>"),
        ], mode="a")
        idx = cr.transcript_index(self.path)
        self.assertEqual((idx.title, idx.mode, idx.notes), ("新", "plan", {"t9": "completed"}))
        self.assertEqual(idx.mode_version, v + 1)


# --------------------------------------------------------------------------
# Agents and todos
# --------------------------------------------------------------------------

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp(dir=FIXTURE))
        self.transcript = self.proj / "11111111-2222-3333-4444-555555555555.jsonl"
        sdir = self.proj / self.transcript.stem
        sub = sdir / "subagents"
        (sub / "workflows" / "wf_live").mkdir(parents=True)
        (sub / "workflows" / "wf_old").mkdir(parents=True)
        (sdir / "workflows").mkdir()
        (sdir / "workflows" / "wf_old.json").write_text("{}")
        write_jsonl(self.transcript, [
            asst("a1", tool_use("tu_fg_done", "Agent", description="d"), tool_use("tu_fg_run", "Agent"),
                 tool_use("tu_bg", "Agent")),
            user("u1", [tool_result("tu_fg_done", "ok")]),
            user("u2", "<task-notification>\n<tool-use-id>tu_bg</tool-use-id>\n<status>failed</status>\n</task-notification>"),
        ])
        agents = {
            "afgdone": {"description": "查资料", "toolUseId": "tu_fg_done", "requestShape": "foreground"},
            "afgrun": {"description": "写代码", "toolUseId": "tu_fg_run", "requestShape": "foreground"},
            "abg": {"description": "后台", "toolUseId": "tu_bg", "requestShape": "background"},
        }
        for aid, meta in agents.items():
            (sub / f"agent-{aid}.meta.json").write_text(json.dumps(meta, ensure_ascii=False))
            write_jsonl(sub / f"agent-{aid}.jsonl", [
                user("x1", "go", isSidechain=True),
                asst("x2", {"type": "text", "text": "先看看"}, tool_use("k1", "Grep", pattern="TODO\nmore"), isSidechain=True),
            ])
        # Background agents without a logged notification: one handed back a
        # minute ago (done), one still mid-tool (running), one silent for an hour (done).
        for aid, last, age in (("abghand", tool_use("h", "SubagentHandback", summary="x"), 60),
                               ("abgbusy", tool_use("b", "Bash", command="make"), 60),
                               ("abgstale", tool_use("b", "Bash", command="sleep 9999"), 3600)):
            (sub / f"agent-{aid}.meta.json").write_text(json.dumps({"description": aid, "toolUseId": "tu_" + aid,
                                                                    "requestShape": "background"}))
            write_jsonl(sub / f"agent-{aid}.jsonl", [asst("p1", {"type": "text", "text": "报告"}, isSidechain=True),
                                                    asst("p2", last, isSidechain=True)])
            os.utime(sub / f"agent-{aid}.jsonl", (time.time() - age, time.time() - age))
        write_jsonl(sub / "workflows" / "wf_live" / "journal.jsonl", [
            {"type": "launched"},
            {"type": "started", "agentId": "w1", "label": "调研", "phase": "第一步"},
            {"type": "started", "agentId": "w2", "label": "整理", "phase": "第一步"},
            {"type": "started", "agentId": "w3", "label": "失败的", "phase": "第二步"},
            {"type": "result", "agentId": "w2", "result": "..."},
            {"type": "failed", "agentId": "w3"},
        ])
        write_jsonl(sub / "workflows" / "wf_live" / "agent-w1.jsonl",
                    [asst("y1", {"type": "text", "text": "正在读 " + "长" * 300}, isSidechain=True)])
        write_jsonl(sub / "workflows" / "wf_old" / "journal.jsonl",
                    [{"type": "started", "agentId": "o1", "label": "旧的", "phase": None}])

    def test_discovery(self):
        agents, running = cr.discover_agents(str(self.transcript))
        pub = {a["id"]: a for a in cr.public_agents(agents)}
        self.assertEqual(running, 3)
        self.assertEqual({k: a["status"] for k, a in pub.items()}, {
            "afgdone": "done", "afgrun": "running", "abg": "failed",
            "abghand": "done", "abgbusy": "running", "abgstale": "done",
            "w1": "running", "w2": "done", "w3": "failed", "o1": "failed",
        })
        self.assertEqual([a["status"] for a in agents[:3]], ["running"] * 3)
        self.assertEqual(pub["abghand"]["lastAction"], "报告")  # the hand-back call is not an action
        self.assertEqual(pub["afgrun"]["lastAction"], "Grep · TODO more")
        self.assertEqual(pub["afgrun"]["kind"], "task")
        self.assertEqual(pub["afgrun"]["label"], "写代码")
        self.assertEqual(pub["w1"]["lastAction"], "正在读 " + "长" * 116)
        self.assertEqual((pub["w1"]["kind"], pub["w1"]["phase"], pub["w1"]["runId"]), ("workflow", "第一步", "wf_live"))
        self.assertIsInstance(pub["w1"]["updatedAt"], int)
        self.assertNotIn("_path", pub["w1"])
        quick, n = cr.discover_agents(str(self.transcript), full=False)
        self.assertEqual((n, sorted(a["id"] for a in quick)), (3, ["abgbusy", "afgrun", "w1"]))
        self.assertTrue(cr.find_agent_file(str(self.transcript), "w1").endswith("agent-w1.jsonl"))
        self.assertIsNone(cr.find_agent_file(str(self.transcript), "../x"))

    def test_foreground_agent_of_a_dead_claude_is_not_running_forever(self):
        sub = self.proj / self.transcript.stem / "subagents"
        old = time.time() - cr.AGENT_STALE_MS / 1000 - 60
        os.utime(sub / "agent-afgrun.jsonl", (old, old))
        agents, _ = cr.discover_agents(str(self.transcript))
        self.assertEqual({a["id"]: a["status"] for a in agents}["afgrun"], "done")
        agents, running = cr.discover_agents(str(self.transcript), alive=False)
        self.assertEqual(running, 0)
        self.assertEqual({a["id"]: a["status"] for a in agents}["abgbusy"], "failed")

    def test_tasks_dir_todos(self):
        sid = "99999999-2222-3333-4444-555555555555"
        d = CLAUDE / "tasks" / sid
        d.mkdir(parents=True)
        (d / "2.json").write_text(json.dumps({"id": "2", "subject": "二", "status": "pending"}))
        (d / "10.json").write_text(json.dumps({"id": "10", "subject": "十", "status": "completed", "activeForm": "做十"}))
        (d / "1.json").write_text(json.dumps({"id": "1", "subject": "一", "status": "deleted"}))
        self.assertEqual(cr.tasks_todos(sid), [
            {"content": "二", "status": "pending", "activeForm": None},
            {"content": "十", "status": "completed", "activeForm": "做十"},
        ])
        self.assertEqual(cr.tasks_todos("../../x"), [])


# --------------------------------------------------------------------------
# Prompts read from the pane
# --------------------------------------------------------------------------

BASH_DIALOG = """\
● I'll check the repository state first.

╭──────────────────────────────────────────────────────────────────────────────╮
│ Bash command                                                                 │
│                                                                              │
│   git status                                                                 │
│   Show working tree status                                                   │
│                                                                              │
│ Do you want to proceed?                                                      │
│ ❯ 1. Yes                                                                     │
│   2. Yes, and don't ask again for git status commands in /x                  │
│   3. No, and tell Claude what to do differently (esc)                        │
╰──────────────────────────────────────────────────────────────────────────────╯
"""

BARE_DIALOG = """\
1. earlier list item
2. another one

────────────────────────────────────────────────────────────────────────────────
 Edit file
 src/app.py
 Do you want to make this edit to app.py?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No, and tell Claude what to do differently (esc)

"""


class PaneTests(unittest.TestCase):
    def test_bash_permission_options(self):
        q, opts = cr.parse_pane_options(BASH_DIALOG)
        self.assertEqual(q, "Do you want to proceed?")
        self.assertEqual(opts, [
            {"key": "1", "label": "Yes"},
            {"key": "2", "label": "Yes, and don't ask again for git status commands in /x"},
            {"key": "3", "label": "No, and tell Claude what to do differently (esc)"},
        ])

    def test_unboxed_dialog_wins_over_earlier_list(self):
        q, opts = cr.parse_pane_options(BARE_DIALOG)
        self.assertEqual(q, "Edit file src/app.py Do you want to make this edit to app.py?")
        self.assertEqual([o["key"] for o in opts], ["1", "2", "3"])
        self.assertTrue(opts[1]["label"].startswith("Yes, allow all edits"))
        self.assertEqual(cr.parse_pane_options("just text\n> prompt"), (None, []))

    def test_pending(self):
        item = {"status": "waiting", "waitingFor": "approve Bash"}
        tool = {"name": "Bash", "input": {"command": "git status"}}
        p = cr.build_pending(item, tool, BASH_DIALOG)
        self.assertEqual((p["kind"], p["tool"], p["question"]), ("permission", "Bash", "Do you want to proceed?"))
        self.assertEqual(len(p["options"]), 3)
        self.assertEqual(p["raw"].splitlines()[-1][0], "╰")
        self.assertLessEqual(len(p["raw"].splitlines()), 20)
        self.assertEqual(p["rawHash"], cr.pane_raw(BASH_DIALOG)[1])
        self.assertEqual(len(p["rawHash"]), 16)
        ask = {"name": "AskUserQuestion", "input": {"questions": [
            {"question": "用哪个库？", "options": [{"label": "requests"}, {"label": "urllib"}]}]}}
        p = cr.build_pending(item, ask, None)
        self.assertEqual((p["kind"], p["question"], p["raw"]), ("question", "用哪个库？", ""))
        self.assertEqual(p["options"], [{"key": "1", "label": "requests"}, {"key": "2", "label": "urllib"}])
        p = cr.build_pending(item, None, None)
        self.assertEqual((p["kind"], p["options"]), ("dialog", []))
        self.assertIsNone(cr.build_pending({"status": "busy"}, tool, BASH_DIALOG))

    def test_mode_detection(self):
        rule = "─" * 40
        box = f"╭{rule}╮\n│ >  │\n╰{rule}╯\n"
        self.assertEqual(cr.detect_mode(box + "  ? for shortcuts\n"), "default")
        self.assertEqual(cr.detect_mode(box + "  ⏵⏵ accept edits on (shift+tab to cycle)\n"), "acceptEdits")
        self.assertEqual(cr.detect_mode(box + "  ⏸ plan mode on (shift+tab to cycle)\n"), "plan")
        self.assertEqual(cr.detect_mode(box + "  ⏵⏵ auto mode on (shift+tab to cycle)\n"), "auto")
        self.assertEqual(cr.detect_mode(box + "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"), "bypassPermissions")
        self.assertEqual(cr.detect_mode(f"{rule}\n> hi\n{rule}\n  ⏸ plan mode on (shift+tab to cycle)\n\n"), "plan")
        # Only the footer counts, not conversation text far above it.
        self.assertEqual(cr.detect_mode("plan mode on\n" + "x\n" * 10 + box), "default")
        # Text in the prompt or the last output lines cannot fake the mode.
        spoof = f"● ⏵⏵ bypass permissions on (shift+tab to cycle)\n{rule}\n> bypass permissions on\n{rule}\n"
        self.assertEqual(cr.detect_mode(spoof + "  ? for shortcuts\n"), "default")
        self.assertEqual(cr.detect_mode(spoof + "  ⏵⏵ accept edits on (shift+tab to cycle)\n"), "acceptEdits")
        self.assertEqual(cr.detect_mode(box + "  plan mode on is what I typed\n"), "default")
        # No input box on screen (a dialog replaced it): unknown, not "default".
        self.assertIsNone(cr.detect_mode(BASH_DIALOG))
        self.assertIsNone(cr.detect_mode(BARE_DIALOG))
        self.assertIsNone(cr.detect_mode("plan mode on (shift+tab to cycle)"))

    def test_menu_on_screen(self):
        rule = "─" * 40
        self.assertTrue(cr.menu_on_screen(BASH_DIALOG))
        self.assertTrue(cr.menu_on_screen(BARE_DIALOG))
        # A numbered list in Claude's answer, with the input box under it, is not a prompt.
        idle = f"● Steps:\n  1. build\n  2. test\n\n{rule}\n> \n{rule}\n  ? for shortcuts\n"
        self.assertFalse(cr.menu_on_screen(idle))
        self.assertFalse(cr.menu_on_screen(f"{rule}\n> \n{rule}\n"))

    def test_permission_card_names_the_call_on_screen(self):
        item = {"status": "waiting", "waitingFor": "approve Bash"}
        rm = {"id": "t1", "name": "Bash", "input": {"command": "rm -rf build/"}}
        ls = {"id": "t2", "name": "Bash", "input": {"command": "ls"}}
        git = {"id": "t3", "name": "Bash", "input": {"command": "git status"}}
        # The screen asks about git status: that call is named, not the last one.
        p = cr.build_pending(item, [git, rm], BASH_DIALOG)
        self.assertEqual((p["tool"], p["input"]["command"], p["ambiguous"]), ("Bash", "git status", False))
        # Two open calls and the screen shows neither clearly: no tool-specific card.
        p = cr.build_pending(item, [rm, ls], "Do you want to proceed?\n❯ 1. Yes\n  2. No\n")
        self.assertEqual((p["tool"], p["input"], p["kind"], p["ambiguous"]), (None, None, "dialog", True))
        self.assertEqual(len(p["options"]), 2)
        # A sub-agent (Task) is running: its own calls ask too, so do not describe the Task.
        task = {"id": "t4", "name": "Task", "input": {"description": "重构"}}
        p = cr.build_pending(item, [task], BASH_DIALOG)
        self.assertEqual((p["tool"], p["ambiguous"]), (None, True))
        p = cr.build_pending(item, [task, git], BASH_DIALOG)
        self.assertEqual((p["tool"], p["ambiguous"]), ("Bash", False))
        # Without a screen (read-only session) a single call is still named.
        self.assertEqual(cr.build_pending(item, [rm], None)["tool"], "Bash")
        self.assertIsNone(cr.build_pending(item, [rm, ls], None)["tool"])

    def test_multi_question_uses_the_question_on_screen(self):
        item = {"status": "waiting", "waitingFor": "answer"}
        ask = {"id": "q", "name": "AskUserQuestion", "input": {"questions": [
            {"question": "用哪个数据库？", "options": [{"label": "Postgres"}, {"label": "SQLite"}]},
            {"question": "缓存放在哪里？", "options": [{"label": "内存"}, {"label": "用 Redis"}], "multiSelect": True}]}}
        screen = ("☒ 数据库  ☐ 缓存  ✔ Submit\n\n缓存放在哪里？\n\n❯ 1. [ ] 内存\n     进程内\n"
                  "  2. [ ] 用 Redis\n  3. Type something.\n")
        p = cr.build_pending(item, [ask], screen)
        self.assertEqual((p["kind"], p["question"], p["multiSelect"]), ("question", "缓存放在哪里？", True))
        self.assertEqual([o["label"] for o in p["options"]], ["[ ] 内存", "[ ] 用 Redis", "Type something."])
        # No screen: one question is shown from the transcript, several are not guessed.
        p = cr.build_pending(item, [ask], None)
        self.assertEqual(p["options"], [])
        self.assertIn("2 个问题", p["question"])


class BindTests(unittest.TestCase):
    def test_private_hosts(self):
        for h in ("127.0.0.1", "localhost", "::1", "100.84.114.85", "fd7a:115c:a1e0::1"):
            self.assertTrue(cr.host_is_private(h), h)
        for h in ("0.0.0.0", "192.168.1.5", "8.8.8.8", "::", "example.com"):
            self.assertFalse(cr.host_is_private(h), h)


# --------------------------------------------------------------------------
# HTTP plumbing (fake app)
# --------------------------------------------------------------------------

class FakeApp:
    """Just enough of App for the HTTP layer."""

    def __init__(self):
        self.cfg = {"token": "secret-token", "ntfy": {}}
        self.tmux = cr.Tmux()
        self.claude = self.cswap = None
        self.lock = threading.Lock()
        self.item = {"key": "t:%1", "status": "waiting", "waitingFor": "批准 Bash … 中文",
                     "controllable": True, "sessionId": "x"}

    def sessions(self):
        return [self.item]

    def events(self, key, before=None, limit=100):
        tag = cr.hashlib.sha1(json.dumps([self.item, before, limit], ensure_ascii=False).encode()).hexdigest()[:20]
        return self.item, "t", [], False, None, f'"{tag}"', {}


def request(port, method, path, body=None, headers=None, token="secret-token"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    if isinstance(body, dict):
        body = json.dumps(body)
        h["Content-Type"] = "application/json"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = FakeApp()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cr.make_handler(cls.app))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, *a, **kw):
        return request(self.port, *a, **kw)

    def test_requires_token(self):
        self.assertEqual(self.req("GET", "/api/sessions", token=None)[0].status, 401)
        self.assertEqual(self.req("GET", "/api/sessions", token="wrong")[0].status, 401)
        self.assertEqual(self.req("GET", "/api/sessions")[0].status, 200)
        self.assertEqual(self.req("GET", "/api/session/t%3A%251/stream", token=None)[0].status, 401)

    def test_static_traversal_blocked_and_csp(self):
        self.assertEqual(self.req("GET", "/../cswap_remote.py", token=None)[0].status, 404)
        self.assertEqual(self.req("GET", "/%2e%2e/cswap_remote.py", token=None)[0].status, 404)
        r, _ = self.req("GET", "/", token=None)
        self.assertEqual(r.status, 200)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("script-src 'self';", csp)
        self.assertNotIn("unsafe-inline';", csp.split("script-src")[1].split(";")[0] + ";")
        self.assertIn("style-src 'self';", csp)
        self.assertNotIn("unsafe-inline", csp)

    def test_etag_is_ascii_even_with_cjk_waiting_for(self):
        r, data = self.req("GET", "/api/session/t%3A%251/events")
        self.assertEqual(r.status, 200)
        etag = r.getheader("ETag")
        self.assertTrue(etag.isascii() and etag.startswith('"'))
        body = json.loads(data)
        self.assertEqual(body["session"]["waitingFor"], "批准 Bash … 中文")
        self.assertIs(body["more"], False)
        self.assertIn("pending", body)
        r2, _ = self.req("GET", "/api/session/t%3A%251/events", headers={"If-None-Match": etag})
        self.assertEqual(r2.status, 304)
        self.assertEqual(self.req("GET", "/api/session/t%3A%251/events?limit=x")[0].status, 400)

    def test_bad_content_length(self):
        r, _ = self.req("POST", "/api/sessions", body=b"", headers={"Content-Length": "-1"})
        self.assertEqual(r.status, 400)
        r, _ = self.req("POST", "/api/sessions", body=b"x" * 10, headers={"Content-Length": "999999"})
        self.assertEqual(r.status, 400)

    def test_rejected_requests_do_not_read_the_body(self):
        # Announce a big body but never send it: a server that read it first
        # would hang until its timeout instead of answering at once.
        for path, token, status in (("/api/sessions", None, 401), ("/api/sessions", "wrong", 401),
                                    ("/index.html", None, 405)):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.putrequest("POST", path)
            c.putheader("Content-Length", "200000")
            if token:
                c.putheader("Authorization", "Bearer " + token)
            c.endheaders()
            t0 = time.time()
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, status, path)
            self.assertLess(time.time() - t0, 2)
            self.assertEqual(r.getheader("Connection"), "close")
            c.close()

    def test_connection_cap(self):
        old = cr.MAX_CONNECTIONS
        cr.MAX_CONNECTIONS = 1
        try:
            httpd = cr.Server(("127.0.0.1", 0), cr.make_handler(self.app))
        finally:
            cr.MAX_CONNECTIONS = old
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        idle = socket.create_connection(("127.0.0.1", port), timeout=5)  # holds the only slot
        time.sleep(0.2)
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.assertEqual(extra.recv(10), b"")  # closed at once
        extra.close()
        idle.close()
        for _ in range(50):  # the slot comes back once the idle connection is gone
            try:
                if request(port, "GET", "/api/sessions")[0].status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            self.fail("slot not released")

    def test_key_allowlist(self):
        r, _ = self.req("POST", "/api/session/t%3A%251/key", body={"key": "q; rm -rf ~"})
        self.assertEqual(r.status, 400)


# --------------------------------------------------------------------------
# Real App against the fixture (SSE end to end)
# --------------------------------------------------------------------------

class SSE:
    """Reads a /stream response the way the page does (fetch + stream)."""

    def __init__(self, port, key, token="tok", query=""):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        if "?" in key:
            key, query = key.split("?", 1)
            query = "?" + query
        self.conn.request("GET", f"/api/session/{key}/stream{query}", headers={"Authorization": "Bearer " + token})
        self.sock = self.conn.sock  # the response takes over the socket
        self.resp = self.conn.getresponse()

    def next(self, skip_ping=False, want=None):
        while True:
            line = self.resp.fp.readline()
            if not line:
                raise EOFError("stream closed")
            line = line.decode("utf-8").rstrip("\n")
            if not line.startswith("data: "):
                continue
            msg = json.loads(line[6:])
            if skip_ping and msg["type"] == "ping":
                continue
            if want and msg["type"] not in want:
                continue
            return msg

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.resp.close()
        self.conn.close()


def wait_streams_free(app, timeout=10):
    """Watchers notice a closed client at their next write (ping) and
    free their slot; wait until all slots are free again."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = 0
        while got < cr.MAX_STREAMS and app.streams.acquire(blocking=False):
            got += 1
        for _ in range(got):
            app.streams.release()
        if got == cr.MAX_STREAMS:
            return True
        time.sleep(0.2)
    return False


def start_process(cmd):
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if cr.proc_start_ticks(p.pid) is not None:
            break
        time.sleep(0.02)
    return p


def write_session(pid, sid, status="busy", **extra):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    d = {"pid": pid, "sessionId": sid, "cwd": FIXTURE, "procStart": str(cr.proc_start_ticks(pid)),
         "kind": "interactive", "entrypoint": "cli", "name": "fixture", "status": status,
         "updatedAt": int(time.time() * 1000)}
    d.update(extra)
    tmp = SESSIONS / f"{pid}.json.tmp"
    tmp.write_text(json.dumps(d))
    os.replace(tmp, SESSIONS / f"{pid}.json")


class LiveServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_ping = cr.PING_INTERVAL
        cr.PING_INTERVAL = 1
        cls.app = cr.App({"token": "tok", "ntfy": {}})
        cls.port = free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), cr.make_handler(cls.app))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cr.PING_INTERVAL = cls.old_ping

    def setUp(self):
        self.sid = "aaaaaaaa-0000-4000-8000-%012d" % (int(time.time() * 1000) % 10 ** 12)
        self.proj = PROJECTS / "-fixture-proj"
        self.proj.mkdir(parents=True, exist_ok=True)
        self.transcript = self.proj / f"{self.sid}.jsonl"
        write_jsonl(self.transcript, [
            {"type": "ai-title", "aiTitle": "修复登录"},
            user("u1", "帮我修一下", permissionMode="acceptEdits"),
            asst("a1", {"type": "text", "text": "好的"}),
        ])
        sub = self.proj / self.sid / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-ag1.meta.json").write_text(json.dumps({"description": "子任务", "toolUseId": "tu_x"}))
        write_jsonl(sub / "agent-ag1.jsonl", [asst("z1", tool_use("k", "Bash", command="make"), isSidechain=True)])
        tr = self.proj / self.sid / "tool-results"
        tr.mkdir()
        (tr / "tu_big.txt").write_text("FULL OUTPUT\n" * 3)
        self.proc = start_process(["sleep", "600"])
        write_session(self.proc.pid, self.sid)
        self.streams = []

    def tearDown(self):
        for s in self.streams:
            s.close()
        self.assertTrue(wait_streams_free(self.app))
        self.proc.kill()
        self.proc.wait()
        for f in SESSIONS.glob("*.json"):
            f.unlink()

    def req(self, method, path, body=None):
        r, data = request(self.port, method, path, body=body, token="tok")
        return r.status, (json.loads(data) if data else None)

    def key(self):
        _, body = self.req("GET", "/api/sessions")
        item = next(s for s in body["sessions"] if s["pid"] == self.proc.pid)
        return item, urllib_quote(item["key"])

    def open(self, key):
        s = SSE(self.port, key)
        self.streams.append(s)
        return s

    def test_session_list_fields(self):
        item, _ = self.key()
        self.assertEqual((item["title"], item["mode"], item["agentsRunning"]), ("修复登录", "acceptEdits", 1))
        self.assertFalse(item["controllable"])

    def test_stream_end_to_end(self):
        item, key = self.key()
        s = self.open(key)
        self.assertEqual(s.resp.status, 200)
        self.assertTrue(s.resp.getheader("Content-Type").startswith("text/event-stream"))
        snap = s.next()
        self.assertEqual(snap["type"], "snapshot")
        self.assertEqual([e["id"] for e in snap["events"]], ["u1", "a1:0"])
        self.assertFalse(snap["more"])
        self.assertIsNone(snap["pending"])
        self.assertEqual(snap["todos"], [])
        self.assertEqual([(a["id"], a["status"], a["lastAction"]) for a in snap["agents"]],
                         [("ag1", "running", "Bash · make")])
        self.assertEqual(snap["session"]["key"], item["key"])

        write_jsonl(self.transcript, [user("u2", "再试试")], mode="a")
        msg = s.next(skip_ping=True)
        self.assertEqual((msg["type"], msg["event"]["id"], msg["event"]["text"]), ("event", "u2", "再试试"))

        # A line written in two pieces arrives once, whole.
        line = json.dumps(asst("a2", tool_use("tu_1", "Bash", command="pytest")))
        with open(self.transcript, "a") as f:
            f.write(line[:20])
        time.sleep(0.5)
        with open(self.transcript, "a") as f:
            f.write(line[20:] + "\n")
        msg = s.next(skip_ping=True)
        self.assertEqual((msg["type"], msg["event"]["kind"], msg["event"]["status"]), ("event", "tool", "running"))

        write_jsonl(self.transcript, [user("u3", [tool_result("tu_1", "2 passed")],
                                           toolUseResult={"stdout": "2 passed", "stderr": ""})], mode="a")
        msg = s.next(skip_ping=True)
        self.assertEqual((msg["type"], msg["event"]["id"], msg["event"]["status"]), ("patch", "tu_1", "ok"))
        self.assertEqual(msg["event"]["result"]["stdout"], "2 passed")

        # The sub-agent's tool_result lands: agents update.
        write_jsonl(self.transcript, [user("u4", [tool_result("tu_x", "done")])], mode="a")
        msg = s.next(want={"agents"})
        self.assertEqual(msg["agents"][0]["status"], "done")

        write_jsonl(self.transcript, [asst("a3", tool_use("tu_2", "TodoWrite", todos=[
            {"content": "一", "status": "in_progress", "activeForm": "做一"}]))], mode="a")
        msg = s.next(want={"todos"})
        self.assertEqual(msg["todos"][0]["content"], "一")

        write_session(self.proc.pid, self.sid, status="waiting", waitingFor="approve Bash")
        msg = s.next(want={"status"})
        self.assertEqual(msg["session"]["status"], "waiting")
        # Non-tmux: no pane to read, so no options; the open tool_use is named.
        self.assertEqual((msg["pending"]["kind"], msg["pending"]["tool"], msg["pending"]["options"], msg["pending"]["raw"]),
                         ("permission", "TodoWrite", [], ""))

        self.assertEqual(s.next(want={"ping"})["type"], "ping")

        self.proc.kill()
        self.proc.wait()
        self.assertEqual(s.next(want={"gone"})["type"], "gone")
        with self.assertRaises(EOFError):
            s.next()

    def test_ninth_stream_is_refused(self):
        _, key = self.key()
        opened = []
        for _ in range(cr.MAX_STREAMS):
            s = self.open(key)
            self.assertEqual(s.next()["type"], "snapshot")
            opened.append(s)
        r, data = request(self.port, "GET", f"/api/session/{key}/stream", token="tok")
        self.assertEqual(r.status, 503)
        self.assertIn("error", json.loads(data))
        for s in opened:
            s.close()
        self.assertTrue(wait_streams_free(self.app))
        s = self.open(key)
        self.assertEqual(s.resp.status, 200)

    def test_history_agent_and_toolresult_endpoints(self):
        write_jsonl(self.transcript, [user(f"h{i}", f"m{i}") for i in range(5)] + [
            asst("a9", tool_use("tu_big", "Bash", command="seq 1 100000")),
            user("u9", [tool_result("tu_big", "Output too large. Full output saved to: /etc/passwd.txt")]),
            asst("a10", tool_use("tu_small", "Bash", command="echo hi")),
            user("u10", [tool_result("tu_small", "hi")], toolUseResult={"stdout": "hi", "stderr": "warn"}),
        ], mode="a")
        _, key = self.key()
        st, body = self.req("GET", f"/api/session/{key}/events?before=h3&limit=2")
        self.assertEqual(st, 200)
        self.assertEqual([e["id"] for e in body["events"]], ["h1", "h2"])
        self.assertTrue(body["more"])
        st, body = self.req("GET", f"/api/session/{key}/agent/ag1/events")
        self.assertEqual(st, 200)
        self.assertEqual(body["agent"]["label"], "子任务")
        self.assertEqual([e["kind"] for e in body["events"]], ["tool"])
        self.assertEqual(self.req("GET", f"/api/session/{key}/agent/nope/events")[0], 404)
        self.assertEqual(self.req("GET", f"/api/session/{key}/toolresult/tu_big"), (200, {"text": "FULL OUTPUT\n" * 3}))
        self.assertEqual(self.req("GET", f"/api/session/{key}/toolresult/tu_small"), (200, {"text": "hi\nwarn"}))
        self.assertEqual(self.req("GET", f"/api/session/{key}/toolresult/..%2Fx")[0], 400)
        self.assertEqual(self.req("GET", f"/api/session/{key}/toolresult/tu_none")[0], 404)
        # Unknown `before`: an empty last page at once.
        self.assertEqual(self.req("GET", f"/api/session/{key}/events?before=nope&limit=500")[1]["events"], [])
        # The newest page carries agents and todos for the polling fallback.
        st, body = self.req("GET", f"/api/session/{key}/events")
        self.assertEqual(([a["id"] for a in body["agents"]], body["todos"]), (["ag1"], []))
        # Read-only (non-tmux) sessions refuse control actions.
        self.assertEqual(self.req("POST", f"/api/session/{key}/interrupt", {})[0], 409)
        self.assertEqual(self.req("POST", f"/api/session/{key}/answer", {"choice": "1", "rawHash": ""})[0], 409)
        self.assertEqual(self.req("POST", f"/api/session/{key}/mode", {"target": "bypassPermissions"})[0], 400)

    def test_inline_tool_output_is_clipped(self):
        write_jsonl(self.transcript, [
            asst("c1", tool_use("tu_inline", "Bash", command="yes")),
            user("c2", [tool_result("tu_inline", "y")], toolUseResult={"stdout": "y\n" * 5000, "stderr": ""}),
            user("c3", "后面还有别的记录 tu_inline"),
        ], mode="a")
        old = cr.TOOL_RESULT_MAX
        cr.TOOL_RESULT_MAX = 1000
        try:
            _, key = self.key()
            st, body = self.req("GET", f"/api/session/{key}/toolresult/tu_inline")
        finally:
            cr.TOOL_RESULT_MAX = old
        self.assertEqual(st, 200)
        self.assertTrue(body["text"].startswith("y\ny\n"))
        self.assertLess(len(body["text"]), 1100)
        self.assertTrue(body["text"].endswith("(+9000)"))

    def test_reconnect_from_the_same_page_replaces_its_old_stream(self):
        _, key = self.key()
        old = SSE(self.port, key, query="?c=page-aaaaaaaa")
        self.streams.append(old)
        self.assertEqual(old.next()["type"], "snapshot")
        new = self.open(key + "?c=page-aaaaaaaa")
        self.assertEqual(new.next()["type"], "snapshot")
        with self.assertRaises(EOFError):  # the old one ends without another message
            old.next(skip_ping=True)
        other = self.open(key + "?c=page-bbbbbbbb")  # another page keeps its own
        self.assertEqual(other.next()["type"], "snapshot")
        self.assertEqual(new.next(want={"ping"})["type"], "ping")

    def test_saved_output_outside_project_is_not_read(self):
        outside = Path(FIXTURE) / "secret.txt"
        outside.write_text("SECRET")
        write_jsonl(self.transcript, [
            asst("b1", tool_use("tu_out", "Bash", command="x")),
            user("b2", [tool_result("tu_out", f"Full output saved to: {outside}")]),
        ], mode="a")
        _, key = self.key()
        st, body = self.req("GET", f"/api/session/{key}/toolresult/tu_out")
        self.assertEqual(st, 200)
        self.assertNotIn("SECRET", body["text"].replace(str(outside), ""))


# --------------------------------------------------------------------------
# tmux-hosted session (cr-fixture-* running cat)
# --------------------------------------------------------------------------

@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class ResumeTests(unittest.TestCase):
    """Continuing a closed conversation in tmux (claude --resume)."""

    def setUp(self):
        self.proj = PROJECTS / "-fixture-resume"
        self.proj.mkdir(parents=True, exist_ok=True)
        self.cwd = tempfile.mkdtemp(dir=FIXTURE)
        self.sid = "bbbbbbbb-0000-4000-8000-%012d" % (int(time.time() * 1000) % 10 ** 12)
        self.path = self.proj / f"{self.sid}.jsonl"
        write_jsonl(self.path, [
            user("u1", "第一句话", cwd=self.cwd),
            asst("a1", {"type": "text", "text": "好"}),
            {"type": "last-prompt", "lastPrompt": "最后一句"},
        ])
        self.app = cr.App({"token": "tok", "ntfy": {}})
        self.calls = []
        self.app.new_session = lambda cwd, name, resume, claude_args=None: (
            self.calls.append((cwd, claude_args)) or {"key": "t:%9", "tmux": "cr-x"})
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        for f in SESSIONS.glob("*.json"):
            f.unlink()
        shutil.rmtree(self.proj, True)

    def test_title_order(self):
        info = cr.conversation_info(str(self.path))
        self.assertEqual(info["cwd"], self.cwd)
        self.assertTrue(info["hasUser"])
        self.assertEqual(info["title"], "最后一句")
        write_jsonl(self.path, [{"type": "ai-title", "aiTitle": "AI 标题"}], mode="a")
        os.utime(self.path, (time.time() + 5, time.time() + 5))
        self.assertEqual(cr.conversation_info(str(self.path))["title"], "AI 标题")
        write_jsonl(self.path, [{"type": "custom-title", "customTitle": "我起的名字"}], mode="a")
        os.utime(self.path, (time.time() + 10, time.time() + 10))
        self.assertEqual(cr.conversation_info(str(self.path))["title"], "我起的名字")

    def test_recent_lists_closed_conversations_only(self):
        ids = [c["sessionId"] for c in self.app.recent_conversations()]
        self.assertIn(self.sid, ids)
        c = next(c for c in self.app.recent_conversations() if c["sessionId"] == self.sid)
        self.assertTrue(c["cwdExists"])
        proc = start_process(["sleep", "600"])
        self.procs.append(proc)
        write_session(proc.pid, self.sid)
        self.assertNotIn(self.sid, [c["sessionId"] for c in self.app.recent_conversations()])

    def test_resume_refuses_while_open_elsewhere(self):
        proc = start_process(["sleep", "600"])
        self.procs.append(proc)
        write_session(proc.pid, self.sid)
        with self.assertRaises(cr.ApiError) as e:
            self.app.resume_conversation(self.sid)
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.calls, [])

    def test_resume_runs_claude_resume_in_the_conversation_dir(self):
        r = self.app.resume_conversation(self.sid)
        self.assertEqual(r["key"], "t:%9")
        self.assertEqual(self.calls, [(self.cwd, ["--resume", self.sid])])

    def test_resume_rejects_unknown_and_bad_ids(self):
        for sid in ("cccccccc-0000-4000-8000-000000000000", "../../etc/passwd", ""):
            with self.assertRaises(cr.ApiError) as e:
                self.app.resume_conversation(sid)
            self.assertEqual(e.exception.status, 404)
        self.assertEqual(self.calls, [])


class TmuxTests(unittest.TestCase):
    """A cr-fixture-* tmux session running tests/fake_tui.py, which draws a
    permission dialog (or an input box with a mode footer) and logs every
    answer and submitted line, so the tests see what would reach claude."""

    NAME = "cr-fixture-%d" % os.getpid()

    @classmethod
    def setUpClass(cls):
        cls.app = cr.App({"token": "tok", "ntfy": {}})
        cls.port = free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), cr.make_handler(cls.app))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        script = os.path.join(FIXTURE, "dialog.txt")
        Path(script).write_text(BASH_DIALOG, encoding="utf-8")
        self.log = os.path.join(FIXTURE, "tui-%d.log" % time.time_ns())
        tui = str(Path(__file__).resolve().parent / "fake_tui.py")
        cmd = f"exec {shlex_quote(sys.executable)} {shlex_quote(tui)} {shlex_quote(self.log)} {shlex_quote(script)}"
        out = subprocess.run(["tmux", "new-session", "-d", "-P", "-F", "#{pane_id} #{pane_pid}", "-s", self.NAME,
                              "-x", "100", "-y", "30", cmd],
                             capture_output=True, text=True, check=True).stdout.split()
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", "=" + self.NAME], capture_output=True)
        self.pane, self.pid = out[0], int(out[1])
        for _ in range(50):
            panes = {p["pane"]: p for p in self.app.tmux.panes()}
            if panes.get(self.pane, {}).get("command") not in (None, "", "sh", "bash") and \
                    "Do you want" in self.app.tmux.capture(self.pane, None):
                break
            time.sleep(0.1)
        self.sid = "bbbbbbbb-0000-4000-8000-%012d" % (int(time.time() * 1000) % 10 ** 12)
        proj = PROJECTS / "-fixture-tmux"
        proj.mkdir(parents=True, exist_ok=True)
        write_jsonl(proj / f"{self.sid}.jsonl", [asst("a1", tool_use("tu_git", "Bash", command="git status"))])
        self.status("waiting", waitingFor="approve Bash")
        self.addCleanup(lambda: [f.unlink() for f in SESSIONS.glob("*.json")])
        self.key = urllib_quote(f"t:{self.pane}")
        self.app.answered.clear()  # pane ids repeat once tmux restarts between tests

    def status(self, status, **extra):
        write_session(self.pid, self.sid, status=status, **extra)

    def req(self, method, path, body=None):
        r, data = request(self.port, method, path, body=body, token="tok")
        return r.status, (json.loads(data) if data else None)

    def logged(self):
        time.sleep(0.3)
        try:
            return Path(self.log).read_text("utf-8").splitlines()
        except FileNotFoundError:
            return []

    def screen(self):
        return self.app.tmux.capture(self.pane, None)

    def wait_screen(self, text):
        for _ in range(30):
            if text in self.screen():
                return True
            time.sleep(0.1)
        return False

    def test_events_carry_pending_for_polling(self):
        st, body = self.req("GET", f"/api/session/{self.key}/events")
        self.assertEqual(st, 200)
        p = body["pending"]
        self.assertEqual((p["kind"], p["tool"]), ("permission", "Bash"))
        self.assertEqual([o["key"] for o in p["options"]], ["1", "2", "3"])
        # the same hash the stream would give, so /answer accepts it
        self.assertEqual(p["rawHash"], cr.pane_raw(self.screen())[1])

    def test_open_prompt_blocks_send_and_mode(self):
        # Enter would confirm "1. Yes"; Shift+Tab would pick "allow all edits".
        st, body = self.req("POST", f"/api/session/{self.key}/send", {"text": "先别跑"})
        self.assertEqual(st, 409)
        self.assertIn("提示", body["error"])
        self.assertEqual(self.req("POST", f"/api/session/{self.key}/mode", {"target": "plan"})[0], 409)
        # Even when the status file lags behind, the dialog on screen is enough.
        self.status("busy")
        self.assertEqual(self.req("POST", f"/api/session/{self.key}/send", {"text": "先别跑"})[0], 409)
        self.assertEqual(self.req("POST", f"/api/session/{self.key}/mode", {"target": "plan"})[0], 409)
        self.assertEqual(self.logged(), [])
        self.assertIn("Do you want to proceed?", self.screen())

    def test_answer_checks_screen_label_and_repeats(self):
        s = SSE(self.port, self.key)
        self.addCleanup(s.close)
        snap = s.next()
        self.assertEqual(snap["type"], "snapshot")
        self.assertTrue(snap["session"]["controllable"] and snap["session"]["owned"])
        p = snap["pending"]
        self.assertEqual((p["kind"], p["tool"], p["question"]), ("permission", "Bash", "Do you want to proceed?"))
        self.assertEqual([o["key"] for o in p["options"]], ["1", "2", "3"])
        st, body = self.req("POST", f"/api/session/{self.key}/answer", {"choice": "1", "rawHash": "0" * 16})
        self.assertEqual((st, body["error"]), (409, "提示已经变化，请重新查看"))
        st, _ = self.req("POST", f"/api/session/{self.key}/answer", {"choice": "1", "rawHash": p["rawHash"],
                                                                      "label": "No"})
        self.assertEqual(st, 409)  # the phone showed a different option for this key
        self.assertEqual(self.logged(), [])
        st, body = self.req("POST", f"/api/session/{self.key}/answer", {
            "choice": "3", "rawHash": p["rawHash"], "label": p["options"][2]["label"], "text": "换个做法"})
        self.assertEqual((st, body), (200, {"ok": True, "sentText": True}))
        self.assertEqual(self.logged(), ["[dialog] 3", "[send] 换个做法"])
        st, body = self.req("POST", f"/api/session/{self.key}/answer", {"choice": "1", "rawHash": p["rawHash"]})
        self.assertEqual(st, 409)  # the screen changed since
        self.assertEqual(len(self.logged()), 2)
        self.assertEqual(self.req("POST", f"/api/session/{self.key}/interrupt", {}), (200, {"ok": True}))

    def test_send_and_mode_after_the_prompt(self):
        st, _ = self.req("POST", f"/api/session/{self.key}/answer", {"choice": "1", "rawHash": cr.pane_raw(self.screen())[1]})
        self.assertEqual(st, 200)
        self.assertTrue(self.wait_screen("? for shortcuts"))
        self.status("idle")
        # A draft that mentions a mode does not fool the mode detection.
        self.assertEqual(self.req("POST", f"/api/session/{self.key}/send",
                                  {"text": "plan mode on (shift+tab to cycle)", "submit": False}), (200, {"ok": True}))
        self.assertTrue(self.wait_screen("> plan mode on"))
        self.assertEqual(cr.detect_mode(self.screen()), "default")
        st, body = self.req("POST", f"/api/session/{self.key}/mode", {"target": "plan"})
        self.assertEqual((st, body), (200, {"mode": "plan"}))
        self.assertIn("⏸ plan mode on", self.screen())
        st, body = self.req("POST", f"/api/session/{self.key}/mode", {"target": "default"})
        self.assertEqual((st, body), (200, {"mode": "default"}))
        self.assertEqual(self.req("POST", f"/api/session/{self.key}/send", {"text": "继续"}), (200, {"ok": True}))
        self.assertEqual(self.logged(), ["[dialog] 1", "[send] plan mode on (shift+tab to cycle)继续"])


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class ClaudeExitTests(unittest.TestCase):
    """claude (here: sleep) exits in a cr- pane and the pane drops back to a
    shell: the stream keeps the conversation and only sends a status."""

    NAME = "cr-fixture-exit-%d" % os.getpid()

    @classmethod
    def setUpClass(cls):
        cls.app = cr.App({"token": "tok", "ntfy": {}})
        cls.port = free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), cr.make_handler(cls.app))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_exit_keeps_the_conversation(self):
        out = subprocess.run(["tmux", "new-session", "-d", "-P", "-F", "#{pane_id} #{pane_pid}", "-s", self.NAME,
                              "-x", "100", "-y", "30", "exec bash --norc --noprofile"],
                             capture_output=True, text=True, check=True).stdout.split()
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", "=" + self.NAME], capture_output=True)
        pane, shell = out[0], int(out[1])
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "exec 2>/dev/null; sleep 600"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        child = None
        for _ in range(50):
            kids = subprocess.run(["pgrep", "-P", str(shell), "sleep"], capture_output=True, text=True).stdout.split()
            if kids and cr.proc_start_ticks(int(kids[0])) is not None:
                child = int(kids[0])
                break
            time.sleep(0.1)
        self.assertIsNotNone(child)
        self.addCleanup(lambda: subprocess.run(["kill", str(child)], capture_output=True))
        sid = "cccccccc-0000-4000-8000-%012d" % (int(time.time() * 1000) % 10 ** 12)
        proj = PROJECTS / "-fixture-exit"
        proj.mkdir(parents=True, exist_ok=True)
        write_jsonl(proj / f"{sid}.jsonl", [user("e1", "做完了吗"), asst("e2", {"type": "text", "text": "做完了"})])
        write_session(child, sid, status="idle")
        self.addCleanup(lambda: [f.unlink() for f in SESSIONS.glob("*.json")])
        s = SSE(self.port, urllib_quote(f"t:{pane}"))
        self.addCleanup(s.close)
        snap = s.next()
        self.assertEqual([e["id"] for e in snap["events"]], ["e1", "e2:0"])
        os.kill(child, 9)
        msg = s.next(want={"status", "snapshot", "gone"})
        self.assertEqual(msg["type"], "status")
        self.assertEqual((msg["session"]["status"], msg["session"]["sessionId"]), ("shell", None))


def shlex_quote(s):
    import shlex
    return shlex.quote(s)


def urllib_quote(s):
    import urllib.parse
    return urllib.parse.quote(s, safe="")


if __name__ == "__main__":
    unittest.main()
