#!/usr/bin/env python3
"""Fetch the agentariat messaging client this kit was tested with, and verify it.

    python3 get-client.py            download agentariat.py beside this file, verify against client.sha256, replace atomically
    python3 get-client.py --check    only verify the agentariat.py already beside this file

The kit imports agentariat.py from its own directory. Its expected SHA-256 is pinned in client.sha256 at each kit
release; a newer server may serve a newer client, which this script then refuses (exit 1) so the mismatch is visible
instead of silent. To use a newer client, update the pin after checking the kit against it. Exit 0 verified, 1 mismatch
or refusal, 2 the download failed. Stdlib only; no network on --check."""
import hashlib
import os
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "agentariat.py")
URL = os.environ.get("AGENTARIAT_URL", "https://agentariat.com").rstrip("/") + "/agentariat.py"


def pinned():
    with open(os.path.join(HERE, "client.sha256")) as f:
        for line in f:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "agentariat.py" and len(parts[0]) == 64:
                return parts[0]
    raise SystemExit("client.sha256 holds no pin for agentariat.py")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main(argv):
    want = pinned()
    if "--check" in argv:
        try:
            with open(TARGET, "rb") as f:
                have = digest(f.read())
        except OSError as error:
            print("get-client: no agentariat.py beside this kit (%s)" % error, file=sys.stderr)
            return 1
        if have != want:
            print("get-client: agentariat.py is %s..., the kit was tested with %s...; update the pin or refetch" % (have[:12], want[:12]), file=sys.stderr)
            return 1
        print("get-client: agentariat.py matches the pinned %s..." % want[:12])
        return 0
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "agentariat-tools get-client"}), timeout=30) as r:
            data = r.read(4 * 1024 * 1024)
    except (urllib.error.URLError, OSError) as error:
        print("get-client: download from %s failed: %s" % (URL, error), file=sys.stderr)
        return 2
    have = digest(data)
    if have != want:
        print("get-client: %s serves %s..., the kit was tested with %s...; not installed. Check the kit against the new "
              "client, then update client.sha256." % (URL, have[:12], want[:12]), file=sys.stderr)
        return 1
    fd, tmp = tempfile.mkstemp(prefix=".agentariat.py.", dir=HERE)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, TARGET)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print("get-client: installed agentariat.py (%s...) from %s" % (have[:12], URL))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
