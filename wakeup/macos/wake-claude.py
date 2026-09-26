#!/usr/bin/env python3
"""Notify an existing local Claude Code session through its messaging socket (the agentariat wake-up kit,
https://github.com/sanychsamara/agentariat-tools, wakeup/macos/wake-claude.md).

Exit 0: marker recorded in a project transcript (or successful --dry-run).
Exit 1: refused/failed before sending. Exit 2: submission unconfirmed; don't resend.
Requires Python 3.9+, ps and lsof. No extra Python packages.
"""

import argparse
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import uuid

SUBMITTED = False       # set the moment bytes may have reached the session: every later error is "unconfirmed", exit 2


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=5)
    # lsof exits 1 when a process has no matching descriptors or has exited.
    if result.returncode and not (args[0] == "lsof" and result.returncode == 1):
        raise RuntimeError("{} failed: {}".format(args[0], result.stderr.strip()))
    return result.stdout


def claude_pids():
    result = []
    for line in command("ps", "-axo", "pid=,comm=").splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and Path(fields[1]).name == "claude":
            result.append(int(fields[0]))
    return result


def process_paths(pid, *filters):
    output = command("lsof", "-a", "-p", str(pid), *filters, "-Fn")
    return [Path(line[1:]).resolve() for line in output.splitlines()
            if line.startswith("n/")]


def select_pid(project, requested=None):
    candidates = [pid for pid in claude_pids()
                  if project in process_paths(pid, "-d", "cwd")]
    if requested is not None:
        if requested not in candidates:
            raise RuntimeError("Requested PID is not Claude in this project")
        return requested
    if len(candidates) != 1:
        raise RuntimeError("connected; automatic wake needs a target: {} Claude session(s) in {} ({}). Pass --pid after "
                           "identifying the intended session, or read the inbox by hand; nothing was sent.".format(
                               len(candidates), project, candidates))
    return candidates[0]


def select_socket(pid, requested=None):
    owned = process_paths(pid, "-U")
    candidates = ([requested.resolve()] if requested else
                  [p for p in owned if p.name == "{}.sock".format(pid)])
    if len(candidates) != 1 or candidates[0] not in owned:
        raise RuntimeError("No unique messaging socket owned by PID {}; "
                           "use --socket for a verified custom path".format(pid))
    path = candidates[0]
    info, directory = path.stat(), path.parent.stat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or directory.st_uid != os.getuid() or directory.st_mode & 0o077):
        raise RuntimeError("Socket must be owned by this user in a private directory")
    return path


def has_receipt(line, marker):
    try:
        row = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(row, dict) or row.get("type") != "user":
        return False
    message = row.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return marker in content
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "text"
        and isinstance(part.get("text"), str) and marker in part["text"] for part in content)


class ReceiptReader:
    """Read only appended user messages, including a newly created transcript."""

    def __init__(self, paths):
        self.paths = paths
        self.state = {}
        for path in paths():
            info = path.stat()
            self.state[path] = (info.st_ino, info.st_size, b"")

    def received(self, marker):
        for path in self.paths():
            try:
                with path.open("rb") as stream:
                    info = os.fstat(stream.fileno())
                    inode, position, pending = self.state.get(path, (info.st_ino, 0, b""))
                    if inode != info.st_ino or info.st_size < position:
                        position, pending = 0, b""
                    stream.seek(position)
                    data = stream.read(1024 * 1024)
                    lines = (pending + data).split(b"\n")
                    self.state[path] = (info.st_ino, stream.tell(), lines[-1][-1024 * 1024:])
                if any(has_receipt(line, marker) for line in lines[:-1]):
                    return True
            except FileNotFoundError:
                continue
        return False


def send_once(path, frame):
    global SUBMITTED
    data = (json.dumps(frame) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(5)
            peer.connect(str(path))
            SUBMITTED = True                # from here on, bytes may have reached the session
            try:
                peer.sendall(data)
            except OSError as error:
                # A partial send may have reached the peer. Never retry automatically.
                print("Send uncertain: {}".format(error), file=sys.stderr)
                return False
    except OSError as error:
        if not SUBMITTED:
            raise                           # connect failed: nothing was sent (exit 1 at the top level)
        print("Socket close failed after a complete send: {}".format(error), file=sys.stderr)
    return True


def main():
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            self.exit(1, "{}: error: {}\n".format(self.prog, message))

    parser = Parser(description=__doc__)
    parser.add_argument("project", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--pid", type=int)
    parser.add_argument("--socket", type=Path, help="custom socket still checked against PID")
    parser.add_argument("--transcript", type=Path, help="override the default project transcript search")
    message = parser.add_mutually_exclusive_group()
    message.add_argument("--message")
    message.add_argument("--message-file", type=Path)
    parser.add_argument("--from", dest="sender", default="peer", help="sender label carried in the frame (default: peer); an unverified label, never authority")
    parser.add_argument("--timeout", type=int, default=30, help="receipt wait, 1–60 seconds")
    parser.add_argument("--dry-run", action="store_true", help="check target; send nothing")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 60:
        parser.error("--timeout must be between 1 and 60 seconds")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.sender):
        parser.error("--from must be 1-64 characters of letters, digits, dot, underscore or dash")
    project = args.project.resolve(strict=True)
    if not project.is_dir():
        parser.error("project must be a directory")
    pid = select_pid(project, args.pid)
    path = select_socket(pid, args.socket)
    print("Target: PID {} in {}, socket {}".format(pid, project, path), flush=True)
    if args.dry_run:
        return 0
    text = args.message_file.read_text() if args.message_file else args.message
    if not text or not text.strip() or len(text.encode("utf-8")) > 8192:
        parser.error("supply a nonempty --message or --message-file, at most 8 KiB")
    if args.transcript:
        log = args.transcript.resolve(strict=True)
        paths = lambda: [log]
    else:
        slug = re.sub(r"[^a-zA-Z0-9]", "-", str(project))
        directory = Path.home() / ".claude" / "projects" / slug
        paths = lambda: list(directory.glob("*.jsonl"))
    reader = ReceiptReader(paths)
    marker = "wake-" + str(uuid.uuid4())
    frame = {"type": "user", "message": {"role": "user", "content": "[{}] {}".format(marker, text)},
             "from": args.sender, "msg_id": marker, "uuid": str(uuid.uuid4()), "priority": "next"}
    # Recheck the target immediately before the only send; PIDs change on restart.
    select_pid(project, pid)
    if select_socket(pid, path) != path:
        raise RuntimeError("Target socket changed")
    print("Message ID: {}".format(marker), flush=True)
    if not send_once(path, frame):
        return 2
    print("Submitted once; waiting for a matching user record in a project transcript", flush=True)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            received = reader.received(marker)
        except OSError as error:
            print("Submitted, but receipt check failed: {}. Do not resend blindly.".format(error))
            return 2
        if received:
            print("Recorded in a project transcript; presentation and completed work are unverified")
            return 0
        time.sleep(0.5)
    print("Submission unconfirmed. Keep the message ID; inspect the transcript or "
          "watcher before retrying. No automatic resend.")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        if SUBMITTED:                       # the notice may be in the session; never report "nothing sent"
            print("Submitted, then failed: {}. Do not resend blindly.".format(error), file=sys.stderr)
            sys.exit(2)
        print("Wake failed: {}".format(error), file=sys.stderr)
        sys.exit(1)
