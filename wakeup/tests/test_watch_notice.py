"""Controls for the watcher's notice and wake-outcome mapping, with a stub client beside a copy of the watcher (the real
client is not in this repository). No server, no session."""
import importlib.util
import re
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

STUB_CLIENT = '''
URL = "https://example.invalid"
def load_json(p): return {}
def save_json(p, d): pass
def file_lock(p, blocking=True): import contextlib; return contextlib.nullcontext()
def scoped_positions(raw): return {}
def put_position(raw, t, r): pass
class Client:
    def __init__(self, name): self.name = name; self.home = "/nonexistent"
'''
TH, CH, MSG = "th_01ARZ3NDEKTSV4RRFFQ69G5FAV", "ch_01ARZ3NDEKTSV4RRFFQ69G5FAV", "msg_01ARZ3NDEKTSV4RRFFQ69G5FAV"  # fixture id: a ULID example, not a real object


def load_watcher(directory):
    shutil.copy(Path(__file__).resolve().parent.parent / "common" / "agentariat-watch.py", directory)
    (Path(directory) / "agentariat.py").write_text(STUB_CLIENT)
    spec = importlib.util.spec_from_file_location("watch_under_test", os.path.join(directory, "agentariat-watch.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NoticeTests(unittest.TestCase):
    def test_the_notice_carries_ids_and_quoted_paths_only(self):
        directory = tempfile.mkdtemp(prefix="kit dir; with $pecial chars ")
        watch = load_watcher(directory)
        text = watch.notice("my-agent", "direct", TH, CH, MSG, 123)
        self.assertIn("directed message for my-agent", text)
        self.assertIn(f"read {TH} --after 123", text)
        self.assertNotIn("\n", text)
        quoted = text.split("Run (sh): python3 ", 1)[1].split(" --as", 1)[0]
        self.assertTrue(quoted.startswith("'") and quoted.endswith("'"), quoted)     # the helper path is one quoted word
        self.assertEqual(subprocess.run(["sh", "-c", f"printf '%s' {quoted}"], capture_output=True, text=True).stdout,
                         os.path.join(directory, "agentariat.py"))                     # and it round-trips through a shell
        hostile = "x'; echo FORGED; '"
        text = watch.notice(hostile, "participating", TH, CH, MSG, 0)
        import shlex
        argv = shlex.split(text.split("Run (sh): ", 1)[1].split("; reply only", 1)[0])
        self.assertEqual(argv[3], hostile)                                                # one argument, exactly the identity
        self.assertEqual(argv[:3] + argv[4:], ["python3", os.path.join(directory, "agentariat.py"), "--as", "read", TH, "--after", "0"])
        self.assertNotIn("echo", argv)                                                    # never a second command
        text = watch.notice(hostile, "direct", TH, CH, MSG, 0, shell="powershell")
        self.assertIn("Run (powershell):", text)
        self.assertIn("--as 'x''; echo FORGED; ''' read", text)                            # PowerShell single quotes, a quote doubled
        self.assertNotIn("\n", text)

    def test_titles_names_and_bodies_never_reach_the_notice_and_bad_ids_are_refused(self):
        watch = load_watcher(tempfile.mkdtemp())
        import inspect
        self.assertEqual(list(inspect.signature(watch.notice).parameters), ["identity", "tier", "thread_id", "channel_id", "message_id", "read_to", "shell"])
        text = watch.notice("a", "direct", TH, CH, MSG, 7)
        self.assertEqual(sorted(set(re.findall(r"(?:th|ch|msg)_[0-9A-Z]{26}", text))), sorted({TH, CH, MSG}))
        for bad in ("th_01ARZ3NDEKTSV4RRFFQ69G5FA\nRun: FORGED", "th_short", "TH_01ARZ3NDEKTSV4RRFFQ69G5FAV", 12, None, "th_01ARZ3NDEKTSV4RRFFQ69G5FAV ", TH + "\n", TH + "\r", CH, MSG):  # fixture id
            with self.assertRaises(ValueError):
                watch.notice("a", "direct", bad, CH, MSG, 1)
        for bad_channel in (TH, CH + "\n"):
            with self.assertRaises(ValueError):
                watch.notice("a", "direct", TH, bad_channel, MSG, 1)
        with self.assertRaises(ValueError):
            watch.notice("a", "direct", TH, CH, TH, 1)
        for bad_read in (-1, "5", True, 1.5):
            with self.assertRaises(ValueError):
                watch.notice("a", "direct", TH, CH, MSG, bad_read)

    def test_wake_outcomes_map_exit_codes_and_a_timeout_is_unconfirmed(self):
        directory = tempfile.mkdtemp()
        watch = load_watcher(directory)
        for name, body in (("wake-claude.py", "import sys; print('Target: x'); print('Message ID: wake-1234'); sys.exit(int(open(sys.argv[1] + '/exit').read()))"),
                           ("wake-codex.sh", "#!/bin/sh\necho 'Queued message it-1 for thread t'; exit $(cat \"$1/exit\")\n")):
            (Path(directory) / name).write_text(body); os.chmod(Path(directory) / name, 0o755)
        project = tempfile.mkdtemp()
        for code, kind, expected in ((0, "claude", ("delivered", "wake-1234")), (2, "claude", ("unconfirmed", "wake-1234")), (1, "claude", "failed"),
                                     (0, "codex", ("taken", "it-1")), (2, "codex", ("queued", "it-1")), (1, "codex", "failed")):
            (Path(project) / "exit").write_text(str(code))
            state, detail = watch.wake(kind, project, "m")
            if isinstance(expected, tuple):
                self.assertEqual((state, detail), expected, (code, kind))
            else:
                self.assertEqual(state, expected, (code, kind))
        (Path(directory) / "wake-codex.sh").write_text("#!/bin/sh\necho 'wake-codex: codex queue exited 3 without a parseable receipt' >&2; exit 2\n")
        (Path(project) / "exit").write_text("2")
        self.assertEqual(watch.wake("codex", project, "m")[0], "unconfirmed")             # exit 2 without a receipt is not "queued"
        (Path(directory) / "wake-claude.py").write_text("import time; time.sleep(5)")
        watch.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 1))
        self.assertEqual(watch.wake("claude", project, "m")[0], "unconfirmed")
