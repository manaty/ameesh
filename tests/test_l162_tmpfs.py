# SPDX-License-Identifier: AGPL-3.0-only
"""Rien de lourd en tmpfs (lot L162, suivi #172).

Le 2026-10-11 à 03:38, le /tmp du poste (tmpfs de 12 Go, partagé par douze
agents) était plein à 83 % : la porte de pression (80 %) a bloqué tous les
nouveaux tours. Le ménage signalait les entrées laissées par les tours
(`tmp_orphan`) sans rien en faire. Ici : dossiers gérés toujours sur disque
(et pour `attach`), entrées de /tmp abandonnées supprimées avec leurs
garde-fous, alertes qui disent les plus grosses entrées et l'action attendue,
consigne du tour.

Aucun test ne touche le vrai /tmp : le « /tmp du système » est un dossier du
bac à sable (`AMEESH_SYSTEM_TMP`)."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import adapters, exploitation, mail, menage, registry, storage
from ameesh import runner as runner_mod

from .support import PgTestCase
from .test_l73_menage import _Cfg, _git, _repo, _write

HEURE = 3600.0


class _Systeme:
    """Un faux /tmp du système, sous le bac à sable du test."""

    def systeme(self, base: str) -> str:
        path = os.path.join(base, "tmp-systeme")
        os.makedirs(path, exist_ok=True)
        old = os.environ.get("AMEESH_SYSTEM_TMP")
        os.environ["AMEESH_SYSTEM_TMP"] = path
        self.addCleanup(lambda: os.environ.__setitem__("AMEESH_SYSTEM_TMP", old)
                        if old is not None else os.environ.pop("AMEESH_SYSTEM_TMP", None))
        return path


# ==========================================================================
# politique et dossiers gérés
# ==========================================================================

class PolitiqueTest(unittest.TestCase):
    def test_cles_orphan_cleanup_et_orphan_idle(self):
        parsed, problems, unknown = menage.parse_policy(
            {"orphan_cleanup": False, "orphan_idle": "90m"})
        self.assertEqual(parsed, {"orphan_cleanup": False, "orphan_idle": 5400.0})
        self.assertEqual((problems, unknown), ([], []))
        self.assertEqual(menage.parse_policy({"orphan_idle": 7200})[0]["orphan_idle"], 7200.0)
        for mauvais in ("abc", 0, -5, True, "2 semaines"):
            with self.subTest(orphan_idle=mauvais):
                self.assertTrue(menage.parse_policy({"orphan_idle": mauvais})[1])
        self.assertTrue(menage.parse_policy({"orphan_cleanup": "non"})[1])
        defaut = menage.Policy()
        self.assertTrue(defaut.orphan_cleanup)
        self.assertEqual(defaut.orphan_idle, 2 * HEURE)

    def test_fusion_coupure_au_moindre_canon_et_inactivite_la_plus_longue(self):
        class Fiche:
            def __init__(self, hk):
                self.policy = type("P", (), {"housekeeping": hk})()

        class Canon:
            def __init__(self, ident, hk):
                self.id, self._f = ident, Fiche(hk)

            def host(self, name):
                return self._f if name == "pc" else None

        a = Canon("a", {"orphan_cleanup": True, "orphan_idle": 3600.0})
        b = Canon("b", {"orphan_cleanup": False, "orphan_idle": 4 * HEURE})
        pol = menage.effective([a, b], "pc")
        self.assertFalse(pol.orphan_cleanup)
        self.assertEqual(pol.orphan_idle, 4 * HEURE)
        self.assertEqual(pol.origin["orphan_cleanup"], "b")
        self.assertTrue(menage.effective([a], "pc").orphan_cleanup)


class RacineSurDisqueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-l162-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.base])
        self.cfg = _Cfg(os.path.join(self.base, "state"))

    def test_racine_declaree_en_memoire_ecartee(self):
        declaree = os.path.join(self.base, "en-memoire")
        pol = menage.Policy(tmp_root=declaree, cache_root=declaree + "-cache")
        defaut = os.path.join(self.cfg.state_dir, ".menage")
        with mock.patch.object(menage, "in_memory", side_effect=lambda p: p.startswith(declaree)):
            self.assertEqual(menage.tmp_root(self.cfg, pol), os.path.join(defaut, "tmp"))
            self.assertEqual(menage.cache_root(self.cfg, pol), os.path.join(defaut, "cache"))
            env = menage.turn_env(self.cfg, pol, "a1", {})
            self.assertEqual(env["TMPDIR"], os.path.join(defaut, "tmp", "a1"))
            self.assertEqual(env["TMP"], env["TMPDIR"])
            self.assertEqual(env["TEMP"], env["TMPDIR"])
            notes = menage.declared_root_refused(self.cfg, pol)
            self.assertEqual(len(notes), 2)
            self.assertIn("tmpfs", notes[0])
        # sur disque : la racine déclarée est gardée
        with mock.patch.object(menage, "in_memory", return_value=False):
            self.assertEqual(menage.tmp_root(self.cfg, pol), declaree)
            self.assertEqual(menage.declared_root_refused(self.cfg, pol), [])
        # tout en mémoire (l'état aussi) : rien de mieux, la déclaration reste
        with mock.patch.object(menage, "in_memory", return_value=True):
            self.assertEqual(menage.tmp_root(self.cfg, pol), declaree)

    def test_in_memory_lit_le_type_du_montage(self):
        with mock.patch.object(menage.platform, "mount_of", return_value=("/tmp", "tmpfs")):
            self.assertTrue(menage.in_memory("/tmp/x"))
        with mock.patch.object(menage.platform, "mount_of", return_value=("/", "ext4")):
            self.assertFalse(menage.in_memory("/home/x"))
        with mock.patch.object(menage.platform, "mount_of", side_effect=OSError("x")):
            self.assertFalse(menage.in_memory("/home/x"))   # on ne devine pas

    def test_attach_redirige_aussi(self):
        self.cfg.container_runtime = "none"
        env = runner_mod._attach_tmp_env(self.cfg, "a1", {})
        self.assertEqual(env["TMPDIR"], os.path.join(self.cfg.state_dir, ".menage", "tmp",
                                                     "a1"))
        self.assertEqual(env["AMEESH_TMPDIR"], env["TMPDIR"])
        self.assertEqual(runner_mod._attach_tmp_env(
            self.cfg, "a1", {"AMEESH_HOUSEKEEPING": "off"}), {})


class ActiviteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-l162-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.base])

    def test_ctime_compte_et_parcours_borne(self):
        entree = os.path.join(self.base, "extrait")
        _write(os.path.join(entree, "sous", "f"), 3000, age_s=40 * 365 * 24 * HEURE)
        scan = menage.scan_activity(entree)
        self.assertTrue(scan["complete"])
        # mtime de 1986 (archive extraite) : le ctime dit « à l'instant »
        self.assertGreater(scan["latest"], time.time() - 60)
        self.assertGreaterEqual(scan["bytes"], 3000)
        _write(os.path.join(entree, "g"), 10)
        borne = menage.scan_activity(entree, limit=2)
        self.assertFalse(borne["complete"])
        self.assertIn("entrées", borne["why"])
        self.assertFalse(menage.scan_activity(os.path.join(self.base, "absent"))["complete"])

    def test_plus_grosses_entrees(self):
        for nom, taille in (("petit", 10), ("gros", 90000), ("moyen", 20000)):
            _write(os.path.join(self.base, nom, "f"), taille)
        os.symlink(os.path.join(self.base, "gros"), os.path.join(self.base, "lien"))
        top = menage.tmp_top(self.base, n=2)
        self.assertEqual([os.path.basename(e["path"]) for e in top], ["gros", "moyen"])
        self.assertTrue(top[0]["owner"])
        texte = menage.tmpfs_hint(top, menage.Policy())
        self.assertIn("gros", texte)
        self.assertIn("action attendue", texte)
        self.assertIn("2 h", texte)
        self.assertIn("ameesh menage", menage.tmpfs_hint([], menage.Policy()))
        self.assertEqual(menage.fmt_idle(5400), "90 min")


class ConsigneTest(unittest.TestCase):
    def test_le_tour_de_courrier_porte_la_regle(self):
        for kind in ("mail", "event", "urgent"):
            with self.subTest(kind=kind):
                texte = mail._assemble(kind, ["— message n°1 de a :\n> x"], 0)
                self.assertIn(adapters.TMP_NOTE, texte)
        self.assertIn("$TMPDIR", adapters.TMP_NOTE)
        self.assertIn("500 Mo", adapters.TMP_NOTE)
        racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(racine, "docs", "ORCHESTRATEUR.md"), encoding="utf-8") as fh:
            orchestrateur = fh.read()
        self.assertIn("$TMPDIR", orchestrateur)
        self.assertIn("500 Mo", orchestrateur)


class PressionDuTmpfsTest(unittest.TestCase):
    def test_un_menage_immediat_par_episode(self):
        faux = mock.Mock(host="pc", _pressure_episode=None)
        tmpfs = {"blocked": True, "critical": False,
                 "breaches": [{"key": "max_tmpfs_used", "label": "tmpfs /tmp occupé",
                               "value": 0.83, "limit": 0.8, "critical": False}]}
        swap = {"blocked": True, "critical": False,
                "breaches": [{"key": "max_swap_used", "label": "swap utilisé",
                              "value": 9, "limit": 8, "critical": False}]}
        with mock.patch.object(runner_mod, "log_async"):
            runner_mod.Runner._pressure_episode_update(faux, swap)
            self.assertEqual(faux.tmpfs_pressure_pass.call_count, 0)
            runner_mod.Runner._pressure_episode_update(faux, tmpfs)   # le tmpfs s'y ajoute
            runner_mod.Runner._pressure_episode_update(faux, tmpfs)   # le même épisode
            self.assertEqual(faux.tmpfs_pressure_pass.call_count, 1)
            runner_mod.Runner._pressure_episode_update(faux, {"blocked": False})
            runner_mod.Runner._pressure_episode_update(faux, tmpfs)   # nouvel épisode
            self.assertEqual(faux.tmpfs_pressure_pass.call_count, 2)

    def test_pas_de_menage_immediat_sans_passage_periodique(self):
        faux = mock.Mock(once=False, cfg=mock.Mock(housekeeping_interval=0))
        self.assertFalse(runner_mod.Runner.tmpfs_pressure_pass(faux))
        faux = mock.Mock(once=True, cfg=mock.Mock(housekeeping_interval=600))
        self.assertFalse(runner_mod.Runner.tmpfs_pressure_pass(faux))


# ==========================================================================
# avec la base : entrées abandonnées supprimées, alertes
# ==========================================================================

class AbandonDbTest(_Systeme, PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees", "host_resources"):
            self.db.execute("DELETE FROM %s" % table)
        self.cfgl = _Cfg(self.state)
        self.cfgl.host = self.cfg.host
        self.sys = self.systeme(self.tmp)
        self.pol = menage.Policy(orphan_min_size=1)
        registry.upsert(self.db, "a1", harness="claude", host="pc")

    def _tour(self, creer, agent: str = "a1", turn: str = "t" * 32) -> list[dict]:
        """Un tour de `agent` pendant lequel `creer()` remplit le faux /tmp."""
        tour = menage.Turn.begin(self.cfgl, self.pol, agent, None, new_session=False,
                                 base_env={})
        creer()
        entries = tour.end(lot=None, turn_id=turn, db=self.db, host="pc")
        storage.of(self.db).housekeeping.log("pc", entries, actor="test")
        return entries

    def _passage(self, decalage: float, **kw) -> dict:
        return menage.run_pass(self.cfgl, self.db, kw.pop("policy", self.pol), host="pc",
                               actor="t", now=time.time() + decalage, **kw)

    def test_abandonne_supprime_recent_attend_essai_ne_touche_rien(self):
        abandon = os.path.join(self.sys, "preuve-images")
        self.addCleanup(subprocess.run, ["chmod", "-R", "u+rwx", abandon],
                        capture_output=True)

        def creer():
            _write(os.path.join(abandon, "image.tar"), 50000)
            # un cache de modules Go : dossiers en lecture seule
            _write(os.path.join(abandon, "mod", "lecture-seule", "f"), 100)
            os.chmod(os.path.join(abandon, "mod", "lecture-seule"), 0o555)
            os.chmod(os.path.join(abandon, "mod"), 0o555)

        entries = self._tour(creer)
        signale = [e for e in entries if e["kind"] == "orphan"]
        self.assertEqual([e["path"] for e in signale], [abandon])
        self.assertIn("supprimé par le ménage après 2 h", signale[0]["detail"])
        self.assertIn("ino", signale[0]["data"])
        # 30 min plus tard : il attend
        rapport = self._passage(1800.0)
        self.assertTrue(os.path.exists(abandon))
        self.assertEqual(rapport["orphans"][0]["status"], "attend")
        self.assertIn("supprimé après 2 h", rapport["orphans"][0]["detail"])
        # essai, 3 h plus tard : prévu, rien de supprimé
        essai = self._passage(3 * HEURE, dry_run=True)
        self.assertTrue(os.path.exists(abandon))
        self.assertEqual([p["action"] for p in essai["plan"] if p["kind"] == "orphan"],
                         ["suppression"])
        # passage réel 3 h plus tard : supprimé, journalisé avec la taille
        fait = self._passage(3 * HEURE)
        self.assertFalse(os.path.exists(abandon))
        self.assertEqual(fait["orphans"], [])
        journal = storage.of(self.db).housekeeping.recent("pc", 3600, kind="orphan")
        supprime = [r for r in journal if r["action"] == "deleted"]
        self.assertEqual([r["path"] for r in supprime], [abandon])
        self.assertGreaterEqual(supprime[0]["bytes"], 50000)
        self.assertEqual(supprime[0]["agent"], "a1")
        self.assertEqual({r["kind"] for r in journal}, {"orphan"})
        # plus d'alerte `tmp_orphan` pour une entrée supprimée
        types = [a["type"] for a in exploitation.alerts(self.cfg, self.db)]
        self.assertNotIn("tmp_orphan", types)

    def test_garde_fous(self):
        ouvert = os.path.join(self.sys, "ouvert")
        sale = os.path.join(self.sys, "clone-sale")
        homonyme = os.path.join(self.sys, "recree")
        inconnu = os.path.join(self.sys, "agent-parti")

        def creer():
            _write(os.path.join(ouvert, "f"), 5000)
            os.makedirs(sale)
            _git("init", "-q", cwd=sale)
            _write(os.path.join(sale, "non-commite.txt"), 5000)
            _write(os.path.join(homonyme, "f"), 5000)

        self._tour(creer)
        self._tour(lambda: _write(os.path.join(inconnu, "f"), 5000), agent="parti",
                   turn="u" * 32)
        # recréé depuis le signalement : une autre entrée (autre inode ;
        # l'ancienne est gardée à côté pour que son inode ne soit pas réutilisé)
        os.rename(homonyme, homonyme + "-ancien")
        _write(os.path.join(homonyme, "f"), 5000)
        # une ligne qui désigne un chemin HORS du /tmp du système
        dehors = os.path.join(self.tmp, "ailleurs", "precieux")
        _write(os.path.join(dehors, "f"), 5000)
        storage.of(self.db).housekeeping.log("pc", [{
            "kind": "orphan", "action": "signaled", "path": dehors, "bytes": 5000,
            "agent": "a1", "detail": "test", "data": {"turn": "x"}}], actor="test")
        occupant = subprocess.Popen(["sleep", "60"], cwd=ouvert)
        self.addCleanup(occupant.kill)
        rapport = self._passage(3 * HEURE)
        sorts = {o["path"]: (o["status"], o["detail"]) for o in rapport["orphans"]}
        self.assertEqual(sorts[ouvert][0], "attend")
        self.assertIn("tenu par le processus %d" % occupant.pid, sorts[ouvert][1])
        self.assertEqual(sorts[sale][0], "garde")
        self.assertIn("travail git", sorts[sale][1])
        self.assertIn("autre inode", sorts[homonyme][1])
        self.assertIn("absent du registre", sorts[inconnu][1])
        self.assertIn("hors du /tmp du système", sorts[dehors][1])
        for chemin in (ouvert, sale, homonyme, inconnu, dehors):
            self.assertTrue(os.path.exists(chemin), chemin)
        self.assertEqual([e for e in rapport["entries"] if e["kind"] == "orphan"], [])
        # le processus parti, l'entrée ouverte est supprimée au passage suivant
        occupant.kill()
        occupant.wait()
        self._passage(3 * HEURE)
        self.assertFalse(os.path.exists(ouvert))
        self.assertTrue(os.path.exists(sale))
        # alerte : le sort dit dans le texte, pour ce qui reste
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "tmp_orphan"]
        self.assertIn(sale, {a["path"] for a in alertes})
        self.assertNotIn(ouvert, {a["path"] for a in alertes})
        self.assertTrue(all("supprimé par le ménage une fois inchangé" in a["detail"]
                            for a in alertes))

    def test_worktree_de_relecture_propre_supprime_et_elague(self):
        main = _repo(self.tmp)
        relecture = os.path.join(self.sys, "relecture")
        self._tour(lambda: _git("worktree", "add", "-q", "--detach", relecture, cwd=main))
        self.assertIn(relecture, _git("worktree", "list", cwd=main))
        self._passage(3 * HEURE)
        self.assertFalse(os.path.exists(relecture))
        self.assertNotIn(relecture, _git("worktree", "list", cwd=main))

    def test_coupure_par_hote_et_tour_en_cours(self):
        abandon = os.path.join(self.sys, "jest_rs")
        self._tour(lambda: _write(os.path.join(abandon, "cache"), 5000))
        coupe = menage.Policy(orphan_min_size=1, orphan_cleanup=False)
        rapport = self._passage(3 * HEURE, policy=coupe)
        self.assertTrue(os.path.exists(abandon))
        self.assertIn("orphan_cleanup: false", rapport["orphans"][0]["detail"])
        rapport = self._passage(3 * HEURE, agents_in_turn={"a1"})
        self.assertTrue(os.path.exists(abandon))
        self.assertIn("tour en cours", rapport["orphans"][0]["detail"])
        with mock.patch.dict(os.environ, {"AMEESH_HOUSEKEEPING": "off"}):
            self._passage(3 * HEURE)
        self.assertTrue(os.path.exists(abandon))

    def _lecture(self, used_gib: float) -> None:
        storage.of(self.db).hosts.record({
            "host": self.cfg.host, "mem_available_bytes": 8 * 1024 ** 3, "swap_used_bytes": 0,
            "load1": 0.1, "cpu_count": 8, "disk_free_bytes": 100 * 1024 ** 3,
            "disk_path": "/", "turns_in_progress": 0, "tmp_path": "/tmp",
            "tmp_fstype": "tmpfs", "tmp_size_bytes": 12 * 1024 ** 3,
            "tmp_used_bytes": int(used_gib * 1024 ** 3)})

    def test_alerte_de_pression_dit_les_plus_gros_et_l_action(self):
        self._lecture(10.0)   # 83 % : pression (80 %) et alerte (60 %)
        alertes = {a["type"]: a for a in exploitation.alerts(self.cfg, self.db)}
        self.assertIn("ameesh menage", alertes["host_pressure"]["detail"])   # sans bilan
        self.assertIn("action attendue", alertes["host_pressure"]["detail"])
        top = [{"path": "/tmp/preuve-images", "bytes": 4 * 1024 ** 3, "owner": "u",
                "agent": "a1", "status": "attend"},
               {"path": "/tmp/jest_rs", "bytes": 1536 * 1024 ** 2, "owner": "u"}]
        storage.of(self.db).housekeeping.log(self.cfg.host, [{
            "kind": "mesure", "action": "measured", "path": "/x", "bytes": 0,
            "detail": "bilan du ménage", "data": {"system_tmp_top": top}}], actor="t")
        alertes = {a["type"]: a for a in exploitation.alerts(self.cfg, self.db)}
        for type_ in ("host_pressure", "tmpfs_full"):
            with self.subTest(type=type_):
                detail = alertes[type_]["detail"]
                self.assertIn("/tmp/preuve-images 4.0 GiB (u, laissé par a1 : attend)", detail)
                self.assertIn("/tmp/jest_rs 1.5 GiB", detail)
                self.assertIn("action attendue", detail)
                self.assertIn("$TMPDIR", detail)
                self.assertEqual([e["path"] for e in alertes[type_]["tmp_top"]],
                                 ["/tmp/preuve-images", "/tmp/jest_rs"])
        proc = self.mesh("hosts")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("/tmp gros  /tmp/preuve-images 4.0 GiB", proc.stdout)

    def test_le_bilan_releve_les_plus_gros_quand_le_tmpfs_se_remplit(self):
        _write(os.path.join(self.sys, "gros", "f"), 90000)
        plein = {"tmp_path": self.sys, "tmp_fstype": "tmpfs", "tmp_size_bytes": 1000,
                 "tmp_used_bytes": 850}
        with mock.patch.object(menage.resources_mod, "fs_usage", return_value=plein):
            rapport = self._passage(0.0)
        top = rapport["measure"]["system_tmp_top"]
        self.assertEqual([e["path"] for e in top], [os.path.join(self.sys, "gros")])
        self.assertIn("rm -rf -- ", top[0]["command"])
        dernier = storage.of(self.db).housekeeping.last_measure("pc")[0]["data"]
        self.assertEqual(dernier["system_tmp_top"][0]["path"], os.path.join(self.sys, "gros"))


if __name__ == "__main__":
    unittest.main()
