#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Operator-only PgBouncer recipe for agents-1/agents-2 (#653).

No remote execution, no runner restart. Run with the installed Ameesh Python.
Errors deliberately omit exception text: libpq errors can contain a DSN.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pwd
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
import uuid
from urllib.request import urlopen

VERSION = '1.26.0'
SHA256 = 'afd25dd61ee6775d37b40629b87ce08736b3e6955f3057bb212e410fbf21c71d'
URL = f'https://www.pgbouncer.org/downloads/files/{VERSION}/pgbouncer-{VERSION}.tar.gz'
APPROVED_OPERATORS = {
    'ameesh-agents-1': 'ameesh',
    'ameesh-agents-2': 'ameesh',
    'ameesh-agents-3': 'ameesh',
}
#: Pool sizing (#532, #6054). Session mode holds one server session per client
#: session, so a host of runners needs one client slot and one server slot per
#: resting or working session, plus the CLI bursts. The original 100 clients /
#: 32+8 servers / 40 total saturated on 2026-10-13 (40 active, 60 waiting,
#: then `no more connections allowed`): the fallback below keeps the raised
#: 2026-10-13 values for a host not named in `POOL_LIMITS`.
#: `default_pool_size + reserve_pool_size` must not exceed
#: `max_db_connections`. #6054 sizes each host on the peak measured by #5983
#: on 2026-11-06: 4.6 concurrent clients per persona (agents-2: 92 clients
#: for 20 personas), one server session per active client in session mode.
#: The pre-switch control is the host card's **admission capacity**, not the
#: installed runner count: `max_agents * 4.6` must fit `max_db_connections`.
#: agents-1 (19) -> 87.4 -> 88 = 72+16; agents-2 (20) -> 92 -> 96 = 80+16;
#: agents-3 (10) -> 46 -> 48 = 40+8. pc-smichea carries no execution agent
#: since 2026-10-28 (decision #133454 a, lot #3463) and its canon
#: `max_agents` 27 is stale: its provisional pool is 24+8 = 32, pending the
#: bureau's measured peak, and a companion canon lot lowers `max_agents` to
#: what the pool carries. The total is 88+96+48+32 = 264 of the 297 ordinary
#: slots (max_connections 300, 3 superuser reserved), leaving 33 for direct
#: clients. Before a switch, run `check --max-agents N` with the canon value.
MAX_CLIENT_CONN = 250
DEFAULT_POOL_SIZE = 64
RESERVE_POOL_SIZE = 16
MAX_DB_CONNECTIONS = 80
#: Peak concurrent clients per persona measured by #5983 on 2026-11-06
#: (agents-2: 92 clients / 20 personas). In session mode each active client
#: holds one server session, so this also sizes the server pool.
PEAK_CLIENTS_PER_PERSONA = 4.6
# Named runner hosts, including the workstation whose pool is installed separately.
POOL_LIMITS = {
    'pc-smichea': (24, 8, 32),
    'ameesh-agents-1': (72, 16, 88),
    'ameesh-agents-2': (80, 16, 96),
    'ameesh-agents-3': (40, 8, 48),
}


def capacity(max_agents, host=None):
    """Server sessions the canonical admission ceiling needs at peak (#6054)."""
    if isinstance(max_agents, bool) or not isinstance(max_agents, int) or max_agents < 1:
        raise ValueError('max_agents must be a positive integer')
    host = host or socket.gethostname()
    default, reserve, ceiling = POOL_LIMITS.get(
        host, (DEFAULT_POOL_SIZE, RESERVE_POOL_SIZE, MAX_DB_CONNECTIONS))
    needed = math.ceil(max_agents * PEAK_CLIENTS_PER_PERSONA)
    return dict(host=host, max_agents=max_agents, needed=needed,
                default_pool_size=default, reserve_pool_size=reserve,
                max_db_connections=ceiling, ok=needed <= ceiling,
                max_agents_covered=math.floor(ceiling / PEAK_CLIENTS_PER_PERSONA))


def check(max_agents, host=None):
    """Print the peak verdict and return 0 when the pool covers it (#6054)."""
    result = capacity(max_agents, host)
    print('%s: max_agents %d x %.1f = %d server session(s) at peak; '
          'pool %d+%d = %d: %s (covers up to %d max_agents)'
          % (result['host'], result['max_agents'], PEAK_CLIENTS_PER_PERSONA,
             result['needed'], result['default_pool_size'],
             result['reserve_pool_size'], result['max_db_connections'],
             'OK' if result['ok'] else 'KO, resize before switching',
             result['max_agents_covered']))
    return 0 if result['ok'] else 1


def approved_home():
    # Check the operating-system identity, not an environment variable or a
    # configurable mesh host label. Root must invoke the reviewed recipe as
    # the runner's user; it cannot silently install into /root.
    uid = os.getuid()
    if uid == 0 or os.geteuid() != uid:
        raise ValueError('run as the approved non-root runner user')
    account = pwd.getpwuid(uid)
    if APPROVED_OPERATORS.get(socket.gethostname()) != account.pw_name:
        raise ValueError('host/user pair is not approved')
    home = Path(account.pw_dir).resolve()
    if Path.home().resolve() != home:
        raise ValueError('HOME differs from the approved user home')
    return home


def private_dir(path):
    if path.is_symlink():
        raise ValueError('symlink directory')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('directory must be owned by operator and private')


def write_new(path, content, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise


def replace(path, content):
    fd, temp = tempfile.mkstemp(prefix='.pgbouncer-653-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def run(argv):
    subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def compile_binary(home, create):
    # A private on-disk TMPDIR is supplied by the operator, never a hardcoded
    # shared memory path. Source download is capped at 32 MiB.
    root = Path(os.environ['TMPDIR'])
    private_dir(root)
    with tempfile.TemporaryDirectory(prefix='pgbouncer-653-', dir=root) as tmp:
        archive = Path(tmp) / 'source.tar.gz'
        with urlopen(URL, timeout=60) as response:
            raw = response.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != SHA256:
            raise ValueError('archive checksum mismatch')
        archive.write_bytes(raw)
        with tarfile.open(archive) as source:
            source.extractall(tmp, filter='data')
        source_dir = Path(tmp) / f'pgbouncer-{VERSION}'
        # cwd is explicit: no path or shell text derived from credentials.
        subprocess.run(['ameesh', 'heavy', '--', 'sh', '-c',
                        './configure --prefix="$1" --with-openssl && make -j2 pgbouncer',
                        'pgbouncer-build', str(home / '.local')], cwd=source_dir,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        destination = home / '.local/bin/pgbouncer'
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Record ownership before TemporaryDirectory cleanup can raise.
        create(destination, (source_dir / 'pgbouncer').read_bytes(), 0o755)


def userlist_quote(value):
    if '\n' in value or '\r' in value or '\x00' in value:
        raise ValueError('invalid userlist value')
    # auth_file follows PgBouncer's double-quote escaping, not shell/libpq.
    return '"' + value.replace('"', '""') + '"'


def socket_conninfo(dsn, socket_dir):
    import psycopg
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    # Upstream TLS negotiation/certificates/binding do not apply to the Unix
    # connection. In particular direct negotiation is invalid with TLS off.
    for key in list(params):
        if key.startswith('ssl') or key in ('hostaddr', 'channel_binding'):
            del params[key]
    # Explicitly override PGSSLNEGOTIATION and libpq service defaults, too.
    params.update(host=str(socket_dir), port='6432', sslmode='disable', password='')
    # libpq <= 16 has neither this option nor direct TLS negotiation.
    if psycopg.pq.version() >= 170000:
        params['sslnegotiation'] = 'postgres'
    return psycopg.conninfo.make_conninfo(**params)


# #3745: every `-c` setting ameesh sends in its startup packet
# (`db.startup_options`) must be tracked here, or pgbouncer refuses the session
# ("unsupported startup parameter in options"). `default_transaction_read_only`
# is tracked by default. This setting is CF_NO_RELOAD in 1.26: a HUP leaves it
# unchanged, only a restart applies a new value.
TRACK_EXTRA_PARAMETERS = 'search_path,statement_timeout,jit'


def pool_ini(dbname, upstream, port, *, socket_dir, auth_file, admin_user, tls, host=None):
    """The reviewed [pgbouncer] configuration, sizing included (#532)."""
    default_pool, reserve_pool, max_db = POOL_LIMITS.get(
        host or socket.gethostname(),
        (DEFAULT_POOL_SIZE, RESERVE_POOL_SIZE, MAX_DB_CONNECTIONS))
    return f'''[databases]
{dbname} = host={upstream} port={port} dbname={dbname}

[pgbouncer]
listen_addr =
listen_port = 6432
unix_socket_dir = {socket_dir}
unix_socket_mode = 0600
auth_type = trust
auth_file = {auth_file}
admin_users = {admin_user}
pool_mode = session
server_reset_query = DISCARD ALL
server_fast_close = 1
server_tls_sslmode = {tls}
server_idle_timeout = 30
server_lifetime = 3600
max_client_conn = {MAX_CLIENT_CONN}
default_pool_size = {default_pool}
reserve_pool_size = {reserve_pool}
reserve_pool_timeout = 5
max_db_connections = {max_db}
track_extra_parameters = {TRACK_EXTRA_PARAMETERS}
log_connections = 0
log_disconnections = 0
log_stats = 0
'''


def install(home, state, config):
    binary = home / '.local/bin/pgbouncer'
    # Refuse even a symlink or dangling symlink before running it or creating
    # any installation file. --version is not proof of binary provenance.
    if os.path.lexists(binary):
        raise ValueError('preexisting binary has no verified provenance; refusing reuse')
    import psycopg
    from ameesh.config import load
    cfg = load()
    if cfg.backend == 'mediated':
        raise ValueError('mediated host')
    configured = json.loads((home / '.config/ameesh/config.json').read_text())
    if configured.get('dsn') != cfg.dsn:
        raise ValueError('DSN override differs from config; unset overrides first')
    settings = home / '.config/pgbouncer'
    unit = home / '.config/systemd/user/ameesh-pgbouncer.service'
    artifacts = (config, settings / 'userlist.txt', state / 'socket.dsn', unit)
    if any(os.path.lexists(path) for path in artifacts):
        raise ValueError('existing installation; refusing overwrite')
    private_dir(settings)
    private_dir(state / 'socket')
    # Resolve defaults and .pgpass in libpq, without writing secrets to argv.
    with psycopg.connect(cfg.dsn, connect_timeout=10) as conn:
        # get_parameters() omits values equal to libpq defaults (port 5432,
        # user or dbname from the environment): read the resolved ones.
        params = conn.info.get_parameters()
        dbname, user = conn.info.dbname, conn.info.user
        upstream, port = conn.info.host, str(conn.info.port)
        password = conn.pgconn.password.decode()
        if not conn.pgconn.ssl_in_use:
            raise ValueError('upstream must use TLS')
    if not all(re.fullmatch(r'[A-Za-z0-9_.:-]+', value)
               for value in (dbname, user, upstream, port)):
        raise ValueError('unsupported upstream identifiers')
    if not password:
        raise ValueError('upstream password unavailable')
    # Require TLS; preserve certificate verification if the original DSN
    # requested it. Certificate paths are unsupported until explicitly reviewed.
    tls = params.get('sslmode', 'require')
    if tls not in ('require', 'verify-ca', 'verify-full'):
        tls = 'require'
    if any(params.get(key) for key in ('sslrootcert', 'sslcert', 'sslkey')):
        raise ValueError('custom TLS files require a reviewed configuration')
    # Validate libpq options and secrets before writing any install artifact.
    socket_dsn = socket_conninfo(cfg.dsn, state / 'socket')
    credentials = userlist_quote(user) + ' ' + userlist_quote(password) + '\n'
    created = []
    start_attempted = False

    def create(path, content, mode=0o600):
        write_new(path, content, mode)
        created.append(path)

    try:
        compile_binary(home, create)
        # Only the binary just built from the pinned archive may be executed.
        result = subprocess.run([str(binary), '--version'],
                                capture_output=True, text=True, check=True)
        if f'PgBouncer {VERSION}' not in result.stdout:
            raise ValueError('unexpected binary version')
        create(settings / 'userlist.txt', credentials)
        create(config, pool_ini(dbname, upstream, port, socket_dir=state / 'socket',
                                auth_file=settings / 'userlist.txt', admin_user=user,
                                tls=tls))
        # Socket DSN is persisted only in a private file, not printed.
        create(state / 'socket.dsn', socket_dsn)
        unit.parent.mkdir(parents=True, exist_ok=True)
        create(unit, f'''[Unit]
Description=Local PgBouncer for Ameesh (#653)
After=network.target

[Service]
Type=simple
ExecStart={home / '.local/bin/pgbouncer'} {config}
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=default.target
''')
        run(['systemd-analyze', '--user', 'verify', str(unit)])
        run(['systemctl', '--user', 'daemon-reload'])
        start_attempted = True
        run(['systemctl', '--user', 'start', 'ameesh-pgbouncer.service'])
    except BaseException:
        # A failed start may still have spawned a process. Stop it before
        # deleting its files; if stop fails, retain them for operator diagnosis.
        if start_attempted:
            run(['systemctl', '--user', 'stop', 'ameesh-pgbouncer.service'])
        for path in reversed(created):
            path.unlink()
        if unit in created:
            run(['systemctl', '--user', 'daemon-reload'])
        raise
    print('Installed and started; Ameesh configuration unchanged; service not enabled.')


# #6862 (EP8.5) : PgBouncer has no `target_session_attrs`; a comma-separated
# `host=` list is used round-robin, standbys included. The pool's upstream is
# therefore one host, the primary, and `amont` follows it: libpq resolves the
# read-write host of the multi-host DSN, and the [databases] line is rewritten
# and reloaded only when it differs. RELOAD marks the old servers
# close_needed; `server_fast_close = 1` closes them at the end of their
# transaction (session mode), so the clients reconnect to the new primary.
AMONT_DSN = 'amont.dsn'
_IDENT = re.compile(r'[A-Za-z0-9_.:-]+')


def database_line(text, dbname):
    """(index, host, port) of the pool's `[databases]` entry for `dbname`."""
    lines = text.split('\n')
    section = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('['):
            section = stripped
            continue
        if section != '[databases]' or '=' not in stripped:
            continue
        name, _, value = stripped.partition('=')
        if name.strip() != dbname:
            continue
        fields = dict(part.split('=', 1) for part in value.split() if '=' in part)
        return index, fields.get('host'), fields.get('port', '5432')
    raise ValueError('pool has no [databases] entry for the database')


def with_upstream(text, dbname, host, port):
    index, _host, _port = database_line(text, dbname)
    lines = text.split('\n')
    lines[index] = f'{dbname} = host={host} port={port} dbname={dbname}'
    return '\n'.join(lines)


def primary_of(dsn, connect=None):
    """(host, port, dbname) of the read-write server the multi-host DSN reaches."""
    import psycopg
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    if params.get('password'):
        raise ValueError('upstream DSN carries a password; use ~/.pgpass')
    if ',' not in params.get('host', ''):
        raise ValueError('upstream DSN is not multi-host')
    params['target_session_attrs'] = 'read-write'
    params.setdefault('connect_timeout', '10')
    connect = connect or psycopg.connect
    with connect(psycopg.conninfo.make_conninfo(**params), autocommit=True) as conn:
        if conn.execute('SELECT pg_is_in_recovery()').fetchone()[0]:
            raise ValueError('reached server is in recovery')
        host, port, dbname = conn.info.host, str(conn.info.port), conn.info.dbname
    if host not in params['host'].split(','):
        raise ValueError('reached host is not in the DSN host list')
    if not all(_IDENT.fullmatch(value) for value in (host, port, dbname)):
        raise ValueError('unsupported upstream identifiers')
    return host, port, dbname


def amont(home, state, *, connect=None, reload=None, dry_run=False):
    """Point the pool at the current primary; return 'unchanged' or 'changed'."""
    settings = home / '.config/pgbouncer'
    config = settings / 'pgbouncer.ini'
    source = settings / AMONT_DSN
    for path in (config, source):
        if path.is_symlink():
            raise ValueError('symlink configuration')
    if source.stat().st_mode & 0o077:
        raise ValueError('upstream DSN file must be private (0600)')
    host, port, dbname = primary_of(source.read_text().strip(), connect)
    text = config.read_text()
    _index, current_host, current_port = database_line(text, dbname)
    if (current_host, current_port) == (host, port):
        print(f'Upstream already the primary {host}:{port}.')
        return 'unchanged'
    print(f'Upstream {current_host}:{current_port} -> primary {host}:{port}'
          + (' (dry run)' if dry_run else '') + '.')
    if dry_run:
        return 'changed'
    backup = config.with_name('pgbouncer.ini.avant-amont')
    if backup.is_symlink():
        raise ValueError('symlink configuration')
    replace(backup, text.encode())
    os.chmod(backup, 0o600)
    replace(config, with_upstream(text, dbname, host, port).encode())
    os.chmod(config, 0o600)
    (reload or console_reload)(state)
    return 'changed'


def console_reload(state):
    import psycopg
    admin_dsn = psycopg.conninfo.make_conninfo(
        (state / 'socket.dsn').read_text().strip(), dbname='pgbouncer', options='')
    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10) as admin:
        admin.execute('RELOAD', prepare=False)


AMONT_SERVICE = '''[Unit]
Description=Local PgBouncer upstream follows the database primary (#6862)
After=ameesh-pgbouncer.service

[Service]
Type=oneshot
ExecStart={python} {script} amont
TimeoutStartSec=30
UMask=0077
NoNewPrivileges=true
'''
AMONT_TIMER = '''[Unit]
Description=Local PgBouncer upstream follows the database primary (#6862)

[Timer]
OnBootSec=1min
OnUnitActiveSec=15s
AccuracySec=1s

[Install]
WantedBy=timers.target
'''


def install_amont(home, python, script):
    """Write and start the user timer that runs `amont` every 15 s."""
    if not (home / '.config/pgbouncer' / AMONT_DSN).is_file():
        raise ValueError('upstream DSN file missing')
    units = home / '.config/systemd/user'
    service = units / 'ameesh-pgbouncer-amont.service'
    timer = units / 'ameesh-pgbouncer-amont.timer'
    if any(os.path.lexists(path) for path in (service, timer)):
        raise ValueError('existing installation; refusing overwrite')
    units.mkdir(parents=True, exist_ok=True)
    write_new(service, AMONT_SERVICE.format(python=python, script=script), 0o644)
    write_new(timer, AMONT_TIMER, 0o644)
    run(['systemd-analyze', '--user', 'verify', str(service)])
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'enable', '--now', 'ameesh-pgbouncer-amont.timer'])
    print('Upstream follower timer enabled.')


def switch(home, state, rollback=False):
    config = home / '.config/ameesh/config.json'
    backup = config.with_name('config.json.before-pgbouncer-653')
    if config.is_symlink() or backup.is_symlink():
        raise ValueError('symlink config')
    if rollback:
        raw = backup.read_bytes()
        json.loads(raw)
    else:
        raw = config.read_bytes()
        data = json.loads(raw)
        dsn = (state / 'socket.dsn').read_text().strip()
        if data.get('dsn') == dsn:
            raise ValueError('already switched')
        write_new(backup, raw)
        data['dsn'] = dsn
        raw = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    replace(config, raw)
    print('Configuration restored.' if rollback else 'Configuration switched.')
    print('Existing runners need operator-managed drain/restart to adopt the DSN.')


def measure(state, label):
    from ameesh.config import load
    from ameesh.db import connect
    cfg = load()
    db = connect(cfg)
    try:
        capacity = db.query("""SELECT current_setting('max_connections')::int AS maximum,
            count(*)::int AS used FROM pg_stat_activity
            WHERE backend_type='client backend'""")[0]
        hosts = db.query("""SELECT coalesce(client_addr::text, 'local') AS address,
            state, count(*)::int AS connections FROM pg_stat_activity
            WHERE backend_type='client backend' GROUP BY 1,2 ORDER BY 1,2""")
    finally:
        db.close()
    import psutil
    runners = []
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            argv = process.info['cmdline'] or []
            # Match the runner entrypoints, not child mail/work CLI processes.
            is_runner = any(Path(arg).name == 'agent-runner' for arg in argv[:2]) or \
                (len(argv) > 1 and Path(argv[0]).name == 'ameesh' and argv[1] == 'run') or \
                (len(argv) > 2 and Path(argv[1]).name == 'ameesh' and argv[2] == 'run')
            if not is_runner:
                continue
            connections = process.net_connections(kind='inet')
            runners.append(dict(pid=process.pid, direct_connections=sum(
                bool(c.raddr and c.raddr.port == 5432) for c in connections)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    pool = {'status': 'not_installed'}
    if (state / 'socket.dsn').exists() and label != 'rollback':
        import psycopg
        from psycopg.rows import dict_row
        admin_dsn = psycopg.conninfo.make_conninfo(
            (state / 'socket.dsn').read_text().strip(), dbname='pgbouncer', options='')
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10,
                             row_factory=dict_row) as admin:
            clients = admin.execute('SHOW CLIENTS', prepare=False).fetchall()
            servers = admin.execute('SHOW SERVERS', prepare=False).fetchall()
        # Unix peer PID identifies a runner without logging application names,
        # arbitrary query text or full admin-console rows.
        for runner in runners:
            runner['pooled_connections'] = sum(
                int(c['remote_pid'] or 0) == runner['pid'] and c['database'] != 'pgbouncer'
                for c in clients)
            runner['total_connections'] = runner['direct_connections'] + \
                runner['pooled_connections']
        pool = dict(status='ok', server_connections=len(servers),
                    unassigned_servers=sum(not s['link'] for s in servers),
                    clients=sum(c['database'] != 'pgbouncer' for c in clients))
    elif label == 'after':
        raise ValueError('after measurement requires a running pool')
    payload = dict(at=datetime.now(timezone.utc).isoformat(), host=socket.gethostname(),
                   label=label, capacity=capacity, by_address=hosts,
                   local_runners=runners, runner_count=len(runners),
                   resting_connection_target=2 * len(runners), pool=pool)
    write_new(state / f'measure-{label}.json', json.dumps(payload, indent=2) + '\n')
    print(json.dumps(payload))


def verify(state):
    import psycopg
    dsn = (state / 'socket.dsn').read_text().strip()
    channel = 'pgbouncer_653_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10,
                         options='-c search_path=pg_catalog -c statement_timeout=12345 '
                                 '-c default_transaction_read_only=on') as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('search_path'), "
                        "current_setting('statement_timeout'), "
                        "current_setting('default_transaction_read_only')")
            if cur.fetchone() != ('pg_catalog', '12345ms', 'on'):
                raise ValueError('startup settings not preserved')
            cur.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')
            if cur.fetchone() != (True,):
                raise ValueError('server connection is not TLS')
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as listener, \
            psycopg.connect(dsn, autocommit=True, connect_timeout=10) as sender:
        listener.execute('LISTEN ' + channel)
        sender.execute('SELECT pg_notify(%s,%s)', (channel, 'probe'))
        received = list(listener.notifies(timeout=5, stop_after=1))
        if len(received) != 1 or received[0].payload != 'probe':
            raise ValueError('LISTEN/NOTIFY failed')
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10,
                         options='-c search_path=public -c statement_timeout=23456 '
                                 '-c default_transaction_read_only=off') as conn:
        row = conn.execute("SELECT current_setting('search_path'), "
                           "current_setting('statement_timeout'), "
                           "current_setting('default_transaction_read_only')").fetchone()
        if row != ('public', '23456ms', 'off'):
            raise ValueError('second client startup settings not preserved')
    print('Verified: server TLS, startup settings, LISTEN/NOTIFY.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'verify', 'switch', 'rollback',
                                           'measure', 'check', 'amont', 'install-amont'])
    parser.add_argument('--dry-run', action='store_true',
                        help='amont: say what would change, write nothing')
    parser.add_argument('--label', choices=['before', 'after', 'rollback'], default='before')
    parser.add_argument('--max-agents', type=int,
                        help='canonical max_agents of the host card, for check')
    args = parser.parse_args()
    home = approved_home()
    if args.action == 'check':
        if args.max_agents is None:
            parser.error('check requires --max-agents N')
        return check(args.max_agents, socket.gethostname())
    # Plain reviewed paths are required by PgBouncer and systemd unit syntax.
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(home)):
        raise ValueError('unsupported home path')
    state = home / '.local/state/pgbouncer'
    private_dir(state)
    if args.action == 'install':
        install(home, state, home / '.config/pgbouncer/pgbouncer.ini')
    elif args.action == 'measure':
        measure(state, args.label)
    elif args.action == 'verify':
        verify(state)
    elif args.action == 'amont':
        amont(home, state, dry_run=args.dry_run)
    elif args.action == 'install-amont':
        install_amont(home, os.path.realpath(sys.executable), os.path.realpath(__file__))
    else:
        switch(home, state, rollback=args.action == 'rollback')


if __name__ == '__main__':
    try:
        code = main()
    except Exception as error:
        print('Operation failed (%s); no exception text or DSN logged. Inspect privately.' %
              type(error).__name__)
        raise SystemExit(1)
    raise SystemExit(code or 0)
