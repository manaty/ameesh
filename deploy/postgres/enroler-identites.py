#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Root-only PostgreSQL identity provisioning from a private, explicit source.

No DSN or password is accepted on argv. The source is written durably before
SQL so retries use exactly the same credentials. SQL effects are transactional
per cluster; a lost reply is reconciled by role comments and exact audits.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path('/etc/ameesh-release-identities')
NAME = re.compile(r'[a-z][a-z0-9_]{0,62}\Z')
CONTROLLER_READS = frozenset(('agent_registry', 'agent_mailbox', 'mesh_approvals',
    'mesh_consumed_nonces', 'turn_resources', 'spend_pending', 'host_release_operations',
    'host_release_fences', 'host_release_fence_receipts', 'decision_supersessions',
    'mesh_approvals_status', 'schema_non_additive_migrations', 'mesh_keys'))


class IdentityError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise IdentityError(reason)


def protected(path, directory=False):
    path = Path(path)
    for parent in reversed(path.parents):
        st = parent.lstat()
        require(stat.S_ISDIR(st.st_mode) and not parent.is_symlink()
                and st.st_uid in (0, os.geteuid()) and not st.st_mode & 0o022,
                'untrusted private source parent')
    st = path.lstat()
    require(st.st_uid == os.geteuid() and not path.is_symlink()
            and stat.S_IMODE(st.st_mode) == (0o700 if directory else 0o600)
            and (stat.S_ISDIR(st.st_mode) if directory else
                 stat.S_ISREG(st.st_mode) and st.st_nlink == 1), 'unsafe private source')
    return path


def read(path):
    protected(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r') as stream:
        st = os.fstat(stream.fileno())
        require(st.st_size <= 65536 and st.st_nlink == 1 and st.st_uid == os.geteuid()
                and stat.S_IMODE(st.st_mode) == 0o600 and stat.S_ISREG(st.st_mode),
                'unsafe private source descriptor')
        def unique(pairs):
            value = {}
            for k, v in pairs:
                require(k not in value, 'duplicate source field')
                value[k] = v
            return value
        return json.load(stream, object_pairs_hook=unique)


def save_new(path, value):
    data = (json.dumps(value, sort_keys=True) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate(value):
    require(isinstance(value, dict) and set(value) == {
        'version', 'operation', 'source_owner_dsn', 'restore_owner_dsn', 'schema',
        'controller_role', 'backup_role', 'restore_role', 'group_role'}, 'exact private input required')
    require(value['version'] == 1, 'unsupported source version')
    for key in ('operation', 'schema', 'controller_role', 'backup_role', 'restore_role', 'group_role'):
        require(isinstance(value[key], str) and NAME.fullmatch(value[key]), 'invalid identity name')
    names = [value[k] for k in ('controller_role', 'backup_role', 'restore_role', 'group_role')]
    require(len(set(names)) == 4 and all(not n.startswith('pg_') for n in names), 'distinct private roles required')
    for key in ('source_owner_dsn', 'restore_owner_dsn'):
        connection(value[key])
    return value


def connection(dsn):
    p = conninfo_to_dict(dsn)
    require(set(p) <= {'host', 'hostaddr', 'port', 'dbname', 'user', 'password', 'sslmode',
                       'sslrootcert', 'sslcert', 'sslkey', 'connect_timeout'},
            'explicit flat connection required; no services or options')
    require(all(p.get(k) for k in ('host', 'port', 'dbname', 'user')), 'explicit endpoint required')
    require(not any(c in p['host'] for c in ',\n\r') and ',' not in p['port'], 'single endpoint required')
    p['connect_timeout'] = '10'
    return p


def login_dsn(owner, role, password):
    p = connection(owner)
    p.update(user=role, password=password)
    return make_conninfo(**p)


def session(dsn):
    conn = psycopg.connect(**connection(dsn))
    try:
        # Disable statement/parameter logging before even a SCRAM verifier SQL.
        # No plaintext password is ever sent in an SQL statement.
        conn.execute("SET LOCAL log_statement = 'none'")
        conn.execute("SET LOCAL log_min_error_statement = 'panic'")
        conn.execute('SET LOCAL log_parameter_max_length = 0')
        conn.execute('SET LOCAL log_parameter_max_length_on_error = 0')
        require(conn.execute('SELECT rolsuper FROM pg_roles WHERE rolname=current_user').fetchone()[0],
                'explicit cluster administrator required')
        return conn
    except Exception:
        conn.close()
        raise


def cluster(conn):
    return str(conn.execute('SELECT system_identifier FROM pg_control_system()').fetchone()[0])


def marker(value, kind):
    return 'ameesh-release-identities:' + value['operation'] + ':' + kind


def role(conn, value, kind, password):
    name = value[kind + '_role']
    found = conn.execute('SELECT oid,shobj_description(oid,\'pg_authid\') FROM pg_roles WHERE rolname=%s',
                         (name,)).fetchone()
    if found:
        require(found[1] == marker(value, kind), 'existing identity belongs to another operation')
        return
    conn.execute(sql.SQL('CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEROLE '
                         'NOREPLICATION NOBYPASSRLS {} PASSWORD {}').format(
        sql.Identifier(name), sql.SQL('CREATEDB' if kind == 'restore' else 'NOCREATEDB'),
        sql.Literal(conn.pgconn.encrypt_password(password.encode(), name.encode(), b'scram-sha-256').decode())))
    conn.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(sql.Identifier(name), sql.Literal(marker(value, kind))))


def audit_flags(conn, name, parent=None, createdb=False):
    row = conn.execute('SELECT oid,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,'
                       'rolbypassrls,rolinherit FROM pg_roles WHERE rolname=%s', (name,)).fetchone()
    require(row is not None and row[1] and row[7] and row[2:7] == (False, createdb, False, False, False),
            'identity flags differ from contract')
    parents = conn.execute('SELECT r.rolname,m.admin_option FROM pg_auth_members m '
                           'JOIN pg_roles r ON r.oid=m.roleid WHERE m.member=%s', (row[0],)).fetchall()
    require(parents == ([(parent, False)] if parent else []), 'unexpected role membership')
    # No pre-existing per-role or per-database setting may change effective identity.
    require(not conn.execute('SELECT 1 FROM pg_db_role_setting WHERE setrole=%s', (row[0],)).fetchone(),
            'unexpected identity settings')
    return row[0]


def objects(conn):
    return conn.execute("SELECT c.oid,n.nspname,c.relname,c.relkind,c.relowner,c.relrowsecurity "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
                        "AND c.relkind IN ('r','p','v','m','f','S')").fetchall()


def audit_data(conn, name, schema, parent=None):
    oid = audit_flags(conn, name, parent)
    require(not conn.execute("SELECT 1 FROM pg_database WHERE datdba=%s", (oid,)).fetchone(),
            'source identity must not own a database')
    require(not conn.execute("SELECT 1 FROM pg_namespace WHERE nspowner=%s", (oid,)).fetchone(),
            'source identity must not own a schema')
    require(not conn.execute("SELECT 1 FROM pg_proc WHERE proowner=%s", (oid,)).fetchone(),
            'source identity must not own a function')
    require(not conn.execute("SELECT 1 FROM pg_database WHERE datname=current_database() AND "
                             "has_database_privilege(%s,oid,'CREATE,TEMP')", (oid,)).fetchone(),
            'source identity has database creation privileges (including PUBLIC)')
    require(not conn.execute("SELECT 1 FROM pg_namespace WHERE has_schema_privilege(%s,oid,'CREATE')",
                             (oid,)).fetchone(), 'source identity has schema creation privilege')
    require(not conn.execute("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                             "WHERE p.prosecdef AND n.nspname !~ '^pg_' "
                             "AND has_function_privilege(%s,p.oid,'EXECUTE')", (oid,)).fetchone(),
            'identity can execute a SECURITY DEFINER function')
    principals = [0, oid]
    if parent:
        principals.append(conn.execute('SELECT oid FROM pg_roles WHERE rolname=%s', (parent,)).fetchone()[0])
    require(not conn.execute('SELECT 1 FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) a '
                             'WHERE a.grantee=ANY(%s::oid[]) AND a.privilege_type <> \'SELECT\'', (principals,)).fetchone(),
            'identity has unexpected default privileges')
    modern = int(conn.execute("SHOW server_version_num").fetchone()[0]) >= 170000
    for obj, ns, table, kind, owner, rls in objects(conn):
        require(owner != oid, 'source identity must not own relations')
        if parent:
            select = conn.execute('SELECT has_sequence_privilege(%s,%s,\'SELECT\')' if kind == 'S' else
                                  'SELECT has_table_privilege(%s,%s,\'SELECT\')', (oid, obj)).fetchone()[0]
            require(select == (ns == schema and table in CONTROLLER_READS and kind != 'S'),
                    'controller read privilege differs from contract')
        if parent is None:
            require(not rls, 'backup cannot bypass row security')
            require(conn.execute('SELECT has_sequence_privilege(%s,%s,\'SELECT\')' if kind == 'S' else
                                 'SELECT has_table_privilege(%s,%s,\'SELECT\')', (oid, obj)).fetchone()[0],
                    'backup lacks complete read privilege')
        for privilege in (('USAGE', 'UPDATE') if kind == 'S' else
                          (('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER', 'MAINTAIN') if modern else
                           ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'))):
            actual = conn.execute('SELECT has_table_privilege(%s,%s,%s)' if kind != 'S' else
                                  'SELECT has_sequence_privilege(%s,%s,%s)', (oid, obj, privilege)).fetchone()[0]
            allowed = False
            # Controller comparison below handles its intended table/column rights.
            if parent:
                allowed = (kind != 'S' and ns == schema and table in (
                    'host_release_operations', 'host_release_fences', 'host_release_fence_receipts')
                    and privilege in ('INSERT', 'UPDATE', 'DELETE'))
            require(actual == allowed, 'identity has unrelated relation privilege')
        if kind != 'S':
            for column, in conn.execute('SELECT attnum FROM pg_attribute WHERE attrelid=%s '
                                        'AND attnum>0 AND NOT attisdropped', (obj,)):
                for privilege in ('INSERT', 'UPDATE', 'REFERENCES'):
                    actual = conn.execute('SELECT has_column_privilege(%s,%s,%s,%s)',
                                           (oid, obj, column, privilege)).fetchone()[0]
                    expected = bool(parent) and conn.execute('SELECT has_column_privilege(%s,%s,%s,%s)',
                                                             (parent, obj, column, privilege)).fetchone()[0]
                    if expected:
                        attname = conn.execute('SELECT attname FROM pg_attribute WHERE attrelid=%s AND attnum=%s', (obj, column)).fetchone()[0]
                        require(ns == schema and ((table == 'agent_registry' and attname == 'updated_at' and privilege == 'UPDATE')
                            or table in ('host_release_operations', 'host_release_fences', 'host_release_fence_receipts')),
                            'controller group has unrelated column privilege')
                    require(actual == expected, 'identity has unrelated column privilege')


def audit_restore(conn, value, source_cluster):
    require(cluster(conn) != source_cluster, 'restore target must be a distinct PostgreSQL cluster')
    oid = audit_flags(conn, value['restore_role'], createdb=True)
    require(not conn.execute('SELECT 1 FROM pg_database WHERE datdba=%s', (oid,)).fetchone(),
            'restore identity has an outstanding owned database; reconcile before enrollment')
    require(not conn.execute('SELECT 1 FROM pg_namespace WHERE nspowner=%s', (oid,)).fetchone(),
            'restore identity owns an existing schema')
    require(not conn.execute('SELECT 1 FROM pg_namespace WHERE has_schema_privilege(%s,oid,\'CREATE\')', (oid,)).fetchone(),
            'restore identity can create objects in an existing schema')
    require(not conn.execute("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                             "WHERE (p.proowner=%s OR (p.prosecdef AND n.nspname !~ '^pg_' "
                             "AND has_function_privilege(%s,p.oid,'EXECUTE')))", (oid, oid)).fetchone(),
            'restore identity owns or can execute an unsafe function')
    require(not conn.execute('SELECT 1 FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) a '
                             'WHERE a.grantee=ANY(%s::oid[])', ([0, oid],)).fetchone(),
            'restore identity has unexpected default privileges')
    for obj, ns, table, kind, owner, rls in objects(conn):
        require(owner != oid, 'restore identity owns an existing relation')
        privileges = 'USAGE,UPDATE' if kind == 'S' else 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
        query = 'SELECT has_sequence_privilege(%s,%s,%s)' if kind == 'S' else 'SELECT has_table_privilege(%s,%s,%s)'
        require(not conn.execute(query, (oid, obj, privileges)).fetchone()[0], 'restore identity writes existing objects')
        if kind != 'S':
            require(not conn.execute("SELECT 1 FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped "
                                     "AND has_column_privilege(%s,attrelid,attnum,'INSERT,UPDATE,REFERENCES')",
                                     (obj, oid)).fetchone(), 'restore identity writes existing columns')


def grant_reads(conn, value):
    name = sql.Identifier(value['backup_role'])
    namespaces = conn.execute("SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' "
                              "AND nspname <> 'information_schema'").fetchall()
    for ns, in namespaces:
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(ns), name))
        conn.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(ns), name))
        conn.execute(sql.SQL('GRANT SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}').format(sql.Identifier(ns), name))


def apply_group(value, directory):
    """Use the existing audited NOLOGIN contract verbatim; credentials via pgpass."""
    params = connection(value['source_owner_dsn'])
    password = params.pop('password', None)
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'HOME': '/nonexistent',
           'PGOPTIONS': '-c search_path=' + value['schema']}
    with tempfile.TemporaryDirectory(prefix='.psql-', dir=directory) as temp:
        if password is not None:
            require(not any(c in password for c in '\r\n\x00'), 'unrepresentable owner credential')
            escaped = password.replace('\\', '\\\\').replace(':', '\\:')
            passfile = Path(temp) / 'pgpass'
            fd = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write('*:*:*:*:' + escaped + '\n')
            env['PGPASSFILE'] = str(passfile)
        script = Path(__file__).resolve().parents[1] / 'sql/role-release-controller.sql'
        result = subprocess.run(['/usr/bin/psql', '-X', '-q', '-w', '-d', make_conninfo(**params),
                                 '-v', 'ON_ERROR_STOP=1', '-v', 'role=' + value['group_role'],
                                 '-f', str(script)], env=env, capture_output=True, timeout=60)
        require(result.returncode == 0, 'controller group contract refused; inspect privileges privately')


def enroll(value, directory):
    value = validate(value)
    require(not (Path(directory) / 'retired.json').exists(), 'operation retired; use a fresh private source')
    with session(value['source_owner_dsn']) as live, session(value['restore_owner_dsn']) as restore:
        source_cluster, restore_cluster = cluster(live), cluster(restore)
        require(source_cluster != restore_cluster, 'restore target must be a distinct PostgreSQL cluster')
        require(not live.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (value['restore_role'],)).fetchone(),
                'restore identity must not exist on the live cluster')
    source_path = Path(directory) / 'credentials.json'
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    if source_path.exists():
        source = read(source_path)
        require(source.get('input_sha256') == digest, 'private source input changed')
        require(source.get('source_cluster') == source_cluster and source.get('restore_cluster') == restore_cluster,
                'enrolled cluster identity changed')
    else:
        # Refuse role collisions BEFORE recording ownership of an intent.
        with session(value['source_owner_dsn']) as live, session(value['restore_owner_dsn']) as restore:
            require(cluster(live) != cluster(restore), 'restore target must be a distinct PostgreSQL cluster')
            for conn, kinds in ((live, ('controller', 'backup')), (restore, ('restore',))):
                for kind in kinds:
                    require(not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',
                                             (value[kind + '_role'],)).fetchone(), 'identity already exists without source')
        passwords = {k: secrets.token_urlsafe(48) for k in ('controller', 'backup', 'restore')}
        source = dict(version=1, input_sha256=digest, passwords=passwords,
                      source_cluster=source_cluster, restore_cluster=restore_cluster,
                      database=dict(backend='postgres', schema=value['schema'],
                                    dsn=login_dsn(value['source_owner_dsn'], value['controller_role'], passwords['controller'])),
                      backup_dsn=login_dsn(value['source_owner_dsn'], value['backup_role'], passwords['backup']),
                      restore_dsn=login_dsn(value['restore_owner_dsn'], value['restore_role'], passwords['restore']))
        save_new(source_path, source)
    apply_group(value, directory)
    with session(value['source_owner_dsn']) as live:
        live.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('release-identities:' + value['operation'],))
        for kind in ('controller', 'backup'):
            role(live, value, kind, source['passwords'][kind])
        # Never repair an existing identity silently: audits precede grants on retry.
        live.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(value['group_role']), sql.Identifier(value['controller_role'])))
        grant_reads(live, value)
        audit_data(live, value['controller_role'], value['schema'], parent=value['group_role'])
        audit_data(live, value['backup_role'], schema=value['schema'])
        source_cluster = cluster(live)
    with session(value['restore_owner_dsn']) as restore:
        role(restore, value, 'restore', source['passwords']['restore'])
        audit_restore(restore, value, source_cluster)
    # Reconnect using the actual generated credentials, not SET ROLE.
    for dsn in (source['database']['dsn'], source['backup_dsn'], source['restore_dsn']):
        with psycopg.connect(**connection(dsn)) as check:
            check.execute('SELECT 1')
    return dict(outcome='enrolled', source_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                source_cluster=source_cluster)


def audit(value, directory):
    value = validate(value)
    source = read(Path(directory) / 'credentials.json')
    require(source['input_sha256'] == hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(),
            'private source input changed')
    with session(value['source_owner_dsn']) as live:
        audit_data(live, value['controller_role'], value['schema'], parent=value['group_role'])
        audit_data(live, value['backup_role'], value['schema'])
        source_cluster = cluster(live)
    require(source_cluster == source['source_cluster'], 'enrolled source cluster identity changed')
    with session(value['restore_owner_dsn']) as restore:
        require(cluster(restore) == source['restore_cluster'], 'enrolled restore cluster identity changed')
        audit_restore(restore, value, source_cluster)
    return dict(outcome='audit_ok', source_cluster=source_cluster)


def leave(value, directory):
    value = validate(value)
    source = read(Path(directory) / 'credentials.json')
    require(source['input_sha256'] == hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(),
            'private source input changed')
    with session(value['source_owner_dsn']) as live, session(value['restore_owner_dsn']) as restore:
        require(cluster(live) == source['source_cluster'] and cluster(restore) == source['restore_cluster'],
                'enrolled cluster identity changed')
    retirement = Path(directory) / 'retired.json'
    if not retirement.exists():
        save_new(retirement, dict(outcome='retirement_requested', input_sha256=source['input_sha256']))
    for dsn, kinds in ((value['source_owner_dsn'], ('controller', 'backup')),
                       (value['restore_owner_dsn'], ('restore',))):
        with session(dsn) as conn:
            for kind in kinds:
                name = value[kind + '_role']
                found = conn.execute("SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
                if not found:
                    continue
                require(found[0] == marker(value, kind), 'identity belongs to another operation')
                if kind == 'restore':
                    with session(value['source_owner_dsn']) as live:
                        audit_restore(conn, value, cluster(live))
                else:
                    audit_data(conn, name, value['schema'], parent=value['group_role'] if kind == 'controller' else None)
                ident = sql.Identifier(name)
                # Never DROP OWNED: ownership/data is an explicit refusal, not cleanup.
                if kind == 'controller':
                    conn.execute(sql.SQL('REVOKE {} FROM {}').format(sql.Identifier(value['group_role']), ident))
                if kind == 'backup':
                    for ns, in conn.execute("SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"):
                        conn.execute(sql.SQL('REVOKE SELECT ON ALL TABLES IN SCHEMA {} FROM {}').format(sql.Identifier(ns), ident))
                        conn.execute(sql.SQL('REVOKE SELECT ON ALL SEQUENCES IN SCHEMA {} FROM {}').format(sql.Identifier(ns), ident))
                        conn.execute(sql.SQL('REVOKE USAGE ON SCHEMA {} FROM {}').format(sql.Identifier(ns), ident))
                conn.execute(sql.SQL('DROP ROLE {}').format(ident))
    receipt = Path(directory) / 'leave-receipt.json'
    if not receipt.exists():
        save_new(receipt, dict(outcome='retired', input_sha256=source['input_sha256']))
    return dict(outcome='retired', shared_group_retained=True, private_source_retained=True)


def main():
    try:
        require(os.geteuid() == 0 and sys.flags.isolated and sys.flags.dont_write_bytecode
                and sys.argv[1:] in (['enroll'], ['audit'], ['leave']), 'isolated root enroll|audit|leave required')
        for path in (Path(__file__).resolve(), Path(__file__).resolve().parents[1] / 'sql/role-release-controller.sql'):
            require(not Path(__file__).is_symlink(), 'linked root program')
            for parent in (path, *path.parents):
                st = parent.lstat()
                require(not parent.is_symlink() and st.st_uid == 0 and not st.st_mode & 0o022,
                        'root-owned immutable program required')
        os.umask(0o077)
        os.environ.clear()
        protected(ROOT, directory=True)
        value = read(ROOT / 'input.json')
        lock = ROOT / 'lock'
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+') as stream:
            protected(lock)
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            operation = sys.argv[1]
            result = audit(value, ROOT) if operation == 'audit' else leave(value, ROOT) if operation == 'leave' else enroll(value, ROOT)
            print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        # psycopg errors, DSNs, server details and passwords must not cross stdout/stderr.
        print('{"error":"identity_enrollment_refused; private source and intents retained"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
