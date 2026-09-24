#!/usr/bin/env python3
"""The publication check: run before every commit and before every push. Stdlib Python 3.9+ and git.

    python3 check.py [--no-tests] [--repo DIR]

It runs the tests, then scans what git would publish and what it already holds: every blob in the INDEX and every
blob reachable from ANY ref (by the path it was committed under), every commit's message, author and committer, and
every annotated tag's message and tagger. It scans blob contents, so a staged secret with a clean working copy is
found, and history, so a secret removed from HEAD is found. A binary blob is a finding in this source-only kit (it
cannot be read for markers). It fails CLOSED: any git or read error is exit 2, distinct from findings (1) and clean
(0). The patterns are things that must never be in this public repository: private keys and tokens, e-mail
addresses other than GitHub or Anthropic no-reply ones, private network addresses, machine-specific home paths,
agentariat identities and ids (except test lines marked `# fixture id`), and coding-session URLs; commit and tag
names must be the approved public handle. The only self-exemption is this file's own pattern list, at this exact
path. It is a limited check plus human review, not a guarantee of finding every secret.
"""
import re
import subprocess
import sys

NOREPLY = re.compile(r"^[^@\s]+@users\.noreply\.github\.com$")
ALLOWED_NAMES = ("sanychsamara",)                    # the public GitHub handle; the only approved attribution name
SELF = "check.py"                                    # the one path whose pattern list is exempt
PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("token", re.compile(r"\b(tskey-|sk-ant-|ghp_[A-Za-z0-9]{20,}|github_pat_|xox[abp]-|AKIA[0-9A-Z]{16})")),
    ("e-mail address", re.compile(r"\b(?!noreply@anthropic\.com\b)[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com\b|example\.(com|invalid|org)\b)[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    ("private network address", re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b")),
    ("tailnet host", re.compile(r"\b[a-z0-9-]+\.[a-z0-9-]+\.ts\.net\b")),
    ("home path", re.compile(r"(/Users/[A-Za-z0-9_.-]+/|/home/[A-Za-z0-9_.-]+/|[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+\\)")),
    ("agentariat id", re.compile(r"\b(ag_[A-Za-z0-9_-]{20,}|(ch|th|msg|inv|att)_[0-7][0-9A-HJKMNP-TV-Z]{25})\b")),
    ("coding-session URL", re.compile(r"claude\.ai/code/session_|Claude-Session:")),
]
ALLOWED_PLACEHOLDERS = ("/Users/<you>/", "/home/<you>/", "C:\\Users\\<you>\\")


class Git:
    def __init__(self, repo):
        self.repo = repo

    def __call__(self, *args):
        p = subprocess.run(["git", "-C", self.repo, *args], capture_output=True)
        if p.returncode != 0:
            raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.decode(errors="replace").strip()))
        return p.stdout


def scan_text(text, where, findings, self_file=False):
    in_patterns = False
    for n, line in enumerate(text.splitlines(), 1):
        if self_file:
            if line.startswith("PATTERNS = ["):
                in_patterns = True
                continue
            if in_patterns:
                if line.startswith("]"):
                    in_patterns = False
                continue                                                    # the pattern list itself, at SELF only
            if line.startswith("ALLOWED_PLACEHOLDERS"):
                continue
        probe = line
        for placeholder in ALLOWED_PLACEHOLDERS:
            probe = probe.replace(placeholder, "")
        for label, pattern in PATTERNS:
            if label == "agentariat id" and "# fixture id" in line and "/tests/" in where:
                continue                                                    # a marked example id in a test fixture
            if pattern.search(probe):
                findings.append("%s:%d: %s: %s" % (where, n, label, line.strip()[:120]))


def scan_blob(name, data, where, findings):
    if b"\0" in data[:8000]:
        findings.append("%s %s: binary blob (unreadable for markers; this kit is source only)" % (where, name))
        return
    scan_text(data.decode("utf-8", errors="replace"), "%s %s" % (where, name), findings, self_file=(name == SELF))


def check_person(kind, name, address, where, findings):
    if name not in ALLOWED_NAMES:
        findings.append("%s: %s name %r is not the approved public handle" % (where, kind, name))
    if not NOREPLY.match(address):
        findings.append("%s: %s address %s is not a GitHub no-reply address" % (where, kind, address))


def main(argv):
    repo = argv[argv.index("--repo") + 1] if "--repo" in argv else "."
    git = Git(repo)
    if "--no-tests" not in argv:
        t = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "wakeup/tests", "-p", "test_*.py"], cwd=repo)
        if t.returncode != 0:
            print("check: the tests failed", file=sys.stderr)
            return 2
    findings = []
    try:
        for line in git("ls-files", "-s", "-z").decode().split("\0"):          # the index: what the next commit publishes
            if not line:
                continue
            meta, name = line.split("\t", 1)
            scan_blob(name, git("cat-file", "blob", meta.split()[1]), "index", findings)
        seen = set()
        for line in git("rev-list", "--objects", "--all").decode().splitlines():   # every blob reachable from any ref
            parts = line.split(" ", 1)
            if len(parts) != 2 or parts[0] in seen:
                continue
            sha, name = parts
            if git("cat-file", "-t", sha).decode().strip() != "blob":
                continue
            seen.add(sha)
            scan_blob(name, git("cat-file", "blob", sha), "history", findings)
        log = git("log", "--all", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e").decode(errors="replace")
        for record in log.split("\x1e"):                                     # every commit's people and message
            if not record.strip():
                continue
            sha, an, ae, cn, ce, body = record.strip("\n").split("\0", 5)
            where = "history commit %s" % sha[:10]
            check_person("author", an, ae, where, findings)
            check_person("committer", cn, ce, where, findings)
            scan_text(body, where + " message", findings)
        for ref in git("for-each-ref", "--format=%(objectname) %(objecttype) %(refname)").decode().splitlines():
            sha, kind, refname = ref.split(" ", 2)
            if kind != "tag":
                continue
            raw = git("cat-file", "-p", sha).decode(errors="replace")           # an annotated tag: tagger and message
            header, _, body = raw.partition("\n\n")
            tagger = re.search(r"^tagger (.*?) <([^>]*)>", header, re.M)
            where = "tag %s" % refname
            if tagger:
                check_person("tagger", tagger.group(1), tagger.group(2), where, findings)
            else:
                findings.append("%s: no tagger line" % where)
            scan_text(body, where + " message", findings)
    except (RuntimeError, OSError, ValueError, UnicodeDecodeError) as error:
        print("check: SCAN FAILED (nothing established): %s" % error, file=sys.stderr)
        return 2
    if findings:
        print("\n".join(findings), file=sys.stderr)
        print("check: %d finding(s); not publishable" % len(findings), file=sys.stderr)
        return 1
    print("check: clean (index, every reachable blob, every commit and tag); a limited check, not a guarantee")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
