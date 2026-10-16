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
import tempfile
import time
import unittest
from unittest import mock

from ameesh import (canon, containers, exploitation, menage, platform, registry,
                    resources, storage, work)

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
        self.assertEqual([a["path"] for a in alertes], [sale])
        self.assertIn("untracked", alertes[0]["detail"])

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
