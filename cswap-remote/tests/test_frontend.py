"""Checks for the static web UI (static/*.html, *.js, *.css).

Run: python3 -m unittest discover -s tests
The JavaScript unit tests (tests/md_test.js, tests/app_test.js) run too when a
node binary is found (PATH, or the one bundled with VS Code Server).
"""

import glob
import html.parser
import http.client
import os
import re
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
sys.path.insert(0, str(ROOT))
import cswap_remote as cr  # noqa: E402


class _Tags(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.inline_scripts, self.styles, self.style_attrs, self.handlers = [], 0, 0, 0, []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self._in_script = True
            if a.get("src"):
                self.scripts.append(a["src"])
        if tag == "style":
            self.styles += 1
        if "style" in a:
            self.style_attrs += 1
        self.handlers += [k for k in a if k.startswith("on")]

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline_scripts += 1


def find_node():
    node = shutil.which("node")
    if node:
        return node
    found = sorted(glob.glob(os.path.expanduser("~/.vscode-server/cli/servers/*/server/node")))
    return found[-1] if found else None


class StaticFileTests(unittest.TestCase):
    def test_index_is_a_shell_without_inline_code(self):
        p = _Tags()
        p.feed((STATIC / "index.html").read_text("utf-8"))
        self.assertEqual(p.scripts, ["md.js", "app.js"])
        self.assertEqual(p.inline_scripts, 0)
        self.assertEqual(p.styles, 0)
        self.assertEqual(p.style_attrs, 0)
        self.assertEqual(p.handlers, [])
        self.assertIn('href="style.css"', (STATIC / "index.html").read_text("utf-8"))

    def test_js_builds_no_inline_handlers_or_style_attributes(self):
        # innerHTML strings would need 'unsafe-inline' for these; DOM APIs do not.
        for name in ("app.js", "md.js"):
            src = (STATIC / name).read_text("utf-8")
            self.assertNotRegex(src, r"""[\s"'`]on[a-z]+=["']""", name)
            self.assertNotIn('style="', src, name)
            self.assertNotIn("eval(", src, name)
            self.assertNotIn("new Function", src, name)

    def test_untrusted_text_only_reaches_innerhtml_through_md(self):
        src = (STATIC / "app.js").read_text("utf-8")
        uses = re.findall(r"html: ([^}]+?) \}", src)
        self.assertTrue(uses)
        for u in uses:
            self.assertTrue(u.startswith("md.render("), u)
        self.assertEqual(len(re.findall(r"\.innerHTML\s*=", src)), 1)  # inside h(), fed by `html:` only

    def test_api_paths_used_by_the_ui_exist(self):
        src = (STATIC / "app.js").read_text("utf-8")
        for action in ("stream", "events", "answer", "interrupt", "mode", "pane", "git", "send", "key",
                       "kill", "start", "toolresult", "agent"):
            self.assertIn(f"/{action}", src.replace("session/${k}/", "/").replace("session/${ctx.k}/", "/"), action)


class StaticServingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class App:
            cfg = {"token": "t0k", "ntfy": {}}
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cr.make_handler(App()))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_assets_and_csp(self):
        for path, ctype in (("/", "text/html"), ("/app.js", "text/javascript"), ("/md.js", "text/javascript"),
                            ("/style.css", "text/css")):
            r, body = self.get(path)
            self.assertEqual(r.status, 200, path)
            self.assertTrue(r.getheader("Content-Type").startswith(ctype), path)
            self.assertTrue(body, path)
            csp = r.getheader("Content-Security-Policy")
            script_src = re.search(r"script-src ([^;]*)", csp).group(1).split()
            self.assertEqual(script_src, ["'self'"], csp)
            self.assertIn("connect-src 'self'", csp)
            self.assertIn("frame-ancestors 'none'", csp)


@unittest.skipUnless(find_node(), "node not found")
class JavaScriptTests(unittest.TestCase):
    def run_node(self, *args):
        p = subprocess.run([find_node(), *args], cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def test_syntax(self):
        for name in ("app.js", "md.js"):
            self.run_node("--check", str(STATIC / name))

    def test_markdown_renderer(self):
        self.assertIn(" 0 failed", self.run_node(str(ROOT / "tests" / "md_test.js")))

    def test_app_helpers(self):
        self.assertIn(" 0 failed", self.run_node(str(ROOT / "tests" / "app_test.js")))


if __name__ == "__main__":
    unittest.main()
