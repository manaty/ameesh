# Runner and leases

## One runner per host

`ameesh run` (alias `agent-runner`) is the host's runner. It claims the agents
placed on its host, renews their leases, and starts a **turn** in the agent's
harness when there is something to do. It wakes on Postgres `LISTEN/NOTIFY`,
with a fallback poll (`--poll`, default 5 s).

A turn is triggered, in this order, by: a pending prompt, unread messages,
events, or an idle nudge after `--idle-nudge` seconds of inactivity
(default 1200).

Three harness adapters ship with v1:

| Harness | Command line (simplified) | Resume |
|---|---|---|
| Claude Code | `claude -p --output-format stream-json …` | `--resume <session>` |
| Codex | `codex exec --json …` | `codex exec … resume <thread>` |
| DeepSeek Harness | `dsh --profile agent --json …` | `--session-id <session>` |

The runner reads each harness's JSONL stream, records the session id in the
database **and** on disk, and logs the raw stream under the agent's state
directory. The harness binary is found in `AMEESH_<HARNESS>_BIN`, then
`AMEESH_BIN_DIR/<name>`, then the `PATH`, which is how the bench uses fake
harnesses.

## Leases

An agent is **pinned to a host** (in v1, harness sessions are local files),
unless moving between admitted hosts is enabled (see
[below](#host-resources-and-back-pressure)). The **lease is the only
permission to run**.

An Agent card may instead declare `releve: election` with a list of
`candidats` (hosts its Placement already admits). The runner is installed on
every candidate; any of them may take the lease when it is free or expired,
and the winner becomes the holder. `canon sync` writes the placement verdict
of **each** candidate host, so the host policy and visibility rule are still
checked on the host that claims. Such a role is not moved by L232e: there is

no local work to publish, and the lease epoch fences the former holder. A
planned `host drain` returns the holder's lease at once (epoch incremented in
the drain transaction), so a candidate can take over without waiting for the
expiry; an unplanned stop waits for the lease to expire. The repository note
`docs/HOST-HANDOFFS.md` has the full election rules. The
best *ready* candidate wins, with a short per-rank delay before the claim
(0 s, 20 s, 40 s…); availability over seven days decides, capacity only breaks
ties, and a holder is replaced only after a challenger has stayed 5 points
ahead for an hour, at the end of the holder's turn. `ameesh role status` shows
the holder, the epoch and the ranking.
