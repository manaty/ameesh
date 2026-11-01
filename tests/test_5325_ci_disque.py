# SPDX-License-Identifier: AGPL-3.0-only
"""#5325 : disque des VM de runners de la CI — rapport, alerte et ménage sans root.

Le diagnostic réel (disque d'un hôte, `docker system df -v`, `du`) se fait
depuis un job ; ce fichier tient sans docker ni réseau la partie qui décide :
quels volumes peuvent partir, ce qu'une taille docker vaut, le seuil d'alerte,
et le contrat des étapes ajoutées à `tests.yml` (rapport à chaque job lourd,
ménage en `if: always()` qui ne fait jamais rougir une part).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import types
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "ci_disque", REPO / "scripts" / "ci-disque.py")
disque = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(disque)

GIB = 1024 ** 3
ANCIEN = "2026-10-30T10:00:00Z"
RECENT = "2026-11-01T11:00:00Z"
#: un volume anonyme : docker le nomme par l'identifiant de 64 hexadécimaux
ANONYME = "a" * 64


def volume(nom, cree):
    return {"Name": nom, "CreatedAt": cree}


class VolumeTest(unittest.TestCase):
    """Un volume nommé porte des données : il ne part jamais, même vieux."""

    def test_anonyme_seulement(self):
        self.assertTrue(disque.est_anonyme(ANONYME))
        self.assertFalse(disque.est_anonyme("ameesh-pg-1333-data"))
        self.assertFalse(disque.est_anonyme("A" * 64))
        self.assertFalse(disque.est_anonyme(""))

    def test_vieux_anonyme_part_recent_et_nomme_restent(self):
        maintenant = 1793000000.0
        recent = disque.dt.datetime.fromtimestamp(
            maintenant - 3600, disque.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        vieux, gardes = disque.volumes_a_supprimer(
            [volume(ANONYME, "2026-10-20T10:00:00Z"),
             volume("b" * 64, recent),
             volume("ameesh-pg-data", "2026-10-01T10:00:00Z")],
            maintenant=maintenant, age_heures=6)
        self.assertEqual(vieux, [ANONYME])
        self.assertIn("b" * 64, gardes)
        self.assertIn("ameesh-pg-data", gardes)

    def test_date_illisible_garde_le_volume(self):
        vieux, gardes = disque.volumes_a_supprimer(
            [volume(ANONYME, "pas une date"), volume("c" * 64, None)],
            maintenant=1793000000.0, age_heures=6)
        self.assertEqual(vieux, [])
        self.assertEqual(sorted(gardes), [ANONYME, "c" * 64])

    def test_age_relatif(self):
        maintenant = 1793000000.0
        recent = disque.dt.datetime.fromtimestamp(
            maintenant - 3600, disque.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        vieux, gardes = disque.volumes_a_supprimer(
            [volume("d" * 64, recent)], maintenant=maintenant, age_heures=6)
        self.assertEqual((vieux, gardes), ([], ["d" * 64]))


class OutilsTest(unittest.TestCase):
    def test_taille_docker(self):
        self.assertEqual(disque.taille_docker("Total reclaimed space: 1.234GB"), 1_234_000_000)
        self.assertEqual(disque.taille_docker("Total reclaimed space: 512MB"), 512_000_000)
        self.assertEqual(disque.taille_docker("Total reclaimed space: 0B"), 0)
        self.assertEqual(disque.taille_docker("Deleted Volumes:\nabc\n"), 0)
        self.assertEqual(disque.taille_docker(""), 0)

    def test_seuil(self):
        self.assertEqual(disque.seuil_depuis_env({}), 5.0)
        self.assertEqual(disque.seuil_depuis_env({"AMEESH_CI_DISQUE_MIN_GIB": "3,5"}), 3.5)
        self.assertEqual(disque.seuil_depuis_env({"AMEESH_CI_DISQUE_MIN_GIB": "8"}), 8.0)
        self.assertEqual(disque.seuil_depuis_env({"AMEESH_CI_DISQUE_MIN_GIB": ""}), 5.0)
        self.assertEqual(disque.seuil_depuis_env({"AMEESH_CI_DISQUE_MIN_GIB": "beaucoup"}), 5.0)
        self.assertEqual(disque.seuil_depuis_env({"AMEESH_CI_DISQUE_MIN_GIB": "-2"}), 5.0)

    def test_date(self):
        self.assertEqual(disque._date("1970-01-01T00:00:00Z"), 0.0)
        self.assertAlmostEqual(disque._date("2026-11-01T12:00:00Z"), 1793534400.0, places=0)
        self.assertIsNone(disque._date(""))
        self.assertIsNone(disque._date(None))
        self.assertIsNone(disque._date("hier"))

    def test_espace_comme_df(self):
        faux = types.SimpleNamespace(f_blocks=1000, f_frsize=GIB, f_bfree=500, f_bavail=400)
        with mock.patch.object(disque.os, "statvfs", return_value=faux):
            etat = disque.espace("/")
        self.assertEqual(etat["total"], 1000 * GIB)
        self.assertEqual(etat["libre"], 400 * GIB)
        self.assertAlmostEqual(etat["utilise_pct"], 100.0 * 500 / 900)  # comme df

    def test_commande_absente_ou_trop_lente(self):
        code, _, erreur = disque.commande(["outil-qui-nexiste-pas-5325"])
        self.assertEqual(code, 127)
        self.assertIn("introuvable", erreur)
        code, _, erreur = disque.commande(["sleep", "5"], timeout=0.2)
        self.assertEqual(code, 124)
        self.assertIn("délai", erreur)

    def test_racine_runner(self):
        with mock.patch.dict(os.environ, {"RUNNER_WORKSPACE": "/home/runner/r4/_work/ameesh"}):
            self.assertEqual(disque._racine_runner(), pathlib.Path("/home/runner/r4"))
        with mock.patch.dict(os.environ, {"RUNNER_WORKSPACE": "/home/runner/r4/_work"}):
            self.assertIsNone(disque._racine_runner())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(disque._racine_runner())


class MenageTest(unittest.TestCase):
    """Hors CI rien ne part ; en CI, seules les commandes bornées partent."""

    def test_hors_ci_rien_ne_part(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(disque, "docker_pret") as pret, \
                contextlib.redirect_stdout(io.StringIO()) as sortie:
            self.assertEqual(disque.menage(), 0)
        pret.assert_not_called()
        self.assertIn("GITHUB_ACTIONS", sortie.getvalue())

    def _menage(self, argv):
        appels = []

        def faux_commande(cmd, timeout=120):
            appels.append(list(cmd))
            if cmd[:3] == ["docker", "volume", "ls"]:
                return 0, ANONYME + "\n" + "ameesh-pg-data" + "\n", ""
            if cmd[:3] == ["docker", "volume", "inspect"]:
                return 0, json.dumps([volume(ANONYME, "2026-09-01T10:00:00Z"),
                                      volume("ameesh-pg-data", "2026-03-01T10:00:00Z")]), ""
            if cmd[:3] == ["docker", "volume", "rm"]:
                return 0, ANONYME + "\n", ""
            if cmd[:3] in (["docker", "container", "prune"], ["docker", "image", "prune"]):
                return 0, "Total reclaimed space: 1GB\n", ""
            if cmd[:3] == ["docker", "builder", "prune"]:
                return 0, "Total reclaimed space: 0B\n", ""
            return 0, "", ""

        env = {"GITHUB_ACTIONS": "true", "RUNNER_NAME": "ameesh-ci-1-4"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(disque, "commande", side_effect=faux_commande), \
                mock.patch.object(disque, "docker_pret", return_value=(True, "27.3.1")), \
                mock.patch.object(disque, "espace", return_value={"chemin": "/", "total": 36 * GIB,
                                                                 "libre": 2 * GIB,
                                                                 "utilise_pct": 94.0}), \
                mock.patch.object(disque, "_verrou", return_value=types.SimpleNamespace(close=lambda: None)), \
                mock.patch.object(disque, "_resume"), \
                contextlib.redirect_stdout(io.StringIO()) as sortie:
            code = disque.menage(dry_run="--dry-run" in argv, force=False,
                                 images_inutilisees="--images-inutilisees" in argv)

        self.assertEqual(code, 0)
        conteneurs = [c for c in appels if c[:3] == ["docker", "container", "prune"]]
        images = [c for c in appels if c[:3] == ["docker", "image", "prune"]]
        volumes = [c for c in appels if c[:3] == ["docker", "volume", "rm"]]
        essai = "--dry-run" in argv
        agressif = "--images-inutilisees" in argv
        self.assertEqual(len(conteneurs), 0 if essai else 1)
        self.assertEqual(len(images), 0 if essai else (2 if agressif else 1))
        self.assertEqual(len(volumes), 0 if essai else 1)
        if not essai:
            self.assertIn("until=6h", conteneurs[0])
            self.assertIn("--filter", images[0])
            self.assertNotIn("-a", images[0])
            self.assertEqual(volumes[0][3:], [ANONYME])  # jamais le volume nommé
            if agressif:
                self.assertIn("-a", images[1])
                self.assertIn("until=168h", images[1])
        return sortie.getvalue()

    def test_menage_reel_borne(self):
        self._menage([])

    def test_menage_dry_run(self):
        sortie = self._menage(["--dry-run"])
        self.assertIn("+ docker container prune", sortie)
        self.assertNotIn("+ docker image prune -a", sortie)
        sortie = self._menage(["--dry-run", "--images-inutilisees"])
        self.assertIn("+ docker image prune -a", sortie)

    def test_fichiers_anciens_efface_et_mesure(self):
        with tempfile.TemporaryDirectory() as tmp:
            vieux = pathlib.Path(tmp) / "vieux.log"
            recent = pathlib.Path(tmp) / "recent.log"
            vieux.write_text("x" * 1024, encoding="utf-8")
            recent.write_text("y", encoding="utf-8")
            cinq_jours = 5 * 24 * 3600
            os.utime(vieux, (0, 1_700_000_000 - cinq_jours))
            resultat = disque._fichiers_anciens([pathlib.Path(tmp)], jours=2, dry_run=False)
            self.assertFalse(vieux.exists())
            self.assertTrue(recent.exists())
            self.assertEqual(resultat["fichiers"], 1)
            self.assertEqual(resultat["octets"], 1024)


class RapportTest(unittest.TestCase):
    def test_alerte_sous_le_seuil_et_resume(self):
        etat_disque = {"chemin": "/", "total": 36 * GIB, "libre": 2 * GIB,
                       "utilise_pct": 94.0}
        with tempfile.TemporaryDirectory() as tmp:
            resume = os.path.join(tmp, "resume.md")
            with mock.patch.object(disque, "espace", return_value=etat_disque), \
                    mock.patch.object(disque, "docker_pret", return_value=(False, "docker absent")), \
                    mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": resume,
                                                 "AMEESH_CI_DISQUE_MIN_GIB": "5"}), \
                    contextlib.redirect_stdout(io.StringIO()) as sortie:
                etat = disque.rapport()
            self.assertTrue(etat["alerte"])
            self.assertIn("::warning::", sortie.getvalue())
            contenu = pathlib.Path(resume).read_text(encoding="utf-8")
            self.assertIn("Disque de l'hôte", contenu)
            self.assertIn("2.00 GiB", contenu)

    def test_pas_d_alerte_au_dessus_du_seuil(self):
        etat_disque = {"chemin": "/", "total": 36 * GIB, "libre": 20 * GIB,
                       "utilise_pct": 44.0}
        with mock.patch.object(disque, "espace", return_value=etat_disque), \
                mock.patch.object(disque, "docker_pret", return_value=(False, "docker absent")), \
                mock.patch.dict(os.environ, {}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as sortie:
            etat = disque.rapport()
        self.assertFalse(etat["alerte"])
        self.assertNotIn("::warning::", sortie.getvalue())


WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"


def _job(texte: str, nom: str) -> str:
    import re
    m = re.search(rf"^  {re.escape(nom)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", texte, re.M | re.S)
    assert m, f"job {nom} absent"
    return m.group(1)


class WorkflowTest(unittest.TestCase):
    """Le contrat des étapes dans tests.yml : rapport sur les jobs de tests,
    ménage à toute issue, et jamais une étape qui rougit la part."""

    def setUp(self):
        self.texte = WORKFLOW.read_text(encoding="utf-8")

    def test_les_ancres_sont_definies_une_fois(self):
        self.assertEqual(self.texte.count("&disque_rapport"), 1)
        self.assertEqual(self.texte.count("&disque_menage"), 1)
        self.assertIn("scripts/ci-disque.py rapport", self.texte)
        self.assertIn("scripts/ci-disque.py menage", self.texte)

    def test_les_jobs_lourds_mesurent_et_elaguent(self):
        for nom in ("psycopg", "psql", "fs-class"):
            job = _job(self.texte, nom)
            self.assertIn("*disque_rapport", job, nom)
            self.assertIn("&disque_menage" if nom == "psycopg" else "*disque_menage", job, nom)

    def test_le_plan_mesure_sans_elaguer(self):
        plan = _job(self.texte, "plan")
        self.assertIn("&disque_rapport", plan)
        self.assertNotIn("disque_menage", plan)

    def test_le_menage_ne_fait_jamais_rougir_la_part(self):
        # l'ancre de l'étape porte `if: always()` et `continue-on-error: true`
        debut = self.texte.index("&disque_menage")
        ancre = self.texte[debut:debut + 700]
        self.assertIn("if: always()", ancre)
        self.assertIn("continue-on-error: true", ancre)

    def test_rapport_tolerant(self):
        debut = self.texte.index("&disque_rapport")
        bloc = self.texte[debut:debut + 400]
        self.assertIn("continue-on-error: true", bloc)
        self.assertIn("runner.environment == 'self-hosted'", bloc)


if __name__ == "__main__":
    unittest.main()
