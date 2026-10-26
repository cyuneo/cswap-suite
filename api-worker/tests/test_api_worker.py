"""Tests for api_worker.py. A fake claude_agent_sdk stands in for the real one,
so nothing here calls the API or spends credits."""

import contextlib
import fcntl
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "worker" / "api_worker.py"
sys.path.insert(0, str(HERE.parent / "worker"))
import api_worker  # noqa: E402

FAKE_KEY = "sk-ant-api03-" + "x" * 40

FAKE_SDK = textwrap.dedent('''
    import asyncio, json, os
    from dataclasses import dataclass, field

    @dataclass
    class TextBlock:
        text: str
    @dataclass
    class ToolUseBlock:
        id: str
        name: str
        input: dict
    @dataclass
    class AssistantMessage:
        content: list
        model: str = "fake"
    @dataclass
    class ResultMessage:
        subtype: str
        duration_ms: int
        duration_api_ms: int
        is_error: bool
        num_turns: int
        session_id: str
        total_cost_usd: float = None
        result: str = None
    @dataclass
    class PermissionResultAllow:
        behavior: str = "allow"
    @dataclass
    class PermissionResultDeny:
        behavior: str = "deny"
        message: str = ""
    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    async def query(*, prompt, options):
        async for _ in prompt:
            pass
        out = {"env": {k: os.environ.get(k) for k in
                       ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")},
               "budget": options.max_budget_usd, "model": options.model, "cwd": options.cwd,
               "settingSources": options.setting_sources}
        perms = {}
        for tool, inp in (("Write", {"file_path": "inside.txt"}),
                          ("Write", {"file_path": "/etc/passwd"}),
                          ("Bash", {"command": "ls"})):
            r = await options.can_use_tool(tool, inp, None)
            perms[f"{tool} {list(inp.values())[0]}"] = r.behavior
        out["perms"] = perms
        with open(os.environ["FAKE_SDK_OUT"], "w") as f:
            json.dump(out, f)
        yield AssistantMessage([TextBlock("working"), ToolUseBlock("1", "Write", {"file_path": "inside.txt"})])
        if os.environ.get("FAKE_SDK_SLEEP"):
            await asyncio.sleep(float(os.environ["FAKE_SDK_SLEEP"]))
        if os.environ.get("FAKE_SDK_RAISE"):
            raise RuntimeError("boom " + os.environ.get("ANTHROPIC_API_KEY", ""))
        yield ResultMessage("success", 1, 1, False, 1, "s", total_cost_usd=0.42, result="all done")
''')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "sdk" / "claude_agent_sdk").mkdir(parents=True)
        (t / "sdk" / "claude_agent_sdk" / "__init__.py").write_text(FAKE_SDK)
        self.work = t / "work"
        self.work.mkdir()
        self.out = t / "sdk-out.json"
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(t / "cfg"), XDG_DATA_HOME=str(t / "data"),
                        PYTHONPATH=str(t / "sdk"), FAKE_SDK_OUT=str(self.out),
                        CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-should-not-leak")
        self.env.pop("ANTHROPIC_API_KEY", None)
        self._old = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
        os.environ["XDG_CONFIG_HOME"] = self.env["XDG_CONFIG_HOME"]
        os.environ["XDG_DATA_HOME"] = self.env["XDG_DATA_HOME"]

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def cli(self, *args, input=None, env=None):
        return subprocess.run([sys.executable, str(TOOL), *args], input=input, capture_output=True,
                              text=True, env=dict(self.env, **(env or {})), cwd=str(self.work), timeout=60)

    def set_key(self):
        r = self.cli("set-key", input=FAKE_KEY + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)

    def wait(self, tid, states=("done", "failed", "cancelled", "lost"), timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            info = json.loads(self.cli("status", tid).stdout)
            if info["state"] in states:
                return info
            time.sleep(0.2)
        self.fail(f"task {tid} did not finish: {info}")


class KeyTests(Base):
    def test_set_key_saves_0600_and_rejects_setup_tokens(self):
        r = self.cli("set-key", input="sk-ant-oat01-" + "y" * 40)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("setup-token", r.stderr)
        self.set_key()
        p = Path(self.env["XDG_CONFIG_HOME"]) / "api-worker" / "keys" / "default"
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertNotIn(FAKE_KEY, self.cli("set-key", input=FAKE_KEY).stdout)

    def test_usr_keys_are_accepted(self):
        r = self.cli("set-key", input="sk-ant-usr01-" + "u" * 40)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("set-key", input="sk-ant-admin01-" + "u" * 40)
        self.assertNotEqual(r.returncode, 0)

    def test_open_key_file_is_refused(self):
        self.set_key()
        p = Path(self.env["XDG_CONFIG_HOME"]) / "api-worker" / "keys" / "default"
        os.chmod(p, 0o644)
        r = self.cli("start", "-p", "hi", "--no-worktree")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("chmod 600", r.stderr)

    def test_start_without_key(self):
        r = self.cli("start", "-p", "hi")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("set-key", json.loads(r.stderr)["error"])


class RunTests(Base):
    def test_task_runs_with_api_key_only(self):
        self.set_key()
        r = self.cli("start", "-p", "write a file", "--no-worktree", "--budget", "1.5")
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = json.loads(r.stdout)["id"]
        info = self.wait(tid)
        self.assertEqual(info["state"], "done", info)
        self.assertEqual(info["costUsd"], 0.42)
        seen = json.loads(self.out.read_text())
        self.assertEqual(seen["env"]["ANTHROPIC_API_KEY"], FAKE_KEY)
        self.assertIsNone(seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"])
        self.assertTrue(seen["env"]["CLAUDE_CONFIG_DIR"].endswith("claude-home"))
        self.assertEqual(seen["budget"], 1.5)
        self.assertEqual(seen["settingSources"], [])
        self.assertEqual(seen["perms"], {"Write inside.txt": "allow", "Write /etc/passwd": "deny",
                                         "Bash ls": "deny"})
        res = json.loads(self.cli("result", tid).stdout)
        self.assertEqual(res["result"], "all done")
        # the key never lands in task files
        for f in (Path(self.env["XDG_DATA_HOME"]) / "api-worker" / "tasks" / tid).iterdir():
            self.assertNotIn(FAKE_KEY, f.read_text(errors="replace"), f.name)
        lst = json.loads(self.cli("list").stdout)
        self.assertEqual(lst["monthSpentUsd"], 0.42)

    def test_allow_bash(self):
        self.set_key()
        tid = json.loads(self.cli("start", "-p", "x", "--no-worktree", "--allow-bash").stdout)["id"]
        self.wait(tid)
        self.assertEqual(json.loads(self.out.read_text())["perms"]["Bash ls"], "allow")

    def test_error_message_hides_key(self):
        self.set_key()
        tid = json.loads(self.cli("start", "-p", "x", "--no-worktree", env={"FAKE_SDK_RAISE": "1"}).stdout)["id"]
        info = self.wait(tid)
        self.assertEqual(info["state"], "failed")
        self.assertIn("boom [key]", info["error"])
        self.assertNotIn(FAKE_KEY, json.dumps(info))

    def test_cancel(self):
        self.set_key()
        tid = json.loads(self.cli("start", "-p", "x", "--no-worktree", env={"FAKE_SDK_SLEEP": "30"}).stdout)["id"]
        self.wait(tid, states=("running",))
        end = time.time() + 10
        while not self.out.exists() and time.time() < end:
            time.sleep(0.1)
        r = self.cli("cancel", tid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.wait(tid)["state"], "cancelled")

    def test_worktree_in_git_repo(self):
        self.set_key()
        g = lambda *a: subprocess.run(["git", "-C", str(self.work), *a], check=True, capture_output=True)
        g("init", "-q")
        (self.work / "f.txt").write_text("x")
        g("add", ".")
        g("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
        out = json.loads(self.cli("start", "-p", "x").stdout)
        self.assertEqual(out["branch"], f"api-worker/{out['id']}")
        self.assertNotEqual(os.path.realpath(out["workdir"]), os.path.realpath(self.work))
        self.wait(out["id"])
        self.assertEqual(json.loads(self.out.read_text())["cwd"], out["workdir"])


class MultiKeyTests(Base):
    def add(self, name, budget=None, ch="k"):
        args = ["set-key", "--name", name] + (["--budget", str(budget)] if budget else [])
        r = self.cli(*args, input=f"sk-ant-api03-{ch * 40}")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_picks_key_with_most_left_and_runs_on_it(self):
        self.add("a", 5, "a")
        self.add("b", 50, "b")
        out = json.loads(self.cli("start", "-p", "x", "--no-worktree").stdout)
        self.assertEqual(out["key"], "b")
        self.wait(out["id"])
        self.assertEqual(json.loads(self.out.read_text())["env"]["ANTHROPIC_API_KEY"], "sk-ant-api03-" + "b" * 40)
        out = json.loads(self.cli("start", "-p", "x", "--no-worktree", "--key", "a").stdout)
        self.assertEqual(out["key"], "a")
        self.wait(out["id"])
        keys = {k["name"]: k for k in json.loads(self.cli("list").stdout)["keys"]}
        self.assertEqual((keys["a"]["spentUsd"], keys["b"]["spentUsd"]), (0.42, 0.42))
        self.assertEqual(keys["a"]["leftUsd"], 4.58)
        self.assertNotIn("sk-ant", self.cli("spend").stdout)

    def test_budget_only_change_without_a_terminal(self):
        self.add("a", 5, "a")
        r = self.cli("set-key", "--name", "a", "--budget", "40", input="")
        self.assertEqual(r.returncode, 0, r.stderr)
        keys = {k["name"]: k for k in json.loads(self.cli("list").stdout)["keys"]}
        self.assertEqual(keys["a"]["monthlyBudgetUsd"], 40.0)
        stored = Path(self.env["XDG_CONFIG_HOME"]) / "api-worker" / "keys" / "a"
        self.assertEqual(stored.read_text().strip(), "sk-ant-api03-" + "a" * 40)
        # a name that has no key yet still needs one
        r = self.cli("set-key", "--name", "b", "--budget", "40", input="")
        self.assertNotEqual(r.returncode, 0)

    def test_each_key_has_its_own_cap(self):
        self.add("a", 1, "a")
        r = self.cli("start", "-p", "x", "--no-worktree", "--key", "a", "--budget", "2")
        self.assertIn("额度不够", json.loads(r.stderr)["error"])
        r = self.cli("start", "-p", "x", "--no-worktree", "--key", "nope")
        self.assertIn("没有名为", json.loads(r.stderr)["error"])

    def test_legacy_single_key_is_default(self):
        d = Path(self.env["XDG_CONFIG_HOME"]) / "api-worker"
        d.mkdir(parents=True)
        (d / "key").write_text(FAKE_KEY + "\n")
        os.chmod(d / "key", 0o600)
        out = json.loads(self.cli("start", "-p", "x", "--no-worktree").stdout)
        self.assertEqual(out["key"], "default")
        self.assertEqual(self.wait(out["id"])["state"], "done")
        self.assertEqual(self.cli("remove-key", "default").returncode, 0)
        self.assertFalse((d / "key").exists())


class LimitTests(Base):
    def test_budget_caps(self):
        self.set_key()
        r = self.cli("start", "-p", "x", "--no-worktree", "--budget", "50")
        self.assertIn("预算", json.loads(r.stderr)["error"])
        self.cli("config", "--set", "monthlyBudgetUsd=1")
        r = self.cli("start", "-p", "x", "--no-worktree", "--budget", "2")
        self.assertIn("额度不够", json.loads(r.stderr)["error"])

    def test_parallel_cap(self):
        self.set_key()
        self.cli("config", "--set", "maxParallel=1")
        tid = json.loads(self.cli("start", "-p", "x", "--no-worktree", env={"FAKE_SDK_SLEEP": "30"}).stdout)["id"]
        r = self.cli("start", "-p", "y", "--no-worktree")
        self.assertIn("上限 1", json.loads(r.stderr)["error"])
        self.cli("cancel", tid)

    def test_bad_task_id(self):
        r = self.cli("status", "../x")
        self.assertNotEqual(r.returncode, 0)

    def test_config_set_keeps_other_settings(self):
        self.cli("set-key", "--name", "a", "--budget", "7", input=FAKE_KEY)
        r = self.cli("config", "--set", "maxParallel=2")
        self.assertEqual(r.returncode, 0, r.stderr)
        stored = json.loads((Path(self.env["XDG_CONFIG_HOME"]) / "api-worker" / "config.json").read_text())
        self.assertEqual((stored["maxParallel"], stored["keyBudgets"]), (2, {"a": 7.0}))


class StartStatusTests(Base):
    def test_worker_state_is_not_overwritten_by_start(self):
        """The worker may write "running" at any moment after it is started,
        including while `start` itself is updating status.json."""
        self.set_key()
        started = []

        def child_writes(tid):
            st_path = api_worker.tasks_dir() / tid / "status.json"
            st = api_worker.read_json(st_path)
            st.update(state="running", pid=os.getpid())
            api_worker.write_json(st_path, st)

        class FakePopen:
            def __init__(self, argv, **kw):
                started.append(argv[-1])
                self.pid = 99999999

        real_read, real_popen = api_worker.read_json, api_worker.subprocess.Popen
        raced = []

        def read_json(p, default=None):  # the child lands right after the parent reads
            out = real_read(p, default)
            if started and Path(p).name == "status.json" and not raced:
                raced.append(1)
                child_writes(started[0])
            return out
        api_worker.read_json, api_worker.subprocess.Popen = read_json, FakePopen
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(api_worker.main(["start", "-p", "x", "--no-worktree"]), 0)
        finally:
            api_worker.read_json, api_worker.subprocess.Popen = real_read, real_popen
        tid = json.loads(out.getvalue())["id"]
        if not raced:
            child_writes(tid)
        st = api_worker.read_json(api_worker.tasks_dir() / tid / "status.json")
        self.assertEqual((st["state"], st["pid"]), ("running", os.getpid()))
        info = api_worker.task_info(api_worker.tasks_dir() / tid)
        self.assertEqual((info["state"], info["final"]), ("running", False))


class AccountSwitchTests(Base):
    def setUp(self):
        super().setUp()
        self.log = Path(self.tmp.name) / "switches"
        exe = Path(self.tmp.name) / "cswap"
        exe.write_text(f'#!/bin/sh\necho "$2" >> "{self.log}"\n')
        exe.chmod(0o755)
        self._patched = {n: getattr(api_worker, n) for n in ("cswap_bin", "cswap_accounts")}
        api_worker.cswap_bin = lambda: str(exe)
        self.active = []  # what each cswap_accounts() call reports as active, in order

        def accounts():
            now = self.active.pop(0) if self.active else None
            return [{"number": n, "email": f"{n}@x", "active": n == now} for n in ("1", "2", "3")]
        api_worker.cswap_accounts = accounts

    def tearDown(self):
        for n, f in self._patched.items():
            setattr(api_worker, n, f)
        super().tearDown()

    def switches(self):
        return self.log.read_text().split() if self.log.exists() else []

    def lock_is_free(self):
        with open(api_worker.data_dir() / "account-switch.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held
        return True

    def test_refuses_when_the_active_account_cannot_be_read(self):
        self.active = [None]
        with self.assertRaises(api_worker.Fail) as cm:
            with api_worker.AccountSwitch("1"):
                pass
        self.assertIn("切不回来", str(cm.exception))
        self.assertEqual(self.switches(), [])
        self.assertTrue(self.lock_is_free())

    def test_switches_back_when_still_on_the_borrowed_account(self):
        self.active = ["2", "1", "1"]  # before, after switching, before switching back
        sw = api_worker.AccountSwitch("1")
        with sw:
            pass
        self.assertEqual(self.switches(), ["1", "2"])
        self.assertIsNone(sw.warning)
        self.assertTrue(self.lock_is_free())

    def test_leaves_the_account_alone_when_auto_moved_it(self):
        self.active = ["2", "1", "3"]  # cswap auto moved to 3 meanwhile
        sw = api_worker.AccountSwitch("1")
        with sw:
            pass
        self.assertEqual(self.switches(), ["1"])
        self.assertIn("3", sw.warning)
        self.assertIn("没有切回 2", sw.warning)
        self.assertTrue(self.lock_is_free())


    def test_undoes_a_switch_that_reported_failure_but_happened(self):
        exe = Path(api_worker.cswap_bin())
        exe.write_text(f'#!/bin/sh\necho "$2" >> "{self.log}"\n[ "$2" = 1 ] && exit 1\nexit 0\n')
        self.active = ["2", "1", "1"]  # before, after the "failed" switch, before undoing it
        with self.assertRaises(api_worker.Fail):
            with api_worker.AccountSwitch("1"):
                self.fail("the body must not run")
        self.assertEqual(self.switches(), ["1", "2"])
        self.assertTrue(self.lock_is_free())

    def test_switch_back_unknown_after_failed_switch(self):
        exe = Path(api_worker.cswap_bin())
        exe.write_text(f'#!/bin/sh\necho "$2" >> "{self.log}"\n[ "$2" = 1 ] && exit 1\nexit 0\n')
        self.active = ["2", None, None]  # cannot tell where it ended up: put the old one back
        with self.assertRaises(api_worker.Fail):
            with api_worker.AccountSwitch("1"):
                pass
        self.assertEqual(self.switches(), ["1", "2"])

    def test_exception_in_body_switches_back(self):
        self.active = ["2", "1", "1"]
        with self.assertRaises(ZeroDivisionError):
            with api_worker.AccountSwitch("1"):
                1 / 0
        self.assertEqual(self.switches(), ["1", "2"])
        self.assertTrue(self.lock_is_free())

    def test_sigterm_in_body_switches_back(self):
        self.active = ["2", "1", "1"]
        old = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(SystemExit) as cm:
            with api_worker.AccountSwitch("1"):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)
                self.fail("SIGTERM did not interrupt")
        self.assertEqual(cm.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(self.switches(), ["1", "2"])
        self.assertIs(signal.getsignal(signal.SIGTERM), old)
        self.assertTrue(self.lock_is_free())

    def test_no_warning_when_already_back(self):
        self.active = ["2", "1", "2"]
        sw = api_worker.AccountSwitch("1")
        with sw:
            pass
        self.assertEqual(self.switches(), ["1"])
        self.assertIsNone(sw.warning)


class RefreshBudgetTests(Base):
    def test_list_stops_asking_remotes_after_its_budget(self):
        for i in range(6):
            api_worker.write_json(api_worker.tasks_dir() / f"t0000000{i}" / "task.json", {
                "id": f"t0000000{i}", "kind": "cloud", "repo": "/nowhere", "remote": "origin",
                "resultBranch": f"claude/aw-t0000000{i}", "createdAt": time.time() - i})
        asked = []

        def slow_git(cwd, *args, timeout=60, text=True):
            asked.append(timeout)
            time.sleep(0.6)
            return subprocess.CompletedProcess(args, 0, "", "")
        real = api_worker.git
        api_worker.git = slow_git
        try:
            t0 = time.monotonic()
            api_worker.refresh_cloud(budget=2.0)
            took = time.monotonic() - t0
        finally:
            api_worker.git = real
        self.assertLess(took, 3.0)
        self.assertTrue(1 <= len(asked) < 6, asked)
        self.assertTrue(all(t <= 2.0 for t in asked), asked)


FAKE_CSWAP = '''A=$(cat "$FAKE_ACTIVE")
t() { [ "$A" = "$1" ] && echo true || echo false; }
case "$1" in
  policy) echo x >> "$FAKE_ACTIVE.policy"; printf '{"accounts":[{"number":"1","email":"a@x","active":%s},{"number":"2","email":"b@x","active":%s}]}' "$(t 1)" "$(t 2)";;
  switch) echo "$2" > "$FAKE_ACTIVE"; echo "$2" >> "$FAKE_ACTIVE.log";;
esac
'''


@unittest.skipUnless(__import__("shutil").which("tmux"), "needs tmux")
class CloudTests(Base):
    def setUp(self):
        super().setUp()
        self.bindir = Path(self.tmp.name) / "bin"
        self.bindir.mkdir()
        self.active = Path(self.tmp.name) / "active"
        self.active.write_text("2\n")
        self.env.update(PATH=f"{self.bindir}:{os.environ['PATH']}", FAKE_ACTIVE=str(self.active),
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.script("cswap", FAKE_CSWAP)

    def script(self, name, body):
        f = self.bindir / name
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(0o755)

    def ok_claude(self):
        # records the prompt and which cswap account was live while it ran
        self.script("claude", '[ "$1" = --cloud ] || exit 3\nprintf "%s" "$2" > "$FAKE_SDK_OUT"\n'
                              'cat "$FAKE_ACTIVE" > "$FAKE_SDK_OUT.account"\n'
                              'echo "Created cloud session: x"\n'
                              'echo "View: https://claude.ai/code/session_abc?from=cli"\n')

    def git(self, *a, cwd=None):
        return subprocess.run(["git", *a], cwd=cwd or self.work, check=True, capture_output=True, text=True,
                              env=self.env).stdout

    def repo(self):
        bare = Path(self.tmp.name) / "origin.git"
        self.git("init", "-q", "--bare", str(bare))
        self.git("init", "-q")
        (self.work / "a.txt").write_text("a\n")
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        self.git("remote", "add", "origin", str(bare))
        self.git("push", "-q", "-u", "origin", "HEAD")
        return bare

    def switches(self):
        p = Path(str(self.active) + ".log")
        return p.read_text().split() if p.exists() else []

    def test_non_git_folder_records_link_only(self):
        self.ok_claude()
        prompt = "fix it; echo $HOME `id` \"quoted\""
        r = self.cli("cloud", "--cwd", str(self.work), "--title", "fix", input=prompt)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["sessionUrl"], "https://claude.ai/code/session_abc?from=cli")
        self.assertEqual(out["teleport"], "claude --teleport session_abc")
        self.assertEqual(out["account"], "b@x")
        self.assertIsNone(out["resultBranch"])
        self.assertIn("不是 git 仓库", out["warnings"][0])
        self.assertEqual(self.out.read_text(), prompt)  # passed whole, never through a shell
        lst = json.loads(self.cli("list").stdout)
        self.assertEqual(lst["tasks"][0]["state"], "cloud")
        self.assertEqual((lst["monthSpentUsd"], lst["monthReservedUsd"]), (0, 0))
        self.assertEqual([a["email"] for a in lst["accounts"]], ["a@x", "b@x"])
        self.assertIn("claude.ai", json.loads(self.cli("cancel", out["id"]).stderr)["error"])
        self.assertEqual(self.switches(), [])

    def test_result_branch_is_pulled_back(self):
        bare = self.repo()
        self.ok_claude()
        out = json.loads(self.cli("cloud", "--cwd", str(self.work), input="add b").stdout)
        branch = out["resultBranch"]
        self.assertEqual(branch, f"claude/aw-{out['id']}")
        self.assertIn(branch, self.out.read_text())
        self.assertEqual(json.loads(self.cli("status", out["id"]).stdout)["state"], "running")
        # the cloud session pushes its work from its own clone
        clone = Path(self.tmp.name) / "cloud"
        self.git("clone", "-q", str(bare), str(clone))
        self.git("checkout", "-qb", branch, cwd=clone)
        (clone / "b.txt").write_text("b\n")
        (clone / "CLOUD_RESULT.md").write_text("Added b.txt.\n")
        self.git("add", ".", cwd=clone)
        self.git("commit", "-qm", "add b", cwd=clone)
        self.git("push", "-q", "origin", branch, cwd=clone)
        before = self.git("status", "--porcelain")
        res = json.loads(self.cli("result", out["id"]).stdout)
        self.assertEqual(res["state"], "done")
        self.assertEqual(res["result"], "Added b.txt.\n")
        self.assertIn("b.txt", res["diffStat"])
        self.assertEqual(len(res["commits"]), 1)
        self.assertEqual(self.git("status", "--porcelain"), before)  # working tree untouched
        self.assertFalse((self.work / "b.txt").exists())
        self.assertEqual(json.loads(self.cli("list").stdout)["tasks"][0]["state"], "done")

    def test_other_account_switches_and_back(self):
        self.ok_claude()
        out = json.loads(self.cli("cloud", "--cwd", str(self.work), "--account", "a@x", input="x").stdout)
        self.assertEqual(out["account"], "a@x")
        self.assertEqual(Path(str(self.out) + ".account").read_text().strip(), "1")  # ran as account 1
        self.assertEqual(self.active.read_text().strip(), "2")  # and switched back
        self.assertEqual(self.switches(), ["1", "2"])
        self.cli("cloud", "--cwd", str(self.work), "--account", "2", input="x")
        self.assertEqual(self.switches(), ["1", "2"])  # the active account needs no switch
        r = self.cli("cloud", "--cwd", str(self.work), "--account", "9", input="x")
        self.assertIn("没有账号", json.loads(r.stderr)["error"])

    def test_switches_back_when_launch_fails(self):
        self.script("claude", 'echo "Error: not logged in"\n')
        r = self.cli("cloud", "--cwd", str(self.work), "--account", "1", "--timeout", "3", input="x")
        self.assertIn("not logged in", json.loads(r.stderr)["error"])
        self.assertEqual(self.active.read_text().strip(), "2")
        t = json.loads(self.cli("list").stdout)["tasks"][0]  # the failed launch is on record
        self.assertEqual((t["state"], t["final"]), ("failed", True))
        self.assertIn("not logged in", t["error"])

    def test_auto_switch_during_launch_is_not_undone(self):
        # claude --cloud runs while cswap auto moves the login on to 3 by itself
        self.script("cswap", FAKE_CSWAP.replace('"active":%s}]}\' "$(t 1)" "$(t 2)"',
                                                '"active":%s},{"number":"3","email":"c@x","active":%s}]}\' '
                                                '"$(t 1)" "$(t 2)" "$(t 3)"'))
        self.script("claude", 'echo 3 > "$FAKE_ACTIVE"\necho "View: https://claude.ai/code/session_m"\n')
        r = self.cli("cloud", "--cwd", str(self.work), "--account", "1", input="x")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(self.switches(), ["1"])  # no switch back on top of auto's
        self.assertEqual(self.active.read_text().strip(), "3")
        self.assertTrue(any("没有切回 2" in w for w in out["warnings"]), out["warnings"])
        t = json.loads(self.cli("list").stdout)["tasks"][0]
        self.assertTrue(any("没有切回 2" in w for w in t["warnings"]))

    def test_auto_moving_back_to_the_old_account_needs_no_warning(self):
        self.script("claude", 'echo 2 > "$FAKE_ACTIVE"\necho "View: https://claude.ai/code/session_m"\n')
        out = json.loads(self.cli("cloud", "--cwd", str(self.work), "--account", "1", input="x").stdout)
        self.assertEqual(self.switches(), ["1"])
        self.assertFalse(any("没有切回" in w for w in out["warnings"]), out["warnings"])

    def test_killed_launch_leaves_a_lost_task(self):
        """A plugin timeout kills `cloud`; the task must still be findable."""
        self.ok_claude()
        lockfile = Path(self.env["XDG_DATA_HOME"]) / "api-worker" / "account-switch.lock"
        lockfile.parent.mkdir(parents=True, exist_ok=True)
        with open(lockfile, "w") as held:  # keeps `cloud` waiting before it switches
            fcntl.flock(held, fcntl.LOCK_EX)
            proc = subprocess.Popen([sys.executable, str(TOOL), "cloud", "--cwd", str(self.work), "--account", "1",
                                     "--title", "slow"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, text=True, env=self.env, cwd=str(self.work))
            proc.stdin.write("x")
            proc.stdin.close()
            end = time.time() + 20
            tasks = []
            while time.time() < end:
                tasks = json.loads(self.cli("list", "--no-accounts").stdout)["tasks"]
                if tasks:
                    break
                time.sleep(0.2)
            self.assertEqual((tasks[0]["title"], tasks[0]["state"], tasks[0]["final"]), ("slow", "launching", False))
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait()
        t = json.loads(self.cli("list").stdout)["tasks"][0]
        self.assertEqual((t["title"], t["state"], t["final"]), ("slow", "lost", True))
        self.assertIn("claude.ai/code", t["note"])
        self.assertEqual(self.switches(), [])

    def test_sigterm_during_launch_switches_back(self):
        """A plugin timeout that sends SIGTERM must not leave the borrowed account live."""
        self.script("claude", 'touch "$FAKE_SDK_OUT.started"\nsleep 30\n')
        proc = subprocess.Popen([sys.executable, str(TOOL), "cloud", "--cwd", str(self.work), "--account", "1",
                                 "--title", "term"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, text=True, env=self.env, cwd=str(self.work))
        proc.stdin.write("x")
        proc.stdin.close()
        started = Path(str(self.out) + ".started")
        end = time.time() + 20
        while not started.exists() and time.time() < end:
            time.sleep(0.2)
        self.assertTrue(started.exists())
        self.assertEqual(self.active.read_text().strip(), "1")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=60), 128 + signal.SIGTERM)
        self.assertEqual(self.active.read_text().strip(), "2")
        self.assertEqual(self.switches(), ["1", "2"])
        t = json.loads(self.cli("list", "--no-accounts").stdout)["tasks"][0]
        self.assertEqual((t["title"], t["state"], t["final"]), ("term", "lost", True))

    def test_list_without_accounts_does_not_run_cswap(self):
        self.ok_claude()
        self.cli("cloud", "--cwd", str(self.work), input="x")
        marker = Path(str(self.active) + ".policy")
        marker.unlink()
        lst = json.loads(self.cli("list", "--no-accounts").stdout)
        self.assertNotIn("accounts", lst)
        self.assertFalse(marker.exists())
        self.assertEqual((lst["tasks"][0]["state"], lst["tasks"][0]["final"]), ("cloud", False))
        self.assertEqual(len(json.loads(self.cli("list").stdout)["accounts"]), 2)

    def test_close_marks_an_untracked_cloud_task_ended(self):
        self.ok_claude()
        tid = json.loads(self.cli("cloud", "--cwd", str(self.work), input="x").stdout)["id"]
        r = self.cli("cancel", tid)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"close {tid}", r.stderr)
        r = self.cli("close", tid)
        self.assertEqual(r.returncode, 0, r.stderr)
        t = json.loads(self.cli("list", "--no-accounts").stdout)["tasks"][0]
        self.assertEqual((t["state"], t["final"]), ("closed", True))
        self.assertIn("sessionUrl", t["note"])
        self.assertTrue(t["endedAt"])
        r = self.cli("close", tid)  # already ended
        self.assertNotEqual(r.returncode, 0)

    def test_close_refuses_api_tasks(self):
        tid = "t00000aa1"
        api_worker.write_json(api_worker.tasks_dir() / tid / "task.json", {"id": tid, "kind": "api"})
        r = self.cli("close", tid)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cancel", r.stderr)

    def desk(self):
        bare = Path(self.tmp.name) / "desk.git"
        self.git("init", "-q", "--bare", str(bare))
        r = self.cli("desk-setup", "--repo", str(bare))
        self.assertEqual(r.returncode, 0, r.stderr)
        return bare

    def test_desk_uploads_inputs_and_collects_outputs(self):
        bare = self.desk()
        self.script("claude", '[ "$1" = --cloud ] || exit 3\nprintf "%s" "$2" > "$FAKE_SDK_OUT"\npwd > "$FAKE_SDK_OUT.cwd"\n'
                              'echo "View: https://claude.ai/code/session_d"\n')
        (self.work / "data").mkdir()
        (self.work / "data" / "a.csv").write_text("x,y\n1,2\n")
        (self.work / "data" / ".env").write_text("A=1\n")
        (self.work / "data" / "notes.txt").write_text("key sk-ant-api03-" + "z" * 30 + "\n")
        (self.work / "data" / "node_modules").mkdir()
        (self.work / "data" / "node_modules" / "big.js").write_text("x")
        (self.work / "plan.md").write_text("# plan\n")
        r = self.cli("cloud", "--cwd", str(self.work), "--file", "data", "--file", "plan.md", input="analyse a.csv")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        tid = out["id"]
        self.assertTrue(out["desk"])
        self.assertEqual(sorted(out["sent"]), ["data/a.csv", "plan.md"])
        self.assertIn(".env", out["warnings"][0])
        self.assertIn("notes.txt", out["warnings"][0])
        desk = Path(self.env["XDG_DATA_HOME"]) / "api-worker" / "desk"
        self.assertEqual(Path(str(self.out) + ".cwd").read_text().strip(), str(desk))
        self.assertIn(f"tasks/{tid}/output/", self.out.read_text())
        # what reached the remote
        pushed = self.git("--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main").split()
        self.assertIn(f"tasks/{tid}/input/data/a.csv", pushed)
        self.assertIn(f"tasks/{tid}/TASK.md", pushed)
        self.assertFalse([n for n in pushed if ".env" in n or "notes" in n or "node_modules" in n])
        # the cloud session answers on its branch
        clone = Path(self.tmp.name) / "cloud"
        self.git("clone", "-q", str(bare), str(clone))
        self.git("checkout", "-qb", f"claude/aw-{tid}", cwd=clone)
        o = clone / "tasks" / tid / "output"
        o.mkdir(parents=True)
        (o / "summary.md").write_text("mean y = 2\n")
        (o / "plot.bin").write_bytes(b"\x89PNG\x00\xff\xfe\r\n")
        (clone / "tasks" / tid / "CLOUD_RESULT.md").write_text("Analysed.\n")
        self.git("add", ".", cwd=clone)
        self.git("commit", "-qm", "out", cwd=clone)
        self.git("push", "-q", "origin", f"claude/aw-{tid}", cwd=clone)
        res = json.loads(self.cli("result", tid).stdout)
        self.assertEqual((res["state"], res["result"], sorted(res["outputs"])),
                         ("done", "Analysed.\n", ["plot.bin", "summary.md"]))
        c = json.loads(self.cli("collect", tid).stdout)
        self.assertEqual(sorted(c["files"]), ["plot.bin", "summary.md"])
        self.assertEqual((self.work / "cloud-results" / tid / "summary.md").read_text(), "mean y = 2\n")
        self.assertEqual((self.work / "cloud-results" / tid / "plot.bin").read_bytes(), b"\x89PNG\x00\xff\xfe\r\n")
        self.assertNotEqual(self.cli("collect", tid).returncode, 0)  # never overwrites

    def test_desk_refuses_private_dirs_and_needs_setup(self):
        r = self.cli("cloud", "--cwd", str(self.work), "--file", "x", input="t")
        self.assertIn("desk-setup", json.loads(r.stderr)["error"])
        self.desk()
        self.ok_claude()
        cfgdir = Path(self.env["XDG_CONFIG_HOME"]) / "api-worker"
        cfgdir.mkdir(parents=True, exist_ok=True)
        (cfgdir / "x.txt").write_text("x")
        r = self.cli("cloud", "--cwd", str(self.work), "--file", str(cfgdir / "x.txt"), input="t")
        self.assertIn("不能上传", json.loads(r.stderr)["error"])

    def test_own_github_repo_does_not_use_desk(self):
        self.desk()
        self.repo()
        self.ok_claude()
        out = json.loads(self.cli("cloud", "--cwd", str(self.work), input="x").stdout)
        self.assertFalse(out["desk"])

    def test_untrusted_folder_is_explained(self):
        self.script("claude", 'echo "Is this a project you trust? Yes, I trust this folder"; sleep 30\n')
        r = self.cli("cloud", "--cwd", str(self.work), input="x")
        self.assertIn("信任", json.loads(r.stderr)["error"])


class DecideTests(unittest.TestCase):
    def test_paths(self):
        with tempfile.TemporaryDirectory() as d:
            task = {"workdir": d}
            self.assertTrue(api_worker.decide("Read", {"file_path": "/etc/passwd"}, task)[0])
            self.assertTrue(api_worker.decide("Edit", {"file_path": os.path.join(d, "a/b.py")}, task)[0])
            self.assertFalse(api_worker.decide("Edit", {"file_path": os.path.join(d, "../x")}, task)[0])
            self.assertFalse(api_worker.decide("Write", {"file_path": d + "-other/x"}, task)[0])
            self.assertFalse(api_worker.decide("Write", {}, task)[0])
            self.assertFalse(api_worker.decide("WebFetch", {"url": "x"}, task)[0])
            self.assertTrue(api_worker.decide("WebFetch", {"url": "x"}, dict(task, allowWeb=True))[0])
            self.assertFalse(api_worker.decide("mcp__x__y", {}, task)[0])


if __name__ == "__main__":
    unittest.main()
