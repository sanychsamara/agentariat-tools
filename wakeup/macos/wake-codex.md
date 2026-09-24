# wake-codex.sh

Wakes a Codex CLI session by queueing a message into its thread with `codex queue`, Codex's durable per-thread inbox.
No terminal injection. Bash, `sqlite3`, `lsof`, and a Codex CLI that supports `codex queue`. On Windows use
[`../windows/wake-codex-win.py`](../windows/README.md), which has the same contract.

```
wake-codex.sh [THREAD_ID | PROJECT_DIR] [MESSAGE]
```

Exit **0**: delivered: the exact queued item was observed in the queue store it landed in, and then observed gone
from that same store, which is the session taking it as a turn.
Exit **1**: not sent, only for what is established **before `codex queue` is launched**: no or ambiguous target, a
missing dependency, a bad `WAKE_TIMEOUT` or `WAKE_PROBE_SECONDS`, a state store that is unreadable, of another
schema, or still busy after `WAKE_PROBE_SECONDS` (1..600, default 10; ordinary contention with a running Codex is
retried within that bound, then refused as unavailable so the watcher retries normally), a liveness probe that could
not tell.
Exit **2**: launched, not confirmed: still queued after `WAKE_TIMEOUT` seconds (1..3600, default 60) because the
session is busy or not running, the message waiting durably; no or unparseable receipt (a message may have been
accepted, and even consumed, without one: an empty queue is not proof of anything); a receipt whose item is not
observed in any readable store within the timeout; a store that cannot be read after launch. Once the queue process
has started, this adapter never says "not sent". A receipt is always printed when there is one.

## How it selects the target

- A `THREAD_ID` (a UUID) is used as given.
- Otherwise `PROJECT_DIR` (default: the current directory) selects the interactive session started there:
  candidate threads come from Codex's newest state store (`~/.codex/state_<n>.sqlite` by numeric generation, opened
  read-only, its `threads` table checked for the expected columns): `cwd` equal to the resolved directory,
  `source = 'cli'` so one-shot `exec` jobs are skipped, not archived, newest first; every id must be a UUID. A
  running session holds its thread's writer lock, `~/.codex/thread-writer-locks/<thread>.lock`. Presence is
  established by `stat`: "No such file or directory" is an absent lock (not held); any other stat failure (an
  unreadable directory, a permission error) is unknown. On a present lock, `lsof -t` says held (exit 0 with a pid);
  not held only when the probe completed with exit 1 and nothing on stderr; anything else (a diagnostic on stderr,
  such as a file that vanished between stat and probe, or another exit code) is unknown, and its stderr is passed
  through.
  - one live thread: that one;
  - none live, established for every candidate: the newest candidate, and the message waits for it (exit 2 after
    the timeout);
  - several live: refused with the list; pass a `THREAD_ID`;
  - any unknown: refused. A probe that cannot tell never becomes a guess.
- `CODEX_HOME` overrides `~/.codex`.

## What it sends and how it confirms

`codex queue --thread <id> --message "<MESSAGE>"`. The default message says a peer left a message and to check the
inbox; the watcher supplies a precise one. Codex's receipt, `Queued message <item> for thread <thread>`, is passed
through unchanged and the item id is taken from it. The adapter then looks for that exact item in every readable
`~/.codex/queue_<n>.sqlite` (read-only, newest generation first) and remembers the store it found it in; delivery is
confirmed when the item leaves that same store. Absence from some other store, newer or older, never counts; other
items pending for the same thread neither block nor fake confirmation. Taken means the session started a turn with
it, not that it finished or replied.
Measured on an idle session: about 10 seconds from queue to turn.

## Tests

`tests/test_wake_codex.py` runs the real script against fixture stores with a stubbed `codex` and `lsof` (bash and
sqlite3 needed; never a real Codex): the exact item observed in its store and confirmed from that store (an older
store with a stale row and a newer empty store change nothing), still-queued as exit 2 with the receipt, failures
after launch as exit 2, no receipt as exit 2 whether codex queued nothing, queued without printing, or queued and
consumed at once, a probe failure and an exit-1-with-diagnostic probe both refused, an unreadable lock directory
refused as unknown, a missing lock file established as absent, a state store busy for two seconds retried and one
busy past the bound refused as unavailable (never as a schema error), offline queueing only when established,
several live sessions refused, dependencies, timeout, schema and ids checked before launch.

## Known limits

- A brand-new interactive session that has not received its first prompt holds a writer lock but has no thread row
  yet, so `codex queue` fails with "no rollout found" (exit 1); send the session any first prompt.
- A message queued for a session that never resumes stays in the queue.
