# agent-mesh

A multi-harness agent mesh (Claude Code, Codex, DeepSeek Harness): a durable
mailbox on Postgres, one runner per machine, agent leases, owner authority by
signature.

Spec: [`MESH-SPEC.md`](MESH-SPEC.md). v0 (local, files, still in production):
`agent-mail.v0.py`, `nexlink-agent.v0.sh` — **not touched by v1**.

## State — v1 points 1 to 5 are built (bench)

| v1 (spec §5) | State |
|---|---|
| 1. agent-mail on Postgres (`agent_registry`, `agent_mailbox`, migrations, LISTEN/NOTIFY, CLI compatible v0) | **done** — [`docs/V1-MAILBOX-RUNNER.md`](docs/V1-MAILBOX-RUNNER.md) |
| 2. one runner per machine, 3 harness adapters with session resume, leases, wake on NOTIFY | **done** — same document |
| 3. owner authority by Ed25519 signature, content- and expiry-bound; two key roles (`owner` = authority, `agent` = provenance only); approvals with single use | **done** — [`docs/V1-AUTORITE-MESH.md`](docs/V1-AUTORITE-MESH.md) |
| 4. `work_items` state machine (intake → build → qa → merged → promoted, + blocked/waiting_human), loop bound, event ledger | **done**; the board stays in git |
| 5. `mesh list` observability | **done** — `agent-mesh list` / `show`, on `agent_mesh_overview` |

Nothing is wired to the real agents yet: v1 runs on a **test bench** (fake
harnesses, a local Postgres container). `~/.local/bin/agent-mail` and the v0
scripts are untouched. The switch is described step by step, with rollback, in
[`docs/BASCULE.md`](docs/BASCULE.md) — to be executed with the owner.

## Quickstart (bench)

```bash
scripts/pg-up.sh                    # container ameesh-pg, postgres:17, 127.0.0.1:55432
export PYTHONPATH=src
# Aucun secret n'est codé dans le dépôt : le mot de passe du banc vient de
# PGPASSWORD ou de ~/.pgpass (mode 600) ; sinon donnez le DSN complet.
export AMEESH_DSN=postgresql://agent_mesh@127.0.0.1:55432/agent_mesh

python3 -m ameesh migrate       # versioned migrations, idempotent
python3 -m ameesh doctor --notify-test

# register an agent pinned to this host, then run the machine's runner
python3 -m ameesh.runner register deepseek7 deepseek --cwd ~/development/manaty/agent-mesh
python3 -m ameesh.runner --max-turns 1

# talk to it
python3 -m ameesh send deepseek7 "ton lot a bougé"
python3 -m ameesh list            # mesh list: host, lease, unread, budget, key

# owner authority (test key only on the bench — the real key is the owner's act)
python3 -m ameesh.mesh_cli key generate --out /tmp/cles --name test --i-am-the-owner
python3 -m ameesh.mesh_cli key register proprietaire --public-key /tmp/cles/test.pub
python3 -m ameesh.cli send deepseek7 "décision" --sign --key /tmp/cles/test.key --expires 12h

# work items
python3 -m ameesh.mesh_cli work add --title "un lot" --app nexlink --assignee deepseek7
python3 -m ameesh.mesh_cli work list
```

Repo-local wrappers: `bin/agent-mail`, `bin/agent-runner`, `bin/agent-mesh`
(they only set `PYTHONPATH=src`; nothing is installed into `~/.local/bin`).

## Commands

`agent-mail` keeps the v0 interface — and the v0 hook JSON, byte for byte:

```
agent-mail send <dest> <texte…> [--from NOM] [--lot ID]   # dest = nom ou "all"
agent-mail list                                # nom, outil, âge, non lus, dossier, hôte, bail
agent-mail inbox [NOM]                         # non lus, sans les marquer lus
agent-mail whoami                              # identité liée (nom + bail), jamais le dossier
agent-mail alias <NOM> <DOSSIER> [CHANTIER]
agent-mail status "<travail en cours>"
agent-mail hook <claude|codex|deepseek>        # JSON identique à la v0
agent-mail statusline                          # barre d'état Claude Code
agent-mail migrate                             # migrations versionnées
agent-mail doctor [--notify-test]              # pilote, schéma, migrations, LISTEN/NOTIFY
```

If Postgres is unreachable, the CLI falls back to the v0 file mailbox
(`~/.local/state/agent-mail`) with a readable warning on stderr; hooks fall back
silently and never fail the agent.

Every message (Postgres or file fallback) is also appended to a **readable
thread** (R12): one Markdown file per thread under `AMEESH_THREADS` (default
`~/.local/state/ameesh/fils/<project>/<lot or _projet>.md`). The project is the
sender's `chantier`, else the recipient's, else `AMEESH_PROJECT`, else
`default`; the lot is `--lot`. Each body is copied verbatim into a fenced code
block whose fence is longer than any backtick run it contains, so no line of a
message can become a heading, HTML or anything else in the rendered thread.
A thread write failure is reported on stderr and
never loses the message. Unreadable bodies (empty, JSON only, control/binary
characters, base64/hex blobs over 200 characters, even folded into lines or
spaced out) are refused;
`--allow-structured` is for tests and tools only.

```
ameesh fil list                                 # threads (index + local files)
ameesh fil show <projet> [<lot>] [--last N] [--meta]
ameesh fil tail <projet> [<lot>] [--last N]     # follow; Ctrl-C to exit
```

`agent-mesh` (observability, authority, work items):

```
agent-mesh list [--json] | show <agent>
agent-mesh key generate --out DIR --i-am-the-owner     # owner's act, never an agent
agent-mesh key register <agent> --public-key FICHIER [--role owner|agent] | key show|list|revoke <agent>
agent-mesh approve --key FICHIER --action A --hash H [--kind K] [--expires 48h]
agent-mesh approvals [--action A] [--hash H] | verify --action A --hash H [--consume]
agent-mesh work add|list|show|move|note …
agent-mesh import-v0 | export-v0                       # v0 → Postgres and back
agent-mesh migrate | doctor [--notify-test]
```

`agent-runner`:

```
agent-runner [--host H] [--runner-id ID] [--agents a,b] [--once [--wait S]]
             [--dry-run] [--max-turns N] [--lease-ttl S] [--poll S] [--migrate]
agent-runner register <nom> <claude|codex|deepseek> [--cwd DIR] [--prompt TEXTE]
             [--session ID] [--chantier C] [--model M] [--budget USD]
agent-runner stop <nom>
```

## Language choice

**Python 3 (stdlib) + `psycopg` when importable, otherwise the `psql` binary.**
Justified in [`docs/V1-MAILBOX-RUNNER.md`](docs/V1-MAILBOX-RUNNER.md):
v0 is Python (same alias/identity/hook semantics, one runtime, no second
toolchain), the stdlib already covers JSON/subprocess/sockets/threads, and Node
has no stdlib Postgres client — it would have forced a dependency, whereas the
Python path runs with **zero** dependencies via `psql`.

## Tests

```bash
scripts/test.sh                      # starts the container, runs the suite twice
scripts/test.sh tests.test_runner    # one module
```

158 tests against the local container, run twice (`psql`, then `psycopg` with
`cryptography`): migrations and immutability, mailbox and LISTEN/NOTIFY round
trips, lease races on 2 and 4 concurrent connections, **two runner processes
competing for the same lease**, lease expiry takeover, fencing of a stale
runner, notification waking a runner in service mode, the three adapters (argv +
session resume, via fake harness binaries that never call the real CLIs), v0
hook-JSON equality, the file fallback, RFC 8032 vectors, **OpenSSL cross-check**
of the Ed25519 implementation, signed/unsigned/tampered/expired messages,
approval single use, `mesh list`, the work_items state machine and its loop
bound, v0 import/export, and the codex3 verdict probes: two key roles
(owner/agent), signed expiry and creation time bound to the row, nonce-based
single-use approvals, live-lease checks in renew/begin_turn/take_pending_prompt,
heartbeat stopping the harness on DB failure, durable `current_prompt`, and the
hook marking mail delivered only after a successful write, heartbeat waking at
the lease deadline, SIGTERM→SIGKILL escalation, prompt restored after a failed
or killed turn, the consumed-nonce ledger backfilled on upgrade (0007), and a
statement timeout (server and client) and a renewal call abandoned at the lease
deadline, process-group termination with SIGKILL on lease loss (even after the
parent dies), the merge of two prompts when a turn is restored, and a
lease-bound agent-mail identity that is never derived from the working
directory, and a renewal budget with no floor once the lease deadline has
passed.

## Layout

```
src/ameesh/
  cli.py          agent-mail (v0-compatible)
  runner.py       agent-runner: leases, turns, NOTIFY wake, CLI
  adapters.py     claude / codex / deepseek command lines and JSONL readers
  db.py           psycopg or psql; LISTEN/NOTIFY; JSON row marshalling
  registry.py     agent_registry: presence, leases, prompts, budget
  mail.py         agent_mailbox: send, unread, deliver, history
  fil.py          readable threads: Transport, file transport, index, ameesh fil
  migrations.py   NNNN_name.sql, checksummed, advisory-locked
  migrations/     0001_init.sql … 0007, 0009_threads.sql (0008 reserved)
  mesh_cli.py     agent-mesh: mesh list, keys, approvals, work items, import/export v0
  authority.py    Ed25519 proofs bound to content and expiry; approvals
  signing.py      Ed25519 (pure RFC 8032 + optional cryptography/nacl), key files
  work.py         work_items state machine, loop bound, event ledger
  backend.py      Postgres backend + v0 file fallback
  identity.py     v0 alias/name resolution (shared aliases.tsv)
tests/            unittest suite + fakebin/ fake harnesses
scripts/          pg-up.sh, test.sh
```

## License

ameesh is released under **AGPL-3.0-only**. The full, unmodified GNU Affero
General Public License v3 text is in [`LICENSE`](LICENSE); it covers every file
of this repository, including the SQL migrations (they carry no per-file header,
because their sha256 is recorded in the database schema) and the test fixtures.
External contributions require a contribution agreement (CLA).
