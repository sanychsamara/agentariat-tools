"""Controls for check.py, the publication check, against throwaway repositories. No network."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CHECK = Path(__file__).resolve().parents[2] / "check.py"
KEY = "-----BEGIN " + "PRIVATE KEY-----"     # a dummy marker, assembled so this file is not itself a finding
PERSON = {"GIT_AUTHOR_NAME": "sanychsamara", "GIT_AUTHOR_EMAIL": "1+sanychsamara@users.noreply.github.com",
          "GIT_COMMITTER_NAME": "sanychsamara", "GIT_COMMITTER_EMAIL": "1+sanychsamara@users.noreply.github.com"}


@unittest.skipUnless(shutil.which("git"), "git is needed")
class CheckTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.git("init", "-q", "-b", "main")
        shutil.copy(CHECK, self.repo / "check.py")
        (self.repo / "wakeup" / "tests").mkdir(parents=True)
        (self.repo / "wakeup" / "tests" / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        (self.repo / "README.md").write_text("clean\n")
        self.commit("clean")

    def git(self, *args, env=None):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, env={**os.environ, **PERSON, **(env or {})})

    def commit(self, message, env=None):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, env=env)

    def check(self):
        out = subprocess.run([sys.executable, str(CHECK), "--no-tests", "--repo", str(self.repo)], capture_output=True, text=True)
        return out.returncode, out.stdout + out.stderr

    def test_a_clean_repository_is_clean(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_a_staged_secret_hidden_by_a_clean_working_copy_is_found(self):
        (self.repo / "note.txt").write_text(KEY + "\n")
        self.git("add", "note.txt")
        (self.repo / "note.txt").write_text("harmless\n")
        code, out = self.check()
        self.assertEqual(code, 1, out); self.assertIn("index note.txt", out)

    def test_a_secret_removed_from_head_but_kept_in_history_is_found(self):
        (self.repo / "old.txt").write_text(KEY + "\n"); self.commit("add")
        (self.repo / "old.txt").unlink(); self.commit("remove")
        code, out = self.check()
        self.assertEqual(code, 1, out); self.assertIn("history old.txt", out)

    def test_binary_blobs_commit_messages_and_tag_messages_are_findings(self):
        (self.repo / "blob.bin").write_bytes(b"\0" + KEY.encode())
        self.commit("binary")
        code, out = self.check()
        self.assertEqual(code, 1, out); self.assertIn("binary blob", out)
        (self.repo / "blob.bin").unlink(); self.commit("m " + KEY)
        code, out = self.check()
        self.assertRegex(out, r"commit [0-9a-f]+ message:1: private key")
        self.git("tag", "-a", "v1", "-m", "tag " + KEY)
        code, out = self.check()
        self.assertIn("tag refs/tags/v1 message:1: private key", out)

    def test_the_self_exemption_is_only_the_pattern_list_at_the_root_check_py(self):
        (self.repo / "othercheck.py").write_text('x = re.compile("' + KEY + '")\n'); self.git("add", "othercheck.py")
        code, out = self.check()
        self.assertEqual(code, 1, out); self.assertIn("othercheck.py", out)
        (self.repo / "othercheck.py").unlink(); self.git("rm", "-q", "--cached", "othercheck.py")
        (self.repo / "wakeup" / "check.py").write_text('PATTERNS = [\n    ("k", re.compile("' + KEY + '")),\n]\n'); self.git("add", "wakeup/check.py")
        code, out = self.check()
        self.assertEqual(code, 1, out); self.assertIn("wakeup/check.py", out)

    def test_people_must_be_the_public_handle_with_a_noreply_address(self):
        (self.repo / "a.txt").write_text("a\n")
        self.commit("named", env={"GIT_AUTHOR_NAME": "Fixture Person", "GIT_AUTHOR_EMAIL": "person@example.com"})
        code, out = self.check()
        self.assertEqual(code, 1, out); self.assertIn("author name 'Fixture Person'", out); self.assertIn("not a GitHub no-reply", out)

    def test_a_marked_fixture_id_in_a_test_is_allowed_and_elsewhere_is_not(self):
        example = "th_01ARZ3NDEKTSV4RRFFQ69G5FAV"  # fixture id
        (self.repo / "wakeup" / "tests" / "test_y.py").write_text('X = "' + example + '"  # fixture id\n'); self.commit("fixture")
        self.assertEqual(self.check()[0], 0)
        (self.repo / "README.md").write_text(example + "  # fixture id\n"); self.commit("readme")
        code, out = self.check()
        self.assertEqual(code, 1, out); self.assertIn("agentariat id", out)

    def test_a_git_failure_is_a_scan_failure_not_clean(self):
        shutil.rmtree(self.repo / ".git")
        code, out = self.check()
        self.assertEqual(code, 2, out); self.assertIn("SCAN FAILED", out)
