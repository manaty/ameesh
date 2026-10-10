# SPDX-License-Identifier: AGPL-3.0-only
"""Ménage de ce que les agents créent (lot L73) : dossier temporaire par
agent, caches partagés bornés, worktrees retirés en fin de lot, /tmp signalé
(jamais supprimé), mesure du tmpfs et contre-pression, `ameesh menage`."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest

from ameesh import canon, containers, exploitation, menage, registry, resources, storage, work

from .support import PgTestCase
from .test_l43_perimetre_hote import HOST, _Canons, codes, host

GIB = 1024 ** 3


class _Cfg:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.host = "pc"


def _write(path: str, size: int, age_s: float = 0.0) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"z" * size)
    if age_s:
        moment = time.time() - age_s
        os.utime(path, (moment, moment))


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
        self.assertTrue(st["clean"] and st["pushed"], st)
        _write(os.path.join(wt, "brouillon.txt"), 3)
        st = menage.worktree_state(wt)
        self.assertFalse(st["clean"])
        self.assertIn("non suivi", st["reason"])
        _git("add", ".", cwd=wt)
        _git("commit", "-q", "-m", "travail", cwd=wt)
        st = menage.worktree_state(wt)
        self.assertTrue(st["clean"])
        self.assertFalse(st["pushed"])
        self.assertIn("non poussés", st["reason"])
        # intégré par rebase : le même changement est sur origin/main
        _git("cherry-pick", "lot", cwd=main)
        _git("push", "-q", "origin", "main", cwd=main)
        _git("fetch", "-q", "origin", cwd=wt)
        _git("remote", "set-head", "origin", "main", cwd=main)
        self.assertTrue(menage.worktree_state(wt)["pushed"])
        self.assertFalse(menage.worktree_state(os.path.join(self.base, "absent"))["exists"])


# ==========================================================================
# conteneurs (L73) : étiquetage, liste, suppression des seuls conteneurs d'un tour
# ==========================================================================

class _FauxMoteur:
    binary = "/usr/bin/docker"

    def __init__(self, rows):
        self.rows = rows
        self.removed: list[str] = []

    def list_all(self):
        return [dict(r) for r in self.rows if r["id"] not in self.removed]

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

    def test_le_script_etiquette_run_et_create_seulement(self):
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
        self.assertEqual(docker("rm", "x*"), "[rm][x*]")
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


# ==========================================================================
# avec la base : tour, passage, CLI, alertes
# ==========================================================================

class MenageDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees", "host_resources"):
            self.db.execute("DELETE FROM %s" % table)
        self.cfgl = _Cfg(self.state)
        self.cfgl.host = self.cfg.host

    def _lot(self, title="lot") -> int:
        return int(work.add(self.db, title=title, app="ameesh")["id"])

    def test_tour_enregistre_worktree_signale_tmp_et_vide_a_la_session_neuve(self):
        main = _repo(self.tmp)
        systeme = os.path.join(self.tmp, "tmp-systeme")
        os.makedirs(systeme)
        pol = menage.Policy(orphan_min_size=4000)
        old = os.environ.get("AMEESH_SYSTEM_TMP")
        os.environ["AMEESH_SYSTEM_TMP"] = systeme
        self.addCleanup(lambda: os.environ.__setitem__("AMEESH_SYSTEM_TMP", old or ""))
        mine = menage.turn_env(self.cfgl, pol, "a1", {})["TMPDIR"]
        _write(os.path.join(mine, "de-la-session-precedente"), 100)
        tour = menage.Turn.begin(self.cfgl, pol, "a1", main, new_session=True, base_env={})
        self.assertFalse(os.path.exists(os.path.join(mine, "de-la-session-precedente")))
        self.assertEqual(tour.env["TMPDIR"], mine)
        # pendant le tour : un worktree de relecture et un clone dans /tmp
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
        self.assertEqual([(r["path"], r["agent"], r["lot"]) for r in rows], [(wt, "a1", "7")])
        # session reprise : le dossier temporaire est conservé
        menage.Turn.begin(self.cfgl, pol, "a1", main, new_session=False, base_env={})
        self.assertTrue(os.path.exists(os.path.join(mine, "garde-pour-la-session")))

    def _suivi(self, main: str, nom: str, lot: int | None) -> str:
        wt = os.path.join(self.tmp, nom)
        _git("worktree", "add", "-q", wt, "-b", nom, cwd=main)
        storage.of(self.db).housekeeping.register_worktree(
            host="pc", path=wt, repo=menage.common_dir(main), agent="a1",
            lot=str(lot) if lot else None, turn_id="t", branch=nom, head="")
        return wt

    def test_fin_de_lot_retire_le_propre_garde_le_reste(self):
        main = _repo(self.tmp)
        fini, ouvert = self._lot("fini"), self._lot("ouvert")
        propre = self._suivi(main, "propre", fini)
        sale = self._suivi(main, "sale", fini)
        _write(os.path.join(sale, "wip.txt"), 3)
        attend = self._suivi(main, "attend", ouvert)
        work.close(self.db, fini, abandoned=True)
        pol = menage.Policy()
        essai = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", dry_run=True)
        self.assertTrue(os.path.isdir(propre))
        self.assertEqual(storage.of(self.db).housekeeping.recent("pc", 3600), [])
        self.assertIn("retrait", {p["action"] for p in essai["plan"]})
        menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t")
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
        self.assertEqual([a["path"] for a in alertes], [sale])
        self.assertIn("non suivi", alertes[0]["detail"])

    def test_dossier_d_agent_disparu_et_caches_remis_pendant_un_tour(self):
        registry.upsert(self.db, "a1", harness="claude", host="pc")
        pol = menage.Policy(cache_quota=10)
        menage.turn_env(self.cfgl, pol, "a1", {})
        menage.turn_env(self.cfgl, pol, "parti", {})
        _write(os.path.join(menage.agent_tmp(self.cfgl, pol, "parti"), "f"), 100)
        pnpm = os.path.join(menage.cache_root(self.cfgl, pol), "pnpm", "v3", "f")
        _write(pnpm, 5000, age_s=7200)
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t", busy=True)
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
        self.assertIn("conteneurs 4 lancé(s) par un tour d'ameesh", proc.stdout)
        self.assertIn("docker rm -f c-7", proc.stdout)
        # au tour suivant, seul le conteneur de CE tour est regardé
        seul = menage.reap_containers(moteur, self.db, "pc", only_turn="encours")
        self.assertEqual(seul["entries"], [])
        self.assertIsNone(seul["others"])

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
        self.assertIn("ALERTE", proc.stdout)
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
        menage.turn_env(cfg_state, pol, "parti", {})
        proc = self.mesh("menage")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ESSAI", proc.stdout)
        self.assertTrue(os.path.exists(menage.agent_tmp(cfg_state, pol, "parti")))
        proc = self.mesh("menage", "--apply", env=self.env(AGENT_MAIL_NAME="a1"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refus", proc.stderr)
        self.assertTrue(os.path.exists(menage.agent_tmp(cfg_state, pol, "parti")))
        proc = self.mesh("menage", "--apply", "--json", "--by", "human:test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rapport = json.loads(proc.stdout)
        self.assertEqual(rapport["schema"], "ameesh-menage/1")
        self.assertFalse(os.path.exists(menage.agent_tmp(cfg_state, pol, "parti")))
        journal = storage.of(self.db).housekeeping.recent(None, 3600)
        self.assertEqual({r["actor"] for r in journal}, {"human:test"})
        proc = self.mesh("hosts")
        self.assertIn("ménage     dossiers temporaires", proc.stdout)
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
