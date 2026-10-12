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
                    project=project, lot=lot)
driver = resident.driver_for(adapter, cwd=cwd, on_event=recorder.on_event)
# After the logical turn completes (including its steered inputs):
recorder.finish(final=result.final, status=result.status, cost=result.cost,
                usage=result.usage, duration_s=result.duration_s)
recorder.close()
```

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
entries, retaining any undelivered R12 copy and the sequence head. Recorder
startup drains pending copies then prunes its agent's detail. A deployment
can call the same API periodically for dormant agents or choose a longer
canon retention. This lot does not prune shared, append-only R12 files.
The transport currently retains them indefinitely;
automatic one-year thread expiry from 0033 is not implemented by this
detail-store change. L79 supplies the terminal client and L83 the web input.
