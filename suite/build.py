#!/usr/bin/env python3
"""Builds dist/cswap-suite.tar.gz: everything a new machine needs, installed
by its install.sh. cswap goes in as a wheel built from claude-swap/, so the
new machine downloads nothing from the original claude-swap project (only
PyPI dependencies such as textual and claude-agent-sdk).

    python3 suite/build.py                # runs suite/test.sh first
    python3 suite/build.py --skip-tests

Tests, caches and git data are left out, file modes are normalised (the
sources live on a drive where everything is 0777), and the build stops if a
file looks like it holds a token.
"""

import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "suite"
OUT = ROOT / "dist" / "cswap-suite.tar.gz"
TOOLS = ["cswap-hooks", "cswap-satellite", "cswap-remote", "api-worker"]
SKIP = {"tests", "__pycache__", ".git", ".pytest_cache", "node_modules", ".venv"}
TOKEN = re.compile(rb"sk-ant-(?:api|oat|admin|usr|ort)[0-9a-z]*-[A-Za-z0-9_\-]{20,}")
SECRET_NAMES = re.compile(r"(^|/)(\.env|\.credentials\.json|credentials|keys?)(/|$)")


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def ignore(_dir, names):
    return [n for n in names if n in SKIP or n.endswith((".pyc", ".vsix", ".tar.gz"))]


def versions(cswap_dir, wheel):
    commit = run("git", "rev-parse", "--short", "HEAD", cwd=cswap_dir)
    dirty = "+未提交的改动" if run("git", "status", "--porcelain", cwd=cswap_dir) else ""
    pkg = json.loads((ROOT / "cswap-status-vscode" / "package.json").read_text(encoding="utf-8"))
    worker = json.loads((ROOT / "api-worker" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    cswap_version = wheel.name.split("-")[1]
    suite = run("git", "rev-parse", "--short", "HEAD", cwd=ROOT)
    suite_dirty = "+未提交的改动" if run("git", "status", "--porcelain", cwd=ROOT) else ""
    return [
        f"cswap-suite {time.strftime('%Y-%m-%d')} ({suite}{suite_dirty})",
        f"cswap {cswap_version} ({commit}{dirty})",
        f"cswap-status {pkg['version']}",
        f"api-worker {worker.get('version', '?')}",
    ]


def check_secrets(top):
    """Stops the build on token-looking text in any file, wheel members included."""
    for p in sorted(top.rglob("*")):
        rel = p.relative_to(top).as_posix()
        if SECRET_NAMES.search(rel) and not p.is_dir():
            sys.exit(f"refusing to pack {rel}: looks like a credentials file")
        if not p.is_file():
            continue
        blobs = [p.read_bytes()]
        if p.suffix in (".whl", ".vsix"):
            with zipfile.ZipFile(p) as z:
                blobs += [z.read(n) for n in z.namelist()]
        if any(TOKEN.search(b) for b in blobs):
            sys.exit(f"refusing to pack {rel}: it contains something that looks like a token")


def mode_filter(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o644
    return info


def main():
    if "--skip-tests" not in sys.argv[1:]:
        if subprocess.run([str(SUITE / "test.sh")]).returncode != 0:
            sys.exit("tests failed: nothing was packed (--skip-tests packs anyway)")
    cswap_dir = ROOT / "claude-swap"
    with tempfile.TemporaryDirectory() as tmp:
        top = Path(tmp) / "cswap-suite"
        top.mkdir()
        # cswap as a wheel
        run("uv", "build", "--wheel", "--out-dir", str(top / "cswap"), cwd=cswap_dir)
        (top / "cswap" / ".gitignore").unlink(missing_ok=True)
        info = versions(cswap_dir, next((top / "cswap").glob("claude_swap-*.whl")))
        shutil.copy2(cswap_dir / "LICENSE", top / "cswap" / "LICENSE")
        # the VS Code extension, built from its current sources
        run(sys.executable, str(ROOT / "cswap-status-vscode" / "build-vsix.py"), "--out", str(top))
        for name in TOOLS:
            shutil.copytree(ROOT / name, top / name, ignore=ignore)
        shutil.copy2(SUITE / "install.sh", top / "install.sh")
        shutil.copy2(SUITE / "README.md", top / "README.md")
        shutil.copytree(SUITE / "systemd", top / "systemd")
        (top / "VERSION").write_text("\n".join(info) + "\n", encoding="utf-8")
        check_secrets(top)
        # executables: anything starting with #!
        execs = {p for p in top.rglob("*") if p.is_file() and p.read_bytes()[:2] == b"#!"}
        OUT.parent.mkdir(exist_ok=True)
        with tarfile.open(OUT, "w:gz") as tar:

            def add_filter(ti):
                ti = mode_filter(ti)
                if (Path(tmp) / ti.name) in execs:
                    ti.mode = 0o755
                return ti

            tar.add(top, arcname="cswap-suite", filter=add_filter)
    print(OUT)
    print("\n".join(info))


if __name__ == "__main__":
    main()
