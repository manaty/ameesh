# SPDX-License-Identifier: AGPL-3.0-only
"""Ménage de ce que les agents créent (lot L73) : dossier temporaire par
agent, caches partagés bornés, worktrees retirés en fin de lot, /tmp signalé
(jamais supprimé), mesure du tmpfs et contre-pression, `ameesh menage`."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ameesh import (build_artifacts, canon, containers, exploitation, menage,
                    platform, registry, resources, storage, work)

from .support import PgTestCase
from .test_l43_perimetre_hote import HOST, _Canons, codes, host

GIB = 1024 ** 3


def _sans_processus(test: unittest.TestCase) -> None:
    """#1665 : neutralise la table des processus de l'hôte pour un test de
    retrait. Un démon non « dumpable » (gpg-agent, `ameesh-offload`) ou un
    zombie laisse un doute qui retient tout retrait : la décision testée ici
    est celle des garde-fous git (propre, poussé, âge), pas celle des
    processus — la lecture réelle (`platform.user_paths`, `platform.holders`)
    a ses propres tests (`tests/test_l63_platform.py`). #1805 : les deux
    entrées de la couche plateforme sont neutralisées, pas seulement
    `user_paths` (`worktree_in_use` lit `holders`)."""
    for name, answer in (("user_paths", lambda *a, **k: platform.UserPaths()),
                         ("holders", lambda *a, **k: [])):
        patcher = mock.patch.object(platform, name, answer)
        patcher.start()
        test.addCleanup(patcher.stop)


class _Cfg:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.host = "pc"
        # #4609 : `exploitation.alerts` lit la fenêtre et la part de la
        # sauvegarde du mesh. Valeurs par défaut de `config.Config`
        # (03:30-04:30 UTC, 90 %, groupe 5 %), pour que la doublure reste un
        # config crédible sans dépendre de l'environnement du banc.
        self.mesh_backup_window = (12600, 16200)
        self.mesh_backup_min_share = 0.9
        self.mesh_backup_min_group_share = 0.05


def _write(path: str, size: int, age_s: float = 0.0) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"z" * size)
    if age_s:
        moment = time.time() - age_s
        os.utime(path, (moment, moment))


def _age_tree(path: str, age_s: float) -> None:
    at = time.time() - age_s
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files + dirs:
            os.utime(os.path.join(root, name), (at, at))
    os.utime(path, (at, at))


def _git(*args: str, cwd: str) -> str:
    out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                         check=True, env=dict(os.environ, GIT_AUTHOR_NAME="t",
                                              GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                              GIT_COMMITTER_EMAIL="t@t"))
    return out.stdout


def _repo(base: str) -> str:
    """Un dépôt `main` cloné d'un dépôt nu `origin.git`, un commit poussé."""
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main",
                    os.path.join(base, "origin.git")], check=True)
    subprocess.run(["git", "clone", "-q", os.path.join(base, "origin.git"),
                    os.path.join(base, "main")], check=True, capture_output=True)
    main = os.path.join(base, "main")
    _git("commit", "-q", "--allow-empty", "-m", "racine", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    return main


def _pose_system_tmp(test: unittest.TestCase, chemin: str) -> None:
    """Pose `AMEESH_SYSTEM_TMP` pour un test et le rend tel qu'il l'a trouvé
    au nettoyage : absent s'il l'était, au lieu de le laisser vide. Vide vaut
    `/tmp` en produit (`resources.system_tmp`) mais une variable vide n'est
    pas une variable absente et fausse l'isolation du run suivant (#2532)."""
    old = os.environ.get("AMEESH_SYSTEM_TMP")
    os.environ["AMEESH_SYSTEM_TMP"] = chemin
    test.addCleanup(lambda: os.environ.pop("AMEESH_SYSTEM_TMP", None)
                    if old is None
                    else os.environ.__setitem__("AMEESH_SYSTEM_TMP", old))


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-l73-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.base])
        self.cfg = _Cfg(os.path.join(self.base, "state"))


# ==========================================================================
# politique
# ==========================================================================

class PolitiqueTest(unittest.TestCase):
    def test_cles_valides_invalides_et_inconnues(self):
        parsed, problems, unknown = menage.parse_policy({
            "tmp_root": "/srv/ameesh-tmp", "tmp_quota": "2GiB", "cache_quota": 5 * GIB,
            "tmpfs_alert": "50%", "xdg_cache": True, "redirect": "oui", "inconnue": 1})
        self.assertEqual(parsed["tmp_root"], "/srv/ameesh-tmp")
        self.assertEqual(parsed["tmp_quota"], 2 * GIB)
        self.assertEqual(parsed["tmpfs_alert"], 0.5)
        self.assertTrue(parsed["xdg_cache"])
        self.assertNotIn("redirect", parsed)
        self.assertEqual(len(problems), 1)
        self.assertEqual(unknown, ["inconnue"])

    def test_racines_partagees_refusees(self):
        for racine in ("/tmp", "/", "~", "/tmp/", "relatif/x", "/x/{agent}"):
            with self.subTest(racine=racine):
                _p, problems, _u = menage.parse_policy({"tmp_root": racine})
                self.assertTrue(problems)

    def test_fusion_des_canons_la_plus_stricte(self):
        class Fiche:
            def __init__(self, hk):
                self.policy = type("P", (), {"housekeeping": hk})()

        class Canon:
            def __init__(self, ident, hk):
                self.id, self._f = ident, Fiche(hk)

            def host(self, name):
                return self._f if name == "pc" else None

        a = Canon("a", {"tmp_root": "/srv/a", "tmp_quota": 8 * GIB, "tmpfs_alert": 0.7})
        b = Canon("b", {"tmp_root": "/srv/b", "tmp_quota": 2 * GIB})
        pol = menage.effective([a, b], "pc")
        self.assertEqual(pol.tmp_root, "/srv/a")       # première déclaration
        self.assertEqual(pol.tmp_quota, 2 * GIB)       # la plus stricte
        self.assertEqual(pol.tmpfs_alert, 0.7)
        self.assertEqual(pol.origin["tmp_quota"], "b")
        self.assertEqual(menage.effective([a, b], "autre"), menage.Policy())


class CanonTest(_Canons, unittest.TestCase):
    def test_canon_check_valide_housekeeping_et_tmpfs(self):
        texte = host("bob", harnesses=("codex",), resources_yaml=(
            "  resources:\n    max_tmpfs_used: 70%\n"
            "  housekeeping:\n    tmp_quota: 1GiB\n    tmpfs_alert: 0.5\n"
            "    tmp_root: /tmp\n    bizarre: 1\n"))
        loaded = canon.load(self.acme(host_text=texte), default=False)
        found = canon.validate(loaded)
        fiche = loaded.host(HOST)
        self.assertEqual(fiche.policy.resources["max_tmpfs_used"], 0.7)
        self.assertEqual(fiche.policy.housekeeping["tmp_quota"], GIB)
        self.assertNotIn("tmp_root", fiche.policy.housekeeping)   # refusé : /tmp
        self.assertIn("host-policy-invalid", codes(found, canon.ERROR))
        self.assertIn("host-housekeeping-unknown", codes(found, canon.WARNING))
        self.assertEqual(menage.effective([loaded], HOST).tmpfs_alert, 0.5)
        self.assertEqual(resources.host_limits([loaded], HOST)["limits"]["max_tmpfs_used"], 0.7)


# ==========================================================================
# dossiers gérés, éviction, suppression sûre
# ==========================================================================

class DossiersTest(_Tmp):
    def test_environnement_du_tour(self):
        pol = menage.Policy()
        env = menage.turn_env(self.cfg, pol, "a1", {"npm_config_cache": "/choix/hote"})
        mine = os.path.join(self.cfg.state_dir, ".menage", "tmp", "a1")
        self.assertEqual(env["TMPDIR"], mine)
        self.assertEqual(env["TMP"], mine)
        self.assertEqual(env["TEMP"], mine)
        self.assertTrue(os.path.isdir(mine))
        # le choix de l'hôte est respecté ; les autres caches sont gérés
        self.assertNotIn("npm_config_cache", env)
        cache = os.path.join(self.cfg.state_dir, ".menage", "cache")
        self.assertEqual(env["npm_config_store_dir"], os.path.join(cache, "pnpm"))
        self.assertEqual(env["pnpm_config_store_dir"], os.path.join(cache, "pnpm"))
        self.assertEqual(env["YARN_CACHE_FOLDER"], os.path.join(cache, "yarn"))
        self.assertNotIn("XDG_CACHE_HOME", env)
        self.assertTrue(os.path.isfile(os.path.join(cache, menage.MARKER)))
        self.assertEqual(menage.turn_env(self.cfg, menage.Policy(redirect=False), "a1", {}), {})
        self.assertEqual(menage.turn_env(self.cfg, pol, "a1", {"AMEESH_HOUSEKEEPING": "off"}),
                         {})
        xdg = menage.turn_env(self.cfg, menage.Policy(xdg_cache=True), "a1", {})
        self.assertEqual(xdg["XDG_CACHE_HOME"], os.path.join(cache, "xdg"))

    @unittest.skipUnless(platform.is_linux(),
                         "#2704 : les chemins tenus se lisent dans /proc (Linux)")
    def test_nouvelle_session_ne_vide_pas_un_dossier_tmp_tenu(self):
        """#2704 : une application lancée depuis le dossier temporaire de
        l'agent (AppImage extraite) le tient hors tour ; la nouvelle session
        ne le vide pas sous elle."""
        pol = menage.Policy()
        mine = menage.turn_env(self.cfg, pol, "a1", {})["TMPDIR"]
        garde = os.path.join(mine, "garde")
        _write(garde, 10)
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(60)"], cwd=mine)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        tour = menage.Turn.begin(self.cfg, pol, "a1", None, new_session=True,
                                 base_env={})
        self.addCleanup(tour.lock.release)
        self.assertTrue(os.path.exists(garde))
        self.assertIn(("tmp", "held"),
                      {(e["kind"], e["action"]) for e in tour.entries})

    def test_eviction_du_plus_ancien_jusqu_au_quota(self):
        root = os.path.join(self.base, "cache")
        menage._ensure_root(root)
        npm = os.path.join(root, "npm")
        _write(os.path.join(npm, "vieux"), 4000, age_s=10 * 3600)
        _write(os.path.join(npm, "moyen", "f"), 4000, age_s=5 * 3600)
        _write(os.path.join(npm, "recent"), 4000, age_s=0)
        essai = menage.evict_lru([npm], root, 6000, dry_run=True)
        self.assertGreater(essai["evicted"], 0)
        self.assertTrue(os.path.exists(os.path.join(npm, "vieux")))      # essai : rien
        fait = menage.evict_lru([npm], root, 3000)
        self.assertFalse(os.path.exists(os.path.join(npm, "vieux")))
        self.assertFalse(os.path.exists(os.path.join(npm, "moyen")))     # dossier vide retiré
        self.assertTrue(os.path.exists(os.path.join(npm, "recent")))     # en écriture possible
        self.assertTrue(fait["over_quota"])          # le récent ne part pas : c'est dit
        self.assertEqual(fait["files"], 2)

    def test_racine_sans_marqueur_jamais_touchee(self):
        root = os.path.join(self.base, "pas-a-nous")
        _write(os.path.join(root, "x", "f"), 5000, age_s=10 * 3600)
        with self.assertRaises(menage.RefusedDeletion):
            menage.evict_lru([os.path.join(root, "x")], root, 10)
        with self.assertRaises(menage.RefusedDeletion):
            menage._safe_rmtree(os.path.join(root, "x"), root)
        self.assertTrue(os.path.exists(os.path.join(root, "x", "f")))

    def test_lien_symbolique_jamais_suivi_ni_chemin_hors_racine(self):
        root = os.path.join(self.base, "geré")
        menage._ensure_root(root)
        dehors = os.path.join(self.base, "dehors")
        _write(os.path.join(dehors, "precieux"), 100)
        os.makedirs(os.path.join(root, "a1"))
        os.symlink(dehors, os.path.join(root, "a1", "lien"))
        menage._safe_rmtree(os.path.join(root, "a1", "lien"), root)
        self.assertTrue(os.path.exists(os.path.join(dehors, "precieux")))
        self.assertFalse(os.path.lexists(os.path.join(root, "a1", "lien")))
        for chemin in (dehors, root, os.path.join(root, menage.MARKER),
                       os.path.join(root, "..", "dehors")):
            with self.subTest(chemin=chemin), self.assertRaises(menage.RefusedDeletion):
                menage._safe_rmtree(chemin, root)
        self.assertTrue(os.path.exists(os.path.join(dehors, "precieux")))

    def test_tmp_du_systeme_signale_jamais_supprime(self):
        systeme = os.path.join(self.base, "tmp-systeme")
        os.makedirs(systeme)
        _write(os.path.join(systeme, "ancien", "f"), 9000)
        avant = menage.tmp_names(systeme)
        _write(os.path.join(systeme, "clone-relecture", "f"), 9000)
        _write(os.path.join(systeme, "petit"), 10)
        _write(os.path.join(systeme, "exclu", "f"), 9000)
        found = menage.new_tmp_entries(avant, path=systeme, min_size=5000,
                                       exclude=(os.path.join(systeme, "exclu"),))
        self.assertEqual([f["path"] for f in found], [os.path.join(systeme, "clone-relecture")])
        self.assertIn("rm -rf -- ", found[0]["command"])
        self.assertTrue(os.path.exists(os.path.join(systeme, "clone-relecture", "f")))

    def test_appelant_agent_refuse(self):
        self.assertIsNone(menage.caller_refusal({}))
        self.assertIn("AGENT_MAIL_NAME", menage.caller_refusal({"AGENT_MAIL_NAME": "a1"}))
        self.assertIn("AMEESH_TURN_ID", menage.caller_refusal({"AMEESH_TURN_ID": "x"}))


# ==========================================================================
# worktrees : état git
# ==========================================================================

class WorktreeEtatTest(_Tmp):
    def test_propre_pousse_sale_non_pousse_et_rebase(self):
        main = _repo(self.base)
        wt = os.path.join(self.base, "wt")
        _git("worktree", "add", "-q", wt, "-b", "lot", cwd=main)
        self.assertEqual([w["path"] for w in menage.list_worktrees(main)][1:], [wt])
        st = menage.worktree_state(wt)
        self.assertTrue(st["clean"] and st["pushed"] and st["integrated"], st)
        _write(os.path.join(wt, "brouillon.txt"), 3)
        st = menage.worktree_state(wt)
        self.assertFalse(st["clean"])
        self.assertIn("untracked", st["reason"])
        _git("add", ".", cwd=wt)
        _git("commit", "-q", "-m", "travail", cwd=wt)
        st = menage.worktree_state(wt)
        self.assertTrue(st["clean"])
        self.assertFalse(st["pushed"] or st["integrated"])
        self.assertIn("not pushed", st["reason"])
        # L73b : poussé sur sa branche mais pas fusionné — pas intégré
        _git("push", "-q", "origin", "lot", cwd=wt)
        st = menage.worktree_state(wt)
        self.assertTrue(st["pushed"])
        self.assertFalse(st["integrated"])
        self.assertIn("pushed but not merged", st["reason"])
        # intégré par rebase : le même changement est sur origin/main
        _git("cherry-pick", "lot", cwd=main)
        _git("push", "-q", "origin", "main", cwd=main)
        _git("fetch", "-q", "origin", cwd=wt)
        _git("remote", "set-head", "origin", "main", cwd=main)
        self.assertTrue(menage.worktree_state(wt)["integrated"])
        # verrouillé : signalé, jamais retirable
        _git("worktree", "lock", wt, cwd=main)
        self.assertTrue(menage.list_worktrees(main)[1]["locked"])
        st = menage.worktree_state(wt)
        self.assertTrue(st["locked"])
        self.assertIn("locked",st["reason"])
        self.assertFalse(menage.worktree_state(os.path.join(self.base, "absent"))["exists"])

    def test_integre_par_fusion_ecrasee(self):
        main = _repo(self.base)
        wt = os.path.join(self.base, "wt")
        _git("worktree", "add", "-q", wt, "-b", "lot", cwd=main)
        for nom in ("a.txt", "b.txt"):
            _write(os.path.join(wt, nom), 3)
            _git("add", nom, cwd=wt)
            _git("commit", "-q", "-m", nom, cwd=wt)
        _git("push", "-q", "origin", "lot", cwd=wt)
        self.assertFalse(menage.worktree_state(wt)["integrated"])
        _git("merge", "-q", "--squash", "lot", cwd=main)
        _git("commit", "-q", "-m", "lot (#1)", cwd=main)
        _git("push", "-q", "origin", "main", cwd=main)
        self.assertTrue(menage.worktree_state(wt)["integrated"])

    def test_attribution_au_seul_agent_qui_l_a_cree(self):
        a1 = os.path.join(self.base, "a1")
        a2 = os.path.join(self.base, "a1", "imbrique-a2")
        sous = os.path.join(a1, ".claude", "worktrees", "agent-x")
        self.assertTrue(menage.owns_worktree(sous, a1, []))
        # hors du dossier de l'agent : un autre agent du même dépôt
        self.assertFalse(menage.owns_worktree(os.path.join(self.base, "relecture"), a1, []))
        self.assertFalse(menage.owns_worktree(a1, a1, []))
        # même dossier partagé avec un autre agent : dans le doute, non
        self.assertFalse(menage.owns_worktree(sous, a1, [a1]))
        # sous le dossier d'un autre agent plus proche du worktree : à lui
        self.assertFalse(menage.owns_worktree(os.path.join(a2, "wt"), a1, [a2]))
        self.assertTrue(menage.owns_worktree(sous, a1, [a2, os.path.join(self.base, "b")]))
        self.assertFalse(menage.owns_worktree(sous, None, []))

    def test_lot_nomme_par_la_branche_ou_le_dossier(self):
        """#1665 : les noms qui rattachent un worktree frère à son lot."""
        self.assertEqual(menage.cited_lot("lot/1665-menage-worktrees"), 1665)
        self.assertEqual(menage.cited_lot("agent/deepseek3-1201-bench"), 1201)
        self.assertEqual(menage.cited_lot("review/mty-nxl-rel-01-571-ota"), 571)
        self.assertEqual(menage.cited_lot("lot/750"), 750)
        self.assertIsNone(menage.cited_lot("main"))
        self.assertIsNone(menage.cited_lot("relecture"))
        self.assertEqual(menage.named_lot("ameesh-dev1-131", "ameesh-dev1"), 131)
        self.assertEqual(menage.named_lot("deepseek3-1665", "deepseek3"), 1665)
        self.assertIsNone(menage.named_lot("deepseek3-tmp", "deepseek3"))
        self.assertIsNone(menage.named_lot("nexlink-ds3-431", "deepseek3"))
        # le nom de l'agent porte un chiffre : c'est le dernier groupe qui
        # compte sous agent/review (l'id du lot, pas le suffixe de l'agent)
        self.assertEqual(menage.cited_lot("agent/mty-nxl-impl-04-1229-x"), 1229)
        self.assertIsNone(menage.cited_lot("agent/nx-compute-runner-n-n"))
        self.assertIsNone(menage.cited_lot(""))

    def test_le_dossier_nomme_le_lot_par_les_conventions_des_clones(self):
        """#3266 : le dossier d'un worktree se lit avec les conventions du
        ménage des clones (#2743), au-delà de `<agent>-<id>` : préfixe de
        projet (`nexlink-<agent>-<lot>`), `.wt<lot>`, `<agent>.wt<lot>`. La
        convention stricte garde sa priorité (un suffixe n'est pas un lot) ;
        un fragment de SHA n'est pas un lot ; un nom inconnu ne nomme rien."""
        self.assertEqual(menage.worktree_named_lots("/w/nexlink-a1-431", ("a1",)),
                         ("a1", [431]))
        self.assertEqual(menage.worktree_named_lots("/w/a1-431", ("a1",)), ("a1", [431]))
        # `a1-431-22` nomme #431 (convention stricte), pas #22
        self.assertEqual(menage.worktree_named_lots("/w/a1-431-22", ("a1",)),
                         ("a1", [431]))
        self.assertEqual(menage.worktree_named_lots("/w/mty-amh-impl-08.wt3219",
                                                    ("mty-amh-impl-08",)),
                         ("mty-amh-impl-08", [3219]))
        self.assertEqual(menage.worktree_named_lots("/w/.wt1349-r1", ("a1",)),
                         ("", [1349]))
        # un nombre collé à un fragment de SHA n'est pas un lot (relecture)
        self.assertEqual(menage.worktree_named_lots("/w/wt3220-ca47", ("a1",)),
                         ("", [3220]))
        self.assertEqual(menage.worktree_named_lots("/w/review-3220-ab1234cd", ("a1",)),
                         ("", [3220]))
        self.assertEqual(menage.worktree_named_lots("/w/nexlink-a2-431", ("a1", "a2")),
                         ("a2", [431]))
        self.assertEqual(menage.worktree_named_lots("/w/inconnu", ("a1",)), ("", []))
        # sans registre, aucun agent n'est nommé : les marqueurs de convention
        # restent des candidats (vérifiés ensuite), mais un nom qui ne dit
        # rien (`a1-431`, une date) n'est pas lu.
        self.assertEqual(menage.worktree_named_lots("/w/wt-431", ()), ("", [431]))
        self.assertEqual(menage.worktree_named_lots("/w/a1-431", ()), ("", []))
        self.assertEqual(menage.worktree_named_lots("/w/release-2026-10-23", ("a1",)),
                         ("", []))

    def test_lot_of_worktree_reads_the_folder_conventions_only_when_assigned(self):
        """#3266 : `.wt<lot>` et `nexlink-<agent>-<lot>` rattachent le worktree
        au lot SEULEMENT si ce lot est assigné à l'agent ; un dossier qui
        nomme un autre agent du registre ne prouve rien pour celui-ci."""
        class _Registre:
            ROWS = {1132: {"assignee": "a1"}, 999: {"assignee": "a2"},
                    431: {"assignee": "a1"}, 22: {"assignee": "a1"},
                    432: {"assignee": "a1"}, 433: {"assignee": "a1"}}

            def get(self, ident):
                return self.ROWS.get(ident)

        class _Base:
            work = _Registre()

        with mock.patch.object(menage.storage, "of", return_value=_Base()):
            self.assertEqual(menage.lot_of_worktree(
                None, "a1", "/w/a1/.wt1132", ""), "1132")
            self.assertIsNone(menage.lot_of_worktree(
                None, "a1", "/w/a1/.wt999", ""))
            self.assertEqual(menage.lot_of_worktree(
                None, "a1", "/w/nexlink-a1-1132", "", agents=("a1", "a2")), "1132")
            self.assertIsNone(menage.lot_of_worktree(
                None, "a1", "/w/nexlink-a2-1132", "", agents=("a1", "a2")))
            self.assertIsNone(menage.lot_of_worktree(
                None, "a1", "/w/a1/inconnu", ""))
            # convention stricte : `a1-431-22` suit #431, pas #22
            self.assertEqual(menage.lot_of_worktree(
                None, "a1", "/w/a1-431-22", ""), "431")
            # plusieurs lots nommés : doute, la passe vérifie tous les candidats
            self.assertIsNone(menage.lot_of_worktree(
                None, "a1", "/w/wt-432-433", ""))
            # branche et dossier doivent nommer le MÊME lot unique
            self.assertEqual(menage.lot_of_worktree(
                None, "a1", "/w/nexlink-a1-431", "lot/431"), "431")
            self.assertIsNone(menage.lot_of_worktree(
                None, "a1", "/w/nexlink-a1-431", "lot/432"))

    def test_cited_lots_are_tried_in_order(self):
        """#1694: a branch citing several numeric groups lists its candidates
        in trial order — last group first under agent/review, first group
        first under lot/work."""
        self.assertEqual(menage.cited_lots("review/claude2-t3-unit-459-460-r2"),
                         [460, 459])
        self.assertEqual(menage.cited_lots("agent/mty-nxl-impl-04-1229-x"), [1229])
        self.assertEqual(menage.cited_lots("lot/1665-menage-worktrees"), [1665])
        self.assertEqual(menage.cited_lots("main"), [])
        # cited_lot keeps its single-candidate role: the first candidate.
        self.assertEqual(menage.cited_lot("review/claude2-t3-unit-459-460-r2"), 460)

    def test_le_chiffre_du_nom_d_agent_n_est_pas_un_lot(self):
        """#2182 N2: under `agent/`/`review/`, the number an agent name carries
        (`mty-amh-impl-02`) is not a cited lot — dropped with the agent name
        when it is known, and never read from a zero-padded group (`-02`);
        #1694's real candidates are unchanged."""
        self.assertEqual(
            menage.branch_agent("review/mty-amh-impl-02-1234",
                                ("mty-amh", "mty-amh-impl-02")), "mty-amh-impl-02")
        self.assertEqual(
            menage.branch_agent("agent/mty-nxl-impl-04-1229-x",
                                ("mty-nxl-impl-04",)), "mty-nxl-impl-04")
        self.assertEqual(menage.branch_agent("review/mty-amh-impl-02-1234",
                                             ("mty-amh-impl-01",)), "")
        self.assertEqual(menage.branch_agent("lot/2182-x", ("mty-amh-impl-02",)), "")
        self.assertEqual(
            menage.cited_lots("review/mty-amh-impl-02-1234",
                              agent="mty-amh-impl-02"), [1234])
        self.assertEqual(menage.cited_lots("review/mty-amh-impl-02-1234"), [1234])
        self.assertEqual(menage.cited_lots("review/mty-nxl-rel-01-571-ota"), [571])
        self.assertEqual(menage.cited_lots("review/paquet-h-0115-correctif-508"), [508])
        # #1694 survives: several real lots are still tried, last one first.
        self.assertEqual(
            menage.cited_lots("review/claude2-t3-unit-459-460-r2", agent="claude2"),
            [460, 459])

    def test_4234_les_numeros_de_contexte_ne_sont_pas_des_lots(self):
        """#4234 : les nombres de contexte d'un nom de branche (`am10-16`,
        `am12-3`, `offload-agents-3`) ne sont pas des lots — lus comme tels,
        ils nommaient un lot inexistant qui interdisait à jamais le retrait
        d'un worktree de lot livré (11 arbres, 5,7 Gio sur agents-1). La
        première suite numérique reste le lot ; une suite suivante n'en est
        un que si son plus grand numéro atteint `RELATED_LOT_MIN` (#3266 :
        `lot/431-432` sert bien deux lots)."""
        # les branches réelles qui bloquaient les plus gros arbres
        self.assertEqual(
            menage.cited_lots("agent/claude2-4101-am10-16-notification-level",
                              agent="claude2"), [4101])
        self.assertEqual(
            menage.cited_lots("agent/deepseek3-4129-am12-3-webhook-endpoint",
                              agent="deepseek3"), [4129])
        self.assertEqual(
            menage.cited_lots("lot/4011-offload-agents-3"), [4011])
        self.assertEqual(
            menage.cited_lots("agent/ameesh-dev1-3719-rt-w5",
                              agent="ameesh-dev1"), [3719])
        self.assertEqual(
            menage.cited_lots("agent/claude1-3703-dh-key-2",
                              agent="claude1"), [3703])
        # une suite liée réelle est toujours vérifiée (#3266)
        self.assertEqual(menage.cited_lots("lot/431-432"), [431, 432])
        self.assertEqual(
            menage.cited_lots("agent/codex3-compute-298-299",
                              agent="codex3"), [298, 299])
        self.assertEqual(
            menage.cited_lots("agent/deepseek1-3284-dossier-3155-g2-pass2",
                              agent="deepseek1"), [3284, 3155])
        # un groupe complété d'un zéro n'est jamais un lot, sous aucun préfixe
        self.assertEqual(
            menage.cited_lots("lot/1305-sauvegardes-0600"), [1305])
        # review : la dernière suite utile, sans lire `t3` ni `r2`
        self.assertEqual(
            menage.cited_lots("review/codex1-4129-webhook-ingress"), [4129])
        self.assertEqual(
            menage.cited_lots("review/claude2-t3-unit-459-460-r2"),
            [460, 459])
        # sans aucune suite « grande », un nom de lots anciens (< 100) garde
        # toutes ses suites, comme avant #4234
        self.assertEqual(menage.cited_lots("lot/67-fix-89"), [67, 89])
        self.assertEqual(menage.cited_lots("review/a2-42-fix-56",
                                          agent="a2"), [56, 42])
        # un premier lot ancien reste vérifié à côté d'une suite liée grande
        self.assertEqual(menage.cited_lots("agent/a2-16-fix-431",
                                          agent="a2"), [16, 431])
        # une date n'est pas une suite de lots ; le petit numéro d'étiquette
        # qui suit un lot lié n'en est pas un
        self.assertEqual(
            menage.cited_lots("review/ep7-3024-preuves-2026-10-24",
                              agent="ep7"), [3024])
        self.assertEqual(
            menage.cited_lots("agent/claude2-3285-dossier-3155-4-4",
                              agent="claude2"), [3285, 3155])

    def test_the_agent_number_is_not_a_lot_to_attach_to(self):
        """#2182 N2: `lot_of_worktree` must not attach a review copy to the
        lot numbered like its agent (`review/reviewer-02-999` is not #2), even
        when #2 is assigned to that agent."""
        class _Registre:
            ROWS = {999: {"assignee": "a2"}, 2: {"assignee": "reviewer-02"}}

            def get(self, ident):
                return self.ROWS.get(ident)

        class _Base:
            work = _Registre()

        with mock.patch.object(menage.storage, "of", return_value=_Base()):
            self.assertIsNone(menage.lot_of_worktree(
                None, "reviewer-02", "/tmp/reviewer-02-999", "review/reviewer-02-999",
                branch_owner="reviewer-02"))

        class _RegistreSansZero:
            ROWS = {999: {"assignee": "a2"}, 2: {"assignee": "a1"}}

            def get(self, ident):
                return self.ROWS.get(ident)

        class _BaseSansZero:
            work = _RegistreSansZero()

        with mock.patch.object(menage.storage, "of", return_value=_BaseSansZero()):
            # without the branch's agent, the zero-padded `-02` is still not
            # a lot: nothing attaches this copy of #999 to #2.
            self.assertIsNone(menage.lot_of_worktree(
                None, "a1", "/tmp/a1-999", "review/reviewer-02-999"))
            # but a plain second group is a candidate (#1694): #2 wins here.
            self.assertEqual(menage.lot_of_worktree(
                None, "a1", "/tmp/a1-999", "review/reviewer-2-999"), "2")

    def test_the_right_lot_wins_among_the_candidates(self):
        """#1694: the first candidate owned by the agent wins; a candidate
        owned by another agent proves nothing, and the folder name is only
        read when the branch cites no number (#1665)."""
        class _Registre:
            ROWS = {460: {"assignee": "a2"}, 459: {"assignee": "a1"},
                    12: {"assignee": "a1"}, 999: {"assignee": "a2"}}

            def get(self, ident):
                return self.ROWS.get(ident)

        class _Base:
            work = _Registre()

        branche = "review/claude2-t3-unit-459-460-r2"
        with mock.patch.object(menage.storage, "of", return_value=_Base()):
            self.assertEqual(
                menage.lot_of_worktree(None, "a1", "/tmp/a1-459", branche), "459")
            # both candidates are the agent's: the trial order decides (460).
            _Registre.ROWS[460] = {"assignee": "a1"}
            self.assertEqual(
                menage.lot_of_worktree(None, "a1", "/tmp/a1-459", branche), "460")
            # a cited lot of another agent does not fall back to the folder.
            self.assertIsNone(
                menage.lot_of_worktree(None, "a1", "/tmp/a1-459", "lot/999-x"))
            # no number cited: the folder names the lot, as in #1665.
            self.assertEqual(menage.lot_of_worktree(None, "a1", "/tmp/a1-12", ""), "12")
            self.assertEqual(menage.lot_of_worktree(None, "a1", "/tmp/a1-12", "main"),
                             "12")

    def test_registre_illisible_ne_prouve_rien(self):
        """#1665 : un registre des lots illisible ne rattache rien (None),
        sans faire échouer le ménage."""
        class _Cassee:
            @property
            def work(self):
                raise RuntimeError("registre illisible")

        with mock.patch.object(menage.storage, "of", return_value=_Cassee()):
            self.assertIsNone(menage.lot_of_worktree(None, "a1", "/tmp/a1-12", "lot/12-x"))
            self.assertIsNone(menage.lot_of_worktree(None, "a1", "/tmp/a1-12", ""))

    def test_processus_dans_le_worktree(self):
        """#1805 : la table des processus est injectée (déterministe) ; la
        lecture réelle de /proc a ses tests (`tests/test_l63_platform.py`).
        Le cas d'un processus vivant dans le worktree reste couvert."""
        wt = os.path.join(self.base, "wt")
        os.makedirs(wt)
        vus: list[dict] = []
        patcher = mock.patch.object(platform, "holders", lambda *a, **k: list(vus))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertFalse(menage.worktree_in_use(wt))
        proc = subprocess.Popen(["sleep", "30"], cwd=wt)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        vus.append({"pid": proc.pid, "why": "working directory", "cmd": "sleep 30"})
        self.assertTrue(menage.worktree_in_use(wt))

    def test_processus_dans_le_worktree_os_muet(self):
        """L63 : si l'OS ne dit pas qui travaille dedans, le worktree attend."""
        from unittest import mock
        from ameesh import platform
        wt = os.path.join(self.base, "wt-muet")
        os.makedirs(wt)
        muet = platform.NotAvailable("détenteurs d'un fichier", "test", "macos")
        with mock.patch.object(platform, "holders", side_effect=muet):
            self.assertTrue(menage.worktree_in_use(wt))

    def test_processus_illisible_nomme_par_l_hote_n_est_pas_un_doute(self):
        """L230/#1807 : un processus illisible que l'hôte nomme dans
        `unreadable_ok` n'est plus un doute ; sans allow, la réponse reste OUI
        (fail-closed)."""
        wt = os.path.join(self.base, "wt-allow")
        os.makedirs(wt)

        def holders(path, **kw):
            if "gpg-agent" in (kw.get("allow") or ()):
                return []
            raise platform.NotAvailable("détenteurs d'un fichier", "test", "linux")

        with mock.patch.object(platform, "holders", side_effect=holders):
            self.assertTrue(menage.worktree_in_use(wt))
            self.assertFalse(menage.worktree_in_use(wt, allow=("gpg-agent",)))


# ==========================================================================
# conteneurs (L73) : étiquetage, liste, suppression des seuls conteneurs d'un tour
# ==========================================================================

class _FauxMoteur:
    binary = "/usr/bin/docker"

    def __init__(self, rows, running=None):
        self.rows = rows
        #: ids considérés EN COURS (`ps` sans `-a`) ; None : tous le sont
        self.running = running
        self.removed: list[str] = []

    def list_all(self, stopped=False):
        out = [dict(r) for r in self.rows if r["id"] not in self.removed]
        if not stopped and self.running is not None:
            out = [r for r in out if r["id"] in self.running]
        return out

    def remove(self, ident):
        self.removed.append(ident)
        return True, ""


class ConteneursTest(_Tmp):
    def test_etiquettes_et_liste_docker(self):
        self.assertEqual(containers.turn_labels("t1", "a1", "12"),
                         "ameesh.turn=t1,ameesh.agent=a1,ameesh.lot=12")
        self.assertEqual(containers.parse_labels("a=1,b=x=y"), {"a": "1", "b": "x=y"})
        self.assertEqual(containers.parse_labels({"a": 1}), {"a": "1"})
        appels = []

        def run(argv, **_kw):
            appels.append(argv)
            sortie = "\n".join(json.dumps(r) for r in (
                {"ID": "abc", "Names": "pg-test", "Image": "postgres:16",
                 "Labels": "ameesh.turn=t1,ameesh.agent=a1", "Status": "Up 2 hours"},
                {"ID": "def", "Names": "autre", "Image": "redis", "Labels": ""}))
            return subprocess.CompletedProcess(argv, 0, sortie, "")

        moteur = containers.Runtime(binary="docker", run=run)
        rows = moteur.list_all()
        self.assertEqual([r["labels"].get("ameesh.turn") for r in rows], ["t1", None])
        self.assertEqual(rows[0]["name"], "pg-test")
        self.assertEqual(moteur.remove("abc"), (True, ""))
        self.assertEqual(appels[-1], ["docker", "rm", "-f", "-v", "abc"])

    def test_ps_a_est_le_seul_releve_des_arretes(self):
        """#1547 (N2a) : `stopped=True` ajoute `-a` à l'argv réel de `ps` —
        un conteneur arrêté n'est vu que par ce relevé ; sans lui, la lecture
        reste celle des conteneurs en cours."""
        appels = []

        def run(argv, **_kw):
            appels.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        moteur = containers.Runtime(binary="docker", run=run)
        self.assertEqual(moteur.list_all(stopped=True), [])
        self.assertEqual(appels[0], ["docker", "ps", "--no-trunc", "-a",
                                     "--format", "{{json .}}"])
        moteur.list_all()
        self.assertEqual(appels[1], ["docker", "ps", "--no-trunc",
                                     "--format", "{{json .}}"])

    def test_le_script_etiquette_run_create_et_reprend_les_volumes_anonymes(self):
        faux = os.path.join(self.base, "vrai-bin")
        os.makedirs(faux)
        with open(os.path.join(faux, "docker"), "w") as fh:
            fh.write("#!/bin/sh\nfor a in \"$@\"; do printf '[%s]' \"$a\"; done\n")
        os.chmod(os.path.join(faux, "docker"), 0o755)
        shims = os.path.join(self.base, "shims")
        env = containers.install_shims(shims, faux)
        self.assertTrue(env["PATH"].startswith(shims + os.pathsep))
        self.assertEqual(env["AMEESH_REAL_DOCKER"], os.path.join(faux, "docker"))
        tourne = dict(os.environ, **env,
                      AMEESH_CONTAINER_LABELS="ameesh.turn=t1,ameesh.agent=a 1")

        def docker(*args):
            return subprocess.run([os.path.join(shims, "docker"), *args], env=tourne,
                                  capture_output=True, text=True, check=True).stdout

        self.assertEqual(docker("run", "-d", "img"),
                         "[run][--label][ameesh.turn=t1][--label][ameesh.agent=a 1][-d][img]")
        self.assertEqual(docker("container", "create", "img"),
                         "[container][create][--label][ameesh.turn=t1][--label]"
                         "[ameesh.agent=a 1][img]")
        self.assertEqual(docker("ps", "-a"), "[ps][-a]")
        # #4560 : le volume anonyme d'un conteneur qu'une suite supprime part
        # avec lui ; un `-v` déjà demandé n'est pas doublé.
        self.assertEqual(docker("rm", "x*"), "[rm][-v][x*]")
        self.assertEqual(docker("container", "rm", "-f", "x"),
                         "[container][rm][-v][-f][x]")
        self.assertEqual(docker("rm", "-f", "-v", "x"), "[rm][-f][-v][x]")
        # une construction SANS tag ne produit qu'une image dangling : elle
        # porte la durée de vie de test ; une construction taguée, artefact
        # voulu, n'est pas déclarée jetable.
        self.assertEqual(docker("build", "."),
                         "[build][--label][ameesh.lifetime=test][.]")
        self.assertEqual(docker("image", "build", "."),
                         "[image][build][--label][ameesh.lifetime=test][.]")
        self.assertEqual(docker("build", "-t", "img", "."),
                         "[build][-t][img][.]")
        self.assertEqual(docker("image", "build", "--tag=img", "."),
                         "[image][build][--tag=img][.]")
        self.assertEqual(containers.install_shims(shims, os.path.join(self.base, "vide")), {})


# ==========================================================================
# mesure et contre-pression
# ==========================================================================

class TmpfsTest(unittest.TestCase):
    def test_part_et_franchissement(self):
        self.assertEqual(resources.parse_fraction("80%"), 0.8)
        self.assertEqual(resources.parse_fraction(0.5), 0.5)
        for mauvais in (0, 1.5, "abc", "150%", True, None):
            self.assertIsNone(resources.parse_fraction(mauvais))
        limits = resources.thresholds(None)
        self.assertEqual(limits["max_tmpfs_used"], resources.DEFAULT_MAX_TMPFS_USED)
        lecture = {"tmp_path": "/tmp", "tmp_fstype": "tmpfs", "tmp_size_bytes": 12 * GIB,
                   "tmp_used_bytes": int(10.2 * GIB)}
        found = resources.breaches(lecture, limits)
        self.assertEqual([b["key"] for b in found], ["max_tmpfs_used"])
        self.assertFalse(found[0]["critical"])          # 85 % < 90 %
        lecture["tmp_used_bytes"] = int(11.5 * GIB)
        self.assertTrue(resources.breaches(lecture, limits)[0]["critical"])
        # un /tmp sur disque relève du disque libre, pas de ce seuil
        lecture["tmp_fstype"] = "ext4"
        self.assertEqual(resources.breaches(lecture, limits), [])
        self.assertEqual(resources.fmt_value("max_tmpfs_used", 0.8), "80 %")

    def test_fs_usage(self):
        out = resources.fs_usage(tempfile.gettempdir())
        self.assertGreater(out["tmp_size_bytes"], 0)
        self.assertIsNotNone(out["tmp_fstype"])
        self.assertIsNone(resources.fs_usage("/chemin/absent")["tmp_size_bytes"])


class SystemTmpNettoyageTest(unittest.TestCase):
    def test_une_variable_absente_redevient_absente_apres_nettoyage(self):
        """#2532 : le nettoyage d'un test rend `AMEESH_SYSTEM_TMP` absente
        quand elle l'était. Le `old or ""` d'avant la laissait vide — vide
        vaut `/tmp` en produit, mais une variable vide n'est pas une variable
        absente et fausse l'isolation du run suivant."""
        original = os.environ.pop("AMEESH_SYSTEM_TMP", None)
        self.addCleanup(lambda: os.environ.__setitem__("AMEESH_SYSTEM_TMP", original)
                        if original is not None
                        else os.environ.pop("AMEESH_SYSTEM_TMP", None))
        # les nettoyages sont LIFO : la restauration part en dernier, la
        # vérification juste après le nettoyage du helper
        self.addCleanup(self.assertNotIn, "AMEESH_SYSTEM_TMP", os.environ)
        self.assertNotIn("AMEESH_SYSTEM_TMP", os.environ)
        _pose_system_tmp(self, "/tmp/autre")
        self.assertEqual(os.environ["AMEESH_SYSTEM_TMP"], "/tmp/autre")
        self.doCleanups()


# ==========================================================================
# avec la base : tour, passage, CLI, alertes
# ==========================================================================

class MenageDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees", "host_resources"):
            self.db.execute("DELETE FROM %s" % table)
        # #4754 : le passage de la CLI re-épingle les unités de veille de
        # l'hôte ; un test n'en lit ni n'en réécrit aucune réelle.
        units = tempfile.mkdtemp(prefix="units-", dir=self.tmp)
        patcher = mock.patch.dict(os.environ, {"AMEESH_SYSTEMD_USER_DIR": units})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfgl = _Cfg(self.state)
        self.cfgl.host = self.cfg.host

    def _lot(self, title="lot") -> int:
        return int(work.add(self.db, title=title, app="ameesh")["id"])

    def test_tour_enregistre_worktree_signale_tmp_et_vide_a_la_session_neuve(self):
        main = _repo(self.tmp)
        systeme = os.path.join(self.tmp, "tmp-systeme")
        os.makedirs(systeme)
        pol = menage.Policy(orphan_min_size=4000)
        _pose_system_tmp(self, systeme)
        mine = menage.turn_env(self.cfgl, pol, "a1", {})["TMPDIR"]
        _write(os.path.join(mine, "de-la-session-precedente"), 100)
        tour = menage.Turn.begin(self.cfgl, pol, "a1", main, new_session=True, base_env={})
        self.assertFalse(os.path.exists(os.path.join(mine, "de-la-session-precedente")))
        self.assertEqual(tour.env["TMPDIR"], mine)
        # pendant le tour : un worktree de sous-agent (sous le dossier de
        # l'agent), un worktree hors de son dossier, et un clone dans /tmp
        sous = os.path.join(main, ".claude", "worktrees", "agent-x")
        _git("worktree", "add", "-q", "--detach", sous, cwd=main)
        wt = os.path.join(self.tmp, "relecture")
        _git("worktree", "add", "-q", "--detach", wt, cwd=main)
        _write(os.path.join(systeme, "clone", "f"), 5000)
        _write(os.path.join(mine, "garde-pour-la-session"), 100)
        entries = tour.end(lot="7", turn_id="t" * 32, db=self.db, host="pc")
        kinds = {(e["kind"], e["action"]) for e in entries}
        self.assertIn(("tmp", "deleted"), kinds)
        self.assertIn(("worktree", "registered"), kinds)
        self.assertIn(("orphan", "signaled"), kinds)
        self.assertTrue(os.path.exists(os.path.join(systeme, "clone", "f")))   # jamais supprimé
        rows = storage.of(self.db).housekeeping.worktrees("pc")
        self.assertEqual(sorted((r["path"], r["agent"], r["lot"]) for r in rows),
                         sorted([(sous, "a1", "7"), (wt, "a1", None)]))
        detail = {r["path"]: r["detail"] for r in rows}
        self.assertTrue(detail[wt].startswith(menage.UNATTRIBUTED), detail)
        self.assertFalse(detail[sous].startswith(menage.UNATTRIBUTED), detail)
        # session reprise : le dossier temporaire est conservé
        menage.Turn.begin(self.cfgl, pol, "a1", main, new_session=False, base_env={})
        self.assertTrue(os.path.exists(os.path.join(mine, "garde-pour-la-session")))

    def test_worktree_a_la_racine_du_tmp_d_un_autre_mesh_n_est_jamais_supprime(self):
        """#2326 : un worktree créé par un agent à la racine partagée du tmp
        (nom inconnu de CE registre, sans fichier de propriétaire à nous) n'est
        jamais supprimé, même très ancien : un registre vide, partiel ou ancien
        ne doit rien détruire."""
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        root = os.path.join(troot, "nexlink-dev1-0123abcd-review")
        os.makedirs(root)
        _write(os.path.join(root, ".git"), 10)
        _write(os.path.join(root, "notes.txt"), 10)
        loin = time.time() + 30 * 24 * 3600
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", now=loin)
        self.assertTrue(os.path.isdir(root))
        self.assertNotIn("tmp %s kept: not owned by this mesh" % root,
                         rapport["skipped"])
        self.assertIn(root, [row["path"] for row in rapport["unowned"]])
        self.assertEqual(len([e for e in rapport["entries"]
                              if e.get("path") == root and e.get("action") == "unowned"]), 1)
        self.assertNotIn(root, [p["path"] for p in rapport["plan"]
                                if p.get("action") == "suppression"])

    def test_dossier_tmp_d_un_agent_absent_mais_a_nous_est_encore_supprime(self):
        """#2326 : le nettoyage légitime reste — un dossier créé par ce mesh
        pour un agent qui n'est plus au registre part après l'inactivité."""
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        root = os.path.join(troot, "ancien-agent")
        os.makedirs(root)
        menage._write_owner(troot, "ancien-agent", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        _write(os.path.join(root, "reste.txt"), 10)
        loin = time.time() + 30 * 24 * 3600
        menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", now=loin)
        self.assertFalse(os.path.exists(root))

    @unittest.skipUnless(platform.is_linux(),
                         "#2704 : les chemins tenus se lisent dans /proc (Linux)")
    def test_dossier_tmp_d_un_agent_absent_tenu_par_un_processus_est_garde(self):
        """#2704 : un dossier ancien qu'un processus vivant utilise (ici son
        dossier courant) n'est pas supprimé, même sans worktree dedans."""
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        root = os.path.join(troot, "ancien-agent")
        os.makedirs(root)
        menage._write_owner(troot, "ancien-agent", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        _write(os.path.join(root, "reste.txt"), 10)
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(60)"], cwd=root)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        loin = time.time() + 30 * 24 * 3600
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                  now=loin)
        self.assertTrue(os.path.isdir(root))
        self.assertTrue(any("kept: held by process" in note
                            for note in rapport["skipped"]), rapport["skipped"])

    def test_processus_tmp_verifie_avant_retrait_des_worktrees(self):
        """#2704 : récupérer un worktree le supprime déjà ; la garde des
        fichiers projetés doit donc précéder cette récupération."""
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        root = os.path.join(troot, "ancien-agent")
        os.makedirs(root)
        menage._write_owner(troot, "ancien-agent", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        procs = platform.UserPaths()
        procs.paths[os.path.join(root, "copie", "lib.node")] = "123 electron"
        with mock.patch.object(platform, "user_paths", return_value=procs), \
                mock.patch.object(menage, "_git_copies_report",
                                  return_value=([os.path.join(root, "copie")], "")), \
                mock.patch.object(menage, "tmp_git_work", return_value=""), \
                mock.patch.object(menage, "recover_tmp_git_copies",
                                  return_value=(True, "", [])) as recover:
            menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                            now=time.time() + 30 * 24 * 3600)
        recover.assert_not_called()
        self.assertTrue(os.path.isdir(root))

    @unittest.skipUnless(platform.is_linux(),
                         "#2704 : les chemins tenus se lisent dans /proc (Linux)")
    def test_dossier_tmp_tenu_par_un_processus_n_est_pas_evince(self):
        """#2704 : le quota ne libère pas les fichiers d'un dossier temporaire
        qu'un processus vivant utilise (AppImage extraite et lancée)."""
        self.register("a1", "claude")
        pol = menage.Policy(tmp_quota=1024)
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        root = os.path.join(troot, "a1")
        os.makedirs(root)
        menage._write_owner(troot, "a1", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        gros = os.path.join(root, "gros")
        _write(gros, 100000, age_s=10 * 3600)
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(60)"], cwd=root)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t")
        self.assertTrue(os.path.exists(gros))
        self.assertTrue(any("kept: held by process" in note
                            for note in rapport["skipped"]), rapport["skipped"])

    def test_dossier_de_repli_avec_une_copie_git_n_est_pas_supprime(self):
        """#2326 : même à nous, un dossier de repli qui porte une copie git
        (worktree créé dans le dossier temporaire) est gardé et dit."""
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        root = os.path.join(troot, "a1+abcd1234")
        os.makedirs(root)
        menage._write_owner(troot, "a1+abcd1234", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        _write(os.path.join(root, ".git"), 10)
        loin = time.time() + 30 * 24 * 3600
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", now=loin)
        self.assertTrue(os.path.isdir(root))
        kept = [note for note in rapport["skipped"]
                if note.startswith("tmp %s kept: git work —" % root)]
        self.assertEqual(len(kept), 1)
        self.assertIn("git metadata unreadable", kept[0])

    def test_worktree_propre_et_pousse_dans_un_orphelin_possede_est_recupere(self):
        """#2493 N2 : une copie git propre et poussée dans un dossier tmp
        possédé n'est plus gardée à jamais : elle est récupérée par
        `git worktree remove` (sans --force), puis le dossier part ; la branche
        reste."""
        _sans_processus(self)
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        main = _repo(self.tmp)
        root = os.path.join(troot, "a1+abcd9999")
        os.makedirs(root)
        menage._write_owner(troot, "a1+abcd9999", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        wt = os.path.join(root, "wt")
        _git("worktree", "add", "-q", wt, "-b", "wt-recup", cwd=main)
        _git("push", "-q", "origin", "wt-recup", cwd=main)
        _age_tree(root, 3 * 3600)
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                  now=time.time() + 3 * 3600)
        self.assertFalse(os.path.isdir(root), rapport["skipped"])
        deleted = [e for e in rapport["entries"]
                   if e.get("path") == root and e.get("action") == "deleted"]
        self.assertEqual(len(deleted), 1)
        self.assertIn("git worktrees recovered", deleted[0]["detail"])
        self.assertNotIn(wt, _git("worktree", "list", cwd=main))
        self.assertIn("wt-recup", _git("branch", "--list", "wt-recup", cwd=main))

    def test_worktree_sale_dans_un_orphelin_possede_est_garde(self):
        """#2493 N2 : aucun travail non commité n'est détruit : un worktree
        sale garde le dossier, avec la raison et la commande d'inspection."""
        _sans_processus(self)
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        main = _repo(self.tmp)
        root = os.path.join(troot, "a1+abcd9998")
        os.makedirs(root)
        menage._write_owner(troot, "a1+abcd9998", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        wt = os.path.join(root, "wt")
        _git("worktree", "add", "-q", wt, "-b", "wt-sale", cwd=main)
        _write(os.path.join(wt, "brouillon.txt"), 10)
        _age_tree(root, 3 * 3600)
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                  now=time.time() + 3 * 3600)
        self.assertTrue(os.path.isdir(root))
        kept = [note for note in rapport["skipped"]
                if note.startswith("tmp %s kept: git work —" % root)]
        self.assertEqual(len(kept), 1)
        self.assertIn("git -C", kept[0])
        self.assertNotIn(root, [p["path"] for p in rapport["plan"]
                                if p.get("action") == "suppression"])

    def test_clone_avec_branche_locale_non_poussee_est_garde(self):
        """#2493 (relecture dev7, cx1) : un clone emporte ses branches
        locales, son stash, ses fichiers ignorés et casse ses worktrees liés ;
        HEAD propre et poussé ne suffit pas — chacun garde le dossier."""
        _sans_processus(self)
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        _repo(self.tmp)
        for nom, preparer in (
                ("a1+abcd9997", lambda c: (
                    _git("checkout", "-q", "-b", "travail", cwd=c),
                    _git("commit", "-q", "--allow-empty", "-m", "jamais pousse", cwd=c),
                    _git("checkout", "-q", "main", cwd=c))),
                ("a1+abcd9996", lambda c: (
                    _write(os.path.join(c, "brouillon.txt"), 10),
                    _git("stash", "push", "-q", "-u", cwd=c))),
                # relecture cx1 : un fichier ignoré (.env) partirait avec le clone
                ("a1+abcd9995", lambda c: (
                    _write(os.path.join(c, ".git", "info", "exclude"), 0),
                    open(os.path.join(c, ".git", "info", "exclude"), "w").write("*.env\n"),
                    _write(os.path.join(c, "private.env"), 10))),
                # un clone qui porte un autre worktree lié ailleurs
                ("a1+abcd9994", lambda c: _git(
                    "worktree", "add", "-q", c + "-lie", "-b", "lie", cwd=c))):
            root = os.path.join(troot, nom)
            os.makedirs(root)
            menage._write_owner(troot, nom, menage.mesh_key(self.cfgl),
                                menage.ORIGIN_CREATED)
            clone = os.path.join(root, "clone")
            subprocess.run(["git", "clone", "-q", os.path.join(self.tmp, "origin.git"),
                            clone], check=True, capture_output=True)
            preparer(clone)
            _age_tree(root, 3 * 3600)
            rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                      now=time.time() + 3 * 3600)
            self.assertTrue(os.path.isdir(clone), nom)
            kept = [note for note in rapport["skipped"]
                    if note.startswith("tmp %s kept: git work —" % root)]
            self.assertEqual(len(kept), 1, rapport["skipped"])

    def test_worktree_dont_le_depot_commun_est_dans_le_tmp_est_garde(self):
        _sans_processus(self)
        main = _repo(self.tmp)
        root = os.path.join(self.tmp, "tmp-recovery")
        os.makedirs(root)
        bare = os.path.join(root, "bare.git")
        subprocess.run(["git", "clone", "-q", "--bare", main, bare],
                       check=True, capture_output=True)
        _git("fetch", "-q", "origin", "main:refs/remotes/origin/main", cwd=bare)
        # #2525 : le dépôt nu est désormais vu lui aussi, mais le worktree
        # dont le dépôt commun vit dans l'entrée passe en premier (gardes
        # plus précises) et donne la raison.
        self.assertIn(bare, menage._git_copies(root))
        wt = os.path.join(root, "wt")
        _git("worktree", "add", "-q", wt, "main", cwd=bare)
        self.assertIn("common repository lives inside", menage.tmp_git_work(root))

    def test_clone_dont_le_remote_est_dans_le_dossier_tmp_est_garde(self):
        """#2493 (relecture) : les refs de suivi ne prouvent rien si le remote
        vit dans le dossier temporaire supprimé : le clone et son origin
        partiraient ensemble. Le dossier reste, avec la raison."""
        _sans_processus(self)
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        root = os.path.join(troot, "a1+abcd9993")
        os.makedirs(root)
        menage._write_owner(troot, "a1+abcd9993", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        origin = os.path.join(root, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin],
                       check=True)
        amont = os.path.join(self.tmp, "amont")
        subprocess.run(["git", "clone", "-q", origin, amont], check=True,
                       capture_output=True)
        _git("commit", "-q", "--allow-empty", "-m", "racine", cwd=amont)
        _git("push", "-q", "origin", "main", cwd=amont)
        clone = os.path.join(root, "clone")
        subprocess.run(["git", "clone", "-q", origin, clone], check=True,
                       capture_output=True)
        # All these usable local URLs point to the same sole backup.
        os.mkdir(os.path.join(clone, "sub"))
        for url in (origin, "file://localhost" + origin,
                    "file://" + origin.replace("origin.git", "%6frigin.git"),
                    "sub/../../origin.git"):
            with self.subTest(url=url):
                _git("remote", "set-url", "origin", url, cwd=clone)
                self.assertTrue(_git("ls-remote", "origin", cwd=clone))
                self.assertIn("inside the temporary directory", menage.tmp_git_work(root))
        _git("config", "url.%s.insteadOf" % origin, "review-backup:", cwd=clone)
        _git("remote", "set-url", "origin", "review-backup:", cwd=clone)
        self.assertTrue(_git("ls-remote", "origin", cwd=clone))
        _age_tree(root, 3 * 3600)
        self.assertIn("inside the temporary directory", menage.tmp_git_work(root))
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                  now=time.time() + 3 * 3600)
        self.assertTrue(os.path.isdir(clone))
        self.assertTrue(os.path.isdir(origin))
        kept = [note for note in rapport["skipped"]
                if note.startswith("tmp %s kept: git work —" % root)]
        self.assertEqual(len(kept), 1, rapport["skipped"])
        self.assertIn("inside the temporary directory", kept[0])

    def test_entree_tmp_attribuee_avec_copie_git_non_sauvee_est_gardee(self):
        """#2512 : une entrée de /tmp attribuée d'avant #222 ne se supprime
        qu'avec les mêmes preuves qu'un dossier d'agent. Un fichier ignoré
        hors artefacts, puis une branche locale non poussée d'un clone, la
        gardent au lieu de partir avec le travail qu'ils portent."""
        _sans_processus(self)
        registry.upsert(self.db, "a1", harness="claude", host="pc")
        systeme = os.path.join(self.tmp, "tmp-systeme")
        os.makedirs(systeme)
        _pose_system_tmp(self, systeme)
        _repo(self.tmp)

        def ignorer(copie: str) -> None:
            with open(os.path.join(copie, ".git", "info", "exclude"), "w") as fh:
                fh.write("*.env\n")
            _write(os.path.join(copie, "private.env"), 10)

        def branche_locale(copie: str) -> None:
            _git("checkout", "-q", "-b", "travail", cwd=copie)
            _git("commit", "-q", "--allow-empty", "-m", "jamais pousse", cwd=copie)
            _git("checkout", "-q", "main", cwd=copie)

        for nom, preparer in (("ignore", ignorer), ("branche", branche_locale)):
            root = os.path.join(systeme, nom)
            os.makedirs(root)
            clone = os.path.join(root, "clone")
            subprocess.run(["git", "clone", "-q",
                            os.path.join(self.tmp, "origin.git"), clone],
                           check=True, capture_output=True)
            preparer(clone)
            _age_tree(root, 3 * 3600)
            self.assertTrue(menage.unsaved_git_work(root), nom)
            st = os.lstat(root)
            storage.of(self.db).housekeeping.log("pc", [{
                "kind": "orphan", "action": "signaled", "path": root,
                "bytes": menage.size_of(root), "agent": "a1",
                "detail": "signalement attribué (avant #222)",
                "data": {"ino": st.st_ino, "dev": st.st_dev}}], actor="test")
            rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(),
                                      host="pc", actor="t",
                                      now=time.time() + 3 * 3600)
            self.assertTrue(os.path.isdir(clone), nom)
            garde = [p for p in rapport["plan"]
                     if p.get("path") == root and p.get("action") == "garde"]
            self.assertEqual(len(garde), 1, (nom, rapport["plan"]))
            self.assertIn("git work to keep", garde[0]["detail"], nom)

    def test_entree_tmp_attribuee_avec_artefact_de_build_seul_est_supprimee(self):
        """#2512 : les règles d'artefacts de l'hôte restent exemptées — un clone
        propre et poussé qui ne porte qu'un `node_modules` ignoré part, sinon
        le contrôle garderait toute copie git par prudence."""
        _sans_processus(self)
        registry.upsert(self.db, "a1", harness="claude", host="pc")
        systeme = os.path.join(self.tmp, "tmp-systeme")
        os.makedirs(systeme)
        _pose_system_tmp(self, systeme)
        _repo(self.tmp)
        root = os.path.join(systeme, "artefact")
        os.makedirs(root)
        clone = os.path.join(root, "clone")
        subprocess.run(["git", "clone", "-q", os.path.join(self.tmp, "origin.git"),
                        clone], check=True, capture_output=True)
        with open(os.path.join(clone, ".git", "info", "exclude"), "w") as fh:
            fh.write("node_modules/\n")
        _write(os.path.join(clone, "node_modules", "x", "f"), 10)
        _age_tree(root, 3 * 3600)
        # sans les règles de l'hôte, l'ignoré garderait ; avec elles, il part
        self.assertTrue(menage.unsaved_git_work(root))
        self.assertEqual(menage.unsaved_git_work(root, build_artifacts.rules_of(None)), "")
        st = os.lstat(root)
        storage.of(self.db).housekeeping.log("pc", [{
            "kind": "orphan", "action": "signaled", "path": root,
            "bytes": menage.size_of(root), "agent": "a1",
            "detail": "signalement attribué (avant #222)",
            "data": {"ino": st.st_ino, "dev": st.st_dev}}], actor="test")
        rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(),
                                  host="pc", actor="t", now=time.time() + 3 * 3600)
        self.assertFalse(os.path.exists(root))
        self.assertIn(("orphan", "deleted"),
                      {(e.get("kind"), e.get("action")) for e in rapport["entries"]})

    def test_depot_nu_dans_une_entree_tmp_attribuee_est_garde(self):
        """#2525 : `_git_copies` ne reconnaissait une copie qu'à son `.git` ;
        un dépôt nu à la racine de l'entrée (`origin.git` : HEAD, objects,
        refs) était invisible, et la suppression emportait ses commits."""
        _sans_processus(self)
        registry.upsert(self.db, "a1", harness="claude", host="pc")
        systeme = os.path.join(self.tmp, "tmp-systeme")
        os.makedirs(systeme)
        _pose_system_tmp(self, systeme)
        main = _repo(self.tmp)
        root = os.path.join(systeme, "nu")
        os.makedirs(root)
        bare = os.path.join(root, "origin.git")
        subprocess.run(["git", "clone", "-q", "--bare", main, bare],
                       check=True, capture_output=True)
        _age_tree(root, 3 * 3600)
        self.assertIn(bare, menage._git_copies(root))
        self.assertIn("bare repository", menage.unsaved_git_work(root))
        st = os.lstat(root)
        storage.of(self.db).housekeeping.log("pc", [{
            "kind": "orphan", "action": "signaled", "path": root,
            "bytes": menage.size_of(root), "agent": "a1",
            "detail": "signalement attribué (avant #222)",
            "data": {"ino": st.st_ino, "dev": st.st_dev}}], actor="test")
        rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(),
                                  host="pc", actor="t", now=time.time() + 3 * 3600)
        self.assertTrue(os.path.isdir(root), rapport["skipped"])
        garde = [p for p in rapport["plan"]
                 if p.get("path") == root and p.get("action") == "garde"]
        self.assertEqual(len(garde), 1, rapport["plan"])
        self.assertIn("git work to keep", garde[0]["detail"])
        self.assertIn("bare repository", garde[0]["detail"])

    def test_depot_nu_dans_un_dossier_d_agent_est_garde(self):
        """#2525 : même angle mort pour `tmp_git_work` — un dossier d'agent
        qui ne porte qu'un dépôt nu avec un commit reste, au lieu de partir
        avec ses commits."""
        _sans_processus(self)
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        main = _repo(self.tmp)
        root = os.path.join(troot, "a1+abcd9992")
        os.makedirs(root)
        menage._write_owner(troot, "a1+abcd9992", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        bare = os.path.join(root, "origin.git")
        subprocess.run(["git", "clone", "-q", "--bare", main, bare],
                       check=True, capture_output=True)
        _age_tree(root, 3 * 3600)
        self.assertIn("bare repository", menage.tmp_git_work(root))
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                  now=time.time() + 3 * 3600)
        self.assertTrue(os.path.isdir(root), rapport["skipped"])
        kept = [note for note in rapport["skipped"]
                if note.startswith("tmp %s kept: git work —" % root)]
        self.assertEqual(len(kept), 1, rapport["skipped"])
        self.assertIn("bare repository", kept[0])
        self.assertNotIn(root, [p["path"] for p in rapport["plan"]
                                if p.get("action") == "suppression"])

    def test_copie_git_imbriquee_non_sauvee_est_gardee(self):
        """#2525 : une copie à plus d'un niveau (`entry/x/y/clone`) n'était
        pas vue ; son travail non commité partait avec l'entrée. Le parcours
        borné la trouve, pour l'entrée attribuée comme pour l'agent."""
        _sans_processus(self)
        registry.upsert(self.db, "a1", harness="claude", host="pc")
        systeme = os.path.join(self.tmp, "tmp-systeme")
        os.makedirs(systeme)
        _pose_system_tmp(self, systeme)
        _repo(self.tmp)
        root = os.path.join(systeme, "imbrique")
        os.makedirs(root)
        clone = os.path.join(root, "x", "y", "clone")
        os.makedirs(os.path.dirname(clone))
        subprocess.run(["git", "clone", "-q", os.path.join(self.tmp, "origin.git"),
                        clone], check=True, capture_output=True)
        _write(os.path.join(clone, "brouillon.txt"), 10)
        _age_tree(root, 3 * 3600)
        self.assertIn(clone, menage._git_copies(root))
        self.assertIn("modified or untracked", menage.unsaved_git_work(root))
        self.assertIn("modified or untracked", menage.tmp_git_work(root))
        st = os.lstat(root)
        storage.of(self.db).housekeeping.log("pc", [{
            "kind": "orphan", "action": "signaled", "path": root,
            "bytes": menage.size_of(root), "agent": "a1",
            "detail": "signalement attribué (avant #222)",
            "data": {"ino": st.st_ino, "dev": st.st_dev}}], actor="test")
        rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(),
                                  host="pc", actor="t", now=time.time() + 3 * 3600)
        self.assertTrue(os.path.isdir(clone), rapport["skipped"])

    def test_copie_git_imbriquee_propre_et_poussee_part_avec_l_entree(self):
        """#2525 : le parcours ne garde pas par simple présence — une copie
        imbriquée propre et poussée, sans remote dans l'entrée, part avec
        elle, comme une copie de premier niveau."""
        _sans_processus(self)
        registry.upsert(self.db, "a1", harness="claude", host="pc")
        systeme = os.path.join(self.tmp, "tmp-systeme")
        os.makedirs(systeme)
        _pose_system_tmp(self, systeme)
        _repo(self.tmp)
        root = os.path.join(systeme, "imbrique-propre")
        os.makedirs(root)
        clone = os.path.join(root, "x", "y", "clone")
        os.makedirs(os.path.dirname(clone))
        subprocess.run(["git", "clone", "-q", os.path.join(self.tmp, "origin.git"),
                        clone], check=True, capture_output=True)
        _age_tree(root, 3 * 3600)
        self.assertIn(clone, menage._git_copies(root))
        self.assertEqual(menage.unsaved_git_work(root), "")
        st = os.lstat(root)
        storage.of(self.db).housekeeping.log("pc", [{
            "kind": "orphan", "action": "signaled", "path": root,
            "bytes": menage.size_of(root), "agent": "a1",
            "detail": "signalement attribué (avant #222)",
            "data": {"ino": st.st_ino, "dev": st.st_dev}}], actor="test")
        rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(),
                                  host="pc", actor="t", now=time.time() + 3 * 3600)
        self.assertFalse(os.path.exists(root), rapport["skipped"])
        self.assertIn(("orphan", "deleted"),
                      {(e.get("kind"), e.get("action")) for e in rapport["entries"]})

    def test_worktree_imbrique_propre_est_retire_par_git_worktree_remove(self):
        """#2525 : un worktree lié à plus d'un niveau est vu comme les autres ;
        propre et poussé, il est retiré par `git worktree remove` (jamais
        forcé) avant la suppression du dossier, et son dépôt est élagué — pas
        de métadonnées périmées laissées derrière."""
        _sans_processus(self)
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        main = _repo(self.tmp)
        root = os.path.join(troot, "a1+abcd9991")
        os.makedirs(os.path.join(root, "x", "y"))
        menage._write_owner(troot, "a1+abcd9991", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        wt = os.path.join(root, "x", "y", "wt")
        _git("worktree", "add", "-q", wt, "-b", "wt-imbrique", cwd=main)
        _git("push", "-q", "origin", "wt-imbrique", cwd=main)
        _age_tree(root, 3 * 3600)
        self.assertIn(wt, menage._git_copies(root))
        self.assertEqual(menage.tmp_git_work(root), "")
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                  now=time.time() + 3 * 3600)
        self.assertFalse(os.path.isdir(root), rapport["skipped"])
        deleted = [e for e in rapport["entries"]
                   if e.get("path") == root and e.get("action") == "deleted"]
        self.assertEqual(len(deleted), 1, rapport["entries"])
        self.assertIn("git worktrees recovered", deleted[0]["detail"])
        self.assertNotIn(wt, _git("worktree", "list", cwd=main))
        self.assertIn("wt-imbrique", _git("branch", "--list", "wt-imbrique", cwd=main))

    def test_parcours_des_copies_borne_au_walk_limit_garde_l_entree(self):
        """#2525 : parcours borné à `WALK_LIMIT` dossiers ouverts ; budget
        épuisé = doute, l'entrée est gardée au lieu de conclure à tort qu'il
        n'y a pas de copie."""
        root = os.path.join(self.tmp, "borne")
        clone = os.path.join(root, "a", "b", "c", "clone")
        os.makedirs(os.path.dirname(clone))
        _repo(self.tmp)
        subprocess.run(["git", "clone", "-q", os.path.join(self.tmp, "origin.git"),
                        clone], check=True, capture_output=True)
        _write(os.path.join(clone, "brouillon.txt"), 10)
        with mock.patch.object(menage, "WALK_LIMIT", 3):
            raison = menage.unsaved_git_work(root)
            self.assertIn("more than 3 directories", raison)
            self.assertIn("copies not fully inspected", raison)
        self.assertIn("modified or untracked", menage.unsaved_git_work(root))

    def _suivi(self, main: str, nom: str, lot: int | None) -> str:
        wt = os.path.join(self.tmp, nom)
        _git("worktree", "add", "-q", wt, "-b", nom, cwd=main)
        storage.of(self.db).housekeeping.register_worktree(
            host="pc", path=wt, repo=menage.common_dir(main), agent="a1",
            lot=str(lot) if lot else None, turn_id="t", branch=nom, head="")
        return wt

    def test_fin_de_lot_retire_le_propre_garde_le_reste(self):
        _sans_processus(self)
        main = _repo(self.tmp)
        fini, ouvert = self._lot("fini"), self._lot("ouvert")
        propre = self._suivi(main, "propre", fini)
        sale = self._suivi(main, "sale", fini)
        _write(os.path.join(sale, "wip.txt"), 3)
        attend = self._suivi(main, "attend", ouvert)
        work.close(self.db, fini, abandoned=True)
        pol = menage.Policy()
        plus_tard = time.time() + 2 * 3600   # garde d'âge d'une heure franchie
        essai = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", dry_run=True,
                                now=plus_tard)
        self.assertTrue(os.path.isdir(propre))
        self.assertEqual(storage.of(self.db).housekeeping.recent("pc", 3600), [])
        self.assertIn("retrait", {p["action"] for p in essai["plan"]})
        menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", now=plus_tard)
        self.assertFalse(os.path.exists(propre))
        self.assertTrue(os.path.isdir(sale))
        self.assertTrue(os.path.isdir(attend))
        etats = {r["path"]: r["status"] for r in storage.of(self.db).housekeeping.worktrees("pc")}
        self.assertEqual(etats, {propre: "removed", sale: "kept", attend: "active"})
        journal = storage.of(self.db).housekeeping.recent("pc", 3600)
        self.assertIn(("worktree", "removed"), {(r["kind"], r["action"]) for r in journal})
        self.assertIn(("mesure", "measured"), {(r["kind"], r["action"]) for r in journal})
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "worktree_kept"]
        # #4442 : une alerte par hôte, qui nomme les worktrees gardés
        self.assertEqual(len(alertes), 1)
        self.assertEqual((alertes[0]["host"], alertes[0]["value"]), ("pc", 1))
        self.assertEqual(alertes[0]["paths"], [sale])
        self.assertIn("untracked", alertes[0]["detail"])
        self.assertIn("no archive policy", alertes[0]["detail"])

    def test_unreadable_ok_de_l_hote_est_passe_au_garde_fou_processus(self):
        """L230/#1807 : le plan des worktrees passe `policy.unreadable_ok` à
        `worktree_in_use` — un processus illisible déclaré par l'hôte ne
        retient plus un worktree de lot fini ; sans allow, il attend
        (fail-closed)."""
        _sans_processus(self)
        main = _repo(self.tmp)
        fini = self._lot("fini")
        propre = self._suivi(main, "propre", fini)
        work.close(self.db, fini, abandoned=True)
        plus_tard = time.time() + 2 * 3600   # garde d'âge d'une heure franchie

        def holders(path, **kw):
            if "gpg-agent" in (kw.get("allow") or ()):
                return []
            raise platform.NotAvailable("détenteurs d'un fichier", "test", "linux")

        with mock.patch.object(platform, "holders", side_effect=holders):
            garde = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                                    actor="t", dry_run=True, now=plus_tard)
            permis = menage.run_pass(self.cfgl, self.db,
                                     menage.Policy(unreadable_ok=("gpg-agent",)),
                                     host="pc", actor="t", dry_run=True, now=plus_tard)
        self.assertEqual({p["path"]: p["action"] for p in garde["plan"]}[propre], "attend")
        self.assertEqual({p["path"]: p["action"] for p in permis["plan"]}[propre], "retrait")

    def test_worktree_d_un_autre_agent_orphelin_garde_puis_retire(self):
        """L73b/#1665, cas réel : un agent crée un worktree de relecture
        PENDANT le tour d'un autre agent sur le même dépôt ; rien ne le
        rattache à l'agent du tour ni à son lot : il est suivi « non
        attribué ». Il survit tant qu'il est récent ou qu'un tour de l'agent
        qui l'a vu apparaître est en cours, puis part une fois orphelin
        (propre, poussé, inactif depuis 24 h, aucun processus dedans, table
        des processus lisible) ; sa branche reste."""
        _sans_processus(self)
        main = _repo(self.tmp)
        a1 = os.path.join(self.tmp, "a1")
        _git("worktree", "add", "-q", a1, "-b", "a1", cwd=main)
        registry.upsert(self.db, "a1", harness="claude", host="pc", cwd=a1)
        registry.upsert(self.db, "a2", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a2"))
        lot = self._lot("lot du tour")
        tour = menage.Turn.begin(self.cfgl, menage.Policy(), "a1", a1, new_session=False,
                                 base_env={})
        relecture = os.path.join(self.tmp, "relecture-a2")
        _git("worktree", "add", "-q", relecture, "-b", "relecture", cwd=main)  # par a2
        tour.end(lot=str(lot), turn_id="t" * 32, db=self.db, host="pc")
        rows = storage.of(self.db).housekeeping.worktrees("pc")
        self.assertEqual([(r["path"], r["lot"]) for r in rows], [(relecture, None)])
        self.assertTrue(rows[0]["detail"].startswith(menage.UNATTRIBUTED))
        work.close(self.db, lot, abandoned=True)
        # moins de 24 h d'inactivité : attend
        jeune = time.time() + 2 * 3600
        rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                                  actor="t", now=jeune)
        self.assertTrue(os.path.isdir(relecture))
        self.assertNotIn("retrait", {p["action"] for p in rapport["plan"]})
        # un tour de l'agent qui le suit : attend, même après 24 h
        # (le worktree n'est rattaché à personne : seul le tour de l'agent
        # qui l'a vu apparaître est connu, pas celui qui l'a créé)
        loin = time.time() + 30 * 24 * 3600
        rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                                  actor="t", agents_in_turn={"a1"}, now=loin)
        self.assertTrue(os.path.isdir(relecture))
        self.assertNotIn("retrait", {p["action"] for p in rapport["plan"]})
        # orphelin : propre, poussé, inactif depuis 24 h, aucun agent dessus
        rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                                  actor="t", now=loin)
        self.assertFalse(os.path.exists(relecture))
        self.assertIn("relecture", _git("branch", "--list", "relecture", cwd=main))
        self.assertEqual([r["status"] for r in storage.of(self.db).housekeeping.worktrees("pc")],
                         ["removed"])

    def _orphelin(self, main: str, nom: str, branche: str = "") -> str:
        """Un worktree suivi « non attribué », comme ceux d'avant #1665."""
        wt = os.path.join(self.tmp, nom)
        branche = branche or nom
        _git("worktree", "add", "-q", wt, "-b", branche, cwd=main)
        row = storage.of(self.db).housekeeping.register_worktree(
            host="pc", path=wt, repo=menage.common_dir(main), agent="a1",
            lot=None, turn_id="t", branch=branche, head="")
        storage.of(self.db).housekeeping.set_worktree_status(
            row["id"], "active",
            "%s: apparu pendant un tour, hors du dossier de l'agent" % menage.UNATTRIBUTED)
        return wt

    def test_worktree_hors_dossier_attribue_par_la_branche_ou_le_dossier(self):
        """#1665 : hors du dossier de l'agent, la branche (`lot/<id>`) ou le
        dossier (`<agent>-<id>`) nomme le lot ; s'il est assigné à cet agent,
        le worktree lui est attribué. Un lot d'un autre agent ne prouve rien."""
        main = _repo(self.tmp)
        registry.upsert(self.db, "a1", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a1"))
        registry.upsert(self.db, "a2", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a2"))
        lot, autre = self._lot("lot frère"), self._lot("lot d'un autre")
        work.assign(self.db, lot, "a1")
        work.assign(self.db, autre, "a2")
        tour = menage.Turn.begin(self.cfgl, menage.Policy(), "a1", main, new_session=False,
                                 base_env={})
        par_branche = os.path.join(self.tmp, "frere-branche")
        _git("worktree", "add", "-q", par_branche, "-b", "lot/%d-frere" % lot, cwd=main)
        par_dossier = os.path.join(self.tmp, "a1-%d" % lot)
        _git("worktree", "add", "-q", "--detach", par_dossier, cwd=main)
        etranger = os.path.join(self.tmp, "frere-etranger")
        _git("worktree", "add", "-q", etranger, "-b", "lot/%d-x" % autre, cwd=main)
        inconnu = os.path.join(self.tmp, "frere-inconnu")
        _git("worktree", "add", "-q", inconnu, "-b", "lot/999999-x", cwd=main)
        tour.end(lot="999", turn_id="t" * 32, db=self.db, host="pc")
        rows = {r["path"]: r for r in storage.of(self.db).housekeeping.worktrees("pc")}
        self.assertEqual(rows[par_branche]["lot"], str(lot))
        self.assertFalse(rows[par_branche]["detail"].startswith(menage.UNATTRIBUTED))
        self.assertEqual(rows[par_dossier]["lot"], str(lot))
        self.assertIsNone(rows[etranger]["lot"])
        self.assertTrue(rows[etranger]["detail"].startswith(menage.UNATTRIBUTED))
        self.assertIsNone(rows[inconnu]["lot"])
        self.assertTrue(rows[inconnu]["detail"].startswith(menage.UNATTRIBUTED))

    def test_worktree_non_attribue_rattache_a_son_lot_fini(self):
        """#1665 : un worktree déjà suivi « non attribué » dont la branche
        nomme un lot assigné à l'agent suit la fin de ce lot (il est retiré
        s'il est propre et intégré), et le journal porte le lot."""
        _sans_processus(self)
        main = _repo(self.tmp)
        registry.upsert(self.db, "a1", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a1"))
        fini = self._lot("fini")
        work.assign(self.db, fini, "a1")
        # la branche enregistrée nomme le lot fini, mais le suivi est resté
        # « non attribué » (comme les 329 worktrees frères d'avant #1665)
        wt = self._orphelin(main, "orphelin-rattache", "lot/%d-frere" % fini)
        work.close(self.db, fini, abandoned=True)
        plus_tard = time.time() + 2 * 3600
        essai = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t",
                                dry_run=True, now=plus_tard)
        plan = {p["path"]: p for p in essai["plan"] if p.get("kind") == "worktree"}
        self.assertEqual(plan[wt]["action"], "retrait")
        self.assertEqual(plan[wt]["lot"], str(fini))
        menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t",
                        now=plus_tard)
        self.assertFalse(os.path.exists(wt))
        rows = storage.of(self.db).housekeeping.worktrees("pc")
        self.assertEqual(rows[0]["status"], "removed")
        journal = storage.of(self.db).housekeeping.recent("pc", 3600)
        self.assertIn(str(fini), [str(r.get("lot")) for r in journal])

    def test_worktree_sans_lot_est_attribue_par_son_dossier(self):
        """#3266 : un worktree suivi sans lot dont le dossier le nomme
        (`nexlink-<agent>-<lot>`, `.wt<lot>`) est attribué à son lot — même
        quand l'enregistrement n'a pas lu le nom — puis la politique existante
        s'applique : lot fini, propre et intégré → retiré ; lot ouvert → gardé ;
        nom inconnu → reste non attribué. Aucune règle de suppression n'est
        ajoutée : l'orphelin inconnu attend ses 24 h."""
        _sans_processus(self)
        main = _repo(self.tmp)
        registry.upsert(self.db, "a1", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a1"))
        registry.upsert(self.db, "a2", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a2"))
        fini, ouvert = self._lot("fini"), self._lot("ouvert")
        work.assign(self.db, fini, "a1")
        work.assign(self.db, ouvert, "a2")   # un lot non démarré par agent
        nomme_fini = os.path.join(self.tmp, "nexlink-a1-%d" % fini)
        _git("worktree", "add", "-q", nomme_fini, "-b", "copie-fini", cwd=main)
        nomme_ouvert = os.path.join(self.tmp, ".wt%d" % ouvert)
        _git("worktree", "add", "-q", nomme_ouvert, "-b", "copie-ouvert", cwd=main)
        inconnu = os.path.join(self.tmp, "copie-inconnue")
        _git("worktree", "add", "-q", inconnu, "-b", "copie-inconnue", cwd=main)
        for path, agent in ((nomme_fini, "a1"), (nomme_ouvert, "a2"), (inconnu, "a1")):
            row = storage.of(self.db).housekeeping.register_worktree(
                host="pc", path=path, repo=menage.common_dir(main), agent=agent,
                lot=None, turn_id="t", branch="", head="")
            storage.of(self.db).housekeeping.set_worktree_status(row["id"], "active", "")
        self.assertEqual(menage.lot_of_worktree(self.db, "a2", nomme_ouvert),
                         str(ouvert))
        work.close(self.db, fini, abandoned=True)
        plus_tard = time.time() + 2 * 3600
        essai = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t",
                                dry_run=True, now=plus_tard)
        plan = {p["path"]: p for p in essai["plan"] if p.get("kind") == "worktree"}
        self.assertEqual(plan[nomme_fini]["action"], "retrait")
        self.assertEqual(plan[nomme_fini]["lot"], str(fini))
        self.assertEqual(plan[nomme_ouvert]["action"], "attend", plan[nomme_ouvert])
        self.assertEqual(plan[nomme_ouvert]["lot"], str(ouvert))
        self.assertIsNone(plan[inconnu]["lot"])
        self.assertTrue(os.path.isdir(nomme_fini))   # l'essai n'écrit rien
        menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t",
                        now=plus_tard)
        self.assertFalse(os.path.exists(nomme_fini))
        self.assertTrue(os.path.isdir(nomme_ouvert))
        self.assertTrue(os.path.isdir(inconnu))
        rows = {r["path"]: r for r in storage.of(self.db).housekeeping.worktrees("pc")}
        self.assertEqual((rows[nomme_ouvert]["agent"], rows[nomme_ouvert]["lot"]),
                         ("a2", str(ouvert)))
        self.assertIsNone(rows[inconnu]["lot"])
        journal = storage.of(self.db).housekeeping.recent("pc", 3600)
        self.assertIn(("worktree", "attributed"),
                      {(r["kind"], r["action"]) for r in journal})

    def test_worktree_nommant_un_autre_agent_corrige_son_agent(self):
        """#3266 : le dépôt commun fait voir à un agent le worktree d'un
        voisin ; `work/<agent>-<lot>` nomme le vrai agent, et le lot lui est
        assigné. L'attribution corrige l'agent ET le lot, puis la fin du lot
        s'applique (le worktree est retiré)."""
        _sans_processus(self)
        main = _repo(self.tmp)
        registry.upsert(self.db, "a1", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a1"))
        registry.upsert(self.db, "a2", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a2"))
        fini = self._lot("fini")
        work.assign(self.db, fini, "a1")
        wt = os.path.join(self.tmp, "a1-%d" % fini)
        _git("worktree", "add", "-q", wt, "-b", "copie-a1", cwd=main)
        row = storage.of(self.db).housekeeping.register_worktree(
            host="pc", path=wt, repo=menage.common_dir(main), agent="a2",
            lot=None, turn_id="t", branch="", head="")
        storage.of(self.db).housekeeping.set_worktree_status(row["id"], "active", "")
        work.close(self.db, fini, abandoned=True)
        plus_tard = time.time() + 2 * 3600
        menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t",
                        now=plus_tard)
        self.assertFalse(os.path.exists(wt))
        rows = storage.of(self.db).housekeeping.worktrees("pc")
        self.assertEqual((rows[0]["agent"], rows[0]["lot"], rows[0]["status"]),
                         ("a1", str(fini), "removed"))

    def test_relecture_tous_les_candidats_du_dossier_avant_retrait(self):
        """#3266 (relecture) : la branche ACTUELLE est relue avant attribution
        (une branche enregistrée périmée ne fait pas retirer) ; la convention
        stricte garde la priorité (`a1-431-22` suit #431) ; un dossier qui
        nomme plusieurs lots, dont un ouvert ou inconnu, n'autorise aucun
        retrait ; le propriétaire nommé reste l'agent même quand le lot est
        assigné ailleurs (tour en cours)."""
        _sans_processus(self)
        main = _repo(self.tmp)
        for name in ("a1", "a2", "a3", "a4", "a5"):
            registry.upsert(self.db, name, harness="claude", host="pc",
                            cwd=os.path.join(self.tmp, name))
        # a1 : lot fini (étranger), lot fini (strict), lot ouvert (strict)
        etranger = self._lot("etranger")
        work.assign(self.db, etranger, "a1")
        work.close(self.db, etranger, abandoned=True)
        strict_fini = self._lot("strict-fini")
        work.assign(self.db, strict_fini, "a1")
        work.close(self.db, strict_fini, abandoned=True)
        strict_ouvert = self._lot("strict-ouvert")
        work.assign(self.db, strict_ouvert, "a1")
        # a3 : lot fini puis lot ouvert, tous deux nommés par un dossier
        multi_fini = self._lot("multi-fini")
        work.assign(self.db, multi_fini, "a3")
        work.close(self.db, multi_fini, abandoned=True)
        multi_ouvert = self._lot("multi-ouvert")
        work.assign(self.db, multi_ouvert, "a3")
        # a4 : branche périmée (topic) puis bascule vers lot/ouvert
        stale_fini = self._lot("stale-fini")
        work.assign(self.db, stale_fini, "a4")
        work.close(self.db, stale_fini, abandoned=True)
        stale_ouvert = self._lot("stale-ouvert")
        work.assign(self.db, stale_ouvert, "a4")
        # a5 : branche lot/fini, dossier qui nomme aussi un lot ouvert
        disagree_fini = self._lot("disagree-fini")
        work.assign(self.db, disagree_fini, "a5")
        work.close(self.db, disagree_fini, abandoned=True)
        disagree_ouvert = self._lot("disagree-ouvert")
        work.assign(self.db, disagree_ouvert, "a5")
        rows = {}

        def add(name, agent, branch):
            path = os.path.join(self.tmp, name)
            _git("worktree", "add", "-q", path, "-b", branch, cwd=main)
            row = storage.of(self.db).housekeeping.register_worktree(
                host="pc", path=path, repo=menage.common_dir(main), agent=agent,
                lot=None, turn_id="t", branch=branch, head="")
            storage.of(self.db).housekeeping.set_worktree_status(row["id"], "active", "")
            rows[name] = path
            return path

        add("a1-%d-%d" % (strict_ouvert, strict_fini), "a1", "topic-strict")
        add("nexlink-a2-%d" % etranger, "a2", "topic-foreign")
        add("wt-%d-%d" % (multi_fini, multi_ouvert), "a3", "topic-multi")
        add("wt-999999-%d" % multi_fini, "a3", "topic-unknown")
        stale = add("nexlink-a4-%d" % stale_fini, "a4", "topic-stale")
        # la branche enregistrée est périmée : le worktree a basculé
        _git("checkout", "-q", "-b", "lot/%d" % stale_ouvert, cwd=stale)
        add("wt-%d-%d" % (disagree_ouvert, disagree_fini), "a5",
            "lot/%d" % disagree_fini)
        _entries, plan = menage._worktree_pass(
            self.db, "pc", dry_run=True, protected=set(), running={"a2"},
            now=time.time() + 25 * 3600, known_agents=("a1", "a2", "a3", "a4", "a5"))
        plan = {p["path"]: p for p in plan if p.get("kind") == "worktree"}
        strict = plan[rows["a1-%d-%d" % (strict_ouvert, strict_fini)]]
        self.assertEqual((strict["action"], strict["lot"]),
                         ("attend", str(strict_ouvert)))
        tour = plan[rows["nexlink-a2-%d" % etranger]]
        self.assertEqual(tour["agent"], "a2")
        self.assertIn("turn in progress for a2", tour["detail"])
        kept = {r["path"]: r for r in storage.of(self.db).housekeeping.worktrees("pc")}
        self.assertIsNone(kept[rows["nexlink-a2-%d" % etranger]]["lot"])
        self.assertIsNone(kept[rows["nexlink-a4-%d" % stale_fini]]["lot"])
        for name in ("wt-%d-%d" % (multi_fini, multi_ouvert),
                     "wt-999999-%d" % multi_fini,
                     "nexlink-a4-%d" % stale_fini,
                     "wt-%d-%d" % (disagree_ouvert, disagree_fini)):
            row = plan[rows[name]]
            self.assertEqual((row["action"], row["lot"]), ("garde", None), row)
            self.assertIn("not attributable", row["detail"])

    def test_attribution_persistee_relit_la_branche_avant_retrait(self):
        """#3266 (relecture cx3) : la passe 1 attribue #431 (fermé) par le
        dossier `nexlink-a2-431` et garde le worktree modifié. À la passe 2,
        après `checkout -b lot/432` (ouvert), le lot persisté ne dispense pas
        de relire la branche ACTUELLE : #432 est un candidat, le worktree est
        gardé et l'attribution reste. La base f71414d8 attendait aux deux
        passes (aucune attribution) ; retirer la ligne persistée est le défaut
        que ce témoin couvre. Deux passes, Git jetable, registre réel."""
        _sans_processus(self)
        main = _repo(self.tmp)
        registry.upsert(self.db, "a2", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a2"))
        fini = self._lot("fini")
        work.assign(self.db, fini, "a2")
        work.close(self.db, fini, abandoned=True)
        ouvert = self._lot("ouvert")
        work.assign(self.db, ouvert, "a2")
        wt = os.path.join(self.tmp, "nexlink-a2-%d" % fini)
        _git("worktree", "add", "-q", wt, "-b", "topic", cwd=main)
        _write(os.path.join(wt, "non-suivi"), 10)
        hk = storage.of(self.db).housekeeping
        row = hk.register_worktree(
            host="pc", path=wt, repo=menage.common_dir(main), agent="a2",
            lot=None, turn_id="t", branch="topic", head="")
        hk.set_worktree_status(row["id"], "active", "")
        now = time.time() + 2 * 3600
        interdit = AssertionError("aucune suppression Git dans ce témoin")
        with mock.patch.object(menage, "remove_worktree", side_effect=interdit), \
                mock.patch.object(menage, "_remove_kept_worktree", side_effect=interdit):
            _entries, plan = menage._worktree_pass(
                self.db, "pc", dry_run=False, protected=set(), running=set(),
                now=now, known_agents=("a2",))
            plan = {p["path"]: p for p in plan if p.get("kind") == "worktree"}
            self.assertEqual((plan[wt]["action"], plan[wt]["lot"]),
                             ("garde", str(fini)), plan[wt])
            self.assertEqual(hk.worktrees("pc")[0]["lot"], str(fini))
            # la branche passe au lot encore ouvert ; le worktree devient propre
            os.unlink(os.path.join(wt, "non-suivi"))
            _git("checkout", "-q", "-b", "lot/%d" % ouvert, cwd=wt)
            _entries, plan = menage._worktree_pass(
                self.db, "pc", dry_run=True, protected=set(), running=set(),
                now=now, known_agents=("a2",))
            plan = {p["path"]: p for p in plan if p.get("kind") == "worktree"}
            self.assertEqual(plan[wt]["action"], "garde", plan[wt])
            self.assertIn(str(ouvert), plan[wt]["detail"])
            self.assertEqual(hk.worktrees("pc")[0]["lot"], str(fini))
            self.assertTrue(os.path.isdir(wt))

    def test_premiere_attribution_verifie_tous_les_lots_avant_retrait(self):
        """#3266 (relecture cx3) : à la PREMIÈRE attribution, la branche est
        lue pour attribuer ; cette lecture ne dispense pas de vérifier tous
        les lots qu'elle nomme avant un retrait. `nexlink-a2-431` sur la
        branche `lot/431-432` attribue #431 (fermé), mais #432 est encore en
        construction : le worktree est gardé, aucune suppression. Le contrôle
        (lot #431 déjà persisté, deux passes) passait déjà ; c'est
        l'attribution de la même passe qui sautait la vérification."""
        _sans_processus(self)
        main = _repo(self.tmp)
        registry.upsert(self.db, "a2", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a2"))
        fini = self._lot("fini")
        work.assign(self.db, fini, "a2")
        work.close(self.db, fini, abandoned=True)
        ouvert = self._lot("ouvert")
        work.assign(self.db, ouvert, "a2")
        wt = os.path.join(self.tmp, "nexlink-a2-%d" % fini)
        _git("worktree", "add", "-q", wt, "-b",
             "lot/%d-%d" % (fini, ouvert), cwd=main)
        hk = storage.of(self.db).housekeeping
        row = hk.register_worktree(
            host="pc", path=wt, repo=menage.common_dir(main), agent="a2",
            lot=None, turn_id="t", branch="topic", head="")
        hk.set_worktree_status(row["id"], "active", menage.UNATTRIBUTED)
        interdit = AssertionError("aucune suppression Git dans ce témoin")
        with mock.patch.object(menage, "remove_worktree", side_effect=interdit), \
                mock.patch.object(menage, "_remove_kept_worktree", side_effect=interdit):
            _entries, plan = menage._worktree_pass(
                self.db, "pc", dry_run=False, protected=set(), running=set(),
                now=time.time() + 2 * 3600, known_agents=("a2",))
        plan = {p["path"]: p for p in plan if p.get("kind") == "worktree"}
        self.assertEqual(plan[wt]["action"], "garde", plan[wt])
        self.assertIn(str(ouvert), plan[wt]["detail"])
        self.assertTrue(os.path.isdir(wt))
        self.assertEqual(hk.worktrees("pc")[0]["lot"], str(fini))

    def test_worktrees_suivis_sans_plafond_pour_le_menage(self):
        """#2174 : `limit=None` rend tous les worktrees suivis de l'hôte (le
        ménage), le plafond par défaut reste pour les autres lectures."""
        hk = storage.of(self.db).housekeeping
        for i in range(3):
            hk.register_worktree(host="pc", path=os.path.join(self.tmp, "w%d" % i),
                                 repo="r", agent="a1", lot=None, turn_id="t",
                                 branch="b%d" % i, head="")
        self.assertEqual(len(hk.worktrees("pc", ("active",), limit=2)), 2)
        self.assertEqual(len(hk.worktrees("pc", ("active",), limit=None)), 3)

    def test_lot_nomme_d_un_autre_agent_fini_suit_la_regle_des_orphelins(self):
        """#2174 : un worktree dont la branche nomme le lot d'un AUTRE agent
        (copie de relecture, lot réassigné) n'est plus gardé à vie une fois ce
        lot fini : il part comme un orphelin (24 h d'inactivité, propre,
        poussé) ; sale, il reste ; lot encore ouvert, il reste gardé."""
        _sans_processus(self)
        main = _repo(self.tmp)
        registry.upsert(self.db, "a1", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a1"))
        registry.upsert(self.db, "a2", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a2"))
        fini, ouvert = self._lot("fini de a2"), self._lot("ouvert de a2")
        work.assign(self.db, fini, "a2")
        work.assign(self.db, ouvert, "a2")
        propre = self._orphelin(main, "relecture-propre", "lot/%d-relecture" % fini)
        sale = self._orphelin(main, "relecture-sale", "lot/%d-relecture-b" % fini)
        _write(os.path.join(sale, "wip.txt"), 3)
        encours = self._orphelin(main, "relecture-ouverte", "lot/%d-relecture" % ouvert)
        work.close(self.db, fini, abandoned=True)

        def plan(**kw):
            rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                                      actor="t", dry_run=True, **kw)
            return {p["path"]: p for p in rapport["plan"] if p.get("kind") == "worktree"}

        tot = plan(now=time.time() + 2 * 3600)
        self.assertEqual(tot[propre]["action"], "attend")       # moins de 24 h
        self.assertIn("24 h", tot[propre]["detail"])
        loin = time.time() + 30 * 24 * 3600
        p = plan(now=loin)
        self.assertEqual(p[propre]["action"], "retrait")
        self.assertEqual(p[propre]["lot"], str(fini))
        self.assertIn("assigned to another agent", p[propre]["detail"])
        self.assertEqual(p[sale]["action"], "garde")
        self.assertEqual(p[encours]["action"], "garde")
        self.assertIn("not attributable", p[encours]["detail"])
        menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t", now=loin)
        self.assertFalse(os.path.exists(propre))
        self.assertTrue(os.path.isdir(sale))
        self.assertTrue(os.path.isdir(encours))
        self.assertIn("lot/%d-relecture" % fini,
                      _git("branch", "--list", "lot/%d-relecture" % fini, cwd=main))

    def test_copie_etrangere_non_poussee_est_signalee(self):
        """#2182 N1: a foreign copy whose HEAD is not pushed is never removed,
        but it is recorded `kept` — the `worktree_kept` alert then names that
        unpushed work (before, it only appeared in the pass plan)."""
        _sans_processus(self)
        main = _repo(self.tmp)
        registry.upsert(self.db, "a1", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a1"))
        registry.upsert(self.db, "a2", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a2"))
        fini = self._lot("fini de a2")
        work.assign(self.db, fini, "a2")
        wt = self._orphelin(main, "relecture-non-poussee", "lot/%d-relecture" % fini)
        _write(os.path.join(wt, "travail.txt"), 3)
        _git("add", ".", cwd=wt)
        _git("commit", "-q", "-m", "travail non poussé", cwd=wt)
        work.close(self.db, fini, abandoned=True)
        loin = time.time() + 30 * 24 * 3600

        essai = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t",
                                dry_run=True, now=loin)
        plan = {p["path"]: p for p in essai["plan"] if p.get("kind") == "worktree"}
        self.assertEqual(plan[wt]["action"], "garde")
        self.assertIn("commits not pushed", plan[wt]["detail"])
        self.assertEqual(  # a dry run writes nothing
            [r["status"] for r in storage.of(self.db).housekeeping.worktrees("pc")],
            ["active"])

        menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t", now=loin)
        self.assertTrue(os.path.isdir(wt))
        row = storage.of(self.db).housekeeping.worktrees("pc")[0]
        self.assertEqual(row["status"], "kept")
        self.assertIn("commits not pushed", row["detail"])
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "worktree_kept"]
        self.assertEqual(len(alertes), 1)
        self.assertEqual(alertes[0]["paths"], [wt])
        self.assertIn("commits not pushed", alertes[0]["detail"])
        journal = storage.of(self.db).housekeeping.recent("pc", 3600)
        self.assertIn(("worktree", "kept"), {(r["kind"], r["action"]) for r in journal})

    def test_review_d_un_agent_a_chiffre_ne_cite_pas_son_numero(self):
        """#2182 N2: `review/<agent>-<lot>` must not read the number carried by
        the agent's name (`reviewer-07`) as a cited lot. An open lot numbered
        like that agent used to keep the copy of a FINISHED lot forever; the
        copy now follows the orphan rule (24 h, clean, pushed). A branch that
        genuinely cites a second, still-open lot is still kept (#1694)."""
        _sans_processus(self)
        main = _repo(self.tmp)
        registry.upsert(self.db, "a1", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a1"))
        fini, ouvert = self._lot("fini de reviewer"), self._lot("ouvert de reviewer")
        nom = "reviewer-%02d" % ouvert   # son nom porte le numéro du lot ouvert
        registry.upsert(self.db, nom, harness="deepseek", host="pc",
                        cwd=os.path.join(self.tmp, nom))
        work.assign(self.db, fini, nom)
        work.assign(self.db, ouvert, nom)
        copie = self._orphelin(main, "relecture-a-chiffre", "review/%s-%d" % (nom, fini))
        # une branche qui cite VRAIMENT le lot ouvert en plus du lot fini
        deux = self._orphelin(main, "relecture-deux-lots",
                              "review/%s-%d-%d" % (nom, fini, ouvert))
        work.close(self.db, fini, abandoned=True)
        loin = time.time() + 30 * 24 * 3600

        def plan(**kw):
            rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                                      actor="t", dry_run=True, **kw)
            return {p["path"]: p for p in rapport["plan"] if p.get("kind") == "worktree"}

        p = plan(now=loin)
        self.assertEqual(p[copie]["action"], "retrait")
        self.assertEqual(p[copie]["lot"], str(fini))
        self.assertEqual(p[deux]["action"], "garde")
        self.assertIn("not attributable", p[deux]["detail"])
        menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t", now=loin)
        self.assertFalse(os.path.exists(copie))
        self.assertTrue(os.path.isdir(deux))

    def test_orphelin_propre_pousse_inactif_24h_puis_retire(self):
        """#1665 : propre, poussé et inactif depuis 24 h, un orphelin part ;
        sale, non poussé ou tenu par un agent, il reste et il est signalé."""
        _sans_processus(self)
        main = _repo(self.tmp)
        propre = self._orphelin(main, "orphelin-propre")
        sale = self._orphelin(main, "orphelin-sale")
        _write(os.path.join(sale, "wip.txt"), 3)
        non_pousse = self._orphelin(main, "orphelin-non-pousse")
        _write(os.path.join(non_pousse, "f.txt"), 3)
        _git("add", ".", cwd=non_pousse)
        _git("commit", "-q", "-m", "travail", cwd=non_pousse)

        def plan(**kw):
            rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                                      actor="t", dry_run=True, **kw)
            return {p["path"]: p for p in rapport["plan"] if p.get("kind") == "worktree"}

        jeune = time.time() + 2 * 3600
        self.assertEqual(plan(now=jeune)[propre]["action"], "attend")
        loin = time.time() + 30 * 24 * 3600
        self.assertEqual(plan(now=loin, agents_in_turn={"a1"})[propre]["action"], "attend")
        p = plan(now=loin)
        self.assertEqual(p[propre]["action"], "retrait")
        self.assertIn("orphan", p[propre]["detail"])
        self.assertEqual(p[sale]["action"], "garde")
        self.assertEqual(p[non_pousse]["action"], "garde")
        menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t", now=loin)
        self.assertFalse(os.path.exists(propre))
        self.assertTrue(os.path.isdir(sale))
        self.assertTrue(os.path.isdir(non_pousse))
        self.assertIn("orphelin-propre", _git("branch", "--list", "orphelin-propre", cwd=main))

    def test_lot_fini_non_integre_retire_apres_24h(self):
        """#1665 : un lot fini (clos) dont le worktree est propre et poussé
        mais NON intégré n'était que gardé (L73b) ; il part après 24 h
        d'inactivité, comme un orphelin, et sa branche reste."""
        _sans_processus(self)
        main = _repo(self.tmp)
        fini = self._lot("fini")
        wt = self._suivi(main, "fini-non-integre", fini)
        _write(os.path.join(wt, "travail.txt"), 3)
        _git("add", ".", cwd=wt)
        _git("commit", "-q", "-m", "travail", cwd=wt)
        _git("push", "-q", "origin", "fini-non-integre", cwd=wt)
        work.close(self.db, fini, abandoned=True)

        def plan(**kw):
            rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                                      actor="t", dry_run=True, **kw)
            return {p["path"]: p for p in rapport["plan"] if p.get("kind") == "worktree"}

        tot = time.time() + 2 * 3600
        p = plan(now=tot)[wt]
        self.assertEqual(p["action"], "garde")
        self.assertIn("not integrated", p["detail"])
        loin = time.time() + 30 * 24 * 3600
        p = plan(now=loin)[wt]
        self.assertEqual(p["action"], "retrait")
        self.assertIn("idle for more than 24 h", p["detail"])
        menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t", now=loin)
        self.assertFalse(os.path.exists(wt))
        self.assertIn("fini-non-integre", _git("branch", "--list", "fini-non-integre", cwd=main))

    def test_garde_fous_age_tour_en_cours_verrou_et_integration(self):
        _sans_processus(self)
        main = _repo(self.tmp)
        fini = self._lot("fini")
        jeune = self._suivi(main, "jeune", fini)
        pol = menage.Policy()
        work.close(self.db, fini, abandoned=True)

        def plan(**kw):
            rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                      dry_run=True, **kw)
            return {p["path"]: p for p in rapport["plan"] if p.get("kind") == "worktree"}

        # (b) moins d'une heure : attend
        self.assertEqual(plan()[jeune]["action"], "attend")
        plus_tard = time.time() + 2 * 3600
        self.assertEqual(plan(now=plus_tard)[jeune]["action"], "retrait")
        # dossier modifié récemment : attend aussi
        self.assertEqual(plan(now=os.stat(jeune).st_mtime + 600)[jeune]["action"], "attend")
        # (c) l'agent propriétaire a un tour en cours : attend
        p = plan(now=plus_tard, agents_in_turn={"a1"})[jeune]
        self.assertEqual(p["action"], "attend")
        self.assertIn("turn in progress", p["detail"])
        # verrouillé : gardé
        _git("worktree", "lock", jeune, cwd=main)
        p = plan(now=plus_tard)[jeune]
        self.assertEqual(p["action"], "garde")
        self.assertIn("locked",p["detail"])
        _git("worktree", "unlock", jeune, cwd=main)
        # (d) poussé mais pas fusionné : gardé ; fusionné : retiré
        _write(os.path.join(jeune, "f.txt"), 3)
        _git("add", ".", cwd=jeune)
        _git("commit", "-q", "-m", "travail", cwd=jeune)
        _git("push", "-q", "origin", "jeune", cwd=jeune)
        menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", now=plus_tard)
        self.assertTrue(os.path.isdir(jeune))
        rows = storage.of(self.db).housekeeping.worktrees("pc")
        self.assertEqual(rows[0]["status"], "kept")
        self.assertIn("pushed but not merged", rows[0]["detail"])
        _git("merge", "-q", "--ff-only", "jeune", cwd=main)
        _git("push", "-q", "origin", "main", cwd=main)
        menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", now=plus_tard)
        self.assertFalse(os.path.exists(jeune))
        # le dossier de travail d'un agent (ou un dossier qui le contient) : gardé
        parent = self._suivi(main, "parent", fini)
        registry.upsert(self.db, "a3", harness="claude", host="pc",
                        cwd=os.path.join(parent, "sous-dossier"))
        self.assertEqual(plan(now=plus_tard)[parent]["action"], "garde")

    def test_dossier_d_agent_disparu_et_caches_remis_pendant_un_tour(self):
        registry.upsert(self.db, "a1", harness="claude", host="pc")
        pol = menage.Policy(cache_quota=10)
        menage.turn_env(self.cfgl, pol, "a1", {})
        menage.turn_env(self.cfgl, pol, "parti", {})
        _write(os.path.join(menage.agent_tmp(self.cfgl, pol, "parti"), "f"), 100,
               age_s=3 * 3600)
        # L219: an orphan directory is deleted once idle for `orphan_idle`
        ancien = time.time() - 3 * 3600
        os.utime(menage.agent_tmp(self.cfgl, pol, "parti"), (ancien, ancien))
        pnpm = os.path.join(menage.cache_root(self.cfgl, pol), "pnpm", "v3", "f")
        _write(pnpm, 5000, age_s=7200)
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", busy=True,
                                  now=time.time() + 3 * 3600)
        self.assertFalse(os.path.exists(menage.agent_tmp(self.cfgl, pol, "parti")))
        self.assertTrue(os.path.exists(menage.agent_tmp(self.cfgl, pol, "a1")))
        self.assertTrue(os.path.exists(pnpm))
        self.assertTrue(any("caches" in s for s in rapport["skipped"]))
        menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", busy=False)
        self.assertFalse(os.path.exists(pnpm))

    def test_conteneurs_d_un_tour_supprimes_les_autres_signales(self):
        turns = storage.of(self.db).turn_resources
        for tid, host in (("fini", "pc"), ("encours", "pc"), ("lotfini", "pc"),
                          ("lotouvert", "pc"), ("ailleurs", "autre")):
            turns.open_turn(tid, "a1", host, pgid=None, label=None)
        for tid in ("fini", "lotfini", "lotouvert", "ailleurs"):
            turns.close_turn(tid, orphan=True)
        ouvert, ferme = self._lot("ouvert"), self._lot("ferme")
        work.close(self.db, ferme, abandoned=True)

        def c(ident, **labels):
            return {"id": ident, "name": "c-" + ident, "image": "postgres", "status": "Up",
                    "labels": labels}

        moteur = _FauxMoteur([
            c("1", **{"ameesh.turn": "fini", "ameesh.agent": "a1"}),
            c("2", **{"ameesh.turn": "encours", "ameesh.agent": "a1"}),
            c("3", **{"ameesh.turn": "lotfini", "ameesh.lot": str(ferme)}),
            c("4", **{"ameesh.turn": "lotouvert", "ameesh.lot": str(ouvert)}),
            c("5", **{"ameesh.turn": "ailleurs"}),
            c("6", **{"ameesh.turn": "inconnu"}),
            c("7"),   # nexlink-sqltest-… : lancé hors d'un tour d'ameesh
        ])
        essai = menage.reap_containers(moteur, self.db, "pc", dry_run=True)
        self.assertEqual(moteur.removed, [])
        self.assertEqual({p["path"] for p in essai["plan"] if p["action"] == "suppression"},
                         {"c-1", "c-3"})
        menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t",
                        runtime=moteur)
        self.assertEqual(sorted(moteur.removed), ["1", "3"])
        dernier = storage.of(self.db).housekeeping.last_measure("pc")[0]["data"]
        self.assertEqual([o["command"] for o in dernier["containers"]["others"]],
                         ["docker rm -f c-7"])
        attente = {o["path"]: o["action"] for o in dernier["containers"]["ameesh"]}
        self.assertEqual(attente, {"c-2": "attend", "c-4": "attend", "c-5": "signalé",
                                   "c-6": "signalé"})
        journal = storage.of(self.db).housekeeping.recent("pc", 3600)
        self.assertEqual(sorted(r["path"] for r in journal if r["kind"] == "container"),
                         ["c-1", "c-3"])
        proc = self.mesh("hosts")
        self.assertIn("containers 4 started by an ameesh turn", proc.stdout)
        self.assertIn("docker rm -f c-7", proc.stdout)
        # au tour suivant, seul le conteneur de CE tour est regardé
        seul = menage.reap_containers(moteur, self.db, "pc", only_turn="encours")
        self.assertEqual(seul["entries"], [])
        self.assertIsNone(seul["others"])

    def test_conteneurs_arretes_visibles_et_base_de_lot_gardee(self):
        """#1411 : `ps -a` rend visibles les conteneurs arrêtés (récoltés ou
        signalés) ; une base de lot ENCORE OUVERT reste gardée jusqu'à sa fin.
        Repli borné après la R1 : pas de destruction sur la photographie des
        tours, faute d'exclusion commune avec le démarrage d'un tour."""
        turns = storage.of(self.db).turn_resources
        for tid in ("fini", "encours"):
            turns.open_turn(tid, "a1", "pc", pgid=None, label=None)
        turns.close_turn("fini", orphan=True)
        ouvert, ferme = self._lot("ouvert"), self._lot("ferme")
        work.close(self.db, ferme, abandoned=True)

        def c(ident, status="Up", **labels):
            return {"id": ident, "name": "c-" + ident, "image": "postgres",
                    "status": status, "labels": labels}

        moteur = _FauxMoteur([
            # arrêté, tour fini, sans lot : récolté (invisible avant #1411)
            c("8", status="Exited (137) 2 days ago",
              **{"ameesh.turn": "fini", "ameesh.agent": "a1"}),
            # arrêté, base d'un lot OUVERT : gardé jusqu'à la fin du lot
            c("9", status="Created", **{"ameesh.turn": "fini", "ameesh.agent": "a1",
                                        "ameesh.lot": str(ouvert)}),
            # arrêté, hors tour : signalé, jamais supprimé
            c("10", status="Exited (0) 3 h ago"),
            # en cours, lot ouvert et tour vivant : attend
            c("11", status="Up", **{"ameesh.turn": "encours", "ameesh.agent": "a1",
                                    "ameesh.lot": str(ouvert)}),
            # en cours, tour fini et lot fermé : récolté
            c("12", status="Up", **{"ameesh.turn": "fini", "ameesh.agent": "a1",
                                    "ameesh.lot": str(ferme)}),
        ], running={"11", "12"})
        essai = menage.reap_containers(moteur, self.db, "pc", dry_run=True)
        self.assertEqual(moteur.removed, [])
        self.assertEqual({p["path"] for p in essai["plan"] if p["action"] == "suppression"},
                         {"c-8", "c-12"})
        self.assertEqual({o["path"]: o["action"] for o in essai["ameesh"]},
                         {"c-8": "suppression", "c-9": "attend",
                          "c-11": "attend", "c-12": "suppression"})
        # la base du lot ouvert attend sa fin, quoi qu'en dise la photographie
        self.assertEqual("lot #%s intake" % ouvert,
                         [o["detail"] for o in essai["ameesh"]
                          if o["path"] == "c-9"][0])
        # le conteneur arrêté hors tour est vu et signalé, avec sa commande
        self.assertEqual([o["command"] for o in essai["others"]], ["docker rm -f c-10"])
        self.assertEqual(essai["others"][0]["status"], "Exited (0) 3 h ago")
        # réalisation : le tour fini sans lot et la base du lot fermé partent
        menage.reap_containers(moteur, self.db, "pc")
        self.assertEqual(sorted(moteur.removed), ["12", "8"])

    def test_le_releve_des_conteneurs_hors_tour_est_borne(self):
        """#1547 (N3) : le journal ne garde qu'`OTHERS_TOP` conteneurs hors
        tour, et le compte total dans `others_total` — la liste n'est plus
        non bornée sur un hôte qui en porte beaucoup."""
        def c(i):
            return {"id": str(i), "name": "c-%d" % i, "image": "redis",
                    "status": "Up", "labels": {}}

        moteur = _FauxMoteur([c(i) for i in range(5)])
        with mock.patch.object(menage, "OTHERS_TOP", 2):
            essai = menage.reap_containers(moteur, self.db, "pc", dry_run=True)
            self.assertEqual([o["name"] for o in essai["others"]], ["c-0", "c-1"])
            self.assertEqual(essai["others_total"], 5)
            menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc", actor="t",
                            runtime=moteur)
        dernier = storage.of(self.db).housekeeping.last_measure("pc")[0]["data"]
        self.assertEqual(len(dernier["containers"]["others"]), 2)
        self.assertEqual(dernier["containers"]["others_total"], 5)
        # un tour précis ne regarde que les siens : ni liste, ni compte
        seul = menage.reap_containers(moteur, self.db, "pc", only_turn="encours")
        self.assertIsNone(seul["others"])
        self.assertIsNone(seul["others_total"])

    def _lecture(self, used: float) -> None:
        storage.of(self.db).hosts.record({
            "host": self.cfg.host, "mem_available_bytes": 8 * GIB, "swap_used_bytes": 0,
            "load1": 0.1, "cpu_count": 8, "disk_free_bytes": 100 * GIB, "disk_path": "/",
            "turns_in_progress": 0, "tmp_path": "/tmp", "tmp_fstype": "tmpfs",
            "tmp_size_bytes": 12 * GIB, "tmp_used_bytes": int(used * GIB)})

    def test_hosts_alertes_et_contre_pression(self):
        self._lecture(7.4)
        alertes = {a["type"]: a for a in exploitation.alerts(self.cfg, self.db)}
        self.assertIn("tmpfs_full", alertes)            # 62 % ≥ 60 %
        self.assertNotIn("host_pressure", alertes)      # < 80 %
        proc = self.mesh("hosts")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("/tmp       tmpfs", proc.stdout)
        self.assertIn("ALERT", proc.stdout)
        self._lecture(10.0)
        alertes = {a["type"]: a for a in exploitation.alerts(self.cfg, self.db)}
        self.assertIn("max_tmpfs_used", alertes["host_pressure"]["breaches"])
        proc = self.mesh("hosts", "--json")
        objet = json.loads(proc.stdout.splitlines()[0])
        self.assertEqual(objet["latest"]["tmp_fstype"], "tmpfs")
        self.assertTrue(objet["housekeeping"]["tmpfs_alert"])
        self.assertTrue(objet["pressure"]["blocked"])

    def test_cli_essai_par_defaut_apply_refuse_a_un_agent(self):
        pol = menage.Policy()
        cfg_state = self.cfgl
        # L219: same mesh as the CLI (owner file)
        cfg_state.dsn, cfg_state.schema = self.cfg.dsn, self.cfg.schema
        menage.turn_env(cfg_state, pol, "parti", {})
        proc = self.mesh("menage")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DRY RUN", proc.stdout)
        # #4234 : une passe qui dure ne rend plus une sortie vide — la ligne
        # de vie est sur stdout, la progression des phases sur stderr
        self.assertIn("running (dry run", proc.stdout)
        self.assertIn("started (dry run)", proc.stderr)
        self.assertIn("housekeeping: phase", proc.stderr)
        self.assertTrue(os.path.exists(menage.agent_tmp(cfg_state, pol, "parti")))
        proc = self.mesh("menage", "--apply", env=self.env(AGENT_MAIL_NAME="a1"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refus", proc.stderr)
        self.assertTrue(os.path.exists(menage.agent_tmp(cfg_state, pol, "parti")))
        # L219: the CLI's pass, 3 h later (an orphan directory is judged on
        # its mtimes AND ctimes, which no test can set back)
        class _Plus3h:
            def __getattr__(self, name):
                return getattr(time, name)

            def time(self):
                return time.time() + 3 * 3600
        args = argparse.Namespace(apply=True, dry_run=False, json=True, by="human:test")
        out = io.StringIO()
        with mock.patch.object(menage, "time", _Plus3h()), \
                contextlib.redirect_stdout(out):
            self.assertEqual(menage.cmd_menage(self.cfg, args), 0)
        rapport = json.loads(out.getvalue())
        self.assertEqual(rapport["schema"], "ameesh-menage/1")
        self.assertFalse(os.path.exists(menage.agent_tmp(cfg_state, pol, "parti")))
        journal = storage.of(self.db).housekeeping.recent(None, 3600)
        self.assertEqual({r["actor"] for r in journal}, {"human:test"})
        proc = self.mesh("hosts")
        self.assertIn("cleanup    temporary directories", proc.stdout)
        self.assertIn("tmp deleted", proc.stdout)

    def test_l_executeur_redirige_le_tmpdir_du_tour(self):
        cwd = os.path.join(self.tmp, "work", "a1")
        os.makedirs(cwd)
        self.assertEqual(self.register("a1", "claude", cwd=cwd, prompt="un").returncode, 0)
        proc = self.runner("--once", "--agents", "a1",
                           env=self.env(AMEESH_TEST_MAKE_TMP=os.path.join(self.tmp, "x")))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        vu = self.turns()[-1]["env"]
        mine = os.path.join(self.state, ".menage", "tmp", "a1")
        self.assertEqual(vu["TMPDIR"], mine)
        self.assertEqual(vu["AMEESH_TMPDIR"], mine)
        self.assertEqual(vu["npm_config_store_dir"],
                         os.path.join(self.state, ".menage", "cache", "pnpm"))
        self.assertTrue([n for n in os.listdir(mine) if n.startswith("tour-")])


if __name__ == "__main__":
    unittest.main()
