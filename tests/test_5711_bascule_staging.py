# SPDX-License-Identifier: AGPL-3.0-only
"""#5711 : bascule.py prépare et active le venv de staging scellé (#5666).

Le banc reste hors réseau, hors hôte et hors base : le paquet opérateur est
synthétique (roues du test #5666), le venv est jetable, le vérificateur est
celui du dépôt. On vérifie :

* la préparation verrouillée : création avec invite fixe, installation depuis
  le lock et le wheelhouse locaux sans index, scellement #2763, puis contrat
  #5666 vérifié APRÈS scellement ;
* l'activation d'un venv scellé : les lanceurs vivent dans le venv stable, la
  version scellée n'est jamais réécrite ;
* un écart d'octet ou de mode est refusé par le vérificateur ;
* une vérification en échec ne laisse ni venv partiel ni marqueur ;
* les constantes partagées avec `venv-staging.py` ne dérivent pas.
"""
from tests import support  # noqa: F401 — environnement isolé du poste

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from tests.test_5666_venv_staging import (
    Paquet, _lock, _patches, _roue, module as staging)

ROOT = Path(__file__).resolve().parents[1]
BASCULE = ROOT / 'deploy/mise-a-jour/bascule.py'
VERIFICATEUR = ROOT / 'deploy/mise-a-jour/venv-staging.py'


def _charger(chemin, nom):
    spec = importlib.util.spec_from_file_location(nom, chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bascule = _charger(BASCULE, 'bascule_5711')


def _env_propre():
    """L'environnement du lanceur, sans le PYTHONPATH du banc.

    Hérité, `PYTHONPATH=src` ferait importer le paquet du dépôt au lieu de
    celui du venv scellé : le lanceur doit prouver le venv, pas le banc.
    """
    return {nom: valeur for nom, valeur in os.environ.items()
            if nom not in ('PYTHONPATH', 'PYTHONHOME')}


class PaquetStaging(Paquet):
    """Le paquet opérateur M2 (#5495) : runtime standard, sans extra crypto.

    Le lock reprend exactement les distributions de `runtime-pins.txt` et
    `runtime-build-pins.txt` (psutil, psycopg[binary], psycopg-binary,
    setuptools, typing-extensions, ameesh). Le paquet réel n'a pas
    cryptography : le banc ne doit pas en ajouter une roue factice, sinon la
    préparation réelle échouerait sur l'import de #3619 (B auditeur #5711).
    """

    def __init__(self, root):
        super().__init__(root)
        self.setuptools['path'].unlink()
        self.setuptools = _roue(self.wheelhouse, 'setuptools', '84.2.1',
                                module_name='setuptools')
        self.tiny['path'].unlink()
        self.ameesh['path'].unlink()
        self.ameesh = _roue(
            self.wheelhouse, 'ameesh', '1.36.0',
            scripts=('ameesh', 'agent-mail'), module_name='ameesh',
            data=(('share/ameesh/mise-a-jour/helper.py', b'HELPER = 1\n'),))
        self._build = dict(
            path=self.ameesh['path'], name=self.ameesh['filename'],
            version=self.ameesh['version'], sha256=self.ameesh['sha256'])
        self.psycopg = _roue(self.wheelhouse, 'psycopg', '3.3.6',
                             module_name='psycopg')
        self.psycopg_binary = _roue(self.wheelhouse, 'psycopg-binary', '3.3.6',
                                    module_name='psycopg_binary')
        self.psutil = _roue(self.wheelhouse, 'psutil', '7.2.2',
                            module_name='psutil')
        self.typing_extensions = _roue(
            self.wheelhouse, 'typing-extensions', '4.16.0',
            module_name='typing_extensions')
        self.lock = _lock(self.root, (
            self.psutil, self.psycopg, self.psycopg_binary, self.setuptools,
            self.typing_extensions, self.ameesh))
        self.lock.write_text(self.lock.read_text().replace(
            'psycopg==3.3.6', 'psycopg[binary]==3.3.6'))


class PreparationVerrouilleeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='5711-preparation-')
        cls.root = Path(cls.temp.name)
        cls.paquet = PaquetStaging(cls.root)
        if not cls.paquet.verifie_root:
            raise unittest.SkipTest(
                'Python root indisponible : la vérification #5666 est fail closed')
        cls.sha7 = cls.paquet.sha[:7]
        with cls.paquet.garde():
            patch_preparer, patch_construire = _patches(cls.paquet.ameesh)
            with patch_preparer, patch_construire:
                cls.document = staging._produire(
                    cls.paquet.depot, sha=cls.paquet.sha, tag=None,
                    lock_path=cls.paquet.lock,
                    wheelhouse=cls.paquet.wheelhouse,
                    python=cls.paquet.python,
                    output=cls.paquet.sortie / 'scelle.json',
                    travail=cls.root / 'travail', scelle=True)
        cls.manifeste = cls.paquet.sortie / 'scelle.json'
        cls.lock_sha256 = cls.document['lock']['sha256']

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.hote = Path(tempfile.mkdtemp(prefix='hote-', dir=self.root))
        self.src = self.hote / 'src'
        self.src.mkdir()
        self.venv = self.hote / 'venv'
        self.cible = self.hote / ('venv-' + self.sha7)

    def preparer(self, manifeste=None, version=None, options=None):
        manifeste = manifeste or self.manifeste
        version = version or self.sha7
        argv = [
            'preparer', '--venv', str(self.venv), '--src', str(self.src),
            '--version', version, '--python', self.paquet.python,
            '--lock', str(self.paquet.lock),
            '--wheelhouse', str(self.paquet.wheelhouse),
            '--manifeste', str(manifeste),
            '--verificateur', str(VERIFICATEUR)]
        if options is not None:
            argv = ['preparer', '--venv', str(self.venv),
                    '--src', str(self.src), '--version', version,
                    '--python', self.paquet.python, *options]
        return bascule.main(argv)

    def test_preparation_scellee_verifiee(self):
        self.assertEqual(0, self.preparer())
        self.assertTrue((self.cible / bascule.MARKER).is_file())
        # Le contrat #5666 accepte le venv APRÈS scellement.
        document = staging._verifier(self.manifeste, self.cible)
        self.assertEqual(staging.FORMAT_SCELLE, document['format'])
        # Invite fixe (N2) et environnement sans bytecode.
        cfg = (self.cible / 'pyvenv.cfg').read_text()
        self.assertIn("prompt = 'ameesh'", cfg)
        self.assertIn('include-system-site-packages = false', cfg)
        # pip refusé, lecture seule exacte partout sous lib/ et bin/.
        pip = subprocess.run([str(self.cible / 'bin' / 'pip')],
                             capture_output=True, text=True)
        self.assertEqual(1, pip.returncode)
        self.assertIn('pip est interdit', pip.stderr)
        for racine in (self.cible / 'lib', self.cible / 'bin'):
            for dossier, _sous, fichiers in os.walk(racine):
                self.assertFalse(stat.S_IMODE(
                    os.lstat(dossier).st_mode) & 0o222, dossier)
                for nom in fichiers:
                    chemin = os.path.join(dossier, nom)
                    if not os.path.islink(chemin):
                        self.assertFalse(
                            stat.S_IMODE(os.lstat(chemin).st_mode) & 0o222,
                            chemin)
        self.assertEqual(0o444, stat.S_IMODE(
            (self.cible / bascule.MARKER).stat().st_mode))
        scellees = [entree for entree in document['installed']['entry_points']
                    if entree['launcher'] == 'scelle']
        self.assertTrue(scellees)
        self.assertTrue(all(entree['name'].startswith('pip')
                            for entree in scellees))

    def test_ecart_octet_ou_mode_refuse(self):
        self.assertEqual(0, self.preparer())
        fichier = staging._dossier_site(self.cible) / 'ameesh' / '__init__.py'
        original = fichier.read_bytes()
        self.assertEqual(0o444, stat.S_IMODE(fichier.stat().st_mode))
        fichier.chmod(0o644)
        fichier.write_bytes(original + b'# modification\n')
        with self.assertRaisesRegex(staging.Ecart, 'arbre installé'):
            staging._verifier(self.manifeste, self.cible)
        fichier.write_bytes(original)
        fichier.chmod(0o444)
        staging._verifier(self.manifeste, self.cible)
        # Un mode élargi seul est un écart, même sans octet changé.
        fichier.chmod(0o644)
        with self.assertRaisesRegex(staging.Ecart, 'arbre installé'):
            staging._verifier(self.manifeste, self.cible)
        fichier.chmod(0o444)
        staging._verifier(self.manifeste, self.cible)
        # Un script d'entrée élargi est refusé par son propre contrat.
        script = self.cible / 'bin' / 'ameesh'
        script.chmod(0o755)
        with self.assertRaisesRegex(staging.Ecart, 'scripts d’entrée'):
            staging._verifier(self.manifeste, self.cible)
        script.chmod(0o555)
        staging._verifier(self.manifeste, self.cible)
        # Les scripts d'activation ne sont couverts que par leur contenu :
        # le scellement exige aussi leur lecture seule (format 3).
        activation = self.cible / 'bin' / 'activate'
        mode = stat.S_IMODE(activation.stat().st_mode)
        self.assertFalse(mode & 0o222)
        activation.chmod(mode | 0o200)
        with self.assertRaisesRegex(staging.Ecart, 'bin/ non scellé'):
            staging._verifier(self.manifeste, self.cible)
        activation.chmod(mode)
        staging._verifier(self.manifeste, self.cible)

    def test_deja_prepare_rejoue_le_verificateur(self):
        self.assertEqual(0, self.preparer())
        # Idempotent : la version complète est revérifiée, sans écart.
        self.assertEqual(0, self.preparer())
        # Un arbre modifié après préparation n'est pas réputé prêt (N
        # auditeur #5711) : le marqueur intact ne suffit pas.
        fichier = staging._dossier_site(self.cible) / 'ameesh' / '__init__.py'
        original = fichier.read_bytes()
        fichier.chmod(0o644)
        fichier.write_bytes(original + b'# modified after preparation\n')
        self.assertEqual(1, self.preparer())
        self.assertTrue((self.cible / bascule.MARKER).is_file())
        fichier.write_bytes(original)
        fichier.chmod(0o444)
        self.assertEqual(0, self.preparer())

    def test_verification_echouee_ne_laisse_rien(self):
        document = json.loads(self.manifeste.read_text())
        document['sha'] = 'f' * 40
        document['installed']['tree_sha256'] = 'f' * 64
        faux = self.hote / 'faux-manifeste.json'
        faux.write_text(json.dumps(document))
        faux.chmod(0o644)
        code = bascule.main([
            'preparer', '--venv', str(self.venv), '--src', str(self.src),
            '--version', 'fffffff', '--python', self.paquet.python,
            '--lock', str(self.paquet.lock),
            '--wheelhouse', str(self.paquet.wheelhouse),
            '--manifeste', str(faux),
            '--verificateur', str(VERIFICATEUR)])
        self.assertEqual(1, code)
        self.assertFalse((self.hote / 'venv-fffffff').exists(),
                         'venv partiel laissé après un refus du vérificateur')
        self.assertFalse((self.hote / 'venv-fffffff' / bascule.MARKER).exists())

    def test_manifeste_non_scelle_et_options_incompletes_refusees(self):
        document = json.loads(self.manifeste.read_text())
        document['format'] = staging.FORMAT
        document.pop('scellement')
        non_scelle = self.hote / 'non-scelle.json'
        non_scelle.write_text(json.dumps(document))
        non_scelle.chmod(0o644)
        with self.assertRaises(SystemExit) as refus:
            self.preparer(manifeste=non_scelle)
        self.assertNotEqual(0, refus.exception.code)
        self.assertFalse(self.cible.exists())
        with self.assertRaises(SystemExit) as refus:
            self.preparer(options=['--lock', str(self.paquet.lock)])
        self.assertNotEqual(0, refus.exception.code)
        self.assertFalse(self.cible.exists())

    def test_activation_apres_preparation_scellee(self):
        self.assertEqual(0, self.preparer())
        # Un venv vivant minimal, comme celui quitte par une bascule.
        subprocess.run([self.paquet.python, '-m', 'venv', '--without-pip',
                        str(self.venv)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(0, bascule.main([
            'activer', '--venv', str(self.venv), '--version', self.sha7]))
        etat = bascule.etat_de(str(self.venv))
        self.assertEqual(self.sha7, etat['vivante'])
        sortie = subprocess.run([str(self.venv / 'bin' / 'ameesh')],
                                capture_output=True, text=True,
                                env=_env_propre())
        self.assertEqual(0, sortie.returncode, sortie.stderr)
        # La version scellée n'a pas été réécrite par l'activation.
        staging._verifier(self.manifeste, self.cible)

    def test_constantes_partagees_sans_derive(self):
        self.assertEqual(staging.MARQUEUR, bascule.MARKER)
        self.assertEqual(staging.INVITE, bascule.INVITE)
        self.assertEqual(staging.FORMAT_SCELLE, bascule.FORMAT_SCELLE)
        self.assertEqual(staging.PIP_REFUS, bascule._PIP_REFUS.encode('utf-8'))
        for version, lock in ((self.sha7, self.lock_sha256),
                              ('0000000', 'a' * 64)):
            self.assertEqual(
                staging.marqueur(version, lock),
                bascule.marqueur_verrouille(version, lock).encode('utf-8'))


if __name__ == '__main__':
    unittest.main()
