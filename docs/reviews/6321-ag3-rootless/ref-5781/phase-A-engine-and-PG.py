"""AG1 phase A only: protected prerequisites, rootless engine, local test PG.

--inspect is read-only apart from the operator's own private receipt.
--apply requires the operator's fresh BG/5748/drain proof; no key is created.
All private configuration remains on this VM. Group removal is phase B.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import time
import uuid

ROOT = Path('/root/ameesh-operator-5781-AG1-20261104')
ARCHIVE = Path('/var/tmp/ameesh-operator-5495-kit-5c1e679b-20261104T1845-fc90fba7/L2-rootless-review-20261104.tar.gz')
ARCHIVE_SHA = '805890afbe7facc8721834c56bb734850f6c798a946dc94332e54f83e4140f62'
RF = 'unix:///run/docker.sock'
RL = 'unix:///run/user/1001/docker.sock'
HOME = '/home/ameesh'
os.umask(0o077)
record = {'lot': 5781, 'phase': 'starting', 'host': os.uname().nodename,
          'group_removal_performed': False, 'L2_key_created': False}


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_private(path, data):
    assert not path.is_symlink()
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def progress(phase, **fields):
    record.update(fields, phase=phase, at=stamp())
    write_private(ROOT / 'phase-A-progress.json', (json.dumps(record, indent=2) + '\n').encode())


def command(args, *, data=None, timeout=60, allow_error=False):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if result.returncode and not allow_error:
        # The root-only diagnostic can contain private configuration; never forward it.
        write_private(ROOT / 'last-command-error.private', result.stderr)
        raise RuntimeError(args[0] + ' rc=' + str(result.returncode))
    return result


def user_args(args, extra=()):
    return ['runuser', '-u', 'ameesh', '--', 'env', '-i', '--chdir=' + HOME, 'HOME=' + HOME,
            'USER=ameesh', 'LOGNAME=ameesh', 'PATH=/usr/local/bin:/usr/bin:/bin',
            'XDG_RUNTIME_DIR=/run/user/1001', 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus',
            *extra, *args]


def user(args, *, data=None, timeout=60):
    return command(user_args(args), data=data, timeout=timeout).stdout.decode().strip()


def docker(endpoint, *args, timeout=60, allow_error=False):
    return command(['docker', '--host', endpoint, *args], timeout=timeout, allow_error=allow_error)


def inspect(endpoint, name):
    return json.loads(docker(endpoint, 'inspect', name).stdout)[0]


def user_json(code, payload):
    # This code runs as UID 1001: user-controlled home paths never get root writes.
    return user(['/usr/bin/python3', '-I', '-c', code], data=json.dumps(payload).encode())


def sql(endpoint, container, role, query):
    r = command(['docker', '--host', endpoint, 'exec', '-i', container,
                 'psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-U', role, '-d', 'postgres'],
                data=('BEGIN READ ONLY; SET LOCAL statement_timeout=\'10s\';\n' + query + '\nCOMMIT;\n').encode())
    return [json.loads(x) for x in r.stdout.decode().splitlines() if x.startswith(('{', '[')) or x == 'null']


def database_baseline(endpoint, container, role):
    databases = sql(endpoint, container, role,
                    "SELECT json_agg(datname ORDER BY datname) FROM pg_database WHERE NOT datistemplate;")[0]
    tablespaces = sql(endpoint, container, role,
                      "SELECT coalesce(json_agg(spcname),'[]'::json) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global');")[0]
    assert not tablespaces, 'External tablespaces need a separately qualified migration'
    result = {}
    query = r"""BEGIN READ ONLY;
SET LOCAL statement_timeout='10s';
SELECT format('SELECT json_build_object(''schema'', %L, ''count'', count(*)) FROM %I.schema_migrations;', schemaname, schemaname)
 FROM pg_tables WHERE tablename='schema_migrations' ORDER BY schemaname
\gexec
COMMIT;
"""
    for database in databases:
        r = command(['docker', '--host', endpoint, 'exec', '-i', container,
                     'psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-U', role, '-d', database], data=query.encode())
        result[database] = [json.loads(x) for x in r.stdout.decode().splitlines() if x.startswith('{')]
    return result


def stream(producer, consumer, timeout=300):
    with (ROOT / 'stream-producer.private').open('wb') as pe, (ROOT / 'stream-consumer.private').open('wb') as ce:
        p = subprocess.Popen(producer, stdout=subprocess.PIPE, stderr=pe)
        c = subprocess.Popen(consumer, stdin=p.stdout, stdout=ce, stderr=ce)
        p.stdout.close()
        try:
            cr = c.wait(timeout=timeout)
            pr = p.wait(timeout=30)
        except BaseException:
            for child in (c, p):
                if child.poll() is None:
                    child.terminate()
            for child in (c, p):
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=15)
            raise
        assert cr == pr == 0, 'Stream failed; preserve source and reconcile'


def volume_manifest(endpoint, image, volume):
    code = "cd /data; find . -xdev -type f -exec sha256sum '{}' + | LC_ALL=C sort; echo __OWNERS__; find . -xdev -printf '%y %U %G %m %P\\n' | LC_ALL=C sort"
    out = docker(endpoint, 'run', '--rm', '--network=none', '--label=ameesh.operator=5781',
                 '--mount', 'type=volume,src=' + volume + ',dst=/data,readonly',
                 '--entrypoint=/bin/sh', image, '-c', code, timeout=300).stdout
    return hashlib.sha256(out).hexdigest(), out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inspect', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    assert args.inspect != args.apply
    assert os.geteuid() == 0 and os.uname().nodename == 'ameesh-agents-1'
    ROOT.mkdir(mode=0o700, exist_ok=True)
    assert not ROOT.is_symlink() and ROOT.stat().st_uid == 0 and stat.S_IMODE(ROOT.stat().st_mode) == 0o700
    source = inspect(RF, 'ameesh-pg')
    assert source['State']['Running'] and source['State']['Health']['Status'] == 'healthy'
    assert source['HostConfig']['PortBindings'] == {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '55432'}]}
    mounts = [x for x in source['Mounts'] if x['Destination'] == '/var/lib/postgresql/data']
    assert len(mounts) == 1 and mounts[0]['Type'] == 'volume'
    source_volume = mounts[0]['Name']
    assert re.fullmatch(r'[A-Za-z0-9_.-]+', source_volume)
    assert len(source['Mounts']) == 1, 'Additional mounts need separate review'
    assert source['Config'].get('Entrypoint') == ['docker-entrypoint.sh']
    env = dict(x.split('=', 1) for x in source['Config']['Env'] if '=' in x)
    assert env.get('PGDATA', '/var/lib/postgresql/data') == '/var/lib/postgresql/data'
    role = env.get('POSTGRES_USER', 'postgres')
    assert re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', role)
    summary = {'source_container': source['Id'], 'source_image': source['Image'],
               'source_volume': source_volume, 'role': role,
               'source_restart_policy': source['HostConfig']['RestartPolicy']['Name']}
    if args.inspect:
        summary.update(observed_at=stamp(), mutations=False,
                       rootless_socket_exists=Path('/run/user/1001/docker.sock').exists())
        write_private(ROOT / 'inspection.json', (json.dumps(summary, indent=2) + '\n').encode())
        print(json.dumps(summary))
        return
    assert not (ROOT / 'phase-A-progress.json').exists(), 'Inspect prior attempt; do not replay'
    proof_path = ROOT / 'start-authority.json'
    assert proof_path.stat().st_uid == 0 and stat.S_IMODE(proof_path.stat().st_mode) == 0o600
    proof = json.loads(proof_path.read_text())
    for key in ['BG_and_5748_complete', 'native_AG1_drain_active', 'no_active_turns',
                'composer_timer_masked', 'composer_inactive', 'roles_have_other_candidates',
                'no_unapproved_personal_session']:
        assert proof.get(key) is True, 'Missing prerequisite: ' + key
    assert 0 <= time.time() - proof['verified_epoch'] < 600
    now = datetime.datetime.now(datetime.timezone.utc)
    assert datetime.datetime(2026, 11, 4, 22, tzinfo=datetime.timezone.utc) < now < datetime.datetime(2026, 11, 5, 8, 30, tzinfo=datetime.timezone.utc)
    assert 'ActiveState=inactive' in user(['systemctl', '--user', 'show', 'ameesh-paquet-horaire.service', '-p', 'ActiveState'])
    assert 'masked' in user(['systemctl', '--user', 'show', 'ameesh-paquet-horaire.timer', '-p', 'UnitFileState'])
    assert not Path('/home/ameesh-staging/.ssh/id_ed25519').exists(), 'No pre-key rollback while a key exists'
    assert not Path('/run/user/1001/docker.sock').exists(), 'Reconcile existing rootless engine first'
    progress('verified prerequisites', **summary, pilot=190619, reviews=[126213, 126245])
    write_private(ROOT / 'source-container.private.json', json.dumps(source).encode())
    snapshot_code = r'''import base64,json,os,pathlib,stat
h=pathlib.Path.home(); names=['.docker/config.json','.config/docker/daemon.json','.config/ameesh/env','.config/ameesh/bascule.env']; e=h/'.config/environment.d'
if e.is_dir(): names.extend(str(p.relative_to(h)) for p in e.iterdir())
out={}
for name in names:
 p=h/name
 try:s=p.lstat()
 except FileNotFoundError:out[name]={'type':'absent'};continue
 row={'mode':stat.S_IMODE(s.st_mode)}
 if stat.S_ISLNK(s.st_mode):row.update(type='symlink',target=os.readlink(p))
 elif stat.S_ISREG(s.st_mode):row.update(type='file',data=base64.b64encode(p.read_bytes()).decode())
 else:raise RuntimeError('Unexpected configuration file type')
 out[name]=row
print(json.dumps(out))'''
    private_snapshot = user(['/usr/bin/python3', '-I', '-c', snapshot_code])
    write_private(ROOT / 'home-config-before.private.json', private_snapshot.encode())
    for operation, args_state in [('enabled', ['list-unit-files', '--no-pager', '--output=json']),
                                   ('active', ['list-units', '--all', '--no-pager', '--output=json'])]:
        write_private(ROOT / ('user-units-before-' + operation + '.private.json'),
                      user(['systemctl', '--user', *args_state]).encode())
    write_private(ROOT / 'docker-context-before.json', user(['docker', 'context', 'show']).encode())
    assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() == ARCHIVE_SHA
    extracted = ROOT / 'verified-public-inputs'
    extracted.mkdir(mode=0o700)
    with tarfile.open(ARCHIVE) as archive:
        archive.extractall(extracted, filter='data')
    kit = extracted / 'L2-rootless-review-20261104'
    authenticated = json.loads((kit / 'apt/offline-verification.json').read_text())
    for name in ['/usr/bin/docker', '/usr/bin/dockerd', '/usr/bin/containerd', '/usr/bin/runc']:
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == authenticated['expected_programs'][name]['sha256']
    package = next((kit / 'apt/debs').glob('rootlesskit_1.1.1-1ubuntu0.24.04.3_amd64.deb'))
    assert hashlib.sha256(package.read_bytes()).hexdigest() == authenticated['expected_programs']['/usr/bin/rootlesskit']['package_sha256']
    # Dependency failure is explicit; never resolve it with an unpinned upgrade.
    command(['dpkg', '--no-act', '--install', str(package)], timeout=90)
    progress('installing authenticated rootlesskit')
    command(['dpkg', '--install', str(package)], timeout=180)
    for name, digest in [('dockerd-rootless-setuptool.sh', 'e68ef6550fea1b5c01b5cc7a400a6b44ee28159d7714758c3845759e03e079dc'),
                         ('dockerd-rootless.sh', '8eb00d61b5e78ffc0dcd628f056389b5859cc61d6fe3ce17b4a3a4b08143c8a5')]:
        data = (kit / 'upstream' / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        target = Path('/usr/local/bin') / name
        assert not target.is_symlink() and target.parent.stat().st_uid == 0
        if target.exists():
            assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
        else:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
    assert user(['loginctl', 'show-user', 'ameesh', '-p', 'Linger', '--value']) == 'yes'
    for name in ['newuidmap', 'newgidmap']:
        assert Path('/usr/bin', name).stat().st_mode & stat.S_ISUID
    for name in ['subuid', 'subgid']:
        assert any(x.startswith('ameesh:') and int(x.split(':')[2]) >= 65536 for x in Path('/etc', name).read_text().splitlines())
    progress('installing native rootless engine under UID1001')
    context = user(['/usr/bin/mktemp', '-d', '/home/ameesh/.docker-operator-5781-XXXXXX'])
    configure = "import json,pathlib,os,sys;d=pathlib.Path.home()/'.config/docker';d.mkdir(parents=True,exist_ok=True);p=d/'daemon.json';assert not p.exists();p.write_text(json.dumps({'default-address-pools':[{'base':'172.30.0.0/16','size':24}]}));os.chmod(p,0o600)"
    user(['/usr/bin/python3', '-I', '-c', configure])
    command(user_args(['/usr/local/bin/dockerd-rootless-setuptool.sh', 'install', '--force', '--skip-iptables'],
                      extra=['DOCKER_CONFIG=' + context]), timeout=180)
    info = json.loads(docker(RL, 'info', '--format', '{{json .}}').stdout)
    assert any('rootless' in x for x in info['SecurityOptions']) and info['Driver'] != 'vfs'
    assert info['DockerRootDir'] == '/home/ameesh/.local/share/docker'
    before = database_baseline(RF, 'ameesh-pg', role)
    write_private(ROOT / 'database-baseline.private.json', json.dumps(before).encode())
    aliases = {source['Config']['Image'], 'postgres:17'}
    selected = user_json("import pathlib,json,subprocess,shlex;d=[];h=pathlib.Path.home();lines=[]\nfor name in ['.config/ameesh/env','.config/ameesh/release.env']:\n p=h/name\n if p.exists():lines+=p.read_text().splitlines()\nlines+=subprocess.run(['systemctl','--user','show-environment'],check=True,capture_output=True,text=True).stdout.splitlines()\nfor l in lines:\n k,s,v=l.partition('=')\n if k in ['AMEESH_PG_IMAGE','AGENT_MESH_PG_IMAGE'] and s and v:\n  parsed=shlex.split(v);assert len(parsed)==1;d.append(parsed[0])\nprint(json.dumps(d))", {})
    aliases.update(json.loads(selected))
    alias_ids = {name: json.loads(docker(RF, 'image', 'inspect', name).stdout)[0]['Id'] for name in aliases if name}
    images = set(alias_ids.values()) | {source['Image']}
    required = sum(json.loads(docker(RF, 'image', 'inspect', x).stdout)[0]['Size'] for x in images)
    size = int(command(['du', '-sb', mounts[0]['Source']]).stdout.split()[0])
    available = os.statvfs(HOME).f_bavail * os.statvfs(HOME).f_frsize
    assert available > required + size + 10 * 2**30, 'Insufficient space; do not purge automatically'
    for image in images:
        stream(['docker', '--host', RF, 'save', image], ['docker', '--host', RL, 'load'])
    for alias, image in alias_ids.items():
        docker(RL, 'tag', image, alias)
    target_volume = 'ameesh-pg-data'
    current_volumes = [json.loads(x) for x in docker(RL, 'volume', 'ls', '--format', '{{json .Name}}').stdout.decode().splitlines() if x]
    assert target_volume not in current_volumes, 'Existing destination volume; reconcile without overwriting'
    docker(RL, 'volume', 'create', '--label=ameesh.operator=5781', target_volume)
    progress('rootless engine ready; stopping only source PG', databases=len(before), copied_image_count=len(images), source_bytes=size)
    docker(RF, 'stop', '--time=60', 'ameesh-pg', timeout=90)
    record['source_stopped'] = True
    stopped = inspect(RF, 'ameesh-pg')
    assert not stopped['State']['Running'] and stopped['State']['ExitCode'] == 0
    progress('copying stopped PostgreSQL volume; original retained')
    source_cmd = ['docker', '--host', RF, 'run', '--rm', '--network=none', '--label=ameesh.operator=5781',
                  '--mount', 'type=volume,src=' + source_volume + ',dst=/data,readonly',
                  '--entrypoint=/bin/tar', source['Image'], '--numeric-owner', '-C', '/data', '-cpf', '-', '.']
    target_cmd = ['docker', '--host', RL, 'run', '--rm', '-i', '--network=none', '--label=ameesh.operator=5781',
                  '--mount', 'type=volume,src=' + target_volume + ',dst=/data',
                  '--entrypoint=/bin/tar', source['Image'], '--numeric-owner', '-C', '/data', '-xpf', '-']
    stream(source_cmd, target_cmd)
    source_hash, source_manifest = volume_manifest(RF, source['Image'], source_volume)
    target_hash, target_manifest = volume_manifest(RL, source['Image'], target_volume)
    assert source_hash == target_hash and source_manifest == target_manifest
    write_private(ROOT / 'source-volume-manifest.private', source_manifest)
    private_env = user_json("import pathlib,json,sys,os,tempfile;v=json.load(sys.stdin);fd,n=tempfile.mkstemp(prefix='.pg-env-5781-',dir=pathlib.Path.home());os.fchmod(fd,0o600);f=os.fdopen(fd,'w');f.write('\\n'.join(v)+'\\n');f.close();print(n)", source['Config']['Env'])
    # The source container's immutable image is used even if its floating tag moved.
    docker(RL, 'tag', source['Image'], source['Config']['Image'])
    args_run = ['run', '-d', '--name=ameesh-pg', '--restart=unless-stopped', '--label=ameesh.operator=5781',
                '--env-file', private_env, '-p', '127.0.0.1:55432:5432',
                '--mount', 'type=volume,src=' + target_volume + ',dst=/var/lib/postgresql/data',
                '--health-cmd=pg_isready -U ' + role + ' -d postgres', '--health-interval=2s', '--health-timeout=3s', '--health-retries=60',
                source['Config']['Image'], *(source['Config'].get('Cmd') or [])]
    created = docker(RL, *args_run).stdout.decode().strip()
    record['target_container'] = created
    for alias, image in alias_ids.items():
        docker(RL, 'tag', image, alias)
    deadline = time.monotonic() + 150
    while True:
        target = inspect(RL, 'ameesh-pg')
        if target['State'].get('Health', {}).get('Status') == 'healthy':
            break
        assert time.monotonic() < deadline, 'Target health timeout; inspect same engine before retry'
        time.sleep(2)
    after = database_baseline(RL, 'ameesh-pg', role)
    assert before == after
    assert target['Image'] == source['Image']
    user(['/usr/bin/rm', '--', private_env])
    progress('completed phase A; group/client/manager still unchanged', target_container=created,
             image_identical=True, volume_bytes_and_numeric_owners_equal=True,
             database_and_schema_counts_equal=True, databases=len(before),
             schema_migration_tables=sum(len(x) for x in before.values()),
             source_volume_retention_days_at_least=7, source_container_stopped=True,
             client_groups_restart_pending=True, no_other_container_touched=True)
    print(json.dumps(record), flush=True)


if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        if ROOT.exists() and '--apply' in __import__('sys').argv and (ROOT / 'phase-A-progress.json').exists():
            # No blind restart or deletion. Source and target IDs remain in the receipt.
            progress('failed; preserve drainage and reconcile actual phase',
                     error_type=type(exc).__name__, error=str(exc), no_blind_replay=True)
        raise
