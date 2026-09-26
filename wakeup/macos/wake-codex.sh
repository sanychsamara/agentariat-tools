#!/usr/bin/env bash
# Wake a Codex CLI session by queueing a message into its thread, instead of
# having it poll. Uses `codex queue`, Codex's durable per-thread inbox. No PTY
# injection. Part of the agentariat wake-up kit:
# https://github.com/sanychsamara/agentariat-tools (wakeup/macos/wake-codex.md).
#
# Usage: wake-codex.sh [THREAD_ID | PROJECT_DIR] [MESSAGE]
#   PROJECT_DIR (default: cwd) picks the live interactive Codex session
#   started in that directory: the one whose thread lock a running process
#   holds. Several live sessions there: pass a THREAD_ID. None live, and that
#   established for every candidate: queues for the most recent one, delivered
#   when it resumes. A probe that cannot tell is a refusal, never a guess.
# Exit: 0 delivered (the exact queued item was observed in Codex's queue store
#   and then observed gone from that same store: taken as a turn); 1 not sent,
#   only for what is established BEFORE `codex queue` is launched (no or
#   ambiguous target, a dependency, store or probe problem, a store still busy
#   after WAKE_PROBE_SECONDS, default 10); 2 launched but not confirmed (still
#   queued after WAKE_TIMEOUT seconds, default 60; no or unparseable receipt;
#   any failure after launch). Once the queue process has started, nothing
#   here claims "not sent": a message may be accepted and even consumed
#   without a receipt. Receipts are printed.
set -uo pipefail                                   # no -e: every status is handled where it happens

home="${CODEX_HOME:-$HOME/.codex}"
target="${1:-$PWD}"
message="${2:-A peer agent left you a message: check your inbox and reply only if a response or action is needed.}"
timeout="${WAKE_TIMEOUT:-60}"
probe_seconds="${WAKE_PROBE_SECONDS:-10}"
uuid='^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$'
refuse() { echo "wake-codex: $1" >&2; exit 1; }
unconfirmed() { echo "wake-codex: $1" >&2; exit 2; }
SQL_OUT="$(mktemp "${TMPDIR:-/tmp}/wake-codex.XXXXXX")" || refuse "cannot create a temporary file; nothing sent"
trap 'rm -f "$SQL_OUT"' EXIT
SQL_ERR=""
sql() {  # sql DB QUERY: read-only; stdout printed, stderr kept in SQL_ERR (never a write into a running Codex's store)
  SQL_ERR="$(sqlite3 -readonly "$1" "$2" 2>&1 >"$SQL_OUT")"; local rc=$?
  cat "$SQL_OUT"; return $rc
}
busy() { [[ "$SQL_ERR" == *"database is locked"* || "$SQL_ERR" == *"busy"* || "$SQL_ERR" == *"unable to open"* || "$SQL_ERR" == *"disk I/O"* || "$SQL_ERR" == *"locking protocol"* ]]; }
schema_error() { [[ "$SQL_ERR" == *"no such table"* || "$SQL_ERR" == *"no such column"* || "$SQL_ERR" == *"not a database"* || "$SQL_ERR" == *"malformed"* ]]; }
sql_wait() {  # sql_wait DB QUERY: retries ordinary contention within the pre-submission deadline; 0 ok, 1 error (SQL_ERR), 3 still busy
  local deadline=$(( $(date +%s) + probe_seconds )) rc
  while :; do
    sql "$1" "$2"; rc=$?
    (( rc == 0 )) && return 0
    busy || return 1
    (( $(date +%s) < deadline )) || return 3
    sleep 0.5
  done
}

# ---- everything that can be wrong is found before anything is launched ----
for dep in codex sqlite3 lsof stat; do command -v "$dep" >/dev/null 2>&1 || refuse "$dep is not on PATH; nothing sent"; done
[[ "$timeout" =~ ^[0-9]+$ ]] && (( timeout >= 1 && timeout <= 3600 )) || refuse "WAKE_TIMEOUT must be 1..3600 seconds; nothing sent"
[[ "$probe_seconds" =~ ^[0-9]+$ ]] && (( probe_seconds >= 1 && probe_seconds <= 600 )) || refuse "WAKE_PROBE_SECONDS must be 1..600; nothing sent"
stores() {  # stores state|queue -> every <prefix>_<n>.sqlite, highest numeric generation first
  local f n
  for f in "$home"/"$1"_*.sqlite; do
    [[ -f "$f" ]] || continue
    n="${f##*/"$1"_}"; n="${n%.sqlite}"
    [[ "$n" =~ ^[0-9]+$ ]] && printf '%s %s\n' "$n" "$f"
  done | sort -rn | cut -d' ' -f2-
}
state_db="$(stores state | head -1)"; [[ -n "$state_db" ]] || refuse "no Codex state store under $home; nothing sent"
sql_wait "$state_db" "select id, cwd, source, archived, updated_at from threads limit 0;" >/dev/null; rc=$?
case $rc in
  0) ;;
  3) refuse "the state store ${state_db##*/} stayed busy for $probe_seconds s ($SQL_ERR); unavailable now, retry later; nothing sent" ;;
  *) if schema_error; then refuse "the state store's threads table is not the expected schema ($SQL_ERR); nothing sent"
     else refuse "the state store could not be read ($SQL_ERR); nothing sent"; fi ;;
esac

if [[ "$target" =~ $uuid ]]; then
  thread="$target"
else
  dir="$(cd "$target" 2>/dev/null && pwd -P)" || refuse "$target is not a directory; nothing sent"
  candidates="$(sql_wait "$state_db" "select id from threads
    where cwd = '${dir//\'/\'\'}' and source = 'cli' and archived = 0
    order by updated_at desc;")" || refuse "reading the state store failed or stayed busy ($SQL_ERR); nothing sent"
  [[ -n "$candidates" ]] || refuse "no interactive Codex thread in $dir; nothing sent"
  live=(); unknown=()
  for id in $candidates; do
    [[ "$id" =~ $uuid ]] || refuse "the state store holds a thread id that is not a UUID ($id); nothing sent"
    lock="$home/thread-writer-locks/$id.lock"
    # Existence is established by stat: "No such file or directory" is an absent lock (nobody holds it); any other
    # failure (an unreadable directory, a permission error) is unknown, never "absent".
    stat_err="$(stat "$lock" 2>&1 >/dev/null)"; stat_rc=$?
    if (( stat_rc != 0 )); then
      if [[ "$stat_err" == *"No such file or directory"* ]]; then continue; fi
      unknown+=("$id"); echo "wake-codex: stat on $lock: $stat_err" >&2; continue
    fi
    # A running session holds the writer lock. lsof -t: exit 0 with a pid = held; exit 1 with NOTHING on stderr = a
    # completed, empty probe = not held; exit 1 with a diagnostic (a file that vanished, a permission error, a status
    # error) or any other exit = unknown, never "not live".
    probe_err="$(lsof -t "$lock" 2>&1 >/dev/null)"; probe=$?
    if (( probe == 0 )); then live+=("$id")
    elif (( probe == 1 )) && [[ -z "$probe_err" ]]; then :
    else unknown+=("$id"); [[ -n "$probe_err" ]] && echo "wake-codex: lsof on $lock: $probe_err" >&2
    fi
  done
  (( ${#unknown[@]} == 0 )) || refuse "the liveness probe could not establish whether ${unknown[*]} is live; nothing sent"
  case ${#live[@]} in
    1) thread="${live[0]}" ;;
    0) thread="$(head -1 <<<"$candidates")"
       echo "wake-codex: no live Codex session in $dir (established for every candidate); queueing for $thread" >&2 ;;
    *) { echo "wake-codex: connected; automatic wake needs a target: ${#live[@]} live Codex sessions in $dir; pass one thread id, or read the inbox by hand; nothing was sent:"; printf '  %s\n' "${live[@]}"; } >&2; exit 1 ;;
  esac
fi

# ---- launch: from here on, nothing is "not sent" ----
receipt="$(codex queue --thread "$thread" --message "$message" 2>&1)"; queued=$?
item="$(sed -n 's/^Queued message \([A-Za-z0-9_.:-]\{1,128\}\) for thread .*/\1/p' <<<"$receipt" | head -1)"
[[ -n "$receipt" ]] && printf '%s\n' "$receipt"          # Codex's own receipt (or its error), passed through unchanged
[[ -n "$item" ]] || unconfirmed "codex queue exited $queued without a parseable receipt; a message may be queued for $thread (or already taken); not resent"
(( queued == 0 )) || echo "wake-codex: codex queue exited $queued after printing a receipt for $item; treating it as submitted" >&2

# ---- observation: the exact item, in the store it actually landed in, then gone from that same store ----
deadline=$(( $(date +%s) + timeout ))
active=""
while [[ -z "$active" ]] && (( $(date +%s) < deadline )); do
  while read -r db; do
    [[ -n "$db" ]] || continue
    n="$(sql "$db" "select count(*) from queued_items where id = '${item//\'/\'\'}';")" || continue      # busy or unreadable: try again
    [[ "$n" == 0 ]] || { active="$db"; break; }
  done < <(stores queue)
  [[ -n "$active" ]] || sleep 1
done
[[ -n "$active" ]] || unconfirmed "queued $item for $thread, but it was not observed in any readable queue store under $home within $timeout s; cannot confirm"
while (( $(date +%s) < deadline )); do
  if pending="$(sql "$active" "select count(*) from queued_items where id = '${item//\'/\'\'}';")"; then
    [[ "$pending" == 0 ]] && { echo "delivered to $thread ($item observed in ${active##*/}, then taken as a turn)"; exit 0; }
  elif ! busy; then unconfirmed "queued $item for $thread (observed in ${active##*/}), then reading that store failed ($SQL_ERR)"
  fi
  sleep 1
done
unconfirmed "queued $item for $thread (observed in ${active##*/}), not yet taken (session busy or not running)"
