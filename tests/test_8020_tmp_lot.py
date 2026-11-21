# SPDX-License-Identifier: AGPL-3.0-only
"""#8020 : un dossier temporaire par lot (`TMPDIR`), nettoyé à la fin du lot,
et balayage des dossiers `ameesh-*` / `nexlink-*` abandonnés de `/tmp` et
`/dev/shm`.

Les chemins vivent sous la racine gérée de l'agent (sur disque, L162), pas
littéralement dans `/tmp` : c'est l'écart déclaré de ce lot. Le balayage, lui,
ne touche QUE des enfants directs de `/tmp` et `/dev/shm`.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import housekeeping as menage
from ameesh import platform


class _Cfg:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.host = "pc"
        self.dsn = ""
        self.schema = "public"


def _write(path: str, size: int = 10) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"z" * size)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-8020-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.base], check=False)
        self.cfg = _Cfg(os.path.join(self.base, "state"))
        self.pol = menage.Policy()
        self.agent_dir = os.path.join(self.cfg.state_dir, ".menage", "tmp", "a1")

    def lot_dir(self, lot) -> str:
        return os.path.join(self.agent_dir, "ameesh-lot-%s" % lot)


class NomDuDossierTest(unittest.TestCase):
    def test_nom_numerique_et_refus_des_autres(self):
        self.assertEqual(menage.lot_tmp_name(8020), "ameesh-lot-8020")
        self.assertEqual(menage.lot_tmp_name("#8020"), "ameesh-lot-8020")
        self.assertEqual(menage.lot_tmp_name(" 12 "), "ameesh-lot-12")
        for value in (None, "", "lot/8020", "é", "12a"):
            with self.subTest(value=value):
                self.assertEqual(menage.lot_tmp_name(value), "")


class TmpDirParLotTest(_Base):
    def test_tmpdir_pointe_le_sous_dossier_du_lot(self):
        env = menage.turn_env(self.cfg, self.pol, "a1", {}, lot=8020)
        attendu = self.lot_dir(8020)
        self.assertEqual(env["TMPDIR"], attendu)
        self.assertEqual(env["TMP"], attendu)
        self.assertEqual(env["TEMP"], attendu)
        self.assertEqual(env["AMEESH_TMPDIR"], attendu)
        self.assertTrue(os.path.isdir(attendu))
        self.assertEqual(os.stat(attendu).st_mode & 0o777, 0o700)

    def test_deux_lots_ont_deux_dossiers_et_le_dossier_est_garde(self):
        premier = menage.turn_env(self.cfg, self.pol, "a1", {}, lot=10)["TMPDIR"]
        _write(os.path.join(premier, "garde"))
        # un second tour du même lot retrouve le même dossier et son contenu
        encore = menage.turn_env(self.cfg, self.pol, "a1", {}, lot=10)["TMPDIR"]
        self.assertEqual(encore, premier)
        self.assertTrue(os.path.isfile(os.path.join(encore, "garde")))
        # un autre lot a le sien, vide
        autre = menage.turn_env(self.cfg, self.pol, "a1", {}, lot=11)["TMPDIR"]
        self.assertNotEqual(autre, premier)
        self.assertFalse(os.path.exists(os.path.join(autre, "garde")))
        # un tour sans lot garde le dossier de l'agent, comme avant
        sans = menage.turn_env(self.cfg, self.pol, "a1", {})["TMPDIR"]
        self.assertEqual(sans, self.agent_dir)
        # un identifiant qui n'est pas un numéro ne fabrique pas de dossier
        self.assertEqual(menage.turn_env(self.cfg, self.pol, "a1", {},
                                         lot="lot/8020")["TMPDIR"], self.agent_dir)


class NettoyageDesLotsTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        menage.turn_env(self.cfg, self.pol, "a1", {}, lot=10)
        menage.turn_env(self.cfg, self.pol, "a1", {}, lot=11)
        _write(os.path.join(self.lot_dir(10), "f"))
        _write(os.path.join(self.lot_dir(11), "f"))
        # la table des processus ne doit pas retenir les dossiers du test
        patcher = mock.patch.object(menage.platform, "user_paths",
                                    lambda *a, **k: platform.UserPaths())
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(menage, "tmp_holder", lambda *a, **k: "")
        patcher.start()
        self.addCleanup(patcher.stop)

    def etat(self, ident):
        return "closed" if str(ident) == "10" else "active"

    def test_seul_le_lot_termine_perd_son_dossier(self):
        out = menage.cleanup_lot_dirs(self.cfg, self.pol, agents=["a1"],
                                      host="pc", lot_state=self.etat)
        self.assertFalse(os.path.exists(self.lot_dir(10)))
        self.assertTrue(os.path.exists(self.lot_dir(11)))
        self.assertEqual([d["lot"] for d in out["dirs"]], ["10"])
        self.assertTrue(out["entries"])
        self.assertEqual(out["entries"][0]["kind"], "tmp")

    def test_essai_ne_supprime_rien_et_donne_le_plan(self):
        out = menage.cleanup_lot_dirs(self.cfg, self.pol, agents=["a1"],
                                      lot_state=lambda ident: "closed", dry_run=True)
        self.assertTrue(os.path.exists(self.lot_dir(10)))
        self.assertTrue(os.path.exists(self.lot_dir(11)))
        self.assertEqual({p["path"] for p in out["plan"]},
                         {self.lot_dir(10), self.lot_dir(11)})
        self.assertEqual(out["dirs"], [])

    def test_arret_du_runner_garde_un_lot_actif(self):
        out = menage.cleanup_lot_dirs(self.cfg, self.pol, agents=["a1"],
                                      lot_state=self.etat, ended_only=True,
                                      reason="runner stopped")
        self.assertFalse(os.path.exists(self.lot_dir(10)))
        self.assertTrue(os.path.exists(self.lot_dir(11)))
        self.assertEqual([d["lot"] for d in out["dirs"]], ["10"])

    def _copie_git_avec_commit_non_pousse(self, path: str) -> None:
        origin = os.path.join(self.base, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin],
                       check=True, capture_output=True)
        subprocess.run(["git", "clone", "-q", origin, path], check=True,
                       capture_output=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        _write(os.path.join(path, "pousse"), 10)
        subprocess.run(["git", "-C", path, "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", path, "commit", "-q", "-m", "pousse"],
                       check=True, env=env)
        subprocess.run(["git", "-C", path, "push", "-q", "origin", "main"],
                       check=True, env=env)
        _write(os.path.join(path, "garde"), 10)
        subprocess.run(["git", "-C", path, "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", path, "commit", "-q", "-m", "non pousse"],
                       check=True, env=env)

    def test_un_lot_termine_avec_travail_git_non_pousse_est_garde(self):
        menage.turn_env(self.cfg, self.pol, "a1", {}, lot=12)
        copie = os.path.join(self.lot_dir(12), "clone")
        self._copie_git_avec_commit_non_pousse(copie)
        out = menage.cleanup_lot_dirs(self.cfg, self.pol, agents=["a1"],
                                      lot_state=lambda ident: "closed",
                                      ended_only=True)
        self.assertTrue(os.path.isdir(copie))
        self.assertTrue(any("git work" in note for note in out["skipped"]))
        commits = subprocess.run(["git", "-C", copie, "rev-list", "--count", "HEAD"],
                                 check=True, capture_output=True, text=True).stdout
        self.assertEqual(commits.strip(), "2")

    def test_arret_du_runner_garde_un_lot_en_build_avec_commit_non_pousse(self):
        menage.turn_env(self.cfg, self.pol, "a1", {}, lot=13)
        copie = os.path.join(self.lot_dir(13), "clone")
        self._copie_git_avec_commit_non_pousse(copie)
        menage.cleanup_lot_dirs(self.cfg, self.pol, agents=["a1"],
                               lot_state=lambda ident: "active",
                               ended_only=True, reason="runner stopped")
        self.assertTrue(os.path.isdir(copie))
        commits = subprocess.run(["git", "-C", copie, "rev-list", "--count", "HEAD"],
                                 check=True, capture_output=True, text=True).stdout
        self.assertEqual(commits.strip(), "2")

    def test_un_tour_en_cours_garde_le_dossier(self):
        tour = menage.Turn.begin(self.cfg, self.pol, "a1", None,
                                 new_session=False, base_env={}, lot=11)
        self.addCleanup(lambda: tour.lock and tour.lock.release())
        out = menage.cleanup_lot_dirs(self.cfg, self.pol, agents=["a1"],
                                      lot_state=lambda ident: "closed",
                                      ended_only=False)
        self.assertTrue(os.path.exists(self.lot_dir(11)))
        self.assertTrue(any("in turn (lock held)" in note for note in out["skipped"]))

    def test_un_autre_agent_n_est_pas_touche(self):
        menage.turn_env(self.cfg, self.pol, "a2", {}, lot=10)
        out = menage.cleanup_lot_dirs(self.cfg, self.pol, agents=["a1"],
                                      lot_state=lambda ident: "closed",
                                      ended_only=False)
        self.assertFalse(os.path.exists(self.lot_dir(10)))
        self.assertTrue(os.path.exists(os.path.join(
            self.cfg.state_dir, ".menage", "tmp", "a2", "ameesh-lot-10")))
        self.assertEqual([d["lot"] for d in out["dirs"]], ["10", "11"])


class BalayageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-8020-sweep-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.base], check=False)
        self.tmp = os.path.join(self.base, "tmp")
        self.shm = os.path.join(self.base, "shm")
        os.makedirs(self.tmp)
        os.makedirs(self.shm)
        self.now = time.time()
        self.pol = menage.Policy(
            sweep_age=3600,
            # deux projets déclarés : le préfixe du projet vient de la fiche,
            # jamais du cœur (#7660)
            sweep_prefixes=menage.DEFAULT_SWEEP_PREFIXES + ("nexlink-",))
        patcher = mock.patch.object(menage.platform, "user_paths",
                                    lambda *a, **k: platform.UserPaths())
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(menage, "tmp_holder", lambda *a, **k: "")
        patcher.start()
        self.addCleanup(patcher.stop)

    def dossier(self, racine: str, nom: str) -> str:
        path = os.path.join(racine, nom)
        _write(os.path.join(path, "f"))
        return path

    def scan(self, vieux: set):
        def lecture(path, limit=menage.WALK_LIMIT, budget=None, now=None):
            return {"bytes": 4000, "complete": True, "why": "",
                    "latest": self.now - (7200 if path in vieux else 10)}
        return lecture

    def test_balaye_les_prefixes_abandonnes_des_deux_racines(self):
        vieux = {self.dossier(self.tmp, "ameesh-old"),
                 self.dossier(self.shm, "nexlink-old")}
        recent = self.dossier(self.tmp, "ameesh-fresh")
        autre = self.dossier(self.tmp, "autre-old")
        lien = os.path.join(self.tmp, "ameesh-link")
        os.symlink(autre, lien)
        with mock.patch.object(menage, "scan_activity", self.scan(vieux)):
            out = menage.tmp_sweep(self.pol, roots=[self.tmp, self.shm], now=self.now)
        for path in vieux:
            self.assertFalse(os.path.lexists(path), path)
        for path in (recent, autre, lien):
            self.assertTrue(os.path.lexists(path), path)
        self.assertEqual({d["path"] for d in out["deleted"]}, vieux)
        self.assertGreater(out["bytes"], 0)

    def test_essai_et_desactivation_ne_suppriment_rien(self):
        vieux = self.dossier(self.tmp, "ameesh-old")
        with mock.patch.object(menage, "scan_activity", self.scan({vieux})):
            out = menage.tmp_sweep(self.pol, roots=[self.tmp], now=self.now,
                                   dry_run=True)
        self.assertTrue(os.path.isdir(vieux))
        self.assertEqual([p["path"] for p in out["plan"]], [vieux])
        self.assertEqual(out["deleted"], [])
        out = menage.tmp_sweep(menage.Policy(sweep=False), roots=[self.tmp],
                               now=self.now)
        self.assertTrue(os.path.isdir(vieux))
        self.assertTrue(any("disabled" in note for note in out["skipped"]))

    def test_un_doute_sur_les_processus_ou_un_travail_git_garde(self):
        vieux = self.dossier(self.tmp, "ameesh-old")
        doute = platform.UserPaths()
        doute.unreadable = ["999999 : outil non lisible"]
        with mock.patch.object(menage.platform, "user_paths", return_value=doute), \
                mock.patch.object(menage, "scan_activity", self.scan({vieux})):
            out = menage.tmp_sweep(self.pol, roots=[self.tmp], now=self.now)
        self.assertTrue(os.path.isdir(vieux))
        self.assertIn("unreadable", out["doubt"])
        with mock.patch.object(menage, "scan_activity", self.scan({vieux})), \
                mock.patch.object(menage, "tmp_git_work", return_value="modifications"):
            out = menage.tmp_sweep(self.pol, roots=[self.tmp], now=self.now)
        self.assertTrue(os.path.isdir(vieux))
        self.assertTrue(any("git work" in note for note in out["skipped"]))

    def test_un_dossier_trop_recent_ou_incomplet_est_garde(self):
        recent = self.dossier(self.tmp, "ameesh-recent")
        incomplet = self.dossier(self.tmp, "ameesh-incomplete")

        def lecture(path, limit=menage.WALK_LIMIT, budget=None, now=None):
            if path == incomplet:
                return {"bytes": 1, "latest": self.now - 7200, "complete": False,
                        "why": "unreadable entry"}
            return {"bytes": 1, "latest": self.now - 10, "complete": True, "why": ""}
        with mock.patch.object(menage, "scan_activity", lecture):
            out = menage.tmp_sweep(self.pol, roots=[self.tmp], now=self.now)
        self.assertTrue(os.path.isdir(recent))
        self.assertTrue(os.path.isdir(incomplet))
        self.assertTrue(any("unreadable entry" in note for note in out["skipped"]))
        self.assertEqual(out["deleted"], [])


class MesureEtPolitiqueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-8020-measure-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.base], check=False)
        self.cfg = _Cfg(os.path.join(self.base, "state"))

    def test_la_mesure_porte_dev_shm_et_son_seuil(self):
        shm = os.path.join(self.base, "shm")
        os.makedirs(shm)
        old_tmp, old_shm = os.environ.get("AMEESH_SYSTEM_TMP"), \
            os.environ.get("AMEESH_SYSTEM_SHM")
        os.environ["AMEESH_SYSTEM_TMP"] = os.path.join(self.base, "tmp")
        os.environ["AMEESH_SYSTEM_SHM"] = shm
        self.addCleanup(lambda: os.environ.pop("AMEESH_SYSTEM_TMP", None)
                        if old_tmp is None else os.environ.__setitem__(
                            "AMEESH_SYSTEM_TMP", old_tmp))
        self.addCleanup(lambda: os.environ.pop("AMEESH_SYSTEM_SHM", None)
                        if old_shm is None else os.environ.__setitem__(
                            "AMEESH_SYSTEM_SHM", old_shm))
        os.makedirs(os.path.join(self.base, "tmp"))
        m = menage.measure(self.cfg, menage.Policy())
        self.assertEqual(m["system_shm"]["path"], shm)
        self.assertIsNotNone(m["system_shm"]["size_bytes"])
        self.assertEqual(m["shm_alert"], menage.DEFAULT_SHM_ALERT)
        # un dossier de test n'est pas forcément un tmpfs : la part peut être
        # None, mais le champ est toujours là (l'alerte lit `fraction`)
        self.assertIn("fraction", m["system_shm"])
        self.assertIn("system_shm_fraction", m)

    def test_les_cles_de_politique_sont_lues(self):
        parsed, problems, unknown = menage.parse_policy(
            {"shm_alert": "25%", "sweep": False, "sweep_age": "3h"})
        self.assertEqual(problems, [])
        self.assertEqual(unknown, [])
        self.assertEqual(parsed["shm_alert"], 0.25)
        self.assertIs(parsed["sweep"], False)
        self.assertEqual(parsed["sweep_age"], 3 * 3600)
        # le balayage est coupé dès qu'une fiche le coupe
        class Fiche:
            def __init__(self, hk):
                self.policy = type("P", (), {"housekeeping": hk})()

        class Canon:
            def __init__(self, hk):
                self._f = Fiche(hk)

            def host(self, name):
                return self._f if name == "pc" else None
        pol = menage.effective([Canon({"sweep": True}),
                                Canon({"sweep": False, "sweep_age": 12 * 3600})], "pc")
        self.assertIs(pol.sweep, False)
        self.assertEqual(pol.sweep_age, 12 * 3600)

    def test_les_prefixes_du_balayage_viennent_des_projets_du_canon(self):
        # #7660 : aucun nom de client dans le cœur ; les préfixes sont lus aux
        # fiches Agent (`projects:`/`team:`) et à `naming.projects`.
        class Agent:
            effective_projects = ["ameesh", "nexlink"]

        class Canon:
            naming = type("N", (), {"projects": {"autre": "aut"}})()
            agents = [Agent()]

            def host(self, name):
                return None

        self.assertEqual(menage.mesh_sweep_prefixes([Canon()]),
                         ("ameesh-", "autre-", "nexlink-"))
        self.assertEqual(menage.mesh_sweep_prefixes([]),
                         menage.DEFAULT_SWEEP_PREFIXES)
        pol = menage.effective([Canon()], "pc")
        self.assertEqual(pol.sweep_prefixes,
                         ("ameesh-", "autre-", "nexlink-"))
        self.assertEqual(menage.sweep_prefixes_label(pol),
                         "`ameesh-*` / `autre-*` / `nexlink-*`")


if __name__ == "__main__":
    unittest.main()
