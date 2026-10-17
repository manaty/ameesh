# Install on a workstation

ameesh installs as a Python **tool**: one command, an isolated environment,
and its five executables (`ameesh`, `agent-mail`, `agent-runner`,
`ameesh-approve`, `ameesh-it-proof`) on your `PATH`.

This page is the **installation on a human's workstation**.
[Getting started](../getting-started.md) covers the disposable test bench
instead.

!!! warning "ameesh is not published on PyPI"
    `uv tool install ameesh` does **not** resolve: ameesh lives in its Git
    repository. Install from the repository or from a local clone, as below.
    No packaged or signed executable is provided for now (owner answer
    #9416, 2026-10-12).

## Platform support in v1.10.0

| Platform | State |
|---|---|
| **Linux** | The reference platform; the full suite runs in CI. Install and run. |
| **macOS** | **Not supported yet.** The platform layer of L63 is merged (psutil, guarantees that refuse instead of passing) and CI runs a restricted macOS part (`tests.test_l63_platform`), but the launchd service, the Keychain credentials check and the macOS full suite (L65, L68) are not delivered. Use a **remote database only** (owner answer #9414). |
| **Windows** | **WSL2 only.** Native Windows is deferred (owner answer #9411; lot L67), and ameesh does not start there. Inside WSL2 it is a Linux, so the Linux instructions below apply, with one constraint: the **harness must run inside WSL too**, because ameesh cannot see Windows processes and cannot verify that a session is closed. |

## Requirements

- Linux, or WSL2, with `bash`;
- Python **3.9 or later** — `uv` can install one for you;
- `git`, to install from the repository;
- a PostgreSQL database reachable from the workstation; `psycopg` comes with
  the package, so no `psql` binary is required (on macOS, not supported yet,
  only a remote database applies — see the table above);
- the harness binaries (`claude`, `codex`, `dsh`) are installed separately.

## 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On macOS, `brew install uv` also works. Other routes (Windows installer,
pipx, standalone binary) are on the
[official installation page](https://docs.astral.sh/uv/getting-started/installation/).

## 2. Install ameesh

From the Git repository — pin a tag or a commit for a reproducible install:

```bash
uv tool install "git+https://github.com/manaty/ameesh@main"
# or a frozen ref:
uv tool install "git+https://github.com/manaty/ameesh@<tag-or-commit>"
```

From a local clone, when you follow a checkout:

```bash
git clone https://github.com/manaty/ameesh.git ~/src/ameesh
uv tool install ~/src/ameesh              # add --editable to follow the clone
```

`uv` prints the `bin` directory it installed into; if it is not on your
`PATH`, run `uv tool update-shell` and open a new shell. Check the install:

```bash
ameesh --version      # ameesh 1.10.0
ameesh doctor         # driver, schema, migrations (--probe: one-line light check for container probes)
```

The older route still works and installs the same package:

```bash
python3 -m venv ~/.local/share/ameesh/venv
~/.local/share/ameesh/venv/bin/pip install -e ~/src/ameesh
```

## 3. Configure

ameesh reads `AMEESH_*` environment variables first, then
`~/.config/ameesh/config.json`, then its defaults — the full list is in the
[configuration reference](../reference/configuration.md). The two settings
that matter first:

- `dsn` (`AMEESH_DSN`): the PostgreSQL DSN. No secret goes in that file: the
  password comes from `PGPASSWORD` or `~/.pgpass` (mode `0600`), or is part of
  the DSN;
- `host` (`AMEESH_HOST`): the name of the host this runner serves.

```bash
ameesh migrate        # first install, and after every upgrade
ameesh doctor         # schema and migrations up to date
```

Then declare the host, its agents and their placements in the canon
([Write a canon](write-a-canon.md)), and start the machine's runner
([Operate agents](operate-agents.md)). A container profile for a single
isolated agent is described in [Cluster profile](cluster-profile.md).

## 4. Upgrade

```bash
uv tool upgrade ameesh                                  # same ref
uv tool install --force "git+https://github.com/manaty/ameesh@<new ref>"
```

The command above updates **one** host. How to roll it out across hosts sharing
a database depends on the migrations of the new version:

- **additive migrations**, the normal case: they add without changing what the
  older code writes, so the previous version still runs against the migrated
  database. The shared staging database is migrated once, then the hosts are
  updated one by one — agent VMs first, the workstation last, and only if the
  VMs are healthy. A host is healthy when its restarted runners publish their
  liveness, `ameesh doctor` is green and a test mail is delivered; an unhealthy
  host returns to the previous version (`retour` of `deploy/mise-a-jour/`) and
  the deployment stops. This staging update needs no release procedure.
- **non-additive migrations**: a migration changes a key or a shape the older
  code writes to, so the older binary no longer runs against the migrated
  database (v1.4.0 and its migrations 0032 and 0035 were that case). Then every
  host and every service sharing the database (runners, `ameesh-approve`,
  `ameesh notify`, canon sync timers, v1 `agent-mail` hooks) is stopped,
  upgraded **together**, and restarted; a migration is not undone, and the
  backup taken before `ameesh migrate` is the database's way back.

Back up before `ameesh migrate` in both cases. The version-by-version procedure
is in
[Switch from a v0 setup](switch-from-v0.md#upgrading-an-existing-installation);
the mesh's staging update is the step-by-step `deploy/mise-a-jour/` procedure
described in `docs/BASCULE.md` (in French).

## What v1.10.0 does not provide

- no PyPI package, and no signed or notarized executable;
- no native Windows (WSL2 only);
- on macOS, no service manager and no Keychain credentials check, and a CI
  part limited to the platform tests.

## How this page was verified

The Linux commands above were executed for this page (lot #394, `uv` 0.13.0,
ameesh 1.10.0 at `b85777c1e`): install from a local clone, install from
`git+…@main`, `--editable`, `uv tool upgrade`, and `ameesh --version` on the
installed tool. macOS and WSL2 were not executed by the project; the table
above is the measured state of v1.10.0.
