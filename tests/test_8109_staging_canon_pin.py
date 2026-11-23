# SPDX-License-Identifier: AGPL-3.0-only
"""#8109 : l'épingle canon de l'exécutant suit le canon des agents au staging.

Le banc est hors réseau, hors base et hors hôte : trois dépôts git jetables
(source des agents, copie de l'exécutant, home du compte), un faux binaire
`ameesh` dans le venv du compte. On vérifie :

* une tête en avance rapide déplace `canon_ref` (et le ref de suivi) d'un
  geste atomique, en gardant une copie de l'ancien fichier ;
* une passe déjà à la tête ne réécrit rien ;
* un `canon check` rouge, une divergence, une configuration illisible ou un
  lien de configuration laissent la copie et l'épingle intactes ;
* la forme `canons` (première entrée = canon par défaut) et un `canon_ref`
  de branche (déjà suivi par #7053) ne sont pas confondus ;
* compte absent, configuration absente, copie absente, source absente ;
* le branchement dans `vm.sh` et `staging.sh` existe et reste non bloquant.
"""
from tests import support  # noqa: F401 — environnement isolé du poste

import contextlib
import getpass
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'deploy/mise-a-jour/canon-executant.py'
VM_SH = ROOT / 'deploy/mise-a-jour/vm.sh'
STAGING_SH = ROOT / 'deploy/mise-a-jour/staging.sh'


def _charger(chemin, nom):
    spec = importlib.util.spec_from_file_location(nom, chemin)
    module = importlib.util.module_from_spec(spec)
    sys.modules[nom] = module
    spec.loader.exec_module(module)
    return module


helper = _charger(HELPER, 'canon_executant_8109')
COMPTE = getpass.getuser()


def _env_git():
    """Un git hermétique : ni configuration du poste ni identité héritée."""
    env = {nom: valeur for nom, valeur in os.environ.items()
           if not nom.startswith('GIT_')}
    env.update(
        GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME='banc-8109',
        GIT_AUTHOR_EMAIL='banc-8109@example.invalid',
        GIT_COMMITTER_NAME='banc-8109',
        GIT_COMMITTER_EMAIL='banc-8109@example.invalid',
        LC_ALL='C',
    )
    return env


def _git(*args, cwd=None, check=True):
    fait = subprocess.run(['git', *map(str, args)], cwd=cwd,
                          env=_env_git(), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
    if check and fait.returncode != 0:
        raise AssertionError('git %s : %s' % (' '.join(map(str, args)),
                                              fait.stderr.strip()))
    return fait


class Banc(unittest.TestCase):
    """Deux commits dans la source, la copie à l'ancien, l'épingle à l'ancien."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='8109-pin-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'source'
        self.copie = self.root / 'copie'
        self.home = self.root / 'home'
        self.venv = self.home / '.local/share/ameesh/venv'
        self.config = self.home / '.config/ameesh/config.json'
        self.controle = self.home / 'canon-check.log'
        self.source.mkdir()
        _git('init', '-q', '-b', 'main', cwd=self.source)
        (self.source / 'fiche').write_text('un\n')
        _git('add', 'fiche', cwd=self.source)
        _git('commit', '-qm', 'un', cwd=self.source)
        self.ancien = _git('rev-parse', 'HEAD', cwd=self.source).stdout.strip()
        (self.source / 'fiche').write_text('un\ndeux\n')
        _git('commit', '-qam', 'deux', cwd=self.source)
        self.tete = _git('rev-parse', 'HEAD', cwd=self.source).stdout.strip()
        _git('update-ref', 'refs/remotes/origin/main', self.tete, cwd=self.source)
        _git('clone', '-q', str(self.source), str(self.copie))
        _git('update-ref', 'refs/remotes/origin/main', self.ancien, cwd=self.copie)
        self._poser_binaire(0)
        self._poser_config({'canon': str(self.copie), 'canon_ref': self.ancien,
                            'segments': {'garde': True}})

    def _poser_binaire(self, code):
        binaire = self.venv / 'bin/ameesh'
        binaire.parent.mkdir(parents=True, exist_ok=True)
        script = ('#!/bin/sh\nprintf \'%%s\\n\' "$*" >> %s\n'
                  % shlex.quote(str(self.controle)))
        if code:
            script += 'echo "canon invalide" >&2\nexit %d\n' % code
        else:
            script += 'exit 0\n'
        binaire.write_text(script)
        binaire.chmod(0o755)

    def _poser_config(self, donnees=None, brut=None):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        if brut is not None:
            self.config.write_bytes(brut)
        else:
            self.config.write_text(json.dumps(donnees, indent=2) + '\n')

    def _lire_config(self):
        return json.loads(self.config.read_text())

    def _lancer(self, **remplacements):
        """Le geste, sous le git hermétique du banc et sans minuterie."""
        options = dict(account_name=COMPTE, home=str(self.home),
                       config=str(self.config), repo=str(self.copie),
                       venv=str(self.venv), source=str(self.source))
        options.update(remplacements)
        with mock.patch.dict(os.environ, _env_git()):
            return helper.avancer(**options)

    def _assert_refus(self, resultat, motif, *, controle=False):
        self.assertEqual(resultat['state'], 'refused', resultat)
        self.assertIn(motif, resultat['reason'])
        self.assertEqual(self._lire_config()['canon_ref'], self.ancien)
        self.assertEqual(_git('rev-parse', 'refs/remotes/origin/main',
                              cwd=self.copie).stdout.strip(), self.ancien)
        self.assertFalse((self.config.parent / 'config.json.avant-8109').exists())
        self.assertEqual(self.controle.exists(), controle)
        self.assertEqual(list((self.home / '.cache/ameesh').glob('*.bundle')), [])

    # -- le geste nominal -------------------------------------------------

    def test_avance_rapide_deplace_epingle_et_ref_de_suivi(self):
        avant = self.config.read_bytes()
        resultat = self._lancer()
        self.assertEqual(resultat['state'], 'installed', resultat)
        self.assertEqual(resultat['from'], self.ancien)
        self.assertEqual(resultat['to'], self.tete)
        self.assertEqual(resultat['backup'], 'config.json.avant-8109')
        donnees = self._lire_config()
        self.assertEqual(donnees['canon_ref'], self.tete)
        self.assertEqual(donnees['segments'], {'garde': True})
        self.assertEqual((self.config.parent / 'config.json.avant-8109')
                         .read_bytes(), avant)
        self.assertEqual(_git('rev-parse', 'refs/remotes/origin/main',
                              cwd=self.copie).stdout.strip(), self.tete)
        self.assertEqual(self.controle.read_text().strip(),
                         'canon check --canon %s --ref %s' % (self.copie, self.tete))
        self.assertEqual(list((self.home / '.cache/ameesh').glob('*.bundle')), [])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_deja_a_la_tete_ne_reecrit_rien(self):
        self.assertEqual(self._lancer()['state'], 'installed')
        avant = self.config.read_bytes()
        sauvegarde = self.config.parent / 'config.json.avant-8109'
        avant_sauvegarde = sauvegarde.read_bytes()
        resultat = self._lancer()
        self.assertEqual(resultat['state'], 'current', resultat)
        self.assertEqual(self.config.read_bytes(), avant)
        self.assertEqual(sauvegarde.read_bytes(), avant_sauvegarde)
        # Une seule validation : la seconde passe s'arrête avant le check.
        self.assertEqual(len(self.controle.read_text().splitlines()), 1)

    def test_copie_deduite_de_la_configuration(self):
        # Aucun `~/canon` : la racine vient du champ `canon` de la config.
        self.assertFalse((self.home / 'canon').exists())
        resultat = self._lancer(repo=None)
        self.assertEqual(resultat['state'], 'installed', resultat)
        self.assertEqual(resultat['repo'], str(self.copie))
        self.assertEqual(self._lire_config()['canon_ref'], self.tete)

    def test_copie_deduite_de_canons(self):
        self._poser_config({'canons': [{'path': str(self.copie),
                                        'ref': self.ancien}]})
        resultat = self._lancer(repo=None)
        self.assertEqual(resultat['state'], 'installed', resultat)
        self.assertEqual(resultat['repo'], str(self.copie))
        self.assertEqual(self._lire_config()['canons'][0]['ref'], self.tete)

    def test_deux_executants_suivent_la_meme_tete(self):
        """AG1 puis AG2 : chaque épingle finit sur la tête validée."""
        home2 = self.root / 'home2'
        copie2 = self.root / 'copie2'
        config2 = home2 / '.config/ameesh/config.json'
        _git('clone', '-q', str(self.source), str(copie2))
        _git('update-ref', 'refs/remotes/origin/main', self.ancien, cwd=copie2)
        config2.parent.mkdir(parents=True)
        config2.write_text(json.dumps({'canon': str(copie2),
                                       'canon_ref': self.ancien}))
        for home, copie, config in ((self.home, self.copie, self.config),
                                    (home2, copie2, config2)):
            resultat = self._lancer(home=str(home), config=str(config),
                                    repo=str(copie), venv=str(self.venv))
            self.assertEqual(resultat['state'], 'installed', resultat)
            donnees = json.loads(config.read_text())
            self.assertEqual(donnees['canon_ref'], self.tete)
            self.assertEqual(_git('rev-parse', 'refs/remotes/origin/main',
                                  cwd=copie).stdout.strip(), self.tete)

    # -- refus : rien ne bouge -------------------------------------------

    def test_canon_check_rouge_laisse_tout(self):
        self._poser_binaire(1)
        self._assert_refus(self._lancer(), 'canon check', controle=True)

    def test_divergence_laisse_tout(self):
        _git('checkout', '-q', '--orphan', 'autre', cwd=self.source)
        (self.source / 'fiche').write_text('autre\n')
        _git('add', 'fiche', cwd=self.source)
        _git('commit', '-qm', 'autre', cwd=self.source)
        divergent = _git('rev-parse', 'HEAD', cwd=self.source).stdout.strip()
        _git('update-ref', 'refs/remotes/origin/main', divergent, cwd=self.source)
        self._assert_refus(self._lancer(), 'diverg')

    def test_json_illisible_laisse_le_fichier(self):
        self._poser_config(brut=b'{pas du json\n')
        avant = self.config.read_bytes()
        resultat = self._lancer()
        self.assertEqual(resultat['state'], 'refused', resultat)
        self.assertIn('configuration JSON illisible', resultat['reason'])
        self.assertEqual(self.config.read_bytes(), avant)
        self.assertFalse((self.config.parent / 'config.json.avant-8109').exists())

    def test_canons_et_canon_ref_ensemble_refuse(self):
        self._poser_config({'canon': str(self.copie), 'canon_ref': self.ancien,
                            'canons': [{'path': str(self.copie)}]})
        self._assert_refus(self._lancer(), 'canons et canon_ref')

    def test_lien_de_configuration_refuse(self):
        cible = self.root / 'ailleurs.json'
        cible.write_text(json.dumps({'canon': str(self.copie),
                                     'canon_ref': self.ancien}))
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.unlink()
        self.config.symlink_to(cible)
        resultat = self._lancer()
        self.assertEqual(resultat['state'], 'refused', resultat)
        self.assertIn('configuration illisible', resultat['reason'])
        self.assertEqual(json.loads(cible.read_text())['canon_ref'], self.ancien)

    def test_tete_deja_epinglee_mais_absente_de_la_copie(self):
        """Une épingle à la tête ne vaut `current` que si la copie la porte."""
        copie = self.root / 'copie-ancienne'
        _git('init', '-q', '-b', 'main', copie)
        _git('fetch', '-q', str(self.source), self.ancien, cwd=copie)
        _git('update-ref', 'refs/remotes/origin/main', self.ancien, cwd=copie)
        self._poser_config({'canon': str(copie), 'canon_ref': self.tete})
        resultat = self._lancer(repo=str(copie))
        self.assertEqual(resultat['state'], 'refused', resultat)
        self.assertIn('absente', resultat['reason'])
        self.assertEqual(self._lire_config()['canon_ref'], self.tete)

    # -- formes de configuration -----------------------------------------

    def test_canons_premiere_entree_avance(self):
        second = self.root / 'second'
        second.mkdir()
        self._poser_config({'canons': [
            {'path': str(self.copie), 'ref': self.ancien},
            {'path': str(second)}]})
        resultat = self._lancer()
        self.assertEqual(resultat['state'], 'installed', resultat)
        donnees = self._lire_config()
        self.assertEqual(donnees['canons'][0]['ref'], self.tete)
        self.assertNotIn('canon_ref', donnees)
        self.assertEqual(donnees['canons'][1], {'path': str(second)})

    def test_canons_sans_ref_suit(self):
        self._poser_config({'canons': [{'path': str(self.copie)}]})
        resultat = self._lancer()
        self.assertEqual(resultat['state'], 'follows', resultat)
        self.assertEqual(self._lire_config(),
                         {'canons': [{'path': str(self.copie)}]})

    def test_canon_ref_de_branche_suit(self):
        self._poser_config({'canon': str(self.copie), 'canon_ref': 'origin/main'})
        resultat = self._lancer()
        self.assertEqual(resultat['state'], 'follows', resultat)
        self.assertEqual(self._lire_config()['canon_ref'], 'origin/main')

    # -- états d'absence --------------------------------------------------

    def test_compte_absent(self):
        resultat = self._lancer(account_name='compte-8109-inexistant')
        self.assertEqual(resultat['state'], 'absent', resultat)

    def test_configuration_absente(self):
        self.config.unlink()
        resultat = self._lancer()
        self.assertEqual(resultat['state'], 'unconfigured', resultat)

    def test_copie_absente(self):
        resultat = self._lancer(repo=str(self.root / 'copie-absente'))
        self.assertEqual(resultat['state'], 'refused', resultat)
        self.assertIn('copie', resultat['reason'])
        self.assertEqual(self._lire_config()['canon_ref'], self.ancien)

    def test_source_absente(self):
        resultat = self._lancer(source=str(self.root / 'source-absente'))
        self.assertEqual(resultat['state'], 'no-source', resultat)
        self.assertEqual(self._lire_config()['canon_ref'], self.ancien)
        self.assertFalse(self.controle.exists())

    # -- branchement ------------------------------------------------------

    def test_code_de_sortie_du_cli(self):
        """0 pour un état nominal, 2 pour un refus (staging trace le verdict)."""
        def appeler():
            sortie = io.StringIO()
            with contextlib.redirect_stdout(sortie):
                code = helper.main([
                    '--account', COMPTE, '--home', str(self.home),
                    '--config', str(self.config), '--repo', str(self.copie),
                    '--venv', str(self.venv), '--source', str(self.source)])
            return code, json.loads(sortie.getvalue())

        self._poser_binaire(1)
        code, donnees = appeler()
        self.assertEqual(code, 2)
        self.assertEqual(donnees['state'], 'refused')
        self._poser_binaire(0)
        code, donnees = appeler()
        self.assertEqual(code, 0)
        self.assertEqual(donnees['state'], 'installed')

    def test_scripts_syntaxe_et_branchement(self):
        for script in (VM_SH, STAGING_SH):
            fait = subprocess.run(['bash', '-n', str(script)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(fait.returncode, 0, fait.stderr.decode())
        vm = VM_SH.read_text()
        self.assertIn('canon-executant)', vm)
        self.assertIn('canon_executant_agents()', vm)
        self.assertIn('ENTREE_STDIN=1 ssh_agents "python3 -"', vm)
        self.assertIn('< "$ICI/canon-executant.py"', vm)
        sequence = STAGING_SH.read_text()
        self.assertIn('canon_json=$(exec_hote "$nom" canon-executant)', sequence)
        self.assertIn('trace "$nom" canon-executant non-concluant', sequence)
        self.assertNotIn('trace "$nom" canon-executant ko', sequence)
        # L'étape vient après les minuteries, avant les contrôles.
        self.assertLess(sequence.index('trace "$nom" timers ok'),
                        sequence.index('debut_etape "$nom" canon-executant'))
        self.assertLess(sequence.index('debut_etape "$nom" canon-executant'),
                        sequence.index('debut_etape "$nom" controler'))
        # Un refus n'appelle ni `echec_hote` ni `exit` dans le bloc.
        bloc = sequence[sequence.index('if [ "$(mode_de "$nom")" = agents ]; then',
                                      sequence.index('# #8109')):]
        bloc = bloc[:bloc.index('  etape "$nom : controler"')]
        self.assertNotIn('echec_hote', bloc)
        self.assertNotIn('exit ', bloc)


if __name__ == '__main__':
    unittest.main()
