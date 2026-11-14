# SPDX-License-Identifier: AGPL-3.0-only
"""#7380 : cache pip de la CI — un cache par runner, et purge des entrées illisibles.

Constat du 14/11/2026 (run 43599097259, `ameesh-ci-1-8`) : pip a lu une roue
dans `/home/runner/.cache/pip`, commun aux huit runners de l'hôte, et son
empreinte ne correspondait pas. La cause est la restauration tar de
`actions/setup-python` dans ce cache partagé, pendant que d'autres jobs lisent
la même entrée.

Ce fichier tient sans réseau ni hôte :
* le contrat de `tests.yml` : `PIP_CACHE_DIR` par runner, et une reprise sans
  cache sur empreinte refusée, sans jamais désactiver les empreintes ;
* le contrat de `menage-disque.yml` : le cache pip est vérifié et purgé depuis
  les jobs, sans root ;
* la décision de `scripts/ci-pip-cache.py` : quelles entrées du cache HTTP sont
  prouvées corrompues (RECORD en base64 urlsafe, PEP 427, ou hexadécimal
  ancien), ce qui est seulement à vérifier, et ce que la purge enlève.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import pathlib
import tempfile
import unittest
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "ci_pip_cache", REPO / "scripts" / "ci-pip-cache.py")
pip_cache = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pip_cache)

WORKFLOW = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
MENAGE = (REPO / ".github" / "workflows" / "menage-disque.yml").read_text(encoding="utf-8")


def _empreinte(contenu: bytes, hexa: bool = False) -> str:
    digest = hashlib.sha256(contenu).digest()
    if hexa:
        return digest.hex()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _corps_roue(nom: str, fichiers: dict[str, bytes], hexa: bool = False,
                empreinte_forcée: str | None = None) -> bytes:
    """Une roue zip valide, avec un RECORD cohérent (base64 ou hexadécimal)."""
    record = []
    for chemin, contenu in fichiers.items():
        digest = empreinte_forcée or _empreinte(contenu, hexa=hexa)
        record.append("%s,sha256=%s,%d" % (chemin, digest, len(contenu)))
    record.append("%s.dist-info/RECORD,," % nom)
    tous = dict(fichiers)
    tous["%s.dist-info/RECORD" % nom] = ("\n".join(record) + "\n").encode()
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as archive:
        for chemin, contenu in tous.items():
            archive.writestr(chemin, contenu)
    return tampon.getvalue()


def _ecrire_entree(cache: pathlib.Path, corps: bytes, cle: str = "a" * 64) -> tuple:
    """Écrit une entrée http-v2 (corps + métadonnées) et la rend."""
    dossier = cache / "http-v2" / cle[0] / cle[1] / cle[2] / cle[3] / cle[4]
    dossier.mkdir(parents=True, exist_ok=True)
    body = dossier / (cle + ".body")
    meta = dossier / cle
    body.write_bytes(corps)
    meta.write_bytes(b"cc=4,metadata")
    return meta, body


class VerificationTest(unittest.TestCase):
    """Une roue est saine si son zip et les empreintes de son RECORD passent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.racine = pathlib.Path(self.tmp.name)
        self.nom = "cryptography-50.0.2"
        self.fichiers = {
            "%s.dist-info/METADATA" % self.nom: b"Name: cryptography\nVersion: 50.0.2\n",
            "%s.dist-info/WHEEL" % self.nom: b"Wheel-Version: 1.0\n",
            "cryptography/__init__.py": b"# implementation\n",
        }

    def _roue(self, corrompue: bool = False, hexa: bool = False) -> bytes:
        corps = _corps_roue(self.nom, self.fichiers, hexa=hexa)
        if not corrompue:
            return corps
        # Même RECORD, un fichier changé : le zip reste lisible, l'empreinte
        # d'un membre ne correspond plus (le cas exact d'une entrée corrompue).
        tampon = io.BytesIO(corps)
        with zipfile.ZipFile(tampon) as archive:
            membres = {m: archive.read(m) for m in archive.namelist()}
        membres["cryptography/__init__.py"] = b"# autre contenu\n"
        sortie = io.BytesIO()
        with zipfile.ZipFile(sortie, "w") as archive:
            for chemin, contenu in membres.items():
                archive.writestr(chemin, contenu)
        return sortie.getvalue()

    def test_roue_saine_record_base64(self):
        _, body = _ecrire_entree(self.racine / "cache", self._roue())
        nom, raison, prouvee = pip_cache.verifier_roue(body)
        self.assertEqual((nom, raison, prouvee), (self.nom, None, False))

    def test_roue_saine_record_hexadecimal_ancien(self):
        _, body = _ecrire_entree(self.racine / "cache", self._roue(hexa=True))
        nom, raison, prouvee = pip_cache.verifier_roue(body)
        self.assertEqual((nom, raison, prouvee), (self.nom, None, False))

    def test_roue_au_record_incoherent(self):
        _, body = _ecrire_entree(self.racine / "cache", self._roue(corrompue=True))
        nom, raison, prouvee = pip_cache.verifier_roue(body)
        self.assertEqual(nom, self.nom)
        self.assertIn("sha256", raison)
        self.assertTrue(prouvee)

    def test_zip_tronque(self):
        corps = self._roue()[:80]
        _, body = _ecrire_entree(self.racine / "cache", corps)
        _, raison, prouvee = pip_cache.verifier_roue(body)
        self.assertIsNotNone(raison)
        self.assertTrue(prouvee)

    def test_empreinte_record_illisible_gardee(self):
        corps = _corps_roue(self.nom, self.fichiers, empreinte_forcée="%%%")
        _, body = _ecrire_entree(self.racine / "cache", corps)
        _, raison, prouvee = pip_cache.verifier_roue(body)
        self.assertIn("RECORD illisible", raison)
        self.assertFalse(prouvee)

    def test_roue_qui_vendorise_un_dist_info(self):
        # setuptools embarque `setuptools/_vendor/autocommand-…dist-info/` : son
        # RECORD est relatif à son dossier et ne décrit pas la roue. Seul le
        # RECORD à la racine fait foi (régression : 24 roues saines signalées).
        nom = "setuptools-84.2.1"
        fichiers = {
            "%s.dist-info/METADATA" % nom: b"Name: setuptools\n",
            "setuptools/__init__.py": b"# setuptools\n",
            "setuptools/_vendor/autocommand-2.2.2.dist-info/RECORD":
                b"autocommand/__init__.py,sha256=abc,1\n",
            "setuptools/_vendor/autocommand/__init__.py": b"x\n",
        }
        corps = _corps_roue(nom, fichiers)
        _, body = _ecrire_entree(self.racine / "cache", corps)
        nom_lu, raison, prouvee = pip_cache.verifier_roue(body)
        self.assertEqual((nom_lu, raison, prouvee), (nom, None, False))

    def test_corps_qui_n_est_pas_une_roue(self):
        _, body = _ecrire_entree(self.racine / "cache", b"<html>index</html>")
        nom, raison, prouvee = pip_cache.verifier_roue(body)
        self.assertEqual((nom, raison, prouvee), ("", None, False))


class DiagnosticPurgeTest(unittest.TestCase):
    """La purge n'enlève que les entrées prouvées, corps et métadonnées."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = pathlib.Path(self.tmp.name) / "cache"
        self.nom = "cryptography-50.0.2"
        self.fichiers = {
            "%s.dist-info/METADATA" % self.nom: b"Name: cryptography\n",
            "cryptography/__init__.py": b"# code\n",
        }

    def _entrees(self):
        sain = _ecrire_entree(self.cache, _corps_roue(self.nom, self.fichiers),
                              cle="b" * 64)
        corrompu = _ecrire_entree(self.cache, b"PK\x03\x04" + b"tronque",
                                  cle="c" * 64)
        neutre = _ecrire_entree(self.cache, b"<html>index</html>", cle="d" * 64)
        douteux = _ecrire_entree(
            self.cache,
            _corps_roue(self.nom, self.fichiers, empreinte_forcée="%%%"),
            cle="e" * 64)
        return sain, corrompu, neutre, douteux

    def test_diagnostic_nomme_l_entree_corrompue(self):
        sain, corrompu, neutre, douteux = self._entrees()
        etat = pip_cache.diagnostic([("cache de test", self.cache)])
        self.assertEqual(etat["corps"], 4)
        self.assertEqual(etat["roues"], 2)
        chemins = [entree["chemin"] for entree in etat["illisibles"]]
        self.assertIn(corrompu[1], chemins)
        self.assertIn(douteux[1], chemins)
        corrompues = [e for e in etat["illisibles"] if e["corrompue"]]
        self.assertEqual([e["chemin"] for e in corrompues], [corrompu[1]])

    def test_purge_dry_run_ne_supprime_rien(self):
        sain, corrompu, neutre, douteux = self._entrees()
        resultat = pip_cache.purger([("cache de test", self.cache)], dry_run=True)
        self.assertEqual(resultat["supprimees"], 0)
        for meta, body in (sain, corrompu, neutre, douteux):
            self.assertTrue(body.exists(), body)
            self.assertTrue(meta.exists(), meta)

    def test_purge_enleve_le_corps_et_la_metadonnee(self):
        sain, corrompu, neutre, douteux = self._entrees()
        resultat = pip_cache.purger([("cache de test", self.cache)])
        self.assertEqual(resultat["supprimees"], 1)
        self.assertEqual(resultat["a_verifier"], 1)
        self.assertFalse(corrompu[1].exists())
        self.assertFalse(corrompu[0].exists())
        self.assertTrue(sain[1].exists())
        self.assertTrue(neutre[1].exists())
        self.assertTrue(douteux[1].exists())


class CachesTest(unittest.TestCase):
    """Le job voit son cache, l'historique partagé, sans doublon."""

    def test_pip_cache_dir_et_partage(self):
        with tempfile.TemporaryDirectory() as tmp:
            caches = pip_cache.caches_pip({"PIP_CACHE_DIR": tmp})
            chemins = [str(chemin) for _, chemin in caches]
            self.assertEqual(chemins[0], tmp)
            self.assertIn(str(pip_cache.CACHE_PARTAGE), chemins)
            self.assertEqual(len(chemins), len(set(chemins)))

    def test_meme_chemin_une_seule_fois(self):
        with tempfile.TemporaryDirectory() as tmp:
            caches = pip_cache.caches_pip({"PIP_CACHE_DIR": tmp})
            self.assertEqual(sum(1 for _, chemin in caches if str(chemin) == tmp), 1)


class WorkflowTest(unittest.TestCase):
    """Le correctif est dans tests.yml, pas dans un réglage d'hôte invisible."""

    def test_cache_pip_par_runner(self):
        self.assertIn('echo "PIP_CACHE_DIR=$RUNNER_WORKSPACE/../pip-cache"', WORKFLOW)
        # Le dossier doit exister avant setup-python : son post-job sauvegarde
        # ce chemin et rougit la part quand il manque (run 43603076812, parts
        # dont le venv vient du cache et qui n'appellent donc jamais pip).
        self.assertIn('mkdir -p "$RUNNER_WORKSPACE/../pip-cache"', WORKFLOW)
        self.assertLess(WORKFLOW.index('mkdir -p "$RUNNER_WORKSPACE/../pip-cache"'),
                        WORKFLOW.index("cache: pip"))
        self.assertNotIn("PIP_CACHE_DIR=/home/runner/.cache/pip", WORKFLOW)
        self.assertNotIn("PIP_CACHE_DIR=$HOME/.cache/pip", WORKFLOW)
        self.assertLess(WORKFLOW.index("PIP_CACHE_DIR=$RUNNER_WORKSPACE"),
                        WORKFLOW.index("cache: pip"))

    def test_reprise_sans_cache_sur_empreinte_refusee(self):
        debut = WORKFLOW.index("name: dépendances (pilote psycopg)")
        fin = WORKFLOW.index("- &base", debut)
        corps = WORKFLOW[debut:fin]
        self.assertIn("DO NOT MATCH THE HASHES", corps)
        self.assertIn(".venv-ci/bin/pip cache purge", corps)
        self.assertIn("--no-cache-dir", corps)
        self.assertEqual(corps.count("--no-cache-dir"), 1)
        # Les empreintes restent vérifiées : aucune option qui les éteint.
        for interdit in ("--no-verify", "--trusted-host", "PIP_NO_VERIFY",
                         "trusted-host="):
            self.assertNotIn(interdit, WORKFLOW)
        # Le premier essai, lui, passe par le cache du runner.
        self.assertLess(corps.index('.venv-ci/bin/pip install "${paquets[@]}"'),
                        corps.index("--no-cache-dir"))

    def test_menage_verifie_et_purge_le_cache_pip(self):
        self.assertIn("scripts/ci-pip-cache.py diagnostic", MENAGE)
        self.assertIn("scripts/ci-pip-cache.py purge", MENAGE)
        self.assertIn("pip-cache:", MENAGE)
        self.assertIn("if: ${{ !inputs.pip-cache }}", MENAGE)


if __name__ == "__main__":
    unittest.main()
