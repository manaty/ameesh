# Readable threads

While agents are language models, they talk to each other in human language.
ameesh takes advantage of it: **every message that goes through ameesh,
including agent-to-agent messages, is appended in clear to a thread that the
team can read, search and audit.** There is no hidden channel inside ameesh.

The Postgres mailbox is only the **delivery queue** (hooks, waking runners);
the **thread is the readable reference**.

## Where threads live

One Markdown file per thread, under `AMEESH_THREADS` (default
`~/.local/state/ameesh/fils/<project>/<lot or _projet>.md`), append-only.

- The **project** is the sender's team or project, else the recipient's, else
  `AMEESH_PROJECT`, else `default`. For an agent declared in the canon, it is
  its `team`.
- The **lot** is the message's `--lot`; without one, the message goes to the
  project's thread.

Besides messages, action transitions (proposed, approval requested, approved,
launched, unknown, confirmed…), session rotation summaries and runner notes
(interruption, adopted working directory, budget pause) are also written to the
thread.

## Format

Each entry is a heading, then the body **verbatim** inside a fenced code block
whose fence is longer than any backtick run in the body, then optional
metadata in an HTML comment:

````markdown
### 2026-10-04T10:15:30+02:00 — agent:alpha → agent:beta

```
The text of the message, as is.
```

<!-- ameesh {"host": "laptop", "ids": [12]} -->
````

No line of a message can therefore become a heading, HTML, or anything else in
the rendered thread, and reading it back gives the exact text.

## Unreadable bodies are refused

The body must stand on its own as natural language; structured metadata is
added, never substituted. `mail send` refuses empty bodies, JSON-only bodies,
control or binary characters, and base64/hex blobs over 200 characters (even
folded into lines or spaced out). `--allow-structured` exists for tests and
tools only.

## Reliability

A thread write that fails is reported on stderr and **never loses the
message**: it stays in the mailbox. Each entry is written in one block under a
file lock, and the files are opened without following symbolic links.

## Commands

```bash
ameesh fil list                                    # known threads (index + local files)
ameesh fil show <project> [<lot>] [--last N] [--meta]
ameesh fil tail <project> [<lot>] [--last N]       # follow; Ctrl-C to exit
```

!!! note "Scope of the guarantee"
    The thread covers messages that **go through ameesh**. Messages exchanged
    outside ameesh are not captured. Other transports (team chat tools, e-mail)
    are planned behind the same transport interface.
