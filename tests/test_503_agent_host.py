# SPDX-License-Identifier: AGPL-3.0-only
"""Agent-host deployment through fake SSH: no VM, unit or production database."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import unittest

from ameesh import platform

from tests import support  # isolate the workstation environment

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/mise-a-jour/vm.sh'

SSH = r'''#!/usr/bin/env python3
import os, signal, subprocess, sys
from pathlib import Path
if os.environ.get('FAKE_STALL_SSH') \
        and os.environ.get('FAKE_STALL_MATCH', '') in sys.argv[-1]:
    # #2198/N1 : lien vivant et muet ; le signal reçu par le client doit être
    # transmis à l'enfant (le vrai ssh). On le trace pour le banc.
    def _recu(signum, frame):
        if os.environ.get('FAKE_SIGNAL_FILE'):
            Path(os.environ['FAKE_SIGNAL_FILE']).write_text(str(signum))
        sys.exit(128 + signum)
    signal.signal(signal.SIGINT, _recu)
    signal.signal(signal.SIGTERM, _recu)
    # #3608 : le pid de cette session (l'enfant ssh vit hors du groupe bash)
    # permet au banc de tuer le dernier détenteur des tuyaux quand le client
    # est tué avant lui.
    if os.environ.get('FAKE_STALL_PID'):
        Path(os.environ['FAKE_STALL_PID']).write_text(str(os.getpid()))
    # Le témoin n'est posé qu'une fois les gestionnaires installés : le banc
    # peut alors envoyer le signal sans course.
    if os.environ.get('FAKE_STALL_STARTED'):
        Path(os.environ['FAKE_STALL_STARTED']).write_text('1')
    signal.pause()
state = Path(os.environ['FAKE_STATE'])
argv = sys.argv[1:]
command = argv[-1]
# The real client swallows its stdin even when the remote command does not
# read it: a caller that shares its stdin with a `while read` loop loses the
# rest of the list (#1455). Play that, and forward the bytes like ssh does.
# `-n` redirects the client's stdin from /dev/null, as the real one does.
stdin = ''
if '-n' not in argv and not sys.stdin.isatty():
    stdin = sys.stdin.read()
with (state / 'ssh.log').open('a') as f:
    f.write(command + '\n')
# #4358 (B mty-amh-impl-03) : transport SSH perdu (255) sur la lecture d'une
# persona ; le fichier nomme la persona.
coupe = state / 'ssh-255'
if coupe.exists() and (' show %s ' % coupe.read_text().strip()) in command:
    sys.exit(255)
command = command.replace('/home/ameesh/.local/share/ameesh/venv/bin/ameesh', os.environ['FAKE_AMEESH'])
command = command.replace('/home/ameesh/.local/share/ameesh/venv/bin/python', os.environ['FAKE_DB_PYTHON'])
# #3311 : le helper de preuve reçoit le venv stable de la VM en argument ; le
# banc le traduit vers son layout temporaire (les lanceurs ci-dessus sont déjà
# remplacés, donc cette passe ne les touche plus).
command = command.replace('/home/ameesh/.local/share/ameesh', os.environ['FAKE_SHARE'])
command = command.replace('$HOME/.config/ameesh/env', str(state / 'runner.env'))
env = dict(os.environ, FAKE_REMOTE='1')
sys.exit(subprocess.run(['bash', '-c', command], env=env, input=stdin, text=True).returncode)
'''
RUNUSER = r'''#!/usr/bin/env python3
import os, sys
assert sys.argv[1:4] == ['-u', 'ameesh', '--'], sys.argv
os.execvp(sys.argv[4], sys.argv[4:])
'''
AMEESH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
assert os.environ.get('FAKE_REMOTE') == '1', 'registry must be read on the host'
assert os.environ.get('HOME') == '/home/ameesh'
assert os.environ.get('XDG_RUNTIME_DIR') == '/run/user/1001'
assert os.environ.get('DBUS_SESSION_BUS_ADDRESS') == 'unix:path=/run/user/1001/bus'
assert os.environ.get('AMEESH_DSN') == json.loads((state / 'expected-env.json').read_text())['AMEESH_DSN']
assert os.environ.get('AMEESH_SCHEMA') == 'test_runner_schema'
scenario = (state / 'scenario').read_text()
if sys.argv[1:] == ['resume', '--help']:
    # #2152 / R1 B2 : le script ne draine que si la reprise conditionnelle
    # existe côté cible (--expect-reason). Le drapeau simule un CLI antérieur.
    if (state / 'sans-expect-reason').exists():
        print('usage: ameesh resume ... --operator --reason R')
    else:
        print('usage: ameesh resume ... --operator --reason R --expect-reason TEXT')
    sys.exit(0)
if sys.argv[1:] == ['pause', '--help']:
    # #2184/R3 : sans --not-after, la pose n'a pas de barrière ; le script
    # désactive le drain plutôt que de poser sans échéance.
    if (state / 'sans-not-after').exists():
        print('usage: ameesh pause AGENT --reason R [--human H] [--json]')
    else:
        print('usage: ameesh pause AGENT --reason R [--human H] '
              '[--not-after EPOCH] [--json]')
    sys.exit(0)
if sys.argv[1:] == ['list', '--json']:
    if scenario == 'list-error': sys.exit(1)
    if scenario == 'bad-json': print('not json'); sys.exit(0)
    # #4373 : lecture vide passagère (code 0, aucune sortie), puis liste
    # valide à la relecture ; `list-vide` ne rend jamais la liste.
    if scenario == 'list-vide-puis-ok' and not (state / 'liste-vide-faite').exists():
        (state / 'liste-vide-faite').touch()
        sys.exit(0)
    if scenario == 'list-255-puis-ok' and not (state / 'liste-255-faite').exists():
        (state / 'liste-255-faite').touch()
        sys.exit(255)
    if scenario == 'list-vide':
        sys.exit(0)
    print((state / 'agents.json').read_text())
elif sys.argv[1] == 'show':
    name = sys.argv[2]
    # #2198/N4 : lecture lente ; la borne de lecture doit la couper avant la
    # borne (plus longue) des gestes de drain. #4528 : plus longue que cette
    # dernière, pour que seule la borne de lecture explique un balayage court.
    if scenario == 'show-lent':
        time.sleep(45)
    # #4358 (après l'ancre #2198/N4 que test_3114 remplace) : sortie VIDE, code 0 (staging h-07, relève du rôle mesh-design) ;
    # le fichier compte les lectures vides restantes, « toujours » ne
    # s'épuise jamais.
    vide = state / ('vide-' + name)
    if vide.exists():
        reste = vide.read_text().strip()
        if reste != 'toujours':
            if int(reste) <= 1:
                vide.unlink()
            else:
                vide.write_text(str(int(reste) - 1))
        sys.exit(0)
    row = next(a for a in json.loads((state / 'agents.json').read_text()) if a['name'] == name)
    row = dict(row, lease_owner='runner-' + name + '@host-agents', lease_epoch=7, lease_expires_ts=time.time()+300)
    pause = state / ('pause-' + name)
    motif = state / ('motif-' + name)
    if scenario == 'pause-coupee-etat-perdu' and name == 'dev9' and pause.exists() \
            and not (state / 'lecture-perdue').exists():
        (state / 'lecture-perdue').touch()
        sys.exit(1)
    # R1 B1 : après notre pose et avant notre reprise, le monde extérieur lève
    # notre drain puis pose une NOUVELLE pause (humaine, étrangère).
    if scenario == 'drain-remplace' and name == 'dev9' and motif.exists() \
            and '#2152' in motif.read_text() \
            and not (state / 'remplace-fait').exists():
        motif.write_text('pause humaine etrangere')
        (state / 'remplace-fait').touch()
    # N5 B2 : levée humaine de notre premier drain, l'agent restant en tour :
    # le second drain doit seul rester suivi (aucune entrée périmée).
    if scenario == 'chaines-relance' and name == 'dev9' and motif.exists() \
            and '#2152' in motif.read_text() \
            and not (state / 'levee-humaine').exists():
        pause.unlink(); motif.unlink(missing_ok=True)
        (state / 'levee-humaine').touch()
    if scenario == 'expired': row['lease_expires_ts'] = time.time()-1
    if scenario == 'no-lease': row['lease_owner'] = None
    # #4239 : résident expérimental sans bail, unité désactivée.
    if scenario == 'resident-sans-bail' and name == 'pilot-resident':
        row['lease_owner'] = None; row['lease_epoch'] = 0
        row['lease_expires_ts'] = None
    if scenario == 'foreign-owner': row['lease_owner'] = 'another-runner'
    if scenario == 'default-owner':
        # #3311 : le propriétaire par défaut est host:MainPID ; le banc fournit
        # un vrai processus vivant, plus le 123 historique.
        try:
            pid = (state / 'mainpid').read_text().strip()
        except OSError:
            pid = '123'
        row['lease_owner'] = 'host-agents:' + pid
    if scenario == 'db-ahead': row['lease_expires_ts'] = time.time()+30
    if scenario == 'moved-before': row['host'] = 'elsewhere'
    # #2184/N1 : le mode peut changer entre la liste et la relecture ; dev9
    # était execute au listage et passe externe au show.
    if scenario == 'mode-change' and name == 'dev9': row['mode'] = 'externe'
    # #2184/N1 + 0459 : le mode change alors qu'un drain est déjà posé ; la
    # relecture doit encore pouvoir constater et lever notre pause.
    if scenario == 'mode-change-apres-drain' and name == 'dev9' and pause.exists():
        row['mode'] = 'externe'
    if scenario == 'always-busy': row['status'] = 'running'
    # #3120/B2 : une première pose incertaine (rc124) peut encore aboutir ;
    # l'agent est alors vu en tour et sans pause à la relecture.
    if scenario in ('pose-incertaine', 'pose-incertaine-inverse'):
        agent_marque = state / 'pose-incertaine-agent'
        if agent_marque.exists() and name == agent_marque.read_text().strip() \
                and (state / 'pose-incertaine-attente').exists() and not pause.exists():
            row['status'] = 'running'
    # #2152 : un agent qui enchaîne les tours ne rend la main qu'une fois
    # drainé ; « chaines-toujours » ne la rend jamais.
    if name == 'dev9' and scenario in ('chaines', 'chaines-pause-error',
                                       'chaines-resume-error', 'pause-concurrente',
                                       'drain-remplace', 'pause-reponse-perdue',
                                       'pause-coupee', 'pause-coupee-etat-perdu',
                                       'pause-perdue-sans-pose', 'drain-puis-stop-error',
                                       'drain-refuse-identite', 'pause-expiree-tardive',
                                       'mode-change-apres-drain', 'resume-error-deux-fois') \
            and not pause.exists():
        row['status'] = 'running'
    if name == 'dev9' and scenario in ('chaines-toujours', 'chaines-relance'):
        row['status'] = 'running'
    if pause.exists():
        row['operator_paused'] = True
        row['operator_pause_reason'] = motif.read_text() if motif.exists() else ''
    if (state / ('restart-' + name)).exists():
        row['lease_epoch'] = 9 if scenario != 'stale-lease' else 7
        if scenario == 'moved': row['host'] = 'elsewhere'
        # #3412 : le rôle élu de dev8 est repris par un autre hôte candidat
        # (époque supérieure) ; ou son bail ne revient pas (vrai échec).
        if scenario == 'elu-ailleurs' and name == 'dev8':
            row['lease_owner'] = 'runner-dev8@other-host'
        if scenario == 'bail-perdu' and name == 'dev8':
            row['lease_epoch'] = 7
    if scenario == 'busy-once' and name == 'dev9' and not (state / 'busy-seen').exists():
        (state / 'busy-seen').touch(); row['status'] = 'running'
    print(json.dumps(row))
    # #3120/B3 : si la barrière d'une requête incertaine n'est pas tenue, elle
    # aboutit après la lecture de reprise et repose le motif, sans reprise.
    if scenario == 'pose-incertaine-inverse':
        agent_marque = state / 'pose-incertaine-agent'
        if agent_marque.exists() and name == agent_marque.read_text().strip() \
                and (state / ('pose-incertaine-resume-fait-' + name)).exists() \
                and not (state / 'pose-incertaine-late-fait').exists():
            hf = os.environ.get('HORLOGE_FICHIER')
            maintenant = float(Path(hf).read_text()) if hf and Path(hf).exists() else time.time()
            premiere = float((state / 'echeance-incertaine').read_text())
            if maintenant < premiere:
                motif.write_text((state / 'motif-incertain').read_text())
                pause.write_text('')
                with (state / 'sequence.log').open('a') as f:
                    f.write('pause %s\n' % name)
                (state / 'pose-incertaine-late-fait').touch()
    # #2184/R3 : un créateur tardif indépendant (systemd-run retardé) rejoue
    # la pose APRÈS la relecture. La base doit la refuser au-delà de
    # --not-after ; sinon la pause resterait orpheline.
    if scenario == 'pause-expiree-tardive' and (state / ('resume-seen-' + name)).exists() \
            and (state / ('motif-tardif-' + name)).exists() \
            and not (state / 'createur-tardif-fait').exists():
        (state / 'createur-tardif-fait').touch()
        echeance = (state / ('echeance-' + name)).read_text().strip()
        subprocess.run(
            [sys.argv[0], 'pause', name,
             '--reason', (state / ('motif-tardif-' + name)).read_text(),
             '--not-after', echeance, '--json'],
            env=dict(os.environ), capture_output=True, text=True)
elif sys.argv[1] in ('pause', 'resume'):
    # #2152 : les gestes du drain, avec le « changed » que lit le script.
    name = sys.argv[2]
    args = sys.argv[3:]
    pause = state / ('pause-' + name)
    motif = state / ('motif-' + name)
    with (state / 'operateur.log').open('a') as f:
        f.write('%s %s %s\n' % (sys.argv[1], name, ' '.join(args)))
    if '--reason' not in args: sys.exit(2)
    if sys.argv[1] == 'resume':
        (state / 'resume-seen').touch()
        (state / ('resume-seen-' + name)).touch()
    if sys.argv[1] == 'pause':
        # #2184/R3 : la base refuse une pose au-delà de --not-after. Le banc
        # compare à l'horloge injectée (HORLOGE_FICHIER) quand elle est posée.
        echeance = None
        if '--not-after' in args:
            echeance = float(args[args.index('--not-after') + 1])
        hf = os.environ.get('HORLOGE_FICHIER')
        maintenant = float(Path(hf).read_text()) if hf and Path(hf).exists() else time.time()
        # #3120/B2 : la première pose a expiré (rc124) sans muter ; elle peut
        # encore aboutir. Le rattrapage doit retrouver ce motif et le lever.
        if scenario in ('pose-incertaine', 'pose-incertaine-inverse'):
            agent_marque = state / 'pose-incertaine-agent'
            if agent_marque.exists() and name == agent_marque.read_text().strip():
                attente = state / 'pose-incertaine-attente'
                if not attente.exists():
                    (state / 'motif-incertain').write_text(
                        args[args.index('--reason') + 1])
                    (state / 'echeance-incertaine').write_text(str(echeance))
                    attente.touch()
                    sys.exit(124)
                if scenario == 'pose-incertaine' and not pause.exists():
                    premiere = float((state / 'echeance-incertaine').read_text())
                    if maintenant < premiere:
                        motif.write_text((state / 'motif-incertain').read_text())
                        pause.write_text('')
                        with (state / 'sequence.log').open('a') as f:
                            f.write('pause %s\n' % name)
                        print(json.dumps({'agent': name, 'mode': 'execute',
                                          'operator_paused': True,
                                          'changed': False, 'event_id': None}))
                        sys.exit(0)
        if echeance is not None and maintenant >= echeance:
            with (state / 'refus-tardif.log').open('a') as f:
                f.write('%s not_after\n' % name)
            print(json.dumps({'agent': name, 'mode': 'execute',
                              'operator_paused': pause.exists(),
                              'changed': False, 'event_id': None,
                              'refusal': 'not_after'}))
            sys.exit(0)
        if scenario == 'chaines-pause-error': sys.exit(1)
        if scenario == 'drain-refuse-identite':
            # Geste d'opérateur refusé : vm.sh tourne dans une session liée à
            # un agent (bad_actor), le CLI refuse AVANT toute mutation.
            print('ameesh operator pause: bad_actor: an agent session cannot act '
                  'as the local human', file=sys.stderr)
            sys.exit(1)
        if scenario == 'pause-perdue-sans-pose':
            # R1 B2 : réponse perdue, la pose n'a PAS abouti.
            print('{bad-json')
            sys.exit(0)
        if scenario == 'pause-expiree-tardive' and name == 'dev9':
            # Timeout de l'appel (rc 124) : la pose n'est pas encore commitée.
            # Le créateur tardif rejouera cette commande avec la même échéance.
            (state / ('motif-tardif-' + name)).write_text(
                args[args.index('--reason') + 1])
            if echeance is not None:
                (state / ('echeance-' + name)).write_text(str(echeance))
            sys.exit(124)
        motif.write_text(args[args.index('--reason') + 1])
        pause.touch()
        with (state / 'sequence.log').open('a') as f:
            f.write('pause %s\n' % name)
        if scenario == 'pause-concurrente':
            # Posée entre notre lecture et notre pause : elle n'est pas de nous.
            motif.write_text('pause humaine etrangere')
            change = False
        elif scenario == 'pause-reponse-perdue':
            # R1 B2 : la pose a abouti, mais la réponse est illisible.
            print('{bad-json')
            sys.exit(0)
        elif scenario in ('pause-coupee', 'pause-coupee-etat-perdu'):
            # R1 B2 : la pose a abouti, puis le lien SSH tombe (rc ≠ 0).
            sys.exit(1)
        else:
            change = True
        print(json.dumps({'agent': name, 'mode': 'execute', 'operator_paused': True,
                          'changed': change, 'event_id': 1}))
    else:
        if '--operator' not in args: sys.exit(2)
        if (state / 'sans-expect-reason').exists() and '--expect-reason' in args:
            sys.exit(2)
        if scenario == 'chaines-resume-error': sys.exit(1)
        if scenario == 'resume-error-deux-fois' and name == 'dev9':
            # #2198/N3 : les deux premières levées échouent (celle de la boucle
            # puis la première passe du trap) ; la seconde passe réussit.
            compteur = state / 'resume-compte'
            compte = int(compteur.read_text()) if compteur.exists() else 0
            compteur.write_text(str(compte + 1))
            if compte < 2:
                sys.exit(1)
        attendu = args[args.index('--expect-reason') + 1] if '--expect-reason' in args else None
        raison = motif.read_text() if motif.exists() else ''
        if pause.exists() and (attendu is None or raison == attendu):
            pause.unlink(); motif.unlink(missing_ok=True)
            if scenario == 'pose-incertaine-inverse':
                (state / ('pose-incertaine-resume-fait-' + name)).touch()
            print(json.dumps({'agent': name, 'mode': 'execute', 'operator_paused': False,
                              'changed': True, 'event_id': 2}))
        else:
            print(json.dumps({'agent': name, 'mode': 'execute',
                              'operator_paused': pause.exists(), 'changed': False,
                              'event_id': None}))
elif sys.argv[1] == 'doctor':
    print('doctor ok')
elif sys.argv[1:3] == ['role', 'timers']:
    # #3311 x #3327 : le contrôle du déploiement vérifie les minuteries de rôle
    # sous l'environnement du service (ameesh_agents) ; le banc l'accepte.
    with (state / 'role-timers.log').open('a') as f:
        f.write(' '.join(sys.argv[1:]) + '\n')
    print('minuteries de rôle ok')
elif sys.argv[1:3] == ['role', 'status']:
    # #3412 : rapport d'élection ; le banc le pose quand un rôle élu change
    # d'hôte candidat pendant le redémarrage.
    nom = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith('-') else None
    if nom is None: sys.exit(0)
    rapport = state / ('role-' + nom + '.json')
    print(rapport.read_text() if rapport.exists()
          else json.dumps({'name': nom, 'elected': False}))
else: sys.exit(2)
'''
SYSTEMCTL = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
assert args[0] == '--user', args
with (state / 'units.log').open('a') as f: f.write(' '.join(args) + '\n')
scenario = (state / 'scenario').read_text()
if 'list-units' in args:
    # #4248 : unités actives réelles de l'hôte (candidats d'un rôle élu
    # compris) ; sans fichier, celles des agents actifs du registre.
    assert args[1:] == ['list-units', 'ameesh-runner-agent@*.service',
                        '--state=active', '--plain', '--no-legend'], args
    if scenario == 'list-units-error': sys.exit(1)
    path = state / 'units-actives'
    if path.exists():
        for n in path.read_text().split():
            if not (state / ('stopped-' + n)).exists():
                print('ameesh-runner-agent@%s.service loaded active running runner' % n)
    sys.exit(0)
unit = args[-1]
name = unit.split('@')[1].removesuffix('.service')
def stopped():
    return (state / ('stopped-' + name)).exists()
if 'stop' in args:
    with (state / 'sequence.log').open('a') as f:
        f.write('stop %s\n' % name)
    if scenario == 'stop-error' or (scenario == 'drain-puis-stop-error' and name == 'dev9') \
            or (scenario == 'stop-error-candidat' and name == 'mesh-design'):
        sys.exit(1)
    if scenario != 'stop-unproven': (state / ('stopped-' + name)).touch()
    if scenario == 'stop-failed': (state / ('failed-' + name)).touch()
elif 'reset-failed' in args:
    if scenario == 'reset-error': sys.exit(1)
    if scenario != 'reset-no-effect':
        (state / ('failed-' + name)).unlink(missing_ok=True)
elif 'start' in args:
    if scenario == 'restart-error': sys.exit(1)
    (state / ('restart-' + name)).touch()
    (state / ('stopped-' + name)).unlink(missing_ok=True)
elif 'restart' in args:
    # old path, forbidden by #1152: the fakes below keep it visible
    if scenario == 'restart-error': sys.exit(1)
    (state / ('restart-' + name)).touch()
elif 'show' in args:
    if 'UnitFileState' in args:
        # #4239 : une unité désactivée n'a pas d'exécuteur actif attendu.
        # #4437 : une unité masquée encore active ne peut pas être rechargée.
        if (state / ('masked-' + name)).exists():
            print('masked')
        else:
            print('disabled' if (state / ('disabled-' + name)).exists() else 'enabled')
    elif 'ActiveState' in args:
        if (state / ('failed-' + name)).exists(): print('failed')
        else: print('inactive' if stopped() else 'active')
    elif 'MainPID' in args:
        if stopped():
            print('0')
        else:
            # #3311 : un vrai processus dont le témoin lit /proc/<pid>/cmdline.
            name = unit.split('@')[1].removesuffix('.service')
            par_unite = state / ('mainpid-' + name)
            if par_unite.exists():
                print(par_unite.read_text().strip())
            elif (state / 'mainpid').exists():
                print((state / 'mainpid').read_text().strip())
            else:
                print('123')
    elif scenario == 'bad-execstart': print('unreadable')
    elif scenario == 'execstart-error': sys.exit(1)
    else:
        owner = 'runner-' + name + '@host-agents'
        option = '--runner-id ' + owner
        if scenario == 'equals-owner': option = '--runner-id=' + owner
        if scenario == 'abbreviated-owner': option = '--r=' + owner
        if scenario == 'last-owner': option = '--runner-id wrong --runner-id=' + owner
        if scenario == 'default-owner': option = ''
        print('{ path=/bin/runner ; argv[]=/bin/runner run ' + option + ' ; }')
elif 'is-active' in args:
    if scenario == 'inactive' or stopped(): sys.exit(3)
    if scenario == 'inactive-dev9' and '@dev9.' in args[-1]: sys.exit(3)
    if (state / ('inactive-' + unit.split('@')[1].removesuffix('.service'))).exists(): sys.exit(3)
    print('active')
'''

SYSTEMD_RUN = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
# #1133 : the value given to -p must already be absolute — systemd-run does
# not expand %h in a transient unit's properties (it answers "not absolute").
assert args[:7] == ['--user', '--pipe', '--wait', '--quiet', '-p',
                    'EnvironmentFile=' + str(state / 'runner.env'), '--'], args
with (state / 'systemd-run.log').open('a') as f: f.write(' '.join(args) + '\n')
# The manager supplies these values; this stub deliberately does not source
# or reimplement EnvironmentFile. A separate test exercises the real parser.
env = dict(os.environ, **json.loads((state / 'expected-env.json').read_text()))
os.execvpe(args[7], args[7:], env)
'''

DB_PYTHON = r'''#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
assert os.environ.get('FAKE_REMOTE') == '1'
assert os.environ.get('AMEESH_DSN') == __import__('json').loads((Path(os.environ['FAKE_STATE']) / 'expected-env.json').read_text())['AMEESH_DSN']
assert os.environ.get('AMEESH_SCHEMA') == 'test_runner_schema'
assert sys.argv[1] == '-c' and 'db.connect(config.load())' in sys.argv[2]
assert 'clock_timestamp()' in sys.argv[2]
scenario = (Path(os.environ['FAKE_STATE']) / 'scenario').read_text()
print(time.time() + (60 if scenario == 'db-ahead' else 0))
'''

#: #2152 : plus aucune attente de créneau « idle » (DRAIN_ATTENTE est un
#: interrupteur) ; les attentes de bail (5 s) restent instantanées. #2184/R3 :
#: l'attente de l'échéance d'une pose incertaine avance l'horloge du banc.
SLEEP = ('#!/bin/sh\n'
         'if [ "$1" = 15 ]; then\n'
         '  if [ -n "${HORLOGE_FICHIER:-}" ]; then\n'
         '    echo "$(( $(cat "$HORLOGE_FICHIER") + 2 ))" > "$HORLOGE_FICHIER"\n'
         '  fi\n'
         '  exec %s 0.5\n'
         'fi\n'
         'exit 0\n') % (shutil.which('sleep') or '/bin/sleep')

#: #2184/R3 : l'échéance absolue remise à la base suit l'horloge injectée,
#: comme le faux CLI qui la compare. Sans fichier d'horloge, date réelle.
DATE = ('#!/bin/sh\n'
        'if [ -n "${HORLOGE_FICHIER:-}" ] && [ -r "$HORLOGE_FICHIER" ] '
        '&& [ "$1" = +%%s ]; then\n'
        '  cat "$HORLOGE_FICHIER"\n'
        '  exit 0\n'
        'fi\n'
        'exec %s "$@"\n') % (shutil.which('date') or '/bin/date')


class AgentHostTest(unittest.TestCase):
    #: #3608 : le faux ssh stallé écrit son pid ici (sa session propre).
    SSH_PID = 'ssh-pid'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-503-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        (self.state / 'runner.env').write_text('AMEESH_DSN=test-runner-dsn\nAMEESH_SCHEMA=test_runner_schema\n')
        (self.state / 'expected-env.json').write_text(json.dumps(dict(AMEESH_DSN='test-runner-dsn', AMEESH_SCHEMA='test_runner_schema')))
        self.bin = self.root / 'bin'; self.bin.mkdir()
        (self.bin / 'python3').symlink_to(sys.executable)
        self.home = self.root / 'home'; self.home.mkdir()
        # #3311 : layout du venv stable de la VM, traduit depuis /home/ameesh.
        self.share = self.home / '.local/share/ameesh'
        self.venv_current = self.share / 'venv-aaa1111'
        self.venv_old = self.share / 'venv-bbb2222'
        for venv in (self.venv_current, self.venv_old):
            (venv / 'bin').mkdir(parents=True)
            (venv / 'bin/python').symlink_to(sys.executable)
        (self.share / 'venv-courant').symlink_to(self.venv_current.name)
        # The guest runtime check resolves the source tree from the installed
        # package metadata, just as a real versioned venv does.
        metadata = self.venv_current / 'lib/python3/site-packages/ameesh-1.dist-info'
        metadata.mkdir(parents=True)
        (metadata / 'direct_url.json').write_text(json.dumps(
            {'url': 'file:///home/ameesh/.local/share/ameesh/src-aaa1111'}))
        self.config = self.root / 'bascule.env'
        self.config.write_text('AGENTS_SSH=root@vm.invalid\nHOTE_AGENTS=host-agents\nPERSONAS_VM=obsolete\n')
        for name, source in [('ssh', SSH), ('runuser', RUNUSER), ('ameesh', AMEESH),
                             ('systemctl', SYSTEMCTL), ('systemd-run', SYSTEMD_RUN), ('db-python', DB_PYTHON), ('sleep', SLEEP),
                             ('date', DATE), ('id', '#!/bin/sh\necho 1001\n'),
                             ('journalctl', '#!/bin/sh\n'
                              '[ -f "$FAKE_STATE/journal" ] && cat "$FAKE_STATE/journal"\n'
                              'exit 0\n')]:
            path = self.bin / name; path.write_text(source); path.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), BASCULE_ENV=str(self.config),
                        FAKE_STATE=str(self.state), FAKE_AMEESH=str(self.bin / 'ameesh'),
                        FAKE_DB_PYTHON=str(self.bin / 'db-python'),
                        FAKE_SHARE=str(self.share),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        # #3604 : sous mesure, chaque faux exécutable de la VM (ssh, ameesh,
        # systemctl…) démarrait sa propre couverture ; test_503 pesait ~62 s
        # par test (run 40240378071, part psycopg 2/12 coupée à 61 min). Seul
        # le processus de test est mesuré : ses lectures et son code restent
        # dans la carte, les enfants ne sont plus instrumentés — même correctif
        # que test_1252 dans 240a58a7.
        for variable in ('CARTE_MESURE', 'COVERAGE_PROCESS_START'):
            self.env.pop(variable, None)
        self.agents = [dict(name=n, host=h, mode=m, status=s) for n,h,m,s in [
            ('dev9', 'host-agents', 'execute', 'idle'),
            ('dev8', 'host-agents', 'execute', 'idle'),
            ('elsewhere', 'other-host', 'execute', 'idle'),
            ('observer', 'host-agents', 'observe', 'idle'),
            ('stopped', 'host-agents', 'execute', 'stopped')]]

    def run_vm(self, action='redemarrer', scenario='lease', agents=None):
        # #3311 : `controler` prouve le venv de chaque MainPID vivant ; sauf
        # processus déjà posé par le test, le banc en fournit un sur le venv
        # courant pour que les contrôles testés (bail, inactivité) restent
        # seuls en cause.
        if action == 'controler' and not (self.state / 'mainpid').exists():
            self._processus_venv(self.venv_current)
        (self.state / 'scenario').write_text(scenario)
        (self.state / 'agents.json').write_text(json.dumps(self.agents if agents is None else agents))
        return subprocess.run(['bash', str(SCRIPT), 'agents', action], env=self.env,
                              capture_output=True, text=True, timeout=300)

    def _processus_venv(self, venv, marque='mainpid'):
        """#3311 : processus vivant dont l'argv[0] est le python concret du venv.

        #3832 : il doit vivre tout le test. 60 s suffisaient à vide ; sous
        --parallel, quatre scénarios `controler` peuvent dépasser la minute et
        le processus disparaissait, faisant dire au script « venv courant non
        prouvé » (rouge h-32, test_runner_id_forms). Le cleanup l'arrête.
        """
        enfant = subprocess.Popen([str(venv / 'bin/python'), '-c',
                                    'import time; time.sleep(600)'])
        self.addCleanup(self._arreter, enfant)
        (self.state / marque).write_text(str(enfant.pid) + '\n')
        return enfant

    @staticmethod
    def _arreter(enfant):
        if enfant.poll() is None:
            enfant.terminate()
        enfant.communicate(timeout=5)

    @staticmethod
    def _tuer_la_session_ssh(pid_file):
        """#3608 : tue la session de l'enfant ssh, dernier détenteur des tuyaux."""
        try:
            pid = int(Path(pid_file).read_text())
        except (OSError, ValueError):
            return
        if pid <= 1:
            return
        try:
            # pid recyclé ou processus hors session propre : killpg(pid)
            # tuerait un groupe étranger, pas celui du faux ssh.
            if os.getsid(pid) != pid:
                return
        except ProcessLookupError:
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _terminer_l_appel(self, proc):
        """#3608 : borner la récolte après le killpg du groupe bash.

        L'enfant ssh vit dans sa propre session (`start_new_session` côté
        client) : il survit au killpg du groupe bash et garde stdout/stderr
        ouverts, donc `communicate()` sans borne ne rend jamais la main
        (run carte 40177589953 : 3 087 s sans sortie). On tue d'abord sa
        session par le pid du faux ssh, puis on borne la récolte : sous
        charge, l'échec est attribué au test au lieu d'attendre le plafond de
        la part de CI.
        """
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._tuer_la_session_ssh(self.state / self.SSH_PID)
        try:
            # #3832 : la récolte reste bornée, mais 5 s ne suffisent plus sous
            # --parallel ; 30 s laisse le groupe rendre ses tuyaux sans
            # attendre le plafond de la part.
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            # Une session ssh inconnue retient encore les tuyaux : renoncer
            # promptement, sans jamais attendre la fin du plafond de CI.
            try:
                proc.kill()
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            self.fail("l'appel de drain n'a pas rendu ses tuyaux dans la "
                      "borne de récolte")

    def units(self):
        path = self.state / 'units.log'
        if not path.exists():
            return ''
        # #4239 : la sonde `UnitFileState` est une lecture de périmètre, pas
        # une opération d'unité ; elle ne compte pas dans le journal observé.
        lignes = [l for l in path.read_text().splitlines()
                  if 'show -p UnitFileState' not in l]
        return '\n'.join(lignes) + ('\n' if lignes else '')

    def operateur(self):
        """#2152 : les gestes de pause/reprise réellement demandés au CLI."""
        path = self.state / 'operateur.log'
        return path.read_text() if path.exists() else ''

    def starts(self):
        """Démarrages (`start`) d'unités observés ; l'ancien `restart` ne doit
        plus apparaître (#1152)."""
        return [l for l in self.units().splitlines()
                if l.startswith('--user start ameesh-runner-agent@')]

    def gestes(self):
        """#4248 : gestes sur les unités, hors la lecture `list-units`."""
        return [l for l in self.units().splitlines()
                if not l.startswith('--user list-units ')]

    def stops(self):
        return [l for l in self.units().splitlines()
                if l.startswith('--user stop ameesh-runner-agent@')]

    def test_missing_runner_env_fails_before_any_execution(self):
        (self.state / 'runner.env').unlink()
        p = self.run_vm()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('runner environment file unreadable', p.stderr)
        self.assertEqual(self.gestes(), [])
        self.assertFalse((self.state / 'systemd-run.log').exists())

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_systemd_supplies_literal_environment(self):
        expected = 'postgresql://test@invalid/mesh_$USER"quoted"#fragment'
        (self.state / 'runner.env').write_text(
            'AMEESH_DSN=postgresql://test@invalid/mesh_$USER"quoted"#fragment\n'
            'AMEESH_SCHEMA=test_runner_schema\n')
        (self.state / 'expected-env.json').write_text(json.dumps(dict(
            AMEESH_DSN=expected, AMEESH_SCHEMA='test_runner_schema')))
        p = self.run_vm('controler')
        self.assertEqual(p.returncode, 0, p.stderr)
        log = (self.state / 'systemd-run.log').read_text()
        # #1133 : an absolute path, expanded before systemd-run sees it (the
        # `%h` specifier is not expanded there and would fail the unit).
        self.assertIn('EnvironmentFile=' + str(self.state / 'runner.env'), log)
        self.assertNotIn('%h', log)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_controler_prouve_le_venv_du_runner_vivant(self):
        """#3311 : `controler` lit l'argv[0] concret du MainPID, pas ExecStart."""
        p = self.run_vm('controler')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('MainPID vivant sur le venv courant', p.stdout)
        self.assertIn('contrôles verts', p.stdout)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_controler_refuse_un_runner_reste_sur_l_ancien_venv(self):
        """#3311, panne d'agents-2 : venv-courant à jour, exécuteur vivant sur
        l'ancien venv — l'hôte n'est pas sain et le dit."""
        self._processus_venv(self.venv_current)
        self._processus_venv(self.venv_old, 'mainpid-dev9')
        p = self.run_vm('controler', scenario='venv-old')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('ameesh-runner-agent@dev9.service : venv courant non prouvé',
                      p.stderr)
        self.assertIn("dev9 : le MainPID vivant n'exécute pas le venv installé",
                      p.stderr)
        # dev8, lui, vit bien sur le venv courant
        self.assertNotIn('ameesh-runner-agent@dev8.service', p.stderr)
        self.assertEqual(p.stdout.count('MainPID vivant sur le venv courant'), 1)
        self.assertNotIn('contrôles verts', p.stdout)

    def test_controler_refuse_un_mainpid_absent_ou_invalide(self):
        for pid in ('0', 'abc'):
            with self.subTest(pid=pid):
                (self.state / 'mainpid').write_text(pid + '\n')
                p = self.run_vm('controler', scenario='venv-ok')
                self.assertNotEqual(p.returncode, 0)
                if pid == '0':
                    self.assertIn('venv courant non prouvé', p.stderr)
                else:
                    self.assertIn('MainPID invalide (abc)', p.stderr)
                self.assertNotIn('contrôles verts', p.stdout)

    def test_restart_uses_remote_registry_and_user_units(self):
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.starts(), ['--user start ameesh-runner-agent@dev8.service',
                                         '--user start ameesh-runner-agent@dev9.service'])
        # #1152 : les personas actives prouvent leur arrêt puis redémarrent,
        # jamais par `restart`. #1633 : la persona arrêtée du registre est du
        # lot — son unité active est seulement rechargée sur le nouveau code
        # (aucun bail exigé) ; l'observateur et l'agent d'un autre hôte restent
        # ignorés.
        restarts = [l for l in self.units().splitlines() if 'restart' in l]
        self.assertEqual(restarts, ['--user restart ameesh-runner-agent@stopped.service'])
        self.assertIn('dev9 : redémarré', p.stdout)
        self.assertIn('stopped : arrêté dans le registre', p.stdout)
        self.assertNotIn('stopped : redémarré (était', p.stdout)
        self.assertNotIn('no lease before the restart', p.stdout)

    def test_restart_leaves_an_inactive_stopped_unit_alone(self):
        # #1633 : unité déjà arrêtée — aucune relance : elle chargera le
        # nouveau code à son démarrage.
        (self.state / 'inactive-stopped').touch()
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('restart ameesh-runner-agent@stopped.service', self.units())
        self.assertIn('stopped : arrêté dans le registre', p.stdout)
        self.assertIn('déjà arrêtée', p.stdout)

    def test_restart_reloads_a_stopped_disabled_active_unit(self):
        """#4437 (essai 4 du staging, 29/10) : une persona « stopped by hand »
        dont l'unité est encore active ET désactivée doit être rechargée par
        `redemarrer`, pas classée hors périmètre puis rejetée au contrôle."""
        (self.state / 'disabled-stopped').touch()
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('--user restart ameesh-runner-agent@stopped.service',
                      self.units())
        self.assertIn('stopped : arrêté dans le registre — unité redémarrée pour'
                      ' charger le nouveau code, aucun tour', p.stdout)
        self.assertNotIn('stopped : unité désactivée', p.stdout)
        self.assertNotIn('no lease before the restart', p.stdout)
        self.assertNotIn('stopped : redémarré (était', p.stdout)

    def test_restart_reloads_a_disabled_active_persona(self):
        """#4239 × #4437 : une unité désactivée encore active n'est pas
        démarrée si elle est inactive, mais elle est rechargée si elle tourne,
        avec preuve d'arrêt et sans bail."""
        agents = [dict(name=n, host='host-agents', mode='execute', status='idle')
                  for n in ('dev7', 'dev8', 'dev9', 'pilot-resident')]
        (self.state / 'disabled-pilot-resident').touch()
        (self.state / 'units-actives').write_text('dev8 dev9 pilot-resident\n')
        p = self.run_vm('redemarrer', agents=agents)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        unite = 'ameesh-runner-agent@pilot-resident.service'
        log = self.units().splitlines()
        self.assertEqual(log.count('--user stop ' + unite), 1)
        self.assertEqual(log.count('--user start ' + unite), 1)
        self.assertLess(log.index('--user stop ' + unite),
                        log.index('--user start ' + unite))
        self.assertIn('pilot-resident : unité désactivée encore active —'
                      ' rechargée sur le nouveau code, bail non exigé', p.stdout)
        self.assertNotIn('WARNING pilot-resident', p.stdout)

    def test_masked_active_unit_is_stopped_not_restarted(self):
        """Une unité masquée ne peut pas être démarrée : si elle tourne
        encore, le redémarrage l'arrête au lieu d'échouer sur `start`."""
        (self.state / 'masked-dev9').touch()
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('--user stop ameesh-runner-agent@dev9.service', self.units())
        self.assertNotIn('--user start ameesh-runner-agent@dev9.service', self.starts())
        self.assertIn('dev9 : unité masquée encore active — arrêtée, aucun'
                      ' exécuteur actif exigé', p.stdout)

    def test_restart_stops_proves_then_starts(self):
        """#1152 : l'arrêt (SIGTERM, drainage) précède la preuve du point sûr
        (ActiveState=inactive ET MainPID=0), elle-même avant le démarrage ;
        `restart`, qui ne prouve rien, n'est jamais utilisé."""
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        log = self.units().splitlines()
        for name in ('dev8', 'dev9'):
            unite = 'ameesh-runner-agent@' + name + '.service'
            stop = log.index('--user stop ' + unite)
            before = log.index('--user show -p ActiveState --value ' + unite)
            actif = log.index('--user show -p ActiveState --value ' + unite, stop + 1)
            pid = log.index('--user show -p MainPID --value ' + unite)
            start = log.index('--user start ' + unite)
            self.assertLess(before, stop, unite)
            self.assertLess(stop, actif, unite)
            self.assertLess(actif, start, unite)
            self.assertLess(pid, start, unite)
        # `restart` ne prouve rien pour une persona active (#1152) ; seule la
        # persona arrêtée du registre recharge son unité par `restart` (#1633).
        for unite in ('ameesh-runner-agent@dev8.service',
                      'ameesh-runner-agent@dev9.service'):
            self.assertNotIn('--user restart ' + unite, self.units())

    def test_preexisting_failed_units_are_reset_before_proven_stop(self):
        for name in ('dev8', 'dev9'):
            (self.state / ('failed-' + name)).touch()
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        log = self.units().splitlines()
        for name in ('dev8', 'dev9'):
            unit = 'ameesh-runner-agent@' + name + '.service'
            before = log.index('--user show -p ActiveState --value ' + unit)
            reset = log.index('--user reset-failed ' + unit)
            stop = log.index('--user stop ' + unit)
            proof = log.index('--user show -p ActiveState --value ' + unit, stop + 1)
            pid = log.index('--user show -p MainPID --value ' + unit, stop + 1)
            start = log.index('--user start ' + unit)
            self.assertLess(before, reset)
            self.assertLess(reset, stop)
            self.assertLess(stop, proof)
            self.assertLess(proof, start)
            self.assertLess(pid, start)
            self.assertIn(name + ' : redémarré', p.stdout)

    def test_preexisting_failure_does_not_bypass_stop_proof(self):
        unit = 'ameesh-runner-agent@dev8.service'
        for scenario in ('stop-failed', 'reset-error', 'reset-no-effect'):
            with self.subTest(scenario=scenario):
                (self.state / 'units.log').unlink(missing_ok=True)
                (self.state / 'failed-dev8').touch()
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0, p.stdout)
                log = self.units().splitlines()
                reset = log.index('--user reset-failed ' + unit)
                self.assertEqual(self.starts(), [])
                self.assertNotIn('--user stop ameesh-runner-agent@dev9.service', log)
                if scenario == 'reset-error':
                    self.assertNotIn('--user stop ' + unit, log)
                else:
                    self.assertLess(reset, log.index('--user stop ' + unit))

    def test_restart_gates_agent_in_turn_without_idle_wait(self):
        # #1152/R1 : un agent vu `running` n'est plus différé — il est arrêté
        # (SIGTERM, drainage borné), prouvé au point sûr, puis démarré.
        p = self.run_vm(scenario='busy-once')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.state / 'busy-seen').exists())
        self.assertIn('--user stop ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('--user start ameesh-runner-agent@dev9.service', self.units())
        self.assertNotIn('--user restart ameesh-runner-agent@dev9.service', self.units())

        # Un tour qui finit tout seul ne déclenche aucun drain.
        self.assertEqual(self.operateur(), '')

    def test_drain_poses_and_lifts_a_short_operator_pause(self):
        """#2152 : un agent qui enchaîne les tours est drainé (pause opérateur
        courte), redémarré hors tour, puis la pause est levée et prouvée."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines')
        self.assertEqual(p.returncode, 0, p.stderr)
        journal = self.operateur()
        self.assertIn('pause dev9', journal)
        self.assertIn('resume dev9', journal)
        # Le motif de la pause nomme le drain (journalisé par le mesh).
        self.assertIn('drain de déploiement #2152', journal)
        self.assertIn('--reason', journal)
        # #2152 / R1 B1 : la levée est conditionnée au motif exact de la pose.
        pose = [l for l in journal.splitlines() if l.startswith('pause dev9')][0]
        reprise = [l for l in journal.splitlines() if l.startswith('resume dev9')][0]
        motif = pose.split('--reason ', 1)[1].split(' --human ', 1)[0]
        self.assertIn('--expect-reason ' + motif, reprise)
        # Posé avant le redémarrage, et redémarré seulement hors tour.
        self.assertLess(p.stdout.index('drain posé'), p.stdout.index('dev9 : redémarré'))
        self.assertIn('dev9 : redémarré (était idle)', p.stdout)
        self.assertIn('drain levé (redémarrage hors tour)', p.stdout)
        self.assertIn('reprise prouvée (operator_paused=false)', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def _un_seul_agent_au_tour(self):
        """Un banc direct (Popen) : un seul agent, vu en tour, pour que la
        première lecture réussisse et que ce soit bien la pose qui bloque."""
        (self.state / 'scenario').write_text('chaines')
        (self.state / 'agents.json').write_text(
            json.dumps([self.agents[0]]))
        # Une pose incertaine fait attendre l'échéance au trap EXIT ; l'horloge
        # injectée (le faux `sleep 15` avance le fichier) rend l'attente nulle.
        (self.state / 'horloge').write_text('0')
        self.env.update(DRAIN_ATTENTE='1', FAKE_STALL_SSH='1',
                        FAKE_STALL_MATCH='ameesh pause dev9',
                        FAKE_STALL_PID=str(self.state / self.SSH_PID),
                        DRAIN_ECHEANCE='1', DRAIN_BORNE_INSTRUCTION='1',
                        DRAIN_DERIVE='1',
                        HORLOGE_FICHIER=str(self.state / 'horloge'))

    def test_a_live_ssh_client_is_bounded_by_the_drain_deadline(self):
        """#2184/N3 : un client SSH vivant mais bloqué ne suspend pas le
        balayage au-delà de DRAIN_APPEL_DELAI (borne locale du groupe)."""
        self._un_seul_agent_au_tour()
        self.env.update(DRAIN_APPEL_DELAI='1', DRAIN_MARGE_LOCALE='1')
        proc = subprocess.Popen(['bash', str(SCRIPT), 'agents', 'redemarrer'],
                                env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            # #3832 : la borne locale vaut 2 s ; 10 s de récolte ne laissaient
            # pas de marge à l'ordonnancement sous --parallel (h-32).
            _, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self._terminer_l_appel(proc)
            self.fail('drain call outlived its deadline with a live SSH client')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('appel de drain expiré', stderr)
        self.assertEqual(self.gestes(), [])

    def test_the_local_deadline_leaves_a_margin_to_the_remote_one(self):
        """#2198/N2 : la borne locale vaut DRAIN_APPEL_DELAI plus une marge,
        pour que le timeout distant rende son code avant que le client ne tue
        le groupe. Sans marge, les deux expiraient ensemble."""
        self._un_seul_agent_au_tour()
        self.env.update(DRAIN_APPEL_DELAI='1', DRAIN_MARGE_LOCALE='2')
        debut = time.monotonic()
        proc = subprocess.Popen(['bash', str(SCRIPT), 'agents', 'redemarrer'],
                                env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            _, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self._terminer_l_appel(proc)
            self.fail('drain call outlived its deadline with a live SSH client')
        ecoule = time.monotonic() - debut
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('appel de drain expiré', stderr)
        self.assertGreaterEqual(ecoule, 2.5,
                                'la borne locale doit inclure DRAIN_MARGE_LOCALE')

    def test_interruption_is_forwarded_to_the_stalled_ssh_child(self):
        """#2198/N1 : le client lance son enfant dans une nouvelle session ;
        un SIGINT reçu par le client doit être transmis à cet enfant, sinon
        le ssh reste orphelin."""
        self._un_seul_agent_au_tour()
        self.env.update(DRAIN_APPEL_DELAI='30',
                        FAKE_STALL_STARTED=str(self.state / 'ssh-commence'),
                        FAKE_SIGNAL_FILE=str(self.state / 'signal'))
        proc = subprocess.Popen(['bash', str(SCRIPT), 'agents', 'redemarrer'],
                                env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            limite = time.monotonic() + 30
            while not (self.state / 'ssh-commence').exists() \
                    and time.monotonic() < limite:
                time.sleep(0.05)
            self.assertTrue((self.state / 'ssh-commence').exists())
            # #3608 : le pid de la session ssh doit être publié avant le
            # témoin, pour que la récolte de repli puisse la tuer.
            self.assertTrue((self.state / self.SSH_PID).exists())
            os.killpg(proc.pid, signal.SIGINT)
            _, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self._terminer_l_appel(proc)
            self.fail('interrupted drain call did not end')
        self.assertTrue((self.state / 'signal').exists(), stderr)
        self.assertEqual((self.state / 'signal').read_text(), str(signal.SIGINT))
        self.assertNotEqual(proc.returncode, 0)

    def test_the_fallback_cleanup_kills_the_ssh_session_and_is_bounded(self):
        """#3608 : après le killpg du groupe bash, l'enfant ssh (session propre)
        garde stdout/stderr ouverts ; la récolte de repli doit tuer sa session
        puis rendre la main, pas attendre le plafond de la part de CI (run
        carte 40177589953)."""
        pid_file = self.state / self.SSH_PID
        client = self.state / 'client-detache.py'
        client.write_text(
            'import subprocess, sys, time\n'
            'subprocess.Popen(\n'
            '    [sys.executable, "-c",\n'
            '     "import os, signal, sys; from pathlib import Path; "\n'
            '     "Path(sys.argv[1]).write_text(str(os.getpid())); "\n'
            '     "signal.pause()", sys.argv[1]],\n'
            '    start_new_session=True)\n'
            'time.sleep(60)\n')
        proc = subprocess.Popen([sys.executable, str(client), str(pid_file)],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        self.addCleanup(self._nettoyer_client, proc, pid_file)
        limite = time.monotonic() + 30
        while not pid_file.exists() and time.monotonic() < limite:
            time.sleep(0.05)
        self.assertTrue(pid_file.exists(), "le faux ssh n'a pas démarré")
        # Filet du test lui-même : si la borne de récolte disparaissait, ce
        # témoin tue la session et le test échoue, au lieu de pendre jusqu'au
        # plafond de la part de CI. #3832 : la récolte est bornée à 30 s sous
        # charge, le filet passe au-delà.
        minuteur = threading.Timer(60, self._tuer_la_session_ssh, (pid_file,))
        minuteur.start()
        self.addCleanup(minuteur.cancel)
        debut = time.monotonic()
        self._terminer_l_appel(proc)
        self.assertLess(time.monotonic() - debut, 30,
                        'la récolte de repli doit être bornée')

    def _nettoyer_client(self, proc, pid_file):
        self._tuer_la_session_ssh(pid_file)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def test_reads_are_bounded_by_their_own_deadline(self):
        """#2198/N4 : list/show/--help ont une borne plus courte que celle des
        gestes de drain ; une lecture lente ne bloque pas le balayage."""
        # #4358 : une lecture coupée est vide, donc non concluante : relue
        # en fin de balayage puis laissée en l'état, sans échec de l'étape.
        # Un seul essai et un seul agent : deux lectures coupées en tout.
        # #4528 : `list` doit aboutir sous la même borne ; sa chaîne factice
        # (garde locale, ssh, sudo, CLI : quatre interpréteurs) passait 2 s
        # (1 + marge 1) sous la charge de la CI (code 124, « appel de drain
        # expiré »). 3 s + 3 s la couvrent avec un CPU au dixième.
        self.env.update(DRAIN_APPEL_DELAI='30', DRAIN_MARGE_LOCALE='3',
                        DRAIN_LECTURE_DELAI='3', LECTURE_ESSAIS='1')
        agents = [dict(name='dev9', host='host-agents', mode='execute', status='idle')]
        debut = time.monotonic()
        p = self.run_vm(scenario='show-lent', agents=agents)
        ecoule = time.monotonic() - debut
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('laissée(s) en l\'état (non concluant) : dev9', p.stdout)
        self.assertEqual(self.starts(), [])
        # #4528 : la borne passée à chaque lecture se lit dans le journal ssh,
        # sans dépendre de l'horloge ; les deux `show` portent celle des
        # lectures, jamais celle des gestes.
        lectures = [l for l in (self.state / 'ssh.log').read_text().splitlines()
                    if l.rstrip().endswith('/bin/ameesh show dev9 --json')]
        self.assertEqual(len(lectures), 2, lectures)
        for ligne in lectures:
            self.assertIn('timeout --kill-after=5 3 /', ligne)
        # #3832, #4528 : le show lent (45 s) dépasse la borne des gestes
        # (30 s) ; une seule lecture coupée par elle porterait le balayage
        # au-delà de 30 s. Deux coupures à 3 s (6 s au pire par la garde
        # locale) laissent la marge d'ordonnancement de --parallel.
        self.assertLess(ecoule, 30,
                        'show doit être coupé par DRAIN_LECTURE_DELAI, pas par '
                        'DRAIN_APPEL_DELAI')

    def test_failed_lift_is_not_announced_before_the_exit_retry(self):
        """#2198/N3 : « NON levé » n'apparaît qu'après la dernière tentative
        du trap EXIT ; une levée qui réussit à la seconde passe ne l'affiche
        pas et ne laisse pas de pause. #2498 : les passes du trap précèdent le
        verdict, une levée réussie rend donc 0."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='resume-error-deux-fois')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn('drain NON levé', p.stderr)
        self.assertNotIn('NON levé', p.stderr)
        self.assertNotIn('pause à lever à la main', p.stderr)
        self.assertFalse((self.state / 'pause-dev9').exists())
        reprises = [l for l in self.operateur().splitlines()
                    if l.startswith('resume dev9')]
        self.assertEqual(len(reprises), 3,
                         'une levée dans la boucle, deux passes du trap EXIT')

    def test_drain_carries_a_deadline_and_the_operator_actor(self):
        """#2184/R3 et N6 : la pose porte une échéance absolue et l'acteur
        humain du poste, jamais le compte `ameesh` de la VM."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines')
        self.assertEqual(p.returncode, 0, p.stderr)
        pose = [l for l in self.operateur().splitlines() if l.startswith('pause dev9')][0]
        self.assertIn('--not-after ', pose)
        self.assertIn('--human human:', pose)

    def test_cli_without_not_after_never_poses_a_drain(self):
        """#2184/R3 : un CLI cible sans --not-after ne peut pas porter la
        barrière ; le drain est désactivé plutôt que posé sans échéance."""
        self.env['DRAIN_ATTENTE'] = '1'
        (self.state / 'sans-not-after').touch()
        p = self.run_vm(scenario='chaines')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('drain #2152 indisponible', p.stderr)
        self.assertIn('--not-after', p.stderr)
        self.assertNotIn('pause dev9', self.operateur())
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_timed_out_pause_cannot_commit_after_the_deadline(self):
        """#2184/R3 : un appel pause expiré (rc 124) peut encore créer son
        unité après la dernière lecture. Le créateur tardif rejoue la pose
        APRÈS la reprise : la barrière d'échéance côté base la refuse, sans
        événement ni pause orpheline."""
        self.env['DRAIN_ATTENTE'] = '1'
        self.env['DRAIN_ECHEANCE'] = '1'
        self.env['DRAIN_BORNE_INSTRUCTION'] = '1'
        self.env['DRAIN_DERIVE'] = '1'
        (self.state / 'horloge').write_text('0')
        self.env['HORLOGE_FICHIER'] = str(self.state / 'horloge')
        p = self.run_vm(scenario='pause-expiree-tardive')
        self.assertNotEqual(p.returncode, 0)
        # Le créateur tardif a bien exécuté sa pose après le nettoyage…
        self.assertTrue((self.state / 'createur-tardif-fait').exists())
        # … et la base l'a refusée : aucune pause, aucun événement.
        self.assertEqual((self.state / 'refus-tardif.log').read_text().strip(),
                         'dev9 not_after')
        self.assertFalse((self.state / 'pause-dev9').exists())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertIn('resume dev9 --operator', self.operateur())
        self.assertIn('--expect-reason', self.operateur())

    def test_drain_refused_does_not_stop_the_other_agents(self):
        """#2184/N2 : un refus de pose ne condamne que cet agent. Les autres
        agents du balayage sont redémarrés ; l'agent non drainé n'est ni
        redémarré ni repris."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-pause-error')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('pose du drain refusée', p.stderr)
        self.assertIn('--user start ameesh-runner-agent@dev8.service', self.units())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertNotIn('resume dev9', self.operateur())
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_refused_operator_identity_restarts_idle_personas_without_drain(self):
        """#3120/B1 : un refus global de pose ne condamne pas les personas au
        repos : elles n'ont pas besoin du drain et redémarrent sans lui."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='drain-refuse-identite')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('dev8 : au repos, drain non posé', p.stderr)
        self.assertIn('--user start ameesh-runner-agent@dev8.service', self.units())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertFalse((self.state / 'pause-dev8').exists())
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_non_execute_agent_is_skipped_before_restart(self):
        """#2184/N1 : un agent passé en mode non-execute pendant le drain
        n'est ni drainé ni redémarré ; le motif du skip le nomme."""
        p = self.run_vm(scenario='mode-change')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('dev9 : déplacé ou mode non execute (mode=externe), ignoré',
                      p.stdout)
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertEqual(self.operateur(), '')

    def test_mode_change_after_a_drain_still_lifts_the_pause(self):
        """#2184/N1 + 0459 : l'agent passe externe après la pose du drain ;
        la pause de ce drain est levée et l'agent n'est pas redémarré."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='mode-change-apres-drain')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('dev9 : déplacé ou mode non execute (mode=externe), ignoré',
                      p.stdout)
        self.assertIn('dev9 : drain levé', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertIn('resume dev9 --operator', self.operateur())

    def test_foreign_pause_replacing_the_drain_is_not_lifted(self):
        """R1 B1 : notre drain est levé de l'extérieur puis une NOUVELLE pause
        étrangère est posée ; la reprise conditionnelle ne lève que la nôtre."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='drain-remplace')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('pause dev9', self.operateur())
        self.assertIn('--expect-reason', self.operateur())
        self.assertTrue((self.state / 'remplace-fait').exists())
        # La pause étrangère est toujours là, intacte.
        self.assertTrue((self.state / 'pause-dev9').exists())
        self.assertEqual((self.state / 'motif-dev9').read_text(), 'pause humaine etrangere')
        self.assertIn('pause étrangère laissée en place', p.stderr)
        self.assertIn('dev9 : redémarré', p.stdout)

    def test_lost_pause_answer_is_reconciled_by_the_real_state(self):
        """R1 B2 : la pose a abouti mais la réponse est illisible ; l'état réel
        (motif identique) la confirme et le drain continue."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='pause-reponse-perdue')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('réponse de pause illisible', p.stderr)
        self.assertIn("drain posé (confirmé par l'état réel)", p.stdout)
        self.assertIn('drain levé (redémarrage hors tour)', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_cut_pause_answer_after_commit_is_reconciled(self):
        """R1 B2 : la pose aboutit puis le lien tombe (rc ≠ 0) ; même
        réconciliation, la pause trouvée est bien la nôtre."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='pause-coupee')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("drain posé (confirmé par l'état réel)", p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_lost_pause_answer_without_our_pause_stops_safely(self):
        """R1 B2 : réponse illisible et aucune pause à nous ; rien n'est levé
        ni redémarré, et le script s'arrête en le disant."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='pause-perdue-sans-pose')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("n'est pas celle de ce drain", p.stderr)
        self.assertFalse((self.state / 'pause-dev9').exists())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])

    def test_lost_pause_and_reconciliation_still_attempt_conditional_cleanup(self):
        """A committed pause stays tracked when its reconciliation read fails."""
        self.env['DRAIN_ATTENTE'] = '1'
        # #4358 : pause_reelle relit (lire_show) ; un seul essai garde la
        # lecture de réconciliation perdue que ce témoin exige.
        self.env['LECTURE_ESSAIS'] = '1'
        p = self.run_vm(scenario='pause-coupee-etat-perdu')
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue((self.state / 'lecture-perdue').exists())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertFalse((self.state / 'pause-dev9').exists())
        self.assertIn('resume dev9 --operator', self.operateur())
        self.assertIn('--expect-reason', self.operateur())

    def test_operator_gesture_refused_in_an_agent_session_stops_safely(self):
        """Périmètre annoncé : le drain est un geste d'opérateur. Un refus
        bad_actor (vm.sh lancé dans une session liée à un agent) n'interrompt
        aucun tour et ne laisse aucune pause."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='drain-refuse-identite')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bad_actor', p.stderr)
        self.assertIn('pose du drain refusée', p.stderr)
        self.assertIn("n'est pas celle de ce drain", p.stderr)
        self.assertFalse((self.state / 'pause-dev9').exists())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])

    def test_drain_refused_stops_without_restarting_the_agent(self):
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-pause-error')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('pose du drain refusée', p.stderr)
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertNotIn('resume dev9', self.operateur())
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_drain_does_not_wait_out_a_turn_that_never_ends(self):
        """#2152 : le drain ne bloque plus l'arrêt. Un agent qui reste en tour
        reçoit la pause, puis l'arrêt prouvé #1152 prend la suite sans attendre
        une échéance ; la pause est levée après le démarrage."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-toujours')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('toujours en tour', p.stderr)
        self.assertIn('pause dev9', self.operateur())
        self.assertIn('resume dev9', self.operateur())
        self.assertIn('--user stop ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('--user start ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('drain levé (redémarrage hors tour)', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_pause_is_posed_before_the_stop(self):
        """#2152 x #1152 : la pause ferme l'admission AVANT le SIGTERM."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-toujours')
        self.assertEqual(p.returncode, 0, p.stderr)
        seq = (self.state / 'sequence.log').read_text().splitlines()
        self.assertLess(seq.index('pause dev9'), seq.index('stop dev9'))

    def test_drain_is_lifted_when_the_proven_stop_fails(self):
        """Pose réussie puis `stop` en échec : le trap EXIT lève NOTRE pause."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='drain-puis-stop-error')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('drain posé', p.stdout)
        self.assertIn('ne redémarre pas', p.stderr)
        self.assertIn('resume dev9 --operator', self.operateur())
        self.assertIn('--expect-reason', self.operateur())
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_human_lift_before_the_resume_leaves_no_stale_drain(self):
        """N5 B2 / #3114 : le drain initial est levé par un humain ; le tour
        toujours actif reçoit un nouveau drain avant l'arrêt. Seul ce dernier
        motif est repris, aucune entrée périmée ne reste suivie."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-relance')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('toujours en tour', p.stderr)
        self.assertTrue((self.state / 'levee-humaine').exists())
        poses = [l for l in self.operateur().splitlines() if l.startswith('pause dev9')]
        self.assertEqual(len(poses), 2)
        motifs = [l.split('--reason ', 1)[1].split(' --human ', 1)[0] for l in poses]
        self.assertNotEqual(motifs[0], motifs[1])
        reprises = [l for l in self.operateur().splitlines() if l.startswith('resume dev9')]
        self.assertEqual(len(reprises), 1)
        self.assertIn('--expect-reason ' + motifs[1], reprises[0])
        self.assertNotIn('--expect-reason ' + motifs[0], reprises[0])
        self.assertIn('stop ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('start ameesh-runner-agent@dev9.service', self.units())
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_resume_failure_is_named_and_the_hold_stays_visible(self):
        self.env['DRAIN_ATTENTE'] = '1'
        # Isolate this agent: a failed lift now prevents pausing the next one (#4000).
        p = self.run_vm(scenario='chaines-resume-error', agents=[self.agents[0]])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('drain NON levé', p.stderr)
        self.assertIn('ameesh resume dev9 --operator', p.stderr)
        self.assertTrue((self.state / 'pause-dev9').exists())

    def test_foreign_pause_is_neither_doubled_nor_lifted(self):
        self.env['DRAIN_ATTENTE'] = '1'
        (self.state / 'pause-dev9').touch()
        p = self.run_vm(scenario='chaines')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('pause dev9', self.operateur())
        self.assertNotIn('resume dev9', self.operateur())
        self.assertTrue((self.state / 'pause-dev9').exists())
        # La pause étrangère n'empêche pas l'arrêt prouvé #1152.
        self.assertIn('--user stop ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('--user start ameesh-runner-agent@dev9.service', self.units())
        self.assertNotIn('--user restart ameesh-runner-agent@dev9.service', self.units())

    def test_concurrent_pause_is_left_in_place(self):
        """Posée entre notre lecture et notre pause : elle n'est pas de nous."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='pause-concurrente')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('pause dev9', self.operateur())
        self.assertNotIn('resume dev9', self.operateur())
        self.assertIn('déjà posée par un autre acteur', p.stdout)
        self.assertTrue((self.state / 'pause-dev9').exists())

    def test_drain_disabled_by_zero(self):
        self.env['DRAIN_ATTENTE'] = '0'
        p = self.run_vm(scenario='chaines-toujours')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('toujours en tour', p.stderr)
        self.assertEqual(self.operateur(), '')
        self.assertIn('--user stop ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('--user start ameesh-runner-agent@dev9.service', self.units())

    def test_restart_rejects_unchanged_lease(self):
        for scenario in ['stale-lease']:
            with self.subTest(scenario=scenario):
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn('bail', p.stderr)
                self.assertIn('journalctl --user', p.stderr)
                # #3412 : le balayage continue après un bail non repris ; les
                # agents en échec sont nommés à la fin, pas un arrêt au premier.
                self.assertNotIn('dev8 : redémarré', p.stdout)
                self.assertIn('2 agent(s) sans bail repris : dev8 dev9', p.stdout)

    def test_restart_elected_role_taken_by_other_host_is_a_warning(self):
        """#3412 : la reprise par un autre hôte candidat est l'issue normale."""
        agents = [dict(name=n, host='host-agents', mode='execute', status='idle')
                  for n in ('dev7', 'dev8', 'dev9')]
        (self.state / 'role-dev8.json').write_text(json.dumps({
            'name': 'dev8', 'elected': True, 'holder': 'other-host',
            'lease_owner': 'runner-dev8@other-host', 'epoch': 9,
            'candidates': [{'host': 'other-host', 'ready': True},
                           {'host': 'host-agents', 'ready': True}]}))
        p = self.run_vm('redemarrer', 'elu-ailleurs', agents=agents)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('WARNING dev8: bail du rôle élu repris par other-host '
                      '(époque 9 > 7)', p.stdout)
        self.assertNotIn('bail non repris', p.stderr)
        starts = self.starts()
        for n in ('dev7', 'dev8', 'dev9'):
            self.assertIn('--user start ameesh-runner-agent@%s.service' % n, starts)

    def test_restart_reserved_role_taken_by_other_host_is_a_warning(self):
        """#4239 : une relève légitime pendant le redémarrage, même après une
        réservation @restart, est un non-concluant — jamais un retour arrière.

        Cas du staging v1.27.0 : agents-1 avait réservé le rôle mesh-design,
        mais un hôte encore sur l'ancien code ignorait la réservation (ou elle
        expirait en 90 s) et l'élection reprenait le rôle à une époque
        supérieure. Le balayage doit poursuivre, dev8 n'étant pas en échec."""
        agents = [dict(name=n, host='host-agents', mode='execute', status='idle')
                  for n in ('dev7', 'dev8', 'dev9')]
        (self.state / 'role-dev8.json').write_text(json.dumps({
            'name': 'dev8', 'elected': True, 'holder': 'other-host',
            'lease_owner': 'runner-dev8@other-host', 'epoch': 9,
            'candidates': [{'host': 'other-host', 'ready': True},
                           {'host': 'host-agents', 'ready': True}]}))
        (self.state / 'journal').write_text(
            '01:02:03 [dev8] elected role reserved to host-agents for 90s'
            ' (executor restart)\n')
        p = self.run_vm('redemarrer', 'elu-ailleurs', agents=agents)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('WARNING dev8: bail du rôle élu repris par other-host '
                      '(époque 9 > 7) — reprise normale — réservation @restart'
                      ' écrite puis non honorée (relève légitime §11, sans'
                      ' retour)', p.stdout)
        self.assertNotIn('bail non repris', p.stderr)
        self.assertNotIn('sans bail repris', p.stdout)
        self.assertIn('dev8 : redémarré', p.stdout)
        self.assertIn('dev7 : redémarré', p.stdout)
        self.assertIn('dev9 : redémarré', p.stdout)
        starts = self.starts()
        for n in ('dev7', 'dev8', 'dev9'):
            self.assertIn('--user start ameesh-runner-agent@%s.service' % n, starts)

    def _trois_agents(self):
        return [dict(name=n, host='host-agents', mode='execute', status='idle')
                for n in ('dev7', 'dev8', 'dev9')]

    def test_restart_absorbs_a_transient_empty_read(self):
        """#4358 : deux lectures vides puis une valide — relues sur place."""
        (self.state / 'vide-dev8').write_text('2')
        p = self.run_vm('redemarrer', 'lease', agents=self._trois_agents())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('JSONDecodeError', p.stderr)
        self.assertNotIn('illisible', p.stdout)
        for n in ('dev7', 'dev8', 'dev9'):
            self.assertIn('%s : redémarré' % n, p.stdout)

    def test_restart_defers_an_unreadable_agent_to_the_end_of_the_sweep(self):
        """#4358 : illisible après 3 essais (relève en cours), la persona est
        relue en fin de balayage et redémarrée — jamais un échec d'étape."""
        (self.state / 'vide-dev8').write_text('3')
        p = self.run_vm('redemarrer', 'lease', agents=self._trois_agents())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('JSONDecodeError', p.stderr)
        self.assertIn('WARNING dev8: registre illisible (lecture vide) — relu en'
                      ' fin de balayage', p.stdout)
        self.assertLess(p.stdout.index('dev9 : redémarré'),
                        p.stdout.index('dev8 : redémarré'))
        self.assertNotIn('laissée(s) en l\'état', p.stdout)
        for n in ('dev7', 'dev8', 'dev9'):
            self.assertIn('--user start ameesh-runner-agent@%s.service' % n,
                          self.starts())

    def test_restart_leaves_a_still_unreadable_agent_with_a_warning(self):
        """#4358 : toujours illisible, la persona est laissée en l'état avec
        un avertissement ; le balayage et l'étape restent verts."""
        (self.state / 'vide-dev8').write_text('toujours')
        p = self.run_vm('redemarrer', 'lease', agents=self._trois_agents())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('JSONDecodeError', p.stderr)
        self.assertIn('WARNING dev8: registre toujours illisible en fin de'
                      ' balayage', p.stdout)
        self.assertIn('WARNING: 1 persona(s) au registre illisible, laissée(s)'
                      ' en l\'état (non concluant) : dev8', p.stdout)
        self.assertIn('dev7 : redémarré', p.stdout)
        self.assertIn('dev9 : redémarré', p.stdout)
        self.assertNotIn('ameesh-runner-agent@dev8', self.units())

    def test_restart_ssh_transport_loss_on_a_read_is_not_conclusive(self):
        """#4358 (B mty-amh-impl-03) : un transport SSH perdu (255) sur la
        lecture d'une persona est non concluant, comme une sortie vide : le
        balayage continue et l'étape reste verte. L'erreur explicite du CLI
        (rc 1) reste un échec (test_3114)."""
        (self.state / 'ssh-255').write_text('dev8')
        p = self.run_vm('redemarrer', 'lease', agents=self._trois_agents())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('code 255', p.stderr)
        self.assertIn('WARNING: 1 persona(s) au registre illisible, laissée(s)'
                      ' en l\'état (non concluant) : dev8', p.stdout)
        self.assertIn('dev7 : redémarré', p.stdout)
        self.assertIn('dev9 : redémarré', p.stdout)
        self.assertNotIn('ameesh-runner-agent@dev8', self.units())

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_check_unreadable_registry_is_not_conclusive(self):
        """#4358 : `controler` sur un registre illisible n'échoue pas."""
        (self.state / 'vide-dev8').write_text('toujours')
        p = self.run_vm('controler', 'lease', agents=self._trois_agents())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('WARNING dev8: registre illisible (lecture vide) — preuve'
                      ' de bail non concluante (sans retour)', p.stdout)
        self.assertIn('contrôles verts', p.stdout)

    def test_restart_lease_failure_continues_the_sweep(self):
        """#3412 : un vrai échec de bail ne stoppe que cet agent."""
        agents = [dict(name=n, host='host-agents', mode='execute', status='idle')
                  for n in ('dev7', 'dev8', 'dev9')]
        p = self.run_vm('redemarrer', 'bail-perdu', agents=agents)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('dev8 : active, mais bail non repris', p.stderr)
        self.assertIn('ERREUR: 1 agent(s) sans bail repris : dev8', p.stdout)
        self.assertNotIn('dev8 : redémarré', p.stdout)
        self.assertIn('dev7 : redémarré', p.stdout)
        self.assertIn('dev9 : redémarré', p.stdout)
        starts = self.starts()
        for n in ('dev7', 'dev8', 'dev9'):
            self.assertIn('--user start ameesh-runner-agent@%s.service' % n, starts)

    def _candidat_elu_ailleurs(self):
        """#4248 (agents-2, 28/10) : mesh-design a son bail sur un autre hôte ;
        son exécuteur candidat tourne ici, absent de la liste du registre."""
        agents = self.agents + [dict(name='mesh-design', host='other-host',
                                     mode='execute', status='idle')]
        (self.state / 'units-actives').write_text('dev8 dev9 mesh-design\n')
        return agents

    def test_restart_reaches_an_elected_candidate_outside_the_registry(self):
        agents = self._candidat_elu_ailleurs()
        p = self.run_vm(agents=agents)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        log = self.units().splitlines()
        unite = 'ameesh-runner-agent@mesh-design.service'
        self.assertLess(log.index('--user stop ' + unite),
                        log.index('--user start ' + unite))
        self.assertNotIn('--user restart ' + unite, log)
        self.assertIn('mesh-design : unité active hors registre de host-agents'
                      " (candidat d'un rôle élu ou persona hors périmètre)", p.stdout)
        # Ni drain (la pause viserait la persona sur l'hôte du bail) ni bail.
        self.assertNotIn('mesh-design', self.operateur())
        self.assertNotIn('WARNING mesh-design', p.stdout)
        # L'agent d'un autre hôte sans unité active ici reste ignoré.
        self.assertNotIn('ameesh-runner-agent@elsewhere.service', self.units())

    def test_restart_fails_when_the_active_units_are_unreadable(self):
        p = self.run_vm(scenario='list-units-error', agents=self._candidat_elu_ailleurs())
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('unités ameesh-runner-agent@ actives illisibles', p.stderr)
        self.assertIn('hors registre non redémarrée(s) : (liste illisible)', p.stdout)
        self.assertIn('dev8 : redémarré', p.stdout)
        self.assertIn('dev9 : redémarré', p.stdout)

    def test_restart_fails_when_only_the_candidate_does_not_restart(self):
        p = self.run_vm(scenario='stop-error-candidat', agents=self._candidat_elu_ailleurs())
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('mesh-design : unité active hors registre : arrêt non prouvé', p.stderr)
        self.assertIn('hors registre non redémarrée(s) : mesh-design', p.stdout)
        self.assertIn('dev9 : redémarré', p.stdout)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_controler_refuse_un_candidat_elu_sur_l_ancien_venv(self):
        """#4248 : l'hôte n'est pas sain si le candidat vit sur l'ancien venv."""
        agents = self._candidat_elu_ailleurs()
        self._processus_venv(self.venv_current)
        self._processus_venv(self.venv_old, 'mainpid-mesh-design')
        p = self.run_vm('controler', agents=agents)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("mesh-design : le MainPID vivant n'exécute pas le venv installé",
                      p.stderr)
        self.assertNotIn('mesh-design : bail', p.stderr)
        self.assertNotIn('contrôles verts', p.stdout)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_controler_vert_avec_un_candidat_elu_sur_le_venv_courant(self):
        p = self.run_vm('controler', agents=self._candidat_elu_ailleurs())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('mesh-design (unité active hors registre', p.stdout)
        self.assertEqual(p.stdout.count('MainPID vivant sur le venv courant'), 3)
        self.assertIn('contrôles verts', p.stdout)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_controler_prouve_le_venv_d_un_arret_actif_sans_bail(self):
        """#4437 : une persona arrêtée dont l'unité tourne encore est contrôlée
        sur le venv — même périmètre que redemarrer — mais aucun bail n'est
        exigé d'une persona arrêtée."""
        (self.state / 'units-actives').write_text('dev8 dev9 stopped\n')
        p = self.run_vm('controler')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('stopped (unité active hors registre', p.stdout)
        self.assertIn('MainPID vivant sur le venv courant', p.stdout)
        self.assertNotIn('stopped : bail', p.stderr)
        self.assertIn('contrôles verts', p.stdout)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_controler_refuse_un_arret_actif_sur_l_ancien_venv(self):
        """#4437 : si le redémarrage n'a pas rechargé l'unité d'une persona
        arrêtée, le contrôle le dit au lieu de l'ignorer."""
        (self.state / 'units-actives').write_text('dev8 dev9 stopped\n')
        self._processus_venv(self.venv_current)
        self._processus_venv(self.venv_old, 'mainpid-stopped')
        p = self.run_vm('controler')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("stopped : le MainPID vivant n'exécute pas le venv installé",
                      p.stderr)
        self.assertNotIn('stopped : bail', p.stderr)
        self.assertNotIn('contrôles verts', p.stdout)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_controler_refuse_un_resident_desactive_actif_sur_l_ancien_venv(self):
        """#4239 × #4437 : une unité désactivée encore active est contrôlée sur
        le venv ; seule une unité inactive est hors périmètre."""
        agents = [dict(name=n, host='host-agents', mode='execute', status='idle')
                  for n in ('dev7', 'dev8', 'dev9', 'pilot-resident')]
        (self.state / 'disabled-pilot-resident').touch()
        (self.state / 'units-actives').write_text('dev8 dev9 pilot-resident\n')
        self._processus_venv(self.venv_current)
        self._processus_venv(self.venv_old, 'mainpid-pilot-resident')
        p = self.run_vm('controler', agents=agents)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("pilot-resident : le MainPID vivant n'exécute pas le venv"
                      ' installé', p.stderr)
        self.assertNotIn('pilot-resident : bail', p.stderr)
        self.assertNotIn('contrôles verts', p.stdout)

    def test_restart_without_prior_lease_reports_warning(self):
        p = self.run_vm(scenario='no-lease')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('WARNING dev8: not checked', p.stdout)
        self.assertIn('WARNING: 2 persona(s) not checked', p.stdout)
        self.assertIn('start ameesh-runner-agent@dev8.service', self.units())

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_runner_id_forms_and_default_are_accepted(self):
        for scenario in ['equals-owner', 'abbreviated-owner', 'last-owner', 'default-owner']:
            with self.subTest(scenario=scenario):
                p = self.run_vm('controler', scenario)
                self.assertEqual(p.returncode, 0, p.stderr)

    def test_unreadable_execstart_prevents_restart(self):
        for scenario in ['bad-execstart', 'execstart-error']:
            with self.subTest(scenario=scenario):
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0)
                self.assertNotIn(' stop ', self.units())
                self.assertNotIn(' start ', self.units())

    def test_check_rejects_foreign_owner(self):
        p = self.run_vm('controler', 'foreign-owner')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)

    def test_missing_runtime_commit_reports_failure_and_checks_leases(self):
        for metadata in self.venv_current.glob('lib/python*/site-packages/*/direct_url.json'):
            metadata.unlink()
        p = self.run_vm('controler', 'foreign-owner')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('runtime invité non prouvé', p.stderr)
        self.assertIn('bail', p.stderr)
        self.assertNotIn('contrôles verts', p.stdout)

    def test_check_uses_database_clock(self):
        p = self.run_vm('controler', 'db-ahead')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)

    def test_check_detects_active_unit_without_lease(self):
        p = self.run_vm('controler', 'no-lease')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)
        self.assertNotIn('restart', self.units())
        self.assertNotIn('start', self.units())

    def test_check_rejects_expired_lease(self):
        p = self.run_vm('controler', 'expired')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_check_accepts_held_lease_and_is_read_only(self):
        p = self.run_vm('controler')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('contrôles verts', p.stdout)
        self.assertNotIn('restart', self.units())
        self.assertNotIn('start', self.units())

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_check_elected_role_taken_by_other_host_is_a_warning(self):
        """#4239 : une relève légitime pendant `controler` aussi est un
        non-concluant, pas une preuve locale manquante qui ferait revenir
        l'hôte en arrière."""
        agents = [dict(name=n, host='host-agents', mode='execute', status='idle')
                  for n in ('dev7', 'dev8', 'dev9')]
        (self.state / 'role-dev8.json').write_text(json.dumps({
            'name': 'dev8', 'elected': True, 'holder': 'other-host',
            'lease_owner': 'runner-dev8@other-host', 'epoch': 9,
            'candidates': [{'host': 'other-host', 'ready': True},
                           {'host': 'host-agents', 'ready': True}]}))
        # le faux registre ne change le bail qu'après un redémarrage simulé
        (self.state / 'restart-dev8').touch()
        p = self.run_vm('controler', 'elu-ailleurs', agents=agents)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('WARNING dev8: bail du rôle élu repris par other-host '
                      '(époque 9 > 0) — reprise normale — preuve locale non'
                      ' concluante (sans retour)', p.stdout)
        self.assertIn('contrôles verts', p.stdout)
        self.assertNotIn('bail absent', p.stderr)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_disabled_experimental_resident_is_out_of_scope(self):
        """#4239 : un résident expérimental sans bail et à l'unité désactivée
        n'a pas d'exécuteur actif attendu ; il ne bloque ni le contrôle
        préalable ni le redémarrage du staging."""
        agents = [dict(name=n, host='host-agents', mode='execute', status='idle')
                  for n in ('dev7', 'dev8', 'dev9', 'pilot-resident')]
        (self.state / 'disabled-pilot-resident').touch()
        # #4239 : une unité désactivée et inactive n'a aucun exécuteur attendu ;
        # #4437 : elle n'est ni démarrée ni contrôlée.
        (self.state / 'inactive-pilot-resident').touch()
        p = self.run_vm('controler', 'resident-sans-bail', agents=agents)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('pilot-resident : unité désactivée, aucun exécuteur actif'
                      ' exigé — hors périmètre', p.stdout)
        self.assertNotIn('pilot-resident : unité inactive', p.stderr)
        self.assertNotIn('pilot-resident : bail absent', p.stderr)
        self.assertIn('contrôles verts', p.stdout)
        p = self.run_vm('redemarrer', 'resident-sans-bail', agents=agents)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('pilot-resident : unité désactivée, aucun exécuteur actif'
                      ' exigé — hors périmètre', p.stdout)
        self.assertNotIn('--user stop ameesh-runner-agent@pilot-resident.service',
                         self.units())
        self.assertNotIn('--user start ameesh-runner-agent@pilot-resident.service',
                         self.units())
        starts = self.starts()
        for n in ('dev7', 'dev8', 'dev9'):
            self.assertIn('--user start ameesh-runner-agent@%s.service' % n, starts)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_retired_agent_is_out_of_scope(self):
        """#4239 : un agent retiré du registre reste dans `ameesh list` avec
        `retired_ts` ; le staging ne le mène plus (cas pilot-resident-4064,
        retiré par le pilote pendant le staging v1.27.0)."""
        agents = [dict(name=n, host='host-agents', mode='execute', status='idle')
                  for n in ('dev7', 'dev8', 'dev9')]
        agents.append(dict(name='pilot-retired', host='host-agents',
                           mode='execute', status='idle', retired_ts=1.0))
        p = self.run_vm('controler', agents=agents)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('pilot-retired', p.stdout)
        self.assertNotIn('pilot-retired', p.stderr)
        self.assertIn('contrôles verts', p.stdout)
        p = self.run_vm('redemarrer', agents=agents)
        self.assertEqual(p.returncode, 0, p.stderr)
        for action in ('stop', 'start'):
            self.assertNotIn('--user %s ameesh-runner-agent@pilot-retired.service'
                             % action, self.units())
        starts = self.starts()
        for n in ('dev7', 'dev8', 'dev9'):
            self.assertIn('--user start ameesh-runner-agent@%s.service' % n, starts)

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_check_visits_every_registered_agent(self):
        """#1455: the client must not eat the list shared with the `while read`."""
        p = self.run_vm('controler')
        self.assertEqual(p.returncode, 0, p.stderr)
        seen = self.units()
        for name in ('dev8', 'dev9'):
            self.assertIn(f'ameesh-runner-agent@{name}.service', seen)
        self.assertIn('dev8', p.stdout)
        self.assertIn('dev9', p.stdout)

    def test_check_fails_when_only_the_last_agent_is_broken(self):
        """#1455: a defect on the last listed agent alone must red the check."""
        p = self.run_vm('controler', scenario='inactive-dev9')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('dev9', p.stdout)
        self.assertIn('contrôles en échec', p.stderr)

    def test_unit_failures_do_not_report_success(self):
        for scenario in ['stop-error', 'stop-unproven', 'stop-failed',
                         'restart-error', 'inactive']:
            with self.subTest(scenario=scenario):
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn('ameesh-runner-agent@dev8.service', p.stderr)
                self.assertNotIn('redémarré', p.stdout)

    def test_moved_agent_is_skipped_before_restart(self):
        p = self.run_vm(scenario='moved-before')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.gestes(), [])
        self.assertIn('dev9 : déplacé ou mode non execute (hôte=elsewhere), ignoré', p.stdout)

    def test_host_change_after_restart_is_not_a_valid_proof(self):
        p = self.run_vm(scenario='moved')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)

    def test_long_turn_is_drained_not_waited_out(self):
        # #1152/R1 (starvation impl07) : `running` à chaque lecture ne fait
        # plus expirer la mise à jour — chaque unité est arrêtée (drainage
        # borné par TimeoutStopSec), prouvée, puis démarrée.
        p = self.run_vm(scenario='always-busy')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('toujours en tour', p.stderr)
        for name in ('dev8', 'dev9'):
            self.assertIn('--user stop ameesh-runner-agent@%s.service' % name,
                          self.units())
            self.assertIn('--user start ameesh-runner-agent@%s.service' % name,
                          self.units())
        self.assertNotIn('restart', self.units())
        # DRAIN_ATTENTE vaut 0 par défaut (#1152) : aucun drain n'est posé.
        self.assertEqual(self.operateur(), '')

    def test_registry_errors_fail_before_restart(self):
        for scenario in ['list-error', 'bad-json']:
            with self.subTest(scenario=scenario):
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0)
                self.assertEqual(self.gestes(), [])

    def test_transient_empty_registry_is_reread_not_a_failure(self):
        """#4373 : une lecture vide passagère de `list --json` (relève d'un
        rôle élu, lien coupé) est relue ; le staging redémarre les agents."""
        p = self.run_vm(scenario='list-vide-puis-ok')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('JSONDecodeError', p.stderr)
        for name in ('dev8', 'dev9'):
            self.assertIn('--user start ameesh-runner-agent@%s.service' % name,
                          self.units())

    @unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
    def test_transient_empty_registry_is_reread_by_controler(self):
        """#4373 : la relecture vaut aussi pour l'étape controler."""
        p = self.run_vm('controler', scenario='list-vide-puis-ok')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('contrôles verts', p.stdout)

    def test_lost_transport_on_registry_read_is_reread(self):
        """#4373 : un transport SSH perdu (255) sur `list --json` est passager,
        comme pour les lectures `show` de #4358 ; relu, il ne fait pas échouer
        l'étape."""
        p = self.run_vm(scenario='list-255-puis-ok')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('--user start ameesh-runner-agent@dev9.service',
                      self.units())

    def test_persistent_empty_registry_is_an_explicit_failure(self):
        """#4373 : un registre encore vide après relectures reste un échec
        explicite — aucun geste, aucun hôte vert sans liste."""
        p = self.run_vm(scenario='list-vide')
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.gestes(), [])
        self.assertIn('registre de host-agents : lecture vide', p.stderr)
        self.assertNotIn('JSONDecodeError', p.stderr)

    def test_empty_host_is_not_green(self):
        p = self.run_vm(agents=[])
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.gestes(), [])
        self.assertIn('aucun agent', p.stderr)

    def test_registry_name_is_validated_before_shell_interpolation(self):
        p = self.run_vm(agents=[dict(name='dev9;touch injected', host='host-agents',
                                     mode='execute', status='idle')])
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.gestes(), [])
        self.assertIn('invalide', p.stderr)

    def test_all_valid_registry_names_restart(self):
        names = ['dev_1', 'Dev1', 'dev.1', 'd' * 64]
        p = self.run_vm(agents=[dict(name=n, host='host-agents', mode='execute', status='idle')
                                for n in names])
        self.assertEqual(p.returncode, 0, p.stderr)
        for name in names:
            self.assertIn('stop ameesh-runner-agent@' + name + '.service', self.units())
            self.assertIn('start ameesh-runner-agent@' + name + '.service', self.units())

    def test_invalid_name_is_named_and_skipped_without_blocking_valid_agents(self):
        bad = dict(name='bad;touch injected', host='host-agents', mode='execute', status='idle')
        p = self.run_vm(agents=self.agents + [bad])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("agent 'bad;touch injected' : nom invalide", p.stderr)
        self.assertIn('start ameesh-runner-agent@dev9.service', self.units())
        self.assertNotIn('injected', self.units())

    def test_mode_refuses_unsupported_action(self):
        p = self.run_vm('publier')
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.state / 'ssh.log').exists())


class SystemdEnvironmentFileTest(unittest.TestCase):
    def test_real_manager_keeps_literal_values(self):
        manager = shutil.which('systemd-run')
        ctl = shutil.which('systemctl')
        if not manager or not ctl:
            self.skipTest('systemd user manager unavailable')
        available = subprocess.run([ctl, '--user', 'show-environment'],
                                   capture_output=True, timeout=10)
        if available.returncode:
            self.skipTest('systemd user manager unavailable')
        with tempfile.TemporaryDirectory(prefix='ameesh-503-systemd-') as tmp:
            path = Path(tmp) / 'runner.env'
            expected = 'postgresql://test@invalid/mesh_$USER"quoted"#fragment'
            path.write_text('AMEESH_DSN=' + expected + '\n')
            result = subprocess.run([
                manager, '--user', '--pipe', '--wait', '--quiet', '-p',
                'EnvironmentFile=' + str(path), '--', sys.executable, '-c',
                'import os; print(os.environ["AMEESH_DSN"])'],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)


class SharedLeasePredicateTest(unittest.TestCase):
    def test_shared_criterion_for_restart_and_check(self):
        import time
        row = dict(status='idle', host='host-agents', lease_owner='runner',
                   lease_epoch=9, lease_expires_ts=time.time()+300)
        cases = [
            (row, 7, 'restart', 0),
            (dict(row, lease_epoch=7), 7, 'restart', 1),
            (dict(row, lease_owner=None), 7, 'restart', 1),
            (dict(row, status='stopped', lease_owner=None), 7, 'restart', 0),
            (dict(row, lease_owner='foreign'), 7, 'restart', 1),
            (row, 0, 'check', 0),
            (dict(row, lease_expires_ts=0), 0, 'check', 1),
            (dict(row, lease_expires_ts=None), 0, 'check', 1),
            (dict(row, status='stopped', lease_owner=None), 0, 'check', 0),
        ]
        for value, before, phase, expected in cases:
            with self.subTest(row=value, phase=phase):
                p = subprocess.run(['bash', '-c',
                    '. "$1"; bail_valide "$2" runner "$3"',
                    'test', str(ROOT / 'deploy/mise-a-jour/bail.sh'), str(before), str(time.time())],
                    input=json.dumps(value), capture_output=True, text=True)
                self.assertEqual(p.returncode, expected, p.stderr)


if __name__ == '__main__':
    unittest.main()
