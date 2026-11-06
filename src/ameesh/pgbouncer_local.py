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

#3745: PgBouncer refuses a session whose startup `options` carry a `-c`
setting it does not track ("unsupported startup parameter in options"). The
`-c jit=off` of #3503 was refused that way on pc-smichea, whose pool predates
it. `startup_lines` compares the settings an ameesh session really sends with
the *running* pool's tracked set (`SHOW CONFIG`, not the file:
`track_extra_parameters` is not reloaded by a HUP), so the next setting added
to `db.startup_options` is named by `doctor` instead of failing every runner.

#6054: the same console sizes the warning on the *running* pool, not on fixed
thresholds. `max_db_connections` and `reserve_pool_timeout` come from
`SHOW CONFIG`; `SHOW SERVERS` counts the server sessions actually open. A queue
is saturation once its oldest client has waited at least `reserve_pool_timeout`
(the reserve pool's own deadline), and the pool is at its ceiling once every
server session `max_db_connections` allows is open. The previous one-second
threshold was stricter than the design (#5983).
"""
from __future__ import annotations

import os
import re
import threading

from pathlib import Path

CONFIG_PATH = '.config/pgbouncer/pgbouncer.ini'
#: default `reserve_pool_timeout` when `SHOW CONFIG` does not name it: a client
#: queued longer than this is past the reserve pool's own deadline (#6054).
RESERVE_POOL_TIMEOUT_S = 5.0
#: kept as the fallback for callers that pass a reading without the running
#: value; `probe` uses `SHOW CONFIG`'s `reserve_pool_timeout` when readable.
WAIT_THRESHOLD_S = RESERVE_POOL_TIMEOUT_S
CONNECT_TIMEOUT_S = 5
#: #2582: hard deadline of one console probe (connection and the four SHOW
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
#: #3745: the parameters PgBouncer 1.26 always tracks (`init_var_lookup`,
#: src/varcache.c), lower-cased: its lookup ignores case.
TRACKED_BY_DEFAULT = frozenset((
    'datestyle', 'client_encoding', 'timezone', 'standard_conforming_strings',
    'application_name', 'default_transaction_read_only', 'intervalstyle',
    'search_path', 'scram_iterations', 'session_authorization'))
#: a `-c name=value` or `--name=value` setting of a startup `options` string
_OPTION = re.compile(r'(?:^|\s)(?:-c\s*|--)([^\s=]+)=')


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


#: a `SHOW CONFIG` duration in seconds, milliseconds or microseconds
_DURATION = re.compile(r'(\d+(?:\.\d+)?)\s*(us|ms|s)?', re.IGNORECASE)


def _seconds(value, default: float) -> float:
    match = _DURATION.fullmatch(str(value).strip()) if value is not None else None
    if match is None:
        return default
    number = float(match.group(1))
    unit = (match.group(2) or 's').lower()
    if unit == 'us':
        return number / 1_000_000
    if unit == 'ms':
        return number / 1_000
    return number


def _reading(pools, lists, config, servers=()) -> dict:
    # The console's own pool (`pgbouncer` database) is this probe's session,
    # never a mesh client: it must not count as load.
    rows = [row for row in pools if row.get('database') != 'pgbouncer']
    # `SHOW SERVERS` counts every server connection the pool holds, idle ones
    # included: they still occupy a `max_db_connections` slot. The local
    # recipe serves one database, so the aggregate is the ceiling comparison;
    # a multi-database pool would need one count per database.
    return {
        'waiting': _total(rows, 'cl_waiting'),
        'maxwait_s': max((int(row.get('maxwait') or 0) for row in rows), default=0),
        'clients_active': _total(rows, 'cl_active'),
        'servers_active': _total(rows, 'sv_active'),
        'servers_idle': _total(rows, 'sv_idle'),
        'servers_total': sum(row.get('database') != 'pgbouncer' for row in servers),
        # #6054: the running pool's own ceiling and reserve deadline
        'max_db_connections': _number(config.get('max_db_connections')),
        'reserve_pool_timeout_s': _seconds(config.get('reserve_pool_timeout'),
                                           WAIT_THRESHOLD_S),
        # `free_clients` counts client objects kept for reuse, not free
        # capacity: the ceiling of connected clients is `max_client_conn`
        # (`SHOW CONFIG`), the only honest denominator here.
        'clients_used': _number(lists.get('used_clients')),
        'clients_max': _number(config.get('max_client_conn')),
        # #3745: the running values, which a HUP does not change
        'track_extra_parameters': config.get('track_extra_parameters'),
        'ignore_startup_parameters': config.get('ignore_startup_parameters'),
    }


def read(dsn) -> dict:
    """SHOW POOLS, SHOW LISTS, SHOW CONFIG and SHOW SERVERS, aggregated.

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
            servers = conn.execute('SHOW SERVERS').fetchall()
        return _reading(pools, lists, config, servers)
    except Exception as exc:  # psycopg errors, and an ill-typed console answer
        raise Unavailable('pgbouncer admin console: %s' % _one_line(exc)) from exc


def pressured(reading: dict) -> bool:
    """A client queued past the running pool's `reserve_pool_timeout` (#6054).

    The reserve pool is the pool's first answer to a full ordinary pool; a
    queue that outlives its deadline is saturation, not the normal session-mode
    wait (#5983). Readings without the running value fall back to 5 s."""
    threshold = reading.get('reserve_pool_timeout_s', WAIT_THRESHOLD_S)
    return reading.get('waiting', 0) > 0 and reading.get('maxwait_s', 0) >= threshold


def at_ceiling(reading: dict) -> bool:
    """Every server session `max_db_connections` allows is open (#6054)."""
    ceiling = reading.get('max_db_connections')
    total = reading.get('servers_total')
    return bool(ceiling and ceiling > 0 and total is not None and total >= ceiling)


def line(reading: dict) -> str:
    threshold = reading.get('reserve_pool_timeout_s', WAIT_THRESHOLD_S)
    total = reading.get('servers_total')
    if total is None:
        total = reading.get('servers_active', 0) + reading.get('servers_idle', 0)
    ceiling = reading.get('max_db_connections')
    used, maximum = reading.get('clients_used'), reading.get('clients_max')
    slots = ('%s/%s client slots used' % (used, maximum)
             if used is not None and maximum is not None
             else 'client slots unknown')
    return ('local PgBouncer: %d client(s) waiting, oldest %ds (warning at %gs); '
            '%d active client(s), %d/%s server session(s) used, %s' % (
                reading.get('waiting', 0), reading.get('maxwait_s', 0), threshold,
                reading.get('clients_active', 0), total,
                ceiling if ceiling else '?', slots))


def ceiling_line(reading: dict) -> str:
    return ('local PgBouncer: %d/%d server session(s) at the pool ceiling; '
            '%d client(s) waiting, oldest %ds (reserve_pool_timeout %gs)' % (
                reading['servers_total'], reading['max_db_connections'],
                reading.get('waiting', 0), reading.get('maxwait_s', 0),
                reading.get('reserve_pool_timeout_s', WAIT_THRESHOLD_S)))


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
    unobserved.

    #6054: the ceiling alert takes precedence over the queue alert. Both are
    saturation; the ceiling names the harder fact, and its detail still carries
    the queue. The notifier's pass interval and dedupe make a ceiling reading
    an episode, not a per-tick message."""
    dsn = admin_dsn(cfg)
    if dsn is None:
        return []
    reading = _read_bounded(dsn)
    common = dict(host=getattr(cfg, 'host', None),
                  maxwait_s=reading.get('maxwait_s', 0),
                  waiting=reading.get('waiting', 0),
                  clients_active=reading.get('clients_active', 0),
                  servers_active=reading.get('servers_active', 0),
                  servers_idle=reading.get('servers_idle', 0),
                  servers_total=reading.get('servers_total'),
                  max_db_connections=reading.get('max_db_connections'),
                  reserve_pool_timeout_s=reading.get(
                      'reserve_pool_timeout_s', WAIT_THRESHOLD_S),
                  clients_used=reading.get('clients_used'),
                  clients_max=reading.get('clients_max'))
    if at_ceiling(reading):
        return [dict(schema='ameesh-alert/1', type='pgbouncer_pool_at_ceiling',
                     agent=None, since=None, value=reading['servers_total'],
                     threshold=reading['max_db_connections'],
                     detail=ceiling_line(reading), **common)]
    if not pressured(reading):
        return []
    return [dict(schema='ameesh-alert/1', type='pgbouncer_pool_waiting', agent=None,
                 since=None, value=reading['waiting'],
                 threshold=reading.get('reserve_pool_timeout_s', WAIT_THRESHOLD_S),
                 detail=line(reading), **common)]


def option_names(options: str) -> list[str]:
    """The setting names of a startup `options` string, in order."""
    return _OPTION.findall(options or '')


def _words(value) -> set[str]:
    return {word.strip().lower() for word in str(value or '').split(',') if word.strip()}


def session_options(cfg) -> str:
    """The startup `options` an ameesh session sends, as `db` builds them: the
    DSN's own `options` (else PGOPTIONS), then `db.startup_options`."""
    import psycopg
    from .db import startup_options
    try:
        existing = psycopg.conninfo.conninfo_to_dict(getattr(cfg, 'dsn', '') or '').get('options')
    except Exception:
        existing = None
    if existing is None:
        existing = os.environ.get('PGOPTIONS', '')
    return startup_options(cfg, str(existing or ''))


def startup_problems(options: str, reading: dict) -> list[str]:
    """The settings of `options` this pool would refuse, or accept without
    applying them (`ignore_startup_parameters`): both break an ameesh session."""
    tracked = TRACKED_BY_DEFAULT | _words(reading.get('track_extra_parameters'))
    ignored = _words(reading.get('ignore_startup_parameters'))
    problems = []
    for name in option_names(options):
        key = name.lower()
        if key in tracked:
            continue
        if key in ignored or 'options' in ignored:
            problems.append('%s ignored (never applied to the session)' % name)
        else:
            problems.append('%s refused (unsupported startup parameter)' % name)
    return problems


def startup_lines(cfg) -> tuple[bool, list[str]]:
    """`doctor` (#3745): does the local pool accept the startup settings of an
    ameesh session? `(False, lines)` names what it would refuse.

    Read from `cfg` alone, before any ordinary connection: when the pool
    refuses ameesh, that connection fails and this line is the diagnosis. No
    local pool, or a DSN that does not use it: nothing to check. An unreadable
    console is reported without a verdict (the pool alerts cover it)."""
    dsn = admin_dsn(cfg)
    if dsn is None:
        return True, []
    from .db import Unavailable
    options = session_options(cfg)
    try:
        reading = _read_bounded(dsn)
    except Unavailable as exc:
        return True, ['startup options not checked (%s)' % _one_line(exc)]
    problems = startup_problems(options, reading)
    if not problems:
        return True, ['startup options accepted (%s)' % (', '.join(option_names(options)) or 'none')]
    return False, ['KO — %s: add to track_extra_parameters in ~/%s, then RESTART the pool '
                   '(a HUP does not apply it; deploy/pgbouncer-host.md)'
                   % ('; '.join(problems), CONFIG_PATH)]


def alerts(db) -> list[dict]:
    cfg = getattr(db, 'cfg', None)
    if cfg is None:
        return []
    return probe(cfg)
