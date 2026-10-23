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

## Platform support in v1.17.1

| Platform | State |
|---|---|
| **Linux** | The reference platform; the full suite runs in CI. Install and run. |
| **macOS** | **Supported for a workstation, with a remote database only** (owner answers #9414 and #9416). Install, point the DSN at the mesh's remote database, then install the launchd services with `ameesh service install` (runner and `ameesh notify`); Claude Code credentials are checked in the login Keychain. CI runs the full suite on macOS against a PostgreSQL installed in the CI step, but that job is not a required check until the suite is green there. |
| **Windows** | **WSL2 only.** Native Windows is deferred (owner answer #9411; lot L67), and ameesh does not start there. Inside WSL2 it is a Linux, so the Linux instructions below apply, with one constraint: the **harness must run inside WSL too**, because ameesh cannot see Windows processes and cannot verify that a session is closed. |

## Requirements

- Linux, or WSL2, with `bash`;
- Python **3.9 or later** — `uv` can install one for you;
- `git`, to install from the repository;
- a PostgreSQL database reachable from the workstation; `psycopg` comes with
  the package, so no `psql` binary is required. On macOS, **only a remote
  database applies**: no local PostgreSQL is installed nor documented (owner
  answer #9414);
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

### macOS: remote database, launchd services and Keychain

macOS has no local PostgreSQL: use the **remote database** the mesh already
runs. The host configuration carries no secret; the password comes from
`~/.pgpass` (mode `0600`), from `PGPASSWORD`, or from the host environment
file below.

```bash
mkdir -p ~/.config/ameesh
chmod 700 ~/.config/ameesh

# ~/.config/ameesh/config.json: no password in this file
cat > ~/.config/ameesh/config.json <<'JSON'
{
  "dsn": "host=db.example.net dbname=ameesh user=ameesh",
  "host": "macbook-smichea"
}
JSON

# The launchd services source this file (0600); it is also the place for
# AMEESH_DSN with a password, PGPASSWORD and GH_TOKEN.
install -m 0600 /dev/null ~/.config/ameesh/env
$EDITOR ~/.config/ameesh/env
set -a; . ~/.config/ameesh/env; set +a

ameesh migrate
ameesh doctor
```

Install the services (launchd, user session):

```bash
ameesh service install                    # one runner for the host + ameesh notify
ameesh service status
```

`ameesh service install` writes `~/Library/LaunchAgents/ameesh-runner.plist`
and `~/Library/LaunchAgents/ameesh-notify.plist`, loads them in
`gui/$(id -u)`, and creates `~/Library/Logs/ameesh/` (mode `0700`). The
services start at login and restart after a failure; their output goes to
`~/Library/Logs/ameesh/runner.log` (or `.err`) and `notify.log` (or `.err`).
The plists contain **no secret**: each service sources
`~/.config/ameesh/env` before `exec`uting ameesh.

On a workstation that runs one process per agent (the model of
`deploy/systemd/ameesh-runner-agent@.service`), name the agents instead of
installing the host runner:

```bash
ameesh service install --agent coord --agent reviewer
ameesh service status --agent coord --agent reviewer
```

The two models are exclusive: before switching from the host runner to
per-agent runners, remove the host runner with
`ameesh service uninstall --no-notify`. The service's `PATH` already covers
mise, `~/.local/bin`, Homebrew (`/opt/homebrew/bin`) and the system
directories; if a harness lives elsewhere, add its directory to `PATH` in
`~/.config/ameesh/env`, or name the binary with `harness_bins` / `AMEESH_BIN`.

Read the launchd state directly with
`launchctl print gui/$(id -u)/ameesh.runner` (or `ameesh.runner.<agent>`),
and remove the services with `ameesh service uninstall` (the same `--agent`
selection as the install). `ameesh service install --dry-run` prints the
plists without writing anything; it works on Linux too, to review what macOS
would receive.

Claude Code on macOS stores its OAuth credentials in the **login Keychain**
(service `Claude Code-credentials`), not in `~/.claude/.credentials.json`.
`ameesh accounts list` checks that the item exists, and never reads the
secret (`security find-generic-password` without `-w`). A secondary account
declared with a `path` uses Claude Code's namespaced item
(`Claude Code-credentials-<sha256(dir)[:8]>`, Claude Code 2.1.56 and later):
log in once for that account with `CLAUDE_CONFIG_DIR=<path> claude`, then
declare it in the `accounts` key of the host configuration. Codex keeps its
`auth.json` file on macOS, so its check is unchanged. If the host keeps its
own `claude.json` harness descriptor
(`~/.config/ameesh/harnesses/claude.json`), keep the
`ameesh.accounts.keychain_service` key: it names the Keychain service, and a
descriptor without it falls back to the old file check.

`ameesh doctor --notify-test` also proves the desktop channel this OS uses
(`osascript` on macOS, L66).

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

## What v1.17.1 does not provide

- no PyPI package, and no signed or notarized executable (L69);
- no native Windows (WSL2 only);
- on macOS, no full test suite in CI yet (L68): the macOS CI part covers the
  platform layer, the generated launchd plists and the Keychain lookup, not
  the whole suite. Local PostgreSQL is not supported nor documented (remote
  database only, owner answer #9414).

## How this page was verified

The Linux commands above were executed for this page (lot #394, `uv` 0.13.0,
ameesh 1.10.0 at `b85777c1e`): install from a local clone, install from
`git+…@main`, `--editable`, `uv tool upgrade`, and `ameesh --version` on the
installed tool. The macOS launchd plists and the Keychain lookup are exercised
by the macOS CI part (L65); a full install on a real Mac was not executed by
the project, and `launchctl bootstrap` and the Keychain login itself were not.
WSL2 was not executed either; the table above is the measured state of
v1.17.1.
