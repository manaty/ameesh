# Conversation log (L78)

An agent has one shared conversation across its sessions. The detail is
stored in `conversation_log` (migration 0368), with the agent, session,
turn, per-agent sequence, kind, qualified author, text and timestamp.
This does not grant an author's messages any approval authority; L77
verifies human identity and input priority separately.

The kinds are `human_message` (message humain), `input` (entrée: mail,
event or instruction), `text` (texte), `tool` (outil), `turn_end`
(fin de tour) and `state` (état: sleep, wake-up, restart or rotation).
Text is capped at 20,000 characters per entry, with `truncated` set when
clipped. Metadata allows only tool name, status, cost, duration, token
counts and dropped-event count. Tool arguments, results, raw protocol
objects and reasoning are excluded. ACP tool titles are excluded too; only
a bounded identifier or a known ACP tool kind is retained. An adapter's
arbitrary `display` is never stored: text is extracted from explicitly typed
assistant-message/chunk/final fields. Plain `TextStream` output stays in the
ordinary disk trace, without being copied into shared conversation detail.

`conversation_heads` serializes writers for an agent through commit.
Sequences therefore follow commit order; a subscriber cannot skip a
late commit with an earlier cursor. A stable `event_id` deduplicates
replayed inputs or final answers while the entry is retained.

The `ameesh_chat` notification contains only `agent` and `seq`; it is
published at commit, never with message text. Consumers re-read using
`conversation.history(db, agent, after=last_seq, limit=100)`. The
`ameesh-conversation/1` result gives ascending `entries`, `last_seq`,
`oldest_seq`, `next_seq`, `more` and `gap`. `gap` explicitly reports a
missing sequences before or within the returned page, and a missing tail
when `more` is false. This includes holes behind a pending R12 entry that
survives detail retention. Continue pagination with `next_seq` while `more`
is true. Limits are 1–500 entries.
LISTEN consumers use `storage.of(db).wakeups.subscribe(['ameesh_chat'])`;
clients without LISTEN poll the same history API. Notifications are hints,
so every reconnect must read from its last cursor even without a signal.

`progress --json` includes `agents[].conversation`: the latest 20 entries
in ascending order, their schema and the durable `last_seq`. Agent cards
follow the existing project filter. The raw R12 outbox is never exported.
The base supervisor role reads only conversation state; text and metadata
require the explicit supervisor content role.

## Producer API

L77 can record a verified input using the synchronous API:

```python
from ameesh.conversation import Log

log = Log(db, cfg, agent=agent, project=project, lot=lot)
log.append('human_message', text, author='human:alice',
           event_id=input_id, session=session, turn=turn)
log.drain_threads()
```

Identity verification is the caller's responsibility. Ordinary `input`
entries can use the mail/event identifier as `event_id`; they do not
duplicate mail already recorded in the readable thread.

L76 attaches a nonblocking sink to L75's reader callback:

```python
from ameesh.conversation import Recorder

recorder = Recorder(cfg, agent=agent, turn=turn.id, session=session,
                    project=project, lot=lot, reader=adapter.reader)
driver = resident.driver_for(adapter, cwd=cwd, on_event=recorder.on_event)
# After the logical turn completes (including its steered inputs):
recorder.finish(final=result.final, status=result.status, cost=result.cost,
                usage=result.usage, duration_s=result.duration_s)
recorder.close()
```

The sink validates the configured reader object against the built-in Claude,
Codex, DeepSeek and ACP readers, then accepts only that reader's protocol.
`TextStream`, missing readers and unknown readers cannot supply assistant
text or tool events, even if stdout contains JSON shaped like agent events.
The runner's final answer uses the same validated source.

Earlier conversation producers had no reliable provenance. Their unmarked
rows remain stored but are quarantined from history, progress, replay content
and pending thread delivery. New storage writes carry a private policy marker;
it is not accepted from caller metadata and is omitted from exported metadata.
Quarantine preserves sequence heads, so history reports gaps and advances its
cursor over hidden rows. Already delivered R12 thread files are outside this
read quarantine.

The writer owns a separate database connection; the callback only selects
bounded fields and queues them. Agent text is grouped into slices of
approximately 500 ms, also flushed before a tool or turn boundary. The
queue holds at most 256 events. Saturation drops live detail with a counted
`state` entry; it cannot block the harness reader. Inputs and final answers
wait up to five seconds for queue capacity at a turn boundary, and report
failure if not queued. `close()` waits up to ten seconds for the writer;
an unavailable writer reports an error without stopping the agent. The
ordinary runner also uses this sink for one-turn-per-event harnesses.
Its original `events.jsonl` trace remains available if the database fails.

## Readable thread and retention

Every human message and final answer has a durable R12 outbox in its log
entry. `Log.drain_threads()` locks pending entries, publishes them with
`fil.record`, then acknowledges successful posts. Failure keeps the entry
pending for the next drain (including at recorder startup). A crash after
posting but before acknowledging may create a duplicate; its metadata
contains the same agent/sequence/turn for auditing. The full thread copy
is bounded at 100,000 characters; truncation is explicit.

Detail defaults to 90 days (0033 §9). `Log.prune(days=90)` removes older
entries, retaining the sequence head and any undelivered R12 copy younger
than thread retention. Recorder startup prunes its agent's detail, then
drains pending copies. A deployment can call the same API periodically for
dormant agents or choose a longer canon retention.

### Thread expiry (L361)

Shared threads keep **one year** of entries by default (0033 §9):
`thread_retention_days` in the host configuration, or
`AMEESH_THREAD_RETENTION_DAYS`; `0` keeps them forever.

* **Who**: the runner's housekeeping pass (`AMEESH_HOUSEKEEPING_INTERVAL`)
  expires this host's thread files at most once a day and logs each thread
  it shortens. `ameesh fil expire [--days N] [--dry-run]` does the same by
  hand.
* **What**: the oldest run of entries dated before the cut-off. The scan
  stops at the first newer entry, the first entry without a readable date,
  and the first text that is not a complete ameesh entry (a heading it does
  not recognise, a hand-written note): text that cannot be dated is never
  removed, nor anything after it. The file stays in append order; an R12
  copy delivered late with an old date leaves with its neighbours.
  Everything after the cut is kept byte for byte, including a torn last
  entry. A thread whose header holds such text does not expire at all;
  `fil expire` and the runner's log say so.
* **How**: under the thread's exclusive lock, the kept part is written to a
  new file in the same directory, under an unpredictable name created
  exclusively (never through a link; leftovers of an interrupted expiry are
  only unlinked), synced, then renamed over the thread: a crash leaves the
  old file or the new one. A writer or reader that locked the replaced file
  notices it no longer carries the thread's name and reopens: no append is
  lost.
* **Ids**: entry ids (`<project>/<lot>#<offset>`) are logical offsets and do
  not change. The header holds the shift between logical and physical
  offsets, so `fil tail` cursors, `thread_index.last_entry_id`, mailbox
  traces and `conversation_log.thread_entry` keep pointing at the same
  entry. A cursor on an expired entry reads from the oldest kept one.
* **Audit**: the header says, in plain text and in an `ameesh` comment,
  the cut-off date, the date of the last expiry and how many entries have
  been removed in total.
* **Index**: expiry takes the thread's `thread_index` row lock before the
  file lock, the order the R12 drain already uses, and indexing takes the
  same row lock. The index then holds the number of entries kept and the
  most recent one; an emptied thread quotes no entry. An entry expired
  between its write and its indexing is neither counted nor quoted. The
  recount records, in the same transaction, the thread's logical end
  (`thread_index.counted_until`, migration 0395): an entry before it is
  already counted and indexing it later does not count it twice. The file is
  not proof of a recount: after an expiry without a database (`fil expire`
  without one), or whose recount did not commit (failure, crash after the
  rename), the index is behind the file's last expiry, and the next indexing
  or expiry of that thread first recounts it from the file as it is.
  Between two entries with the same timestamp, the later one is the most
  recent.
* **Mediated executor host** (L109): no transaction or lock holds from one
  remote request to the next, so the executor contract admits neither
  `threads.lock` nor `threads.expired`. Indexing there works as before
  L361 (no lock, no recount, every entry counted: `threads.index` with
  `count` left at its default, true). Such a host does not expire its
  threads yet, whether its connection to the server works or not
  (`backend: mediated` is enough): `fil expire` answers with an error and
  changes nothing; `--dry-run` lists the entries past retention, says that
  expiry is not available and fails too. The runner, which has no local
  housekeeping there, logs it at start and then once a day.
  The server-side operation that does lock, recount and expiry in a single
  transaction is the follow-up of #421.
* **R12 outbox**: an undelivered copy older than thread retention is
  deleted by `Log.prune` (its thread entry would expire on arrival);
  younger ones still wait for delivery, past detail retention.

L79 supplies the terminal client and L83 the web input.
