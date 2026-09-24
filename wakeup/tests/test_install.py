"""Controls for macos/install.sh against a copy of the kit whose client pin names a fixture served by a local HTTP
server: a fresh install, an upgrade whose download fails (the active bundle untouched), a successful upgrade (the
previous bundle kept), and the refusal while a watcher runs. No real server; skipped on Windows."""
import hashlib
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
STUB_CLIENT = b'''URL = "https://example.invalid"
def load_json(p): return {}
def save_json(p, d): pass
def file_lock(p, blocking=True): import contextlib; return contextlib.nullcontext()
def scoped_positions(raw): return {}
def put_position(raw, t, r): pass
class Client:
    def __init__(self, name): self.name = name; self.home = "/nonexistent"
'''


@unittest.skipUnless(sys.platform != "win32" and shutil.which("pgrep"), "the macOS installer: sh, pgrep")
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.kit = self.root / "kit" / "wakeup"
        for sub in ("common", "macos"):
            shutil.copytree(KIT / sub, self.kit / sub)
        (self.kit / "common" / "client.sha256").write_text(hashlib.sha256(STUB_CLIENT).hexdigest() + "  agentariat.py\n")
        self.serve_ok = True
        tests = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if not tests.serve_ok:
                    self.send_response(500); self.end_headers(); return
                self.send_response(200); self.end_headers(); self.wfile.write(STUB_CLIENT)

            def log_message(self, *a):
                pass
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.dir = self.root / "runtime"
        self.env = dict(os.environ, AGENTARIAT_URL="http://127.0.0.1:%d" % self.server.server_port)

    def tearDown(self):
        self.server.shutdown()

    def install(self, *args):
        out = subprocess.run(["sh", str(self.kit / "macos" / "install.sh"), str(self.dir), *args], capture_output=True, text=True, env=self.env, timeout=120)
        return out.returncode, out.stdout + out.stderr

    def digests(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.dir.iterdir() if not p.name.startswith(".")}   # .running is the mark

    def test_fresh_install_upgrade_failure_successful_upgrade_and_running_watcher_refusal(self):
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["agentariat-watch.py", "agentariat.py", "client.sha256", "get-client.py", "wake-claude.py", "wake-codex.sh"])
        self.assertEqual(subprocess.run([sys.executable, str(self.dir / "get-client.py"), "--check"], capture_output=True).returncode, 0)
        self.assertEqual([p.name for p in self.root.iterdir() if "staging" in p.name], [])
        before = self.digests()
        # an upgrade whose download fails leaves the active bundle exactly as it was, and no staging behind
        (self.kit / "macos" / "wake-codex.sh").write_text("#!/bin/sh\necho new release\n")
        self.serve_ok = False
        code, out = self.install()
        self.assertEqual(code, 2, out)
        self.assertIn("untouched", out)
        self.assertEqual(self.digests(), before)
        self.assertEqual([p.name for p in self.root.iterdir() if "staging" in p.name], [])
        self.assertFalse((self.root / "runtime.previous").exists())
        # a pin mismatch is the same: the pin changes, the server still serves the old bytes
        self.serve_ok = True
        (self.kit / "common" / "client.sha256").write_text("0" * 64 + "  agentariat.py\n")
        code, out = self.install()
        self.assertEqual(code, 2, out)
        self.assertEqual(self.digests(), before)
        (self.kit / "common" / "client.sha256").write_text(hashlib.sha256(STUB_CLIENT).hexdigest() + "  agentariat.py\n")
        # a successful upgrade replaces the bundle in one move and keeps the previous one
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertIn("previous bundle kept", out)
        self.assertEqual((self.dir / "wake-codex.sh").read_text(), "#!/bin/sh\necho new release\n")
        self.assertEqual(hashlib.sha256((self.root / "runtime.previous" / "wake-codex.sh").read_bytes()).hexdigest(), before["wake-codex.sh"])
        # a watcher started by RELATIVE path from the directory holds the running mark: an upgrade is refused however it
        # was launched, until it is stopped or the install is forced
        hold = ("import importlib.util, sys, time; s = importlib.util.spec_from_file_location('w', 'agentariat-watch.py'); "
                "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); m.hold_running_mark('.'); print('held', flush=True); time.sleep(30)")
        watcher = subprocess.Popen([sys.executable, "-c", hold], cwd=str(self.dir), stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(watcher.stdout.readline().strip(), "held")
            code, out = self.install()
            self.assertEqual(code, 1, out)
            self.assertIn("stop it first", out)
            self.assertEqual((self.dir / "wake-codex.sh").read_text(), "#!/bin/sh\necho new release\n")   # untouched
            code, out = self.install("--force")
            self.assertEqual(code, 0, out)
        finally:
            watcher.kill()
        # an activation failure (the final rename refused) puts the previous bundle back
        site = self.root / "site"; site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import os\n_rename = os.rename\n"
            "def rename(a, b):\n    if os.path.basename(b) == 'runtime' and '.staging.' in os.path.basename(a): raise OSError('injected activation failure')\n    return _rename(a, b)\n"
            "os.rename = rename\n")
        before = self.digests()
        env = dict(self.env, PYTHONPATH=str(site))
        out = subprocess.run(["sh", str(self.kit / "macos" / "install.sh"), str(self.dir)], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("previous bundle is back", out.stdout + out.stderr)
        self.assertEqual(self.digests(), before)
        self.assertEqual([p.name for p in self.root.iterdir() if "staging" in p.name], [])
        # a restoration failure on top (both renames refused) keeps the candidate the diagnostic names, and the previous bundle
        (site / "sitecustomize.py").write_text(
            "import os\n_rename = os.rename\n"
            "def rename(a, b):\n    if os.path.basename(b) == 'runtime': raise OSError('injected failure of every rename into runtime')\n    return _rename(a, b)\n"
            "os.rename = rename\n")
        out = subprocess.run(["sh", str(self.kit / "macos" / "install.sh"), str(self.dir)], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("RESTORATION FAILED", out.stderr)
        self.assertFalse(self.dir.exists())                                            # nothing is active
        self.assertEqual(hashlib.sha256((self.root / "runtime.previous" / "wake-codex.sh").read_bytes()).hexdigest(), before["wake-codex.sh"])
        staged = [p for p in self.root.iterdir() if "staging" in p.name]
        self.assertEqual(len(staged), 1, "the candidate must stay for the operator")
        self.assertIn(str(staged[0]), out.stderr)                                      # and the diagnostic names where it is
        self.assertTrue((staged[0] / "agentariat-watch.py").exists())
        (self.root / "runtime.previous").rename(self.dir)                              # the operator's by-hand recovery
        shutil.rmtree(staged[0])
