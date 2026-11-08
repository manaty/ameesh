"""#5495 P6 — installateur v3.1 (N d'impl-03 #200802 : retour vérifie l'arbre 5c1, tmp O_EXCL,
refus constant pour toute exception) ; v3 : aligne le runtime L2 (compte ameesh-staging, uid 980) d'AG1 ou d'AG2
sur le kit scellé de la version de flotte M, SANS première installation. Dérivé de
install-passive-runtime.py (phase 1 V2, 465180850fb1d24d) : mêmes gardes (autorité exacte et
fraîche, empreintes, extraction sûre, commandes sous 980 en environnement vide), mais :
  - le compte, le canon, la clé et la configuration privée existent déjà et ne sont pas touchés ;
  - bascule.py preparer (verrouillé : --lock --wheelhouse --manifeste --verificateur) puis
    activer --precedent 5c1e679 : venv-courant passe de venv-5c1e679 à venv-<M7>, le venv 5c1 reste ;
  - expected_sha256 réécrit dans la copie 980 de staging-executor.json ET dans
    /etc/ameesh/staging-submit.json (octets identiques) ; seuls les chemins du venv changent.
Jamais : minuterie activée, service démarré, clé, DSN, gestionnaire redémarré. Retour : --retour.
Usage (root sur l'hôte, dossier de reçus /root/ameesh-operator-5495-P6-<hôte> 0700 contenant
authority-runtime-p6.json et payload.tar.gz, protégés 0600) :
  python3.12 -I update-l2-runtime-v3.py --apply
  python3.12 -I update-l2-runtime-v3.py --apply --retour
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import time

HOST = os.uname().nodename
ROOT = Path('/root/ameesh-operator-5495-P6-' + HOST)
HOME = Path('/home/ameesh-staging')
REPO = HOME / 'staging/ameesh'
CONF = HOME / '.config/ameesh/staging-executor.json'
SUBMIT = Path('/etc/ameesh/staging-submit.json')
STABLE = HOME / '.local/share/ameesh/venv'
# Kit de M (P6, préparé sur BG le 08/11, build-receipt.json et SHA256SUMS du kit).
SHA = '97285a076dee285097c412d3bd0f2649438a3257'
VERSION = SHA[:7]
PREVIOUS = '5c1e679'
TREE = 'c48d833b3a4e9239a21d1ed331b38633f4b66c36'
KIT_DIR = 'L2-review-97285a07-20261108'
KIT_SHA = '4143455e87bb07f32398efd5df49df3ca274c3a35e78e51b504f49fcf6784a57'
MANIFEST_SHA = '1ec6e407733f55e9bf03071779481b7cca1ec3237fdf579700928580ef2f07ed'
LOCK_SHA = '185d3bef81efd5c35b385b8705ca2b8885064ad3a3781e6b3c63bf12185cf039'
PACKAGE_SHA = '2dca0d97ba27576a6c6c624c711e8f3588c7f0cc87b2f20386dbf45636e8a633'
SOURCE_FILES = 3076
PREVIOUS_PACKAGE_SHA = '0d38f1e79912c1ad14e0fe6c038e259dbae500fa3c79034d37334696d6cb308f'
PYTHON_SHA = 'e50d468e8b0adfb05733f5b87b3cff34829c4a8c1aea50c865aa8bdfe4bb150f'
QUIESCENCE_SHA = 'f988ee7abd0b73335b58186ffc177b386dddbb8b13ef6d7d0dc35a91c44a3111'
QUIESCENCE = str(REPO / 'deploy/mise-a-jour/quiescence-staging.sh')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def protected(path, mode=0o600):
    s = path.lstat()
    assert stat.S_ISREG(s.st_mode) and s.st_uid == 0 and s.st_nlink == 1
    assert stat.S_IMODE(s.st_mode) == mode


def record(name, value):
    fd = os.open(ROOT / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())


def user_args(args):
    return ['/usr/sbin/runuser', '-u', 'ameesh-staging', '--', '/usr/bin/env', '-i',
            'HOME=' + str(HOME), 'USER=ameesh-staging', 'LOGNAME=ameesh-staging',
            'PATH=/usr/bin:/bin', 'LANG=C.UTF-8', 'PYTHONNOUSERSITE=1',
            'PYTHONDONTWRITEBYTECODE=1', 'GIT_TERMINAL_PROMPT=0',
            'GIT_CONFIG_GLOBAL=/dev/null', 'GIT_CONFIG_SYSTEM=/dev/null', *args]


def call(args, label, user=False, timeout=120):
    root_env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root',
                'USER': 'root', 'LOGNAME': 'root', 'LANG': 'C.UTF-8'}
    r = subprocess.run(user_args(args) if user else args, capture_output=True,
                       text=True, cwd=HOME if user else ROOT, timeout=timeout, env=root_env)
    record(label + '.json', {'returncode': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr})
    assert r.returncode == 0, label
    return r.stdout.strip()


def unit_state(unit, prop='ActiveState'):
    return call(['/usr/bin/systemctl', '--user', '-M', 'ameesh-staging@', 'show', unit,
                 '-p', prop, '--value'], 'state-' + unit + '-' + prop + '-' + str(time.monotonic_ns()))


def quiet_l2():
    """L2 ne doit ni tourner ni pouvoir démarrer pendant l'alignement."""
    assert unit_state('ameesh-staging.service') == 'inactive', 'L2 service not inactive'
    assert unit_state('ameesh-staging.timer') == 'inactive', 'L2 timer not inactive'
    r = subprocess.run(['/usr/bin/systemctl', '--user', '-M', 'ameesh-staging@', 'is-enabled',
                        'ameesh-staging.timer'], capture_output=True, text=True)
    assert r.stdout.strip() == 'disabled', 'L2 timer not disabled'


def write_json_980(path, value):
    """Écriture atomique 0600 sous 980 (copie privée de l'exécuteur)."""
    data = json.dumps(value, indent=2) + '\n'
    tmp = str(path) + '.p6.tmp'
    r = subprocess.run(user_args(['/usr/bin/python3.12', '-I', '-c',
        'import os,sys\nfd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\n'
        'os.write(fd,sys.stdin.buffer.read());os.fsync(fd);os.close(fd);os.replace(sys.argv[1],sys.argv[2])',
        tmp, str(path)]), input=data.encode(), capture_output=True, cwd=HOME, timeout=30)
    assert r.returncode == 0, 'write ' + str(path)


def install_submit(source):
    tmp = SUBMIT.with_name(SUBMIT.name + '.p6.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'wb') as f:
        f.write(Path(source).read_bytes())
        f.flush()
        os.fsync(f.fileno())
    os.chown(tmp, 0, 0)
    tmp.chmod(0o644)
    os.replace(tmp, SUBMIT)


def extract(archive, destination):
    assert digest(archive) == KIT_SHA
    with tarfile.open(archive) as tf:
        roots = set()
        for m in tf.getmembers():
            assert m.isfile() or m.isdir()
            assert not m.name.startswith('/') and '..' not in Path(m.name).parts
            assert not (m.mode & 0o7000)
            roots.add(Path(m.name).parts[0])
        assert roots == {KIT_DIR}
        tf.extractall(destination, filter='data')
    top = destination / KIT_DIR
    for p in [top, *top.rglob('*')]:
        assert not p.is_symlink()
        os.chown(p, 980, 980)
        p.chmod(0o755 if p.is_dir() or p.stat().st_mode & 0o111 else 0o644)
    return top


def authority(scope):
    path = ROOT / 'authority-runtime-p6.json'
    protected(path)
    a = json.loads(path.read_text())
    assert a['scope'] == scope and a['host'] == HOST
    assert 0 <= time.time() - a['verified_epoch'] < 600 and a['expires_epoch'] > time.time()
    assert a['controller_sha256'] == digest(__file__)
    assert a['owner_decision'] == 184823
    assert a['pilot_go_mail'] > 0
    # Deux familles hors de l'auteur (claude) : codex et deepseek, identifiants de courrier réels.
    assert set(a['review_mails']) == {'codex', 'deepseek'} and all(v > 0 for v in a['review_mails'].values())
    assert a['auditor_requalification_mail'] > 0
    return a


def expected_for(version, package_sha):
    venv = HOME / ('.local/share/ameesh/venv-' + version)
    return {QUIESCENCE: QUIESCENCE_SHA,
            str(venv / 'bin/python'): PYTHON_SHA,
            str(venv / 'lib/python3.12/site-packages/ameesh'): package_sha}


def align(a):
    assert not (ROOT / 'started.json').exists(), 'inspect existing progress, never replay'
    quiet_l2()
    assert os.readlink(HOME / '.local/share/ameesh/venv-courant') == 'venv-' + PREVIOUS
    assert not (HOME / ('.local/share/ameesh/venv-' + VERSION)).exists()
    assert digest('/usr/bin/python3.12') == PYTHON_SHA
    before = json.loads(CONF.read_text())
    assert CONF.read_bytes() == SUBMIT.read_bytes(), 'executor config and submit copy differ'
    protected(ROOT / 'payload.tar.gz')
    packages = HOME / 'staging/packages'
    assert not (packages / KIT_DIR).exists()
    record('started.json', {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                           'pilot_go_mail': a['pilot_go_mail'], 'source': SHA, 'host': HOST,
                           'config_before_sha256': digest(CONF), 'no_credentials_or_key': True})
    (ROOT / 'staging-executor.json.before').write_bytes(CONF.read_bytes())
    (ROOT / 'staging-submit.json.before').write_bytes(SUBMIT.read_bytes())
    kit = extract(ROOT / 'payload.tar.gz', packages)
    assert digest(kit / 'manifest.json') == MANIFEST_SHA
    assert digest(kit / 'requirements-staging.lock') == LOCK_SHA
    call(['git', '-C', str(REPO), '-c', 'core.hooksPath=/dev/null', 'fetch', '--no-tags', 'origin',
          'refs/heads/main:refs/remotes/origin/main'], 'repo-fetch-public', user=True)
    call(['git', '-C', str(REPO), 'merge-base', '--is-ancestor', SHA, 'refs/remotes/origin/main'],
         'repo-main-ancestor', user=True)
    call(['git', '-C', str(REPO), '-c', 'core.hooksPath=/dev/null', 'checkout', '--quiet', '--detach', SHA],
         'repo-checkout', user=True)
    assert call(['git', '-C', str(REPO), 'rev-parse', 'HEAD^{tree}'], 'repo-tree', user=True) == TREE
    assert not call(['git', '-C', str(REPO), 'status', '--porcelain'], 'repo-clean', user=True)
    inv = json.loads((kit / 'source-file-inventory.json').read_text())
    assert inv['source_sha'] == SHA and len(inv['files']) == SOURCE_FILES
    for entry in inv['files']:
        assert '..' not in Path(entry['path']).parts and not Path(entry['path']).is_absolute()
        p = REPO / entry['path']
        assert p.is_file() and not p.is_symlink() and digest(p) == entry['sha256']
    for name in ['bascule.py', 'venv-staging.py']:
        assert (REPO / 'deploy/mise-a-jour' / name).read_bytes() == (kit / name).read_bytes()
    assert digest(QUIESCENCE) == QUIESCENCE_SHA
    call(['/usr/bin/python3.12', str(kit / 'bascule.py'), 'preparer', '--venv', str(STABLE), '--src', str(REPO),
          '--version', VERSION, '--python', '/usr/bin/python3.12', '--lock', str(kit / 'requirements-staging.lock'),
          '--wheelhouse', str(kit / 'wheelhouse'), '--manifeste', str(kit / 'manifest.json'),
          '--verificateur', str(kit / 'venv-staging.py')], 'native-preparer', user=True, timeout=300)
    quiet_l2()
    call(['/usr/bin/python3.12', str(kit / 'bascule.py'), 'activer', '--venv', str(STABLE),
          '--version', VERSION, '--precedent', PREVIOUS], 'native-activer', user=True)
    call(['/usr/bin/python3.12', str(kit / 'venv-staging.py'), 'verifier', '--manifeste', str(kit / 'manifest.json'),
          '--venv', str(HOME / ('.local/share/ameesh/venv-' + VERSION))], 'native-verifier', user=True)
    check = ('import json,ameesh; from ameesh.staging_executor import _tree_sha256; '
             'print(json.dumps({"tree":_tree_sha256(ameesh.__path__[0])}))')
    assert json.loads(call([str(STABLE / 'bin/python'), '-B', '-c', check], 'native-runtime-tree',
                           user=True))['tree'] == PACKAGE_SHA
    assert before['expected_sha256'] == expected_for(PREVIOUS, before['expected_sha256'][
        str(HOME / ('.local/share/ameesh/venv-' + PREVIOUS + '/lib/python3.12/site-packages/ameesh'))])
    after = dict(before, expected_sha256=expected_for(VERSION, PACKAGE_SHA))
    write_json_980(CONF, after)
    install_submit(CONF)
    assert CONF.read_bytes() == SUBMIT.read_bytes()
    quiet_l2()
    record('runtime-aligned.json', {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'host': HOST, 'source': SHA, 'venv_courant': 'venv-' + VERSION, 'previous': 'venv-' + PREVIOUS,
           'package_tree': PACKAGE_SHA, 'config_after_sha256': digest(CONF), 'timer_enabled': False,
           'service_started': False, 'key_or_DSN_touched': False,
           'retour': 'python3.12 -I update-l2-runtime-v3.py --apply --retour'})
    print(json.dumps({'aligned': True, 'host': HOST, 'venv': 'venv-' + VERSION,
                      'config_after_sha256': digest(CONF)}))


def back():
    assert (ROOT / 'started.json').exists(), 'nothing to return'
    quiet_l2()
    for name in ['staging-executor.json.before', 'staging-submit.json.before']:
        protected(ROOT / name)
    kit = HOME / 'staging/packages' / KIT_DIR
    call(['/usr/bin/python3.12', str(kit / 'bascule.py'), 'retour', '--venv', str(STABLE)],
         'native-retour-' + str(time.monotonic_ns()), user=True)
    assert os.readlink(HOME / '.local/share/ameesh/venv-courant') == 'venv-' + PREVIOUS
    check = ('import json,ameesh; from ameesh.staging_executor import _tree_sha256; '
             'print(json.dumps({"tree":_tree_sha256(ameesh.__path__[0])}))')
    assert json.loads(call([str(STABLE / 'bin/python'), '-B', '-c', check], 'native-retour-tree',
                           user=True))['tree'] == PREVIOUS_PACKAGE_SHA, 'previous runtime tree'
    # Le venv M et le kit restent sur place (preuve, et réalignement sans reconstruction) ;
    # le ménage éventuel est un geste séparé.
    write_json_980(CONF, json.loads((ROOT / 'staging-executor.json.before').read_text()))
    install_submit(ROOT / 'staging-submit.json.before')
    assert CONF.read_bytes() == (ROOT / 'staging-executor.json.before').read_bytes()
    assert SUBMIT.read_bytes() == (ROOT / 'staging-submit.json.before').read_bytes()
    print(json.dumps({'returned': True, 'host': HOST, 'venv': 'venv-' + PREVIOUS, 'config_sha256': digest(CONF)}))


def main():
    if sys.flags.optimize:
        raise SystemExit('Refused: Python optimization removes required operator guards')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--retour', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        parser.error('operator proposal: --apply and fresh exact authority required')
    assert os.geteuid() == 0 and HOST in ('ameesh-agents-1', 'ameesh-agents-2')
    os.umask(0o077)
    s = ROOT.lstat()
    assert stat.S_ISDIR(s.st_mode) and s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o700
    if args.retour:
        authority('L2 runtime alignment P6 return')
        back()
    else:
        align(authority('L2 runtime alignment P6'))


if __name__ == '__main__':
    try:
        main()
    except AssertionError as exc:
        # Refus constant, sans trace ni sortie enfant (borne 4 de #195967).
        sys.stderr.write('P6 alignment refused: %s\n' % (exc.args[0] if exc.args and isinstance(exc.args[0], str) else 'guard'))
        raise SystemExit(1)
    except Exception as exc:  # délai, OSError, JSON… : même refus constant, nom du type seulement
        sys.stderr.write('P6 alignment refused: %s\n' % type(exc).__name__)
        raise SystemExit(1)
