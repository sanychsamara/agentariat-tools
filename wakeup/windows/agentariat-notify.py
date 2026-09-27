#!/usr/bin/env python3
"""Print agentariat-watch.py's wake notices on stdout, for a Claude Code Monitor on Windows.

    python agentariat-notify.py --as FAMILY[/JOB] [--interval 60] [--once]

On Windows, wake-claude.py cannot reach a session: Claude Code listens on a named
pipe whose handshake is undocumented, not the Unix socket the helper writes to. So a
Claude session runs this under its own Monitor, and each stdout line becomes a
notification in that session.

This runs agentariat-watch.py's own cycle for one route, the worker FAMILY[/JOB] with
harness claude-code and the current directory (the session's, under its Monitor) as the project, with only the wake swapped
for a print, so the rules are the watcher's (see its docstring): discovery over the
worker's shared state.json under ~/.agentariat/<family>/workers/<hash>/, pending work
recorded before the scan moves, never a dismissal. Watcher log lines go to stderr, so
only notices reach the Monitor.

It prints only into its own window: the Claude Code process named by CLAUDE_PID, which the
Monitor inherits, with the start time Windows reports (GetProcessTimes; there is no ps),
both captured once at startup; when that process ends or its pid is reused, it stops. Work the dispatcher aims at another window of the worker stays pending for that window's
inbox; give a window its own job to give it its own Monitor. Fixed after the live Windows test
of 2026-09-27; fixture-tested only until a rerun.
Don't also run agentariat-watch.py for the same worker: one route per worker.
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


def capture():
    """The Claude Code window this notifier prints into, fixed once at startup: the process named by CLAUDE_PID, which the
    Monitor's shell inherits from its session, with the start time Windows reports for it. None when either is unknown."""
    text = os.environ.get("CLAUDE_PID", "")
    if not text.isdigit():
        return None
    started = watch.client_module.process_started(int(text))
    return (int(text), started) if started else None


ENDPOINT = capture()        # never re-derived: a reused pid must not make another window this Monitor's host


def host():
    """The captured endpoint while that same process still lives with the same start time; None once it is gone or
    changed (or was never known), so nothing is ever printed for a window this Monitor does not belong to."""
    if ENDPOINT is None:
        return None
    return ENDPOINT if watch.client_module.process_started(ENDPOINT[0]) == ENDPOINT[1] else None


def is_host(binding, endpoint):
    native = (binding or {}).get("native") or {}
    return endpoint is not None and native.get("pid") == endpoint[0] and native.get("started") == endpoint[1]


def host_label(family, job):
    """The session label of this notifier's own window: the binding whose process and start time are the host's."""
    endpoint = host()
    for label, binding in watch.route_client(family, job, "claude", None).bindings().items():
        if is_host(binding, endpoint):
            return label
    return None


dispatch = watch.destinations


def destinations(client, label, route_session, project, exclude=()):
    """The watcher's dispatch rule, limited to what this notifier can reach: it prints only into its own window. A
    destination the rule chose in another window of the worker stays pending with a routing refusal; that
    window reads it from its inbox, or runs its own Monitor under another job. With no binding at all (no window has
    called the helper) the notice goes to this Monitor, the directory's fallback."""
    binding, refusal = dispatch(client, label, route_session, project, exclude=exclude)
    if binding is None or is_host(binding, host()):
        return binding, refusal
    return None, ("session %s is another window of this worker; this Monitor prints only into its own window; kept pending"
                  % binding.get("session"))


def notify(kind, project, message, binding=None):
    if ENDPOINT is not None and host() is None:                               # the host ended or its pid was reused
        return "failed", "this Monitor's window has ended; nothing printed"
    if binding is not None and not is_host(binding, host()):                  # rechecked just before printing
        return "failed", "the selected window is not this Monitor's; nothing printed"
    print(message, flush=True)
    return "delivered", ""


def log(text):
    print(text, file=sys.stderr, flush=True)


watch.wake = notify
watch.destinations = destinations
watch.log = log

# Piped stdout on Windows is the ANSI code page (cp1252 here), which cannot encode an emoji in a thread title; the
# error would abort the whole cycle on every poll. Emit UTF-8, and escape whatever still cannot be encoded.
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as", dest="identity", required=True, help="the family key; FAMILY/JOB names a job (default job: default)")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds to pause after a completed cycle (at least 5; default 60)")
    parser.add_argument("--once", action="store_true", help="run one cycle, then exit")
    parser.add_argument("--project", default=os.getcwd(), help="the session's working directory, which its binding records "
                        "(default: the current directory, where the Claude Code Monitor runs)")
    args = parser.parse_args()
    mark = watch.hold_running_mark(HERE)                                   # noqa: F841  an installer must not swap the bundle under this process
    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds")
    family, _, job = args.identity.partition("/")
    while True:
        if ENDPOINT is not None and host() is None:
            log("agentariat-notify: the window this Monitor belongs to (pid %d) has ended; stopping" % ENDPOINT[0])
            return 1
        # cycle() keeps one bad poll from ending the loop; the route is this worker's, bound to this notifier's own window
        # once that window has called the helper (its label is looked up again every cycle)
        job = job or watch.client_module.DEFAULT_JOB
        watch.cycle([(family, job, "claude", args.project, host_label(family, job))])
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
