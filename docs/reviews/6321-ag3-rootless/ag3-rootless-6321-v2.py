"""#6321 v2 (réponses mesh-design #200904) — O1 seules les 4 images de base ; O2 preuve containerd (dpkg
--verify, version et origine, sha256 du .deb comparé à l'index Packages s'il est en cache) ; O5 fenêtre
09/11 13:00-15:30Z exigée pour --apply ; O7 preuve ss -ltn et garde d'invités en phase C ; O4 critère 1
de la phase B (aucun processus uid 1001 avec 112, pause Podman compris).

#6321 — Docker rootless pour ameesh (uid 1001) sur ameesh-agents-3, retrait du groupe docker.

Contrôleur root unique, exécuté SUR AG3, dérivé des contrôleurs relus et exécutés de #5781 sur AG1
(phase-A-engine-and-PG.py d5b50510…, phase-B-client-and-groups.py, phase-C-local-verification.py,
phase-C-correct-module-list.py, pre-key-rollback.py), avec les leçons de leurs incidents :
  - umask 077 → scripts installés 0700 (AG1 01:44Z) : rien n'est installé ici (prérequis présents,
    vérifiés) ; aucun fichier exécutable n'est créé sous umask 077 ;
  - setuptool l.377 `$BIN/docker version` (AG1 01:53Z) : le lien /usr/local/bin/docker → /usr/bin/docker
    existe sur AG3 depuis le 02/11 (pré-requis de l'essai l5502), vérifié, jamais créé ni retiré ici ;
  - répertoires de contexte orphelins (N2 #5781) : DOCKER_CONFIG jetable créé par mktemp, retiré en fin
    de phase A et consigné ;
  - liste de modules de test-parallele.sh : « test_work », pas « tests.test_work » (AG1 phase C) ;
  - porte d'hôte « snapshot mismatch » et runners « lost » (#5848, #5869) : la phase C n'annonce rien
    tant que la porte n'est pas ouverte et les 8 runners revenus (lecture native).
Sous-commandes : inspecter | phase-a | phase-b | phase-c | retour. Sans --apply : contrôles en
lecture seule et plan imprimé (à blanc). Avec --apply : autorité fraîche exigée
(/root/ameesh-operator-6321-AG3/<phase>-authority.json, root 0600, < 600 s).
Aucune clé, aucun secret en argv ni en sortie ; les configurations privées restent sur AG3.
"""
import argparse
import datetime
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import time
import uuid

HOST = 'ameesh-agents-3'
ROOT = Path('/root/ameesh-operator-6321-AG3')
RF = 'unix:///run/docker.sock'
RL = 'unix:///run/user/1001/docker.sock'
HOME = '/home/ameesh'
PG_NAME = re.compile(r'ameesh-pg(?:-dev[0-9]+)?')
# Programmes relus (kit 805890af, offline-verification.json) et relevés sur AG3 le 08/11 13:3xZ.
PROGRAMS = {'/usr/bin/docker': '7ed12b00293d6474', '/usr/bin/dockerd': '237b0603cc24496c',
            '/usr/bin/runc': 'bdce4d45b2dd2174', '/usr/bin/rootlesskit': '69dc237b30d00fc4',
            '/usr/bin/newuidmap': '9035472d2a4ab77d', '/usr/bin/newgidmap': '32011f9ee3aeda7c'}
# containerd 2.2.1-0ubuntu1~24.04.4 diffère du kit (~24.04.3) : authentifié par dpkg --verify et
# l'origine noble-updates, relevés à l'inspection (point ouvert O2 du dossier).
SCRIPTS = {'/usr/local/bin/dockerd-rootless-setuptool.sh': 'e68ef6550fea1b5c01b5cc7a400a6b44ee28159d7714758c3845759e03e079dc',
           '/usr/local/bin/dockerd-rootless.sh': '8eb00d61b5e78ffc0dcd628f056389b5859cc61d6fe3ce17b4a3a4b08143c8a5'}
# Images chargées du rootful vers le rootless (identifiants exacts, aucun pull) : celles des PG et des
# scripts de test (pg-up.sh, test-parallele.sh, suites Nexlink). Les autres restent dans le rootful.
IMAGES = {'postgres:17': 'sha256:b8faeff7712663c2773a190181e1246e37e364b887611c04658c28b44e2408fe',
          'postgres:16': 'sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a',
          'node:22-bookworm-slim': 'sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da',
          'ubuntu:24.04': 'sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852'}
ADDRESS_POOL = {'default-address-pools': [{'base': '172.30.0.0/16', 'size': 24}]}
# Coupures 10:00Z et 22:00Z : aucune phase de 20 min avant à 90 min après (staging et vm.sh sur AG3).
FORBIDDEN = [(9 * 60 + 40, 11 * 60 + 30), (21 * 60 + 40, 23 * 60 + 30)]
# O5 (#200904) : fenêtre fixée par mesh-design, après C3 ; --apply refusé hors de celle-ci.
WINDOW = (datetime.datetime(2026, 11, 9, 13, 0, tzinfo=datetime.timezone.utc),
          datetime.datetime(2026, 11, 9, 15, 30, tzinfo=datetime.timezone.utc))
PG_PORTS = {'55432', '55441', '55442', '55443'}
GUEST_FIREWALL = Path('/etc/ameesh-guest/firewall.nft')
UNIT_RE = re.compile(r'ameesh-[A-Za-z0-9_.@:-]+\.(?:service|timer)')
os.umask(0o077)


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


def command(args, *, data=None, timeout=60, allow_error=False):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if result.returncode and not allow_error:
        write_private(ROOT / 'last-command-error.private', result.stderr)  # peut contenir du privé
        raise RuntimeError(args[0] + ' rc=' + str(result.returncode))
    return result


def user_args(args, extra=()):
    return ['runuser', '-u', 'ameesh', '--', 'env', '-i', '--chdir=' + HOME, 'HOME=' + HOME,
            'USER=ameesh', 'LOGNAME=ameesh', 'PATH=/usr/local/bin:/usr/bin:/bin',
            'XDG_RUNTIME_DIR=/run/user/1001', 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus',
            *extra, *args]


def user(args, *, data=None, timeout=60):
    return command(user_args(args), data=data, timeout=timeout).stdout.decode().strip()


def user_json(code, payload):
    return user(['/usr/bin/python3', '-I', '-c', code], data=json.dumps(payload).encode())


def docker(endpoint, *args, timeout=60, allow_error=False):
    return command(['docker', '--host', endpoint, *args], timeout=timeout, allow_error=allow_error)


def inspect(endpoint, name):
    return json.loads(docker(endpoint, 'inspect', name).stdout)[0]


def processes():
    rows = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            status = dict(l.split(':', 1) for l in (entry / 'status').read_text().splitlines() if ':' in l)
            if int(status['Uid'].split()[0]) != 1001:
                continue
            rows.append({'pid': int(entry.name), 'name': status['Name'].strip(),
                         'groups': [int(x) for x in status.get('Groups', '').split()],
                         'cgroup': (entry / 'cgroup').read_text().strip()})
        except (FileNotFoundError, ProcessLookupError):
            continue
    return rows


def pg_sources():
    """Les PG longs du rootful : noms ameesh-pg[-devN], un seul volume nommé, port 127.0.0.1 seul."""
    names = docker(RF, 'ps', '-a', '--format', '{{.Names}}').stdout.decode().split()
    out = []
    for name in sorted(names):
        assert PG_NAME.fullmatch(name), 'Conteneur rootful hors liste relue : ' + name
        c = inspect(RF, name)
        mounts = [m for m in c['Mounts'] if m['Destination'] == '/var/lib/postgresql/data']
        assert len(c['Mounts']) == len(mounts) == 1 and mounts[0]['Type'] == 'volume', name + ' montages'
        ports = c['HostConfig']['PortBindings']
        assert list(ports) == ['5432/tcp'] and len(ports['5432/tcp']) == 1 and ports['5432/tcp'][0]['HostIp'] == '127.0.0.1'
        assert c['Config'].get('Entrypoint') == ['docker-entrypoint.sh'] and c['Image'] == IMAGES['postgres:17']
        env = dict(x.split('=', 1) for x in c['Config']['Env'] if '=' in x)
        assert env.get('PGDATA', '/var/lib/postgresql/data') == '/var/lib/postgresql/data'
        out.append({'name': name, 'id': c['Id'], 'image': c['Image'], 'volume': mounts[0]['Name'],
                    'source_path': mounts[0]['Source'], 'port': ports['5432/tcp'][0]['HostPort'],
                    'role': env.get('POSTGRES_USER', 'postgres'), 'cmd': c['Config'].get('Cmd') or [],
                    'healthcheck': c['Config'].get('Healthcheck'), 'env': c['Config']['Env'],
                    'restart': c['HostConfig']['RestartPolicy']['Name'],
                    'running': c['State']['Running'], 'health': c['State'].get('Health', {}).get('Status')})
    return out


def sql_rows(endpoint, container, role, database, query):
    r = command(['docker', '--host', endpoint, 'exec', '-i', container, 'psql', '-X', '-q', '-A', '-t',
                 '-v', 'ON_ERROR_STOP=1', '-U', role, '-d', database], data=query.encode())
    return [json.loads(x) for x in r.stdout.decode().splitlines() if x.startswith(('{', '['))]


def database_baseline(endpoint, container, role):
    databases = sql_rows(endpoint, container, role, 'postgres',
                         "BEGIN READ ONLY; SELECT json_agg(datname ORDER BY datname) FROM pg_database WHERE NOT datistemplate; COMMIT;")[0]
    spaces = sql_rows(endpoint, container, role, 'postgres',
                      "BEGIN READ ONLY; SELECT coalesce(json_agg(spcname),'[]'::json) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global'); COMMIT;")[0]
    assert not spaces, 'Tablespaces externes : migration à qualifier à part'
    query = r"""BEGIN READ ONLY;
SET LOCAL statement_timeout='10s';
SELECT format('SELECT json_build_object(''schema'', %L, ''count'', count(*)) FROM %I.schema_migrations;', schemaname, schemaname)
 FROM pg_tables WHERE tablename='schema_migrations' ORDER BY schemaname
\gexec
COMMIT;
"""
    return {db: sql_rows(endpoint, container, role, db, query) for db in databases}


def stream(producer, consumer, label, timeout=600):
    with (ROOT / (label + '-producer.private')).open('wb') as pe, (ROOT / (label + '-consumer.private')).open('wb') as ce:
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
        assert cr == pr == 0, 'Flux en échec ; source conservée, réconcilier'


def volume_manifest(endpoint, image, volume):
    code = "cd /data; find . -xdev -type f -exec sha256sum '{}' + | LC_ALL=C sort; echo __OWNERS__; find . -xdev -printf '%y %U %G %m %P\\n' | LC_ALL=C sort"
    out = docker(endpoint, 'run', '--rm', '--network=none', '--label=ameesh.operator=6321',
                 '--mount', 'type=volume,src=' + volume + ',dst=/data,readonly',
                 '--entrypoint=/bin/sh', image, '-c', code, timeout=600).stdout
    return hashlib.sha256(out).hexdigest(), out


def authority(name, keys):
    path = ROOT / (name + '-authority.json')
    st = path.lstat()
    assert stat.S_ISREG(st.st_mode) and st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o600, name + ' autorité non protégée'
    a = json.loads(path.read_text())
    for key in keys:
        assert a.get(key) is True, 'Préalable manquant : ' + key
    assert 0 <= time.time() - a['verified_epoch'] < 600, 'Autorité périmée'
    assert a.get('pilot_go_mail', 0) > 0 and set(a.get('reviews', {})) == {'codex', 'deepseek'}
    return a


def outside_cutoffs():
    now = datetime.datetime.now(datetime.timezone.utc)
    assert WINDOW[0] <= now < WINDOW[1], 'Hors de la fenêtre 09/11 13:00-15:30Z (O5 #200904)'
    m = now.hour * 60 + now.minute
    assert not any(a <= m < b for a, b in FORBIDDEN), 'Fenêtre de coupure (10:00Z/22:00Z, -20 min/+90 min)'


def deny_rootful_as_uid1001():
    code = r'''import errno,socket,json
s=socket.socket(socket.AF_UNIX)
try:s.connect('/run/docker.sock')
except OSError as e:print(json.dumps({'denied':e.errno in (errno.EACCES,errno.EPERM),'errno':e.errno}))
else:print(json.dumps({'denied':False}))
finally:s.close()'''
    return json.loads(user(['/usr/bin/python3', '-I', '-c', code]))


def common_checks(apply=False):
    """À blanc, rien n'est créé sur l'hôte (pas même le dossier de reçus)."""
    assert os.geteuid() == 0 and os.uname().nodename == HOST
    if apply:
        ROOT.mkdir(mode=0o700, exist_ok=True)
    if ROOT.exists():
        st = ROOT.lstat()
        assert not ROOT.is_symlink() and st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o700
    acct = pwd.getpwnam('ameesh')
    assert acct.pw_uid == acct.pw_gid == 1001


def prerequisites():
    """Prérequis rootless déjà présents sur AG3 (02/11 22:31Z) : vérifiés, jamais installés ici."""
    facts = {}
    for path, prefix in PROGRAMS.items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest().startswith(prefix), path + ' ≠ kit relu'
    for path, digest in SCRIPTS.items():
        st = Path(path).lstat()
        assert stat.S_ISREG(st.st_mode) and st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o755
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, path
    link = Path('/usr/local/bin/docker')
    assert link.is_symlink() and os.readlink(link) == '/usr/bin/docker', 'lien du client attendu par setuptool l.377'
    for name in ['newuidmap', 'newgidmap']:
        assert Path('/usr/bin', name).stat().st_mode & stat.S_ISUID
    for name in ['subuid', 'subgid']:
        assert any(x.startswith('ameesh:') and int(x.split(':')[2]) >= 65536 for x in Path('/etc', name).read_text().splitlines())
    assert Path('/etc/apparmor.d/rootlesskit').is_file(), 'profil AppArmor rootlesskit (userns restreint sur 24.04)'
    command(['dpkg', '--verify', 'docker.io', 'containerd', 'runc', 'rootlesskit', 'uidmap', 'slirp4netns', 'fuse-overlayfs'])
    facts['containerd'] = containerd_proof()
    assert command(['loginctl', 'show-user', 'ameesh', '-p', 'Linger', '--value']).stdout.decode().strip() == 'yes'
    for image, ident in IMAGES.items():
        assert json.loads(docker(RF, 'image', 'inspect', image).stdout)[0]['Id'] == ident, image
    return facts


def packages_sha256(texts, package, version):
    """SHA256 d'un paquet/version dans des index apt Packages (texte), ou None."""
    for text in texts:
        for stanza in text.split('\n\n'):
            fields = {}
            for line in stanza.splitlines():
                if line[:1] not in (' ', '') and ':' in line:
                    k, v = line.split(':', 1)
                    fields[k] = v.strip()
            if fields.get('Package') == package and fields.get('Version') == version and 'SHA256' in fields:
                return fields['SHA256']
    return None


def containerd_proof():
    """O2 (#200904) : dpkg --verify (fait par l'appelant), version et origine apt, et, si le .deb est
    encore en cache, son sha256 égal à celui de l'index Packages de noble-updates."""
    version = command(['dpkg-query', '-W', '-f', '${Version}', 'containerd']).stdout.decode()
    policy = command(['apt-cache', 'policy', 'containerd']).stdout.decode()
    assert re.search(r'\*\*\* ' + re.escape(version) + r' 500\n\s+500 \S+ noble-updates/main', policy), 'origine containerd ≠ noble-updates'
    proof = {'version': version, 'origin': 'noble-updates/main', 'dpkg_verify': 'OK'}
    deb = Path('/var/cache/apt/archives/containerd_' + version.replace(':', '%3a') + '_amd64.deb')
    if deb.is_file():
        lists = sorted(Path('/var/lib/apt/lists').glob('*_dists_noble-updates_main_binary-amd64_Packages'))
        expected = packages_sha256([x.read_text(errors='replace') for x in lists], 'containerd', version)
        actual = hashlib.sha256(deb.read_bytes()).hexdigest()
        assert expected is not None, 'index Packages non comprimé introuvable pour containerd ' + version
        assert actual == expected, 'sha256 du .deb containerd ≠ index Packages'
        proof['deb_sha256'] = actual
    else:
        proof['deb_sha256'] = 'non comparé : .deb absent du cache apt'
    return proof


def ports_and_guest_gate():
    """O7 (#200904) : les ports PG n'écoutent que sur 127.0.0.1 ; la garde d'invités les refuse (lecture)."""
    rows = command(['ss', '-ltnH']).stdout.decode().splitlines()
    seen = {}
    for row in rows:
        local = row.split()[3]
        host, _, port = local.rpartition(':')
        if port in PG_PORTS:
            seen.setdefault(port, set()).add(host)
    assert set(seen) == PG_PORTS, 'ports PG manquants : %s' % sorted(PG_PORTS - set(seen))
    assert all(h == {'127.0.0.1'} for h in seen.values()), 'port PG lié hors de 127.0.0.1 : %s' % seen
    text = GUEST_FIREWALL.read_text()
    denied = set(re.findall(r'\b(\d{4,5})\b', ' '.join(re.findall(r'tcp dport \{([^}]*)\} reject', text))))
    assert PG_PORTS <= denied, 'garde d invités : ports PG non refusés'
    return {'listen': {k: sorted(v) for k, v in seen.items()}, 'guest_gate_denies': sorted(PG_PORTS)}


def inspecter():
    common_checks()
    facts = prerequisites()
    pgs = pg_sources()
    procs = processes()
    record = {'lot': 6321, 'at': stamp(), 'mutations': False, 'host': HOST,
              'docker_members': grp.getgrnam('docker').gr_mem, 'uid1001_processes': len(procs),
              'uid1001_with_112': sum(112 in p['groups'] for p in procs),
              'uid1001_outside_user_manager': [p['name'] for p in procs if 'user@1001.service' not in p['cgroup']],
              'rootless_socket_exists': Path('/run/user/1001/docker.sock').exists(),
              'pg': [{k: p[k] for k in ('name', 'volume', 'port', 'restart', 'health', 'running')} for p in pgs],
              'free_GiB': round(os.statvfs(HOME).f_bavail * os.statvfs(HOME).f_frsize / 2**30, 1), **facts}
    if ROOT.exists():
        write_private(ROOT / 'inspection.json', (json.dumps(record, indent=2) + '\n').encode())
    print(json.dumps(record, indent=1))


def phase_a(apply):
    common_checks(apply)
    prerequisites()
    pgs = pg_sources()
    assert all(p['running'] and p['health'] == 'healthy' for p in pgs), 'PG source non sain'
    assert not Path('/run/user/1001/docker.sock').exists(), 'Moteur rootless déjà présent : réconcilier'
    for d in ['.local/share/docker', '.config/docker/daemon.json']:
        assert not Path(HOME, d).exists(), d + ' existe déjà'
    need = sum(json.loads(docker(RF, 'image', 'inspect', i).stdout)[0]['Size'] for i in set(IMAGES.values()))
    need += sum(int(command(['du', '-sb', p['source_path']]).stdout.split()[0]) for p in pgs)
    free = os.statvfs(HOME).f_bavail * os.statvfs(HOME).f_frsize
    assert free > need + 10 * 2**30, 'Espace insuffisant ; aucune purge automatique'
    plan = {'pg': [p['name'] + '→rootless:' + p['port'] for p in pgs], 'images': sorted(IMAGES), 'need_GiB': round(need / 2**30, 1),
            'free_GiB': round(free / 2**30, 1)}
    if not apply:
        print(json.dumps({'phase': 'A', 'à_blanc': True, 'plan': plan}, indent=1))
        return
    outside_cutoffs()
    authority('phase-A', ['native_AG3_drain_active', 'no_active_turns', 'no_unapproved_personal_session',
                          'no_staging_in_progress'])
    assert not (ROOT / 'phase-A-progress.json').exists(), 'Tentative antérieure : réconcilier, jamais rejouer'
    record = {'lot': 6321, 'phase': 'start', 'plan': plan, 'group_removal_performed': False}

    def progress(phase, **f):
        record.update(f, phase=phase, at=stamp())
        write_private(ROOT / 'phase-A-progress.json', (json.dumps(record, indent=2) + '\n').encode())

    progress('prérequis vérifiés')
    for p in pgs:
        write_private(ROOT / ('source-' + p['name'] + '.private.json'), json.dumps(inspect(RF, p['name'])).encode())
    snapshot = r'''import base64,json,os,pathlib,stat
h=pathlib.Path.home(); names=['.docker/config.json','.config/docker/daemon.json','.config/ameesh/env']; e=h/'.config/environment.d'
if e.is_dir(): names.extend(str(p.relative_to(h)) for p in e.iterdir())
out={}
for name in names:
 p=h/name
 try:s=p.lstat()
 except FileNotFoundError:out[name]={'type':'absent'};continue
 row={'mode':stat.S_IMODE(s.st_mode)}
 if stat.S_ISLNK(s.st_mode):row.update(type='symlink',target=os.readlink(p))
 elif stat.S_ISREG(s.st_mode):row.update(type='file',data=base64.b64encode(p.read_bytes()).decode())
 else:raise RuntimeError('type inattendu')
 out[name]=row
print(json.dumps(out))'''
    write_private(ROOT / 'home-config-before.private.json', user(['/usr/bin/python3', '-I', '-c', snapshot]).encode())
    for op, a in [('enabled', ['list-unit-files', '--no-pager', '--output=json']),
                  ('active', ['list-units', '--all', '--no-pager', '--output=json'])]:
        write_private(ROOT / ('user-units-before-' + op + '.private.json'), user(['systemctl', '--user', *a]).encode())
    write_private(ROOT / 'docker-context-before.json', user(['docker', 'context', 'show']).encode())
    progress('instantanés pris ; moteur rootless natif sous UID1001')
    configure = ("import json,pathlib,os,sys;d=pathlib.Path.home()/'.config/docker';d.mkdir(parents=True,exist_ok=True);"
                 "p=d/'daemon.json';assert not p.exists();p.write_text(json.dumps(%r));os.chmod(p,0o600)" % ADDRESS_POOL)
    user(['/usr/bin/python3', '-I', '-c', configure])
    context = user(['/usr/bin/mktemp', '-d', HOME + '/.docker-operator-6321-XXXXXX'])
    record['installer_context'] = context
    command(user_args(['/usr/local/bin/dockerd-rootless-setuptool.sh', 'install', '--force', '--skip-iptables'],
                      extra=['DOCKER_CONFIG=' + context]), timeout=240)
    # Leçon N2 #5781 : le contexte jetable n'est jamais conservé.
    user(['/usr/bin/rm', '-rf', '--', context])
    assert user(['systemctl', '--user', 'show', 'docker.service', '-p', 'UnitFileState', '--value']) == 'enabled'
    info = json.loads(docker(RL, 'info', '--format', '{{json .}}').stdout)
    assert any('rootless' in x for x in info['SecurityOptions']) and info['Driver'] != 'vfs'
    assert info['DockerRootDir'] == HOME + '/.local/share/docker'
    assert user(['docker', 'context', 'show']) == (ROOT / 'docker-context-before.json').read_text(), 'contexte ordinaire changé'
    progress('moteur rootless actif ; images chargées par identifiant')
    for image, ident in IMAGES.items():
        stream(['docker', '--host', RF, 'save', ident], ['docker', '--host', RL, 'load'], 'image-' + image.replace('/', '_').replace(':', '_'))
        docker(RL, 'tag', ident, image)
    baselines, targets = {}, {}
    for p in pgs:
        baselines[p['name']] = database_baseline(RF, p['name'], p['role'])
    write_private(ROOT / 'database-baseline.private.json', json.dumps(baselines).encode())
    for p in pgs:
        name = p['name']
        progress('PG ' + name + ' : arrêt de la source, copie, démarrage rootless')
        existing = [json.loads(x) for x in docker(RL, 'volume', 'ls', '--format', '{{json .Name}}').stdout.decode().splitlines() if x]
        assert p['volume'] not in existing, 'Volume cible existant : réconcilier sans écraser'
        docker(RL, 'volume', 'create', '--label=ameesh.operator=6321', p['volume'])
        docker(RF, 'stop', '--time=60', name, timeout=90)
        assert not inspect(RF, name)['State']['Running']
        record.setdefault('sources_stopped', []).append(name)
        stream(['docker', '--host', RF, 'run', '--rm', '--network=none', '--label=ameesh.operator=6321',
                '--mount', 'type=volume,src=' + p['volume'] + ',dst=/data,readonly',
                '--entrypoint=/bin/tar', p['image'], '--numeric-owner', '-C', '/data', '-cpf', '-', '.'],
               ['docker', '--host', RL, 'run', '--rm', '-i', '--network=none', '--label=ameesh.operator=6321',
                '--mount', 'type=volume,src=' + p['volume'] + ',dst=/data',
                '--entrypoint=/bin/tar', p['image'], '--numeric-owner', '-C', '/data', '-xpf', '-'], 'copie-' + name)
        sh, sm = volume_manifest(RF, p['image'], p['volume'])
        th, tm = volume_manifest(RL, p['image'], p['volume'])
        assert sh == th and sm == tm, name + ' : octets/propriétaires différents'
        envfile = user_json("import pathlib,json,sys,os,tempfile;v=json.load(sys.stdin);fd,n=tempfile.mkstemp(prefix='.pg-env-6321-',dir=pathlib.Path.home());"
                            "os.fchmod(fd,0o600);f=os.fdopen(fd,'w');f.write('\\n'.join(v)+'\\n');f.close();print(n)", p['env'])
        hc = p['healthcheck'] or {}
        health = (['--health-cmd=' + ' '.join(hc['Test'][1:])] if hc.get('Test', [''])[0] == 'CMD-SHELL'
                  else ['--health-cmd=pg_isready -U ' + p['role'] + ' -d postgres'])
        try:
            created = docker(RL, 'run', '-d', '--name=' + name, '--restart=unless-stopped', '--label=ameesh.operator=6321',
                             '--env-file', envfile, '-p', '127.0.0.1:' + p['port'] + ':5432',
                             '--mount', 'type=volume,src=' + p['volume'] + ',dst=/var/lib/postgresql/data',
                             *health, '--health-interval=2s', '--health-timeout=3s', '--health-retries=60',
                             IMAGES['postgres:17'], *p['cmd']).stdout.decode().strip()
        finally:
            user(['/usr/bin/rm', '-f', '--', envfile])
        targets[name] = created
        deadline = time.monotonic() + 150
        while inspect(RL, name)['State'].get('Health', {}).get('Status') != 'healthy':
            assert time.monotonic() < deadline, name + ' : santé cible non atteinte ; inspecter avant tout rejeu'
            time.sleep(2)
        assert database_baseline(RL, name, p['role']) == baselines[name], name + ' : bases/schema_migrations différents'
        assert inspect(RL, name)['Image'] == p['image']
        progress('PG ' + name + ' migré', targets=targets)
    progress('phase A terminée ; groupe, client et gestionnaire inchangés', targets=targets,
             sources_retained_days_at_least=7, sources_restart_policy='no (inchangée)')
    print(json.dumps(record))


def phase_b(apply):
    common_checks(apply)
    a = json.loads((ROOT / 'phase-A-progress.json').read_text()) if (ROOT / 'phase-A-progress.json').exists() else {}
    assert a.get('phase', '').startswith('phase A terminée') or not apply, 'Phase A non terminée'
    assert grp.getgrnam('docker').gr_gid == 112 and 'ameesh' in grp.getgrnam('docker').gr_mem
    before = processes()
    outside = [x for x in before if 'user@1001.service/' not in x['cgroup']]
    assert not outside, 'Processus UID1001 hors gestionnaire : réconcilier (aucun kill)'
    # Leçon N8 #5781 (et incident du relevé 6321 du 08/11 13:42Z) : AUCUNE commande podman lancée depuis
    # root, même en lecture, car elle crée un « podman pause » qui garde le groupe 112. On lit le
    # registre de conteneurs du stockage rootless de Podman, en fichier.
    store = Path(HOME, '.local/share/containers/storage/overlay-containers/containers.json')
    podman_containers = json.loads(store.read_text()) if store.exists() else []
    assert not podman_containers, 'Charge Podman de UID1001 : réconcilier'
    pauses = [x for x in before if x['name'] == 'podman' and 'podman-pause' in x['cgroup']]
    assert all('user@1001.service/' in x['cgroup'] for x in pauses), 'pause Podman hors gestionnaire'
    if not apply:
        print(json.dumps({'phase': 'B', 'à_blanc': True, 'uid1001_processes': len(before), 'outside': len(outside),
                          'gestes': ['DOCKER_HOST rootless dans ~/.config/ameesh/env et environment.d/90-ameesh-docker-rootless.conf',
                                     'gpasswd --delete ameesh docker', 'systemctl restart user@1001.service',
                                     'unités natives relancées depuis la liste de l autorité']}, indent=1))
        return
    outside_cutoffs()
    proof = authority('phase-B', ['native_AG3_drain_active', 'no_active_turns', 'no_unapproved_personal_session',
                                  'no_staging_in_progress'])
    assert not (ROOT / 'phase-B-progress.json').exists(), 'Tentative antérieure : réconcilier'
    for name in a['targets']:
        assert inspect(RL, name)['State']['Health']['Status'] == 'healthy'
    approved = set(proof['approved_active_units'])
    for row in before:
        if '/init.scope' in row['cgroup']:
            continue
        units = re.findall(r'/([^/]+\.(?:service|scope))', row['cgroup'])
        assert any(u in approved for u in units), 'Unité/session UID1001 non approuvée'
    record = {'lot': 6321, 'phase': 'start', 'docker_group_removed': False}

    def save(phase, **f):
        record.update(f, phase=phase, at=stamp())
        write_private(ROOT / 'phase-B-progress.json', (json.dumps(record, indent=2) + '\n').encode())

    save('préalables B vérifiés', before_processes=len(before))
    configure = r'''import json,os,pathlib,tempfile
h=pathlib.Path.home();env=h/'.config/ameesh/env';directory=h/'.config/environment.d';directory.mkdir(parents=True,exist_ok=True)
target=directory/'90-ameesh-docker-rootless.conf'
assert not target.exists() and not target.is_symlink()
def replace(path):
 lines=path.read_text().splitlines(keepends=True);out=[];found=False
 for line in lines:
  if line.partition('=')[0].strip()=='DOCKER_HOST':out.append('DOCKER_HOST=unix:///run/user/1001/docker.sock\n');found=True
  else:out.append(line)
 if not found:
  if out and not out[-1].endswith('\n'):out[-1]+='\n'
  out.append('DOCKER_HOST=unix:///run/user/1001/docker.sock\n')
 fd,tmp=tempfile.mkstemp(prefix='.6321-',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as f:f.write(''.join(out));f.flush();os.fsync(f.fileno())
  os.chmod(tmp,0o600);os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
replace(env)
fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:f.write('DOCKER_HOST=unix:///run/user/1001/docker.sock\n');f.flush();os.fsync(f.fileno())
print(json.dumps({'new_files':['.config/environment.d/90-ameesh-docker-rootless.conf']}))'''
    save('client mis à jour sous UID1001', **json.loads(user(['/usr/bin/python3', '-I', '-c', configure])))
    command(['gpasswd', '--delete', 'ameesh', 'docker'])
    assert 'ameesh' not in grp.getgrnam('docker').gr_mem
    save('groupe docker retiré ; redémarrage du gestionnaire', docker_group_removed=True, manager_restart_requested=True)
    command(['systemctl', 'restart', 'user@1001.service'], timeout=180)
    assert command(['systemctl', 'is-active', 'user@1001.service']).stdout.strip() == b'active'
    for unit in sorted(proof['restore_active_native_units']):
        assert UNIT_RE.fullmatch(unit)
        if user(['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '--value']) != 'active':
            user(['systemctl', '--user', 'start', unit], timeout=120)
    after = processes()
    # Critère 1 (O4 #200904) : aucun processus uid 1001 avec 112, pause Podman du 08/11 13:42Z compris.
    assert not any(112 in x['groups'] for x in after), 'Processus UID1001 avec le groupe 112 (pause Podman compris)'
    forbidden = {'sudo', 'wheel', 'admin', 'adm', 'docker', 'lxd', 'libvirt', 'systemd-journal'}
    groups = [grp.getgrgid(g).gr_name for g in os.getgrouplist('ameesh', 1001)]
    assert not forbidden.intersection(groups)
    assert deny_rootful_as_uid1001()['denied'], 'Socket rootful encore accessible'
    for name in a['targets']:
        assert inspect(RL, name)['State']['Health']['Status'] == 'healthy', name + ' après redémarrage'
    assert user(['docker', 'context', 'show']) == (ROOT / 'docker-context-before.json').read_text()
    save('phase B terminée ; SQL réel, doctor, tour d agent et attestation à suivre',
         after_processes=len(after), fresh_groups=groups, rootful_socket_denied=True, rootless_PG_healthy=True)
    print(json.dumps(record))


def phase_c(apply, sha):
    common_checks(apply)
    b = json.loads((ROOT / 'phase-B-progress.json').read_text()) if (ROOT / 'phase-B-progress.json').exists() else {}
    assert b.get('phase', '').startswith('phase B terminée') or not apply
    if not apply:
        print(json.dumps({'phase': 'C', 'à_blanc': True, 'vérifications': [
            'sudo -l refusé', 'montage protégé illisible depuis un conteneur rootless',
            'test-parallele.sh -n 1 -p psql --liste « test_work » dans une unité utilisateur (environment.d effectif)',
            'ameesh doctor', 'ameesh menage --dry-run --json', 'ss -ltn : 55432, 55441-55443 sur 127.0.0.1 seulement', 'garde d invités : ports PG refusés (firewall.nft, lecture)', 'porte d hôte ouverte et 8 runners revenus (lecture)']}, indent=1))
        return
    assert re.fullmatch(r'[0-9a-f]{40}', sha)
    authority('phase-C', ['native_AG3_drain_active', 'no_unapproved_personal_session', 'release_tag_and_test_source_sha_verified'])
    assert not (ROOT / 'phase-C-progress.json').exists()
    record = {'lot': 6321, 'test_source_sha': sha, 'drain_lifted': False}

    def save(phase, **f):
        record.update(f, phase=phase, at=stamp())
        write_private(ROOT / 'phase-C-progress.json', (json.dumps(record, indent=2) + '\n').encode())

    sudo = command(['env', 'LC_ALL=C', 'sudo', '-n', '-l', '-U', 'ameesh'], allow_error=True)
    message = (sudo.stdout + sudo.stderr).decode()
    assert 'not allowed' in message and 'NOPASSWD:' not in message
    fixture = ROOT / 'protected-read-fixture'
    fd = os.open(fixture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(b'fixture operateur 6321\n')
    probe = docker(RL, 'run', '--rm', '--network=none', '--mount', 'type=bind,src=' + str(fixture) + ',dst=/protected,readonly',
                   '--entrypoint=/bin/cat', IMAGES['postgres:17'], '/protected', allow_error=True)
    assert probe.returncode != 0 and not probe.stdout, 'Conteneur rootless lit un fichier root 0600'
    save('sudo refusé, lecture protégée refusée')
    prepare = "import pathlib,tempfile;d=pathlib.Path.home()/'.local/state/ameesh/operators';d.mkdir(parents=True,exist_ok=True);print(tempfile.mkdtemp(prefix='6321-sql-',dir=d))"
    workdir = user(['/usr/bin/python3', '-I', '-c', prepare])
    archive = command(user_args(['git', '--no-replace-objects', '-C', HOME + '/work/ameesh-release', '-c', 'core.hooksPath=/dev/null',
                                 'archive', '--format=tar', sha]), timeout=90).stdout
    extract = r'''import io,tarfile,pathlib,sys
p=pathlib.Path(sys.argv[1]);assert p.is_dir()
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())) as t:t.extractall(p,filter='data')
assert (p/'tests/test_work.py').is_file() and (p/'scripts/test-parallele.sh').is_file()
(p/'.operator-6321-modules.txt').write_text('test_work\n')'''
    command(user_args(['/usr/bin/python3', '-I', '-c', extract, workdir]), data=archive, timeout=120)

    def unit(name, args, timeout=600):
        return command(user_args(['systemd-run', '--user', '--pipe', '--wait', '--collect', '--quiet',
                                  '--unit=ameesh-operator-6321-' + name, '--property=WorkingDirectory=' + workdir,
                                  '--property=EnvironmentFile=' + HOME + '/.config/ameesh/env',
                                  '--setenv=PATH=' + HOME + '/.local/share/ameesh/venv/bin:/usr/local/bin:/usr/bin:/bin', *args]),
                       timeout=timeout, allow_error=True)

    sql = unit('real-sql', ['/bin/bash', workdir + '/scripts/test-parallele.sh', '-n', '1', '-p', 'psql',
                            '--liste', workdir + '/.operator-6321-modules.txt'], timeout=1800)
    write_private(ROOT / 'phase-C-SQL.private.log', sql.stdout + sql.stderr)
    assert sql.returncode == 0 and b'base(s)' in sql.stdout and b'OK' in sql.stdout, 'Module SQL réel en échec'
    binary = HOME + '/.local/share/ameesh/venv/bin/ameesh'
    doctor = unit('doctor', [binary, 'doctor'], timeout=180)
    write_private(ROOT / 'phase-C-doctor.private.log', doctor.stdout + doctor.stderr)
    assert doctor.returncode == 0, 'doctor en échec : drainage maintenu'
    menage = unit('menage-readonly', [binary, 'menage', '--dry-run', '--json'], timeout=600)
    write_private(ROOT / 'phase-C-menage.private.json', menage.stdout)
    assert menage.returncode == 0
    json.loads(menage.stdout)
    ports = ports_and_guest_gate()
    save('ports et garde d invités vérifiés', ports_guest_gate=ports)
    save('vérifications locales passées ; levée du drainage, tour réel et attestation à suivre',
         real_SQL_psql_passed=True, doctor_passed=True, housekeeping_dryrun_passed=True, test_workdir=workdir)
    print(json.dumps(record))


def retour(apply):
    """Retour avant toute clé : moteur rootless arrêté, configuration et groupe restaurés, PG rootful relancés."""
    common_checks(apply)
    a = json.loads((ROOT / 'phase-A-progress.json').read_text()) if (ROOT / 'phase-A-progress.json').exists() else {}
    b = json.loads((ROOT / 'phase-B-progress.json').read_text()) if (ROOT / 'phase-B-progress.json').exists() else {}
    plan = {'phase_A': a.get('phase'), 'phase_B': b.get('phase'), 'sources_stopped': a.get('sources_stopped', [])}
    if not apply:
        print(json.dumps({'retour': 'à blanc', **plan}, indent=1))
        return
    proof = authority('retour', ['native_AG3_drain_active', 'no_active_turns', 'no_unapproved_personal_session',
                                 'target_writes_reconciled'])
    assert not (ROOT / 'retour-progress.json').exists(), 'Retour antérieur : réconcilier'
    record = {'lot': 6321, 'kind': 'retour avant clé'}

    def save(phase, **f):
        record.update(f, phase=phase, at=stamp())
        write_private(ROOT / 'retour-progress.json', (json.dumps(record, indent=2) + '\n').encode())

    save('autorité vérifiée', **plan)
    if Path('/run/user/1001/docker.sock').exists():
        names = docker(RL, 'ps', '-a', '--format', '{{.Names}}').stdout.decode().split()
        assert set(names) <= set(a.get('targets', {})), 'Charge rootless inconnue : réconcilier'
        for name in names:
            if inspect(RL, name)['State']['Running']:
                docker(RL, 'stop', '--time=60', name, timeout=90)
    if user(['systemctl', '--user', 'show', 'docker.service', '-p', 'LoadState', '--value']) != 'not-found':
        user(['systemctl', '--user', 'disable', '--now', 'docker.service'], timeout=120)
    save('moteur rootless arrêté ; volumes rootless conservés')
    snap = ROOT / 'home-config-before.private.json'
    if snap.exists():
        restore = r'''import base64,json,os,pathlib,tempfile,sys
p=json.load(sys.stdin);h=pathlib.Path.home()
def remove(path):
 if path.is_symlink() or path.is_file():path.unlink()
 elif path.exists():raise RuntimeError('répertoire inattendu')
extra=h/'.config/environment.d/90-ameesh-docker-rootless.conf'
if extra.exists() and '.config/environment.d/90-ameesh-docker-rootless.conf' not in p:
 assert extra.read_bytes()==b'DOCKER_HOST=unix:///run/user/1001/docker.sock\n';remove(extra)
for name,row in p.items():
 path=h/name
 if row['type']=='absent':remove(path);continue
 if row['type']=='symlink':remove(path);os.symlink(row['target'],path);continue
 path.parent.mkdir(parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(prefix='.6321-retour-',dir=path.parent)
 try:
  with os.fdopen(fd,'wb') as out:out.write(base64.b64decode(row['data']));out.flush();os.fsync(out.fileno())
  os.chmod(tmp,row['mode']);os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
print(json.dumps({'restored':True}))'''
        assert json.loads(user_json(restore, json.loads(snap.read_text())))['restored']
    if b.get('docker_group_removed'):
        command(['gpasswd', '--add', 'ameesh', 'docker'])
        assert 'ameesh' in grp.getgrnam('docker').gr_mem
        command(['systemctl', 'restart', 'user@1001.service'], timeout=180)
        assert command(['systemctl', 'is-active', 'user@1001.service']).stdout.strip() == b'active'
    save('configuration et groupe restaurés')
    baselines = json.loads((ROOT / 'database-baseline.private.json').read_text()) if (ROOT / 'database-baseline.private.json').exists() else {}
    for name in a.get('sources_stopped', []):
        src = inspect(RF, name)
        assert src['HostConfig']['RestartPolicy']['Name'] == 'no'
        if not src['State']['Running']:
            docker(RF, 'start', name)
        deadline = time.monotonic() + 150
        while inspect(RF, name)['State'].get('Health', {}).get('Status') != 'healthy':
            assert time.monotonic() < deadline, name + ' : santé source non revenue'
            time.sleep(2)
        if name in baselines:
            role = dict(x.split('=', 1) for x in src['Config']['Env'] if '=' in x).get('POSTGRES_USER', 'postgres')
            assert database_baseline(RF, name, role) == baselines[name], name + ' : écart de base au retour'
    for unit in proof.get('restore_active_native_units', []):
        assert UNIT_RE.fullmatch(unit)
        if user(['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '--value']) != 'active':
            user(['systemctl', '--user', 'start', unit], timeout=120)
    save('retour terminé ; PG rootful sains, configuration d origine, groupe docker rétabli')
    print(json.dumps(record))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('commande', choices=['inspecter', 'phase-a', 'phase-b', 'phase-c', 'retour'])
    p.add_argument('--apply', action='store_true')
    p.add_argument('--sha', default='')
    a = p.parse_args()
    if a.commande == 'inspecter':
        assert not a.apply
        inspecter()
    elif a.commande == 'phase-a':
        phase_a(a.apply)
    elif a.commande == 'phase-b':
        phase_b(a.apply)
    elif a.commande == 'phase-c':
        phase_c(a.apply, a.sha)
    else:
        retour(a.apply)


if __name__ == '__main__':
    main()
