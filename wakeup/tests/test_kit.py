"""Controls on the kit as a whole: the Windows adapter's default notice, the pinned client, the platform notes."""
import hashlib
import os
import re
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent


class KitTests(unittest.TestCase):
    def test_default_notices_are_generic(self):
        for name in ("macos/wake-codex.sh", "windows/wake-codex-win.py"):
            text = (KIT / name).read_text()
            self.assertIn("check your inbox", text, name)
            self.assertNotIn("comments folder", text, name)

    def test_the_client_pin_is_a_digest_and_a_present_client_matches_it(self):
        pin = (KIT / "common" / "client.sha256").read_text().split()
        self.assertEqual(pin[1], "agentariat.py"); self.assertRegex(pin[0], r"^[0-9a-f]{64}$")
        client = KIT / "common" / "agentariat.py"
        if not client.exists():
            self.skipTest("no agentariat.py beside the kit (fetched with get-client.py)")
        self.assertEqual(hashlib.sha256(client.read_bytes()).hexdigest(), pin[0], "run get-client.py, or update the pin after checking the kit")

    def test_no_helper_names_a_real_identity_or_deployment(self):
        for path in KIT.rglob("*"):
            if path.is_file() and path.suffix in (".py", ".sh", ".md", ".ps1") and path.name != "agentariat.py" and "tests" not in path.parts:   # check.py covers tests, with marked fixtures
                text = path.read_text(errors="replace")
                self.assertNotRegex(text, r"\b(ag_[A-Za-z0-9_-]{20,}|(ch|th|msg|inv)_[0-7][0-9A-HJKMNP-TV-Z]{25})\b", path)
