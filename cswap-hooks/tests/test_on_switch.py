"""Tests for on-switch.sh, run through a symlink the way cswap calls it.

Run: python3 -m unittest discover -s tests

Everything happens in a temporary HOME / XDG dirs / CLAUDE_CONFIG_DIR.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]


class OnSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cswap-hooks-test-"))
        (self.tmp / "bin").mkdir()
        self.link = self.tmp / "bin" / "cswap-on-switch"
        self.link.symlink_to(HOOKS / "on-switch.sh")
        self.claude = self.tmp / "claude"
        self.claude.mkdir()
        self.log = self.tmp / "state" / "cswap-hooks" / "hook.log"
        self.env = dict(os.environ, HOME=str(self.tmp), XDG_STATE_HOME=str(self.tmp / "state"),
                        XDG_CONFIG_HOME=str(self.tmp / "config"), CLAUDE_CONFIG_DIR=str(self.claude),
                        CSWAP_FROM="2", CSWAP_TO="3", CSWAP_TO_EMAIL="someone@example.invalid",
                        CSWAP_TRIGGER="manual")

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

    def run_hook(self, **env):
        r = subprocess.run([str(self.link)], env=dict(self.env, **env), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def pin(self, level):
        p = self.tmp / "config" / "cswap-hooks" / "effort.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"level": level}))

    def test_logs_numbers_and_trigger_only(self):
        self.run_hook()
        self.run_hook(CSWAP_FROM="3", CSWAP_TO="1", CSWAP_TRIGGER="policy-limit")
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].endswith("switch 2 -> 3 (manual)"), lines[0])
        self.assertTrue(lines[1].endswith("switch 3 -> 1 (policy-limit)"), lines[1])
        self.assertNotIn("example.invalid", self.log.read_text())
        self.assertFalse((self.tmp / ".cswap-remote-control.log").exists())

    def test_pins_effort_once_and_is_quick(self):
        self.pin("high")
        t0 = time.monotonic()
        self.run_hook()
        self.assertLess(time.monotonic() - t0, 5)
        settings = self.claude / "settings.json"
        ms = json.loads(settings.read_text())["modelSettings"]
        self.assertTrue(ms and all(v["effortLevel"] == "high" for v in ms.values()), ms)
        before = (settings.read_text(), settings.stat().st_mtime_ns)
        self.run_hook()  # nothing differs: settings.json is not rewritten
        self.assertEqual((settings.read_text(), settings.stat().st_mtime_ns), before)

    def test_not_pinned_leaves_settings_alone(self):
        self.run_hook()
        self.assertFalse((self.claude / "settings.json").exists())

    def test_log_stays_short(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_text("".join(f"old line {i}\n" for i in range(1200)))
        self.run_hook()
        lines = self.log.read_text().splitlines()
        self.assertLessEqual(len(lines), 1000)
        self.assertTrue(lines[-1].endswith("switch 2 -> 3 (manual)"))

    def test_no_restart_and_no_fixed_paths(self):
        text = (HOOKS / "on-switch.sh").read_text()
        self.assertNotIn("restart-remote-control", text)
        self.assertNotIn("/mnt/", text)
        self.assertNotIn("CSWAP_TO_EMAIL", text)


if __name__ == "__main__":
    unittest.main()
