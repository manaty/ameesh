# SPDX-License-Identifier: AGPL-3.0-only
"""#1323 signal « staging actif » : preuves, idempotence, non-signal.

Deux bancs, aucun acces reel :

* `actif.py` isole (journal fabrique, envoi substitue) : l'attestation n'est
  ecrite et le rapporteur reveille que si l'activation est prouvee, une seule
  fois par commit ;
* `staging.sh` sur stubs : le signal est appele apres une sequence complete et
  saine, jamais apres un echec, jamais sur un sous-ensemble ni a blanc.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/mise-a-jour'

SHA = '0123456789abcdef0123456789abcdef01234567'
DEPUIS = '2026-10-14T16:00:00Z'
TS = '2026-10-14T16:05:00Z'
HOTES = ('ameesh-agents-1', 'ameesh-agents-2', 'manaty-mesh-1', 'pc-smichea')
SONDE = 'courrier %s : sonde remise a quidu en 2.0s (message 7)'

#: variables du runner qui supplanteraient le HOME, la config ou l'etat
#: temporaires : jamais heritees par le banc (meme garde que #1155/#1195).
ENV_PREFIXES = ('AMEESH_', 'AGENT_MESH_', 'AGENT_MAIL_')
ENV_EXTRA = ('XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME')
DSN_REFUSE = 'postgresql://refuse:refuse@127.0.0.1:1/refuse_1323'
ENV_BAC = ('AMEESH_CONFIG', 'AMEESH_DSN', 'AGENT_MESH_DSN', 'AMEESH_SCHEMA',
           'AGENT_MESH_SCHEMA', 'AMEESH_STATE', 'AGENT_MESH_STATE',
           'AMEESH_THREADS', 'AGENT_MAIL_STATE', 'AGENT_MAIL_CONFIG')


def env_isole(environ=None):
    """Environnement du banc, debarrasse des reglages du runner."""
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items()
            if not k.startswith(ENV_PREFIXES) and k not in ENV_EXTRA}


def charger_actif():
    spec = importlib.util.spec_from_file_location('actif_1323', DEPLOY / 'actif.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evenement(hote, etape, verdict='ok', detail=None, ts=TS):
    return {'ts': ts, 'hote': hote, 'etape': etape, 'verdict': verdict,
            'sha': SHA, 'precedent': None, 'detail': detail}


def journal_complet():
    """Journal d'une sequence complete : les quatre hotes actives."""
    out = []
    for hote in HOTES:
        out.append(evenement(hote, 'installer', detail='installe %s' % SHA[:9]))
        out.append(evenement(hote, 'redemarrer'))
        out.append(evenement(hote, 'controler'))
        out.append(evenement(hote, 'sante', detail=SONDE % hote))
    out.append(evenement(None, 'sequence'))
    return out


class ActifTest(unittest.TestCase):
    """actif.py : ce qui merite un signal, et ce qui n'en merite pas."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1323-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.etat = self.root / 'etat'
        self.journal = self.root / 'staging.jsonl'
        self.actif = charger_actif()
        self.envois = []
        patcheur = mock.patch.object(self.actif, 'envoyer', side_effect=self.faux_envoi)
        patcheur.start(); self.addCleanup(patcheur.stop)

    def faux_envoi(self, dest, corps, charge):
        self.envois.append({'dest': dest, 'corps': corps, 'charge': charge})
        return 4242

    def ecrire(self, evenements):
        self.journal.write_text(''.join(json.dumps(e) + '\n' for e in evenements),
                                encoding='utf-8')

    def lancer(self, *args):
        argv = ['--journal', str(self.journal), '--sha', SHA, '--depuis', DEPUIS,
                '--etat-dir', str(self.etat)]
        argv += [str(a) for a in args]
        sortie, erreur = io.StringIO(), io.StringIO()
        with redirect_stdout(sortie), redirect_stderr(erreur):
            code = self.actif.main(argv)
        return code, sortie.getvalue(), erreur.getvalue()

    def hotes(self, actifs=None):
        valeurs = actifs or {h: SHA for h in HOTES}
        out = []
        for hote in HOTES:
            out += ['--hote', '%s=%s' % (hote, valeurs[hote])]
        return out

    def attestations(self):
        return sorted(self.etat.glob('actif-*.json'))

    def test_sequence_complete_atteste_et_reveille_une_fois(self):
        self.ecrire(journal_complet())
        code, sortie, erreur = self.lancer(*self.hotes(), '--lot', '917',
                                           '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        fichiers = self.attestations()
        self.assertEqual(len(fichiers), 1, fichiers)
        attestation = json.loads(fichiers[0].read_text())
        self.assertEqual(attestation['schema'], 'ameesh-staging-actif/1')
        self.assertEqual(attestation['sha'], SHA)
        self.assertEqual(sorted(attestation['hotes']), sorted(HOTES))
        self.assertEqual(attestation['lots'], ['917'])
        for hote in HOTES:
            self.assertEqual(attestation['hotes'][hote]['actif'], SHA)
            self.assertEqual(attestation['hotes'][hote]['etapes'],
                             {e: 'ok' for e in ('installer', 'redemarrer', 'controler', 'sante')})
            self.assertIn('sonde remise', attestation['hotes'][hote]['sante'])
        self.assertEqual(attestation['notifies']['docs01']['message'], 4242)
        self.assertEqual(len(self.envois), 1)
        self.assertEqual(self.envois[0]['dest'], 'docs01')
        for attendu in (SHA[:9], '#917', HOTES[0], 'Aucune cloture'):
            self.assertIn(attendu, self.envois[0]['corps'])
        # relance du meme commit : aucun second courrier, aucune duplication
        code, _, erreur = self.lancer(*self.hotes(), '--lot', '917',
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        self.assertEqual(len(self.envois), 1)
        self.assertEqual(len(self.attestations()), 1)

    def test_un_rapporteur_deja_prevenu_n_est_pas_reveille_pour_un_autre(self):
        self.ecrire(journal_complet())
        self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.lancer(*self.hotes(), '--rapporteur', 'docs01', '--rapporteur', 'bureau')
        self.assertEqual([e['dest'] for e in self.envois], ['docs01', 'bureau'])

    def test_retour_disqualifie_la_sequence(self):
        evenements = journal_complet()
        evenements.append(evenement('manaty-mesh-1', 'retour'))
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('retour/rollback', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_preuve_manquante_n_emet_rien(self):
        evenements = [e for e in journal_complet()
                      if not (e['hote'] == 'ameesh-agents-2' and e['etape'] == 'redemarrer')]
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('redemarrer', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_commit_actif_different_de_la_cible(self):
        self.ecrire(journal_complet())
        actifs = {h: SHA for h in HOTES}
        actifs['pc-smichea'] = 'deadbeefdeadbeef'
        code, _, erreur = self.lancer(*self.hotes(actifs), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('commit actif', erreur)
        self.assertEqual(self.envois, [])

    def test_commit_actif_court_du_poste_accepte(self):
        self.ecrire(journal_complet())
        actifs = {h: SHA for h in HOTES}
        actifs['pc-smichea'] = SHA[:7]
        code, _, erreur = self.lancer(*self.hotes(actifs), '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        self.assertEqual(len(self.attestations()), 1)

    def test_sequence_partielle_refusee(self):
        self.ecrire(journal_complet())
        code, _, erreur = self.lancer('--hote', 'ameesh-agents-1=%s' % SHA,
                                      '--hote', 'ameesh-agents-2=%s' % SHA,
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('partielle', erreur)
        self.assertEqual(self.envois, [])

    def test_sans_sonde_refuse(self):
        self.ecrire(journal_complet())
        code, _, erreur = self.lancer(*self.hotes(), '--sans-sonde',
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('sonde', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_execution_precedente_du_meme_jour_ignoree(self):
        """Le journal est quotidien : un echec anterieur (retour) ne
        disqualifie pas l'execution courante, posterieure a --depuis."""
        anciens = [evenement(h, 'installer', ts='2026-10-14T09:00:00Z') for h in HOTES]
        anciens.append(evenement('manaty-mesh-1', 'retour', ts='2026-10-14T09:01:00Z'))
        self.ecrire(anciens + journal_complet())
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        self.assertEqual(len(self.attestations()), 1)
        self.assertEqual(len(self.envois), 1)

    def test_courrier_non_remis_attestation_conservee(self):
        self.ecrire(journal_complet())
        with mock.patch.object(self.actif, 'envoyer', side_effect=RuntimeError('base absente')):
            code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 4, erreur)
        self.assertIn('base absente', erreur)
        fichiers = self.attestations()
        self.assertEqual(len(fichiers), 1)
        attestation = json.loads(fichiers[0].read_text())
        self.assertEqual(attestation['notifies'], {})


class StagingSignalTest(unittest.TestCase):
    """staging.sh : le signal n'est demande qu'apres une sequence saine."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1323-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        (self.state / 'calls.log').touch()
        (self.state / 'scenario').write_text('')
        self.home = self.root / 'home'; self.home.mkdir()
        self.repo = self.root / 'repo'
        self.script_dir = self.repo / 'deploy/mise-a-jour'
        self.script_dir.mkdir(parents=True)
        for name in ('staging.sh', 'sante-courrier.py', 'actif.py'):
            (self.script_dir / name).write_text((DEPLOY / name).read_text())
        for name, source in (('vm.sh', STUB), ('poste.sh', STUB)):
            path = self.script_dir / name
            path.write_text(source)
            path.chmod(0o755)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        (self.bin / 'ssh').write_text(SSH)
        (self.bin / 'ssh').chmod(0o755)
        venv = self.home / '.local/share/ameesh/venv/bin'; venv.mkdir(parents=True)
        for name, source in (('python', VENV_PYTHON), ('ameesh', VENV_AMEESH)):
            path = venv / name
            path.write_text(source)
            path.chmod(0o755)
        self.config = self.root / 'bascule.env'
        self.config.write_text(
            'HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 '
            'manaty-mesh-1=root@10.77.0.2 pc-smichea=poste"\n'
            'VM=10.77.0.2\nPERSONAS_VM=\n')
        run = ['git', '-C', str(self.repo)]
        env = self.git_env()
        subprocess.run(run + ['init', '-q'], check=True, capture_output=True)
        (self.repo / 'marque').write_text('x\n')
        subprocess.run(run + ['add', 'marque'], check=True, capture_output=True, env=env)
        subprocess.run(run + ['-c', 'user.email=t@invalid', '-c', 'user.name=t',
                              'commit', '-qm', 'base'], check=True, capture_output=True, env=env)
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
                        AMEESH_CONFIG=str(self.root / 'config-absente.json'),
                        AMEESH_STATE=str(bacs['etat']),
                        AGENT_MESH_STATE=str(bacs['etat']),
                        AMEESH_THREADS=str(bacs['fils']),
                        AGENT_MAIL_STATE=str(bacs['v0']),
                        AGENT_MAIL_CONFIG=str(bacs['config']),
                        AMEESH_DSN=DSN_REFUSE, AGENT_MESH_DSN=DSN_REFUSE,
                        AMEESH_SCHEMA='refuse_1323', AGENT_MESH_SCHEMA='refuse_1323',
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')

    def git_env(self):
        return dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                    GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')

    def run_staging(self, *args, env=None):
        env = env or self.env
        self.assertEqual(env.get('HOME'), str(self.home))
        self.assertFalse(Path(env.get('AMEESH_CONFIG', '')).exists())
        for nom in ('AMEESH_STATE', 'AGENT_MESH_STATE', 'AMEESH_THREADS',
                    'AGENT_MAIL_STATE', 'AGENT_MAIL_CONFIG'):
            self.assertTrue(str(env.get(nom, '')).startswith(str(self.root)), nom)
        self.assertEqual(env.get('AMEESH_DSN'), DSN_REFUSE)
        p = subprocess.run(['bash', str(self.script_dir / 'staging.sh'), *args],
                           env=env, capture_output=True, text=True, timeout=180)
        return p

    def calls(self):
        return (self.state / 'calls.log').read_text().splitlines()

    def actifs(self):
        return [l for l in self.calls() if l.startswith('actif ')]

    def notifications(self):
        return [l for l in self.calls() if l.startswith('venv-python notification')]

    def creer_poste_installe(self):
        """Le poste publie un nom court : la relecture doit le retrouver."""
        court = self.sha[:7]
        partage = self.home / '.local/share/ameesh'
        (partage / ('src-%s' % court)).mkdir(parents=True, exist_ok=True)
        (partage / 'src-current').symlink_to('src-%s' % court)

    def test_sequence_complete_appelle_le_signal_avec_les_preuves(self):
        self.creer_poste_installe()
        p = self.run_staging('--lots', '917 988', '--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        appels = self.actifs()
        self.assertEqual(len(appels), 1, self.calls())
        self.assertEqual(self.calls()[-1], appels[0])  # rien apres le signal
        ligne = appels[0]
        self.assertIn('actif.py', ligne)
        self.assertIn('--sha %s' % self.sha, ligne)
        self.assertIn('--depuis ', ligne)
        self.assertIn('--hote pc-smichea=%s' % self.sha[:7], ligne)
        for hote in HOTES:
            self.assertIn('--hote %s=' % hote, ligne)
        self.assertIn('--lot 917', ligne)
        self.assertIn('--lot 988', ligne)
        self.assertIn('--rapporteur docs01', ligne)
        self.assertNotIn('--sans-sonde', ligne)

    def test_echec_et_retour_n_emettent_aucun_signal(self):
        (self.state / 'scenario').write_text('ko=manaty-mesh-1:controler\n')
        p = self.run_staging('--lots', '917', '--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(self.actifs(), [])
        self.assertEqual(len(self.notifications()), 1)
        self.assertIn('mesh-design', self.calls()[-1])

    def test_sous_ensemble_n_atteste_rien(self):
        p = self.run_staging('--hotes', 'ameesh-agents-1 ameesh-agents-2',
                             '--lots', '917', '--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(self.actifs(), [])
        self.assertIn('sequence partielle', p.stderr)

    def test_sans_sonde_transmet_le_refus(self):
        self.creer_poste_installe()
        p = self.run_staging('--sans-sonde', '--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        appels = self.actifs()
        self.assertEqual(len(appels), 1, self.calls())
        self.assertIn('--sans-sonde', appels[0])

    def test_a_blanc_n_appelle_rien(self):
        p = self.run_staging('--a-blanc', '--lots', '917')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.actifs(), [])

    def test_listes_normalisees(self):
        self.creer_poste_installe()
        p = self.run_staging('--lots', '917, 988 917', '--rapporteurs', 'docs01 docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        ligne = self.actifs()[0]
        self.assertEqual(ligne.count('--lot '), 2)
        self.assertEqual(ligne.count('--rapporteur '), 1)


#: stub vm.sh/poste.sh : journalise l'appel, injecte un KO par scenario
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
printf 'call host=%s action=%s args=%s\n' "$host" "$action" "$*" >> "$state/calls.log"
if grep -qx "ko=$host:$action" "$state/scenario" 2>/dev/null; then
  grep -vx "ko=$host:$action" "$state/scenario" > "$state/scenario.tmp" || true
  mv "$state/scenario.tmp" "$state/scenario"
  echo "echec injecte : $host $action" >&2
  exit 1
fi
exit 0
'''

#: venv du poste : distingue la notification, le signal actif et la sonde
VENV_PYTHON = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
case "${1:-}" in
  -) printf 'venv-python notification %s\n' "$*" >> "$state/calls.log"; exit 0 ;;
esac
if [ "$(basename "${1:-}")" = actif.py ]; then
  printf 'actif %s\n' "$*" >> "$state/calls.log"
  exit 0
fi
hote=""; precedent=""
for arg in "$@"; do
  [ "$precedent" = --hote ] && hote=$arg
  precedent=$arg
done
if grep -qx "ko=sante:$hote" "$state/scenario" 2>/dev/null; then
  grep -vx "ko=sante:$hote" "$state/scenario" > "$state/scenario.tmp" || true
  mv "$state/scenario.tmp" "$state/scenario"
  echo "courrier $hote : KO (echec injecte)" >&2
  exit 1
fi
printf 'courrier %s : sonde remise a quidu en 2.0s (message 7)\n' "$hote"
'''

VENV_AMEESH = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
printf 'venv-ameesh %s\n' "$*" >> "$state/calls.log"
exit 0
'''

SSH = r'''#!/usr/bin/env bash
printf 'stub %s %s\n' "$0" "$*" >> "${FAKE_STATE:?}/calls.log"
printf 'ssh %s\n' "$*" >> "${FAKE_STATE:?}/calls.log"
exit 255
'''


if __name__ == '__main__':
    unittest.main()
