"""install.sh on a fake bundle in a temporary home: fake uv, code / code-server,
api-worker installer and crontab, so nothing on this machine is touched and
nothing is downloaded. Checks what it installs, what it leaves alone, and that
one failing part does not stop the rest."""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SUITE = Path(__file__).resolve().parent.parent
# Claude Review joins the bundle with its own install step; until install.sh
# has one, the tests leave it out.
REVIEW = "claude-review" in (SUITE / "install.sh").read_text(encoding="utf-8")
IDS = {"cswap-status": "chenyu.cswap-status", "claude-review": "chenyu.claude-review",
       "cyuneo-agent-monitor": "cyuneo.cyuneo-agent-monitor"}

# A VS Code command line: extensions in $FAKE_VSCODE_STATE, one id@version per line
FAKE_CODE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os, re, sys
    state = os.environ["FAKE_VSCODE_STATE"]
    with open(state + ".calls", "a") as f:
        f.write(os.path.basename(sys.argv[0]) + " " + " ".join(sys.argv[1:]) + "\\n")
    have = dict(l.strip().split("@") for l in open(state) if "@" in l) if os.path.exists(state) else {}
    if "--list-extensions" in sys.argv:
        for k, v in have.items():
            print(f"{k}@{v}" if "--show-versions" in sys.argv else k)
        sys.exit(0)
    if "--install-extension" in sys.argv:
        name = os.path.basename(sys.argv[sys.argv.index("--install-extension") + 1])
        m = re.match(r"(.+)-(\\d[\\w.]*)\\.vsix$", name)
        ids = %r
        if not m or m.group(1) not in ids:
            sys.exit("cannot install " + name)
        have[ids[m.group(1)]] = m.group(2)
        open(state, "w").write("".join(f"{k}@{v}\\n" for k, v in have.items()))
        print("installed " + name)
        sys.exit(0)
    sys.exit(2)
    ''') % IDS

FAKE_UV = textwrap.dedent('''\
    #!/bin/sh
    # `uv tool install ... <wheel>`: put a cswap that accepts `config set`
    if [ "$1 $2" = "tool install" ]; then
      mkdir -p "$HOME/.local/bin"
      printf '#!/bin/sh\\nexit 0\\n' > "$HOME/.local/bin/cswap"
      chmod +x "$HOME/.local/bin/cswap"
    fi
    ''')


def script(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home, self.fakebin, self.bundle = t / "home", t / "bin", t / "bundle"
        self.home.mkdir()
        b = self.bundle
        b.mkdir()
        shutil.copy2(SUITE / "install.sh", b / "install.sh")
        (b / "VERSION").write_text("cswap-suite test\n")
        (b / "cswap").mkdir()
        (b / "cswap" / "claude_swap-1.0-py3-none-any.whl").write_text("")
        for vsix in ("cswap-status-0.13.1", "cyuneo-agent-monitor-0.9.0") + (("claude-review-0.6.2",) if REVIEW else ()):
            (b / f"{vsix}.vsix").write_text("")
        for f in ("cswap-hooks/on-switch.sh", "cswap-satellite/cswap_satellite.py", "cswap-remote/cswap_remote.py"):
            script(b / f, "#!/bin/sh\n")
        script(b / "api-worker" / "install.sh", '#!/bin/sh\n[ -z "$FAKE_API_FAIL" ] || { echo "no network" >&2; exit 1; }\n')
        (b / "systemd").mkdir()
        script(self.fakebin / "uv", FAKE_UV)
        script(self.fakebin / "crontab", '#!/bin/sh\n[ "$1" = -l ] && [ -n "$FAKE_CRONTAB" ] && printf "%s\\n" "$FAKE_CRONTAB" && exit 0\nexit 1\n')
        self.state = t / "vscode-state"
        self.code = self.fakebin / "code"
        script(self.code, FAKE_CODE)

    def tearDown(self):
        self.tmp.cleanup()

    def have(self, **exts):
        if not REVIEW:
            exts.pop("claude_review", None)
        self.state.write_text("".join(f"{IDS[k.replace('_', '-')]}@{v}\n" for k, v in exts.items()))

    @staticmethod
    def all_bundled(status, review, monitor):
        out = {"chenyu.cswap-status": status, "cyuneo.cyuneo-agent-monitor": monitor}
        if REVIEW:
            out["chenyu.claude-review"] = review
        return out

    def installed(self):
        return dict(l.split("@") for l in self.state.read_text().split()) if self.state.exists() else {}

    def calls(self):
        p = Path(str(self.state) + ".calls")
        return p.read_text().splitlines() if p.exists() else []

    def run_install(self, *args, **env):
        e = {"HOME": str(self.home), "PATH": f"{self.fakebin}:/usr/bin:/bin", "FAKE_VSCODE_STATE": str(self.state), "LANG": "C.UTF-8"}
        e.update(env)
        r = subprocess.run([str(self.bundle / "install.sh"), *args], env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def test_fresh_machine_gets_every_extension(self):
        out = self.run_install()
        self.assertEqual(self.installed(), self.all_bundled("0.13.1", "0.6.2", "0.9.0"))
        self.assertIn("VS Code 插件 0.13.1 已装", out)
        self.assertTrue((self.home / ".local/share/cswap-suite/install.sh").exists())

    def test_a_newer_installed_extension_is_not_downgraded(self):
        self.have(cswap_status="0.14.0", claude_review="0.7.0", cyuneo_agent_monitor="1.0.0")
        out = self.run_install()
        self.assertEqual(self.installed(), self.all_bundled("0.14.0", "0.7.0", "1.0.0"))
        self.assertFalse([c for c in self.calls() if "--install-extension" in c])
        self.assertIn("VS Code 插件 0.14.0 已经装了（包里是 0.13.1）", out)

    def test_same_version_is_left_alone_and_older_is_updated(self):
        self.have(cswap_status="0.13.1", claude_review="0.6.1")
        self.run_install()
        installs = [c for c in self.calls() if "--install-extension" in c]
        self.assertFalse([c for c in installs if "cswap-status" in c])
        if REVIEW:
            self.assertEqual(self.installed()["chenyu.claude-review"], "0.6.2")

    def test_force_vscode_installs_the_bundled_versions(self):
        self.have(cswap_status="0.14.0", claude_review="0.7.0", cyuneo_agent_monitor="1.0.0")
        self.run_install("--force-vscode")
        self.assertEqual(self.installed(), self.all_bundled("0.13.1", "0.6.2", "0.9.0"))

    def test_without_code_the_newest_vscode_server_installs(self):
        self.code.unlink()
        servers = self.home / ".vscode-server" / "cli" / "servers"
        old = servers / "Stable-zzz" / "server" / "bin" / "code-server"
        new = servers / "Stable-aaa" / "server" / "bin" / "code-server"
        script(old, "#!/bin/sh\nexit 9\n")
        script(new, FAKE_CODE)
        os.utime(old, (1, 1))  # names are hashes: the newest by time wins
        out = self.run_install()
        self.assertEqual(self.installed()["chenyu.cswap-status"], "0.13.1", out)
        self.assertTrue(all(c.startswith("code-server ") for c in self.calls()))

    def test_without_any_vscode_it_says_how(self):
        self.code.unlink()
        out = self.run_install()
        self.assertIn("code --install-extension", out)
        self.assertEqual(self.installed(), {})

    def test_a_failed_api_worker_does_not_stop_the_rest(self):
        out = self.run_install(FAKE_API_FAIL="1")
        self.assertIn("api-worker 没装好", out)
        self.assertEqual(self.installed()["chenyu.cswap-status"], "0.13.1")

    def test_warns_about_a_second_engine_in_crontab(self):
        out = self.run_install(FAKE_CRONTAB="* * * * * $HOME/.local/bin/cswap auto --once >> x 2>&1")
        self.assertIn("crontab 里还有一行在运行 cswap auto", out)
        out = self.run_install(FAKE_CRONTAB="# * * * * * cswap auto --once\n0 3 * * * backup")
        self.assertNotIn("crontab 里还有", out)

    def test_running_it_again_is_fine(self):
        self.run_install()
        out = self.run_install()
        self.assertIn("已经装了", out)


if __name__ == "__main__":
    unittest.main()
