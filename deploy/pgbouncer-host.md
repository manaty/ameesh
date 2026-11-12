# Per-host session pooling (#653)

This recipe is for **bureau**, after review, on `agents-1`, `agents-2` and `agents-3`,
under the Unix user running Ameesh. The script checks the system hostname and
real/effective UID before creating directories or dispatching any action.
Only `ameesh-agents-1`, `ameesh-agents-2` and `ameesh-agents-3` under `ameesh` are accepted;
root and a mismatched HOME are refused (HOME and the account home are compared
as canonical paths, and the canonical home is used). This accepts an account
home that is itself a symlink, including one targeting a different directory;
this guard checks account identity and path agreement, not a fixed `/home` root. Bureau must invoke the script as that
user, with its user-systemd environment. Agents prepare the files and tests;
they do not install, switch configuration or restart services on these VMs.

Use the Python from the installed Ameesh environment (with psycopg and psutil).
The commands below assume it is `~/.local/share/ameesh/venv/bin/python3`.
Unset DSN/config overrides before running them. Keep each host's output with
the lot; the script prints no DSN, password or SQL statement text.

Prerequisites: user systemd, a compiler, make, pkg-config, libevent and OpenSSL
development files; Python 3.12+ (tar extraction filter); an on-disk private
`TMPDIR`; a direct, TLS-enabled upstream DSN in `~/.config/ameesh/config.json`.
If a dependency is missing, bureau handles its installation. No sudo appears
in the recipe. A custom TLS certificate configuration needs explicit review.

```sh
mkdir -p "$HOME/.local/state/pgbouncer-build"
chmod 700 "$HOME/.local/state/pgbouncer-build"
export TMPDIR="$HOME/.local/state/pgbouncer-build"
~/.local/share/ameesh/venv/bin/python3 deploy/pgbouncer-host.py measure --label before
~/.local/share/ameesh/venv/bin/python3 deploy/pgbouncer-host.py install
~/.local/share/ameesh/venv/bin/python3 deploy/pgbouncer-host.py verify
```

Installation refuses an existing configuration/service or **any preexisting
binary**, including symlinks. A matching `--version` is not proof of provenance.
Only a binary freshly compiled by this invocation may be executed. The local
DSN and credentials are validated before writing installation files. On a later
failure, files created by this invocation are removed so installation can be
retried; any attempted service start is stopped first. If stopping fails, files
are retained and bureau must stop the service and inspect them before cleanup.
Preexisting files are never removed.
Version 1.26.0 is compiled under `ameesh heavy --` from the
[official archive](https://www.pgbouncer.org/downloads/files/1.26.0/pgbouncer-1.26.0.tar.gz),
with pinned SHA256 `afd25dd61ee6775d37b40629b87ce08736b3e6955f3057bb212e410fbf21c71d`.
The source download is capped at 32 MiB; the temporary build is removed.
The service is started, **not enabled**. The binary lives in `~/.local/bin`,
configuration in `~/.config/pgbouncer`, and private sockets/data in
`~/.local/state/pgbouncer`. The password is loaded through libpq (including
`.pgpass`) and written only to a 0600 userlist file. Local trust is restricted
by the 0700 socket directory and 0600 Unix socket. There is no TCP listener.

The [PgBouncer configuration reference](https://www.pgbouncer.org/config.html)
defines session mode: a server session remains assigned until its client
disconnects. This preserves LISTEN/NOTIFY and explicit transactions. Therefore
pooling does **not** reduce the runner's two persistent sessions to one.
The third and fourth connections observed on the PC belonged to auxiliary
passes: their session lifetime spans external work even though each pass
eventually closes its driver. `ScopedDb` holds one session per thread for a
pass (#5979) and returns it at the end of the pass, before a provider HTTP
call, after one idle second (git, gh, ssh between two statements) and after
any error; explicit transactions and independent gate/controller locks retain
their sessions until completion. No transaction is shared across threads.

The pool of pc-smichea permits 24 ordinary server sessions and 8 reserve
sessions, with a total ceiling of 32; agents-1 has 72+16 (ceiling 88),
agents-2 80+16 (96) and agents-3 40+8 (48), see below. Clients have a ceiling
of 250. Idle *unassigned* server sessions are closed after 30 seconds. These
limits descend from the 2026-10-13 incident on pc-smichea: the original pool
(32+8, ceiling 40, 100 clients) saturated in session mode (40 active, 60
waiting, then `no more connections allowed (max_client_conn)`), which blocked
every mesh CLI of the host. Pools are sized on the **peak** measured on
2026-11-06 by #5983, not on the average: agents-2 saw 92 clients for 20
personas (4.6 per persona) and 75/80 server sessions while most agents were
paused; agents-1 saw 56 clients for 16 personas (3.5) and 50/64 sessions;
agents-3 (8 personas) stayed just under 40. Those observed counts are not the
admission ceilings: the canon host cards allow 19/20/10 for agents-1/2/3. The
pre-switch control is the host card's **admission capacity**, not the installed
runner count:
`max_agents × 4.6` must fit `max_db_connections` (one server session per
active client in session mode). agents-1 (19) needs 87.4 → 72+16 = 88,
agents-2 (20) needs 92 → 80+16 = 96, agents-3 (10) needs 46 → 40+8 = 48.
pc-smichea carries no execution agent since 2026-10-28 (decision #133454 a,
lot #3463) and only its bureau session; its canon `max_agents` 27 is stale.
Its provisional pool is 24+8 = 32: ask the bureau for the current peak of its
pool before fixing the figure, and if that peak exceeds 24, resize instead of
leaving the reserve short. A companion lot in `manaty/home` lowers its
`max_agents` to at most `floor(32 / 4.6) = 6`, citing #6054 and #133454 a.
Until that lot lands, readmitting execution agents on the workstation requires
resizing its pool first. The total is 88+96+48+32 = 264 of the server's 297
ordinary slots (`max_connections` 300 minus 3 `superuser_reserved_connections`),
leaving 33 for direct clients; the recipe test keeps at least 30. Before
switching a host, bureau reads `max_agents` from its canon host card and runs
`python3 deploy/pgbouncer-host.py check --max-agents N`: N × 4.6 must not
exceed the host's `max_db_connections`. Increasing an admission ceiling
requires resizing its pool first.

An installation made before this sizing is raised **by hand**: the recipe
refuses an existing installation, and a fresh installation elsewhere cannot
raise the limits of a running one — an installation still carrying the
2026-10-13 sizing (250/64/16/80 or 200/32/8/40) or the #5983 sizing (48+16 on
agents-1, 64+16 on agents-2 and pc-smichea) is the case this covers. Back up
`~/.config/pgbouncer/pgbouncer.ini` first (dated name, 0600), then set
`max_client_conn = 250`, `server_idle_timeout = 30` and the host's own
`default_pool_size`, `reserve_pool_size` and `max_db_connections`
(`POOL_LIMITS` in `pgbouncer-host.py`: 24/8/32 on pc-smichea, 72/16/88 on
agents-1, 80/16/96 on agents-2, 40/8/48 on agents-3). Reload without
dropping clients:

```sh
systemctl --user kill -s HUP ameesh-pgbouncer.service
```

A reload applies the file to new decisions and keeps connected clients; a
restart is not needed for these five values (unlike `track_extra_parameters`,
see below) and must not happen in a release window. Verify on the
admin console, which `max_client_conn` does not gate:

```sh
psql "host=$HOME/.local/state/pgbouncer/socket port=6432 user=ameesh dbname=pgbouncer" \
  -c 'SHOW CONFIG;'
```

`SHOW CONFIG` must show the five values above.

#6054 moves the four hosts from the #5983 sizing (80/64/80/40) to
32/88/96/48: all three agent VMs are raised, and pc-smichea is lowered. The
sum stays 264. Apply the same backup, values and HUP per host; lowering the
workstation is safe because it carries no execution agent, only the bureau
session, since 2026-10-28. The recipe's `check --max-agents N` is the
pre-switch control (canon `max_agents` × 4.6 against the host's
`max_db_connections`).

Known gap (2026-10-13): `pc-smichea` still carries the `server_idle_timeout = 600`
written by hand at the first installation, while this recipe targets 30 s; its
other four limits were raised during the incident (250/64/16/80), and #6054
sets them to 24/8/32. The gap only delays closing an idle server session, it
does not affect correctness. It is to be reconciled at the next planned
restart of the host — not during a release window — with the reload above or by
writing these values at that restart.

## Upstream follows the database primary (#6862)

With the EP8 cluster, the upstream must be the current primary. PgBouncer has
no `target_session_attrs`, and a comma-separated `host=` list is used
round-robin, standbys included. The pool keeps one upstream host;
`pgbouncer-host.py amont` resolves the read-write server of the multi-host DSN
in `~/.config/pgbouncer/amont.dsn` (0600, no password: `~/.pgpass`), rewrites
the `[databases]` line only when it differs (backup
`pgbouncer.ini.avant-amont`) and sends `RELOAD`. `server_fast_close = 1`
(reloadable: a HUP applies it, verified with `SHOW CONFIG` on 1.26) closes the
old upstream's server sessions at the end of their transaction.
`install-amont` writes and enables a user timer running it every 15 s. The
procedure, the refusals and the rollback are in `docs/EXPLOITATION.md`
(« Amont du PgBouncer local »).

## Session settings the pool must track (#3745)

Every Ameesh session sends its settings in the startup packet
(`db.startup_options`: `-c search_path=…`, `-c statement_timeout=…`,
`-c jit=off` since #3503, `-c default_transaction_read_only=on` for read-only
hosts). PgBouncer **refuses** a session carrying a setting it does not track
(`FATAL: unsupported startup parameter in options: jit`), so every runner of the
host fails to connect. It always tracks `application_name`, `client_encoding`,
`DateStyle`, `default_transaction_read_only`, `IntervalStyle`,
`scram_iterations`, `search_path`, `session_authorization`,
`standard_conforming_strings` and `TimeZone`; the recipe adds
`track_extra_parameters = search_path,statement_timeout,jit`. A setting listed
in `ignore_startup_parameters` instead is accepted but never applied.

Since #5976 every session also names itself
(`ameesh/<family>/<agent or role>@<host>`, as `application_name` in the startup
packet): PgBouncer tracks it natively, so the server's `pg_stat_activity` shows
the client's name behind the pool too. Nothing to add to the recipe.

An installation generated before #3745 (pc-smichea, 2026-10-13) stops at
`search_path,statement_timeout` and is corrected **by hand**. Back up the file
first (dated name, 0600), then change that line only:

```sh
cd ~/.config/pgbouncer
cp -p pgbouncer.ini "pgbouncer.ini.bak-$(date +%Y%m%d-%H%M%S)"
sed -i 's/^track_extra_parameters = search_path,statement_timeout$/track_extra_parameters = search_path,statement_timeout,jit/' pgbouncer.ini
grep -n '^track_extra_parameters' pgbouncer.ini
```

A HUP is **not** enough: in 1.26, `track_extra_parameters` is `CF_NO_RELOAD`
(`src/main.c`), and a reload silently keeps the old value; `SHOW CONFIG` keeps
showing it. Only a restart applies it:

```sh
systemctl --user restart ameesh-pgbouncer.service
```

In session mode, a restart closes every client session of the host. Bureau does
it outside a release window (not in the 20 minutes before a cut-off) and then
checks the host's runners like after `switch`. Verify on the admin console that
`track_extra_parameters` includes `jit`, then with the doctor:

```sh
psql "host=$HOME/.local/state/pgbouncer/socket port=6432 user=ameesh dbname=pgbouncer" \
  -c 'SHOW CONFIG;' | grep track_extra_parameters
ameesh doctor
```

When the DSN is the local pool socket, `ameesh doctor` compares the settings an
Ameesh session sends with the running `SHOW CONFIG` of that pool before it opens
its connection: `pgbouncer  : startup options accepted (…)`, or `KO` naming the
refused setting. Its `jit` line shows the session value (`off`). `doctor --probe`,
which guards host updates, names the pool when its connection fails this way. A
new setting in `db.startup_options` must be added to `TRACK_EXTRA_PARAMETERS`
in `deploy/pgbouncer-host.py` (a test fails otherwise) and to every existing
pool, restarted, **before** that code is deployed on the host.

After the corrected runner release is available and installation verification
passes, bureau switches and drains/restarts the runner units it manages:

```sh
~/.local/share/ameesh/venv/bin/python3 deploy/pgbouncer-host.py switch
# Bureau drains/restarts the identified runner units, preserving active turns.
# Wait for restart completion and at least 30 seconds of idle pool time.
~/.local/share/ameesh/venv/bin/python3 deploy/pgbouncer-host.py measure --label after
ameesh doctor --notify-test
```

The config backup is exclusive and 0600:
`~/.config/ameesh/config.json.before-pgbouncer-653`. Switch/rollback write the
configuration atomically with mode 0600. A running process keeps its original
DSN until restart. No runner is restarted by the script.
The socket DSN retains SQL startup options while removing upstream TLS
negotiation, certificate and channel-binding settings and `hostaddr`;
The local DSN explicitly sets `sslmode=disable`. With libpq 17 or later, it also
sets `sslnegotiation=postgres`, overriding `PGSSLNEGOTIATION=direct` and libpq
service defaults. With libpq 16 or earlier, that unsupported option is omitted;
these versions do not implement direct TLS negotiation.

Before/after JSON records contain server-wide client count, `max_connections`,
counts by client IP and state, local runner count and direct TCP connections
per runner. When the pool is running, `SHOW CLIENTS` Unix peer PIDs identify
pooled sessions per runner, and `SHOW SERVERS` distinguishes unassigned server
sessions. They include the measurement connection, which closes immediately.
Counts cover every database on the server. Distinguish child CLI traffic and
unassigned idle pool sessions from resting runner sessions: after the idle
timeout, a quiet host should have **at most two sessions per runner**. Capture
an additional observation if a gate transaction or a child CLI was active.
Retain the exact runner version and count, and the observation's timestamp,
with the host result. The production before/after verdict is delivered only
after bureau runs these commands; unit tests are not production measurements.

`ameesh alerts` warns at **70%** of the server's configured max_connections,
including equality (210 of 300). It is a default urgent notification;
`doctor` prints used/max/idle and returns KO at that threshold. Explicit custom
notification type filters still apply. Mediated clients do not run SQL probes.
If the capacity probe fails, the alert command reports the database error.
The notifier retries the failed pass before updating its active state: an
unobserved capacity value never resolves an existing warning or raises it
again on recovery. A successful reading below the threshold resolves it.

When the effective DSN is the local pool socket itself (its `host` is the
`unix_socket_dir` of `~/.config/pgbouncer/pgbouncer.ini`), `ameesh alerts` and
`ameesh notify` also read the pool console (`SHOW POOLS`, `SHOW SERVERS`,
`SHOW LISTS`, `SHOW CONFIG`) on that socket. They raise
`pgbouncer_pool_at_ceiling` — urgent, pushed by default — as soon as every
server session `max_db_connections` allows is open (`SHOW SERVERS` count),
with the queue in its detail; and `pgbouncer_pool_waiting` when a client has
waited at least the running `reserve_pool_timeout` (`cl_waiting > 0`,
`maxwait >= reserve_pool_timeout`, 5 s by default). The one-second threshold
used before #6054 was stricter than the design: a one-second wait under the
reserve timeout is the normal session-mode behaviour when the ordinary pool is
full (#5983). The ceiling alert wins when both hold, so one pool raises one
alert. The console is deliberately not subject to `max_client_conn` (pgbouncer
only refuses connections to other databases). The display reads the real
ceilings (`used_clients` / `max_client_conn`, `max_db_connections`,
`reserve_pool_timeout` from `SHOW CONFIG`): `free_clients` counts client
objects kept for reuse, it is not free capacity.

`ameesh notify` reads this console from `cfg` alone, **before** its database
pass and without opening the ordinary DSN. While `max_client_conn` refuses the
notifier's own connection — the incident this warning reports — the console
still answers, so the warning is routed and pushed anyway. A database outage
never resolves the other alerts: their deliveries are left untouched. An
unreadable console does not resolve the active warning either; the observation
error is kept, as for the server capacity probe. `ameesh alerts`, which lists
every other alert from the database, still needs a working connection. A host
without a local pool, or whose DSN does not use it yet, adds no probe; the
warning is local to the host that runs the notifier.

The console read is bounded (`PROBE_TIMEOUT_S`, 5 s): `connect_timeout` does
not apply to a Unix-domain socket, and pgbouncer's `SHOW` commands have no
statement timeout. A console that accepts the connection and stops answering
therefore no longer hangs `ameesh alerts` (or ignores `SIGINT`): the probe
gives up, the pass keeps every other alert family, and the console family is
reported as `pgbouncer_console_unreadable` (#1179, #2582).

Rollback, by bureau:

```sh
~/.local/share/ameesh/venv/bin/python3 deploy/pgbouncer-host.py rollback
# Drain/restart runner units so every process uses the restored direct DSN.
# Then, and only then:
systemctl --user stop ameesh-pgbouncer.service
~/.local/share/ameesh/venv/bin/python3 deploy/pgbouncer-host.py measure --label rollback
```

Rollback retains the exclusive backup and installation for diagnosis. If the
installation failed before switch, the Ameesh config is untouched: stop the
service, then bureau may remove only files created by this recipe after checking
that no client uses its socket. Never delete an existing #532 installation.
Build errors are suppressed to keep operator logs secret-free; rerun configure
and `make -j2 pgbouncer` privately to diagnose missing build dependencies.
