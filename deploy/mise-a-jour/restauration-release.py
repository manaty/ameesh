#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Local disposable restore cluster and root capacity producer (#5165).

Administrator CLI only. Installation never touches the live cluster. PostgreSQL
roles/credentials are enrolled separately (#5162); no password reaches argv.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import shutil
import subprocess
import tempfile
import time

NAME = 'ameesh-release-restore'
ACCOUNT = 'ameesh-restore'
PORT = 5432
ADMIN = 'release_restore_admin'
ADMIN_MAP = 'release_admin'
SOCKET = '/run/' + NAME
INSTALL = Path('/opt/ameesh-release/restore')
UNIT_ROOT = Path('/etc/systemd/system')
ENROLLMENT = Path('/etc/ameesh-release/restore-probe.json')
INTENT = Path('/etc/ameesh-release/restore-install.json')


class RestoreError(ValueError):
    pass


def command(args):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=120, env={'PATH': '/usr/bin:/bin',
                                               'LANG': 'C', 'LC_ALL': 'C'}).stdout
    except (OSError, subprocess.SubprocessError):
        # Diagnostics may contain connection strings: never repeat them.
        raise RestoreError('restore maintenance command failed') from None


def protected(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise RestoreError('absolute protected path required')
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise RestoreError('root protected parent required')
    return path


def read_document(path):
    path = protected(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 65536):
            raise RestoreError('private root document required')
        data = stream.read(65537)
    if len(data) > 65536:
        raise RestoreError('document too large')
    return json.loads(data)


def atomic(path, data, mode=0o600):
    path = protected(path)
    if path.is_symlink():
        raise RestoreError('linked output refused')
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def layout(version):
    if not isinstance(version, str) or not re.fullmatch(r'[1-9][0-9]', version):
        raise RestoreError('PostgreSQL major version required')
    return (Path('/var/lib/ameesh-release-restore') / version / 'data',
            Path('/etc/ameesh-release-restore'))


def enrollment(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'backup', 'system_identifier'}
            or not isinstance(value['system_identifier'], str)
            or not re.fullmatch(r'[0-9]{10,30}', value['system_identifier'])):
        raise RestoreError('exact restore probe enrollment required')
    layout(value['version'])
    protected(value['backup'])
    return value


def target(dsn, parser):
    try:
        fields = parser(dsn)
    except Exception:
        raise RestoreError('restore DSN cannot be parsed') from None
    # Explicit single local endpoint; no libpq environment/service/options can
    # redirect the producer to another volume. Password and TLS parameters are
    # allowed, but connection routing is completely pinned here.
    if any(os.environ.get(key) for key in ('PGSERVICE', 'PGSERVICEFILE', 'PGHOSTADDR', 'PGOPTIONS')):
        raise RestoreError('external libpq routing refused')
    allowed = {'host', 'port', 'dbname', 'user', 'password', 'passfile',
               'sslmode', 'connect_timeout', 'application_name', 'require_auth'}
    if (set(fields) - allowed or fields.get('host') != SOCKET
            or 'port' in fields or fields.get('dbname') != 'postgres'
            or fields.get('require_auth') != 'scram-sha-256'
            or not fields.get('user') or fields['user'] in (ACCOUNT, ADMIN)):
        raise RestoreError('restore DSN must address the dedicated local cluster')
    return fields


def local_sql(version, sql):
    return command(['/usr/sbin/runuser', '-u', ACCOUNT, '--',
                    '/usr/lib/postgresql/' + version + '/bin/psql', '-X', '-w',
                    '-h', SOCKET, '-p', str(PORT), '-d', 'postgres',
                    '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-c', sql]).strip()


def inspect_cluster(version):
    data, _ = layout(version)
    result = json.loads(local_sql(version,
        "select json_build_object('system_identifier',"
        " (select system_identifier::text from pg_control_system()),"
        " 'data_directory', current_setting('data_directory'),"
        " 'port', current_setting('port'),"
        " 'listen_addresses', current_setting('listen_addresses'),"
        " 'external_tablespaces', (select count(*) from pg_tablespace"
        " where spcname not in ('pg_default','pg_global')))"))
    if (result.get('data_directory') != str(data) or result.get('port') != str(PORT)
            or result.get('listen_addresses') != ''
            or result.get('external_tablespaces') != 0):
        raise RestoreError('restore cluster layout mismatch')
    info = data.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != pwd.getpwnam(ACCOUNT).pw_uid
            or data.resolve() != data or info.st_mode & 0o077):
        raise RestoreError('private dedicated data directory required')
    return result


def capacity(document, backup, observed, *, parser, free_bytes, now):
    """No output until routing, cluster identity and actual volume agree."""
    config = enrollment(document)
    dsn = backup.get('restore_dsn') if isinstance(backup, dict) else None
    if not isinstance(dsn, str) or not dsn or '\x00' in dsn:
        raise RestoreError('restore DSN required')
    target(dsn, parser)
    data, _ = layout(config['version'])
    if (observed.get('system_identifier') != config['system_identifier']
            or observed.get('data_directory') != str(data)
            or observed.get('port') != str(PORT)
            or observed.get('listen_addresses') != ''
            or observed.get('external_tablespaces') != 0):
        raise RestoreError('dedicated cluster identity mismatch')
    if type(free_bytes) is not int or not 0 <= free_bytes <= 10**15:
        raise RestoreError('invalid target volume capacity')
    return dict(target_sha256=hashlib.sha256(dsn.encode()).hexdigest(),
                free_bytes=free_bytes, checked_at=int(now))


def probe(config_path=ENROLLMENT):
    from psycopg.conninfo import conninfo_to_dict
    config = enrollment(read_document(config_path))
    backup = read_document(config['backup'])
    observed = inspect_cluster(config['version'])
    data, _ = layout(config['version'])
    volume = os.statvfs(data)
    # f_bavail excludes reserved blocks, conservative for the PostgreSQL user.
    value = capacity(config, backup, observed, parser=conninfo_to_dict,
                     free_bytes=volume.f_bavail * volume.f_frsize, now=time.time())
    output = capacity_output(backup, config, config_path)
    atomic(output, (json.dumps(value, sort_keys=True) + '\n').encode())
    return value


def capacity_output(backup, config, config_path=ENROLLMENT):
    """The attestation never replaces an enrollment or configuration document."""
    output = backup.get('restore_capacity')
    if not isinstance(output, str) or '\x00' in output:
        raise RestoreError('capacity output reference required')
    output = protected(output)
    documents = {Path(config_path), Path(config['backup']), ENROLLMENT, INTENT}
    remote = backup.get('remote')
    if isinstance(remote, dict):
        documents.update(Path(value) for value in remote.values()
                         if isinstance(value, str) and value.startswith('/'))
    data, conf = layout(config['version'])
    trees = (ENROLLMENT.parent, INTENT.parent, conf, data, INSTALL, UNIT_ROOT)
    if output in documents or any(output == tree or tree in output.parents for tree in trees):
        raise RestoreError('capacity output must not target enrollment or configuration')
    return output


def units(version, python):
    # Python comes from administrator enrollment, never a runner's environment.
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', python) or '..' in Path(python).parts:
        raise RestoreError('absolute interpreter path required')
    data, conf = layout(version)
    return {
        NAME + '.service': f'''[Unit]
Description=Disposable PostgreSQL restore cluster for release verification
After=local-fs.target
[Service]
Type=forking
PIDFile={data}/postmaster.pid
RuntimeDirectory={NAME}
RuntimeDirectoryMode=0700
User={ACCOUNT}
Group={ACCOUNT}
ExecStart=/usr/lib/postgresql/{version}/bin/pg_ctl -D {data} -l {data}/server.log -o "-c config_file={conf}/postgresql.conf" -w start
ExecStop=/usr/lib/postgresql/{version}/bin/pg_ctl -D {data} -w -m fast stop
TimeoutStartSec=120
TimeoutStopSec=120
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths={data} {SOCKET}
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictSUIDSGID=yes
[Install]
WantedBy=multi-user.target
''',
        NAME + '-capacity.service': f'''[Unit]
Description=Attest free space on the dedicated restore volume
Requires={NAME}.service
After={NAME}.service
[Service]
Type=oneshot
User=root
UMask=0077
ExecStart={python} -I {INSTALL}/restauration-release.py probe
TimeoutStartSec=120
NoNewPrivileges=yes
ProtectHome=yes
PrivateTmp=yes
''',
        NAME + '-capacity.timer': f'''[Unit]
Description=Refresh restore capacity before its five minute expiry
[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=5s
Unit={NAME}-capacity.service
[Install]
WantedBy=timers.target
'''}


def system_identifier(version):
    data, _ = layout(version)
    text = command(['/usr/sbin/runuser', '-u', ACCOUNT, '--',
                    '/usr/lib/postgresql/' + version + '/bin/pg_controldata', str(data)])
    for line in text.splitlines():
        if line.startswith('Database system identifier:'):
            value = line.split(':', 1)[1].strip()
            if re.fullmatch(r'[0-9]{10,30}', value):
                return value
    raise RestoreError('restore cluster identifier unavailable')


def save_document(path, value):
    atomic(path, (json.dumps(value, sort_keys=True) + '\n').encode())


def authentication(restore_role):
    # Restrict identifiers before rendering HBA rules.
    if (not isinstance(restore_role, str)
            or not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', restore_role)
            or restore_role in (ACCOUNT, ADMIN, 'all', 'replication')
            or restore_role.startswith('pg_')):
        raise RestoreError('explicit separate restore role required')
    return (f'local all {ACCOUNT} peer\n'
            f'local all {ADMIN} peer map={ADMIN_MAP}\n'
            f'local all {restore_role} scram-sha-256\n',
            f'{ADMIN_MAP} root {ADMIN}\n')


def install(version, backup_path, python, restore_role):
    data, conf = layout(version)
    rendered = units(version, python)
    hba, ident = authentication(restore_role)
    protected(backup_path)
    if not Path('/usr/lib/postgresql', version, 'bin/initdb').is_file():
        raise RestoreError('PostgreSQL binaries missing')
    ENROLLMENT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    protected(INTENT)
    request = dict(version=version, backup=str(backup_path), python=python,
                   restore_role=restore_role)
    if INTENT.exists():
        intent = read_document(INTENT)
        if (not isinstance(intent, dict) or set(intent) != {'request', 'system_identifier'}
                or intent['request'] != request):
            raise RestoreError('existing restore installation differs')
    else:
        if data.exists() or conf.exists() or ENROLLMENT.exists():
            raise RestoreError('unowned restore cluster exists')
        intent = dict(request=request, system_identifier=None)
        save_document(INTENT, intent)
    try:
        user = pwd.getpwnam(ACCOUNT)
        if user.pw_shell != '/usr/sbin/nologin' or user.pw_dir != '/nonexistent':
            raise RestoreError('dedicated restore account differs')
    except KeyError:
        command(['/usr/sbin/useradd', '--system', '--user-group', '--home-dir',
                 '/nonexistent', '--no-create-home', '--shell', '/usr/sbin/nologin', ACCOUNT])
        user = pwd.getpwnam(ACCOUNT)
    data.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    protected(data)
    if not data.exists():
        if intent['system_identifier'] is not None:
            raise RestoreError('enrolled cluster disappeared')
        data.mkdir(mode=0o700)
        os.chown(data, user.pw_uid, user.pw_gid)
    if data.is_symlink() or data.stat().st_uid != user.pw_uid or data.stat().st_mode & 0o077:
        raise RestoreError('private restore data directory required')
    if not any(data.iterdir()):
        command(['/usr/sbin/runuser', '-u', ACCOUNT, '--',
                 '/usr/lib/postgresql/' + version + '/bin/initdb', '-D', str(data),
                 '--auth-local=peer', '--auth-host=scram-sha-256', '--no-instructions'])
    observed_id = system_identifier(version)
    if intent['system_identifier'] not in (None, observed_id):
        raise RestoreError('restore cluster identity changed')
    intent['system_identifier'] = observed_id
    save_document(INTENT, intent)
    # Config is outside the PostgreSQL-owned tree and cannot be changed by a
    # restored database or a runner. The private socket has no runner access.
    conf.mkdir(mode=0o755, exist_ok=True)
    protected(conf / 'postgresql.conf')
    atomic(conf / 'pg_hba.conf', hba.encode(), 0o644)
    atomic(conf / 'pg_ident.conf', ident.encode(), 0o644)
    atomic(conf / 'postgresql.conf', (
        "listen_addresses = ''\n"
        f"data_directory = '{data}'\nhba_file = '{conf}/pg_hba.conf'\n"
        f"ident_file = '{conf}/pg_ident.conf'\nport = {PORT}\n"
        f"unix_socket_directories = '{SOCKET}'\n"
        "unix_socket_permissions = 0700\npassword_encryption = 'scram-sha-256'\n"
        "max_connections = 10\n").encode(), 0o644)
    INSTALL.mkdir(mode=0o755, parents=True, exist_ok=True)
    atomic(INSTALL / 'restauration-release.py', Path(__file__).read_bytes(), 0o644)
    for name, text in rendered.items():
        atomic(UNIT_ROOT / name, text.encode(), 0o644)
    command(['/usr/bin/systemctl', 'daemon-reload'])
    command(['/usr/bin/systemctl', 'enable', '--now', NAME + '.service'])
    command(['/usr/bin/systemctl', 'restart', NAME + '.service'])
    observed = inspect_cluster(version)
    if observed['system_identifier'] != observed_id:
        raise RestoreError('restore cluster identity changed')
    # Root assumes this identity through the sole explicit pg_ident entry.
    local_sql(version, f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles "
              f"WHERE rolname = '{ADMIN}') THEN CREATE ROLE {ADMIN} LOGIN SUPERUSER; "
              "END IF; END $$;")
    current = dict(version=version, backup=str(backup_path), system_identifier=observed_id)
    if ENROLLMENT.exists() and read_document(ENROLLMENT) != current:
        raise RestoreError('restore enrollment changed')
    save_document(ENROLLMENT, current)
    command(['/usr/bin/systemctl', 'enable', '--now', NAME + '-capacity.timer'])


def remove():
    if not INTENT.exists():
        return  # An already removed installation is a successful no-op.
    intent = read_document(INTENT)
    request = intent['request']
    version = request['version']
    data, conf = layout(version)
    if data.exists():
        if (not intent['system_identifier']
                or system_identifier(version) != intent['system_identifier']):
            raise RestoreError('restore cluster identity changed')
    output = None
    if ENROLLMENT.exists():
        config = enrollment(read_document(ENROLLMENT))
        if config['system_identifier'] != intent['system_identifier']:
            raise RestoreError('restore enrollment differs')
        backup = read_document(config['backup'])
        output = protected(backup['restore_capacity'])
    # Stop the producer before the cluster, including a probe currently running.
    if (UNIT_ROOT / (NAME + '-capacity.timer')).exists():
        command(['/usr/bin/systemctl', 'disable', '--now', NAME + '-capacity.timer'])
    if (UNIT_ROOT / (NAME + '-capacity.service')).exists():
        command(['/usr/bin/systemctl', 'stop', NAME + '-capacity.service'])
    if (UNIT_ROOT / (NAME + '.service')).exists():
        command(['/usr/bin/systemctl', 'disable', '--now', NAME + '.service'])
    if output is not None:
        output.unlink(missing_ok=True)
    if data.exists():
        if data.is_symlink() or data.resolve() != data:
            raise RestoreError('linked restore data refused')
        shutil.rmtree(data)
    if conf.exists():
        protected(conf / 'postgresql.conf')
        shutil.rmtree(conf)
    for name in units(version, request['python']):
        (UNIT_ROOT / name).unlink(missing_ok=True)
    ENROLLMENT.unlink(missing_ok=True)
    (INSTALL / 'restauration-release.py').unlink(missing_ok=True)
    command(['/usr/bin/systemctl', 'daemon-reload'])
    INTENT.unlink()
    # Keep the locked OS identity: deleting/reusing its uid is unsafe. No data,
    # unit or active timer remains. Backup/identity enrollments belong to #5162.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    setup = sub.add_parser('install')
    setup.add_argument('--pg-version', required=True)
    setup.add_argument('--backup', required=True, type=Path)
    setup.add_argument('--python', required=True)
    setup.add_argument('--restore-role', required=True)
    sub.add_parser('probe')
    sub.add_parser('remove')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, 'administrator required\n')
    try:
        if args.action == 'install':
            install(args.pg_version, args.backup, args.python, args.restore_role)
        elif args.action == 'probe':
            probe()
        else:
            remove()
    except Exception:
        parser.exit(1, 'restore cluster operation refused; inspect protected enrollment\n')


if __name__ == '__main__':
    main()
