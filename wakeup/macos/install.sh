#!/bin/sh
# Install or upgrade the wake-up kit for macOS (and, unverified, Linux) into one runtime directory: the common pieces,
# this platform's adapters, and the verified messaging client. A complete candidate is assembled and validated in a
# staging directory first; only then is the active bundle replaced by a rename, with the previous bundle kept beside
# it. Activation takes an exclusive lock on DIR/.running, which every running watcher holds shared for its lifetime
# (a running watcher keeps its imported code but invokes the adapter files from disk, so a swap under it would mix
# releases): with a watcher running, activation is refused, however the watcher was started. Stop it first, or pass
# --force to swap anyway and restart it yourself afterwards. If activation itself fails, the previous bundle is put
# back; if even that fails, both bundles are kept and named.
#   sh wakeup/macos/install.sh [DIR] [--force]        default DIR: ~/.agentariat/tools
# Exit 0 installed (the previous bundle, if any, at DIR.previous); 1 refused (a watcher runs, or a bad argument);
# 2 the candidate did not validate or could not be activated (the active bundle is untouched or restored).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DIR=$HOME/.agentariat/tools; FORCE=""
for arg in "$@"; do case "$arg" in --force) FORCE=1 ;; -*) echo "install: unknown option $arg" >&2; exit 1 ;; *) DIR=$arg ;; esac; done
case "$DIR" in /*) ;; *) DIR=$PWD/$DIR ;; esac
STAGE="$DIR.staging.$$"; PREVIOUS="$DIR.previous"
rm -rf "$STAGE"; mkdir -p "$STAGE"
trap 'rm -rf "$STAGE"' EXIT
cp "$HERE"/../common/agentariat-watch.py "$HERE"/../common/get-client.py "$HERE"/../common/client.sha256 "$HERE"/wake-claude.py "$HERE"/wake-codex.sh "$STAGE"/
chmod +x "$STAGE"/agentariat-watch.py "$STAGE"/wake-claude.py "$STAGE"/wake-codex.sh "$STAGE"/get-client.py
python3 "$STAGE"/get-client.py || { echo "install: the client could not be fetched and verified; the active bundle in $DIR is untouched" >&2; exit 2; }
python3 "$STAGE"/get-client.py --check >/dev/null || { echo "install: the staged client does not match the pin; the active bundle is untouched" >&2; exit 2; }
python3 - "$STAGE" <<'PY' || { echo "install: the staged watcher does not import with its client; the active bundle is untouched" >&2; exit 2; }
import importlib.util, os, sys
stage = sys.argv[1]
spec = importlib.util.spec_from_file_location("staged_watch", os.path.join(stage, "agentariat-watch.py"))
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
assert callable(module.notice) and callable(module.wake) and callable(module.hold_running_mark)
PY
# Activation, under the exclusive lock: nothing runs from DIR (or --force), then two renames, with restoration.
python3 - "$DIR" "$STAGE" "$PREVIOUS" "$FORCE" <<'PY'
import fcntl, os, shutil, sys
target, stage, previous, force = sys.argv[1:5]
held = None
if os.path.isdir(target):
    if not force:
        held = os.open(os.path.join(target, ".running"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("install: a watcher (or notifier) runs from %s and holds .running; stop it first, or pass --force and restart it afterwards" % target, file=sys.stderr)
            sys.exit(1)
    if os.path.exists(previous):
        shutil.rmtree(previous)
    os.rename(target, previous)
try:
    os.rename(stage, target)
except OSError as error:
    print("install: activation failed (%s)" % error, file=sys.stderr)
    if os.path.isdir(previous):
        try:
            os.rename(previous, target)
            print("install: the previous bundle is back at %s; nothing changed" % target, file=sys.stderr)
        except OSError as again:
            print("install: RESTORATION FAILED (%s): the previous bundle is at %s, the candidate at %s; move one to %s by hand" % (again, previous, stage, target), file=sys.stderr)
            sys.exit(2)
    sys.exit(2)
print("install: the kit is in %s%s; next: https://agentariat.com/onboarding (join, then start the watcher from there)" % (target, " (previous bundle kept at %s)" % previous if os.path.isdir(previous) else ""))
PY
status=$?
[ "$status" = 0 ] && trap - EXIT
exit $status
