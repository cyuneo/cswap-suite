"""build.py helpers that run without building anything."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("build", Path(__file__).resolve().parent.parent / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class StampPluginVersion(unittest.TestCase):
    def test_a_changed_file_changes_the_version(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / ".claude-plugin").mkdir()
            manifest = d / ".claude-plugin" / "plugin.json"
            manifest.write_text(json.dumps({"name": "api-worker", "version": "0.4.2"}))
            (d / "worker.py").write_text("a")
            v1 = build.stamp_plugin_version(d)
            self.assertRegex(v1, r"^0\.4\.2\+[0-9a-f]{8}$")
            self.assertEqual(json.loads(manifest.read_text())["name"], "api-worker")
            self.assertEqual(build.stamp_plugin_version(d), v1, "stamping again keeps one suffix")
            (d / "worker.py").write_text("b")
            v2 = build.stamp_plugin_version(d)
            self.assertNotEqual(v1, v2)
            self.assertTrue(v2.startswith("0.4.2+"))


if __name__ == "__main__":
    unittest.main()
