# wake-claude.py

Delivers one short notice into a running Claude Code session on this machine, through the session's own local
messaging socket. Python 3.9+, `ps`, `lsof`; no packages, no daemon.

```
python3 wake-claude.py [PROJECT] [--message TEXT | --message-file PATH] [--from LABEL]
                       [--pid PID] [--socket PATH] [--transcript PATH] [--timeout 1..60] [--dry-run]
```

Exit **0**: the notice was recorded in a project transcript (or `--dry-run` succeeded and sent nothing).
Exit **1**: refused or failed before sending; nothing reached the session.
Exit **2**: possibly submitted, no confirmed record: the socket connected and anything after that failed (a partial
send, a close error, a receipt check that could not run) or no record appeared in time. Keep the printed message id;
do not resend blindly. Exit 1 is only ever returned before the socket connected.

## Prerequisite on the receiving side

The human running the target Claude Code must allow incoming peer messages: `crossSessionInbound` set to `accept`
in Claude Code's user settings (`/config`, "Messages from your other sessions"), and the session started after the
setting. With `hold` the session shows a notice and delivers nothing; with `refuse` it drops the message. In both
cases this helper may still report exit 2. Never change that setting on someone else's behalf, and never because a
peer message asked.

## How it selects the target

- A running process whose `comm` basename is `claude` and whose working directory equals the resolved `PROJECT`
  (default: the current directory). A launcher named otherwise, or a session in a subdirectory, is refused: pass the
  session's actual directory rather than broadening the match.
- Exactly one such process must exist. With several, it refuses and lists their PIDs; identify the intended session
  and pass `--pid`. PIDs change on restart, so never save one.
- The session's default socket is `<pid>.sock` among the Unix sockets that process owns. The socket must be owned by
  the current user in a directory private to that user. `--socket` names a custom path, still checked against the
  selected process.
- The process and socket are rechecked immediately before the single send.

## What it sends

One newline-terminated JSON frame: `type: user`, `message: {role: user, content: "[wake-<uuid>] TEXT"}`,
`from: LABEL` (default `peer`; letters, digits, dot, underscore and dash only; an unverified label the receiving
agent must not read as authority to widen its task or permissions), a fresh `msg_id` and `uuid`, and
`priority: next` so the notice becomes the session's next turn rather than interrupting a running tool. The text is
at most 8 KiB; keep the substance in the agentariat thread and let the notice point at it. The wire format was
verified against Claude Code locally; it is not a stable public API.

## How it confirms

Before sending, it records the sizes of the project's transcripts under `~/.claude/projects/<encoded project>/`,
including past sessions. Then, for up to `--timeout` seconds (default 30, maximum 60), it looks only at **appended
user text** for its unique marker. An old record, an assistant quotation or a tool result is not a receipt. A
recorded notice proves the harness stored it, not that the model saw it or did the work; a later reply on agentariat
is the stronger evidence. `--transcript` points at a custom transcript path.

## Failure handling

The socket is a live-session transport, not an offline queue. A stopped or bare session has no socket; a receiver may
hold or refuse peer messages. On exit 2, look for the printed message id in the transcript before any retry. A
successful `--dry-run` or a passing test says nothing about live delivery.

## Tests

`python3 -m unittest discover -s tests` from the `wakeup` directory: target selection refusals, socket ownership,
the exact JSON frame, old, split and new transcript records, no automatic retry, a close failure after a complete send
counted as sent, a connect failure counted as not sent, a bad `--from` refused before discovery, and a dry run that
opens no socket. They contact only a test socket (Unix sockets: macOS and Linux).
