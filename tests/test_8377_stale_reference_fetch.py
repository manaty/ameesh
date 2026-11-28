# SPDX-License-Identifier: AGPL-3.0-only
"""#8377 — un gel poussé après le dernier fetch d'un clone déclaré est lu.

Le dépôt de référence d'un hôte (`~/development/manaty/nexlink`, déclaré par
`radar.projects.<app>.repo`) n'est fetché par aucun minuteur. Un gel poussé
après son dernier fetch y est absent : la prévalidation journalisait
« unavailable », la classe de revue (`ameesh review-class`), la porte et la
désignation rendaient un doute (« sensitive », « no repository holds the
frozen head »).

Les témoins fixent la correction à la source : la lecture d'un gel absent
demande le SHA exact à l'origine du clone déclaré (borné), puis le relit.
Aucun ne touche la base.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import lot_class
from ameesh import plan_git
from ameesh import prevalidation_calls as calls
from ameesh import review_designation_activation as act


def _git(root: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", root, *args],
                          capture_output=True, text=True)
    if proc.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _init(root: str, *, bare: bool = False) -> None:
    os.makedirs(root, exist_ok=True)
    args = ["git", "init", "-q"]
    if bare:
        args.append("--bare")
    subprocess.run([*args, root], check=True, capture_output=True)


class StaleReferenceTest(unittest.TestCase):
    """Un clone déclaré périmé sert la tête gelée sans geste manuel."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="t8377-")
        self.addCleanup(self.tmp.cleanup)
        self.origin = os.path.join(self.tmp.name, "origin.git")
        _init(self.origin, bare=True)
        self.seed = os.path.join(self.tmp.name, "seed")
        _init(self.seed)
        _git(self.seed, "config", "user.name", "t")
        _git(self.seed, "config", "user.email", "t@example.invalid")

        self._write("base.txt", "base\n")
        self.base = self._commit("base")
        _git(self.seed, "remote", "add", "origin", self.origin)
        _git(self.seed, "push", "-q", "origin", "HEAD:refs/heads/main")

        # Le clone déclaré reste au dernier fetch : il ne connaît que `base`.
        self.reference = os.path.join(self.tmp.name, "reference")
        subprocess.run(["git", "clone", "-q", self.origin, self.reference],
                       check=True, capture_output=True)
        # La forge, elle, est GitHub : c'est la configuration qui identifie le
        # dépôt, pas l'URL réécrite du banc.
        _git(self.reference, "remote", "set-url", "origin",
             "https://github.com/manaty/nexlink.git")
        _git(self.reference, "config",
             "url.%s.insteadOf" % self.origin,
             "https://github.com/manaty/nexlink.git")

        # La cible avance, puis le lot part de cette cible et gèle sa tête :
        # ni l'un ni l'autre n'est dans le clone de référence.
        self._write("merged.txt", "merged\n")
        self.target = self._commit("la cible avance")
        _git(self.seed, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(self.seed, "checkout", "-q", "-b", "lot/8377", self.target)
        self._write("lot.txt", "lot\n")
        self.frozen = self._commit("le lot")
        _git(self.seed, "push", "-q", "origin", "HEAD:refs/heads/lot/8377")

        self.empty = os.path.join(self.tmp.name, "vide")
        os.makedirs(self.empty)
        self.cfg = types.SimpleNamespace(
            host="ameesh-agents-1",
            radar={"projects": {"nexlink": {
                "repo": self.reference,
                "github": "manaty/nexlink",
                "target": "main"}}})
        self.item = {"id": 8377, "app": "nexlink", "assignee": "nobody",
                     "branch": "lot/8377", "branch_target": "main"}

    def _write(self, name: str, text: str) -> None:
        path = os.path.join(self.seed, name)
        os.makedirs(os.path.dirname(path) or self.seed, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _commit(self, message: str) -> str:
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-qm", message)
        return _git(self.seed, "rev-parse", "HEAD")

    def _has_frozen(self) -> bool:
        proc = subprocess.run(
            ["git", "-C", self.reference, "cat-file", "-e",
             self.frozen + "^{commit}"], capture_output=True)
        return proc.returncode == 0

    def _class(self) -> dict:
        return lot_class.of_lot(
            None, 8377, cfg=self.cfg, item=self.item,
            milestones=[{"kind": "frozen", "sha": self.frozen, "id": 1,
                         "at_ts": 0}],
            canons=[])

    def test_la_porte_fetche_la_tete_poussee_apres_le_dernier_fetch(self):
        self.assertFalse(self._has_frozen())
        with mock.patch("ameesh.db.is_dry_pass", return_value=False), \
                mock.patch("ameesh.registry.get",
                           return_value={"cwd": self.empty}), \
                mock.patch("ameesh.registry.overview", return_value=[]), \
                mock.patch.object(lot_class.os, "getcwd", return_value=self.empty):
            found, commit = lot_class.repo_for_freeze(
                None, self.cfg, self.item, self.frozen)
        self.assertEqual(found, os.path.realpath(self.reference))
        self.assertEqual(commit, self.frozen)
        self.assertTrue(self._has_frozen())

    def test_la_classe_de_revue_lit_le_gel_et_rafraichit_la_cible(self):
        # Sans le fetch, `of_lot` rendait « the freeze … is not a commit of » ;
        # avec lui, la tête et la cible publiée sont lues, et le diff ne
        # contient pas les fusions entrées dans la cible depuis le dernier
        # fetch du clone.
        out = lot_class.of_lot(
            None, 8377, cfg=self.cfg, item=self.item,
            milestones=[{"kind": "frozen", "sha": self.frozen, "id": 1,
                         "at_ts": 0}],
            canons=[])
        self.assertEqual(out["frozen_sha"], self.frozen)
        self.assertEqual(out["base_sha"], self.target)
        self.assertEqual([entry["path"] for entry in out["files"]],
                         ["lot.txt"])
        self.assertNotIn("not a commit", out["why"])
        self.assertIn("no readable canon", out["why"])

    def test_deux_appels_consecutifs_gardent_la_cible_publiee(self):
        # B1 (cx2) : au premier appel la tête est rapatriée, au second elle
        # est déjà locale. La base ne doit pas dépendre du fetch de cet appel :
        # sinon `origin/main`, resté à `base`, rend au lot `merged.txt`, déjà
        # entré dans la cible, et peut le surclasser en sensible.
        first = self._class()
        self.assertEqual(first["frozen_sha"], self.frozen)
        self.assertEqual(first["base_sha"], self.target)
        self.assertEqual([entry["path"] for entry in first["files"]],
                         ["lot.txt"])
        self.assertTrue(self._has_frozen())
        # La lecture de la cible publiée ne déplace aucune ref partagée.
        self.assertEqual(
            _git(self.reference, "rev-parse", "refs/remotes/origin/main"),
            self.base)
        second = self._class()
        self.assertEqual(second["frozen_sha"], self.frozen)
        self.assertEqual(second["base_sha"], self.target)
        self.assertEqual([entry["path"] for entry in second["files"]],
                         ["lot.txt"])

    def test_repo_for_freeze_puis_of_lot_garde_la_cible_publiee(self):
        # B1 (cx2) : `repo_for_freeze` (ou la prévalidation) peut rapatrier la
        # tête avant le premier `of_lot`. Le gel est alors présent d'emblée et
        # la base doit quand même être la cible publiée, jamais `origin/main`.
        self.assertFalse(self._has_frozen())
        with mock.patch("ameesh.db.is_dry_pass", return_value=False), \
                mock.patch("ameesh.registry.get",
                           return_value={"cwd": self.empty}), \
                mock.patch("ameesh.registry.overview", return_value=[]), \
                mock.patch.object(lot_class.os, "getcwd", return_value=self.empty):
            found, commit = lot_class.repo_for_freeze(
                None, self.cfg, self.item, self.frozen)
        self.assertEqual(found, os.path.realpath(self.reference))
        self.assertEqual(commit, self.frozen)
        out = self._class()
        self.assertEqual(out["frozen_sha"], self.frozen)
        self.assertEqual(out["base_sha"], self.target)
        self.assertEqual([entry["path"] for entry in out["files"]],
                         ["lot.txt"])

    def test_un_balayage_epuise_ne_paie_pas_le_fetch(self):
        # #2851 : la passe d'alertes borne ses lectures ; une échéance épuisée
        # laisse le doute d'avant, sans ajouter un délai réseau à la passe.
        from ameesh import review_authors
        self.assertFalse(self._has_frozen())
        with mock.patch("ameesh.db.is_dry_pass", return_value=False), \
                mock.patch("ameesh.registry.get",
                           return_value={"cwd": self.empty}), \
                mock.patch("ameesh.registry.overview", return_value=[]), \
                mock.patch.object(lot_class.os, "getcwd", return_value=self.empty):
            with review_authors.sweep_deadline(0.0, "test"):
                found = lot_class.repo_for_freeze(
                    None, self.cfg, self.item, self.frozen)
        self.assertEqual(found, ("", ""))
        self.assertFalse(self._has_frozen())

    def test_un_hote_sans_radar_lit_le_clone_declare_par_release(self):
        # Le profil d'un exécuteur n'a pas de bloc `radar` : le clone de l'app
        # n'est déclaré que par `release.projects` (ou `packet_watch`). Sans
        # cette piste, `of_lot` rendait « no repository known for the lot » et
        # un doute, alors que le dépôt déclaré portait le gel.
        cfg = types.SimpleNamespace(
            host="ameesh-agents-1", radar={},
            release={"projects": {"nexlink": {
                "repo": self.reference, "target": "main"}}})
        item = dict(self.item, branch_target="main")
        out = lot_class.of_lot(
            None, 8377, cfg=cfg, item=item,
            milestones=[{"kind": "frozen", "sha": self.frozen, "id": 1,
                         "at_ts": 0}],
            canons=[])
        self.assertEqual(out["frozen_sha"], self.frozen)
        self.assertEqual(out["repo"], os.path.realpath(self.reference))
        self.assertEqual(out["base_sha"], self.target)
        self.assertNotIn("no repository known", out["why"])
        self.assertIn("no readable canon", out["why"])

    def test_la_prevalidation_fetche_avant_de_conclure_au_doute(self):
        item = {"app": "nexlink", "assignee": "nobody", "branch": "lot/8377"}
        with mock.patch("ameesh.registry.get", return_value={"cwd": self.empty}), \
                mock.patch.object(calls.os, "getcwd", return_value=self.empty):
            found = calls.repository(self.cfg, None, item, self.frozen)
        self.assertEqual(found, os.path.realpath(self.reference))
        self.assertTrue(self._has_frozen())

    def test_la_designation_fetche_sans_bloc_github(self):
        # Le profil des relecteurs ne déclare pas `github.projects` : le slug
        # se lit sur `radar.projects.<app>.github`, puis sur l'origine du
        # clone déclaré. Sans cette lecture, la désignation rendait
        # « no repository holds the frozen head ».
        with mock.patch.object(act.registry, "get",
                               return_value={"cwd": self.empty}), \
                mock.patch.object(act, "_tracked_worktrees", return_value=[]), \
                mock.patch.object(act.os, "getcwd", return_value=self.empty), \
                mock.patch.object(act, "_frozen_repository", return_value=""), \
                mock.patch.object(act.work, "lot_repository",
                                  side_effect=act.work.WorkError("no issue")):
            found = act._repository(None, self.cfg, self.item, self.frozen)
        self.assertEqual(found, os.path.realpath(self.reference))
        self.assertTrue(self._has_frozen())

    def test_une_ref_symbolique_n_est_jamais_fetchee(self):
        # #2786 : un gel ne nomme jamais `HEAD` ; la lecture ne doit pas
        # résoudre la tête de l'hôte.
        self.assertFalse(plan_git.fetch_frozen(self.reference, "HEAD"))


if __name__ == "__main__":
    unittest.main()
