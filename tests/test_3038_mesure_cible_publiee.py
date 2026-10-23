# SPDX-License-Identifier: AGPL-3.0-only
"""#3038 — la taille mesurée se lit sur la cible PUBLIÉE, ou elle doute.

Constat #2999 (23/10) : le lot mesurait `l (654430 lines, 3729 files)` pour un
delta réel de 332 lignes / 4 fichiers. `lot_measures.diff_range` essayait la
cible du lot (`develop`) puis la cible par défaut du clone ; les deux
pouvaient être des branches locales figées (ou `origin/main` d'un clone qui
n'avait pas fetché develop). Quand la fusion n'était ancêtre d'aucune, le
repli `merge-base(cible, sha)..sha` emportait l'histoire entre les deux. Trois
mesures du paquet 128 (#3000, #2960, #2999) se reproduisent exactement sur la
même base périmée `37db32fb` (13/10 14:14).

Témoins :

* un checkout dont la branche locale `develop` est en retard de plusieurs
  jours mesure le delta du lot, pas l'histoire de la cible ;
* une cible illisible (aucune ref publiée, ou ref locale seule alors que le
  clone a un remote) ne produit pas de taille : le doute, jamais le repli ;
* la cible par défaut du clone (`origin/main`) qui ne porte pas la fusion
  n'empêche pas de mesurer sur `origin/develop` qui la porte ;
* une pointe publiée non récupérée (`ls-remote` d'avance sur le clone) fait
  douter au lieu de lire `merge-base` sur une base inconnue.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import lot_measures, storage, work

from .support import PgTestCase


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          env=env)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class PublishedTargetTest(unittest.TestCase):
    """Le delta du lot, lu sur la ref publiée, sans base."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "t")
        self.socle = self.commit("socle.txt")
        # un commit sur la cible APRÈS le socle : une cible locale restée en
        # arrière le ferait entrer dans le diff
        self.base = self.commit("db/retard.sql")
        _git(self.repo, "checkout", "-q", "-b", "lot/2999", "main")
        self.tip = self.commit("docs/preuve.md", "preuve\n" * 2)
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet: #2999", "lot/2999")
        self.merge = _git(self.repo, "rev-parse", "HEAD")

    def commit(self, path, text="x\n"):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(text)
        _git(self.repo, "add", path)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % path)
        return _git(self.repo, "rev-parse", "HEAD")

    def test_une_cible_locale_perimee_mesure_le_delta_du_lot(self):
        """Le clone a `develop` local au socle, `origin/develop` porte la
        fusion : le diff lu est celui du lot (docs/preuve.md), pas l'histoire
        entre le socle et la fusion (#2999 : 654430 lignes pour 332)."""
        _git(self.repo, "update-ref", "refs/heads/develop", self.socle)
        _git(self.repo, "update-ref", "refs/remotes/origin/develop", self.merge)
        self.assertEqual(lot_measures.diff_range(self.repo, self.tip, "develop"),
                         (_git(self.repo, "rev-parse", self.merge + "^1"), self.merge))
        self.assertEqual(lot_measures.diff_stat(self.repo, self.tip, "develop"),
                         {"diff_files": 1, "diff_insertions": 2, "diff_deletions": 0,
                          "measured_size": "s"})

    def test_la_cible_par_defaut_qui_ne_porte_pas_la_fusion_est_remplacee(self):
        """`origin/main` (défaut du clone) est en arrière, `origin/develop`
        porte la fusion : on mesure sur develop, pas contre main."""
        _git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        _git(self.repo, "update-ref", "refs/remotes/origin/develop", self.merge)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/main")
        self.assertEqual(lot_measures.diff_stat(self.repo, self.tip)["diff_files"], 1)

    def test_une_cible_illisible_ne_conclut_pas(self):
        """Aucune ref publiée ne porte la fusion et aucune cible n'est
        déclarée : pas de taille (le repli merge-base était le mensonge)."""
        _git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/main")
        self.assertIsNone(lot_measures.diff_range(self.repo, self.tip))
        self.assertIsNone(lot_measures.diff_stat(self.repo, self.tip))
        # une cible déclarée introuvable est un doute, pas un repli sur main
        self.assertIsNone(lot_measures.diff_stat(self.repo, self.tip, "develop"))

    def test_une_branche_locale_ne_sert_jamais_avec_un_remote(self):
        """#2999 : la branche locale peut être figée ; avec un remote, elle ne
        prouve rien — même quand elle porte la fusion."""
        _git(self.repo, "update-ref", "refs/heads/develop", self.merge)
        _git(self.repo, "remote", "add", "origin", "/nexiste/pas")
        self.assertIsNone(lot_measures.diff_stat(self.repo, self.tip, "develop"))

    def test_la_cible_declaree_du_depot_reste_le_repli(self):
        """`ameesh.target=develop` est la seule cible de repli quand aucune ref
        publiée ne porte le commit (gel pas encore fusionné) : la lecture
        rebase reste possible, sans revenir à `origin/main`."""
        _git(self.repo, "update-ref", "refs/heads/develop", self.socle)
        _git(self.repo, "config", "ameesh.target", "develop")
        out = lot_measures.diff_stat(self.repo, self.tip)
        self.assertIsNotNone(out)
        self.assertEqual(out["diff_files"], 2)   # db/retard.sql + docs/preuve.md

    def test_une_pointe_publiee_non_recuperee_fait_douter(self):
        """`ls-remote` voit une pointe absente du clone : `merge-base` serait
        lu sur une base inconnue, donc aucun diff."""
        _git(self.repo, "remote", "add", "origin", "/nexiste/pas")
        _git(self.repo, "update-ref", "refs/remotes/origin/develop", self.merge)
        with mock.patch.object(lot_measures, "_remote_tip",
                               return_value=("sha", "f" * 40)):
            self.assertIsNone(lot_measures._fresh_target(self.repo, "origin/develop"))
        with mock.patch.object(lot_measures, "_remote_tip",
                               return_value=("error", None)):
            self.assertIsNone(lot_measures._fresh_target(self.repo, "origin/develop"))
        with mock.patch.object(lot_measures, "_remote_tip",
                               return_value=("sha", self.merge)):
            self.assertEqual(lot_measures._fresh_target(self.repo, "origin/develop"),
                             self.merge)
        with mock.patch.object(lot_measures, "_remote_tip",
                               return_value=("local", None)):
            self.assertEqual(lot_measures._fresh_target(self.repo, "origin/develop"),
                             self.merge)

    def test_un_commit_de_fusion_se_mesure_sans_cible(self):
        """Un commit de fusion porte son delta en premier parent : la cible
        n'est pas nécessaire (comportement L157c inchangé)."""
        self.assertEqual(lot_measures.diff_stat(self.repo, self.merge)["diff_files"], 1)

    def test_le_doute_dit_sa_cause(self):
        self.assertIn("published target unreadable",
                      lot_measures.describe_size(
                          {"declared_size": "m", "merge_sha": "a" * 40,
                           "measured_size": None}))
        self.assertIn("merge commit not found",
                      lot_measures.describe_size(
                          {"declared_size": "m", "merge_sha": None,
                           "measured_size": None}))


class MeasurePublishedTargetPgTest(PgTestCase):
    """La mesure stockée lit la cible publiée (#2999)."""

    def test_une_cible_locale_perimee_mesure_le_delta_du_lot(self):
        repo = os.path.join(self.tmp, "depot-3038")

        def write(path, text):
            full = os.path.join(repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(text)

        os.makedirs(repo)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "t")
        write("socle.txt", "socle\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "socle")
        socle = _git(repo, "rev-parse", "HEAD")
        write("db/retard.sql", "retard\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "retard")
        _git(repo, "checkout", "-q", "-b", "lot/3038", "main")
        write("docs/preuve.md", "preuve\n" * 2)
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "preuve")
        tip = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "-q", "--no-ff", "-m", "paquet: #3038", "lot/3038")
        merge = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/heads/develop", socle)
        _git(repo, "update-ref", "refs/remotes/origin/develop", merge)

        lot = work.add(self.db, title="mesure #3038", branch="lot/3038",
                       branch_target="develop")
        lid = int(lot["id"])
        work.move(self.db, lid, "build")
        work.close_merged(self.db, lid, sha=tip)
        rows = lot_measures.measure(self.db, [lid], repos=[repo], ci=None)
        self.assertEqual(len(rows), 1)
        m = lot_measures.view(rows[0])
        self.assertEqual((m["diff_files"], m["diff_insertions"], m["diff_deletions"]),
                         (1, 2, 0))
        self.assertIsNotNone(storage.of(self.db).work.measure(lid))


if __name__ == "__main__":
    unittest.main()
