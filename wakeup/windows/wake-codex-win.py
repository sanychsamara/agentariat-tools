#!/usr/bin/env python3
"""Windows replacement for wake-codex.sh, with the same contract.

Wakes a Codex session by queueing a message into its thread via `codex queue`.

  Usage: wake-codex-win.py [THREAD_ID | PROJECT_DIR] [MESSAGE]
  Exit:  0 taken, 1 not sent (no or ambiguous target, or `codex queue` failed),
         2 sent but not confirmed taken within WAKE_TIMEOUT seconds (default 60),
         including when the queue store cannot be read after sending.

Exit 1 always means nothing was queued, so agentariat-watch.py may retry it; once
`codex queue` has succeeded the result is 0 or 2, never 1.

Upstream needs sqlite3(1) and lsof(1); neither exists on Windows. This reads the
state and queue stores with Python's sqlite3 module, and probes the thread writer
lock with an exclusive CreateFileW open instead of lsof.
"""

import ctypes
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
try:
    from ctypes import wintypes                       # Windows; elsewhere the module still loads so its logic can be tested
except (ImportError, ValueError):
    wintypes = None

THREAD_ID = re.compile(r"^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$")
QUEUED = re.compile(r"^Queued message (\S+) for thread ")
CODEX_HOME = os.environ.get("CODEX_HOME", os.path.join(os.path.expanduser("~"), ".codex"))
DEFAULT_MESSAGE = "A peer agent left you a message: check your inbox and reply only if a response or action is needed."


def prerelease_key(text):
    """SemVer precedence for a prerelease: numeric identifiers compare numerically and rank below alphanumeric ones."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in text.split("."))


def version_key(binary):
    """Sortable version of a codex binary: 0.155.1 > 0.155.1-alpha.10 > 0.155.1-alpha.2 > 0.153.0.
    None if it won't report one."""
    try:
        output = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)(?:-(\S+))?", output)
    if not match:
        return None
    numbers = tuple(int(part) for part in match.group(1, 2, 3))
    prerelease = match.group(4)
    return numbers + ((0, prerelease_key(prerelease)) if prerelease else (1, ()))


def codex_binary():
    """CODEX_BIN, else `codex` on PATH, else the highest-versioned copy bundled with the desktop app."""
    override = os.environ.get("CODEX_BIN")
    if override:
        return override
    on_path = shutil.which("codex")
    if on_path:
        return on_path
    bundled = glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin", "*", "codex.exe"))
    ranked = sorted((key, path) for path in bundled for key in [version_key(path)] if key)
    return ranked[-1][1] if ranked else "codex"


def store(prefix):
    """Path of the highest-numbered <prefix>_<n>.sqlite (numeric, so _10 beats _9), or None."""
    numbered = []
    for path in glob.glob(os.path.join(CODEX_HOME, prefix + "_*.sqlite")):
        match = re.search(r"_(\d+)\.sqlite$", path)
        if match:
            numbered.append((int(match.group(1)), path))
    return max(numbered)[1] if numbered else None


BUSY = ("database is locked", "busy", "unable to open", "disk I/O", "locking protocol")
SCHEMA = ("no such table", "no such column", "not a database", "malformed")


class Busy(Exception):
    """Ordinary contention on a store a running Codex holds: retried within a bound, never a schema diagnosis."""


def query(database, sql, parameters=(), wait=5.0):
    """Read-only, never a write into a running Codex's store. Contention (including an open that fails for it) is
    Busy; SQLite waits at most `wait` seconds for it."""
    try:
        connection = sqlite3.connect("file:{}?mode=ro".format(database.replace("?", "%3f")), uri=True, timeout=max(0.0, wait))
    except sqlite3.OperationalError as error:
        if any(word in str(error) for word in BUSY):
            raise Busy(str(error))
        raise
    try:
        return connection.execute(sql, parameters).fetchall()
    except sqlite3.OperationalError as error:
        if any(word in str(error) for word in BUSY):
            raise Busy(str(error))
        raise
    finally:
        connection.close()


def probe_seconds():
    """WAKE_PROBE_SECONDS, validated once before anything is launched: a whole number of seconds, 1..600."""
    raw = os.environ.get("WAKE_PROBE_SECONDS", "10")
    if not raw.isdigit() or not 1 <= int(raw) <= 600:
        sys.exit("wake-codex: WAKE_PROBE_SECONDS must be a whole number of seconds, 1..600; nothing sent")
    return int(raw)


def query_wait(database, sql, parameters=(), seconds=None):
    """Before launch: retries ordinary contention within one monotonic deadline (SQLite's own wait capped to what is
    left of it); a store still busy after it, or a wait that overran it, is refused as unavailable (exit 1, the
    watcher retries normally); a real schema error is reported as such."""
    seconds = probe_seconds() if seconds is None else seconds
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            sys.exit("wake-codex: {} stayed busy for {:.0f} s; unavailable now, retry later; nothing sent".format(os.path.basename(database), seconds))
        try:
            rows = query(database, sql, parameters, wait=min(5.0, remaining))
        except Busy as error:
            if time.monotonic() >= deadline:
                sys.exit("wake-codex: {} stayed busy for {:.0f} s ({}); unavailable now, retry later; nothing sent".format(os.path.basename(database), seconds, error))
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
            continue
        except sqlite3.OperationalError as error:
            if any(word in str(error) for word in SCHEMA):
                sys.exit("wake-codex: {} is not the expected schema ({}); nothing sent".format(os.path.basename(database), error))
            sys.exit("wake-codex: {} could not be read ({}); nothing sent".format(os.path.basename(database), error))
        if time.monotonic() > deadline:                                 # SQLite waited past the bound: not within it
            sys.exit("wake-codex: {} stayed busy past the {:.0f} s bound; unavailable now, retry later; nothing sent".format(os.path.basename(database), seconds))
        return rows


def comparable(path):
    """Strip the extended-length prefix Codex stores (\\\\?\\C:\\... and \\\\?\\UNC\\host\\...), then fold case."""
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


KERNEL32 = None


def kernel32():
    """The Win32 calls the lock probe needs, bound on first use (so the module imports on any platform)."""
    global KERNEL32
    if KERNEL32 is None:
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        lib.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        lib.CreateFileW.restype = wintypes.HANDLE
        lib.CloseHandle.argtypes = [wintypes.HANDLE]
        lib.CloseHandle.restype = wintypes.BOOL
        KERNEL32 = lib
    return KERNEL32


def lock_held(path):
    """lsof's job upstream. True: some process has the lock file open (a sharing violation on an exclusive open);
    False: nobody does, or there is no lock file; None: the probe failed for another reason, so unknown."""
    GENERIC_READ, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 0x80000000, 3, 0x80
    handle = kernel32().CreateFileW(path, GENERIC_READ, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error == ERROR_SHARING_VIOLATION:
            return True
        if error in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
            return False
        return None
    kernel32().CloseHandle(handle)
    return False


def resolve_thread(target):
    if THREAD_ID.match(target):
        return target
    state_db = store("state")
    if not state_db:
        sys.exit("wake-codex: no state_*.sqlite in {}".format(CODEX_HOME))
    directory = os.path.realpath(target)
    wanted = comparable(directory)
    rows = query_wait(state_db, "select id, cwd from threads where source = 'cli' and archived = 0 order by updated_at desc")
    candidates = [thread for thread, cwd in rows if cwd and comparable(cwd) == wanted]
    if not candidates:
        sys.exit("no interactive Codex thread in {}".format(directory))
    probes = {i: lock_held(os.path.join(CODEX_HOME, "thread-writer-locks", i + ".lock")) for i in candidates}
    live = [i for i in candidates if probes[i] is True]
    unknown = [i for i in candidates if probes[i] is None]
    if len(live) == 1 and not unknown:
        return live[0]
    if not live and not unknown:
        print("no live Codex session in {}; queueing for {}".format(directory, candidates[0]), file=sys.stderr)
        return candidates[0]
    print("cannot pick one live Codex session in {}; pass one thread id:".format(directory), file=sys.stderr)
    for i in live + unknown:
        print("  {}{}".format(i, "" if probes[i] else " (lock probe failed)"), file=sys.stderr)
    sys.exit(1)


def queue_stores():
    """Every queue_<n>.sqlite, highest numeric generation first."""
    numbered = []
    for path in glob.glob(os.path.join(CODEX_HOME, "queue_*.sqlite")):
        match = re.search(r"_(\d+)\.sqlite$", path)
        if match:
            numbered.append((int(match.group(1)), path))
    return [path for _, path in sorted(numbered, reverse=True)]


def observe(item, deadline):
    """The store the exact item landed in, or None when no readable store holds it before the deadline."""
    while True:
        for db in queue_stores():
            try:
                if query(db, "select count(*) from queued_items where id = ?", (item,))[0][0]:
                    return db
            except (OSError, sqlite3.Error, Busy):
                continue                                                # busy or unreadable: try again
        if time.time() >= deadline:
            return None
        time.sleep(1)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    message = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MESSAGE
    try:
        timeout = int(os.environ.get("WAKE_TIMEOUT", "60"))
    except ValueError:
        sys.exit("wake-codex: WAKE_TIMEOUT must be a whole number of seconds")
    if not 1 <= timeout <= 3600:
        sys.exit("wake-codex: WAKE_TIMEOUT must be 1..3600 seconds")
    probe_seconds()                                                     # validated before launch, like everything else
    thread = resolve_thread(target)
    binary = codex_binary()
    # ---- launch: from here on, nothing is "not sent" and no exception may turn into exit 1 ----
    try:
        return launched(binary, thread, message, timeout)
    except BaseException as error:                                      # noqa: BLE001  any failure after launch is unconfirmed
        if isinstance(error, SystemExit) and error.code in (0, 2):
            raise
        print("wake-codex: failed after launching codex queue for {} ({}: {}); a message may be queued or taken; not resent".format(
            thread, type(error).__name__, str(error)[:200]), file=sys.stderr)
        return 2


def launched(binary, thread, message, timeout):
    result = subprocess.run([binary, "queue", "--thread", thread, "--message", message], capture_output=True)
    stdout = result.stdout.decode("utf-8", errors="replace")            # never a decoding error after launch
    stderr = result.stderr.decode("utf-8", errors="replace")
    if stdout:
        sys.stdout.write(stdout)                                        # Codex's own receipt, passed through (the watcher parses it)
        sys.stdout.flush()
    item = next((m.group(1) for m in map(QUEUED.match, (stdout + stderr).splitlines()) if m), None)
    if not item:
        print("wake-codex: codex queue exited {} without a parseable receipt; a message may be queued for {} (or already taken); not resent".format(result.returncode, thread), file=sys.stderr)
        return 2
    if result.returncode:
        print("wake-codex: codex queue exited {} after printing a receipt for {}; treating it as submitted".format(result.returncode, item), file=sys.stderr)
    # ---- observation: the exact item in the store it landed in, then gone from that same store ----
    deadline = time.time() + timeout
    active = observe(item, deadline)
    if not active:
        print("queued {} for {}, but it was not observed in any readable queue store within {} s; cannot confirm".format(item, thread, timeout), file=sys.stderr)
        return 2
    while time.time() < deadline:
        try:
            if not query(active, "select count(*) from queued_items where id = ?", (item,))[0][0]:
                print("delivered to {} ({} observed in {}, then taken as a turn)".format(thread, item, os.path.basename(active)))
                return 0
        except Busy:
            pass                                                        # contention: try again within the deadline
        except (OSError, sqlite3.Error) as error:
            print("queued {} for {} (observed in {}), then reading that store failed: {}".format(item, thread, os.path.basename(active), error), file=sys.stderr)
            return 2
        time.sleep(1)
    print("queued {} for {} (observed in {}), not yet taken (session busy or not running)".format(item, thread, os.path.basename(active)), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
