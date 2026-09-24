"""Controls for wake-claude.py. Local fixtures only: these tests never contact a running Claude session.
Run from the repository root: python3 -m unittest discover -s wakeup/tests"""

import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("wake_claude", Path(__file__).resolve().parent.parent / "macos" / "wake-claude.py")
wake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wake)


def receipt(marker, kind="user", content=None):
    return json.dumps({"type": kind, "message": {
        "role": kind, "content": marker if content is None else content}}).encode() + b"\n"


@unittest.skipUnless(hasattr(os, "getuid") and sys.platform != "win32", "the Claude adapter uses Unix sockets and ownership checks: macOS and Linux")
class WakeTests(unittest.TestCase):
    def test_project_selection_refuses_ambiguous_or_wrong_pid(self):
        project = Path("/project")
        with patch.object(wake, "claude_pids", return_value=[10, 20, 30]), patch.object(
                wake, "process_paths", side_effect=lambda pid, *args: [project if pid != 30 else Path("/other")]):
            with self.assertRaises(RuntimeError):
                wake.select_pid(project)
            with self.assertRaises(RuntimeError):
                wake.select_pid(project, 30)
            self.assertEqual(wake.select_pid(project, 20), 20)

    def test_no_live_session_is_not_resumed(self):
        with patch.object(wake, "claude_pids", return_value=[]):
            with self.assertRaises(RuntimeError):
                wake.select_pid(Path("/project"))

    def test_only_user_text_confirms_receipt(self):
        marker = "unique-wake-marker"
        self.assertTrue(wake.has_receipt(receipt(marker), marker))
        self.assertTrue(wake.has_receipt(receipt(marker, content=[{"type": "text", "text": marker}]), marker))
        self.assertFalse(wake.has_receipt(receipt(marker, kind="assistant"), marker))
        self.assertFalse(wake.has_receipt(receipt(marker, content=[{"type": "tool_result", "text": marker}]), marker))
        self.assertFalse(wake.has_receipt(receipt(marker, content=[{"type": "text", "text": None}]), marker))
        for line in [b"not json", b"[]", receipt("different")]:
            self.assertFalse(wake.has_receipt(line, marker))

    def test_receipt_ignores_history_and_handles_split_and_new_files(self):
        marker = "unique-wake-marker"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing.jsonl"
            existing.write_bytes(receipt(marker))
            reader = wake.ReceiptReader(lambda: list(root.glob("*.jsonl")))
            self.assertFalse(reader.received(marker))
            line = receipt(marker)
            with existing.open("ab") as stream:
                stream.write(line[:20])
            self.assertFalse(reader.received(marker))
            with existing.open("ab") as stream:
                stream.write(line[20:])
            self.assertTrue(reader.received(marker))
            reader = wake.ReceiptReader(lambda: list(root.glob("*.jsonl")))
            (root / "new.jsonl").write_bytes(receipt(marker))
            self.assertTrue(reader.received(marker))

    def test_truncated_transcript_is_reopened(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.jsonl"
            path.write_bytes(receipt("old" * 100))
            reader = wake.ReceiptReader(lambda: [path])
            path.write_bytes(receipt("new-marker"))
            self.assertTrue(reader.received("new-marker"))

    def test_socket_ownership_and_actual_json_frame(self):
        # Short real path also fits macOS's Unix socket path limit.
        base = "/private/tmp" if Path("/private/tmp").is_dir() else "/tmp"
        with tempfile.TemporaryDirectory(prefix="wake-test-", dir=base) as directory:
            path = Path(directory).resolve() / "123.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen(1)
                server.settimeout(2)
                with patch.object(wake, "process_paths", return_value=[path]):
                    self.assertEqual(wake.select_socket(123), path)
                with patch.object(wake, "process_paths", return_value=[]):
                    with self.assertRaises(RuntimeError):
                        wake.select_socket(123, path)
                Path(directory).chmod(0o755)
                with patch.object(wake, "process_paths", return_value=[path]):
                    with self.assertRaises(RuntimeError):
                        wake.select_socket(123)
                Path(directory).chmod(0o700)
                frame = {"type": "user", "message": {"role": "user", "content": "fixture only"}}
                self.assertTrue(wake.send_once(path, frame))
                peer, _ = server.accept()
                with peer:
                    data = peer.recv(8192)
                self.assertEqual(json.loads(data), frame)
                self.assertEqual(data.count(b"\n"), 1)

    def test_uncertain_send_is_never_retried(self):
        with patch.object(wake.socket, "socket") as factory:
            peer = factory.return_value.__enter__.return_value
            peer.sendall.side_effect = TimeoutError("uncertain")
            self.assertFalse(wake.send_once(Path("/fixture.sock"), {"type": "user"}))
            peer.connect.assert_called_once()
            peer.sendall.assert_called_once()

    def test_dry_run_never_opens_a_socket(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                wake, "select_pid", return_value=123), patch.object(
                wake, "select_socket", return_value=Path("/fixture.sock")), patch.object(
                wake, "send_once") as send, patch.object(
                wake.sys, "argv", ["wake-claude.py", directory, "--dry-run"]):
            self.assertEqual(wake.main(), 0)
            send.assert_not_called()

    def test_complete_notice_gets_a_correlated_transcript_receipt(self):
        base = "/private/tmp" if Path("/private/tmp").is_dir() else "/tmp"
        with tempfile.TemporaryDirectory(prefix="wake-test-", dir=base) as directory:
            root = Path(directory).resolve()
            path, log = root / "fixture.sock", root / "session.jsonl"
            log.write_bytes(receipt("older message"))
            notice = root / "notice.txt"
            notice.write_text("Work is finished.\nPlease review the two files.")
            frames = []
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen(1)
                server.settimeout(2)

                def receive():
                    peer, _ = server.accept()
                    with peer:
                        data = b""
                        while not data.endswith(b"\n"):
                            chunk = peer.recv(8192)
                            if not chunk:
                                break
                            data += chunk
                    frame = json.loads(data)
                    frames.append(frame)
                    with log.open("ab") as stream:
                        stream.write(json.dumps(frame).encode() + b"\n")

                worker = threading.Thread(target=receive, daemon=True)
                worker.start()
                args = ["wake-claude.py", str(root), "--message-file", str(notice),
                        "--transcript", str(log), "--timeout", "2"]
                with patch.object(wake, "select_pid", return_value=123), patch.object(
                        wake, "select_socket", return_value=path), patch.object(wake.sys, "argv", args):
                    result = wake.main()
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result, 0)
                self.assertEqual(len(frames), 1)
                self.assertIn(notice.read_text(), frames[0]["message"]["content"])
                self.assertIn(frames[0]["msg_id"], frames[0]["message"]["content"])
                self.assertEqual(frames[0]["priority"], "next")

    def test_a_close_failure_after_a_complete_send_is_still_a_send_and_a_connect_failure_is_not(self):
        with patch.object(wake.socket, "socket") as factory:
            factory.return_value.__exit__.side_effect = OSError("close failed")
            wake.SUBMITTED = False
            self.assertTrue(wake.send_once(Path("/fixture.sock"), {"type": "user"}))
            self.assertTrue(wake.SUBMITTED)                                     # a later error must become exit 2, not 1
        with patch.object(wake.socket, "socket") as factory:
            factory.return_value.__enter__.return_value.connect.side_effect = OSError("refused")
            wake.SUBMITTED = False
            with self.assertRaises(OSError):
                wake.send_once(Path("/fixture.sock"), {"type": "user"})
            self.assertFalse(wake.SUBMITTED)                                    # nothing sent: exit 1 is right
        wake.SUBMITTED = False

    def test_a_bad_from_label_is_refused_before_any_discovery(self):
        script = Path(__file__).resolve().parent.parent / "macos" / "wake-claude.py"
        for label in ("bad label", "x" * 65, "", "a;b"):
            out = subprocess.run([sys.executable, str(script), tempfile.gettempdir(), "--from", label, "--dry-run"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 1, label)
            self.assertIn("--from", out.stderr)
            self.assertNotIn("Target:", out.stdout)


if __name__ == "__main__":
    unittest.main()
