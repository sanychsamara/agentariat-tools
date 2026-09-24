"""Controls for the Windows Codex adapter's Python logic, run on any platform with the Win32 probe stubbed: the
launch boundary (no exit 1 after launch, undecodable output included), the receipt rules, and the bounded busy wait.
Not a Windows runtime test; the native probe and installer stay unverified until run there."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent.parent / "windows" / "wake-codex-win.py"
THREAD = "11111111-1111-4111-8111-111111111111"


def load():
    spec = importlib.util.spec_from_file_location("wake_codex_win", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WindowsLogicTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        os.environ["CODEX_HOME"] = str(self.home)
        self.win = load()
        self.real_subprocess = self.win.subprocess
        self.win.CODEX_HOME = str(self.home)
        db = sqlite3.connect(self.home / "state_9.sqlite")
        db.execute("create table threads (id text, rollout_path text, created_at integer, updated_at integer, source text, model_provider text, cwd text, title text, sandbox_policy text, approval_mode text, archived integer default 0)")
        db.execute("insert into threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode) values (?, '/r', 1, 1, 'cli', 'p', ?, 't', 's', 'a')", (THREAD, str(self.home)))
        db.commit(); db.close()
        db = sqlite3.connect(self.home / "queue_10.sqlite")
        db.execute("create table queued_items (id text primary key not null, thread_id text not null, payload_json text not null, queue_order integer not null, created_at_ms integer not null, updated_at_ms integer not null)")
        db.commit(); db.close()
        self.win.lock_held = lambda path: True
        self.stub = self.home / "codex.py"
        self.win.codex_binary = lambda: sys.executable

    def tearDown(self):
        self.win.subprocess = self.real_subprocess

    def codex(self, body):
        """The stubbed codex, through a shim in place of the module's subprocess reference (never the global module)."""
        import types
        self.stub.write_text(body)
        stub, real = self.stub, self.real_subprocess
        self.win.subprocess = types.SimpleNamespace(run=lambda argv, **kw: real.run([sys.executable, str(stub)] + argv[1:], **kw))

    def run_main(self, env=None):
        argv, sys.argv = sys.argv, ["wake-codex-win.py", THREAD, "hello"]
        old = dict(os.environ); os.environ.update(env or {})
        try:
            try:
                return self.win.main(), None
            except SystemExit as e:
                return e.code, "exit"
        finally:
            sys.argv = argv; os.environ.clear(); os.environ.update(old)

    def pending(self):
        db = sqlite3.connect(self.home / "queue_10.sqlite"); rows = db.execute("select id from queued_items").fetchall(); db.close(); return [r[0] for r in rows]

    def insert(self, extra=""):
        return ("import sqlite3, sys\ndb = sqlite3.connect(%r); db.execute(\"insert into queued_items values ('it-1', %r, '{}', 1, 0, 0)\"); db.commit()\n%s"
                % (str(self.home / "queue_10.sqlite"), THREAD, extra))

    def test_accepted_then_undecodable_output_and_accepted_then_no_receipt_are_unconfirmed(self):
        self.codex(self.insert("sys.stdout.buffer.write(b'Queued message it-1 for thread x\\xff\\xfe\\n'); sys.exit(3)"))
        code, how = self.run_main({"WAKE_TIMEOUT": "1"})
        self.assertEqual(code, 2, how)                                   # a receipt with junk bytes: submitted, then not taken in time
        self.assertEqual(self.pending(), ["it-1"])
        self.codex(self.insert("sys.stdout.buffer.write(b'\\xff\\xfe garbage\\n'); sys.exit(3)"))
        db = sqlite3.connect(self.home / "queue_10.sqlite"); db.execute("delete from queued_items"); db.commit(); db.close()
        code, how = self.run_main({"WAKE_TIMEOUT": "1"})
        self.assertEqual(code, 2, how)                                   # undecodable, no receipt: unconfirmed, never 1
        self.codex("import sys; print('error: no rollout found for thread id', file=sys.stderr); sys.exit(1)")
        code, how = self.run_main({"WAKE_TIMEOUT": "1"})
        self.assertEqual(code, 2, how)                                   # no receipt after launch is never "not sent"
        db = sqlite3.connect(self.home / "queue_10.sqlite"); db.execute("delete from queued_items"); db.commit(); db.close()
        observed = []
        def boom(item, deadline):
            observed.append(item); raise RuntimeError("boom")
        self.win.observe = boom
        self.codex(self.insert("print('Queued message it-1 for thread x')"))   # a fresh row, so the child's insert succeeds
        code, how = self.run_main({"WAKE_TIMEOUT": "1"})
        self.assertEqual(code, 2, how)                                   # an exception during observation: unconfirmed
        self.assertEqual(len(observed), 1, "observation must have been attempted")

    def test_the_busy_bound_is_enforced_with_a_monotonic_deadline_and_the_probe_seconds_are_validated(self):
        state = self.home / "state_9.sqlite"

        def hold(seconds):
            db = sqlite3.connect(state, isolation_level=None); db.execute("begin exclusive"); time.sleep(seconds); db.execute("rollback"); db.close()
        t = threading.Thread(target=hold, args=(2.5,)); t.start(); time.sleep(0.3)
        started = time.monotonic()
        with self.assertRaises(SystemExit) as caught:
            self.win.query_wait(str(state), "select id from threads", seconds=1)
        elapsed = time.monotonic() - started
        t.join()
        self.assertIn("busy", str(caught.exception))
        self.assertLess(elapsed, 2.0, "the wait must not outlive its bound")
        t = threading.Thread(target=hold, args=(1.0,)); t.start(); time.sleep(0.2)
        rows = self.win.query_wait(str(state), "select id from threads", seconds=5)
        t.join()
        self.assertEqual(rows, [(THREAD,)])                              # released within the bound: proceeds
        for bad in ("inf", "0", "1.5", "601", "-1", "abc"):
            self.codex("print('never launched')")
            code, how = self.run_main({"WAKE_PROBE_SECONDS": bad, "WAKE_TIMEOUT": "1"})
            self.assertEqual(how, "exit", bad); self.assertIn("WAKE_PROBE_SECONDS", str(code))
