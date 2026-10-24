"""Tests for cswap-satellite.

Run: python3 -m unittest discover -s tests

The end-to-end tests drive the real cswap (when installed) inside a
temporary HOME / XDG_DATA_HOME / CLAUDE_CONFIG_DIR, with a fake `ssh` that
prints a canned `cswap list --json`, a fake `op` that prints obviously fake
tokens generated at run time, and HTTP(S) proxies pointed at a closed local
port so nothing reaches the network. Your real accounts are never touched.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import cswap_satellite as cs  # noqa: E402

REAL_CSWAP = next((c for c in (str(Path.home() / ".local" / "bin" / "cswap"), shutil.which("cswap"))
                   if c and os.path.isfile(c)), None)
FAKE_TOKEN = "sk-ant-oat01-" + "F" * 48  # not a real credential
FAKE_TOKEN2 = "sk-ant-oat01-" + "G" * 48


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def usage(pct5, pct7):
    now = datetime.now(timezone.utc)
    return {"fiveHour": {"pct": pct5, "resetsAt": iso(now + timedelta(hours=2))},
            "sevenDay": {"pct": pct7, "resetsAt": iso(now + timedelta(days=3))}}


def remote_doc():
    return {"schemaVersion": 1, "activeAccountNumber": 1, "accounts": [
        {"number": 1, "email": "one@example.com", "organizationUuid": "org-a", "isOrganization": True,
         "usageStatus": "ok", "usage": usage(12.0, 34.0), "usageAgeSeconds": 30.0,
         "loginExpiresAt": "2027-01-01T00:00:00Z"},
        {"number": 2, "email": "two@example.com", "organizationUuid": "org-b", "isOrganization": True,
         "usageStatus": "ok", "usage": usage(56.0, 78.0), "usageAgeSeconds": 10.0,
         "loginExpiresAt": "2027-01-01T00:00:00Z"},
        {"number": 3, "email": "three@example.com", "organizationUuid": "org-c", "isOrganization": True,
         "usageStatus": "unavailable"},
        {"number": 4, "email": "setup-token-4@token.local", "organizationUuid": "", "isOrganization": False,
         "usageStatus": "unavailable"},
    ]}


def tok(email="", org=""):
    return {"email": email, "org": org}


class PureTests(unittest.TestCase):
    def test_default_selection_is_real_logins(self):
        # slot 3 has no loginExpiresAt but is an org login; slot 4 is a token slot
        self.assertEqual([r["number"] for r in cs.select_accounts(remote_doc())], [1, 2, 3])
        self.assertEqual([r["number"] for r in cs.select_accounts(remote_doc(), [2])], [2])
        with self.assertRaises(cs.Fail):
            cs.select_accounts(remote_doc(), [9])

    def test_plan(self):
        row = remote_doc()["accounts"][0]
        self.assertEqual(cs.plan_slot(row, {}), ("add", "1"))
        self.assertEqual(cs.plan_slot(row, {"1": tok("one@example.com")}), ("update", "1"))
        # already a token elsewhere: refresh it there, never let cswap move it
        self.assertEqual(cs.plan_slot(row, {"3": tok("one@example.com"), "1": tok("x@example.com")}),
                         ("update", "3"))
        self.assertEqual(cs.plan_slot(row, {"1": tok("one@example.com", "org-a")})[0], "skip")
        self.assertEqual(cs.plan_slot(row, {"5": tok("one@example.com", "org-q")})[0], "skip")
        self.assertEqual(cs.plan_slot(row, {"1": tok("other@example.com")})[0], "skip")

    def test_usage_rekeyed_to_local_slots(self):
        local = {"1": tok("one@example.com"), "5": tok("two@example.com"), "3": tok("three@example.com")}
        doc = cs.usage_document(remote_doc(), local, fetched_s=2.0)
        got = {r["email"]: r for r in doc["accounts"]}
        self.assertEqual(set(got), {"one@example.com", "two@example.com"})
        self.assertEqual(got["one@example.com"]["organizationUuid"], "")
        self.assertEqual(got["one@example.com"]["usageAgeSeconds"], 32.0)
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(set(got["two@example.com"]), {"email", "organizationUuid", "usageStatus", "usage",
                                                       "usageAgeSeconds"})

    def test_real_login_slots_are_never_fed(self):
        self.assertEqual(cs.usage_document(remote_doc(), {"1": tok("one@example.com", "org-a")})["accounts"], [])
        self.assertEqual(cs.usage_document(remote_doc(), {"1": tok("one@example.com", "org-team")})["accounts"], [])
        self.assertEqual(cs.usage_document(remote_doc(), {"5": tok("one@example.com", "org-team")})["accounts"], [])

    def test_ambiguous_email_is_never_guessed(self):
        doc = remote_doc()
        doc["accounts"].append(dict(doc["accounts"][0], number=6, organizationUuid="org-z"))
        self.assertEqual(cs.usage_document(doc, {"7": tok("one@example.com")})["accounts"], [])

    def test_bad_rows_dropped_not_fatal(self):
        doc = remote_doc()
        doc["accounts"][0]["usageAgeSeconds"] = float("nan")
        doc["accounts"][1]["usageAgeSeconds"] = -50.0
        out = cs.usage_document(doc, {"1": tok("one@example.com"), "2": tok("two@example.com")})["accounts"]
        self.assertEqual([r["email"] for r in out], ["two@example.com"])
        self.assertEqual(out[0]["usageAgeSeconds"], 0.0)

    def test_binding(self):
        local = {"1": tok("one@example.com", "org-a"), "4": tok("setup-token-4@token.local")}
        out = {r["email"]: r for r in cs.usage_document(remote_doc(), local, binds={"4": "1"})["accounts"]}
        self.assertEqual(out["setup-token-4@token.local"]["usage"], remote_doc()["accounts"][0]["usage"])
        self.assertEqual(out["setup-token-4@token.local"]["organizationUuid"], "")
        self.assertNotIn("one@example.com", out)
        # never onto a real login, never from a row without a reading
        self.assertEqual(cs.usage_document(remote_doc(), local, binds={"1": "2"})["accounts"], [])
        self.assertEqual(cs.usage_document(remote_doc(), local, binds={"4": "3"})["accounts"], [])

    def test_token_checks(self):
        self.assertEqual(cs.check_token("  " + FAKE_TOKEN + "\n"), FAKE_TOKEN)
        for bad in ("sk-ant-api03-" + "x" * 40, "hello", FAKE_TOKEN + " extra", ""):
            with self.assertRaises(cs.Fail) as e:
                cs.check_token(bad)
            self.assertNotIn(bad.strip() or "\0", str(e.exception))

    def test_server_name(self):
        for bad in ("", "-oProxyCommand=x", "a b"):
            with self.assertRaises(cs.Fail):
                cs.check_server(bad)
        self.assertEqual(cs.check_server("my-server"), "my-server")

    def test_unit_files(self):
        cmd = ["/usr/bin/python3", "/x y/插件/cswap_satellite.py", "sync-usage", "--server", "srv%1",
               "--remote-cswap", "$HOME/bin/cswap"]
        service, timer = cs.systemd_units(cmd, 300, {"XDG_DATA_HOME": "/d%x/$y"})
        self.assertIn('"/x y/插件/cswap_satellite.py"', service)
        self.assertIn('"srv%%1"', service)
        self.assertIn('"$$HOME/bin/cswap"', service)
        self.assertIn('Environment="XDG_DATA_HOME=/d%%x/$y"', service)
        self.assertIn("OnUnitActiveSec=300s", timer)
        plist = cs.launchd_plist(cmd + ["a&b"], 300, "/tmp/log", {"CLAUDE_CONFIG_DIR": "/c"})
        self.assertIn("<string>a&amp;b</string>", plist)
        self.assertIn("<integer>300</integer>", plist)
        self.assertIn("<key>CLAUDE_CONFIG_DIR</key>", plist)

    def test_cron_line_quotes(self):
        line = cs.cron_line(["/p y/python3", "x%y"], 300)
        self.assertTrue(line.startswith("*/5 * * * * "))
        self.assertIn("'/p y/python3'", line)
        self.assertIn("x\\%y", line)

    def test_unit_dir_follows_manager(self):
        self.assertEqual(cs.unit_dir("PATH=/bin\nXDG_CONFIG_HOME=/cfg\n"), Path("/cfg/systemd/user"))
        self.assertEqual(cs.unit_dir(""), Path.home() / ".config" / "systemd" / "user")


@unittest.skipUnless(REAL_CSWAP, "cswap not installed")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cswap-satellite-test-")
        self.home = Path(self.tmp, "home")
        self.bin = Path(self.tmp, "bin")
        for d in (self.home, self.bin, Path(self.tmp, "claude")):
            d.mkdir()
        self.remote = Path(self.tmp, "remote.json")
        self.remote.write_text(json.dumps(remote_doc()))
        self.token_file = self.bin / "token"
        self.token_file.write_text(FAKE_TOKEN)
        (self.bin / "ssh").write_text(f"#!/bin/sh\ncat '{self.remote}'\n")
        (self.bin / "op").write_text(f"#!/bin/sh\ncat '{self.token_file}'\n")
        for f in ("ssh", "op"):
            os.chmod(self.bin / f, 0o755)
        self.data = Path(self.tmp, "data", "claude-swap")
        dead = "http://127.0.0.1:9"
        self.env = dict(os.environ, HOME=str(self.home), XDG_DATA_HOME=str(Path(self.tmp, "data")),
                        XDG_CONFIG_HOME=str(Path(self.tmp, "config")),
                        CLAUDE_CONFIG_DIR=str(Path(self.tmp, "claude")),
                        HTTPS_PROXY=dead, HTTP_PROXY=dead, https_proxy=dead, http_proxy=dead, NO_PROXY="",
                        no_proxy="", PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")
        self.env.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

    def run_tool(self, *args):
        r = subprocess.run([sys.executable, str(HERE / "cswap_satellite.py"), *args],
                           env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
                           start_new_session=True)
        return r.returncode, r.stdout + r.stderr

    def setup(self, accounts="1,2"):
        rc, out = self.run_tool("setup", "--server", "srv", "--cswap", REAL_CSWAP, "--accounts", accounts,
                                "--op", "op://Private/Claude {email}/credential")
        self.assertEqual(rc, 0, out)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn(FAKE_TOKEN2, out)
        return out

    def cswap(self, *args):
        r = subprocess.run([REAL_CSWAP, *args], env=self.env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def seq(self):
        return json.loads((self.data / "sequence.json").read_text())

    def slots(self):
        return {k: (v["email"], v.get("organizationUuid") or "") for k, v in self.seq()["accounts"].items()}

    def test_setup_then_sync(self):
        self.setup()
        self.assertEqual(self.slots(), {"1": ("one@example.com", ""), "2": ("two@example.com", "")})
        self.assertEqual(json.loads(Path(self.tmp, "config", "cswap-satellite", "config.json").read_text())["server"],
                         "srv")
        cred_dir = self.data / "credentials"
        for p in Path(self.tmp).rglob("*"):
            if p.is_file() and cred_dir not in p.parents and self.bin not in p.parents:
                self.assertNotIn(FAKE_TOKEN.encode(), p.read_bytes(), str(p))
        self.assertIn("已更新", self.setup())

        rc, out = self.run_tool("sync-usage", "--cswap", REAL_CSWAP)
        self.assertEqual(rc, 0, out)
        rows = {str(r["number"]): r for r in json.loads(self.cswap("policy", "list", "--json"))["accounts"]}
        self.assertAlmostEqual(rows["1"]["usage5h"], 12.0)
        self.assertAlmostEqual(rows["2"]["usage7d"], 78.0)
        st = json.loads(Path(self.tmp, "config", "cswap-satellite", "last-sync.json").read_text())
        self.assertTrue(st["ok"])

    def test_setup_skips_occupied_slot(self):
        self.setup("1")
        doc = remote_doc()
        doc["accounts"][0]["email"] = "someone-else@example.com"
        self.remote.write_text(json.dumps(doc))
        out = self.setup("1")
        self.assertIn("跳过", out)
        self.assertEqual(self.slots(), {"1": ("one@example.com", "")})

    def test_same_email_in_two_orgs_keeps_one(self):
        doc = remote_doc()
        doc["accounts"][1]["email"] = "one@example.com"
        self.remote.write_text(json.dumps(doc))
        out = self.setup()
        self.assertIn("跳过", out)
        self.assertEqual(self.slots(), {"1": ("one@example.com", "")})
        self.setup()  # again: still one slot, nothing bounces between slots
        self.assertEqual(self.slots(), {"1": ("one@example.com", "")})

    def test_renumbered_server_does_not_move_local_slot(self):
        self.setup("1")
        doc = remote_doc()
        doc["accounts"][0]["number"] = 9
        self.remote.write_text(json.dumps(doc))
        out = self.setup("9")
        self.assertIn("槽位 1", out)
        self.assertEqual(self.slots(), {"1": ("one@example.com", "")})

    def test_update_keeps_disabled_flag(self):
        self.setup()
        self.cswap("disable", "1")
        self.assertTrue(self.seq()["accounts"]["1"].get("disabled"))
        self.setup()
        self.assertTrue(self.seq()["accounts"]["1"].get("disabled"))

    def test_renewing_the_active_account_goes_live(self):
        self.setup()
        self.cswap("switch", "1")
        self.assertEqual(str(self.seq()["activeAccountNumber"]), "1")
        live = Path(self.tmp, "claude", ".credentials.json")
        self.assertIn(FAKE_TOKEN, live.read_text())
        self.token_file.write_text(FAKE_TOKEN2)
        out = self.setup("1")
        self.assertIn("已切换到新 token", out)
        self.assertIn(FAKE_TOKEN2, live.read_text())
        self.cswap("switch", "2")
        self.cswap("switch", "1")
        self.assertIn(FAKE_TOKEN2, live.read_text())

    def run_add(self, n, token):
        r = subprocess.run([sys.executable, str(HERE / "cswap_satellite.py"), "add", "--account", str(n),
                            "--server", "srv", "--cswap", REAL_CSWAP], env=self.env, input=token,
                           capture_output=True, text=True, timeout=180, start_new_session=True)
        return r.returncode, r.stdout + r.stderr

    def test_plan_and_add_for_the_extension(self):
        rc, out = self.run_tool("plan", "--server", "srv", "--cswap", REAL_CSWAP)
        self.assertEqual(rc, 0, out)
        plan = json.loads(out)
        self.assertEqual([(x["number"], x["action"]) for x in plan["accounts"]], [(1, "add"), (2, "add"), (3, "add")])
        rc, out = self.run_add(1, FAKE_TOKEN + "\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("已添加", out)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertEqual(self.slots(), {"1": ("one@example.com", "")})
        rc, out = self.run_tool("plan", "--cswap", REAL_CSWAP)
        self.assertEqual(json.loads(out)["accounts"][0]["action"], "update")
        rc, out = self.run_add(2, "sk-ant-api03-" + "x" * 40)
        self.assertEqual(rc, 1)
        self.assertIn("API key", out)
        rc, out = self.run_add(4, FAKE_TOKEN)  # token slot on the server: not a real login
        self.assertEqual(rc, 1)
        self.assertEqual(self.slots(), {"1": ("one@example.com", "")})

    def test_add_respects_duplicate_email_rule(self):
        doc = remote_doc()
        doc["accounts"][1]["email"] = "one@example.com"
        self.remote.write_text(json.dumps(doc))
        self.assertEqual(self.run_add(1, FAKE_TOKEN)[0], 0)
        rc, out = self.run_add(2, FAKE_TOKEN2)
        self.assertEqual(rc, 1)
        self.assertIn("两个组织", out)
        self.assertEqual(self.slots(), {"1": ("one@example.com", "")})

    def test_bind_on_the_server_itself(self):
        # a machine whose own cswap has slot 1 (token) and binds slot 1 <- its own... use a token slot 4
        self.cswap_add_token(4)
        rc, out = self.run_tool("bind", "4=1")
        self.assertEqual(rc, 0, out)
        doc = remote_doc()
        doc["accounts"] = [doc["accounts"][0]]
        self.remote.write_text(json.dumps(doc))
        rc, out = self.run_tool("sync-usage", "--server", "srv", "--cswap", REAL_CSWAP)
        self.assertEqual(rc, 0, out)
        rows = {str(r["number"]): r for r in json.loads(self.cswap("policy", "list", "--json"))["accounts"]}
        self.assertAlmostEqual(rows["4"]["usage5h"], 12.0)
        rc, out = self.run_tool("status")
        self.assertIn("槽位 4", out)

    def cswap_add_token(self, slot):
        r = subprocess.run([REAL_CSWAP, "add-token", "-", "--slot", str(slot)], env=self.env, input=FAKE_TOKEN + "\n",
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_no_terminal_refuses_to_read_a_token(self):
        rc, out = self.run_tool("setup", "--server", "srv", "--cswap", REAL_CSWAP)
        self.assertEqual(rc, 1)
        self.assertIn("终端", out)
        self.assertNotIn("Traceback", out)

    def test_missing_ssh_is_a_clean_error(self):
        rc, out = self.run_tool("sync-usage", "--server", "srv", "--cswap", REAL_CSWAP, "--ssh", "no-such-ssh")
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", out)


class FakeCswapTests(unittest.TestCase):
    """What the satellite makes of cswap's output, with a scripted cswap."""

    NOTICE = ("A newer version of claude-swap is available (9.9.9). You are using 0.1.0. "
              "Run `cswap upgrade` to update.")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cswap-satellite-fake-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls"
        self.remote = self.tmp / "remote.json"
        self.remote.write_text(json.dumps(remote_doc()))
        data = self.tmp / "data" / "claude-swap"
        data.mkdir(parents=True)
        # local slot 4 is a token slot for two@example.com, so it takes the server's reading
        (data / "sequence.json").write_text(json.dumps({"activeAccountNumber": 1, "accounts": {
            "4": {"email": "two@example.com", "organizationUuid": ""}}}))
        (self.bin / "ssh").write_text(f"#!/bin/sh\ncat '{self.remote}'\n")
        os.chmod(self.bin / "ssh", 0o755)
        self.env = dict(os.environ, HOME=str(self.tmp), XDG_DATA_HOME=str(self.tmp / "data"),
                        XDG_CONFIG_HOME=str(self.tmp / "config"),
                        PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

    def fake_cswap(self, import_body):
        f = self.bin / "cswap"
        f.write_text(f'#!/bin/sh\necho "$*" >> "{self.calls}"\n'
                     f'case "$1" in\n  import-usage) cat > /dev/null\n{import_body}\n  ;;\n'
                     f'  list) echo \'{{"schemaVersion": 1, "accounts": []}}\' ;;\nesac\n')
        os.chmod(f, 0o755)
        return str(f)

    def sync(self, cswap):
        r = subprocess.run([sys.executable, str(HERE / "cswap_satellite.py"), "sync-usage", "--server", "srv",
                            "--cswap", cswap], env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=60)
        st = json.loads((self.tmp / "config" / "cswap-satellite" / "last-sync.json").read_text())
        return r, st

    def test_sync_records_the_import_result_not_the_update_notice(self):
        cswap = self.fake_cswap('    echo "Adopted usage for two@example.com -> slot 4" >&2\n'
                                '    echo "Done: 1 adopted, 0 kept, 0 skipped; fetching held for up to 900s" >&2\n'
                                f'    printf "\\n{self.NOTICE}\\n" >&2')
        r, st = self.sync(cswap)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(st["ok"])
        self.assertEqual(st["message"], "Done: 1 adopted, 0 kept, 0 skipped; fetching held for up to 900s")
        self.assertIn("Done: 1 adopted", r.stdout)

    def test_stdout_result_wins_and_errors_skip_the_notice(self):
        cswap = self.fake_cswap('    echo "progress" >&2\n    echo "imported 1"\n'
                                f'    echo "{self.NOTICE}" >&2')
        self.assertEqual(self.sync(cswap)[1]["message"], "imported 1")
        cswap = self.fake_cswap('    echo "Error: bad usage payload" >&2\n'
                                f'    echo "{self.NOTICE}" >&2\n    exit 1')
        r, st = self.sync(cswap)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(st["ok"])
        self.assertIn("bad usage payload", st["message"])
        self.assertNotIn("newer version", st["message"])

    def test_prepare_wakes_the_store_with_list_not_policy(self):
        cswap = self.fake_cswap("    :")
        a = cs.argparse.Namespace(server="srv", remote_cswap=None, cswap=cswap, ssh=str(self.bin / "ssh"),
                                  accounts="1")
        old = dict(os.environ)
        os.environ.update(XDG_CONFIG_HOME=self.env["XDG_CONFIG_HOME"], XDG_DATA_HOME=self.env["XDG_DATA_HOME"])
        try:
            cs.prepare(a, {})
        finally:
            os.environ.clear()
            os.environ.update(old)
        calls = self.calls.read_text().splitlines()
        self.assertIn("list --json", calls)
        self.assertFalse([c for c in calls if c.startswith("policy")], calls)


if __name__ == "__main__":
    unittest.main()
