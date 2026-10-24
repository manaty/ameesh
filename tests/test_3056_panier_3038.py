# SPDX-License-Identifier: AGPL-3.0-only
"""#3056 — panier de la relecture #3038 : trois défauts non bloquants.

* une mesure redemandée par identifiant qui doute efface la taille stockée
  (le `coalesce` de `save_measure` gardait la taille fausse des 7 doutes du
  recalcul #3038) ; une passe ordinaire, une forge illisible ou un `git diff`
  qui échoue sur une plage prouvée (relecture cx1) la gardent ;
* la pointe publiée lue par `ls-remote` n'est réutilisée que
  `PUBLISHED_TIP_TTL_S` : `class_at_merge`, hors d'une passe de mesure, ne
  relit plus une pointe périmée du même processus ;
* `class_at_merge` ne dit plus « le dépôt ne contient pas le commit » quand
  le dépôt le porte et que c'est la cible publiée qui est illisible.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from ameesh import lot_measures, review_lot, storage, work

from .support import PgTestCase
from .test_3038_mesure_cible_publiee import _git


def _repo(root):
    """Un dépôt : socle, retard sur main, lot/3056 fusionné par merge."""
    repo = os.path.join(root, "depot-3056")
    os.makedirs(repo)

    def commit(path, text="x\n"):
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(text)
        _git(repo, "add", path)
        _git(repo, "commit", "-q", "-m", "ajoute %s" % path)
        return _git(repo, "rev-parse", "HEAD")

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    socle = commit("socle.txt")
    commit("db/retard.sql")
    _git(repo, "checkout", "-q", "-b", "lot/3056", "main")
    tip = commit("docs/preuve.md", "preuve\n" * 2)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "paquet: #3056", "lot/3056")
    return repo, socle, tip, _git(repo, "rev-parse", "HEAD")


class PublishedTipTtlTest(unittest.TestCase):
    """La pointe publiée ne vit pas plus que sa durée hors d'une passe."""

    def setUp(self):
        self.addCleanup(lot_measures._PUBLISHED_TIPS.clear)
        lot_measures._PUBLISHED_TIPS.clear()

    def test_la_pointe_est_relue_apres_sa_duree(self):
        reads = iter(["%s\trefs/heads/develop\n" % ("a" * 40),
                      "%s\trefs/heads/develop\n" % ("b" * 40)])
        clock = [1000.0]
        with mock.patch.object(lot_measures, "_remotes", return_value=["origin"]), \
                mock.patch.object(lot_measures, "_git",
                                  side_effect=lambda *a, **k: next(reads)) as git, \
                mock.patch.object(lot_measures.time, "monotonic",
                                  side_effect=lambda: clock[0]):
            self.assertEqual(lot_measures._remote_tip("r", "origin", "develop"),
                             ("sha", "a" * 40))
            clock[0] += lot_measures.PUBLISHED_TIP_TTL_S - 1
            self.assertEqual(lot_measures._remote_tip("r", "origin", "develop"),
                             ("sha", "a" * 40))
            self.assertEqual(git.call_count, 1)
            clock[0] += 2
            self.assertEqual(lot_measures._remote_tip("r", "origin", "develop"),
                             ("sha", "b" * 40))
            self.assertEqual(git.call_count, 2)

    def test_une_forge_illisible_recente_rend_le_doute_passager(self):
        with mock.patch.object(lot_measures.time, "monotonic", return_value=50.0):
            lot_measures._PUBLISHED_TIPS[("r", "origin", "develop")] = (
                10.0, ("error", None))
            lot_measures._PUBLISHED_TIPS[("s", "origin", "develop")] = (
                10.0, ("sha", "a" * 40))
            self.assertTrue(lot_measures._forge_unreadable(["r"]))
            self.assertFalse(lot_measures._forge_unreadable(["s"]))
        with mock.patch.object(lot_measures.time, "monotonic",
                               return_value=10.0 + lot_measures.PUBLISHED_TIP_TTL_S):
            self.assertFalse(lot_measures._forge_unreadable(["r"]))


class ClassAtMergeDoubtReasonTest(unittest.TestCase):
    """Le motif du doute nomme la cible publiée, pas un commit manquant."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo, _, self.tip, _ = _repo(self.tmp.name)
        _git(self.repo, "remote", "add", "origin", "/nexiste/pas")

    def klass(self, sha):
        with mock.patch.object(review_lot.work, "milestones", return_value=[]), \
                mock.patch.object(review_lot.lot_class, "of_lot") as classify:
            out = review_lot.class_at_merge(None, 3056, item={"id": 3056},
                                            repo=self.repo, target="develop",
                                            sha=sha)
        classify.assert_not_called()
        return out

    def test_cible_publiee_illisible(self):
        out = self.klass(self.tip)
        self.assertTrue(out["doubt"], out)
        self.assertIn("porte le commit %s" % self.tip[:12], out["why"])
        self.assertIn("aucune cible publiée lisible", out["why"])
        self.assertNotIn("ne contient pas", out["why"])

    def test_commit_absent(self):
        out = self.klass("f" * 40)
        self.assertTrue(out["doubt"], out)
        self.assertIn("ne contient pas le commit", out["why"])


class ExplicitMeasureDoubtPgTest(PgTestCase):
    """Une mesure redemandée qui doute efface la taille ; sinon on la garde."""

    def size(self, lid):
        row = storage.of(self.db).work.measure(lid)
        return (row["diff_files"], row["diff_insertions"], row["diff_deletions"],
                row["measured_size"])

    def test_le_doute_d_une_mesure_redemandee_efface_la_taille(self):
        repo, socle, tip, merge = _repo(self.tmp)
        _git(repo, "update-ref", "refs/remotes/origin/develop", merge)
        lot = work.add(self.db, title="mesure #3056", branch="lot/3056",
                       branch_target="develop")
        lid = int(lot["id"])
        work.move(self.db, lid, "build")
        work.close_merged(self.db, lid, sha=tip)
        lot_measures.measure(self.db, [lid], repos=[repo], ci=None)
        self.assertEqual(self.size(lid), (1, 2, 0, "s"))

        # la forge ne répond pas (ls-remote en échec) : doute passager, la
        # taille reste
        _git(repo, "remote", "add", "origin", "/nexiste/pas")
        _git(repo, "update-ref", "refs/remotes/origin/develop", socle)
        lot_measures.measure(self.db, [lid], repos=[repo], ci=None)
        self.assertEqual(self.size(lid), (1, 2, 0, "s"))

        # aucune cible publiée : doute sans panne de forge
        _git(repo, "update-ref", "-d", "refs/remotes/origin/develop")
        # une passe ordinaire (sans CI, donc reprise) ne fait que compléter
        lot_measures.measure(self.db, repos=[repo], ci=None)
        self.assertEqual(self.size(lid), (1, 2, 0, "s"))
        # la mesure redemandée par identifiant tranche : plus de taille
        lot_measures.measure(self.db, [lid], repos=[repo], ci=None)
        self.assertEqual(self.size(lid), (None, None, None, None))
        self.assertIn("published target unreadable",
                      lot_measures.describe_size(lot_measures.view(
                          storage.of(self.db).work.measure(lid))))

    def measured(self, title):
        repo, socle, tip, merge = _repo(self.tmp)
        _git(repo, "update-ref", "refs/remotes/origin/develop", merge)
        lot = work.add(self.db, title=title, branch="lot/3056",
                       branch_target="develop")
        lid = int(lot["id"])
        work.move(self.db, lid, "build")
        work.close_merged(self.db, lid, sha=tip)
        lot_measures.measure(self.db, [lid], repos=[repo], ci=None)
        self.assertEqual(self.size(lid), (1, 2, 0, "s"))
        return repo, socle, lid

    def test_un_echec_de_git_diff_garde_la_taille(self):
        """Relecture cx1 : clone incomplet, plage prouvée mais `git diff`
        illisible (blob absent) — un échec de lecture, pas un doute."""
        repo, _, lid = self.measured("mesure #3056 clone incomplet")
        blob = _git(repo, "rev-parse", "HEAD:docs/preuve.md")
        os.remove(os.path.join(repo, ".git", "objects", blob[:2], blob[2:]))
        lot_measures.measure(self.db, [lid], repos=[repo], ci=None)
        self.assertEqual(self.size(lid), (1, 2, 0, "s"))

    def test_le_doute_se_decide_avant_la_lecture_de_la_ci(self):
        """Relecture cx1 : l'attente de la CI peut faire expirer l'erreur
        ls-remote en cache ; la forge illisible garde quand même la taille."""
        repo, socle, lid = self.measured("mesure #3056 ci lente")
        _git(repo, "remote", "add", "origin", "/nexiste/pas")
        _git(repo, "update-ref", "refs/remotes/origin/develop", socle)

        def slow_ci(repo_dir, branch):
            lot_measures._PUBLISHED_TIPS.clear()     # le cache a expiré
            return None

        lot_measures.measure(self.db, [lid], repos=[repo], ci=slow_ci)
        self.assertEqual(self.size(lid), (1, 2, 0, "s"))

    def test_sans_depot_qui_porte_le_commit_la_taille_reste(self):
        repo, _, tip, merge = _repo(self.tmp)
        _git(repo, "update-ref", "refs/remotes/origin/develop", merge)
        lot = work.add(self.db, title="mesure #3056 b", branch="lot/3056",
                       branch_target="develop")
        lid = int(lot["id"])
        work.move(self.db, lid, "build")
        work.close_merged(self.db, lid, sha=tip)
        lot_measures.measure(self.db, [lid], repos=[repo], ci=None)
        lot_measures.measure(self.db, [lid], repos=[], ci=None)
        self.assertEqual(self.size(lid), (1, 2, 0, "s"))


if __name__ == "__main__":
    unittest.main()
