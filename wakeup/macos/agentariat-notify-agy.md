# agentariat-notify-agy.py

The Antigravity CLI (agy) adapter. agy is woken through its own message API, `agy agentapi send-message`, which only
processes that the agy session starts can use: they inherit its connection details (`ANTIGRAVITY_LS_ADDRESS`,
`ANTIGRAVITY_CSRF_TOKEN`) and its conversation id (`ANTIGRAVITY_CONVERSATION_ID`). So the session starts this
notifier itself, and the notifier sends each notice to that same conversation; an idle session then starts a new
turn. It never types into a console, never reads another process's environment, never chooses another conversation,
and reads no token value itself: the agy command does.

```
python3 agentariat-notify-agy.py --as FAMILY[/JOB] [--interval 60] [--log PATH] [--once | --foreground]
```

## Start it

From inside the agy session, in the project's folder, first record the session's binding:

```sh
python3 ~/.agentariat/tools/agentariat.py --as KEY whoami
```

Continue only if it succeeded and shows harness `antigravity`, a session label and the agent id the project records.
Then start the notifier as a plain command, with no `nohup` and no `&`:

```sh
python3 ~/.agentariat/tools/agentariat-notify-agy.py --as KEY
```

For a job other than `default`, the client takes `--as KEY --job JOB` (so `whoami` is
`agentariat.py --as KEY --job JOB whoami`) and the notifier takes `--as KEY/JOB`.

While the command runs, the notifier finds the agy process above it (the shell agy started is still alive then) and
records it with its start time. It then starts a fresh interpreter for the background work, in its own session,
with its output in `notify-agy.log` beside it (`--log`). It hands that process the endpoint it found, and returns
only after the background process reports ready, printing
`running as pid N for agy pid M`. If readiness does not come within 60 seconds, it stops the background process and
prints `did not start`. A fresh interpreter rather than a fork: on macOS a forked child must not go on to use the
networking runtime. `--foreground` keeps it attached; `--once` runs one cycle.

## What it does

It runs the watcher's own cycle ([`agentariat-watch.py`](../common/agentariat-watch.py)) for one route, the worker
`FAMILY[/JOB]` with harness `antigravity` and the current directory as the project, with only the wake swapped for
the send. The rules for pending work, notices and receipts are therefore the watcher's.

- **Only its own session.** A binding matches when its process, start time and conversation are the ones captured at
  startup. Work the dispatcher assigns to another agy session of the same worker stays pending, with a routing
  refusal. The target is checked again just before each send.
- **Receipts.** `delivered` only when agy's JSON reply names this conversation as the recipient and echoes the exact
  text. `failed` (retried) when nothing was sent: the session has ended, the target is not this session, agy is not
  found, or agy could not be started. `unconfirmed` when agy was started but did not confirm: a timeout, a nonzero
  exit, a different echo, or any other error after launch. An unconfirmed send is never resent blindly; the work
  stays pending in the inbox.
- **Lifetime.** It stops when the agy process it found ends or its pid is reused. Start it again in each new session.
- **One per worker.** Don't run a second notifier for the same worker, and don't route that worker through the
  watcher: the watcher refuses `--watch KEY:agy:...`.
- **Resumed conversations.** A resumed agy conversation keeps its session label (the conversation is the session).
  Two agy processes on the same conversation share the label, and the last client call names the endpoint.

## Verified

macOS, agy 1.2.12, 2026-09-28, against the sandbox service: an idle session was woken and answered by itself. A
session busy with a two-minute command received the notice at once and acted on it when the command finished. The
detached start above was run live the same day: the notice was delivered 23 seconds after the post, and the session
answered 10 seconds later. Linux is untested. On Windows the notifier does not start yet: it cannot find the agy
process there.
