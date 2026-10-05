# Getting started (bench)

This page sets up the **test bench**: a local Postgres container, the ameesh
sources, and fake harness binaries. Nothing here calls a real model, GitHub,
or a real passkey.

## Requirements

- Linux or another Unix-like system with `bash`
- Python **3.9 or later** (the standard library is enough)
- `git`
- Docker, for the bench database (`postgres:17`)
- either the `psql` client or the Python package `psycopg` (ameesh uses
  `psycopg` when it is importable, otherwise the `psql` binary)

## 1. Get the sources and start the bench database

```bash
git clone https://github.com/manaty/ameesh.git
cd ameesh

# No password is stored in the repository. Choose one for the local container:
export AMEESH_PG_PASSWORD='choose-a-local-password'   # read once, when the container is created
export PGPASSWORD="$AMEESH_PG_PASSWORD"                # clients read PGPASSWORD (or ~/.pgpass)

scripts/pg-up.sh     # container "ameesh-pg", postgres:17, published on 127.0.0.1:55432 only
```

The port is published on the loopback interface only, and a named volume keeps
the data between restarts.

## 2. Watch the whole v1 scenario

```bash
scripts/demo-v1.sh
```

The demo replays the end-to-end test (`tests/test_bout_en_bout.py`) step by
step, with comments, on a temporary database that is dropped at the end, even
on error or Ctrl-C. It uses a **fictitious** organisation (`acme`), fake
harnesses, a fake `gh` and a software passkey. See
[Run the demo](guides/run-the-demo.md) for a walk-through of each step.

## 3. Run the test suite

```bash
scripts/test.sh                            # the full suite, twice: psql driver, then psycopg
scripts/test.sh tests.test_bout_en_bout    # only the end-to-end test
```

`scripts/test.sh` starts the container if needed, runs the suite with the
`psql` driver, then creates a local `.venv` with `psycopg` (and `cryptography`)
and runs it again with the `psycopg` driver.

## 4. Try the commands by hand

Repository-local wrappers live in `bin/` (`bin/ameesh`, `bin/agent-runner`,
`bin/agent-mail`, `bin/ameesh-approve`); they only set `PYTHONPATH=src`.
The examples below use `python3 -m ameesh` with `PYTHONPATH=src`.

```bash
export PYTHONPATH=src
export AMEESH_DSN=postgresql://agent_mesh@127.0.0.1:55432/agent_mesh
export AMEESH_BIN_DIR="$PWD/tests/fakebin"   # fake claude / codex / dsh: no real harness is started
export AMEESH_HUMANS=alice                   # names that are humans, not agents

python3 -m ameesh migrate                    # versioned migrations, idempotent
python3 -m ameesh doctor --notify-test       # driver, schema, migrations, LISTEN/NOTIFY

# register an agent pinned to this host, send it a message, run one pass of the runner
python3 -m ameesh run register worker1 deepseek --cwd ~/src/acme
python3 -m ameesh mail send worker1 "Please start on lot 7." --from alice
python3 -m ameesh run --once

python3 -m ameesh list                       # host, lease, unread, budget
python3 -m ameesh fil list                   # readable threads
python3 -m ameesh fil show default            # the message, verbatim

# work items (lots) and the progress view
python3 -m ameesh work add --title "Export invoices" --app demo --assignee worker1
python3 -m ameesh work list
python3 -m ameesh progress
```

!!! warning "Without `AMEESH_BIN_DIR`, the runner starts the real harness"
    The runner looks for the harness binary in `AMEESH_<HARNESS>_BIN`, then
    `AMEESH_BIN_DIR/<name>`, then the `PATH`. On a machine where `claude`,
    `codex` or `dsh` is installed, leaving `AMEESH_BIN_DIR` unset runs the real
    one.

!!! note "If Postgres is unreachable"
    The mail commands fall back to the v0 file mailbox
    (`~/.local/state/agent-mail`) with a readable warning; hooks fall back
    silently and never fail the agent.

## Next steps

- Understand the model: [Concepts](concepts/index.md).
- Describe your own team: [Write a canon](guides/write-a-canon.md).
- Every command: [CLI reference](reference/cli.md).
