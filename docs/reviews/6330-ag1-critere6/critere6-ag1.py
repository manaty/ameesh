#!/usr/bin/env python3
"""#6330 — critère 6 de #5781 revérifié sur AG1 : lecture protégée refusée depuis un conteneur
rootless de l'uid ameesh (1001).

Delta de `protected_read_probe()` de #6321 v4.1 (a314048c, l. 328-353), adapté à AG1, avec les deux
ajouts exigés par le lot (mesh-design, go #6330) :
  (a) témoin positif : un fichier 0644 voisin, dans le même dossier root 0711, monté pareil, doit
      être LU (stdout exact) ;
  (b) preuve du montage : `ls -ln` des deux points de montage dans le conteneur.
Inchangé : dossier root 0711 créé puis retiré, fixture root 0600 en O_EXCL, --network=none,
montage bind readonly, KO sur toute erreur de montage ou de démarrage.

Exécuté SUR AG1, en root. Aucun geste sur les unités ni sur les groupes. Reçu JSON sur stdout et
dans /root/ameesh-operator-6330-AG1/critere6-receipt.json (0600).
Codes : 0 = critère prouvé ; 1 = preuve incomplète ; 3 = INCIDENT (le 0600 a été lu : drainer AG1).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOST = 'ameesh-agents-1'
FIXTURE_DIR = Path('/var/lib/ameesh-operator-6330-fixture')
RECEIPTS = Path('/root/ameesh-operator-6330-AG1')
IMAGE = 'postgres:17'
PROTECTED = b'fixture operateur 6330\n'
VISIBLE = b'temoin positif 6330\n'
MOUNT_ERRORS = ('error mounting', 'OCI runtime', 'invalid mount', 'no such file or directory',
                'Error response from daemon')


def docker(*args):
    env = ['PATH=/usr/bin:/bin', 'HOME=/home/ameesh', 'XDG_RUNTIME_DIR=/run/user/1001',
           'DOCKER_HOST=unix:///run/user/1001/docker.sock']
    return subprocess.run(['runuser', '-u', 'ameesh', '--', 'env', '-i', *env, 'docker', *args],
                          capture_output=True, timeout=120)


def run(entrypoint, *argv):
    return docker('run', '--rm', '--network=none', '--label=ameesh.operator=6330',
                  '--mount', 'type=bind,src=' + str(FIXTURE_DIR / 'protected') + ',dst=/protected,readonly',
                  '--mount', 'type=bind,src=' + str(FIXTURE_DIR / 'visible') + ',dst=/visible,readonly',
                  '--entrypoint=' + entrypoint, IMAGE, *argv)


def write(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)
    os.chmod(path, mode)


def probe():
    assert os.geteuid() == 0 and os.uname().nodename == HOST, 'root sur AG1 seulement'
    assert not FIXTURE_DIR.exists(), 'dossier de fixture déjà présent : réconcilier'
    receipt = {'lot': 6330, 'host': HOST, 'image': IMAGE,
               'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    FIXTURE_DIR.mkdir(mode=0o711)
    os.chmod(FIXTURE_DIR, 0o711)
    try:
        write(FIXTURE_DIR / 'protected', PROTECTED, 0o600)
        write(FIXTURE_DIR / 'visible', VISIBLE, 0o644)
        listing = run('/bin/ls', '-ln', '/protected', '/visible')
        positive = run('/bin/cat', '/visible')
        protected = run('/bin/cat', '/protected')
        out = {name: {'rc': r.returncode, 'stdout': r.stdout.decode(errors='replace'),
                      'stderr': r.stderr.decode(errors='replace')}
               for name, r in (('ls', listing), ('visible', positive), ('protected', protected))}
        receipt['runs'] = out
        if protected.stdout == PROTECTED:
            receipt['verdict'] = 'INCIDENT : fichier root 0600 lu depuis un conteneur rootless'
            return receipt, 3
        errors = [x for x in MOUNT_ERRORS for r in out.values() if x in r['stderr']]
        checks = {
            'mount_listed': listing.returncode == 0 and '/protected' in out['ls']['stdout']
                            and '/visible' in out['ls']['stdout'],
            'positive_read': positive.returncode == 0 and positive.stdout == VISIBLE,
            'protected_denied': protected.returncode == 1 and not protected.stdout
                                and 'Permission denied' in out['protected']['stderr'],
            'no_mount_error': not errors,
        }
        receipt['checks'] = checks
        ok = all(checks.values())
        receipt['verdict'] = 'OK : critère 6 prouvé' if ok else 'KO : preuve incomplète'
        return receipt, 0 if ok else 1
    finally:
        for name in ('protected', 'visible'):
            if (FIXTURE_DIR / name).exists():
                (FIXTURE_DIR / name).unlink()
        FIXTURE_DIR.rmdir()
        receipt['fixture_removed'] = not FIXTURE_DIR.exists()


if __name__ == '__main__':
    result, code = probe()
    RECEIPTS.mkdir(mode=0o700, exist_ok=True)
    path = RECEIPTS / 'critere6-receipt.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(json.dumps(result, ensure_ascii=False, indent=1) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=1))
    sys.exit(code)
