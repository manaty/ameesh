# SPDX-License-Identifier: AGPL-3.0-only
"""#5635 : borner les caches `_work/_update` des runners entre deux cycles.

Le cache d'un auto-update est l'extraction du paquet (~700 MiB) que le
Runner.Listener laisse sous `_work/_update` après avoir copié `bin.<version>`
et `externals.<version>` dans la racine. Il n'est plus lu après la bascule des
liens `bin`/`externals` ; le runner le supprime seulement au début du cycle
suivant. Ces tests tiennent la politique sans VM ni runner réel :

* cache inactif éligible → retiré, `bin.<v>`/`externals.<v>` et les liens
  courants conservés ;
* cache d'une version supérieure → mise à jour en attente, préservé ;
* updater vivant, Worker actif, unité réactivée entre deux contrôles → préservés ;
* lien symbolique en tête, lien sortant, chemin inattendu, référence depuis
  `bin*`/`externals*` → refusés sans rien supprimer ;
* deux cycles de mise à jour simulés (paquet, extraction, versions) avec la
  taille libérée mesurée.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from .test_5325_ci_disque import disque


def deps(version):
    return json.dumps({"targets": {".NETCoreApp,Version=v8.0": {
        "Runner.Listener/%s" % version: {}}}})


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "runner"
        self.root = self.home / "r1"
        self.root.mkdir(parents=True)
        (self.root / ".runner").write_text("{}", encoding="utf-8")
        self.up = self.root / "_work" / "_update"

    def ecrire_bin(self, dossier, version):
        dossier.mkdir(parents=True, exist_ok=True)
        (dossier / "Runner.Listener.deps.json").write_text(
            deps(version), encoding="utf-8")
        (dossier / "Runner.Listener.dll").write_bytes(
            ("dll-" + version).encode("ascii"))

    def installer(self, version, root=None):
        """Copie versionnée + liens bin/externals, comme le script du runner."""
        root = self.root if root is None else root
        racine_bin = root / ("bin." + version)
        self.ecrire_bin(racine_bin, version)
        racine_externals = root / ("externals." + version)
        racine_externals.mkdir(parents=True, exist_ok=True)
        for nom, cible in (("bin", racine_bin), ("externals", racine_externals)):
            lien = root / nom
            if lien.is_symlink() or lien.is_file():
                lien.unlink()
            elif lien.is_dir():
                shutil.rmtree(lien)
            lien.symlink_to(cible)

    def cache(self, version, root=None, archive=False, taille=0):
        """Extraction sous _work/_update, avec les liens internes du paquet."""
        root = self.root if root is None else root
        update = root / "_work" / "_update"
        if update.exists():
            shutil.rmtree(update)
        self.ecrire_bin(update / "bin", version)
        noeud = update / "externals" / "node24"
        (noeud / "lib" / "node_modules" / "npm" / "bin").mkdir(parents=True)
        (noeud / "bin").mkdir(parents=True)
        (noeud / "bin" / "npm").symlink_to(
            "../lib/node_modules/npm/bin/npm-cli.js")
        (update / "run.sh").write_text("#!/bin/bash\n", encoding="utf-8")
        (update / "safe_sleep.sh").write_text("", encoding="utf-8")
        if archive:
            (update / "runner1.tar.gz").write_bytes(b"paquet" * 512)
        if taille:
            (update / "bin" / "blob").write_bytes(b"x" * taille)
        return update

    def decision(self, **options):
        options.setdefault("processus", [])
        options.setdefault("maintenant", time.time())
        resultat = disque._caches_update(self.root, **options)
        par_root = {Path(element["root"]).name: element
                    for element in resultat["caches"]}
        return resultat, par_root

    def sans_processus(self):
        return mock.patch.object(disque, "_processus", return_value=[])

    def avec_processus(self, processus):
        """Recontrôle de suppression isolé des processus ambiants du banc.

        Le banc de CI exécute le test depuis une vraie racine de runner : son
        Worker, hors de `self.home`, serait « attribution impossible » et
        préserverait le cache au recontrôle. Le test fournit donc le même
        instantané aux deux contrôles ; la production, elle, relit /proc.
        """
        return mock.patch.object(disque, "_processus",
                                 return_value=processus)


class CacheInactifTest(BaseTest):
    def test_cache_installe_est_retire_et_conserve_les_versions(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2", archive=True, taille=4096)
        self.assertTrue((update / "externals/node24/bin/npm").is_symlink())
        taille = disque._taille_arbre(update)
        with self.sans_processus():
            resultat, par_root = self.decision()
        self.assertEqual(resultat["retires"], 1)
        self.assertEqual(resultat["libere"], taille)
        self.assertFalse(update.exists())
        self.assertTrue((self.root / "bin.2.338.2").is_dir())
        self.assertTrue((self.root / "externals.2.338.2").is_dir())
        self.assertEqual((self.root / "bin").readlink(),
                         self.root / "bin.2.338.2")
        self.assertEqual(par_root["r1"]["verdict"], "retiré")
        self.assertIn("retiré", resultat["sortie"])

    def test_cache_d_une_version_ancienne_est_retire(self):
        self.installer("2.338.2")
        update = self.cache("2.338.0")
        with self.sans_processus():
            resultat, _ = self.decision()
        self.assertEqual(resultat["retires"], 1)
        self.assertFalse(update.exists())

    def test_dry_run_ne_supprime_rien(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        with self.sans_processus():
            resultat, par_root = self.decision(dry_run=True)
        self.assertTrue(update.exists())
        self.assertEqual(resultat["retires"], 1)
        self.assertEqual(resultat["libere"], 0)
        self.assertEqual(par_root["r1"]["verdict"], "serait retiré")
        self.assertIn("serait retiré", resultat["sortie"])

    def test_deux_passes_sont_idempotentes(self):
        self.installer("2.338.2")
        self.cache("2.338.2")
        with self.sans_processus():
            self.assertEqual(self.decision()[0]["retires"], 1)
            second = self.decision()[0]
        self.assertEqual(second["retires"], 0)
        self.assertIn("aucun cache", second["sortie"])

    def test_huit_slots_liberent_la_marge_mesuree(self):
        # 8 slots × 1 MiB : la même arithmétique que 8 × ~699000 KiB/hôte.
        for numero in range(1, 9):
            root = self.home / ("r%d" % numero)
            root.mkdir(exist_ok=True)
            (root / ".runner").write_text("{}", encoding="utf-8")
            self.installer("2.338.2", root=root)
            self.cache("2.338.2", root=root, taille=1024 * 1024)
        with self.sans_processus():
            resultat, _ = self.decision()
        self.assertEqual(resultat["retires"], 8)
        self.assertGreaterEqual(resultat["libere"], 8 * 1024 * 1024)
        for numero in range(1, 9):
            self.assertTrue((self.home / ("r%d" % numero) / "bin").exists())


class MiseAJourEnAttenteTest(BaseTest):
    def test_version_superieure_preservee_meme_sans_processus(self):
        self.installer("2.338.2")
        update = self.cache("2.339.0")
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertEqual(resultat["retires"], 0)
        self.assertEqual(par_root["r1"]["verdict"], "préservé")
        self.assertIn("mise à jour en attente", par_root["r1"]["raison"])

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "lecture de /proc (CI Linux)")
    def test_updater_vivant_protege_puis_liberation(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        processus = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], cwd=update)
        self.addCleanup(lambda: (processus.terminate(), processus.wait()))
        maintenant = time.time()
        # Ne garder que le témoin : le Worker du banc, hors de `self.home`,
        # serait « attribution impossible » et masquerait la raison cherchée.
        instantane = [vu for vu in disque._processus(maintenant=maintenant)
                      if vu["pid"] == processus.pid]
        resultat, par_root = self.decision(processus=instantane,
                                           maintenant=maintenant)
        self.assertTrue(update.exists())
        self.assertEqual(resultat["retires"], 0)
        self.assertIn("updater vivant", par_root["r1"]["raison"])
        processus.terminate()
        processus.wait()
        with self.sans_processus():
            resultat, _ = self.decision()
        self.assertEqual(resultat["retires"], 1)
        self.assertFalse(update.exists())

    def test_worker_actif_protege(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        maintenant = time.time()
        # cwd réels de la flotte : racine, bin, bin.<version>, workspace.
        for cwd in (self.root,
                    self.root / "bin",
                    self.root / "bin.2.338.2",
                    self.root / "_work" / "ameesh"):
            instantane = [{"pid": 42, "comm": "Runner.Worker",
                           "cwd": cwd, "fds": [], "cmdline": [],
                           "depart": maintenant - 60, "etat": "R"}]
            resultat, par_root = self.decision(processus=instantane,
                                               maintenant=maintenant)
            self.assertTrue(update.exists(), str(cwd))
            self.assertEqual(par_root["r1"]["verdict"], "préservé", str(cwd))
            self.assertIn("Worker actif", par_root["r1"]["raison"])

    def test_worker_d_une_autre_racine_ne_protege_pas(self):
        autre = self.home / "r2"
        autre.mkdir()
        (autre / ".runner").write_text("{}", encoding="utf-8")
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        maintenant = time.time()
        instantane = [{"pid": 43, "comm": "Runner.Worker",
                       "cwd": autre / "bin", "fds": [], "cmdline": [],
                       "depart": maintenant - 60, "etat": "R"}]
        with self.avec_processus(instantane):
            resultat, _ = self.decision(processus=instantane,
                                        maintenant=maintenant)
        self.assertFalse(update.exists())
        self.assertEqual(resultat["retires"], 1)

    def test_worker_attribution_incertaine_preserve(self):
        # cwd = home ne désigne aucune racine : l'attribution est impossible.
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        maintenant = time.time()
        instantane = [{"pid": 44, "comm": "Runner.Worker",
                       "cwd": self.home, "fds": [], "cmdline": [],
                       "depart": maintenant - 60, "etat": "R"}]
        resultat, par_root = self.decision(processus=instantane,
                                           maintenant=maintenant)
        self.assertTrue(update.exists())
        self.assertIn("Worker actif", par_root["r1"]["raison"])

    def test_informations_illisibles_preservent(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        maintenant = time.time()
        for comm, complement, motif in (
                ("Runner.Worker", {"cwd": None}, "cwd illisible"),
                ("Runner.Worker", {"fds": None}, "Worker actif"),
                ("Runner.Worker", {"cmdline": None}, "Worker actif"),
                ("bash", {"cmdline": None}, "shell illisible"),
                ("bash", {"fds": None}, "descripteurs d'un shell")):
            instantane = [dict({"pid": 45, "comm": comm,
                                "cwd": self.root / "bin", "fds": [],
                                "cmdline": [], "depart": maintenant - 60,
                                "etat": "R"}, **complement)]
            resultat, par_root = self.decision(processus=instantane,
                                               maintenant=maintenant)
            self.assertTrue(update.exists(), (comm, motif))
            self.assertEqual(par_root["r1"]["verdict"], "préservé")
            self.assertIn(motif, par_root["r1"]["raison"])

    def test_worker_cmdline_attribue_si_cwd_illisible(self):
        autre = self.home / "r2"
        autre.mkdir()
        (autre / ".runner").write_text("{}", encoding="utf-8")
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        maintenant = time.time()
        instantane = [{"pid": 47, "comm": "Runner.Worker", "cwd": None,
                       "fds": [],
                       "cmdline": [str(self.root / "bin" / "Runner.Worker")],
                       "depart": maintenant - 60, "etat": "R"}]
        resultat, par_root = self.decision(processus=instantane,
                                           maintenant=maintenant)
        self.assertTrue(update.exists())
        self.assertIn("Worker actif", par_root["r1"]["raison"])
        instantane[0]["cmdline"] = [str(autre / "bin" / "Runner.Worker")]
        with self.avec_processus(instantane):
            resultat, _ = self.decision(processus=instantane,
                                        maintenant=maintenant)
        self.assertFalse(update.exists())
        self.assertEqual(resultat["retires"], 1)

    def test_zombie_n_empeche_pas_le_retrait(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        maintenant = time.time()
        instantane = [{"pid": 46, "comm": "", "cwd": None, "fds": None,
                       "cmdline": None, "depart": None, "etat": "Z"}]
        with self.avec_processus(instantane):
            resultat, _ = self.decision(processus=instantane,
                                        maintenant=maintenant)
        self.assertFalse(update.exists())
        self.assertEqual(resultat["retires"], 1)

    def test_script_de_mise_a_jour_vivant_protege(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        maintenant = time.time()
        instantane = [{"pid": 7, "comm": "bash", "cwd": self.root,
                       "fds": [],
                       "cmdline": ["/bin/bash",
                                   str(self.root / "_work" / "_update.sh")],
                       "depart": maintenant - 3600}]
        resultat, par_root = self.decision(processus=instantane,
                                           maintenant=maintenant)
        self.assertTrue(update.exists())
        self.assertIn("script de mise à jour vivant", par_root["r1"]["raison"])


class RedemarrageTest(BaseTest):
    def test_unite_reactivee_entre_deux_controles_preservee(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        maintenant = time.time()
        before, par_root = self.decision(processus=[], maintenant=maintenant,
                                         dry_run=True)
        self.assertEqual(par_root["r1"]["verdict"], "serait retiré")
        instantane = [{"pid": 7, "comm": "Runner.Listener", "cwd": self.root,
                       "fds": [], "cmdline": [], "depart": maintenant - 60}]
        resultat, par_root = self.decision(processus=instantane,
                                           maintenant=maintenant)
        self.assertTrue(update.exists())
        self.assertIn("unité réactivée", par_root["r1"]["raison"])
        instantane[0]["depart"] = maintenant - 3600
        with self.sans_processus():
            resultat, _ = self.decision(processus=instantane,
                                        maintenant=maintenant)
        self.assertEqual(resultat["retires"], 1)
        self.assertFalse(update.exists())

    def test_controle_avant_suppression_revoit_l_unite_reactivee(self):
        # Le premier contrôle voit le cache éligible ; entre les deux, l'unité
        # redémarre. Le contrôle juste avant la suppression doit préserver.
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        maintenant = time.time()
        recent = [{"pid": 9, "comm": "Runner.Listener", "cwd": self.root,
                   "fds": [], "cmdline": [], "depart": maintenant - 30}]
        with mock.patch.object(disque, "_processus", return_value=recent):
            resultat, par_root = self.decision(processus=[],
                                               maintenant=maintenant)
        self.assertTrue(update.exists())
        self.assertEqual(resultat["retires"], 0)
        self.assertIn("unité réactivée", par_root["r1"]["raison"])

    def test_demarrage_ou_descripteurs_illisibles_preservent(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        maintenant = time.time()
        for complement, motif in (
                ({"depart": None, "fds": []}, "démarrage du Listener illisible"),
                ({"depart": maintenant - 3600, "fds": None},
                 "descripteurs du Listener illisibles")):
            instantane = [dict({"pid": 7, "comm": "Runner.Listener",
                                "cwd": self.root, "cmdline": []}, **complement)]
            resultat, par_root = self.decision(processus=instantane,
                                               maintenant=maintenant)
            self.assertTrue(update.exists(), motif)
            self.assertIn(motif, par_root["r1"]["raison"])


class RefusTest(BaseTest):
    def test_lien_en_tete_refuse(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        cible = Path(self.tmp.name) / "dehors"
        cible.write_text("preuve", encoding="utf-8")
        (update / "evil").symlink_to(cible)
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertEqual(cible.read_text(encoding="utf-8"), "preuve")
        self.assertEqual(par_root["r1"]["verdict"], "refusé")
        self.assertIn("lien symbolique en tête", par_root["r1"]["raison"])

    def test_lien_sortant_refuse_et_cible_intacte(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        dehors = Path(self.tmp.name) / "dehors.txt"
        dehors.write_text("preuve", encoding="utf-8")
        (update / "bin" / "escape").symlink_to(dehors)
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertTrue(dehors.exists())
        self.assertIn("lien absolu", par_root["r1"]["raison"])

    def test_lien_relative_sortante_refusee(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        (update / "bin" / "escape").symlink_to("../../../etc/passwd")
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertIn("lien sortant", par_root["r1"]["raison"])

    def test_entree_inattendue_refusee(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        (update / "payload").write_text("inattendu", encoding="utf-8")
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertIn("entrée inattendue", par_root["r1"]["raison"])

    def test_racine_sans_marqueur_runner_refusee(self):
        self.installer("2.338.2")
        self.cache("2.338.2")
        autre = self.home / "pas-un-runner"
        autre.mkdir()
        self.installer("2.338.2", root=autre)
        self.cache("2.338.2", root=autre)
        with self.sans_processus():
            resultat, par_root = self.decision()
        self.assertEqual(resultat["retires"], 1)
        self.assertEqual(par_root["r1"]["verdict"], "retiré")
        self.assertEqual(par_root["pas-un-runner"]["verdict"], "refusé")
        self.assertIn("pas une racine de runner",
                      par_root["pas-un-runner"]["raison"])
        self.assertTrue((autre / "_work" / "_update").exists())

    def test_update_lui_meme_lien_refuse(self):
        self.installer("2.338.2")
        dehors = Path(self.tmp.name) / "dehors"
        dehors.mkdir()
        (self.root / "_work").mkdir(exist_ok=True)
        (self.root / "_work" / "_update").symlink_to(dehors)
        resultat, par_root = self.decision()
        self.assertTrue(dehors.exists())
        self.assertEqual(par_root["r1"]["verdict"], "refusé")
        self.assertIn("lien symbolique", par_root["r1"]["raison"])

    def test_reference_depuis_bin_ou_version_refusee(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        (self.root / "bin").unlink()
        (self.root / "bin").symlink_to(update / "bin")
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertIn("référencé par bin", par_root["r1"]["raison"])

        self.installer("2.338.1")
        shutil.rmtree(self.root / "bin.2.338.2")
        (self.root / "bin.2.338.2").symlink_to(update / "bin")
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertIn("référencé par bin.2.338.2",
                      par_root["r1"]["raison"])


class InstallationTest(BaseTest):
    def test_version_courante_ou_cache_illisible_preserve(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        (self.root / "bin.2.338.2" / "Runner.Listener.deps.json").unlink()
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertIn("version courante illisible", par_root["r1"]["raison"])
        self.installer("2.338.2")
        (update / "bin" / "Runner.Listener.deps.json").unlink()
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertIn("version du cache illisible", par_root["r1"]["raison"])

    def test_installation_incomplete_ou_mixte_preserve(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        (self.root / "externals").unlink()
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertIn("bin/externals", par_root["r1"]["raison"])
        (self.root / "externals").symlink_to(self.root / "externals.2.338.2")
        (self.root / "bin").unlink()
        shutil.copytree(self.root / "bin.2.338.2", self.root / "bin")
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertIn("mixtes", par_root["r1"]["raison"])

    def test_installation_reelle_sans_liens_versionnes(self):
        # Installation neuve : bin et externals sont des dossiers réels, sans
        # bin.<v> ; la copie installée prouve la version par son empreinte.
        self.ecrire_bin(self.root / "bin", "2.338.2")
        (self.root / "externals").mkdir()
        update = self.cache("2.338.2")
        with self.sans_processus():
            resultat, par_root = self.decision()
        self.assertEqual(resultat["retires"], 1)
        self.assertFalse(update.exists())
        self.assertTrue((self.root / "bin" / "Runner.Listener.deps.json").exists())
        self.assertTrue((self.root / "externals").is_dir())

    def test_contenu_different_preserve(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        (update / "bin" / "Runner.Listener.deps.json").write_text(
            json.dumps({"targets": {"autre": {"Runner.Listener/2.338.2": {}}}}),
            encoding="utf-8")
        resultat, par_root = self.decision()
        self.assertTrue(update.exists())
        self.assertIn("contenu du cache différent", par_root["r1"]["raison"])


class DeuxCyclesTest(BaseTest):
    def test_paquet_extraction_et_versions_deux_fois(self):
        self.installer("2.338.0")
        update = self.cache("2.338.2", archive=True, taille=2048)
        taille = disque._taille_arbre(update)
        resultat, par_root = self.decision()
        self.assertEqual(par_root["r1"]["verdict"], "préservé")
        self.assertIn("mise à jour en attente", par_root["r1"]["raison"])
        self.installer("2.338.2")
        with self.sans_processus():
            resultat, _ = self.decision()
        self.assertEqual(resultat["libere"], taille)
        self.assertFalse(update.exists())
        self.assertTrue((self.root / "bin.2.338.0").is_dir())
        self.assertTrue((self.root / "bin.2.338.2").is_dir())

        self.cache("2.338.4", archive=True, taille=4096)
        resultat, par_root = self.decision()
        self.assertIn("mise à jour en attente", par_root["r1"]["raison"])
        self.installer("2.338.4")
        with self.sans_processus():
            resultat, _ = self.decision()
        self.assertEqual(resultat["retires"], 1)
        self.assertFalse(update.exists())
        self.assertTrue((self.root / "bin.2.338.2").is_dir())
        self.assertTrue((self.root / "bin.2.338.4").is_dir())
        self.assertTrue((self.root / "externals.2.338.4").is_dir())

    def test_arborescence_trop_grande_refusee(self):
        self.installer("2.338.2")
        self.cache("2.338.2")
        with mock.patch.object(disque, "MAX_ENTREES_UPDATE", 1):
            resultat, par_root = self.decision()
        self.assertEqual(par_root["r1"]["verdict"], "refusé")
        self.assertIn("arborescence trop grande", par_root["r1"]["raison"])


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "lecture de /proc (CI Linux)")
class ProcessusTest(BaseTest):
    def test_lecture_proc_limitee_aux_champs_utiles(self):
        faux = Path(self.tmp.name) / "proc"
        faux.mkdir()
        (faux / "uptime").write_text("1000.00 0.00\n", encoding="utf-8")
        pid = faux / "123"
        (pid / "fd").mkdir(parents=True)
        (pid / "comm").write_text("Runner.Listener\n", encoding="utf-8")
        (pid / "cwd").symlink_to(self.root)
        descripteur = self.root / "descripteur"
        descripteur.write_text("x", encoding="utf-8")
        (pid / "fd" / "1").symlink_to(descripteur)
        (pid / "cmdline").write_bytes(
            b"/bin/bash\0" + str(self.root / "_work" / "_update.sh").encode()
            + b"\0")
        # champ 22 (starttime) = 500 ticks, horloge 100 Hz, uptime 1000 s.
        (pid / "stat").write_text(
            "123 (Runner.Listener) S " + "0 " * 18 + "500 0\n",
            encoding="utf-8")
        maintenant = time.time()
        vus = disque._processus(faux, maintenant=maintenant)
        self.assertEqual(len(vus), 1)
        vu = vus[0]
        self.assertEqual(vu["pid"], 123)
        self.assertEqual(vu["comm"], "Runner.Listener")
        self.assertEqual(vu["cwd"], self.root)
        self.assertEqual(vu["fds"], [descripteur])
        self.assertEqual(vu["cmdline"][1],
                         str(self.root / "_work" / "_update.sh"))
        self.assertAlmostEqual(vu["depart"], maintenant - 995, delta=1)
        self.assertEqual(vu["etat"], "S")

    def test_worker_reel_cwd_bin_et_bin_versionne_protege(self):
        self.installer("2.338.2")
        update = self.cache("2.338.2")
        faux = Path(self.tmp.name) / "proc-bin"
        faux.mkdir()
        (faux / "uptime").write_text("1000.00 0.00\n", encoding="utf-8")
        for pid, cible in ((201, self.root / "bin"),
                           (202, self.root / "bin.2.338.2")):
            entree = faux / str(pid)
            (entree / "fd").mkdir(parents=True)
            (entree / "comm").write_text("Runner.Worker\n", encoding="utf-8")
            (entree / "cwd").symlink_to(cible)
            (entree / "cmdline").write_bytes(
                b"/opt/runner/Runner.Worker\0")
            (entree / "stat").write_text(
                "%d (Runner.Worker) S " % pid + "0 " * 18 + "500 0\n",
                encoding="utf-8")
        maintenant = time.time()
        instantane = disque._processus(faux, maintenant=maintenant)
        resultat, par_root = self.decision(processus=instantane,
                                           maintenant=maintenant)
        self.assertTrue(update.exists())
        self.assertIn("Worker actif", par_root["r1"]["raison"])


class MenageIntegrationTest(BaseTest):
    def test_menage_execute_la_politique_update(self):
        vide = {"etape": "test", "libere": 0, "sortie": ""}
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
                mock.patch.object(disque, "docker_pret",
                                  return_value=(False, "absent")), \
                mock.patch.object(disque, "_verrou",
                                  return_value=types.SimpleNamespace(close=lambda: None)), \
                mock.patch.object(disque, "espace",
                                  return_value={"libre": 10, "total": 10,
                                                "utilise_pct": 0}), \
                mock.patch.object(disque, "_temp", return_value=vide), \
                mock.patch.object(disque, "_diag", return_value=vide), \
                mock.patch.object(disque, "_tmp", return_value=vide), \
                mock.patch.object(disque, "_updates",
                                  return_value=dict(vide, caches=[])) as updates, \
                mock.patch.object(disque, "_resume_menage"):
            self.assertEqual(disque.menage(), 0)
        updates.assert_called_once_with(False)

    def test_updates_sans_workspace_est_une_etape_sautee(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            resultat = disque._updates(False)
        self.assertEqual(resultat["libere"], 0)
        self.assertIn("RUNNER_WORKSPACE absent", resultat["sortie"])


if __name__ == "__main__":
    unittest.main()
