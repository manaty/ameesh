# Per-host session pooling (#653)

This recipe is for **bureau**, after review, on `agents-1` and `agents-2`,
under the Unix user running Ameesh. The script checks the system hostname and
real/effective UID before creating directories or dispatching any action.
Only `ameesh-agents-1` / `ameesh` and `ameesh-agents-2` / `ameesh` are accepted;
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
eventually closes its driver. `ScopedDb` releases these sessions between SQL
operations; explicit transactions and independent gate/controller locks retain
their sessions until completion. No transaction is shared across threads.

The VM pool permits 64 ordinary server sessions and 16 reserve sessions, with
a total ceiling of 80. Clients have a ceiling of 250. Idle *unassigned* server
sessions are closed after 30 seconds. These limits were raised after the
2026-10-13 incident on pc-smichea: the original pool (32+8, ceiling 40,
100 clients) saturated in session mode (40 active, 60 waiting, then `no more
connections allowed (max_client_conn)`), which blocked every mesh CLI of the
host. Bureau checks `2 × runner_count + burst headroom < 80` before switching,
and keeps the three hosts (pc-smichea, agents-1, agents-2) under the server's
300 with 3 × 80 `max_db_connections`. Increasing agent count beyond this
capacity requires review.

An installation made before this sizing is raised **by hand**: the recipe
refuses an existing installation, and a fresh installation elsewhere cannot
raise the limits of a running one — an installation still carrying the earlier
200/32/8/40 sizing is the case this covers. Back up
`~/.config/pgbouncer/pgbouncer.ini` first (dated name, 0600), then set
`max_client_conn = 250`, `default_pool_size = 64`, `reserve_pool_size = 16`,
`max_db_connections = 80` and `server_idle_timeout = 30`. Reload without
dropping clients:

```sh
systemctl --user kill -s HUP ameesh-pgbouncer.service
```

A reload applies the file to new decisions and keeps connected clients; a
restart is not needed and must not happen in a release window. Verify on the
admin console, which `max_client_conn` does not gate:

```sh
psql "host=$HOME/.local/state/pgbouncer/socket port=6432 user=ameesh dbname=pgbouncer" \
  -c 'SHOW CONFIG;'
```

`SHOW CONFIG` must show the five values above.

Known gap (2026-10-13): `pc-smichea` still carries the `server_idle_timeout = 600`
written by hand at the first installation, while this recipe targets 30 s; its
other four limits were raised during the incident (250/64/16/80). The gap only
delays closing an idle server session, it does not affect correctness. It is to
be reconciled at the next planned restart of the host — not during a release
window — with the reload above or by writing these values at that restart.

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
`ameesh notify` also read the pool console (`SHOW POOLS`, `SHOW LISTS`,
`SHOW CONFIG`) on that socket. They raise `pgbouncer_pool_waiting` — urgent,
pushed by default — as soon as a client has queued for at least a second
(`cl_waiting > 0`, `maxwait >= 1`): the server pool is exhausted, well before
the client ceiling refuses anyone. The console is deliberately not subject to
`max_client_conn` (pgbouncer only refuses connections to other databases). The
display reads the real ceiling (`used_clients` / `max_client_conn` from
`SHOW CONFIG`): `free_clients` counts client objects kept for reuse, it is not
free capacity.

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
