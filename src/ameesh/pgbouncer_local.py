# SPDX-License-Identifier: AGPL-3.0-only
"""Local PgBouncer pool saturation warning (#532, #653 follow-up).

A local PgBouncer in session mode queues clients as soon as its server pool
is exhausted, then refuses them outright when `max_client_conn` is reached
(`no more connections allowed`): on 2026-10-13 that blocked every mesh CLI of
pc-smichea for about ten minutes. The warning is read on the admin console of
the local Unix socket. PgBouncer deliberately keeps that console reachable
when `max_client_conn` is full (a client connecting to the `pgbouncer`
database is not refused by that limit), so it answers while the pool refuses
everyone else.

Nothing is probed unless the effective DSN *is* this socket: a host without
an installed pool, or one whose clients do not use it yet, gets no probe and
no alert. An observation failure is not a resolution: it is raised, so the
notifier keeps an active warning instead of resolving it unobserved — the
same rule as the server capacity probe (`db_capacity`).

The warning must survive the incident it reports: the mesh database is reached
through this same pool, so `ameesh notify` cannot open its ordinary connection
while the pool refuses everyone. `notify` therefore reads this console from
`cfg` alone (`probe`), before and independently of its database pass: the
warning is routed and pushed even then.

#2582: the console read is bounded by `PROBE_TIMEOUT_S`. A console that
accepts the socket and stops answering used to hang `ameesh alerts` (and
`SIGINT` with it) after every other alert family had been read; it now raises
`Unavailable` and becomes a `pgbouncer_console_unreadable` line.
"""
from __future__ import annotations

import threading

from pathlib import Path

CONFIG_PATH = '.config/pgbouncer/pgbouncer.ini'
#: how long the oldest queued client must have waited before warning (seconds)
WAIT_THRESHOLD_S = 1
CONNECT_TIMEOUT_S = 5
#: #2582: hard deadline of one console probe (connection and the three SHOW
#: queries), in seconds. `connect_timeout` is ignored by libpq on a Unix-domain
#: socket and the console has no statement timeout: a console that accepts the
#: connection and stops answering would hang `ameesh alerts` (and the notifier's
#: local pass) forever. The probe then raises `Unavailable`, which the alert
#: pass turns into `pgbouncer_console_unreadable` (#1179) instead of blocking
#: the whole command.
PROBE_TIMEOUT_S = 5.0
#: the family named when the console could not be read (#2582): an alert
#: absent from that pass is not resolved (`--follow`, #1901).
FAMILY = "pgbouncer_console"
#: the raised message keeps one bounded line: a console error may be long
_ERROR_MAX = 200


def _one_line(value) -> str:
    return ' '.join(str(value).split())[:_ERROR_MAX]


def declared_socket_dir(path) -> str | None:
    """The `unix_socket_dir` a pgbouncer.ini declares, or None.

    Only whole lines are read, and `;`/`#` introduce a comment only at the
    start of a line (pgbouncer does not treat them as special later), so a
    socket path is never silently truncated."""
    try:
        lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith((';', '#')):
            continue
        key, separator, value = line.partition('=')
        if separator and key.strip() == 'unix_socket_dir':
            value = value.strip()
            return value or None
    return None


def admin_dsn(cfg):
    """Admin-console DSN of the local pool this host's DSN points at, or None.

    None when the DSN is not a Unix socket, when no local `pgbouncer.ini`
    declares that same directory, or when psycopg is absent: a host with no
    pool, or with a pool nobody uses, has nothing to watch here. The DSN
    already connects through this pool, so only the database and the startup
    options change; `hostaddr` is dropped so the Unix socket is really used."""
    if getattr(cfg, 'backend', '') == 'mediated':
        return None
    try:
        import psycopg
    except ImportError:
        return None
    try:
        params = psycopg.conninfo.conninfo_to_dict(getattr(cfg, 'dsn', '') or '')
    except Exception:
        return None
    host = params.get('host') or ''
    if not host.startswith('/'):
        return None
    declared = declared_socket_dir(Path.home() / CONFIG_PATH)
    if declared is None or Path(declared) != Path(host):
        return None
    params.pop('hostaddr', None)
    params.update(dbname='pgbouncer', options='')
    return psycopg.conninfo.make_conninfo(**params)


def _total(rows, key) -> int:
    return sum(int(row.get(key) or 0) for row in rows)


def _number(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reading(pools, lists, config) -> dict:
    # The console's own pool (`pgbouncer` database) is this probe's session,
    # never a mesh client: it must not count as load.
    rows = [row for row in pools if row.get('database') != 'pgbouncer']
    return {
        'waiting': _total(rows, 'cl_waiting'),
        'maxwait_s': max((int(row.get('maxwait') or 0) for row in rows), default=0),
        'clients_active': _total(rows, 'cl_active'),
        'servers_active': _total(rows, 'sv_active'),
        'servers_idle': _total(rows, 'sv_idle'),
        # `free_clients` counts client objects kept for reuse, not free
        # capacity: the ceiling of connected clients is `max_client_conn`
        # (`SHOW CONFIG`), the only honest denominator here.
        'clients_used': _number(lists.get('used_clients')),
        'clients_max': _number(config.get('max_client_conn')),
    }


def read(dsn) -> dict:
    """SHOW POOLS, SHOW LISTS and SHOW CONFIG of the local console, aggregated.

    Raises `db.Unavailable` when the console cannot be read: an unobserved
    pool is not a healthy pool."""
    from .db import Unavailable
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - production installs psycopg
        raise Unavailable('pgbouncer admin console: psycopg missing') from exc
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S,
                             row_factory=dict_row) as conn:
            pools = conn.execute('SHOW POOLS').fetchall()
            lists = {str(row['list']): row.get('items')
                     for row in conn.execute('SHOW LISTS').fetchall()}
            config = {str(row['key']): row.get('value')
                      for row in conn.execute('SHOW CONFIG').fetchall()}
        return _reading(pools, lists, config)
    except Exception as exc:  # psycopg errors, and an ill-typed console answer
        raise Unavailable('pgbouncer admin console: %s' % _one_line(exc)) from exc


def pressured(reading: dict) -> bool:
    """A client really queued: `maxwait` is whole seconds, so a queue that
    clears within the second it formed is contention, not saturation."""
    return reading['waiting'] > 0 and reading['maxwait_s'] >= WAIT_THRESHOLD_S


def line(reading: dict) -> str:
    slots = ('%s/%s client slots used'
             % (reading['clients_used'], reading['clients_max'])
             if reading['clients_used'] is not None and reading['clients_max'] is not None
             else 'client slots unknown')
    return ('local PgBouncer: %d client(s) waiting, oldest %ds (warning at %ds); '
            '%d active client(s), %d/%d server session(s) busy, %s' % (
                reading['waiting'], reading['maxwait_s'], WAIT_THRESHOLD_S,
                reading['clients_active'], reading['servers_active'],
                reading['servers_active'] + reading['servers_idle'], slots))


class _ConsoleProbe:
    """One blocking console read, off the caller's thread (#2582).

    libpq can block forever on a Unix-domain socket (`connect_timeout` is
    ignored there) and the console's SHOW commands have no statement timeout.
    Running the read off-thread lets `probe` abandon it after
    `PROBE_TIMEOUT_S`; a daemon thread never keeps the process alive, and
    SIGINT stays handled by the waiting caller."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.done = threading.Event()
        self.value: dict | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name='pgbouncer-console',
                                       daemon=True)

    def _run(self) -> None:
        try:
            self.value = read(self.dsn)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            self.error = exc
        finally:
            self.done.set()


#: the one probe in flight. A console that never answers must not add one
#: thread per pass (`alerts --follow`, the notifier's local pass): the next
#: pass waits on the same read, then starts a fresh one once it has ended.
_PROBE_LOCK = threading.Lock()
_PROBE: _ConsoleProbe | None = None


def _read_bounded(dsn: str) -> dict:
    """`read(dsn)` bounded by `PROBE_TIMEOUT_S` (#2582).

    A console that does not answer raises `db.Unavailable`, the same failure
    as any unreadable console: the alert pass keeps its other families and
    reports this one as `pgbouncer_console_unreadable` (#1179)."""
    global _PROBE
    with _PROBE_LOCK:
        probe = _PROBE
        if probe is None or probe.done.is_set() or probe.dsn != dsn:
            probe = _PROBE = _ConsoleProbe(dsn)
            probe.thread.start()
    if not probe.done.wait(PROBE_TIMEOUT_S):
        from .db import Unavailable
        raise Unavailable('pgbouncer admin console: no answer within %gs'
                          % PROBE_TIMEOUT_S)
    if probe.error is not None:
        raise probe.error
    return probe.value


def probe(cfg) -> list[dict]:
    """The local pool warning read from `cfg` alone, at most one.

    No mesh database, no storage read: the console's own DSN is derived from
    the configuration. Raises `db.Unavailable` when the console cannot be
    read — including when it does not answer within `PROBE_TIMEOUT_S`
    (#2582) — so the caller keeps an active warning instead of resolving it
    unobserved."""
    dsn = admin_dsn(cfg)
    if dsn is None:
        return []
    reading = _read_bounded(dsn)
    if not pressured(reading):
        return []
    return [dict(schema='ameesh-alert/1', type='pgbouncer_pool_waiting', agent=None,
                 since=None, value=reading['waiting'], threshold=WAIT_THRESHOLD_S,
                 host=getattr(cfg, 'host', None), maxwait_s=reading['maxwait_s'],
                 clients_active=reading['clients_active'],
                 servers_active=reading['servers_active'],
                 servers_idle=reading['servers_idle'],
                 clients_used=reading['clients_used'], clients_max=reading['clients_max'],
                 detail=line(reading))]


def alerts(db) -> list[dict]:
    cfg = getattr(db, 'cfg', None)
    if cfg is None:
        return []
    return probe(cfg)
