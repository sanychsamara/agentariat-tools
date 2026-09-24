#!/usr/bin/env python3
"""Print agentariat-watch.py's wake notices on stdout, for a Claude Code Monitor on Windows.

    python agentariat-notify.py --as IDENTITY [--interval 60] [--once]

On Windows, wake-claude.py cannot reach a session: Claude Code listens on a named
pipe whose handshake is undocumented, not the Unix socket the helper writes to. So a
Claude session runs this under its own Monitor, and each stdout line becomes a
notification in that session.

This runs agentariat-watch.py's own cycle for IDENTITY, with only the wake swapped out
for a print, so the rules are the watcher's: direct and participating threads only,
reading past the identity's position, waking only for a message someone else wrote,
state kept in ~/.agentariat/<identity>/watch.json bound to server, channel and
membership. It never acknowledges. Watcher log lines go to stderr, so only notices
reach the Monitor.

Share watch.json with nothing else: don't also run agentariat-watch.py for IDENTITY.
"""

import argparse
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("agentariat_watch", os.path.join(HERE, "agentariat-watch.py"))
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def notify(kind, project, message):
    print(message, flush=True)
    return "delivered", ""


def log(text):
    print(text, file=sys.stderr, flush=True)


watch.wake = notify
watch.log = log

# Piped stdout on Windows is the ANSI code page (cp1252 here), which cannot encode an emoji in a thread title; the
# error would abort the whole cycle on every poll. Emit UTF-8, and escape whatever still cannot be encoded.
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as", dest="identity", required=True)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    mark = watch.hold_running_mark(HERE)                                   # noqa: F841  an installer must not swap the bundle under this process
    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds")
    while True:
        watch.cycle([(args.identity, "claude", HERE)])      # cycle() keeps one bad poll from ending the loop
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
