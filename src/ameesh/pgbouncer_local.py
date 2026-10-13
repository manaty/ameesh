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
"""
from __future__ import annotations

from pathlib import Path

CONFIG_PATH = '.config/pgbouncer/pgbouncer.ini'
#: how long the oldest queued client must have waited before warning (seconds)
WAIT_THRESHOLD_S = 1
CONNECT_TIMEOUT_S = 5
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


def _reading(pools, lists) -> dict:
    # The console's own pool (`pgbouncer` database) is this probe's session,
    # never a mesh client: it must not count as load.
    rows = [row for row in pools if row.get('database') != 'pgbouncer']
    return {
        'waiting': _total(rows, 'cl_waiting'),
        'maxwait_s': max((int(row.get('maxwait') or 0) for row in rows), default=0),
        'clients_active': _total(rows, 'cl_active'),
        'servers_active': _total(rows, 'sv_active'),
        'servers_idle': _total(rows, 'sv_idle'),
        'clients_used': _number(lists.get('used_clients')),
        'clients_free': _number(lists.get('free_clients')),
    }


def read(dsn) -> dict:
    """SHOW POOLS and SHOW LISTS of the local console, aggregated.

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
        return _reading(pools, lists)
    except Exception as exc:  # psycopg errors, and an ill-typed console answer
        raise Unavailable('pgbouncer admin console: %s' % _one_line(exc)) from exc


def pressured(reading: dict) -> bool:
    """A client really queued: `maxwait` is whole seconds, so a queue that
    clears within the second it formed is contention, not saturation."""
    return reading['waiting'] > 0 and reading['maxwait_s'] >= WAIT_THRESHOLD_S


def line(reading: dict) -> str:
    slots = ('%s/%s client slots used' % (reading['clients_used'], reading['clients_free'])
             if reading['clients_used'] is not None and reading['clients_free'] is not None
             else 'client slots unknown')
    return ('local PgBouncer: %d client(s) waiting, oldest %ds (warning at %ds); '
            '%d active client(s), %d/%d server session(s) busy, %s' % (
                reading['waiting'], reading['maxwait_s'], WAIT_THRESHOLD_S,
                reading['clients_active'], reading['servers_active'],
                reading['servers_active'] + reading['servers_idle'], slots))


def alerts(db) -> list[dict]:
    cfg = getattr(db, 'cfg', None)
    if cfg is None:
        return []
    dsn = admin_dsn(cfg)
    if dsn is None:
        return []
    reading = read(dsn)
    if not pressured(reading):
        return []
    return [dict(schema='ameesh-alert/1', type='pgbouncer_pool_waiting', agent=None,
                 since=None, value=reading['waiting'], threshold=WAIT_THRESHOLD_S,
                 host=getattr(cfg, 'host', None), maxwait_s=reading['maxwait_s'],
                 clients_active=reading['clients_active'],
                 servers_active=reading['servers_active'],
                 servers_idle=reading['servers_idle'],
                 clients_used=reading['clients_used'], clients_free=reading['clients_free'],
                 detail=line(reading))]
