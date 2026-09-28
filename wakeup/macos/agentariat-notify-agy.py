#!/usr/bin/env python3
"""Wake an Antigravity CLI (agy) session when a message arrives for its worker, through agy's own message API.

    python3 agentariat-notify-agy.py --as FAMILY[/JOB] [--interval 60] [--log PATH] [--once | --foreground]

Start it from inside the agy session as a plain command, with no nohup and no &:

    python3 ~/.agentariat/tools/agentariat-notify-agy.py --as KEY

While that command runs, the notifier finds the agy session above it (the shell agy started is still alive then), then
moves itself to the background, detached from that shell, and returns once it is running, printing its process id. It
writes to ~/.agentariat/tools/notify-agy.log (--log). --foreground keeps it attached; --once runs one cycle.

agy gives the processes it starts what `agy agentapi send-message` needs (ANTIGRAVITY_LS_ADDRESS, ANTIGRAVITY_CSRF_TOKEN)
and the session's own conversation id (ANTIGRAVITY_CONVERSATION_ID). This notifier uses them for one thing: sending
each notice to that same conversation, which starts a turn in an idle session (seen on macOS with agy 1.2.12,
2026-09-28). It never types into a console, never reads another process's environment, and never chooses another
conversation. It reads no token value itself; the agy command does.

It runs agentariat-watch.py's own cycle for one route, the worker FAMILY[/JOB] with harness antigravity and the current
directory as the project, with only the wake swapped for the send, so the rules are the watcher's (see its docstring).
It delivers only to its own session: the agy process above it and its conversation, both captured once at startup,
and the binding the helper records when this session calls it. When that agy process ends or its pid is reused, it
stops. A send that was launched but not confirmed is reported `unconfirmed` and never resent blindly; the work stays
pending in the inbox. Don't also run another notifier for the same worker: one route per worker.
"""

import argparse
import importlib.util
import json
import os
import select
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("agentariat_watch", os.path.join(HERE, "agentariat-watch.py"))
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

CONVERSATION = os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or None
SEND_TIMEOUT = 60


def capture():
    """The agy session this notifier belongs to, fixed once at startup: the nearest agy process above it, with its start
    time. None when there is none (not started from inside an agy session, or on Windows, where the walk is not
    implemented)."""
    pid = watch.client_module.ancestor("agy")
    started = watch.client_module.process_started(pid) if pid else None
    return (pid, started) if started else None


ENDPOINT = capture()        # never re-derived: a reused pid must not make another session this notifier's


def host():
    """The captured session while that same process still lives with the same start time; None once it is gone or
    changed, or was never known."""
    if ENDPOINT is None:
        return None
    return ENDPOINT if watch.client_module.process_started(ENDPOINT[0]) == ENDPOINT[1] else None


def is_host(binding, endpoint):
    native = (binding or {}).get("native") or {}
    return (endpoint is not None and native.get("pid") == endpoint[0] and native.get("started") == endpoint[1]
            and native.get("conversation") == CONVERSATION)


def host_label(family, job):
    """The session label of this notifier's own session: the binding whose process, start time and conversation are the
    host's."""
    endpoint = host()
    for label, binding in watch.route_client(family, job, "agy", None).bindings().items():
        if is_host(binding, endpoint):
            return label
    return None


dispatch = watch.destinations


def destinations(client, label, route_session, project, exclude=()):
    """The watcher's dispatch rule, limited to what this notifier can reach: its own session. A destination the rule
    chose in another session of the worker stays pending with a routing refusal. With no binding at all, the notice goes
    to this session, the directory's fallback."""
    binding, refusal = dispatch(client, label, route_session, project, exclude=exclude)
    if binding is None or is_host(binding, host()):
        return binding, refusal
    return None, ("session %s is another agy session of this worker; this notifier reaches only its own; kept pending"
                  % binding.get("session"))


def agy():
    return os.environ.get("AGENTARIAT_AGY") or shutil.which("agy")


def accepted(output, message):
    """agy's reply names this conversation and the exact text; anything else is not a confirmation."""
    try:
        sent = json.loads(output)["response"]["sendMessage"]
    except (ValueError, KeyError, TypeError):
        return False
    return isinstance(sent, dict) and sent.get("recipientId") == CONVERSATION and sent.get("content") == message


def send(kind, project, message, binding=None, popen=subprocess.Popen):
    if host() is None:                                                        # the session ended, or its pid was reused
        return "failed", "this notifier's agy session has ended; nothing sent"
    if binding is not None and not is_host(binding, host()):                  # rechecked just before sending
        return "failed", "the selected session is not this notifier's; nothing sent"
    command = agy()
    if not command:
        return "failed", "agy was not found on PATH (set AGENTARIAT_AGY); nothing sent"
    try:
        process = popen([command, "agentapi", "send-message", CONVERSATION, message], stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as error:                                                  # never started, so nothing was sent
        return "failed", "agy could not be started (%s); nothing sent" % type(error).__name__
    except BaseException as error:                                            # may have started
        return "unconfirmed", "starting agy did not finish (%s); not resent" % type(error).__name__
    try:
        stdout, _ = process.communicate(timeout=SEND_TIMEOUT)
    except BaseException as error:                                            # launched: it may have been accepted
        try:
            process.kill()
            process.communicate(timeout=10)                  # reaped and its pipes closed; the outcome stays unknown
        except BaseException:
            pass
        return "unconfirmed", "agy agentapi send-message did not finish (%s); not resent" % type(error).__name__
    if process.returncode == 0 and accepted(stdout, message):
        return "delivered", ""
    return "unconfirmed", "agy did not confirm the message (exit %s); not resent" % process.returncode


def log(text):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), text, file=sys.stderr, flush=True)


watch.wake = send
watch.destinations = destinations
watch.log = log


def detach(args, log_path, ready_timeout=60):
    """Continue in the background as a fresh interpreter (never a fork of this one: on macOS a forked child must not go
    on to use the networking runtime), in its own session so the ending shell's hangup never reaches it, with its output
    in log_path. The endpoint this process captured is handed over on the command line and the new process reports
    readiness on a pipe; this one returns only then. Returns the exit status for the starter."""
    read, write = os.pipe()
    out = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    command = [sys.executable, os.path.abspath(__file__), "--as", args.identity, "--interval", str(args.interval),
               "--project", args.project, "--foreground", "--endpoint", "%d:%s" % ENDPOINT, "--ready-fd", str(write)]
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=out, pass_fds=(write,),
                                 start_new_session=True, close_fds=True)
    finally:
        os.close(write)
        os.close(out)
    answer = b""
    deadline = time.time() + ready_timeout
    while time.time() < deadline:
        ready, _, _ = select.select([read], [], [], max(0.0, deadline - time.time()))
        if not ready:
            break
        chunk = os.read(read, 64)
        if not chunk:
            break
        answer += chunk
    os.close(read)
    if answer.startswith(b"ready"):
        print("agentariat-notify-agy: running as pid %d for agy pid %d; log %s" % (child.pid, ENDPOINT[0], log_path))
        return 0
    if child.poll() is None:                                # not ready in time: it must not start later unannounced
        child.kill()
        child.wait()
    print("agentariat-notify-agy: did not start; see %s" % log_path, file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as", dest="identity", required=True, help="the family key; FAMILY/JOB names a job (default job: default)")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds to pause after a completed cycle (at least 5; default 60)")
    parser.add_argument("--once", action="store_true", help="run one cycle in the foreground, then exit")
    parser.add_argument("--foreground", action="store_true", help="stay attached to the starting shell instead of detaching")
    parser.add_argument("--log", default=os.path.join(HERE, "notify-agy.log"), help="where the detached notifier writes "
                        "(default: notify-agy.log beside it)")
    parser.add_argument("--project", default=os.getcwd(), help="the session's working directory, which its binding records "
                        "(default: the current directory)")
    parser.add_argument("--endpoint", help=argparse.SUPPRESS)          # from the starter: the agy session it found
    parser.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)  # from the starter: where to report readiness
    args = parser.parse_args()
    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds")
    missing = [name for name in ("ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN")
               if not os.environ.get(name)]
    if missing:
        parser.error("start this from inside the agy session: %s not set" % ", ".join(missing))
    global ENDPOINT
    if args.endpoint:                                        # started by detach(): the endpoint the starter captured
        pid, _, started = args.endpoint.partition(":")
        if not pid.isdigit() or watch.client_module.process_started(int(pid)) != started:
            parser.error("the agy session the starter found (%s) is gone" % args.endpoint)
        ENDPOINT = (int(pid), started)
    if ENDPOINT is None:
        parser.error("no agy process found above this one; start it from inside the agy session as a plain command, "
                     "without nohup or & (macOS and Linux)")
    if not (args.once or args.foreground):
        return detach(args, os.path.abspath(os.path.expanduser(args.log)))
    return run(args, args.ready_fd)


def run(args, ready):
    mark = watch.hold_running_mark(HERE)                                   # noqa: F841  an installer must not swap the bundle under this process
    family, _, job = args.identity.partition("/")
    job = job or watch.client_module.DEFAULT_JOB
    if ready is not None:
        if host() is None:
            log("agentariat-notify-agy: the agy session (pid %d) ended before the notifier started" % ENDPOINT[0])
            return 1
        log("agentariat-notify-agy: running for agy pid %d, worker %s job %s" % (ENDPOINT[0], family, job))
        os.write(ready, b"ready")
        os.close(ready)
    while True:
        if host() is None:
            log("agentariat-notify-agy: the agy session this notifier belongs to (pid %d) has ended; stopping" % ENDPOINT[0])
            return 1
        try:
            label = host_label(family, job)
        except (Exception, SystemExit) as error:             # like a failed cycle: logged, and tried again next time
            log("agentariat-notify-agy: cycle failed: %s" % (error if isinstance(error, SystemExit) else repr(error)))
        else:
            watch.cycle([(family, job, "agy", args.project, label)])
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
