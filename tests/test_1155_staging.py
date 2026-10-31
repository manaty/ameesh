# SPDX-License-Identifier: AGPL-3.0-only
"""#1155 staging sequence: four hosts, VM first, workstation last.

The orchestration is exercised against stub `vm.sh`/`poste.sh` and a stub
workstation venv, so no VM, unit, database or real mail is touched.
"""
from __future__ import annotations

import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/mise-a-jour'

#: variables du runner qui supplanteraient le HOME, la config ou l'etat
#: temporaires : jamais heritees par le banc (incident du 14/10 02:0xZ,
#: consigne mesh-design 02:21).
ENV_PREFIXES = ('AMEESH_', 'AGENT_MESH_', 'AGENT_MAIL_')
ENV_EXTRA = ('XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME')
#: DSN volontairement injoignable : une commande qui sort des stubs echoue
#: immediatement au lieu d'ecrire sur la base commune du mesh.
DSN_REFUSE = 'postgresql://refuse:refuse@127.0.0.1:1/refuse_1155'
#: variables du bac a sable : les seules AMEESH_*/AGENT_* admises dans l'env.
ENV_BAC = ('AMEESH_CONFIG', 'AMEESH_DSN', 'AGENT_MESH_DSN', 'AMEESH_SCHEMA',
           'AGENT_MESH_SCHEMA', 'AMEESH_STATE', 'AGENT_MESH_STATE',
           'AMEESH_THREADS', 'AGENT_MAIL_STATE', 'AGENT_MAIL_CONFIG')


def env_isole(environ=None):
    """Environnement du banc, debarrasse des reglages du runner qui
    supplanteraient le HOME, la config, l'etat ou le DSN reels (incident du
    14/10 02:0xZ ; replis AGENT_MESH_CONFIG, config historique et alias
    ``*_DATABASE_URL`` releves par l'auditeur le 14/10 02:26)."""
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items()
            if not k.startswith(ENV_PREFIXES) and k not in ENV_EXTRA}

#: a stub deploy script: logs the call, fails once on a scenario line
STUB = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
host=pc-smichea
if [ "$(basename "$0")" = vm.sh ]; then
  if [ "${1:-}" = agents ]; then
    host=$(sed -n 's/^HOTE_AGENTS=//p' "$BASCULE_ENV")
    shift
  else
    host=manaty-mesh-1
  fi
fi
action=${1:-}
if grep -qx "kill=$host:$action" "$state/scenario" 2>/dev/null; then
  kill -KILL "$PPID"
  exit 0
fi
# #4416 : un nouveau timer du canon manque jusqu'a l'installation explicite.
if grep -qx 'nouveau-timer' "$state/scenario" 2>/dev/null && [ "$host" != manaty-mesh-1 ]; then
  case "$action" in
    timers) touch "$state/timer-$host" ;;
    controler) [ -f "$state/timer-$host" ] || exit 1 ;;
  esac
fi
if [ "$host" = pc-smichea ]; then
  case "$action" in
    redemarrer|retour)
      . "$state/garde.sh"
      verifier_hors_agent || exit 1 ;;
  esac
fi
printf 'call host=%s action=%s args=%s\n' "$host" "$action" "$*" >> "$state/calls.log"
if grep -qx "ko=$host:$action" "$state/scenario" 2>/dev/null; then
  grep -vx "ko=$host:$action" "$state/scenario" > "$state/scenario.tmp" || true
  mv "$state/scenario.tmp" "$state/scenario"
  echo "echec injecte : $host $action" >&2
  exit 1
fi
exit 0
'''

#: the workstation venv, two entry points used by staging.sh
VENV_PYTHON = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
printf 'venv-python %s\n' "$*" >> "$state/calls.log"
if [ "${1:-}" = "-" ]; then exit 0; fi   # notification
if [ "${1:-}" = -m ] && [ "${2:-}" = ameesh.staging_watch ]; then shift; fi
if [[ "${1:-}" = */staging_watch.py || "${1:-}" = ameesh.staging_watch ]]; then
  if grep -qx suivi-off-after-start "$state/scenario" && test -s "$state/staging-snapshots.jsonl"; then exit 1; fi
  printf '%s\n' "${*:3}" >> "$state/suivi-args.log"
  # #4885 : debut exclusif refuse, sauf reprise d'une sequence prouvee arretee
  if grep -qx suivi-occupe "$state/scenario" && [[ " $* " = *" --exclusive "* && " $* " != *" --supersede "* ]]; then exit 75; fi
  printf '%s\n' "$2" >> "$state/staging-snapshots.jsonl"
  exit 0
fi
code=$(sed -n 's/^code-sonde=//p' "$state/scenario" 2>/dev/null | head -1)
if [ -n "$code" ]; then
  # #3976 : la sonde n'a pas pu tourner (127 : outil absent ; 3 : base
  # injoignable, rapport non concluant)
  [ "$code" = 3 ] && printf '{"verdict": "non-concluant", "erreur": "base injoignable"}\n'
  exit "$code"
fi
hote=""; precedent=""
for arg in "$@"; do
  [ "$precedent" = --hote ] && hote=$arg
  precedent=$arg
done
if grep -qx "ko=sante:$hote" "$state/scenario" 2>/dev/null; then
  grep -vx "ko=sante:$hote" "$state/scenario" > "$state/scenario.tmp" || true
  mv "$state/scenario.tmp" "$state/scenario"
  # le vrai sante-courrier.py sort son rapport JSON KO sur stdout, meme quand
  # il echoue : le banc le reproduit (#2190).
  printf '{"hote": "%s", "verdict": "ko", "personas": 1, "souffrance": [], "boites_mortes": [], "sonde": {"message": 94495, "destinataire": "ameesh-dev3", "delai_s": null, "remise_s": null, "hote_destinataire": "%s", "statut_cible": "idle", "tour": "non remis"}, "pourquoi": "echec injecte"}\n' "$hote" "$hote"
  echo "courrier $hote : KO (echec injecte)" >&2
  exit 1
fi
echo "courrier $hote : ok"
'''

VENV_AMEESH = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
printf 'venv-ameesh %s\n' "$*" >> "$state/calls.log"
if grep -qx "ko=harness" "$state/scenario" 2>/dev/null; then
  grep -vx "ko=harness" "$state/scenario" > "$state/scenario.tmp" || true
  mv "$state/scenario.tmp" "$state/scenario"
  echo "harness KO" >&2
  exit 1
fi
exit 0
'''

#: read-only ssh probes (installed sha, previous sha) find nothing: the stubs
#: above own every remote gesture.
SSH = r'''#!/usr/bin/env bash
printf 'stub %s %s\n' "$0" "$*" >> "${FAKE_STATE:?}/calls.log"
printf 'ssh %s\n' "$*" >> "${FAKE_STATE:?}/calls.log"
case "$*" in
  *"rev-parse --verify HEAD"*)
    # #4050 : clone de travail des migrations (DEPOT_VM) ; absent par scenario,
    # comme sur un hote neuf avant le premier staging.
    if grep -qx "clone=absent" "$FAKE_STATE/scenario" 2>/dev/null; then
      echo "fatal: not a git repository (or any of the parent directories): .git" >&2
      exit 128
    fi
    echo "0123456789abcdef0123456789abcdef01234567"
    exit 0 ;;
  *"cat > "*)
    # #3981 : journal copie sur HOTE_OUTILS avant actif.py ; la source arrive
    # sur l'entree standard de ssh.
    cat >/dev/null || true
    printf 'ssh-copie-journal %s\n' "$*" >> "$FAKE_STATE/calls.log"
    if grep -qx "ko=copie-journal" "$FAKE_STATE/scenario" 2>/dev/null; then
      echo "copie du journal refusee" >&2
      exit 1
    fi
    exit 0 ;;
  *"bash -s -- "*)
    cat >/dev/null || true
    if grep -qx "ko=migration" "$FAKE_STATE/scenario" 2>/dev/null; then
      grep -vx "ko=migration" "$FAKE_STATE/scenario" > "$FAKE_STATE/scenario.tmp" || true
      mv "$FAKE_STATE/scenario.tmp" "$FAKE_STATE/scenario"
      echo "migration injectee KO" >&2
      exit 1
    fi
    exit 0 ;;
  *"/venv/bin/python3 -"*)
    # #3976 : outil python lance sur HOTE_OUTILS (sonde, notification) ;
    # la source arrive sur l'entree standard.
    source=$(cat)
    printf 'ssh-python %s\n' "$*" >> "$FAKE_STATE/calls.log"
    case "$source" in
      *'EVENT = "staging.sequence"'*) exit 0 ;;
      *"def attestation_partagee"*)
        # #3981 : actif.py tourne la-bas, avec le journal recopie
        printf 'ssh-actif %s\n' "$*" >> "$FAKE_STATE/calls.log"
        exit 0 ;;
      *"mail.send(conn, work.SYSTEM_SENDER, dest"*) exit 0 ;;
    esac
    if grep -qx "ssh-python=127" "$FAKE_STATE/scenario" 2>/dev/null; then
      echo "bash: /home/ameesh/.local/share/ameesh/venv/bin/python3: No such file or directory" >&2
      exit 127
    fi
    hote=$(printf '%s\n' "$*" | sed -n "s/.*'--hote' '\([^']*\)'.*/\1/p")
    if grep -qx "ko=sante:$hote" "$FAKE_STATE/scenario" 2>/dev/null; then
      printf '{"hote": "%s", "verdict": "ko", "sonde": null, "pourquoi": "echec injecte"}\n' "$hote"
      exit 1
    fi
    printf '{"hote": "%s", "verdict": "ok"}\n' "$hote"
    exit 0 ;;
  *ameesh-sauvegarde.service*)
    # relecture dev1 #3848 : dump serveur quand le poste est absent
    cat >/dev/null || true
    printf 'sauvegarde-serveur %s\n' "$*" >> "$FAKE_STATE/calls.log"
    if grep -qx "ko=sauvegarde" "$FAKE_STATE/scenario" 2>/dev/null; then
      echo "sauvegarde injectee KO" >&2
      exit 1
    fi
    printf '/home/ameesh/sauvegardes/ameesh-2026-10-27.dump\n4096\n%064d\n' 0
    exit 0 ;;
esac
exit 255
'''

SYSTEMD_RUN = r'''
import json, os, subprocess, sys
from pathlib import Path
args = iter(sys.argv[1:])
env = dict(os.environ)
# Le gestionnaire ne transmet pas l'environnement de l'appelant.
for name in ('BASCULE_ENV', 'AMEESH_CONFIG'):
    env.pop(name, None)
unit = cwd = None
flags = []
for arg in args:
    if arg in ('--user', '--collect', '--wait', '--pipe'):
        flags.append(arg)
    elif arg == '--unit':
        unit = next(args)
    elif arg == '--working-directory':
        cwd = next(args)
    elif arg.startswith('--setenv='):
        name, value = arg.removeprefix('--setenv=').split('=', 1)
        env[name] = value
    else:
        command = [arg, *args]
        break
assert set(flags) == {'--user', '--collect', '--wait', '--pipe'}
assert unit.startswith('poste-maj-') and cwd
env['FAKE_CGROUP'] = '0::/user.slice/app.slice/' + unit + '.service'
state = Path(env['FAKE_STATE'])
with (state / 'units.jsonl').open('a') as log:
    log.write(json.dumps(dict(unit=unit, cwd=cwd, command=command,
                             bascule=env.get('BASCULE_ENV'),
                             config=env.get('AMEESH_CONFIG'))) + '\n')
if ('unit-ko=' + command[-1]) in (state / 'scenario').read_text().splitlines():
    sys.exit(23)
sys.exit(subprocess.run(command, env=env, cwd=cwd).returncode)
'''

# #3129 : le vrai cat, résolu avant les faux bancs (/usr/bin/cat sous Linux, /bin/cat sous macOS).
REAL_CAT = shutil.which('cat') or '/bin/cat'
CAT = r'''#!/bin/sh
case "$1" in
  /proc/*/cgroup) printf '%s\n' "${FAKE_CGROUP:-0::/user.slice/session.scope}" ;;
  *) exec @CAT@ "$@" ;;
esac
'''.replace('@CAT@', REAL_CAT)


class StagingTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1155-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        (self.state / 'calls.log').touch()
        (self.state / 'scenario').write_text('')
        # La vraie garde #2950, sans lancer les gestes du dispatcher poste.
        poste = (DEPLOY / 'poste.sh').read_text()
        garde = poste.split('verifier_hors_agent() {', 1)[1].split('\nredemarrer() {', 1)[0]
        (self.state / 'garde.sh').write_text('verifier_hors_agent() {' + garde)
        self.home = self.root / 'home'; self.home.mkdir()
        self.repo = self.root / 'repo'
        self.script_dir = self.repo / 'deploy/mise-a-jour'
        self.script_dir.mkdir(parents=True)
        for name in ('staging.sh', 'sante-courrier.py', 'actif.py'):
            (self.script_dir / name).write_text((DEPLOY / name).read_text())
        source = self.repo / 'src/ameesh'
        source.mkdir(parents=True)
        shutil.copy2(ROOT / 'src/ameesh/staging_watch.py', source)
        for name, source in (('vm.sh', STUB), ('poste.sh', STUB)):
            path = self.script_dir / name
            path.write_text(source)
            path.chmod(0o755)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        (self.bin / 'python3').symlink_to(sys.executable)
        # #3848 : le repli de detection de l'hote local ne doit pas dependre du
        # nom de la machine qui execute le banc.
        (self.bin / 'hostname').write_text('#!/usr/bin/env bash\necho ameesh-agents-1\n')
        (self.bin / 'hostname').chmod(0o755)
        for name, source in (('ssh', SSH), ('cat', CAT),
                             ('systemd-run', '#!' + sys.executable + '\n' + SYSTEMD_RUN)):
            path = self.bin / name
            path.write_text(source)
            path.chmod(0o755)
        venv = self.home / '.local/share/ameesh/venv/bin'; venv.mkdir(parents=True)
        for name, source in (('python', VENV_PYTHON), ('ameesh', VENV_AMEESH)):
            path = venv / name
            path.write_text(source)
            path.chmod(0o755)
        self.config = self.root / 'bascule.env'
        self.config.write_text(
            'HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 ameesh-agents-3=root@10.77.0.5 '
            'manaty-mesh-1=root@10.77.0.2 pc-smichea=poste"\n'
            'VM=10.77.0.2\nPERSONAS_VM=\n')
        self.repo = self.root / 'repo'
        run = ['git', '-C', str(self.repo)]
        subprocess.run(run + ['init', '-q'], check=True, capture_output=True)
        env = self.git_env()
        (self.repo / 'marque').write_text('x\n')
        subprocess.run(run + ['add', 'marque'], check=True, capture_output=True, env=env)
        subprocess.run(run + ['-c', 'user.email=t@invalid', '-c', 'user.name=t',
                              'commit', '-qm', 'base'], check=True, capture_output=True, env=env)
        # un deuxieme commit : la cible du staging doit avoir un parent
        (self.repo / 'marque').write_text('x\ny\n')
        subprocess.run(run + ['add', 'marque'], check=True, capture_output=True, env=env)
        subprocess.run(run + ['-c', 'user.email=t@invalid', '-c', 'user.name=t',
                              'commit', '-qm', 'tete'], check=True, capture_output=True, env=env)
        subprocess.run(run + ['update-ref', 'refs/remotes/origin/main', 'HEAD'],
                       check=True, capture_output=True, env=env)
        self.sha = subprocess.run(run + ['rev-parse', 'HEAD'], check=True,
                                  capture_output=True, text=True).stdout.strip()
        base = env_isole()
        bacs = {}
        for nom in ('etat', 'fils', 'v0', 'config', 'tmp'):
            bacs[nom] = self.root / nom
            bacs[nom].mkdir()
        self.env = dict(base, HOME=str(self.home), BASCULE_ENV=str(self.config),
                        FAKE_STATE=str(self.state),
                        PATH=str(self.bin) + os.pathsep + base.get('PATH', ''),
                        TMPDIR=str(bacs['tmp']),
                        # #3848 : le banc tourne sur une VM ; sans cette
                        # declaration, le poste serait (a juste titre) reporte
                        # absent et les sequences completes ne testeraient plus
                        # les cinq hotes.
                        HOTE_LOCAL='pc-smichea',
                        # jamais la configuration, l'etat ni le DSN reels : les
                        # replis de config.load() (AGENT_MESH_CONFIG, alias
                        # *_DATABASE_URL) sont retires par le filtre ; le chemin
                        # explicite absent est en plus refuse avant connexion
                        # (#1127), le DSN restant est injoignable, et toute
                        # ecriture irait sous le bac (incident du 14/10 02:0xZ ;
                        # consigne mesh-design 02:21 ; precision auditeur 02:26).
                        AMEESH_CONFIG=str(self.root / 'config-absente.json'),
                        AMEESH_STATE=str(bacs['etat']),
                        AGENT_MESH_STATE=str(bacs['etat']),
                        AMEESH_THREADS=str(bacs['fils']),
                        AGENT_MAIL_STATE=str(bacs['v0']),
                        AGENT_MAIL_CONFIG=str(bacs['config']),
                        AMEESH_DSN=DSN_REFUSE, AGENT_MESH_DSN=DSN_REFUSE,
                        AMEESH_SCHEMA='refuse_1155', AGENT_MESH_SCHEMA='refuse_1155',
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')

    def git_env(self):
        return dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                    GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')

    def run_staging(self, *args, env=None):
        env = env or self.env
        # garde-fous : le staging de test ne doit jamais lire le HOME, la
        # config, l'etat ni le DSN reels.
        self.assertEqual(env.get('HOME'), str(self.home))
        self.assertFalse(Path(env.get('AMEESH_CONFIG', '')).exists())
        for nom in ('AMEESH_STATE', 'AGENT_MESH_STATE', 'AMEESH_THREADS',
                    'AGENT_MAIL_STATE', 'AGENT_MAIL_CONFIG'):
            self.assertTrue(str(env.get(nom, '')).startswith(str(self.root)), nom)
        self.assertEqual(env.get('AMEESH_DSN'), DSN_REFUSE)
        p = subprocess.run(['bash', str(self.script_dir / 'staging.sh'), *args],
                           env=env, capture_output=True, text=True,
                           timeout=180)
        self.assert_no_escape()
        return p

    def assert_no_escape(self):
        """Tout executable trace doit etre un stub du bac a sable : le banc
        echoue si une commande sort des stubs ou de la config temporaire."""
        for line in self.calls():
            if line.startswith('stub '):
                chemin = line.split(' ', 2)[1]
                self.assertTrue(chemin.startswith(str(self.root)),
                                'executable hors bac a sable : %s' % line)

    def test_environment_is_isolated(self):
        """Aucun reglage herite du runner ne peut atteindre le mesh reel."""
        env = self.env
        for nom in ENV_BAC:
            self.assertIn(nom, env, nom)
        self.assertFalse(Path(env['AMEESH_CONFIG']).exists())
        for nom in ('AMEESH_STATE', 'AGENT_MESH_STATE', 'AMEESH_THREADS',
                    'AGENT_MAIL_STATE', 'AGENT_MAIL_CONFIG'):
            self.assertTrue(env[nom].startswith(str(self.root)), nom)
        for nom in ('AMEESH_DSN', 'AGENT_MESH_DSN'):
            self.assertEqual(env[nom], DSN_REFUSE, nom)
        for cle in env:
            if cle.startswith(ENV_PREFIXES):
                self.assertIn(cle, ENV_BAC, 'variable heritee : %s' % cle)

    def test_le_filtre_ecarte_les_replis_du_runner(self):
        """Temoin de non-recidive (#1195) : un banc qui herite du runner garde
        l'alias AGENT_MESH_CONFIG -- qui lit un fichier reel -- et les alias
        ``*_DATABASE_URL`` -- qui supplantent le DSN. Le banc ne les herite plus
        (temoins 1-2 : sans filtre ; temoin 3 : avec). Le chemin explicite
        absent du banc est en plus refuse par config.load avant connexion
        (#1127), et l'alias perime ne fait pas foi devant un fichier nomme."""
        from ameesh import config
        vide = self.root / 'config-vide.json'
        vide.write_text('{}')
        piege = self.root / 'piege.json'
        piege.write_text(json.dumps({'dsn': 'postgresql://piege@127.0.0.1/piege'}))
        # 1. sans AMEESH_CONFIG, l'alias AGENT_MESH_CONFIG lit un fichier reel
        repli = {'AGENT_MESH_CONFIG': str(piege)}
        self.assertEqual(config.load(dict(repli)).dsn,
                         'postgresql://piege@127.0.0.1/piege')
        # 2. les alias *_DATABASE_URL supplantent le DSN, meme quand un fichier
        #    de config vide est nomme explicitement
        alias = {'AMEESH_CONFIG': str(vide),
                 'AMEESH_DATABASE_URL': 'postgresql://alias@127.0.0.1/alias',
                 'AGENT_MESH_DATABASE_URL': 'postgresql://alias@127.0.0.1/alias'}
        self.assertEqual(config.load(dict(alias)).dsn,
                         'postgresql://alias@127.0.0.1/alias')
        # 3. le filtre du banc les ecarte tous
        herite = dict(repli, **alias)
        herite.update({'AMEESH_STATE': '/home/ameesh/.local/state/ameesh',
                       'AGENT_MAIL_STATE': '/home/ameesh/.local/state/agent-mail',
                       'XDG_STATE_HOME': '/home/ameesh/.local/state',
                       'PATH': '/usr/bin'})
        self.assertEqual(env_isole(herite), {'PATH': '/usr/bin'})

    def calls(self):
        return (self.state / 'calls.log').read_text().splitlines()

    def actions(self):
        out = []
        for line in self.calls():
            if line.startswith('call '):
                fields = dict(part.split('=', 1) for part in line[5:].split(' ', 2)[:2])
                out.append((fields['host'], fields['action']))
        return out

    def traces(self):
        journal = self.home / '.local/state/ameesh/mise-a-jour'
        files = sorted(journal.glob('staging-*.jsonl'))
        self.assertEqual(len(files), 1, files)
        return [json.loads(line) for line in files[0].read_text().splitlines()]

    def test_dry_run_touches_nothing(self):
        p = self.run_staging('--a-blanc')
        self.assertEqual(p.returncode, 0, p.stderr)
        for host in ('ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3', 'manaty-mesh-1', 'pc-smichea'):
            self.assertIn(host, p.stdout)
        self.assertEqual(self.calls(), [])
        self.assertIn('par hote installer, redemarrer, timers (agents et poste), controler, sante', p.stdout)
        self.assertIn('attestation « staging actif »', p.stdout)

    def test_new_role_timer_is_installed_before_each_vm_check(self):
        (self.state / 'scenario').write_text('nouveau-timer\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        actions = self.actions()
        for host in ('ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3', 'pc-smichea'):
            self.assertTrue((self.state / ('timer-' + host)).exists())
            self.assertLess(actions.index((host, 'redemarrer')), actions.index((host, 'timers')))
            self.assertLess(actions.index((host, 'timers')), actions.index((host, 'controler')))
            self.assertTrue(any(t['hote'] == host and t['etape'] == 'timers'
                                and t['verdict'] == 'ok' for t in self.traces()))

    def test_timer_install_failure_rolls_back_and_stops_staging(self):
        (self.state / 'scenario').write_text('ko=ameesh-agents-1:timers\n')
        p = self.run_staging()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(('ameesh-agents-1', 'retour'), self.actions())
        self.assertNotIn(('ameesh-agents-2', 'installer'), self.actions())
        self.assertTrue(any(t['etape'] == 'timers' and t['verdict'] == 'ko'
                            for t in self.traces()))

    def test_full_sequence_order_and_trace(self):
        p = self.run_staging()
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(self.actions(), [
            ('ameesh-agents-1', 'verifier'),
            ('ameesh-agents-2', 'verifier'),
            ('ameesh-agents-3', 'verifier'),
            ('manaty-mesh-1', 'verifier'),
            ('pc-smichea', 'verifier'),
            ('pc-smichea', 'sauvegarder'),
            ('pc-smichea', 'migrer'),
            ('ameesh-agents-1', 'installer'),
            ('ameesh-agents-1', 'redemarrer'),
            ('ameesh-agents-1', 'timers'),
            ('ameesh-agents-1', 'controler'),
            ('ameesh-agents-2', 'installer'),
            ('ameesh-agents-2', 'redemarrer'),
            ('ameesh-agents-2', 'timers'),
            ('ameesh-agents-2', 'controler'),
            ('ameesh-agents-3', 'installer'),
            ('ameesh-agents-3', 'redemarrer'),
            ('ameesh-agents-3', 'timers'),
            ('ameesh-agents-3', 'controler'),
            ('manaty-mesh-1', 'installer'),
            ('manaty-mesh-1', 'redemarrer'),
            ('manaty-mesh-1', 'controler'),
            ('pc-smichea', 'installer'),
            ('pc-smichea', 'role'),
            ('pc-smichea', 'redemarrer'),
            ('pc-smichea', 'timers'),
            ('pc-smichea', 'controler'),
        ])
        self.assertIn('venv-ameesh doctor --harness', self.calls())
        # #4050 : le clone des migrations n'est exige que du chemin sans poste ;
        # une sequence lancee sur le poste ne sonde pas les VM.
        self.assertFalse([ligne for ligne in self.calls()
                          if 'rev-parse --verify HEAD' in ligne])
        for host in ('ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3', 'manaty-mesh-1', 'pc-smichea'):
            self.assertIn('courrier %s : ok' % host, p.stdout)
        traces = self.traces()
        self.assertEqual(traces[-1]['etape'], 'sequence')
        self.assertEqual(traces[-1]['verdict'], 'ok')
        self.assertEqual(traces[-1]['sha'], self.sha)
        for hote in ('ameesh-agents-1', 'pc-smichea'):
            self.assertTrue(any(t['hote'] == hote and t['etape'] == 'sante'
                                and t['verdict'] == 'ok' for t in traces), traces)
        self.assertEqual((self.home / '.local/state/ameesh/mise-a-jour').stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.script_dir.glob('staging-*.env')), [])

    def units(self):
        return [json.loads(line) for line in
                (self.state / 'units.jsonl').read_text().splitlines()]

    def test_release_cgroup_restarts_and_rolls_back_in_separate_units(self):
        self.env['FAKE_CGROUP'] = '0::/user.slice/app.slice/ameesh-release.service'
        # Un chemin relatif avec espaces exige aussi le bon cwd dans l'unite.
        relative = Path(os.path.relpath(self.config, Path.cwd())).with_name('bascule test.env')
        self.config.rename(relative)
        self.env['BASCULE_ENV'] = str(relative)
        self.env['AMEESH_CONFIG'] = 'config test.json'
        (self.state / 'scenario').write_text('ko=sante:pc-smichea\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        units = self.units()
        self.assertEqual([u['command'][-1] for u in units], ['redemarrer', 'retour'])
        self.assertNotEqual(units[0]['unit'], units[1]['unit'])
        for unit in units:
            self.assertEqual(unit['cwd'], str(Path.cwd()))
            self.assertEqual(unit['bascule'], str(relative))
            self.assertEqual(unit['config'], 'config test.json')
        self.assertIn(('pc-smichea', 'redemarrer'), self.actions())
        self.assertIn(('pc-smichea', 'retour'), self.actions())
        self.assertTrue(any(t['etape'] == 'retour' and t['verdict'] == 'ok'
                            for t in self.traces()))

    def test_transient_failure_attempts_rollback_then_notifies(self):
        self.env['FAKE_CGROUP'] = '0::/user.slice/app.slice/ameesh-release.service'
        for rollback_fails in (False, True):
            with self.subTest(rollback_fails=rollback_fails):
                (self.state / 'calls.log').write_text('')
                (self.state / 'units.jsonl').write_text('')
                scenario = 'unit-ko=redemarrer\n'
                if rollback_fails:
                    scenario += 'unit-ko=retour\n'
                (self.state / 'scenario').write_text(scenario)
                p = self.run_staging()
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertEqual([u['command'][-1] for u in self.units()],
                                 ['redemarrer', 'retour'])
                # Aucun repli direct apres l'echec de systemd-run.
                self.assertNotIn(('pc-smichea', 'redemarrer'), self.actions())
                notifications = [l for l in self.calls() if l.startswith('venv-python - ')]
                self.assertEqual(len(notifications), 1)
                expected = 'retour de pc-smichea en ECHEC' if rollback_fails else 'retour effectue'
                self.assertIn(expected, notifications[0])
                self.assertEqual(('pc-smichea', 'retour') in self.actions(),
                                 not rollback_fails)

    def test_failure_stops_sequence_and_rolls_back_host(self):
        (self.state / 'scenario').write_text('ko=manaty-mesh-1:controler\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1, p.stdout)
        actions = self.actions()
        self.assertIn(('manaty-mesh-1', 'retour'), actions)
        self.assertEqual(actions[-1], ('manaty-mesh-1', 'controler'))  # rollback control
        self.assertNotIn(('pc-smichea', 'installer'), actions)
        self.assertNotIn(('manaty-mesh-1', 'sante'), actions)
        self.assertIn('sequence arretee sur manaty-mesh-1', p.stderr)
        notifications = [l for l in self.calls() if l.startswith('venv-python - ')]
        self.assertEqual(len(notifications), 1)
        self.assertIn('mesh-design', notifications[0])
        traces = self.traces()
        self.assertTrue(any(t['hote'] == 'manaty-mesh-1' and t['etape'] == 'controler'
                            and t['verdict'] == 'ko' for t in traces))
        self.assertTrue(any(t['hote'] == 'manaty-mesh-1' and t['etape'] == 'retour'
                            and t['verdict'] == 'ok' for t in traces))
        self.assertTrue(any(t['hote'] == 'manaty-mesh-1' and t['etape'] == 'controle-retour'
                            and t['verdict'] == 'ok' for t in traces))

    def test_failed_rollback_is_reported_and_stops(self):
        (self.state / 'scenario').write_text('ko=sante:ameesh-agents-1\nko=ameesh-agents-1:retour\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1)
        notifications = [l for l in self.calls() if l.startswith('venv-python - ')]
        self.assertEqual(len(notifications), 1)
        self.assertIn('retour de ameesh-agents-1 en ECHEC', notifications[0])
        self.assertNotIn(('ameesh-agents-2', 'installer'), self.actions())
        self.assertTrue(any(t['hote'] == 'ameesh-agents-1' and t['etape'] == 'retour'
                            and t['verdict'] == 'ko' for t in self.traces()))

    def test_sans_retour_stops_without_rollback_and_gives_the_manual_procedure(self):
        """#4616 : apres une migration non additive, un echec d'hote n'appelle
        jamais `retour` ; la sequence s'arrete et le pilote recoit la marche
        manuelle, rollback SQL d'abord, retour de code ensuite."""
        rollback = 'src/ameesh/migrations/rollback/0518_generic_delivery_state.sql'
        (self.state / 'scenario').write_text('ko=ameesh-agents-2:controler\n')
        p = self.run_staging('--sans-retour=' + rollback)
        self.assertEqual(p.returncode, 1, p.stdout)
        actions = self.actions()
        self.assertFalse([a for a in actions if a[1] == 'retour'], actions)
        self.assertEqual(actions[-1], ('ameesh-agents-2', 'controler'))
        self.assertNotIn(('ameesh-agents-3', 'installer'), actions)
        self.assertIn('sequence arretee sur ameesh-agents-2 (retour automatique desactive)',
                      p.stderr)
        trace = next(t for t in self.traces() if t['etape'] == 'retour')
        self.assertEqual((trace['hote'], trace['verdict']), ('ameesh-agents-2', 'desactive'))
        notifications = [l for l in self.calls() if l.startswith('venv-python - ')]
        self.assertEqual(len(notifications), 1)
        self.assertIn('retour automatique desactive', notifications[0])
        journal = (self.state / 'calls.log').read_text()
        self.assertLess(journal.index('2. rollback SQL d\'abord'),
                        journal.index('3. seulement ensuite, retour de code'))
        self.assertIn(rollback, journal)
        self.assertIn('vm.sh agents retour', journal)

    def test_sans_retour_is_named_in_the_attestation_request(self):
        """#4616 : une sequence saine menee avec --sans-retour le dit a actif.py."""
        p = self.run_vm('--sans-retour')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn(' --sans-retour', self.actif_args())
        p = self.run_staging('--a-blanc', '--sans-retour')
        self.assertIn('retour automatique desactive', p.stdout)

    def test_postponed_health_failure_rolls_back_the_workstation(self):
        (self.state / 'scenario').write_text('ko=sante:pc-smichea\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1)
        self.assertIn(('pc-smichea', 'retour'), self.actions())
        self.assertIn(('pc-smichea', 'controler'), self.actions())

    def test_health_failure_keeps_the_json_report_in_trace_and_notification(self):
        """#2190 : un KO de sonde garde son rapport JSON (message, motif) dans
        la trace et dans la notification, au lieu de « sante en echec » seul."""
        (self.state / 'scenario').write_text('ko=sante:ameesh-agents-1\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1)
        trace = next(t for t in self.traces()
                     if t['hote'] == 'ameesh-agents-1' and t['etape'] == 'sante'
                     and t['verdict'] == 'ko')
        self.assertEqual(trace['detail'], 'sante en echec')
        self.assertEqual(trace['rapport']['verdict'], 'ko')
        self.assertEqual(trace['rapport']['sonde']['message'], 94495)
        self.assertEqual(trace['rapport']['sonde']['tour'], 'non remis')
        journal = (self.state / 'calls.log').read_text()
        self.assertIn('Rapport de sonde (JSON)', journal)
        self.assertIn('"tour": "non remis"', journal)

    def test_second_staging_is_refused_by_the_operator_lock(self):
        """#2448 : une seule sequence staging a la fois, meme lancee a la main."""
        folder = self.home / '.local/state/ameesh/mise-a-jour'
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / 'staging.lock', 'w') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            p = self.run_staging()
        self.assertEqual(p.returncode, 75, p.stdout)
        self.assertIn('staging deja en cours', p.stderr)
        self.assertEqual(self.actions(), [])

    def test_preflight_failure_changes_nothing(self):
        (self.state / 'scenario').write_text('ko=ameesh-agents-2:verifier\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1)
        self.assertIn('preflight en echec', p.stderr)
        self.assertEqual(self.actions(), [
            ('ameesh-agents-1', 'verifier'),
            ('ameesh-agents-2', 'verifier'),
        ])
        self.assertFalse([line for line in self.calls()
                          if line.startswith('venv-python ')
                          and 'staging_watch' not in line])

    def test_migrations_failure_installs_nothing(self):
        (self.state / 'scenario').write_text('ko=pc-smichea:migrer\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1)
        actions = self.actions()
        self.assertIn(('pc-smichea', 'migrer'), actions)
        self.assertNotIn(('ameesh-agents-1', 'installer'), actions)

    def test_already_installed_workstation_is_not_reinstalled(self):
        court = self.sha[:7]
        (self.home / '.local/share/ameesh').mkdir(parents=True, exist_ok=True)
        (self.home / ('.local/share/ameesh/src-%s' % court)).mkdir()
        (self.home / '.local/share/ameesh/src-current').symlink_to('src-%s' % court)
        # N2 : la trace doit nommer la cible de retour conservee, pas la cible
        # courante.
        journal = self.home / '.local/state/ameesh/mise-a-jour'
        journal.mkdir(parents=True)
        (journal / 'precedent').write_text('src-deadbee\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 0, p.stderr)
        actions = self.actions()
        self.assertNotIn(('pc-smichea', 'installer'), actions)
        for action in ('role', 'redemarrer', 'controler'):
            self.assertIn(('pc-smichea', action), actions)
        self.assertTrue(any(t['hote'] == 'pc-smichea' and t['etape'] == 'installer'
                            and 'deja installe' in (t['detail'] or '')
                            for t in self.traces()))
        self.assertTrue(any(t['hote'] == 'pc-smichea' and t['etape'] == 'installer'
                            and t['precedent'] == 'src-deadbee'
                            for t in self.traces()))

    def test_subset_keeps_canonical_order_without_poste(self):
        p = self.run_staging('--hotes', 'ameesh-agents-2 ameesh-agents-1', '--sans-sonde')
        self.assertEqual(p.returncode, 0, p.stderr)
        installes = [h for h, a in self.actions() if a == 'installer']
        self.assertEqual(installes, ['ameesh-agents-1', 'ameesh-agents-2'])
        self.assertNotIn(('pc-smichea', 'installer'), self.actions())
        self.assertNotIn(('pc-smichea', 'redemarrer'), self.actions())
        self.assertNotIn('manaty-mesh-1', [h for h, _ in self.actions()])
        migrations = [l for l in self.calls() if 'action=migrer' in l]
        self.assertEqual(len(migrations), 1)
        self.assertIn('--additif-seulement %s' % self.sha, migrations[0])

    def test_subset_with_poste_is_refused(self):
        """canon §14 : le poste ne se met a jour qu'apres les quatre VM saines ;
        un sous-ensemble qui l'inclut n'en apporte aucune preuve (avis R1 B4).
        Le refus porte sur la selection normalisee : les doublons ne comptent
        pas, et tabulation/nouvelle ligne ne contournent plus le controle."""
        cas = {
            'poste seul': 'pc-smichea',
            'poste puis une VM': 'pc-smichea ameesh-agents-1',
            'poste repete quatre fois (zero VM)':
                'pc-smichea pc-smichea pc-smichea pc-smichea',
            'VM + tabulation + poste': 'ameesh-agents-1\tpc-smichea',
            'VM + nouvelle ligne + poste': 'ameesh-agents-1\npc-smichea',
        }
        for nom, sous_ensemble in cas.items():
            with self.subTest(nom):
                p = self.run_staging('--hotes', sous_ensemble)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn('sequence complete des cinq hotes', p.stderr)
                self.assertEqual(self.calls(), [])

    def test_subset_deduplicates_host_names(self):
        """Selection unique : un nom repete ne fait pas rejouer l'hote."""
        p = self.run_staging('--hotes',
                             'ameesh-agents-2 ameesh-agents-1 ameesh-agents-2',
                             '--sans-sonde')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        installes = [h for h, a in self.actions() if a == 'installer']
        self.assertEqual(installes, ['ameesh-agents-1', 'ameesh-agents-2'])

    def test_topology_is_required_and_consistent(self):
        cases = {
            'sans topologie': 'VM=10.77.0.2\nPERSONAS_VM=\n',
            'hote inconnu': ('HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 ameesh-agents-3=root@10.77.0.5 '
                             'manaty-mesh-1=root@10.77.0.2 poste-x=root@10.77.0.9"\nVM=10.77.0.2\n'),
            'vm differente': ('HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 ameesh-agents-3=root@10.77.0.5 '
                              'manaty-mesh-1=root@10.77.0.2 pc-smichea=poste"\nVM=10.77.0.9\n'),
            # #3848 : la sequence tourne ici sur le poste (HOTE_LOCAL) ; une
            # topologie qui l'omet ne peut pas le declarer absent.
            'topologie locale sans poste': (
                'HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 ameesh-agents-3=root@10.77.0.5 '
                'manaty-mesh-1=root@10.77.0.2"\nVM=10.77.0.2\n'),
            'poste distant': ('HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 ameesh-agents-3=root@10.77.0.5 '
                              'manaty-mesh-1=root@10.77.0.2 pc-smichea=root@10.77.0.5"\nVM=10.77.0.2\n'),
            'sans manaty-mesh-1': (
                'HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 ameesh-agents-3=root@10.77.0.5 '
                'pc-smichea=poste"\nVM=10.77.0.2\n'),
            'sans ameesh-agents-2': (
                'HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 '
                'manaty-mesh-1=root@10.77.0.2 pc-smichea=poste"\nVM=10.77.0.2\n'),
        }
        for nom, contenu in cases.items():
            with self.subTest(nom):
                self.config.write_text(contenu)
                p = self.run_staging()
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertEqual(self.calls(), [])

    def test_third_agent_missing_or_postponed_is_refused_before_mutation(self):
        complete = self.config.read_text()
        self.config.write_text(complete.replace('ameesh-agents-3=root@10.77.0.5 ', ''))
        p = self.run_staging()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn('ameesh-agents-3', p.stderr)
        self.assertEqual(self.calls(), [])
        self.config.write_text(complete)
        p = self.run_staging('--absent', 'ameesh-agents-3=indisponible')
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn('non reportable', p.stderr)
        self.assertEqual(self.calls(), [])

    def test_third_agent_alone_can_run_migrations_but_cannot_attest(self):
        p = self.run_vm('--hotes', 'ameesh-agents-3')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([a for a in self.actions() if a[1] == 'installer'],
                         [('ameesh-agents-3', 'installer')])
        self.assertTrue(any(t['hote'] == 'ameesh-agents-3' and t['etape'] == 'migrations'
                            for t in self.traces()))
        # #4050 : le clone de migrations est verifie au preflight, avant la
        # migration qui part de cette VM.
        sondes = [i for i, ligne in enumerate(self.calls())
                  if ligne.startswith('stub ') and 'rev-parse --verify HEAD' in ligne]
        self.assertEqual(len(sondes), 1, self.calls())
        migrations = [i for i, ligne in enumerate(self.calls())
                      if ligne.startswith('stub ') and 'bash -s -- %s' % self.sha in ligne]
        self.assertEqual(len(migrations), 1, self.calls())
        self.assertLess(sondes[0], migrations[0])
        self.assertNotIn('actif.py', ' '.join(self.calls()))

    def test_vm_migration_host_without_clone_is_refused_before_mutation(self):
        """#4050 : hote neuf sans clone de migrations (~/work/ameesh-release) :
        refus nomme au preflight, avec le geste exact, avant sauvegarde,
        migration et tout arret de runner ; aucun retour automatique."""
        (self.state / 'scenario').write_text('clone=absent\n')
        p = self.run_vm('--hotes', 'ameesh-agents-3')
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('clone des migrations absent ou illisible sur ameesh-agents-3',
                      p.stderr)
        self.assertIn('/home/ameesh/work/ameesh-release', p.stderr)
        self.assertIn('git clone https://github.com/manaty/ameesh.git '
                      '/home/ameesh/work/ameesh-release', p.stderr)
        self.assertIn('aucun retour automatique', p.stderr)
        self.assertTrue(any('rev-parse --verify HEAD' in ligne
                            for ligne in self.calls()))
        # le preflight s'arrete la : aucune sauvegarde, migration (bash -s),
        # installation ni retour ; rien d'autre n'a ete appele.
        self.assertFalse([ligne for ligne in self.calls()
                          if ligne.startswith('sauvegarde-serveur ')])
        self.assertFalse([ligne for ligne in self.calls()
                          if ligne.startswith('stub ') and 'bash -s -- ' in ligne])
        self.assertEqual(self.actions(), [], self.calls())
        self.assertNotIn('actif.py', ' '.join(self.calls()))
        trace = next(t for t in self.traces() if t['etape'] == 'verifier')
        self.assertEqual((trace['hote'], trace['verdict']), ('ameesh-agents-3', 'ko'))
        self.assertIn('clone des migrations', trace['detail'])

    def test_third_agent_failure_rolls_back_before_workstation(self):
        (self.state / 'scenario').write_text('ko=sante:ameesh-agents-3\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(('ameesh-agents-3', 'retour'), self.actions())
        self.assertNotIn(('pc-smichea', 'installer'), self.actions())
        self.assertNotIn('actif.py', ' '.join(self.calls()))

    def run_vm(self, *args, **kw):
        """Sequence lancee depuis une VM : le poste est absent par nature."""
        env = dict(kw.pop('env', self.env), HOTE_LOCAL='ameesh-agents-1')
        return self.run_staging(*args, env=env, **kw)

    def actif_args(self):
        lignes = [l for l in self.calls()
                  if l.startswith('venv-python ') and 'actif.py' in l]
        self.assertEqual(len(lignes), 1, self.calls())
        return lignes[0]

    def test_vm_sequence_reports_poste_absent_and_attests_four_vms(self):
        """#3848 : depuis une VM, le poste est saute avec trace, les quatre VM
        sont deployees et le signal actif est demande avec --absent."""
        p = self.run_vm('--lots', '917', '--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        actions = self.actions()
        self.assertFalse([a for a in actions if a[0] == 'pc-smichea'], actions)
        self.assertEqual([a for a in actions if a[1] == 'installer'],
                         [('ameesh-agents-1', 'installer'),
                          ('ameesh-agents-2', 'installer'),
                          ('ameesh-agents-3', 'installer'),
                          ('manaty-mesh-1', 'installer')])
        # sauvegarde du poste non jouee, remplacee par le dump du serveur
        # avant les migrations (relecture dev1 #3848) ; migrations depuis agents-2
        self.assertNotIn(('pc-smichea', 'sauvegarder'), actions)
        sauvegardes = [i for i, ligne in enumerate(self.calls())
                       if ligne.startswith('sauvegarde-serveur ')]
        self.assertEqual(len(sauvegardes), 1, self.calls())
        self.assertIn('root@10.77.0.2', self.calls()[sauvegardes[0]])
        trace_sauvegarde = next(t for t in self.traces() if t['etape'] == 'sauvegarde')
        self.assertEqual(trace_sauvegarde['hote'], 'manaty-mesh-1')
        self.assertEqual(trace_sauvegarde['verdict'], 'ok')
        self.assertIn('ameesh-2026-10-27.dump', trace_sauvegarde['detail'])
        self.assertNotIn(('pc-smichea', 'migrer'), actions)
        migrations = [i for i, ligne in enumerate(self.calls())
                      if ligne.startswith('stub ') and 'bash -s -- %s' % self.sha in ligne]
        self.assertEqual(len(migrations), 1, self.calls())
        self.assertIn('root@10.77.0.4', self.calls()[migrations[0]])
        premier_install = next(i for i, ligne in enumerate(self.calls())
                               if 'action=installer' in ligne)
        self.assertLess(sauvegardes[0], migrations[0])
        self.assertLess(migrations[0], premier_install)
        trace_migration = next(t for t in self.traces() if t['etape'] == 'migrations')
        self.assertEqual(trace_migration['hote'], 'ameesh-agents-2')
        self.assertEqual(trace_migration['verdict'], 'ok')
        self.assertIn('poste absent', p.stdout)
        self.assertIn('mise a jour a son retour', p.stdout)
        trace = next(t for t in self.traces()
                     if t['hote'] == 'pc-smichea' and t['etape'] == 'absent')
        self.assertEqual(trace['verdict'], 'reporte')
        self.assertEqual(trace['sha'], self.sha)
        self.assertIn('mise a jour a son retour', trace['detail'])
        ligne = self.actif_args()
        for hote in ('ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3', 'manaty-mesh-1'):
            self.assertIn('--hote %s=' % hote, ligne)
        self.assertNotIn('--hote pc-smichea=', ligne)
        self.assertIn('--absent pc-smichea=', ligne)
        self.assertIn('--mode manaty-mesh-1=serveur', ligne)
        self.assertIn('--lot 917', ligne)
        self.assertIn('--rapporteur docs01', ligne)

    def test_vm_sequence_without_server_access_reports_it(self):
        """#3848 : sans acces root au serveur, il est reporte comme le poste ;
        les trois VM d'agents restent requises et attestees."""
        p = self.run_vm('--absent', 'manaty-mesh-1=sans-acces')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        actions = self.actions()
        self.assertFalse([a for a in actions if a[0] == 'manaty-mesh-1'], actions)
        self.assertEqual([a for a in actions if a[1] == 'installer'],
                         [('ameesh-agents-1', 'installer'),
                          ('ameesh-agents-2', 'installer'),
                          ('ameesh-agents-3', 'installer')])
        migrations = [i for i, ligne in enumerate(self.calls())
                      if ligne.startswith('stub ') and 'bash -s -- %s' % self.sha in ligne]
        self.assertEqual(len(migrations), 1, self.calls())
        trace = next(t for t in self.traces()
                     if t['hote'] == 'manaty-mesh-1' and t['etape'] == 'absent')
        self.assertEqual(trace['verdict'], 'reporte')
        self.assertEqual(trace['sha'], self.sha)
        self.assertIn('sans-acces', trace['detail'])
        self.assertIn('manaty-mesh-1 : absent', p.stdout)
        # aucun dump tente sur un serveur sans acces ; le serveur reporte ne
        # porte que son evenement absent (actif.py, preuve_absence ; #3853)
        self.assertFalse([l for l in self.calls() if l.startswith('sauvegarde-serveur ')])
        self.assertIn('sauvegarde sautee', p.stdout)
        self.assertEqual([t['etape'] for t in self.traces()
                          if t['hote'] == 'manaty-mesh-1'], ['absent'])
        ligne = self.actif_args()
        self.assertIn('--absent manaty-mesh-1=sans-acces', ligne)
        self.assertIn('--absent pc-smichea=', ligne)
        self.assertNotIn('--hote manaty-mesh-1=', ligne)
        self.assertIn('--hote ameesh-agents-1=', ligne)
        self.assertIn('--hote ameesh-agents-2=', ligne)

    def test_vm_backup_failure_migrates_nothing(self):
        """Dump serveur en echec : ni migration ni installation."""
        (self.state / 'scenario').write_text('ko=sauvegarde\n')
        p = self.run_vm()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('sauvegarde serveur en echec', p.stderr)
        self.assertFalse([l for l in self.calls()
                          if l.startswith('stub ') and 'bash -s -- ' in l], self.calls())
        self.assertNotIn(('ameesh-agents-1', 'installer'), self.actions())
        trace = next(t for t in self.traces() if t['etape'] == 'sauvegarde')
        self.assertEqual((trace['hote'], trace['verdict']), ('manaty-mesh-1', 'ko'))

    def test_vm_sans_sauvegarde_skips_the_server_dump(self):
        p = self.run_vm('--sans-sauvegarde')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertFalse([l for l in self.calls() if l.startswith('sauvegarde-serveur ')])

    def test_vm_migration_failure_installs_nothing(self):
        """Une migration en echec arrete avant toute installation (#3848)."""
        (self.state / 'scenario').write_text('ko=migration\n')
        p = self.run_vm()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('migrations en echec', p.stderr)
        self.assertNotIn(('ameesh-agents-1', 'installer'), self.actions())
        self.assertNotIn('actif.py', ' '.join(self.calls()))
        trace = next(t for t in self.traces() if t['etape'] == 'migrations')
        self.assertEqual(trace['verdict'], 'ko')

    def test_topology_composed_from_per_host_envs(self):
        """#3848 : bureau-goal n'a qu'un fichier par VM ; SERVEUR_SSH complete."""
        dossier = self.root / 'bascule-composee'; dossier.mkdir()
        (dossier / 'bascule-a1.env').write_text(
            'AGENTS_SSH=root@10.77.0.3\nHOTE_AGENTS=ameesh-agents-1\n'
            'PERSONAS_VM=\nDRAIN_ATTENTE=0\nAMEESH_OPERATEUR=bureau\n')
        (dossier / 'bascule-a2.env').write_text(
            'AGENTS_SSH=root@10.77.0.4\nHOTE_AGENTS=ameesh-agents-2\n'
            'PERSONAS_VM=\nDRAIN_ATTENTE=0\nAMEESH_OPERATEUR=bureau\n')
        (dossier / 'bascule-agents-3.env').write_text(
            'AGENTS_SSH=root@10.77.0.5\nHOTE_AGENTS=ameesh-agents-3\nPERSONAS_VM=\n')
        env = dict(self.env, BASCULE_ENV=str(dossier / 'bascule-a1.env'),
                   HOTE_LOCAL='ameesh-agents-1', SERVEUR_SSH='root@10.77.0.2')
        env.pop('VM', None)
        p = self.run_staging('--a-blanc', env=env)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn('topologie staging composee', p.stderr)
        for hote in ('ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3', 'manaty-mesh-1'):
            self.assertIn(hote, p.stdout)
        self.assertIn('pc-smichea', p.stdout)
        self.assertIn('absent', p.stdout)

    def test_composed_topology_without_server_target_is_refused(self):
        dossier = self.root / 'bascule-composee'; dossier.mkdir()
        for nom, cible in (('a1', 'root@10.77.0.3'), ('a2', 'root@10.77.0.4'), ('agents-3', 'root@10.77.0.5')):
            (dossier / ('bascule-%s.env' % nom)).write_text(
                'AGENTS_SSH=%s\nHOTE_AGENTS=ameesh-agents-%s\nPERSONAS_VM=\n'
                % (cible, nom[-1]))
        env = dict(self.env, BASCULE_ENV=str(dossier / 'bascule-a1.env'),
                   HOTE_LOCAL='ameesh-agents-1')
        env.pop('SERVEUR_SSH', None); env.pop('VM', None)
        p = self.run_staging('--a-blanc', env=env)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn('SERVEUR_SSH', p.stderr)
        self.assertEqual(self.calls(), [])

    def test_vm_sequence_detects_local_host_without_HOTE_LOCAL(self):
        """Repli hostname : le banc tourne sur ameesh-agents-1, pas sur le poste."""
        env = dict(self.env)
        env.pop('HOTE_LOCAL', None)
        p = self.run_staging(env=env)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertFalse([a for a in self.actions() if a[0] == 'pc-smichea'])
        self.assertIn('--absent pc-smichea=', self.actif_args())

    def test_hote_local_from_AMEESH_HOST_is_the_poste(self):
        """Un service du poste pose AMEESH_HOST : le poste reste present."""
        env = dict(self.env)
        env.pop('HOTE_LOCAL', None)
        env['AMEESH_HOST'] = 'pc-smichea'
        p = self.run_staging('--a-blanc', env=env)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn('pc-smichea', p.stdout)
        self.assertNotIn('absent (reporte)', p.stdout)

    def test_vm_sequence_accepts_topology_without_poste(self):
        self.config.write_text(
            'HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 ameesh-agents-3=root@10.77.0.5 '
            'manaty-mesh-1=root@10.77.0.2"\nVM=10.77.0.2\nPERSONAS_VM=\n')
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn('--absent pc-smichea=', self.actif_args())
        self.assertTrue(any(t['etape'] == 'absent' and t['hote'] == 'pc-smichea'
                            for t in self.traces()))

    def test_sans_poste_declares_the_absence_on_the_poste_itself(self):
        p = self.run_staging('--sans-poste')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertFalse([a for a in self.actions() if a[0] == 'pc-smichea'])
        self.assertIn("declare absent par l'operateur", p.stdout)
        self.assertIn('--absent pc-smichea=', self.actif_args())

    def test_vm_subset_of_two_vms_still_attests_nothing(self):
        p = self.run_vm('--hotes', 'ameesh-agents-1 ameesh-agents-2')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn('sequence partielle', p.stderr)
        self.assertNotIn('actif.py', ' '.join(self.calls()))

    def test_vm_hotes_naming_absent_poste_ignores_it(self):
        """Poste absent : nomme dans --hotes, il est ignore ; le reste est une
        selection de VM ordinaire (relecture dev1 #3848, plus de sortie 2)."""
        p = self.run_vm('--hotes', 'pc-smichea ameesh-agents-1', '--sans-sonde')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('pc-smichea ignore (poste absent', p.stderr)
        self.assertNotIn('sequence complete des cinq hotes', p.stderr)
        self.assertEqual([a for a in self.actions() if a[1] == 'installer'],
                         [('ameesh-agents-1', 'installer')])
        self.assertIn('sequence partielle', p.stderr)
        self.assertNotIn('actif.py', ' '.join(self.calls()))
        p = self.run_vm('--hotes', 'pc-smichea ameesh-agents-1 ameesh-agents-2 ameesh-agents-3 manaty-mesh-1')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('--absent pc-smichea=', self.actif_args())

    def run_bureau(self, *args, sans_venv=True):
        """#3976 : sequence lancee depuis bureau-goal, sans venv ameesh."""
        if sans_venv:
            shutil.rmtree(self.home / '.local/share/ameesh/venv')
        env = dict(self.env, HOTE_LOCAL='bureau-goal')
        return self.run_staging(*args, env=env)

    def ssh_python(self):
        # #3981 : actif.py tourne aussi par ssh, mais c'est un autre outil ;
        # les temoins de sonde comptent les seules sondes de courrier.
        return [l for l in self.calls()
                if l.startswith('ssh-python ') and '--journal' not in l
                and '"run":' not in l]

    def ssh_actif(self):
        return [l for l in self.calls() if l.startswith('ssh-actif ')]

    def test_bureau_actif_tourne_sur_hote_outils_avec_le_journal(self):
        """#3981 : sans venv local, actif.py tourne sur HOTE_OUTILS avec le
        journal recopie et le venv de la VM ; aucune attestation locale."""
        p = self.run_bureau()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.ssh_actif()), 1, self.calls())
        ligne = self.ssh_actif()[0]
        self.assertIn('root@10.77.0.4', ligne)
        self.assertIn('runuser -u ameesh -- env HOME=/home/ameesh', ligne)
        self.assertIn("'--journal'", ligne)
        self.assertIn('/home/ameesh/.local/state/ameesh/mise-a-jour/staging-', ligne)
        copies = [l for l in self.calls() if l.startswith('ssh-copie-journal ')]
        self.assertEqual(len(copies), 1, self.calls())
        self.assertIn('chown ameesh:ameesh', copies[0])
        self.assertFalse([l for l in self.calls() if l.startswith('venv-python ')])
        self.assertFalse(list(
            (self.home / '.local/state/ameesh/mise-a-jour').glob('actif-*.json')))

    def test_bureau_journal_non_copie_aucun_actif_et_le_dit(self):
        """#3981 : sans journal sur HOTE_OUTILS, actif.py n'est pas lance ;
        le deploiement reste fait (signal best-effort) et l'echec est dit."""
        (self.state / 'scenario').write_text('ko=copie-journal\n')
        p = self.run_bureau()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('journal non copie', p.stderr)
        self.assertIn(self.sha[:9], p.stdout)
        self.assertFalse(self.ssh_actif())
        self.assertFalse(list(
            (self.home / '.local/state/ameesh/mise-a-jour').glob('actif-*.json')))

    def test_bureau_sonde_le_courrier_sur_une_vm_d_agents(self):
        """#3976 : hors du poste et des VM d'agents, la sonde tourne sur
        HOTE_OUTILS (agents-2 par defaut), sous ameesh, avec l'instantane du
        commit ; aucun python local."""
        p = self.run_bureau()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.ssh_python()), 4, self.calls())
        for ligne in self.ssh_python():
            self.assertIn('root@10.77.0.4', ligne)
            self.assertIn('runuser -u ameesh -- env HOME=/home/ameesh', ligne)
            self.assertIn("PYTHONPATH='/home/ameesh/work/snap-%s/src'" % self.sha[:8], ligne)
        for hote in ('ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3', 'manaty-mesh-1'):
            self.assertTrue(any("'--hote' '%s'" % hote in l for l in self.ssh_python()), hote)
        self.assertFalse([l for l in self.calls() if l.startswith('venv-python ')])
        self.assertEqual([t['verdict'] for t in self.traces() if t['etape'] == 'sante'],
                         ['ok', 'ok', 'ok', 'ok'])
        self.assertFalse([t for t in self.traces() if t['etape'] == 'retour'])

    def test_bureau_hote_outils_explicite_et_valide(self):
        env = dict(self.env, HOTE_LOCAL='bureau-goal', HOTE_OUTILS='manaty-mesh-1')
        p = self.run_staging('--a-blanc', env=env)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn('HOTE_OUTILS', p.stderr)
        env['HOTE_OUTILS'] = 'ameesh-agents-1'
        p = self.run_staging('--a-blanc', env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_bureau_sonde_ko_remet_l_hote_en_arriere_et_notifie_par_la_vm(self):
        """Une sonde qui a tourne et constate un defaut reste « malsain »."""
        (self.state / 'scenario').write_text('ko=sante:ameesh-agents-1\n')
        p = self.run_bureau()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(('ameesh-agents-1', 'retour'), self.actions())
        # la notification part aussi par la VM (source sur l'entree standard)
        self.assertEqual(len(self.ssh_python()), 2, self.calls())

    def test_bureau_outil_de_sonde_absent_ne_remet_rien_en_arriere(self):
        """#3976, temoin : python de sonde absent sur la VM (127) = non
        concluant : aucun retour ; #4126 (decision du pilote) : la sequence
        continue sur les hotes suivants, et actif.py juge la mention."""
        (self.state / 'scenario').write_text('ssh-python=127\n')
        p = self.run_bureau()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse([a for a in self.actions() if a[1] == 'retour'])
        self.assertIn(('ameesh-agents-2', 'installer'), self.actions())
        self.assertIn('non concluante', p.stderr)
        self.assertIn('la sequence continue', p.stderr)
        trace = [t for t in self.traces() if t['etape'] == 'sante']
        self.assertEqual(trace[0]['hote'], 'ameesh-agents-1')
        self.assertEqual({t['verdict'] for t in trace}, {'non-concluant'})
        self.assertIn('code 127', trace[0]['detail'])
        self.assertFalse([t for t in self.traces() if t['etape'] == 'retour'])
        self.assertIn(('sequence', 'ok'), [(t['etape'], t['verdict'])
                                           for t in self.traces()])
        self.assertIn('sonde non concluante (reportee au staging suivant)', p.stderr)

    def test_sonde_non_concluante_en_local_ne_remet_rien_en_arriere(self):
        """Poste : outil absent (127) ou base injoignable (rapport non
        concluant, 3) n'est jamais un hote malsain ; #4126 : la sequence
        continue, la notification le dit, actif.py est appele."""
        for code in ('127', '3'):
            with self.subTest(code=code):
                (self.state / 'calls.log').write_text('')
                (self.state / 'scenario').write_text('code-sonde=%s\n' % code)
                p = self.run_staging()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertFalse([a for a in self.actions() if a[1] == 'retour'])
                self.assertIn('non concluante', p.stderr)
                self.assertTrue(any(l.startswith('venv-python - ') for l in self.calls()),
                                'notification attendue')
                self.assertIn('actif.py', ' '.join(self.calls()))

    def test_4126_attente_cible_transmise_a_la_sonde(self):
        """#4126 : ATTENTE_CIBLE (BASCULE_ENV, 300 s par defaut) borne
        l'attente d'une cible nommee occupee."""
        p = self.run_staging()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        sondes = [l for l in self.calls() if 'sante-courrier.py' in l]
        self.assertTrue(sondes, self.calls())
        self.assertIn('--attente-cible 300', sondes[0])

    def test_outside_main_is_refused_without_override(self):
        self.repo.joinpath('marque').write_text('y\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'marque'], check=True,
                       capture_output=True, env=self.git_env())
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.email=t@invalid',
                        '-c', 'user.name=t', 'commit', '-qm', 'next'], check=True,
                       capture_output=True, env=self.git_env())
        p = self.run_staging()
        self.assertEqual(p.returncode, 2)
        self.assertIn("n'est pas un ancetre", p.stderr)
        env = dict(self.env, HORS_MAIN='1')
        p = self.run_staging('--a-blanc', env=env)
        self.assertEqual(p.returncode, 0, p.stderr)


class PosteMigrerTest(unittest.TestCase):
    """Contrat reel du dispatcher de poste.sh (avis R1 B1/B2) : HOME en bac,
    venv factice, depot git reel local ; aucune base, aucun hote, aucun envoi."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1155-poste-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git_env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                            GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')
        self.home = self.root / 'home'; self.home.mkdir()
        self.repo = self.root / 'repo'
        script = self.repo / 'deploy/mise-a-jour/poste.sh'
        script.parent.mkdir(parents=True)
        shutil.copy2(DEPLOY / 'poste.sh', script)
        dsn = self.repo / 'src/ameesh/dsn.py'
        dsn.parent.mkdir(parents=True)
        shutil.copy2(ROOT / 'src/ameesh/dsn.py', dsn)
        self.git('init', '-q')
        self.git('config', 'user.email', 't@invalid')
        self.git('config', 'user.name', 't')
        (self.repo / 'marque').write_text('base\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.premier = self.git('rev-parse', 'HEAD').stdout.strip()
        (self.repo / 'marque').write_text('base\ntete\n')
        self.git('add', 'marque')
        self.git('commit', '-qm', 'tete')
        self.tete = self.git('rev-parse', 'HEAD').stdout.strip()
        self.partage = self.home / '.local/share/ameesh'
        self.venv = self.partage / 'venv/bin'
        self.venv.mkdir(parents=True)
        stub = self.venv / 'python'
        stub.write_text('#!/usr/bin/env bash\n'
                        'printf "venv %s | PYTHONPATH=%s\\n" "$*" "${PYTHONPATH:-}" '
                        '>> "${FAKE_LOG:?}"\n')
        stub.chmod(0o755)
        self.log = self.root / 'venv.log'; self.log.touch()
        self.config = self.home / '.config/ameesh/config.json'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'dsn': 'postgresql://u:p@127.0.0.1:1/x'}))
        self.bascule = self.root / 'bascule.env'
        self.bascule.write_text('VM=10.77.0.2\n')
        (self.root / 'run').mkdir()
        self.env = dict(self.git_env, HOME=str(self.home), BASCULE_ENV=str(self.bascule),
                        FAKE_LOG=str(self.log), XDG_RUNTIME_DIR=str(self.root / 'run'))

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args],
                              capture_output=True, text=True, check=True, env=self.git_env)

    def run_poste(self, *args):
        self.log.write_text('')
        return subprocess.run(['bash', str(self.repo / 'deploy/mise-a-jour/poste.sh'), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_dispatcher_forwards_the_ref(self):
        """B1: migrer REF must migrate REF rather than the repository HEAD.

        #1695: the single dispatcher test for migrer REF also checks the
        snapshot contents, replacing test_1245_poste_migrer_ref.py.
        """
        p = self.run_poste('migrer', self.premier)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        lignes = self.log.read_text().splitlines()
        self.assertTrue(any('migrate --dry-run' in l for l in lignes), lignes)
        self.assertTrue(any('migrate --additive-only' in l for l in lignes), lignes)
        self.assertTrue(any(('src-%s' % self.premier[:7]) in l for l in lignes), lignes)
        self.assertFalse(any(('src-%s' % self.tete[:7]) in l for l in lignes), lignes)
        snapshot = self.partage / ('src-%s' % self.premier[:7])
        self.assertEqual((snapshot / 'marque').read_text(), 'base\n',
                         "snapshot taken from HEAD despite an explicit REF")
        self.assertFalse((self.partage / ('src-%s' % self.tete[:7])).exists())

    def test_migrer_without_ref_takes_head(self):
        """Without REF, migrer takes HEAD (coverage preserved from #1245)."""
        p = self.run_poste('migrer')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        lines = self.log.read_text().splitlines()
        self.assertTrue(any(('src-%s' % self.tete[:7]) in l for l in lines), lines)
        self.assertFalse(any(('src-%s' % self.premier[:7]) in l for l in lines), lines)
        self.assertTrue((self.partage / ('src-%s' % self.tete[:7]) / 'marque').is_file())

    def test_additive_guard_forced_on_a_reprise(self):
        """B2 : sur une cible deja installee, --additif-seulement garde le refus."""
        (self.partage / ('src-%s' % self.tete[:7])).mkdir()
        (self.partage / 'src-current').symlink_to('src-%s' % self.tete[:7])
        p = self.run_poste('migrer', self.tete)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertTrue(any('migrate --additive-only' in l
                            for l in self.log.read_text().splitlines()))
        p = self.run_poste('migrer', '--additif-seulement', self.tete)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertTrue(any('migrate --additive-only' in l
                            for l in self.log.read_text().splitlines()))


class PaquetGitTest(unittest.TestCase):
    """Contrat reel du paquet git (avis R1 B3) : deux depots isoles et un
    destinataire en retard ; l'ancienne exclusion (origin/main) produisait un
    paquet vide quand la cible etait un ancetre de main."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1155-paquet-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / 'origin.git'
        self.poste = self.root / 'poste'
        self.vm = self.root / 'vm'
        self.env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')
        self.git(str(self.root), 'init', '-q', '--bare', '-b', 'main', str(self.origin))
        self.git(str(self.root), 'clone', '-q', str(self.origin), str(self.poste))
        self.shas = []
        for contenu in ('a\n', 'b\n', 'c\n'):
            (self.poste / 'marque').write_text(contenu)
            self.git(str(self.poste), 'add', 'marque')
            self.git(str(self.poste), 'commit', '-qm', contenu.strip())
            self.shas.append(self.git(str(self.poste), 'rev-parse', 'HEAD').stdout.strip())
        self.git(str(self.poste), 'push', '-q', 'origin', 'main')
        self.a, self.b, self.c = self.shas
        self.git(str(self.root), 'clone', '-q', str(self.origin), str(self.vm))
        self.git(str(self.vm), 'checkout', '-q', self.a)  # destinataire en retard

    def git(self, cwd, *args, check=True):
        return subprocess.run(['git', '-C', cwd, *args], capture_output=True,
                              text=True, env=self.env, check=check)

    def test_old_exclusion_makes_an_empty_bundle(self):
        out = self.root / 'vide.bundle'
        self.git(str(self.poste), 'update-ref', 'refs/tmp/vide', self.c)
        r = self.git(str(self.poste), 'bundle', 'create', '-q', str(out),
                     'refs/tmp/vide', '^origin/main', check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('empty bundle', r.stderr)

    def test_bundle_for_a_lagging_vm(self):
        out = self.root / 'paquet.bundle'
        r = subprocess.run(['bash', str(DEPLOY / 'paquet-git.sh'), str(self.poste),
                            'refs/mise-a-jour/test', self.c, str(out), self.a],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.git(str(self.vm), 'fetch', '-q', str(out), 'refs/mise-a-jour/test')
        self.git(str(self.vm), 'cat-file', '-e', '%s^{commit}' % self.c)
        r = self.git(str(self.poste), 'rev-parse', '--verify', 'refs/mise-a-jour/test',
                     check=False)
        self.assertNotEqual(r.returncode, 0, 'reference temporaire non retiree')

    def test_bundle_without_base_for_an_empty_repo(self):
        vide = self.root / 'vide-depot'
        self.git(str(self.root), 'init', '-q', '-b', 'main', str(vide))
        out = self.root / 'complet.bundle'
        r = subprocess.run(['bash', str(DEPLOY / 'paquet-git.sh'), str(self.poste),
                            'refs/mise-a-jour/test', self.b, str(out)],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.git(str(vide), 'fetch', '-q', str(out), 'refs/mise-a-jour/test')
        self.git(str(vide), 'cat-file', '-e', '%s^{commit}' % self.b)

    def test_vm_sh_delegates_bundle_creation(self):
        source = (DEPLOY / 'vm.sh').read_text()
        self.assertEqual(source.count('paquet-git.sh'), 2, source)
        self.assertNotIn('bundle create', source)


class SanteCourrierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'sante_courrier', DEPLOY / 'sante-courrier.py')
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_driven_requires_live_lease(self):
        now = 1000.0
        self.assertTrue(self.mod.driven({'lease_owner': 'r', 'lease_expires_ts': now + 5}, now))
        self.assertFalse(self.mod.driven({'lease_owner': 'r', 'lease_expires_ts': now - 5}, now))
        self.assertFalse(self.mod.driven({'lease_owner': None, 'lease_expires_ts': now + 5}, now))
        self.assertFalse(self.mod.driven({}, now))

    def test_choose_auto_prend_la_premiere_persona_libre(self):
        """#2190 : une persona occupee, meme premiere, n'est jamais sondee."""
        now = 1000.0
        rows = [{'name': 'busy', 'status': 'running', 'lease_owner': 'r',
                 'lease_expires_ts': now + 5},
                {'name': 'b', 'lease_owner': None, 'lease_expires_ts': None},
                {'name': 'a', 'status': 'idle', 'lease_owner': 'r',
                 'lease_expires_ts': now + 5},
                {'name': 'c', 'lease_owner': 'r', 'lease_expires_ts': now + 5}]
        self.assertEqual(self.mod.choose(rows, 'auto', now)[0]['name'], 'a')
        self.assertEqual(self.mod.choose(rows, 'jamais', now), (None, 'sonde desactivee'))
        self.assertEqual(self.mod.choose([], 'auto', now)[0], None)

    def test_libre_exclut_la_pause_operateur(self):
        """#2190 : une persona en pause garde `status` idle mais refuse le tour."""
        self.assertTrue(self.mod.libre({'status': 'idle'}))
        self.assertTrue(self.mod.libre({}))
        self.assertFalse(self.mod.libre({'status': 'idle',
                                         'operator_paused': True}))
        self.assertFalse(self.mod.libre({'status': 'running'}))

    def test_choose_auto_ignore_une_persona_en_pause(self):
        """#2190 : la pause operateur n'est pas une cible de sonde."""
        now = 1000.0
        rows = [{'name': 'pause', 'status': 'idle', 'operator_paused': True,
                 'lease_owner': 'r', 'lease_expires_ts': now + 5},
                {'name': 'libre', 'status': 'idle', 'lease_owner': 'r',
                 'lease_expires_ts': now + 5}]
        self.assertEqual(self.mod.choose(rows, 'auto', now)[0]['name'], 'libre')
        cible, pourquoi = self.mod.choose(rows[:1], 'auto', now)
        self.assertIsNone(cible)
        self.assertIn('pause (pause operateur)', pourquoi)

    def test_choose_auto_toutes_occupees_ne_sonde_pas(self):
        """#2190 : sans persona menee libre, la file n'est pas mesuree en KO."""
        now = 1000.0
        rows = [{'name': 'busy', 'status': 'running', 'lease_owner': 'r',
                 'lease_expires_ts': now + 5},
                {'name': 'bloque', 'status': 'blocked', 'lease_owner': 'r',
                 'lease_expires_ts': now + 5}]
        cible, pourquoi = self.mod.choose(rows, 'auto', now)
        self.assertIsNone(cible)
        self.assertIn('aucune persona menee libre', pourquoi)
        self.assertIn('busy (running)', pourquoi)
        self.assertIn('bloque (blocked)', pourquoi)

    def test_stuck_ignore_une_persona_en_pause(self):
        """#2190 : le courrier qui attend une pause n'est pas une chaine cassee."""
        rows = [{'name': 'pause', 'status': 'idle', 'operator_paused': True}]
        with mock.patch.object(self.mod.mail, 'unread_active') as unread:
            self.assertEqual(self.mod.stuck(object(), rows, 0.0, 1000.0), [])
        unread.assert_not_called()

    def test_stuck_ignore_une_persona_sans_bail(self):
        """#2899 : sans bail vivant, aucun tour ne peut ouvrir : ce n'est pas
        une souffrance de la chaine, c'est `sans_executeur`."""
        rows = [{'name': 'sans-bail', 'status': 'idle'}]
        with mock.patch.object(self.mod.mail, 'unread_active') as unread:
            self.assertEqual(self.mod.stuck(object(), rows, 0.0, 1000.0), [])
        unread.assert_not_called()

    def test_stuck_compte_une_persona_menee_au_courrier_ancien(self):
        rows = [{'name': 'menee', 'status': 'idle', 'lease_owner': 'r',
                 'lease_expires_ts': 1060.0}]
        with mock.patch.object(self.mod.mail, 'unread_active',
                               return_value=[{'id': 3, 'created_ts': 900.0}]), \
             mock.patch.object(self.mod.mail, 'maybe_redelivered', return_value=False):
            self.assertEqual(self.mod.stuck(object(), rows, 0.0, 1000.0),
                             [{'destinataire': 'menee', 'message': 3, 'age_s': 100.0}])

    def test_sans_executeur_nomme_le_destinataire_non_menee(self):
        """#2899 : le courrier operationnel d'un agent sans bail vivant sort en
        `sans_executeur`, avec son age et son dernier tour, jamais en retard de
        remise."""
        rows = [{'name': 'orchestrateur', 'status': 'idle', 'last_turn_ts': 800.0}]
        with mock.patch.object(self.mod.mail, 'unread_active',
                               return_value=[{'id': 1, 'created_ts': 900.0},
                                             {'id': 2, 'created_ts': 950.0}]), \
             mock.patch.object(self.mod.mail, 'maybe_redelivered', return_value=False):
            self.assertEqual(self.mod.sans_executeur(object(), rows, 0.0, 1000.0),
                             [{'destinataire': 'orchestrateur', 'n': 2,
                               'plus_ancien_s': 100.0, 'statut': 'idle',
                               'dernier_tour_s': 200.0,
                               'motif': 'aucun bail vivant'}])

    def test_sans_executeur_ignore_pause_et_bail_vivant(self):
        """#2899 : une pause (operateur, budget) ou un bail vivant n'est pas un
        destinataire sans executant."""
        rows = [{'name': 'pause', 'status': 'idle', 'operator_paused': True},
                {'name': 'budget', 'status': 'blocked',
                 'status_text': 'budget : pause (compte) until 08:00'},
                {'name': 'menee', 'status': 'idle', 'lease_owner': 'r',
                 'lease_expires_ts': 1060.0}]
        with mock.patch.object(self.mod.mail, 'unread_active') as unread:
            self.assertEqual(self.mod.sans_executeur(object(), rows, 0.0, 1000.0), [])
        unread.assert_not_called()

    def test_sans_executeur_exclut_le_profil_humain_meme_en_mode_execute(self):
        """#2957 : une ligne humaine (cle owner / harness none) n'est pas un
        executant a relancer : elle reste dans canal_humain, jamais comptee
        deux fois."""
        rows = [{'name': 'smichea', 'status': 'idle', 'mode': 'execute',
                 'key_role': 'owner', 'harness': 'none',
                 'responsible': 'human:smichea'},
                {'name': 'orchestrateur', 'status': 'idle'}]
        with mock.patch.object(self.mod.mail, 'unread_active',
                               return_value=[{'id': 1, 'created_ts': 900.0}]), \
             mock.patch.object(self.mod.mail, 'maybe_redelivered', return_value=False):
            self.assertEqual(
                [e['destinataire'] for e in
                 self.mod.sans_executeur(object(), rows, 0.0, 1000.0)],
                ['orchestrateur'])
            self.assertEqual(
                [e['destinataire'] for e in
                 self.mod.canal_humain(object(), 'h', 1000.0, rows)],
                ['smichea'])

    def test_sans_executeur_lit_le_prefixe_de_pause_budget(self):
        """#2957 : la pause budget se lit avec budget.PAUSE_PREFIX, pas un
        litteral qui deriverait si le prefixe changeait."""
        rows = [{'name': 'budget', 'status': 'blocked',
                 'status_text': 'pause : compte epuise'}]
        with mock.patch.object(self.mod.budget, 'PAUSE_PREFIX', 'pause : '):
            with mock.patch.object(self.mod.mail, 'unread_active') as unread:
                self.assertEqual(
                    self.mod.sans_executeur(object(), rows, 0.0, 1000.0), [])
            unread.assert_not_called()

    def test_sans_executeur_nomme_un_bloque_avec_erreur(self):
        """#2899 : un bloque sans bail et hors pause budget garde sa cause et
        son courrier ; il n'est plus invisible."""
        rows = [{'name': 'casse', 'status': 'blocked',
                 'last_error': 'auth introuvable', 'last_turn_ts': 800.0}]
        with mock.patch.object(self.mod.mail, 'unread_active',
                               return_value=[{'id': 1, 'created_ts': 900.0}]), \
             mock.patch.object(self.mod.mail, 'maybe_redelivered', return_value=False):
            self.assertEqual(self.mod.sans_executeur(object(), rows, 0.0, 1000.0),
                             [{'destinataire': 'casse', 'n': 1,
                               'plus_ancien_s': 100.0, 'statut': 'blocked',
                               'dernier_tour_s': 200.0,
                               'motif': 'bloqué : auth introuvable'}])

    def test_dead_letters_include_stopped_personas(self):
        """N1 : une boite morte d'une persona arretee de l'hote est rapportee."""
        db = object()
        arrets = [{'name': 'a', 'host': 'h', 'status': 'stopped'},
                  {'name': 'b', 'host': 'autre', 'status': 'idle'}]
        with mock.patch.object(self.mod.registry, 'overview', return_value=arrets), \
             mock.patch.object(self.mod.storage, 'of') as of:
            of.return_value.mailbox.dead_letters.return_value = [
                {'recipient': 'a', 'n': 2}, {'recipient': 'b', 'n': 1}]
            self.assertEqual(self.mod.dead_letters(db, 'h'),
                             [{'destinataire': 'a', 'n': 2}])

    def test_dead_letters_exclut_le_profil_humain_sans_executeur(self):
        """#2899 : le porte-cle n'est pas une boite morte d'agent ; son courrier
        va au canal humain."""
        rows = [{'name': 'smichea', 'host': 'h', 'status': 'stopped',
                 'mode': 'externe', 'key_role': 'owner',
                 'responsible': 'human:smichea'},
                {'name': 'a', 'host': 'h', 'status': 'stopped', 'mode': 'execute'}]
        with mock.patch.object(self.mod.registry, 'overview', return_value=rows), \
             mock.patch.object(self.mod.storage, 'of') as of:
            of.return_value.mailbox.dead_letters.return_value = [
                {'recipient': 'smichea', 'n': 5}, {'recipient': 'a', 'n': 2}]
            self.assertEqual(self.mod.dead_letters(object(), 'h'),
                             [{'destinataire': 'a', 'n': 2}])

    def test_canal_humain_liste_les_sessions_humaines(self):
        """#2899 : toute session humaine sans exécuteur (chat, porte-clé) est
        nommée avec l'humain et la lecture sans marquage, non bloquant."""
        rows = [{'name': 'smichea', 'host': 'h', 'status': 'stopped',
                 'mode': 'externe', 'key_role': 'owner',
                 'responsible': 'human:smichea'},
                {'name': 'chat-smichea', 'host': 'h', 'status': 'idle',
                 'mode': 'externe', 'harness': 'claude',
                 'responsible': 'human:smichea'}]

        def unread(_db, nom):
            return ([{'id': 1, 'created_ts': 900.0},
                     {'id': 2, 'created_ts': 950.0}] if nom == 'smichea' else
                    [{'id': 3, 'created_ts': 990.0}])

        with mock.patch.object(self.mod.mail, 'unread_active', side_effect=unread):
            self.assertEqual(
                self.mod.canal_humain(object(), 'h', 1000.0, rows),
                [{'destinataire': 'smichea', 'n': 2, 'plus_ancien_s': 100.0,
                  'responsable': 'human:smichea', 'canal': 'human:smichea',
                  'alerte': 'stopped_with_mail',
                  'lecture': 'agent-mail inbox smichea'},
                 {'destinataire': 'chat-smichea', 'n': 1, 'plus_ancien_s': 10.0,
                  'responsable': 'human:smichea', 'canal': 'chat',
                  'alerte': None,
                  'lecture': 'agent-mail inbox chat-smichea'}])

    def test_canal_humain_fallback_owner_pour_un_profil_sans_responsable(self):
        """#2957 : un profil externe sans responsable prend le canal humain du
        proprietaire, lu au registre ; le rapport ne laisse pas 'human:?'."""
        rows = [{'name': 'veille', 'host': 'h', 'status': 'idle',
                 'mode': 'externe', 'harness': 'other'}]
        owner = [{'name': 'smichea', 'key_role': 'owner',
                  'responsible': 'human:smichea', 'host': 'h'}]
        with mock.patch.object(self.mod.registry, 'overview', return_value=owner), \
             mock.patch.object(self.mod.mail, 'unread_active',
                               return_value=[{'id': 4, 'created_ts': 990.0}]):
            self.assertEqual(
                self.mod.canal_humain(object(), 'h', 1000.0, rows),
                [{'destinataire': 'veille', 'n': 1, 'plus_ancien_s': 10.0,
                  'responsable': 'human:smichea', 'canal': 'human:smichea',
                  'alerte': 'idle_with_mail',
                  'lecture': 'agent-mail inbox veille'}])

    def test_main_sans_executeur_sort_ko_avec_sa_cause(self):
        """#2899 : un destinataire operationnel sans bail vivant est un KO
        explicite, meme sans souffrance de chaine."""
        rows = [{'name': 'orchestrateur', 'host': 'h', 'mode': 'execute',
                 'status': 'idle'}]
        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview', return_value=rows), \
             mock.patch.object(self.mod, 'stuck', return_value=[]), \
             mock.patch.object(self.mod, 'sans_executeur',
                               return_value=[{'destinataire': 'orchestrateur',
                                              'n': 66, 'plus_ancien_s': 21847.5,
                                              'statut': 'idle'}]), \
             mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
             mock.patch.object(self.mod, 'canal_humain', return_value=[]), \
             mock.patch.object(self.mod.mail, 'send') as envoi, \
             mock.patch.object(self.mod.time, 'time', return_value=1000.0), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'h', '--sonde', 'jamais', '--json'])
        self.assertEqual(code, 1, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'ko')
        self.assertEqual(rapport['souffrance'], [])
        self.assertEqual(rapport['sans_executeur'][0]['destinataire'], 'orchestrateur')
        self.assertEqual(rapport['sans_executeur'][0]['n'], 66)
        envoi.assert_not_called()

    def test_main_canal_humain_non_bloquant(self):
        """#2899 : le courrier du porte-cle est rapporte, mais ne bloque pas une
        sonde de chaine reussie."""
        rows = [{'name': 'cx1', 'host': 'h', 'mode': 'execute', 'status': 'idle',
                 'lease_owner': 'r', 'lease_expires_ts': 1060.0}]
        resultat = {'message': 7, 'destinataire': 'cx1', 'delai_s': 1.0,
                    'remise_s': 0.5, 'hote_destinataire': 'h',
                    'statut_cible': 'idle', 'tour': 'ok'}
        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview', return_value=rows), \
             mock.patch.object(self.mod.registry, 'get', return_value=rows[0]), \
             mock.patch.object(self.mod, 'stuck', return_value=[]), \
             mock.patch.object(self.mod, 'sans_executeur', return_value=[]), \
             mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
             mock.patch.object(self.mod, 'canal_humain',
                               return_value=[{'destinataire': 'smichea', 'n': 5,
                                              'canal': 'human:smichea'}]), \
             mock.patch.object(self.mod, 'probe', return_value=resultat), \
             mock.patch.object(self.mod.time, 'time', return_value=1000.0), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'h', '--json'])
        self.assertEqual(code, 0, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'ok')
        self.assertEqual(rapport['canal_humain'][0]['destinataire'], 'smichea')

    def test_incident_2899_separe_sans_executeur_et_canal_humain(self):
        """#2899 : le cas reel — orchestrateur idle sans bail (66 messages) et
        porte-cle arrete (5 messages) — ne doit plus melanger les deux causes :
        KO par `sans_executeur` seulement, `canal_humain` non bloquant."""
        rows = [
            {'name': 'orchestrateur', 'host': 'pc-smichea', 'mode': 'execute',
             'status': 'idle', 'last_turn_ts': 800.0},
            {'name': 'cx1', 'host': 'pc-smichea', 'mode': 'execute',
             'status': 'idle', 'lease_owner': 'r', 'lease_expires_ts': 1060.0},
            {'name': 'smichea', 'host': 'pc-smichea', 'mode': 'externe',
             'status': 'stopped', 'key_role': 'owner', 'harness': 'none',
             'responsible': 'human:smichea'},
        ]
        resultat = {'message': 7, 'destinataire': 'cx1', 'delai_s': 1.0,
                    'remise_s': 0.5, 'hote_destinataire': 'pc-smichea',
                    'statut_cible': 'idle', 'tour': 'ok'}

        def unread(_db, nom):
            if nom == 'orchestrateur':
                return [{'id': 120201 + i, 'created_ts': 100.0} for i in range(66)]
            if nom == 'smichea':
                return [{'id': 900 + i, 'created_ts': 900.0} for i in range(5)]
            return []

        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview', return_value=rows), \
             mock.patch.object(self.mod.registry, 'get',
                               return_value=rows[1]), \
             mock.patch.object(self.mod.storage, 'of') as of, \
             mock.patch.object(self.mod.mail, 'unread_active', side_effect=unread), \
             mock.patch.object(self.mod, 'probe', return_value=resultat), \
             mock.patch.object(self.mod.time, 'time', return_value=1000.0), \
             redirect_stdout(sortie):
            of.return_value.mailbox.dead_letters.return_value = [
                {'recipient': 'smichea', 'n': 4},
                {'recipient': 'smichea', 'n': 1}]
            code = self.mod.main(['--hote', 'pc-smichea', '--souffrance', '0',
                                  '--json'])
        self.assertEqual(code, 1, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'ko')
        self.assertEqual(rapport['souffrance'], [])
        self.assertEqual([(e['destinataire'], e['n']) for e in rapport['sans_executeur']],
                         [('orchestrateur', 66)])
        self.assertEqual(rapport['boites_mortes'], [])
        self.assertEqual(rapport['canal_humain'][0]['destinataire'], 'smichea')
        self.assertEqual(rapport['canal_humain'][0]['n'], 5)
        self.assertEqual(rapport['canal_humain'][0]['canal'], 'human:smichea')
        self.assertEqual(rapport['sonde']['destinataire'], 'cx1')
        self.assertEqual(rapport['sonde']['tour'], 'ok')

    def sonde(self, etat, repond=False, attente=0.2, accuse_remis=False, lots=()):
        """La sonde sur faux courrier/registre (#1460) : remise a 100.0.

        ``lots`` : les lots ouverts de la cible (``work.open_lots``), pour le
        verdict « travail en cours » (#4181)."""
        envoye = {}

        def send(db, sender, dest, corps, **kw):
            envoye['jeton'] = corps.split('--ack ')[1].split()[0]
            return 7

        def history(db, nom, **kw):
            return [{'sender': nom, 'body': envoye['jeton'],
                     'delivered_ts': 102.0 if accuse_remis else None}] if repond else []

        def unread(db, nom):
            return [m for m in history(db, nom) if not m['delivered_ts']]

        with mock.patch.object(self.mod.work, 'open_lots', return_value=list(lots)), \
             mock.patch.object(self.mod.mail, 'send', side_effect=send), \
             mock.patch.object(self.mod.mail, 'get', return_value={'delivered_ts': 100.0}), \
             mock.patch.object(self.mod.mail, 'unread', side_effect=unread), \
             mock.patch.object(self.mod.mail, 'history', side_effect=history), \
             mock.patch.object(self.mod.registry, 'get',
                               **({'side_effect': etat} if callable(etat)
                                  else {'return_value': etat})), \
             mock.patch.object(self.mod.time, 'sleep'):
            return self.mod.probe(object(), {'name': 'p', 'host': 'h'}, 'abc', attente)

    def test_probe_ko_when_the_harness_exits_127_after_delivery(self):
        etat = {'last_turn_ts': 101.0, 'status': 'blocked', 'last_error':
                'hote non pret : the harness could not run (code 127)'}
        out = self.sonde(etat, attente=60.0)
        self.assertIsNone(out['delai_s'])
        self.assertIn('code 127', out['tour'])

    def test_probe_delivered_without_tool_answer_is_ko(self):
        out = self.sonde({'last_turn_ts': 99.0, 'status': 'idle'})
        self.assertIsNone(out['delai_s'])
        self.assertEqual(out['tour'], "remis, sans reponse de l'outil")

    def test_probe_ok_only_with_the_answer_and_a_finished_turn(self):
        out = self.sonde({'last_turn_ts': 100.5, 'status': 'idle'}, repond=True)
        self.assertEqual(out['tour'], 'ok')
        self.assertIsNotNone(out['delai_s'])

    def test_probe_ko_when_the_answer_is_seen_but_the_turn_never_ends(self):
        """#1460 : l'outil repond, puis le harnais reste bloque jusqu'au timeout."""
        out = self.sonde({'last_turn_ts': 99.0, 'status': 'running'}, repond=True)
        self.assertIsNone(out['delai_s'])
        self.assertEqual(out['tour'], 'reponse vue, tour non termine')

    def test_probe_conclusive_when_the_target_works_a_build_lot(self):
        """#4181 : la cible a repondu puis travaille un lot (build) ; le tour
        long fait un vrai travail, la chaine de l'hote est prouvee."""
        out = self.sonde({'last_turn_ts': 99.0, 'status': 'running'}, repond=True,
                         lots=[{'id': 4176, 'state': 'build'}])
        self.assertIsNotNone(out['delai_s'])
        self.assertTrue(out['concluant'])
        self.assertIn('travail en cours', out['tour'])
        self.assertEqual(out['travail'], {'lot': 4176, 'state': 'build'})

    def test_probe_conclusive_when_the_target_is_in_qa(self):
        """#4181 : un lot en qa est aussi un travail en cours."""
        out = self.sonde({'last_turn_ts': 99.0, 'status': 'running'}, repond=True,
                         lots=[{'id': 4181, 'state': 'qa'}])
        self.assertIsNotNone(out['delai_s'])
        self.assertTrue(out['concluant'])
        self.assertEqual(out['travail'], {'lot': 4181, 'state': 'qa'})

    def test_probe_taken_but_unstarted_lot_stays_inconclusive(self):
        """#4181 : un lot seulement pris (intake) ne prouve pas un tour de
        travail ; le garde de #1460 reste entier."""
        out = self.sonde({'last_turn_ts': 99.0, 'status': 'running'}, repond=True,
                         lots=[{'id': 4176, 'state': 'intake'}])
        self.assertIsNone(out['delai_s'])
        self.assertFalse(out['concluant'])
        self.assertEqual(out['tour'], 'reponse vue, tour non termine')

    def test_probe_failed_turn_stays_ko_even_with_a_lot_in_progress(self):
        """#4181 : l'echec du tour prime sur le travail en cours."""
        out = self.sonde({'last_turn_ts': 101.0, 'status': 'blocked',
                          'last_error': 'timeout (124)'}, repond=True,
                         lots=[{'id': 4176, 'state': 'build'}])
        self.assertIsNone(out['delai_s'])
        self.assertIn('timeout (124)', out['tour'])

    def test_probe_ko_when_the_turn_ends_in_error_after_the_answer(self):
        out = self.sonde({'last_turn_ts': 101.0, 'status': 'running',
                          'last_error': 'timeout (124)'}, repond=True)
        self.assertIsNone(out['delai_s'])
        self.assertIn('timeout (124)', out['tour'])

    def test_probe_ko_when_the_turn_fails_between_two_registry_reads(self):
        """Une seule lecture du registre par passage : un tour qui finit en
        erreur entre deux lectures n'est jamais pris pour une fin propre."""
        lectures = iter([{'last_turn_ts': 99.0, 'status': 'running'},
                         {'last_turn_ts': 101.0, 'status': 'blocked',
                          'last_error': 'timeout (124)'}])
        out = self.sonde(lambda db, nom: next(lectures), repond=True, attente=60.0)
        self.assertIsNone(out['delai_s'])
        self.assertIn('timeout (124)', out['tour'])

    def test_probe_ko_when_a_clean_turn_never_answered(self):
        out = self.sonde({'last_turn_ts': 101.0, 'status': 'idle'})
        self.assertIsNone(out['delai_s'])
        self.assertEqual(out['tour'], "remis, sans reponse de l'outil")

    def test_probe_waits_through_a_budget_start_gap_then_ok(self):
        """#4089 : l'ecart de demarrage (« budget : ») n'est pas un tour en
        echec ; la sonde attend le vrai tour, repondu et termine."""
        attente = {'last_turn_ts': 101.0, 'status': 'blocked',
                   'status_text': 'budget : account primaire: next turn in 17s '
                                  '(start gap 30s between turns, #3779)',
                   'last_error': 'account primaire: next turn in 17s'}
        lectures = iter([attente, {'last_turn_ts': 120.0, 'status': 'idle'}])
        out = self.sonde(lambda db, nom: next(lectures), repond=True, attente=60.0)
        self.assertEqual(out['tour'], 'ok')
        self.assertTrue(out['concluant'])

    def test_probe_budget_wait_until_the_bound_is_inconclusive(self):
        out = self.sonde({'last_turn_ts': 101.0, 'status': 'blocked',
                          'last_error': 'budget : account primaire: next turn in 17s'})
        self.assertIsNone(out['delai_s'])
        self.assertFalse(out['concluant'])
        self.assertIn('attente budget : account primaire', out['tour'])

    def test_probe_real_blocked_error_stays_a_failed_turn(self):
        out = self.sonde({'last_turn_ts': 101.0, 'status': 'blocked',
                          'status_text': 'harness crashed', 'last_error': 'boom'})
        self.assertIn('tour en echec : boom', out['tour'])
        self.assertTrue(out['concluant'])

    def test_probe_answer_without_finished_turn_is_inconclusive(self):
        out = self.sonde({'last_turn_ts': 99.0, 'status': 'running'}, repond=True)
        self.assertFalse(out['concluant'])

    def test_probe_ok_when_the_ack_was_already_delivered(self):
        out = self.sonde({'last_turn_ts': 103.0, 'status': 'idle'},
                         repond=True, accuse_remis=True)
        self.assertEqual(out['tour'], 'ok')
        self.assertIsNotNone(out['delai_s'])

    def test_choose_explicit_target_must_exist(self):
        with self.assertRaises(SystemExit):
            self.mod.choose([{'name': 'a'}], 'inconnu', 1000.0)

    def test_choose_cible_nommee_occupee_ne_sonde_pas(self):
        """#2190 : meme nommee, une cible en tour n'est pas mesuree en file."""
        rows = [{'name': 'a', 'status': 'running', 'lease_owner': 'r',
                 'lease_expires_ts': 1005.0}]
        cible, pourquoi = self.mod.choose(rows, 'a', 1000.0)
        self.assertIsNone(cible)
        self.assertIn('cible nommee a occupee', pourquoi)

    def test_cible_nommee_sans_bail_vivant_ne_sonde_pas(self):
        for fin in (None, 999.0):
            row = {'name': 'a', 'host': 'autre', 'status': 'idle',
                   'lease_owner': 'r', 'lease_expires_ts': fin}
            for cible, pourquoi in (self.mod.choose([row], 'a', 1000.0),
                                    self.mod.choisir_croise([row], 'h', 'a', 1000.0)):
                self.assertIsNone(cible)
                self.assertIn('sans bail vivant', pourquoi)

    def test_choose_cible_nommee_en_pause_ne_sonde_pas(self):
        """#2190 : une cible nommee en pause operateur n'est pas sondee."""
        rows = [{'name': 'a', 'status': 'idle', 'operator_paused': True,
                 'lease_owner': 'r', 'lease_expires_ts': 1005.0}]
        cible, pourquoi = self.mod.choose(rows, 'a', 1000.0)
        self.assertIsNone(cible)
        self.assertIn('cible nommee a occupee (pause operateur)', pourquoi)

    def test_choisir_croise_prend_une_persona_libre_d_un_autre_hote(self):
        """#2190 : le serveur sonde une persona menee *libre* d'un autre hote.

        #94495 visait ameesh-dev3 (running) et la remise a attendu la fin de
        son tour (350 s) : c'est sa file qui a ete mesuree, pas la chaine.
        """
        now = 1000.0
        rows = [{'name': 'busy', 'host': 'ameesh-agents-1', 'status': 'running',
                 'lease_owner': 'r', 'lease_expires_ts': now + 5},
                {'name': 'b', 'host': 'ameesh-agents-1', 'lease_owner': None,
                 'lease_expires_ts': None},
                {'name': 'c', 'host': 'ameesh-agents-2', 'status': 'idle',
                 'lease_owner': 'r', 'lease_expires_ts': now + 5},
                {'name': 'a', 'host': 'manaty-mesh-1', 'status': 'idle',
                 'lease_owner': 'r', 'lease_expires_ts': now + 5}]
        self.assertEqual(
            self.mod.choisir_croise(rows, 'manaty-mesh-1', 'auto', now)[0]['name'], 'c')

    def test_choisir_croise_toutes_occupees_ne_sonde_pas(self):
        """#2190 : un autre hote entierement occupe ou en pause ne produit pas
        de KO de file."""
        now = 1000.0
        rows = [{'name': 'busy', 'host': 'ameesh-agents-1', 'status': 'running',
                 'lease_owner': 'r', 'lease_expires_ts': now + 5},
                {'name': 'bloque', 'host': 'ameesh-agents-2', 'status': 'blocked',
                 'lease_owner': 'r', 'lease_expires_ts': now + 5},
                {'name': 'pause', 'host': 'ameesh-agents-3', 'status': 'idle',
                 'operator_paused': True, 'lease_owner': 'r',
                 'lease_expires_ts': now + 5}]
        cible, pourquoi = self.mod.choisir_croise(rows, 'manaty-mesh-1', 'auto', now)
        self.assertIsNone(cible)
        self.assertIn('aucune persona menee libre sur un autre hote', pourquoi)
        self.assertIn('busy (running)', pourquoi)
        self.assertIn('bloque (blocked)', pourquoi)
        self.assertIn('pause (pause operateur)', pourquoi)

    def test_choisir_croise_cible_nommee_doit_exister_ailleurs(self):
        rows = [{'name': 'a', 'host': 'ameesh-agents-1', 'lease_owner': 'r',
                 'lease_expires_ts': 1005.0},
                {'name': 'locale', 'host': 'manaty-mesh-1', 'lease_owner': 'r',
                 'lease_expires_ts': 1005.0}]
        self.assertEqual(
            self.mod.choisir_croise(rows, 'manaty-mesh-1', 'a', 1000.0)[0]['name'], 'a')
        for cible in ('inconnu', 'locale'):
            with self.subTest(cible=cible):
                with self.assertRaises(SystemExit):
                    self.mod.choisir_croise(rows, 'manaty-mesh-1', cible, 1000.0)

    def test_choisir_croise_cible_nommee_occupee_ne_sonde_pas(self):
        """#2190 : une cible croisee nommee et occupee n'est pas mesuree en file."""
        rows = [{'name': 'a', 'host': 'ameesh-agents-1', 'status': 'running',
                 'lease_owner': 'r', 'lease_expires_ts': 1005.0}]
        cible, pourquoi = self.mod.choisir_croise(rows, 'manaty-mesh-1', 'a', 1000.0)
        self.assertIsNone(cible)
        self.assertIn('cible nommee a occupee', pourquoi)

    def test_choisir_croise_cible_nommee_en_pause_ne_sonde_pas(self):
        """#2190 : une cible croisee nommee en pause n'est pas sondee."""
        rows = [{'name': 'a', 'host': 'ameesh-agents-1', 'status': 'idle',
                 'operator_paused': True, 'lease_owner': 'r',
                 'lease_expires_ts': 1005.0}]
        cible, pourquoi = self.mod.choisir_croise(rows, 'manaty-mesh-1', 'a', 1000.0)
        self.assertIsNone(cible)
        self.assertIn('cible nommee a occupee (pause operateur)', pourquoi)

    def test_choisir_croise_sans_persona_menee(self):
        rows = [{'name': 'b', 'host': 'ameesh-agents-1', 'lease_owner': None,
                 'lease_expires_ts': None}]
        cible, pourquoi = self.mod.choisir_croise(rows, 'manaty-mesh-1', 'auto', 1000.0)
        self.assertIsNone(cible)
        self.assertIn('autre hote', pourquoi)

    def test_personas_sans_hote_liste_tous_les_executants(self):
        rows = [{'name': 'a', 'host': 'h1', 'mode': 'execute', 'status': 'idle'},
                {'name': 'b', 'host': 'h2', 'mode': 'execute', 'status': 'running'},
                {'name': 'c', 'host': 'h2', 'mode': 'execute', 'status': 'stopped'},
                {'name': 'd', 'host': 'h2', 'mode': 'externe', 'status': 'idle'}]
        with mock.patch.object(self.mod.registry, 'overview', return_value=rows):
            self.assertEqual([r['name'] for r in self.mod.personas(object(), None)],
                             ['a', 'b'])
            self.assertEqual([r['name'] for r in self.mod.personas(object(), 'h2')],
                             ['b'])

    def test_main_serveur_sonde_un_autre_hote(self):
        """Le serveur sans executant sonde une persona menee d'un autre hote ;
        la charge remise porte l'hote mesure et l'hote destinataire."""
        sha = '0123456789abcdef0123456789abcdef01234567'
        maintenant = 1000.0
        autres = [{'name': 'vm-agent', 'host': 'ameesh-agents-1', 'mode': 'execute',
                   'status': 'idle', 'lease_owner': 'r',
                   'lease_expires_ts': maintenant + 60}]
        envois = {}

        def faux_send(db, sender, recipient, body, **kw):
            envois.update(recipient=recipient, payload=kw.get('payload'),
                          jeton=body.split('--ack ')[1].split()[0])
            return 7

        def faux_get(db, message_id):
            return {'id': message_id, 'delivered_ts': maintenant}

        def faux_history(db, nom, **kw):
            return [{'sender': nom, 'body': envois['jeton'],
                     'delivered_ts': maintenant}]

        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview', return_value=autres), \
             mock.patch.object(self.mod.registry, 'get',
                               return_value={'last_turn_ts': maintenant + 1,
                                             'lease_owner': 'r',
                                             'lease_expires_ts': maintenant + 60,
                                             'status': 'idle'}), \
             mock.patch.object(self.mod, 'stuck', return_value=[]), \
             mock.patch.object(self.mod, 'sans_executeur', return_value=[]), \
             mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
             mock.patch.object(self.mod, 'canal_humain', return_value=[]), \
             mock.patch.object(self.mod.mail, 'send', side_effect=faux_send), \
             mock.patch.object(self.mod.mail, 'get', side_effect=faux_get), \
             mock.patch.object(self.mod.mail, 'history', side_effect=faux_history), \
             mock.patch.object(self.mod.time, 'time', return_value=maintenant), \
             mock.patch.object(self.mod.time, 'monotonic', return_value=maintenant), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'manaty-mesh-1', '--serveur',
                                  '--sha', sha, '--attente', '1', '--json'])
        self.assertEqual(code, 0, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'ok')
        self.assertEqual(rapport['personas'], 0)
        self.assertEqual(rapport['sonde']['destinataire'], 'vm-agent')
        self.assertEqual(rapport['sonde']['hote_destinataire'], 'ameesh-agents-1')
        self.assertEqual(envois['recipient'], 'vm-agent')
        self.assertEqual(envois['payload']['staging'],
                         {'hote': 'manaty-mesh-1', 'sha': sha,
                          'destinataire_hote': 'ameesh-agents-1'})
        self.assertEqual(rapport['sonde']['statut_cible'], 'idle')
        self.assertEqual(rapport['sonde']['remise_s'], 0.0)

    def test_main_serveur_toutes_occupees_repond_sans_sonde(self):
        """#2190 : toutes les personas menees d'un autre hote sont occupees ->
        `sans-sonde` motive, pas un KO de file ni un rollback."""
        maintenant = 1000.0
        autres = [{'name': 'dev3', 'host': 'ameesh-agents-1', 'mode': 'execute',
                   'status': 'running', 'lease_owner': 'r',
                   'lease_expires_ts': maintenant + 60}]
        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview', return_value=autres), \
             mock.patch.object(self.mod, 'stuck', return_value=[]), \
             mock.patch.object(self.mod, 'sans_executeur', return_value=[]), \
             mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
             mock.patch.object(self.mod, 'canal_humain', return_value=[]), \
             mock.patch.object(self.mod.mail, 'send') as envoi, \
             mock.patch.object(self.mod.time, 'time', return_value=maintenant), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'manaty-mesh-1', '--serveur',
                                  '--attente', '1', '--json'])
        self.assertEqual(code, 0, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'sans-sonde')
        self.assertIsNone(rapport['sonde'])
        self.assertIn('dev3 (running)', rapport['pourquoi'])
        envoi.assert_not_called()

    def test_main_revalide_la_cible_avant_envoi(self):
        """#2190 : une cible libre dans la photo du registre mais entree en
        tour avant l'envoi n'est pas sondee (sa file ne mesure rien)."""
        maintenant = 1000.0
        rows = [{'name': 'agent', 'host': 'ameesh-agents-1', 'mode': 'execute',
                 'status': 'idle', 'lease_owner': 'r',
                 'lease_expires_ts': maintenant + 60}]
        occupee = {'name': 'agent', 'host': 'ameesh-agents-1',
                   'status': 'running', 'lease_owner': 'r',
                   'lease_expires_ts': maintenant + 60}
        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview', return_value=rows), \
             mock.patch.object(self.mod.registry, 'get', return_value=occupee), \
             mock.patch.object(self.mod, 'stuck', return_value=[]), \
             mock.patch.object(self.mod, 'sans_executeur', return_value=[]), \
             mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
             mock.patch.object(self.mod, 'canal_humain', return_value=[]), \
             mock.patch.object(self.mod.mail, 'send') as envoi, \
             mock.patch.object(self.mod.time, 'time', return_value=maintenant), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'ameesh-agents-1', '--json'])
        self.assertEqual(code, 0, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'sans-sonde')
        self.assertIn('cible agent occupee (running)', rapport['pourquoi'])
        envoi.assert_not_called()

    def test_main_cible_disparue_ou_bail_expire_ne_sonde_pas(self):
        row = dict(name='agent', host='h', status='idle', lease_owner='r',
                   lease_expires_ts=1060.0)
        for etat in (None, dict(row, lease_expires_ts=999.0)):
            sortie = io.StringIO()
            with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
                 mock.patch.object(self.mod.config, 'load', return_value={}), \
                 mock.patch.multiple(self.mod.registry,
                                     overview=mock.Mock(return_value=[row]),
                                     get=mock.Mock(return_value=etat)), \
                 mock.patch.object(self.mod, 'stuck', return_value=[]), \
                 mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
                 mock.patch.object(self.mod.mail, 'send', return_value=7) as envoi, \
                 mock.patch.object(self.mod.time, 'time', return_value=1000.0), \
                 redirect_stdout(sortie):
                code = self.mod.main(['--hote', 'h', '--attente', '0', '--json'])
            self.assertEqual(code, 0, sortie.getvalue())
            self.assertEqual(json.loads(sortie.getvalue())['verdict'], 'sans-sonde')
            envoi.assert_not_called()

    # --- #4126 : cible nommee occupee : attente bornee, puis remplacante ---
    def horloge(self):
        """time.monotonic/time.sleep factices : sleep avance l'horloge."""
        t = [0.0]
        return t, (lambda: t[0]), (lambda d: t.__setitem__(0, t[0] + d))

    def rangs_4126(self, maintenant=1000.0):
        mene = dict(mode='execute', host='ameesh-agents-1', lease_owner='r',
                    lease_expires_ts=maintenant + 900)
        return [dict(mene, name='ameesh-dev3', status='running'),
                dict(mene, name='ameesh-dev5', status='idle')]

    def test_4126_cible_nommee_liberee_avant_la_borne_est_sondee(self):
        t, mono, dormir = self.horloge()
        rows = self.rangs_4126()
        occupee, libre = rows[0], dict(rows[0], status='idle')
        etats = iter([occupee, occupee, libre])
        with mock.patch.object(self.mod.registry, 'get',
                               side_effect=lambda db, nom: next(etats)), \
             mock.patch.object(self.mod.time, 'monotonic', side_effect=mono), \
             mock.patch.object(self.mod.time, 'sleep', side_effect=dormir), \
             mock.patch.object(self.mod.time, 'time', return_value=1000.0):
            cible, pourquoi, remplacement = self.mod.selection(
                None, rows, 'ameesh-dev3', 300.0, 1000.0)
        self.assertEqual(cible['name'], 'ameesh-dev3')
        self.assertIsNone(pourquoi)
        self.assertIsNone(remplacement)
        self.assertEqual(t[0], 10.0)

    def test_4126_cible_occupee_a_la_borne_remplacee_par_une_libre(self):
        t, mono, dormir = self.horloge()
        rows = self.rangs_4126()
        etats = {r['name']: r for r in rows}
        with mock.patch.object(self.mod.registry, 'get',
                               side_effect=lambda db, nom: etats[nom]), \
             mock.patch.object(self.mod.time, 'monotonic', side_effect=mono), \
             mock.patch.object(self.mod.time, 'sleep', side_effect=dormir), \
             mock.patch.object(self.mod.time, 'time', return_value=1000.0):
            cible, pourquoi, remplacement = self.mod.selection(
                None, rows, 'ameesh-dev3', 300.0, 1000.0)
        self.assertEqual(cible['name'], 'ameesh-dev5')
        self.assertEqual(t[0], 300.0)
        self.assertEqual(remplacement['cible_nommee'], 'ameesh-dev3')
        self.assertEqual(remplacement['remplacante'], 'ameesh-dev5')
        self.assertIn('cible nommee ameesh-dev3 occupee (running) apres 300.0s',
                      remplacement['raison'])
        self.assertIn('remplacante ameesh-dev5', pourquoi)

    def test_4126_remplacante_liberee_pendant_attente(self):
        t, mono, dormir = self.horloge()
        rows = self.rangs_4126()
        rows[1] = dict(rows[1], status='running')
        def relire(db, nom):
            row = next(r for r in rows if r['name'] == nom)
            return dict(row, status='idle') if nom == 'ameesh-dev5' else row
        with mock.patch.object(self.mod.registry, 'get', side_effect=relire), \
             mock.patch.object(self.mod.time, 'monotonic', side_effect=mono), \
             mock.patch.object(self.mod.time, 'sleep', side_effect=dormir), \
             mock.patch.object(self.mod.time, 'time', return_value=1000.0):
            cible, pourquoi, remplacement = self.mod.selection(
                None, rows, 'ameesh-dev3', 300.0, 1000.0)
        self.assertEqual(t[0], 300.0)
        self.assertEqual(cible['name'], 'ameesh-dev5')
        self.assertEqual(remplacement['remplacante'], 'ameesh-dev5')

    def test_4126_aucune_libre_reste_sans_sonde(self):
        t, mono, dormir = self.horloge()
        rows = self.rangs_4126()
        rows[1] = dict(rows[1], operator_paused=True)
        etats = {r['name']: r for r in rows}
        with mock.patch.object(self.mod.registry, 'get',
                               side_effect=lambda db, nom: etats[nom]), \
             mock.patch.object(self.mod.time, 'monotonic', side_effect=mono), \
             mock.patch.object(self.mod.time, 'sleep', side_effect=dormir), \
             mock.patch.object(self.mod.time, 'time', return_value=1000.0):
            cible, pourquoi, remplacement = self.mod.selection(
                None, rows, 'ameesh-dev3', 300.0, 1000.0)
        self.assertIsNone(cible)
        self.assertIsNone(remplacement)
        self.assertIn('occupee (running)', pourquoi)
        self.assertIn('aucune autre persona menee libre', pourquoi)

    def test_4126_cible_sans_bail_n_est_pas_attendue(self):
        """Sans bail vivant, aucun tour ne la liberera : remplacee aussitot."""
        t, mono, dormir = self.horloge()
        rows = self.rangs_4126()
        rows[0] = dict(rows[0], status='idle', lease_expires_ts=10.0)
        etats = {r['name']: r for r in rows}
        with mock.patch.object(self.mod.registry, 'get',
                               side_effect=lambda db, nom: etats[nom]), \
             mock.patch.object(self.mod.time, 'monotonic', side_effect=mono), \
             mock.patch.object(self.mod.time, 'sleep', side_effect=dormir), \
             mock.patch.object(self.mod.time, 'time', return_value=1000.0):
            cible, pourquoi, remplacement = self.mod.selection(
                None, rows, 'ameesh-dev3', 300.0, 1000.0)
        self.assertEqual(t[0], 0.0)
        self.assertEqual(cible['name'], 'ameesh-dev5')
        self.assertIn('sans bail vivant', remplacement['raison'])

    def test_4126_cible_nommee_absente_reste_une_erreur(self):
        with self.assertRaises(SystemExit):
            self.mod.selection(None, self.rangs_4126(), 'inconnu', 300.0, 1000.0)

    def principal_4126(self, registre_get, faux_get, faux_history, argv):
        maintenant = 1000.0
        envois = {}

        def faux_send(db, sender, recipient, body, **kw):
            envois.update(recipient=recipient,
                          jeton=body.split('--ack ')[1].split()[0])
            return 7

        t, mono, dormir = self.horloge()
        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview',
                               return_value=self.rangs_4126()), \
             mock.patch.object(self.mod.registry, 'get', side_effect=registre_get), \
             mock.patch.object(self.mod, 'stuck', return_value=[]), \
             mock.patch.object(self.mod, 'sans_executeur', return_value=[]), \
             mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
             mock.patch.object(self.mod, 'canal_humain', return_value=[]), \
             mock.patch.object(self.mod.mail, 'send', side_effect=faux_send), \
             mock.patch.object(self.mod.mail, 'get', side_effect=faux_get), \
             mock.patch.object(self.mod.mail, 'history',
                               side_effect=lambda db, nom, **kw: faux_history(nom, envois)), \
             mock.patch.object(self.mod.time, 'time', return_value=maintenant), \
             mock.patch.object(self.mod.time, 'monotonic', side_effect=mono), \
             mock.patch.object(self.mod.time, 'sleep', side_effect=dormir), \
             redirect_stdout(sortie):
            code = self.mod.main(argv)
        return code, json.loads(sortie.getvalue()), envois

    def test_4126_main_remplacante_ok_et_nommee_dans_le_rapport(self):
        """Incident 28/10 02:29Z : SONDE_AMEESH_AGENTS_1=ameesh-dev3 en tour
        jusqu'a la borne ; ameesh-dev5 libre prend la sonde, verdict ok."""
        etats = {r['name']: r for r in self.rangs_4126()}

        def registre_get(db, nom):
            if nom == 'ameesh-dev5' and etats.get('_remis'):
                return dict(etats[nom], last_turn_ts=1001.0)
            return etats[nom]

        def faux_get(db, message_id):
            etats['_remis'] = True
            return {'id': message_id, 'delivered_ts': 1000.0}

        code, rapport, envois = self.principal_4126(
            registre_get, faux_get,
            lambda nom, envois: [{'sender': nom, 'body': envois['jeton']}],
            ['--hote', 'ameesh-agents-1', '--sonde', 'ameesh-dev3',
             '--attente', '30', '--attente-cible', '300', '--json'])
        self.assertEqual(code, 0, rapport)
        self.assertEqual(rapport['verdict'], 'ok')
        self.assertEqual(envois['recipient'], 'ameesh-dev5')
        self.assertEqual(rapport['sonde']['destinataire'], 'ameesh-dev5')
        self.assertEqual(rapport['remplacement']['cible_nommee'], 'ameesh-dev3')
        self.assertEqual(rapport['remplacement']['remplacante'], 'ameesh-dev5')
        self.assertIn('remplacante ameesh-dev5', rapport['pourquoi'])

    def test_4126_main_vraie_erreur_de_tour_reste_un_echec(self):
        """La remplacante dont le tour echoue reste un KO (negatif concluant)."""
        etats = {r['name']: r for r in self.rangs_4126()}

        def registre_get(db, nom):
            if nom == 'ameesh-dev5' and etats.get('_remis'):
                return dict(etats[nom], last_turn_ts=1001.0, status='blocked',
                            last_error='harnais en echec')
            return etats[nom]

        def faux_get(db, message_id):
            etats['_remis'] = True
            return {'id': message_id, 'delivered_ts': 1000.0}

        code, rapport, _ = self.principal_4126(
            registre_get, faux_get, lambda nom, envois: [],
            ['--hote', 'ameesh-agents-1', '--sonde', 'ameesh-dev3',
             '--attente', '30', '--json'])
        self.assertEqual(code, 1, rapport)
        self.assertEqual(rapport['verdict'], 'ko')
        self.assertIn('tour en echec : harnais en echec', rapport['sonde']['tour'])
        self.assertEqual(rapport['remplacement']['remplacante'], 'ameesh-dev5')

    def test_main_revalide_une_cible_mise_en_pause_avant_envoi(self):
        """#2190 : une cible libre dans la photo mais mise en pause avant
        l'envoi n'est pas sondee : son tour sera refuse."""
        maintenant = 1000.0
        rows = [{'name': 'agent', 'host': 'ameesh-agents-1', 'mode': 'execute',
                 'status': 'idle', 'lease_owner': 'r',
                 'lease_expires_ts': maintenant + 60}]
        en_pause = dict(rows[0], operator_paused=True)
        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview', return_value=rows), \
             mock.patch.object(self.mod.registry, 'get', return_value=en_pause), \
             mock.patch.object(self.mod, 'stuck', return_value=[]), \
             mock.patch.object(self.mod, 'sans_executeur', return_value=[]), \
             mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
             mock.patch.object(self.mod, 'canal_humain', return_value=[]), \
             mock.patch.object(self.mod.mail, 'send') as envoi, \
             mock.patch.object(self.mod.time, 'time', return_value=maintenant), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'ameesh-agents-1', '--json'])
        self.assertEqual(code, 0, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'sans-sonde')
        self.assertIn('cible agent occupee (pause operateur)', rapport['pourquoi'])
        envoi.assert_not_called()

    def test_main_ko_non_remise_sort_en_rapport_json_complet(self):
        """#2190 : une remise qui expire garde son rapport JSON (message,
        tour, cible, borne), au lieu d'une sortie vide ou d'un simple KO."""
        sha = '0123456789abcdef0123456789abcdef01234567'
        maintenant = 1000.0
        rows = [{'name': 'agent', 'host': 'ameesh-agents-1', 'mode': 'execute',
                 'status': 'idle', 'lease_owner': 'r',
                 'lease_expires_ts': maintenant + 60}]
        horloge = iter(maintenant + i * 0.5 for i in range(1000))
        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview', return_value=rows), \
             mock.patch.object(self.mod.registry, 'get', return_value=rows[0]), \
             mock.patch.object(self.mod, 'stuck', return_value=[]), \
             mock.patch.object(self.mod, 'sans_executeur', return_value=[]), \
             mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
             mock.patch.object(self.mod, 'canal_humain', return_value=[]), \
             mock.patch.object(self.mod.mail, 'send', return_value=7), \
             mock.patch.object(self.mod.mail, 'get',
                               return_value={'id': 7, 'delivered_ts': None}), \
             mock.patch.object(self.mod.time, 'time', return_value=maintenant), \
             mock.patch.object(self.mod.time, 'monotonic',
                               side_effect=lambda: next(horloge)), \
             mock.patch.object(self.mod.time, 'sleep'), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'ameesh-agents-1', '--sha', sha,
                                  '--attente', '1', '--json'])
        self.assertEqual(code, 1, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'ko')
        self.assertEqual(rapport['sha'], sha)
        self.assertEqual(rapport['attente_s'], 1.0)
        self.assertEqual(rapport['sonde']['message'], 7)
        self.assertEqual(rapport['sonde']['destinataire'], 'agent')
        self.assertEqual(rapport['sonde']['tour'], 'non remis')
        self.assertIsNone(rapport['sonde']['delai_s'])
        self.assertIsNone(rapport['sonde']['remise_s'])
        self.assertEqual(rapport['sonde']['statut_cible'], 'idle')

    def test_main_sonde_non_concluante_sort_en_code_3_sans_ko(self):
        """#4089 : une attente budget jusqu'a la borne n'est pas un hote
        malsain : verdict non-concluant, code 3 (aucun retour arriere)."""
        maintenant = 1000.0
        rows = [{'name': 'cx1', 'host': 'ameesh-agents-2', 'status': 'idle',
                 'lease_expires_ts': maintenant + 60}]
        resultat = {'message': 7, 'destinataire': 'cx1', 'delai_s': None,
                    'remise_s': 2.6, 'hote_destinataire': 'ameesh-agents-2',
                    'statut_cible': 'idle', 'concluant': False,
                    'tour': "remis, sans reponse de l'outil (attente budget : x)"}
        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview', return_value=rows), \
             mock.patch.object(self.mod.registry, 'get', return_value=rows[0]), \
             mock.patch.object(self.mod, 'stuck', return_value=[]), \
             mock.patch.object(self.mod, 'sans_executeur', return_value=[]), \
             mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
             mock.patch.object(self.mod, 'canal_humain', return_value=[]), \
             mock.patch.object(self.mod, 'choose', return_value=(rows[0], None)), \
             mock.patch.object(self.mod, 'driven', return_value=True), \
             mock.patch.object(self.mod, 'libre', return_value=True), \
             mock.patch.object(self.mod, 'probe', return_value=resultat), \
             mock.patch.object(self.mod.time, 'time', return_value=maintenant), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'ameesh-agents-2', '--json'])
        self.assertEqual(code, 3, sortie.getvalue())
        self.assertEqual(json.loads(sortie.getvalue())['verdict'], 'non-concluant')

    def test_main_base_injoignable_sort_en_rapport_non_concluant(self):
        """#2190 : une erreur de base sort en rapport JSON, pas en trace vide ;
        #3976 : rien n'a ete constate sur l'hote sonde, le verdict est non
        concluant (code 3), jamais un KO qui le remettrait en arriere."""
        sortie = io.StringIO()
        with mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.db_mod, 'connect',
                               side_effect=RuntimeError('base injoignable')), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'ameesh-agents-1', '--json'])
        self.assertEqual(code, 3, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'non-concluant')
        self.assertEqual(rapport['hote'], 'ameesh-agents-1')
        self.assertIn('base injoignable', rapport['erreur'])

    def test_main_base_du_serveur_injoignable_reste_ko(self):
        """--serveur : la base est celle de l'hote sonde, redemarre : KO."""
        sortie = io.StringIO()
        with mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.db_mod, 'connect',
                               side_effect=RuntimeError('base injoignable')), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'manaty-mesh-1', '--serveur', '--json'])
        self.assertEqual(code, 1, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'ko')
        self.assertEqual(rapport['erreur'], 'RuntimeError: base injoignable')

    def test_main_cible_inconnue_sort_en_rapport_ko(self):
        """#2190 : une cible nommee absente n'est pas une trace vide."""
        rows = [{'name': 'a', 'host': 'ameesh-agents-1', 'mode': 'execute',
                 'status': 'idle', 'lease_owner': 'r',
                 'lease_expires_ts': 1060.0}]
        sortie = io.StringIO()
        with mock.patch.object(self.mod.db_mod, 'connect', return_value=mock.MagicMock()), \
             mock.patch.object(self.mod.config, 'load', return_value={}), \
             mock.patch.object(self.mod.registry, 'overview', return_value=rows), \
             mock.patch.object(self.mod, 'stuck', return_value=[]), \
             mock.patch.object(self.mod, 'sans_executeur', return_value=[]), \
             mock.patch.object(self.mod, 'dead_letters', return_value=[]), \
             mock.patch.object(self.mod, 'canal_humain', return_value=[]), \
             mock.patch.object(self.mod.mail, 'send') as envoi, \
             mock.patch.object(self.mod.time, 'time', return_value=1000.0), \
             redirect_stdout(sortie):
            code = self.mod.main(['--hote', 'ameesh-agents-1', '--sonde',
                                  'absente', '--json'])
        self.assertEqual(code, 1, sortie.getvalue())
        rapport = json.loads(sortie.getvalue())
        self.assertEqual(rapport['verdict'], 'ko')
        self.assertIn("n'est pas une persona", rapport['erreur'])
        envoi.assert_not_called()


if __name__ == '__main__':
    unittest.main()
