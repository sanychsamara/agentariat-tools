"""Controls for wake-codex.sh against fixture Codex stores, a stubbed `codex` and a stubbed `lsof`. Nothing here runs
a real Codex or touches ~/.codex. Skipped where bash or sqlite3 is missing (Windows uses wake-codex-win.py)."""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "macos" / "wake-codex.sh"
LIVE = "11111111-1111-4111-8111-111111111111"
OLD = "22222222-2222-4222-8222-222222222222"


@unittest.skipUnless(shutil.which("bash") and shutil.which("sqlite3"), "bash and sqlite3 are needed for the POSIX adapter")
class WakeCodexTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.bin = Path(tempfile.mkdtemp())
        self.project = Path(tempfile.mkdtemp())
        (self.home / "thread-writer-locks").mkdir()
        self.state(9, [(LIVE, 200), (OLD, 100)])
        self.queue(9)
        self.queue(10)
        self.lock_state = {}                                   # thread -> lsof exit code when its lock exists
        self.codex_mode = "queue"                              # what the stubbed codex does
        self.item = "item-abc"
        self.write_stubs()

    def state(self, n, threads):
        db = sqlite3.connect(self.home / f"state_{n}.sqlite")
        db.execute("create table threads (id text primary key, rollout_path text, created_at integer, updated_at integer, source text, model_provider text, cwd text, title text, sandbox_policy text, approval_mode text, archived integer default 0)")
        for tid, updated in threads:
            db.execute("insert into threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode) values (?, ?, ?, ?, 'cli', 'p', ?, 't', 's', 'a')",
                       (tid, "/r", updated, updated, str(self.project.resolve())))
        db.commit(); db.close()

    def queue(self, n, items=()):
        db = sqlite3.connect(self.home / f"queue_{n}.sqlite")
        db.execute("create table if not exists queued_items (id text primary key not null, thread_id text not null, payload_json text not null, queue_order integer not null, created_at_ms integer not null, updated_at_ms integer not null)")
        for i, (item, thread) in enumerate(items):
            db.execute("insert into queued_items values (?, ?, '{}', ?, 0, 0)", (item, thread, i))
        db.commit(); db.close()

    def write_stubs(self):
        # lsof -t <lock>: exit code from the control file named after the lock; a second word means "with a diagnostic on stderr"
        (self.bin / "lsof").write_text("#!/bin/sh\nlock=$2\nc=\"$lock.probe\"\nif [ -f \"$c\" ]; then read -r code err < \"$c\"; [ -n \"$err\" ] && echo \"lsof: status error on $lock\" >&2; exit \"$code\"; fi\nexit 1\n")
        # codex queue --thread T --message M: inserts into queue_10 (or misbehaves per mode) and prints Codex's receipt
        (self.bin / "codex").write_text(f'''#!/usr/bin/env python3
import sqlite3, sys
from pathlib import Path
mode = Path({str(self.home)!r}) / "codex.mode"
mode = mode.read_text().strip() if mode.exists() else "queue"
thread = sys.argv[sys.argv.index("--thread") + 1]
item = {self.item!r}
if mode == "fail-silent":
    print("error: no rollout found for thread id", file=sys.stderr); sys.exit(1)
import time
db = sqlite3.connect(Path({str(self.home)!r}) / "queue_10.sqlite")
db.execute("insert into queued_items values (?, ?, '{{}}', 99, ?, 0)", (item, thread, int(time.time() * 1000))); db.commit()
if mode == "accepted-no-receipt": sys.exit(3)
if mode == "accepted-consumed":                    # accepted, taken as a turn at once, and no receipt: the worst case
    db.execute("delete from queued_items where id = ?", (item,)); db.commit(); sys.exit(3)
print("Queued message %s for thread %s" % (item, thread))
if mode == "fail-after-receipt": sys.exit(3)
''')
        for f in ("lsof", "codex"):
            os.chmod(self.bin / f, 0o755)

    def live(self, thread, code=0, diagnostic=False):
        lock = self.home / "thread-writer-locks" / f"{thread}.lock"
        lock.write_text("")
        (self.home / "thread-writer-locks" / f"{thread}.lock.probe").write_text(f"{code} {'error' if diagnostic else ''}")

    def run_wake(self, *args, timeout="2", env_extra=None, path_extra=True):
        env = dict(os.environ, CODEX_HOME=str(self.home), WAKE_TIMEOUT=timeout)
        env["PATH"] = (str(self.bin) + os.pathsep if path_extra else "") + os.environ["PATH"]
        env.update(env_extra or {})
        out = subprocess.run(["bash", str(SCRIPT), *(args or [str(self.project)])], capture_output=True, text=True, env=env, timeout=60)
        return out.returncode, out.stdout + out.stderr

    def pending(self, n=10):
        db = sqlite3.connect(self.home / f"queue_{n}.sqlite")
        rows = db.execute("select id, thread_id from queued_items").fetchall(); db.close()
        return rows

    def take(self, item, n=10):
        db = sqlite3.connect(self.home / f"queue_{n}.sqlite")
        db.execute("delete from queued_items where id = ?", (item,)); db.commit(); db.close()

    def test_a_live_session_is_found_and_the_exact_item_is_confirmed_from_the_newest_store(self):
        self.live(LIVE)
        self.queue(9, [("stale", LIVE)])                               # an older store with a row must not decide anything
        self.queue(10, [("other", LIVE)])                              # another item for the same thread stays pending
        import threading
        threading.Timer(0.8, lambda: self.take(self.item)).start()
        code, out = self.run_wake(timeout="5")
        self.assertEqual(code, 0, out)
        self.assertIn(f"Queued message {self.item} for thread {LIVE}", out)
        self.assertIn("delivered", out)
        self.assertEqual([r[0] for r in self.pending()], ["other"])   # ours was taken; the other item did not block confirmation

    def test_still_queued_is_exit_2_with_the_receipt_and_an_older_empty_store_never_means_delivered(self):
        self.live(LIVE)
        code, out = self.run_wake(timeout="1")
        self.assertEqual(code, 2, out)
        self.assertIn(f"Queued message {self.item}", out)
        self.assertEqual(self.pending()[0][0], self.item)               # the row in queue_10 was the truth; queue_9 (empty) was not consulted

    def test_a_failure_after_submission_is_exit_2_never_1(self):
        self.live(LIVE)
        (self.home / "codex.mode").write_text("fail-after-receipt")
        code, out = self.run_wake(timeout="1")
        self.assertEqual(code, 2, out)
        self.assertIn("after printing a receipt", out)
        (self.home / "codex.mode").write_text("queue")
        self.take(self.item)                                            # the first run's item, taken meanwhile
        import threading
        threading.Timer(1.2, lambda: (self.home / "queue_10.sqlite").write_bytes(b"not a database")).start()   # the observed store breaks mid-wait
        code, out = self.run_wake(timeout="5")
        self.assertEqual(code, 2, out)
        self.assertIn("then reading that store failed", out)
        self.assertIn(f"Queued message {self.item}", out)                # the receipt is still printed

    def test_after_launch_no_receipt_is_always_unconfirmed_even_with_an_empty_queue(self):
        self.live(LIVE)
        (self.home / "codex.mode").write_text("fail-silent")             # codex printed an error and queued nothing (as far as we know)
        code, out = self.run_wake()
        self.assertEqual(code, 2, out)
        self.assertIn("may be queued", out); self.assertNotIn("not sent", out)
        self.assertEqual(self.pending(), [])
        (self.home / "codex.mode").write_text("accepted-consumed")        # accepted AND consumed before any observation
        code, out = self.run_wake()
        self.assertEqual(code, 2, out)
        self.assertIn("may be queued", out)
        self.assertEqual(self.pending(), [])                                 # an empty queue never turns this into a retryable failure

    def test_an_unknown_liveness_probe_refuses_instead_of_guessing(self):
        self.live(LIVE, code=2)                                        # lsof itself fails
        code, out = self.run_wake()
        self.assertEqual(code, 1, out)
        self.assertIn("could not establish", out)
        self.assertEqual(self.pending(), [])
        self.live(LIVE, code=1, diagnostic=True)                       # exit 1 WITH a diagnostic: not "no holder"
        code, out = self.run_wake()
        self.assertEqual(code, 1, out)
        self.assertIn("could not establish", out); self.assertIn("status error", out)
        self.assertEqual(self.pending(), [])

    def test_accepted_without_a_receipt_is_unconfirmed_and_an_unobserved_item_is_never_delivered(self):
        self.live(LIVE)
        (self.home / "codex.mode").write_text("accepted-no-receipt")    # codex inserted the item, then died silently
        code, out = self.run_wake()
        self.assertEqual(code, 2, out)
        self.assertIn("may be queued", out)
        self.assertEqual(self.pending()[0][0], self.item)
        self.take(self.item)
        (self.home / "codex.mode").write_text("queue")
        self.queue(11)                                                 # a newer, EMPTY store: absence there is not "taken"
        code, out = self.run_wake(timeout="1")
        self.assertEqual(code, 2, out)
        self.assertIn("observed in queue_10.sqlite", out)

    def test_no_live_session_established_queues_for_the_newest_and_several_live_refuse(self):
        code, out = self.run_wake(timeout="1")                         # no lock files at all: established absent by stat
        self.assertEqual(code, 2, out)
        self.assertIn("established for every candidate", out)
        self.assertEqual(self.pending()[0][1], LIVE)                    # the newest by updated_at
        self.take(self.item)
        self.live(LIVE); self.live(OLD)
        code, out = self.run_wake()
        self.assertEqual(code, 1, out)
        self.assertIn("automatic wake needs a target", out)
        self.assertEqual(self.pending(), [])

    def test_dependencies_timeout_schema_and_ids_are_checked_before_submission(self):
        self.live(LIVE)
        code, out = self.run_wake(timeout="0")
        self.assertEqual(code, 1, out); self.assertIn("WAKE_TIMEOUT", out)
        code, out = self.run_wake(timeout="abc")
        self.assertEqual(code, 1, out)
        empty = Path(tempfile.mkdtemp()); (empty / "sqlite3").symlink_to(shutil.which("sqlite3")); (empty / "codex").symlink_to(self.bin / "codex")
        env = dict(os.environ, CODEX_HOME=str(self.home), WAKE_TIMEOUT="1", PATH=str(empty) + os.pathsep + "/usr/bin:/bin")
        out = subprocess.run(["bash", str(SCRIPT), str(self.project)], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 1); self.assertIn("lsof is not on PATH", out.stdout + out.stderr)
        db = sqlite3.connect(self.home / "state_11.sqlite"); db.execute("create table threads (id text)"); db.commit(); db.close()
        code, out = self.run_wake()
        self.assertEqual(code, 1, out); self.assertIn("not the expected schema", out)
        (self.home / "state_11.sqlite").unlink()
        self.state(12, [("not-a-uuid", 300)])
        code, out = self.run_wake()
        self.assertEqual(code, 1, out); self.assertIn("not a UUID", out)
        self.assertEqual(self.pending(), [])

    def test_an_unreadable_lock_directory_is_unknown_not_absent(self):
        locks = self.home / "thread-writer-locks"
        self.live(LIVE)
        os.chmod(locks, 0)
        try:
            code, out = self.run_wake()
        finally:
            os.chmod(locks, 0o755)
        self.assertEqual(code, 1, out)
        self.assertIn("could not establish", out); self.assertIn("stat on", out)
        self.assertEqual(self.pending(), [])

    def test_a_busy_state_store_is_retried_within_the_bound_and_refused_beyond_it(self):
        import threading
        self.live(LIVE)
        state = self.home / "state_9.sqlite"

        def hold(seconds):
            db = sqlite3.connect(state, isolation_level=None)
            db.execute("begin exclusive"); time.sleep(seconds); db.execute("rollback"); db.close()
        t = threading.Thread(target=hold, args=(2,)); t.start()
        time.sleep(0.3)
        code, out = self.run_wake(timeout="1", env_extra={"WAKE_PROBE_SECONDS": "10"})
        t.join()
        self.assertEqual(code, 2, out)                                   # released within the bound: the wake went ahead (queued, unconfirmed)
        self.assertIn(f"Queued message {self.item}", out)
        self.take(self.item)
        t = threading.Thread(target=hold, args=(4,)); t.start()
        time.sleep(0.3)
        code, out = self.run_wake(env_extra={"WAKE_PROBE_SECONDS": "1"})
        t.join()
        self.assertEqual(code, 1, out)
        self.assertIn("stayed busy", out); self.assertNotIn("schema", out)
        self.assertEqual(self.pending(), [])

    def test_a_thread_id_argument_skips_discovery(self):
        code, out = self.run_wake(LIVE, "hello", timeout="1")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.pending()[0][1], LIVE)
