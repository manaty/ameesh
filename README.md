# agent-mesh

A multi-harness agent mesh (Claude Code, Codex, DeepSeek Harness): a durable
mailbox on Postgres, one runner per machine, agent leases, owner authority by
signature.

Spec: [`MESH-SPEC.md`](MESH-SPEC.md). v0 (local, files, still in production):
`agent-mail.v0.py`, `nexlink-agent.v0.sh` — **not touched by v1**.

## State — v1 points 1 and 2 are built

| v1 (spec §5) | State |
|---|---|
| 1. agent-mail on Postgres (`agent_registry`, `agent_mailbox`, migrations, LISTEN/NOTIFY, CLI compatible v0) | **done** — [`docs/V1-MAILBOX-RUNNER.md`](docs/V1-MAILBOX-RUNNER.md) |
| 2. one runner per machine, 3 harness adapters with session resume, leases, wake on NOTIFY | **done** — same document |
| 3. owner authority by Ed25519 signature through the governance gate | not started (`signature` columns are in place) |
| 4. board stays in git, `work_items` tracks packages | not started (IMA side) |
| 5. `mesh list` observability | partly: `agent-mail list` shows host, lease, unread, budget; the `agent_mesh_overview` view is ready |

Nothing is wired to the real agents yet: v1 runs on a **test bench** (fake
harnesses, a local Postgres container). `~/.local/bin/agent-mail` and the v0
scripts are untouched.

## Quickstart (bench)

```bash
scripts/pg-up.sh                    # container agent-mesh-pg, postgres:17, 127.0.0.1:55432
export PYTHONPATH=src
export AGENT_MESH_DSN=postgresql://agent_mesh:agent_mesh@127.0.0.1:55432/agent_mesh

python3 -m agent_mesh migrate       # versioned migrations, idempotent
python3 -m agent_mesh doctor --notify-test

# register an agent pinned to this host, then run the machine's runner
python3 -m agent_mesh.runner register deepseek7 deepseek --cwd ~/development/manaty/agent-mesh
python3 -m agent_mesh.runner --max-turns 1

# talk to it
python3 -m agent_mesh send deepseek7 "ton lot a bougé"
python3 -m agent_mesh list
```

Repo-local wrappers: `bin/agent-mail`, `bin/agent-runner`, `bin/agent-mesh`
(they only set `PYTHONPATH=src`; nothing is installed into `~/.local/bin`).

## Commands

`agent-mail` keeps the v0 interface — and the v0 hook JSON, byte for byte:

```
agent-mail send <dest> <texte…> [--from NOM]   # dest = nom ou "all"
agent-mail list                                # nom, outil, âge, non lus, dossier, hôte, bail
agent-mail inbox [NOM]                         # non lus, sans les marquer lus
agent-mail whoami [--cwd DIR]                  # identité résolue (alias v0 partagés)
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

63 tests against the local container: migrations and immutability, mailbox and
LISTEN/NOTIFY round trips, lease races on 2 and 4 concurrent connections,
**two runner processes competing for the same lease**, lease expiry takeover,
fencing of a stale runner, notification waking a runner in service mode, the
three adapters (argv + session resume, via fake harness binaries that never call
the real CLIs), v0 hook-JSON equality, and the file fallback.

## Layout

```
src/agent_mesh/
  cli.py          agent-mail (v0-compatible)
  runner.py       agent-runner: leases, turns, NOTIFY wake, CLI
  adapters.py     claude / codex / deepseek command lines and JSONL readers
  db.py           psycopg or psql; LISTEN/NOTIFY; JSON row marshalling
  registry.py     agent_registry: presence, leases, prompts, budget
  mail.py         agent_mailbox: send, unread, deliver, history
  migrations.py   NNNN_name.sql, checksummed, advisory-locked
  migrations/     0001_init.sql, 0002_overview.sql
  backend.py      Postgres backend + v0 file fallback
  identity.py     v0 alias/name resolution (shared aliases.tsv)
tests/            unittest suite + fakebin/ fake harnesses
scripts/          pg-up.sh, test.sh
```
